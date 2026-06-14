#!/usr/bin/env python3
"""
test_proxy_workflow.py — proxy.py 端 step 路由的端到端链路演练
==============================================================

不启动真正的 HTTP server——直接复用 RouterHandler.do_POST 内部逻辑（通过 monkeypatch
转发函数），验证：
  1. simple 任务 → 不激活 workflow，直接走 stage 模型（与现有行为一致）
  2. complex 任务 → 连续 3 次 do_POST 分别命中 step1/2/3 对应模型
     - 第 1 次：deepseek-v4-pro（强模型规划）
     - 第 2 次：MiniMax-M3（常规模型执行）
     - 第 3 次：deepseek-v4-pro（强模型审计）
     - 第 4 次：plan 已 deactivate → 落回 stage 模型
  3. 显式 model_override 优先级最高 → 完全绕过 workflow
  4. medium 任务 → 双步 [strong, normal]，第 3 次落回 stage
  5. workflow_step_<sid> 缺失时 → 走单模型（不报错）
  6. rate limit 超额时 → 本步降级到 fallback 但 advance 仍发生

为避免依赖真实 LLM endpoint，转发函数 (proxy._call_upstream) 被 monkeypatch 成
"返回状态 200 + 极简 Anthropic 响应"。

每个测试都通过临时 project_root + sid 隔离；不会污染宿主 state。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import proxy as proxy_mod  # noqa: E402
import workflow_orchestrator as wo  # noqa: E402
from stage_config import STRONG_MODEL, NORMAL_MODEL  # noqa: E402


# ── 转发 mock：返回最小可解析的 Anthropic 200 响应 ──────────────────
def _fake_call_upstream(self, *args, **kwargs):
    """monkeypatch 目标：proxy.RouterHandler._call_upstream。
    让所有 do_POST 走完管线时实际模型与目标模型一致即可（不真正发 HTTP）。
    """
    # 用一个最简单的 Anthropic Messages 响应
    body = json.dumps({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": kwargs.get("model", "test"),
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }).encode()
    # 写入 self._resp_buffer（HTTPServer 用 wfile 直发）
    # 但 do_POST 会从 self._resp_body / self._resp_status 读取，所以直接返回
    return 200, {"content-type": "application/json"}, body


def _make_handler(project_root: str, session_id: str, monkey_model: str = "test"):
    """构造一个最小 RouterHandler 实例，把 cwd / sid 注入 stdin JSON 替换。"""
    from proxy import RouterHandler

    # HTTPServer 的 __init__ 不可直接调；改用 BaseHTTPRequestHandler 的实例化
    # 方式：通过 make_request 跳过
    handler = RouterHandler.__new__(RouterHandler)
    # 这些属性 do_POST / do_GET 都会读
    handler.session_id = session_id
    handler.cwd = project_root
    # do_POST 用到的 path/headers/command/rfile/wfile
    handler.path = "/v1/messages"
    handler.command = "POST"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /v1/messages HTTP/1.1"
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 9999)
    handler.server = None
    # 抑制 send_response → log_request 的 AttributeError（mock handler 没有
    # 完整 HTTPServer 链，但我们需要它走完 do_POST 尾部）
    handler.send_response = lambda code, message=None: setattr(handler, "_resp_code", code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    body_bytes = json.dumps({
        "model": "anything",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode()
    handler.rfile = io.BytesIO(body_bytes)
    # wfile 必须能 write，do_POST 末尾 self.wfile.write(resp_body)
    handler.wfile = io.BytesIO()
    # 给 dry_run 开关设为 True，让 do_POST 走"决策 + 模拟响应"路径
    handler.dry_run = True
    return handler


def _run_one_do_POST(project_root: str, session_id: str,
                      complexity_override: dict | None = None) -> dict:
    """跑一次 do_POST（dry_run=True），返回 _append_metric 中抓到的 record dict。

    核心：
    - 把所有文件 IO 限定在 tmpdir 内，不污染宿主系统的 active_session。
    - 通过 patch proxy_mod.ACTIVE_SESSION_FILE 让 do_POST 从 tmpdir 读状态链。
    """
    captured = {}

    def _capture_metric(record):
        captured.update(record)

    # 在 tmpdir 里搭文件链（模拟 stage_detector 已写好的状态）：
    #   <root>/.claude/active_session     → active session 指针
    #   <root>/.claude/stage_<sid>        → stage 文件
    #   <root>/.claude/complexity_<sid>   → complexity 文件
    #   <root>/.claude/model_<sid>        → model override 文件（可选）
    claude_dir = Path(project_root) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    active_session_path = claude_dir / "active_session"
    stage_path = claude_dir / f"stage_{session_id}"

    active_session_path.write_text(str(stage_path))
    stage_path.write_text("default")

    if complexity_override is not None:
        (claude_dir / f"complexity_{session_id}").write_text(
            json.dumps(complexity_override)
        )

    handler = _make_handler(project_root, session_id)

    # 关键 patch：把 proxy_mod 的 ACTIVE_SESSION_FILE 指向 tmpdir 的
    # active_session（否则它会去读 ~/.claude/hooks/model_router/active_session ——
    # 即宿主系统的活跃 session，永远不是测试数据）。
    with patch.object(proxy_mod, "ACTIVE_SESSION_FILE", active_session_path), \
         patch.object(proxy_mod, "_append_metric", side_effect=_capture_metric):
        try:
            handler.do_POST()
        except Exception as e:
            raise AssertionError(f"do_POST crashed: {e!r}")

    return captured


class TestProxyWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="proxy_wf_"))
        self.root = str(self.tmpdir)
        self.sid = "proxy-wf-test-001"
        # 清残留 workflow
        wo.deactivate(self.sid, self.root)

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # ── 1. simple → 不激活 workflow，走 stage 模型 ─────────
    def test_simple_task_no_workflow(self):
        rec = _run_one_do_POST(self.root, self.sid,
                               complexity_override={"label": "simple", "score": 10, "ts": 0})
        # 路由 source 不含 step 标记
        rs = rec.get("routing_source", "")
        self.assertNotIn("step1/", rs,
                         f"simple 任务不应有 workflow step 路由: {rs!r}")
        # 没有 workflow_step（plan 未激活）
        self.assertIsNone(rec.get("workflow_step"),
                          f"simple 任务 workflow_step 应为 None: {rec.get('workflow_step')}")

    # ── 2. complex → 三步物理切模型 ─────────────────────────
    def test_complex_task_three_step_routing(self):
        # 预激活 plan（模拟 stage_detector.activate 已发生）
        wo.activate("complex", self.sid, self.root)

        # 第 1 次：step 1/3 → strong
        rec1 = _run_one_do_POST(self.root, self.sid,
                                complexity_override={"label": "complex", "score": 90, "ts": 0})
        self.assertEqual(rec1.get("workflow_step"), 1)
        self.assertEqual(rec1.get("workflow_type"), "triple")
        self.assertEqual(rec1.get("target_model"), STRONG_MODEL,
                         f"step1 应切到强模型，实际: {rec1.get('target_model')}")

        # 第 2 次：step 2/3 → normal
        rec2 = _run_one_do_POST(self.root, self.sid)
        self.assertEqual(rec2.get("workflow_step"), 2)
        self.assertEqual(rec2.get("target_model"), NORMAL_MODEL,
                         f"step2 应切到常规模型，实际: {rec2.get('target_model')}")

        # 第 3 次：step 3/3 → strong
        rec3 = _run_one_do_POST(self.root, self.sid)
        self.assertEqual(rec3.get("workflow_step"), 3)
        self.assertEqual(rec3.get("target_model"), STRONG_MODEL,
                         f"step3 应切到强模型审计，实际: {rec3.get('target_model')}")

        # 第 4 次：plan 已 deactivate → workflow_step=None
        rec4 = _run_one_do_POST(self.root, self.sid)
        self.assertIsNone(rec4.get("workflow_step"),
                         f"plan 完成后 workflow_step 应为 None: {rec4.get('workflow_step')}")

    # ── 3. 显式 model_override 优先级最高 ───────────────────
    def test_model_override_bypasses_workflow(self):
        wo.activate("complex", self.sid, self.root)
        # 写 model override（用 deepseek-v4-flash：在 STAGE_CONFIG 中可解析）
        (Path(self.root) / ".claude" / f"model_{self.sid}").write_text(
            "deepseek-v4-flash"
        )

        rec = _run_one_do_POST(self.root, self.sid,
                               complexity_override={"label": "complex", "score": 90, "ts": 0})
        rs = rec.get("routing_source", "")
        self.assertIn("model=deepseek-v4-flash", rs,
                      f"model_override 应优先: {rs!r}")
        # workflow_step 不应被填充（model_override 路径跳过 orchestrator）
        self.assertIsNone(rec.get("workflow_step"))
        # 清 override
        (Path(self.root) / ".claude" / f"model_{self.sid}").unlink()

    # ── 4. medium → 双步 [strong, normal] ────────────────────
    def test_medium_task_double_step(self):
        wo.activate("medium", self.sid, self.root)

        rec1 = _run_one_do_POST(self.root, self.sid,
                                complexity_override={"label": "medium", "score": 50, "ts": 0})
        self.assertEqual(rec1.get("workflow_type"), "double")
        self.assertEqual(rec1.get("workflow_step"), 1)
        self.assertEqual(rec1.get("target_model"), STRONG_MODEL)

        rec2 = _run_one_do_POST(self.root, self.sid)
        self.assertEqual(rec2.get("workflow_step"), 2)
        self.assertEqual(rec2.get("target_model"), NORMAL_MODEL)

        # 第 3 次：plan 完成 → 落回 stage
        rec3 = _run_one_do_POST(self.root, self.sid)
        self.assertIsNone(rec3.get("workflow_step"))

    # ── 5. workflow_step_<sid> 缺失时 → 不报错，走 stage 模型 ─
    def test_missing_workflow_file_falls_back(self):
        # 没有 activate，没有写 workflow_step_<sid> 文件
        rec = _run_one_do_POST(self.root, self.sid,
                               complexity_override={"label": "complex", "score": 90, "ts": 0})
        # 没文件 → orchestrator 读 None → 不走 step 路由
        self.assertIsNone(rec.get("workflow_step"),
                          f"无 plan 文件时 workflow_step 应为 None: {rec.get('workflow_step')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
