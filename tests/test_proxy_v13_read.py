"""
test_proxy_v13_read.py — v1.3 proxy 读侧切换测试
==================================================

V1.3 §6 / Stage 6: proxy.py 读侧从"旧 9 文件"切换到"model_router_state_<sid>.json"。

引入纯函数 `_v13_resolve_decision(sid, project_root) -> dict | None`：
  - MODEL_ROUTER_V13_READ=1 (默认):
      1. 优先读新格式 model_router_state_<sid>.json
      2. 找不到再 fallback 读旧 9 文件
      3. 都没有 → None
  - MODEL_ROUTER_V13_READ=0:
      1. 走旧 9 文件（v1.2 兼容）
      2. 都没有 → None
  - 新格式与旧格式同时存在: 新格式胜出（v1.3 优先级更高）

测试目标（Stage 6.1 / TDD RED → 6.2 GREEN）：
  1. 仅有新文件 → 解析新格式 decision 字段
  2. 仅有旧文件 → fallback 到 read_legacy()
  3. 两者都有 → 新格式胜出
  4. 都没有 → None
  5. flag=0 → 跳过新格式,直接读旧文件
  6. flag=0 + 仅有新文件 → None（v1.3 路径关闭时不动）
  7. 新格式 JSON 损坏 → fallback 旧文件
  8. 新格式无 decision 字段 → 返回空 dict（不报错）
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ── helpers ────────────────────────────────────────────────────────────────

def _write_new_state(project_root: Path, sid: str, *, decision: dict | None = None) -> None:
    """写入新格式 model_router_state_<sid>.json。"""
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": "1.3",
        "session_id": sid,
        "decision": decision or {},
        "last_update": 1700000000,
    }
    (claude_dir / f"model_router_state_{sid}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_legacy_files(project_root: Path, sid: str, **fields) -> None:
    """写入旧 9 文件（v1.2 格式）。kwargs: stage/model_override/pattern/complexity/..."""
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    # 字段名 → 文件名前缀的映射（与 state_persistence.read_legacy() 一致）
    # v1.2 文件名是短名（model_<sid>），不是 model_override_<sid>
    prefix_map = {"model_override": "model"}
    for key, val in fields.items():
        if val is None:
            continue
        prefix = prefix_map.get(key, key)
        path = claude_dir / f"{prefix}_{sid}"
        if key in ("stage", "model_override"):
            path.write_text(f"{val}\n", encoding="utf-8")
        else:
            path.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")


def _sample_decision(task_complexity: str = "medium", final_model: str = "MiniMax-M3") -> dict:
    return {
        "session_id": "sid",
        "prompt_id": "p-1",
        "task_pattern": "feature",
        "task_complexity": task_complexity,
        "prompt_confidence": 0.9,
        "runtime_score": 0,
        "todo_score": 0,
        "final_model": final_model,
        "locked": True,
        "decision_source": "prompt",
        "last_update": 1700000000,
    }


# ── 场景 1: 仅有新文件 ─────────────────────────────────────────────────────

class TestNewFormatOnly(unittest.TestCase):
    """仅有 model_router_state_<sid>.json → 解析 decision 字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-new-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_decision_from_new_format(self):
        from proxy import _v13_resolve_decision

        _write_new_state(self.root, self.sid, decision=_sample_decision("medium"))

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        self.assertIsNotNone(resolved, "仅有新文件时应能解析")
        self.assertEqual(resolved["task_complexity"], "medium")
        self.assertEqual(resolved["final_model"], "MiniMax-M3")

    def test_resolves_complex_from_new_format(self):
        from proxy import _v13_resolve_decision

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("complex", "deepseek-v4-pro"),
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        self.assertEqual(resolved["task_complexity"], "complex")
        self.assertEqual(resolved["final_model"], "deepseek-v4-pro")
        self.assertTrue(resolved["locked"])


# ── 场景 2: 仅有旧文件 ─────────────────────────────────────────────────────

class TestLegacyFallback(unittest.TestCase):
    """无新文件、有旧文件 → fallback 到 read_legacy()。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-fallback-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_falls_back_to_legacy_files(self):
        from proxy import _v13_resolve_decision

        # 仅写旧 stage_/model_ 文件
        _write_legacy_files(
            self.root, self.sid,
            stage="implement",
            model_override=None,
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # 旧文件无 decision 字段 → 至少要返回非空 dict（v1.2 字段映射）
        self.assertIsNotNone(resolved, "旧文件存在时应 fallback 解析")
        # stage 字段应被吸收
        self.assertIn("stage", resolved)
        self.assertEqual(resolved["stage"], "implement")

    def test_fallback_includes_legacy_model_override(self):
        from proxy import _v13_resolve_decision

        _write_legacy_files(
            self.root, self.sid,
            stage="brainstorm",
            model_override="deepseek-v4-pro",
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        self.assertEqual(resolved.get("model_override"), "deepseek-v4-pro")
        self.assertEqual(resolved.get("stage"), "brainstorm")


# ── 场景 3: 两者都有 → 新格式胜出 ─────────────────────────────────────────

class TestNewFormatWinsOverLegacy(unittest.TestCase):
    """新旧文件共存 → 新格式胜出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-both-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_format_decision_overrides_legacy_stage(self):
        """新文件 decision.task_complexity=complex 覆盖旧 stage_=implement。"""
        from proxy import _v13_resolve_decision

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("complex", "deepseek-v4-pro"),
        )
        _write_legacy_files(
            self.root, self.sid,
            stage="implement",
            model_override="MiniMax-M3",  # 旧值
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # 新格式胜出
        self.assertEqual(resolved["task_complexity"], "complex",
                         "新格式 task_complexity 必须胜出")
        self.assertEqual(resolved["final_model"], "deepseek-v4-pro",
                         "新格式 final_model 必须胜出")


# ── 场景 4: 都没有 → None ──────────────────────────────────────────────────

class TestNoStateFiles(unittest.TestCase):
    """既无新文件也无旧文件 → None。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-empty-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_files_returns_none(self):
        from proxy import _v13_resolve_decision

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        self.assertIsNone(resolved, "无任何文件应返回 None")


# ── 场景 5-6: flag 关闭 → 跳过新格式 ──────────────────────────────────────

class TestFlagOffBypassV13(unittest.TestCase):
    """MODEL_ROUTER_V13_READ=0 → 跳过新格式读侧,仅读旧文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-flagoff-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_reads_legacy_only(self):
        from proxy import _v13_resolve_decision

        # 旧文件存在
        _write_legacy_files(self.root, self.sid, stage="design")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "0"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # flag 关闭 → 仍能解析（旧路径）
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("stage"), "design")

    def test_flag_off_ignores_new_format(self):
        """flag 关闭 + 仅有新文件 → 不解析（视作无决策）。"""
        from proxy import _v13_resolve_decision

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("complex", "deepseek-v4-pro"),
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "0"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # flag 关 → 不读新文件 → 无旧文件 → None
        self.assertIsNone(resolved,
                          "flag 关闭时应忽略新格式,仅看旧文件")


# ── 场景 7: 新格式 JSON 损坏 → fallback ────────────────────────────────────

class TestNewFormatCorruptFallback(unittest.TestCase):
    """新格式 JSON 损坏 → fallback 旧文件,不让单个坏文件阻塞热路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-corrupt-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_new_falls_back_to_legacy(self):
        from proxy import _v13_resolve_decision

        # 写损坏的新文件
        claude_dir = self.root / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / f"model_router_state_{self.sid}.json").write_text(
            "{ this is not valid json",
            encoding="utf-8",
        )
        # 写正确的旧文件
        _write_legacy_files(self.root, self.sid, stage="implement")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # 不应抛错;应 fallback 到旧文件
        self.assertIsNotNone(resolved, "新格式损坏应 fallback 到旧文件")
        self.assertEqual(resolved.get("stage"), "implement")


# ── 场景 8: 新格式无 decision 字段 → 空 dict 不报错 ───────────────────────

class TestNewFormatEmptyDecision(unittest.TestCase):
    """新格式存在但 decision 字段缺失/空 → 返回空 dict,不抛错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-empty-dec-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_decision_field_returns_empty_dict(self):
        from proxy import _v13_resolve_decision

        _write_new_state(self.root, self.sid, decision={})

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_READ": "1"}):
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        # 字段缺失/空 → 返回空 dict,proxy 据此判断"决策未初始化"
        self.assertEqual(resolved, {}, "decision 字段空时应返回空 dict")


# ── Feature flag default 测试 ─────────────────────────────────────────────

class TestFlagDefaultIsOn(unittest.TestCase):
    """MODEL_ROUTER_V13_READ 不设 → 默认 True（开启 v1.3 读侧）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-default-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_flag_reads_new_format(self):
        from proxy import _v13_resolve_decision

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("medium"),
        )

        # 不设 flag → 走默认（开启）
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODEL_ROUTER_V13_READ", None)
            resolved = _v13_resolve_decision(self.sid, str(self.root))

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["task_complexity"], "medium")


# ── 场景 9: read_stage 兜底走 v1.3 决策反推 ─────────────────────────────

class TestV13FallbackInReadStage(unittest.TestCase):
    """read_stage 兜底路径:无 v1.2 stage_ 文件时,从 v1.3 决策 final_model 反推 stage。

    触发场景:Stage 6.2 灰度期,旧 stage_<sid> 文件已删除(Stage 7 完成后),
    proxy 必须能从 model_router_state_<sid>.json 决策中拿到等价的 stage 字符串,
    否则 STAGE_MODELS[stage] 查表会失败,fallback 到 default。

    映射规则(v1.3 → v1.2 渐进期):
      - final_model=deepseek-v4-pro  → stage="decide"  (复杂任务升档)
      - final_model=deepseek-v4-flash → stage="brainstorm"
      - final_model=MiniMax-M3 + task_complexity=complex  → stage="decide"
      - final_model=MiniMax-M3 + task_complexity=medium   → stage="implement"
      - final_model=MiniMax-M3 + task_complexity=simple   → stage="default"

    唯一性约束:STAGE_MODELS 中 deepseek-v4-pro/decide、deepseek-v4-flash/brainstorm
    是 1-1 映射;MiniMax-M3 在 6 个 stage 中复用,需用 task_complexity 二次消歧。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-v13-fb-001"
        # 模拟 active_session 指针指向的 stage_ 文件路径（不必真实存在）
        self.stage_path = self.root / ".claude" / f"stage_{self.sid}"

    def tearDown(self):
        self.tmp.cleanup()

    def test_complex_with_deepseek_v4_pro_maps_to_decide_stage(self):
        from proxy import _resolve_stage_v13

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("complex", "deepseek-v4-pro"),
        )

        result = _resolve_stage_v13(self.stage_path)
        self.assertEqual(result, "decide",
                         "v1.3 complex + deepseek-v4-pro 应反推为 decide stage")

    def test_complex_with_minimax_m3_maps_to_decide_stage(self):
        """task_complexity=complex 是升档信号,即便 final_model=基线也用 decide stage。"""
        from proxy import _resolve_stage_v13

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("complex", "MiniMax-M3"),
        )

        result = _resolve_stage_v13(self.stage_path)
        self.assertEqual(result, "decide",
                         "task_complexity=complex 应映射到 decide stage（升档语义）")

    def test_medium_with_minimax_m3_maps_to_implement_stage(self):
        from proxy import _resolve_stage_v13

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("medium", "MiniMax-M3"),
        )

        result = _resolve_stage_v13(self.stage_path)
        self.assertEqual(result, "implement",
                         "task_complexity=medium 应映射到 implement stage")

    def test_simple_with_minimax_m3_maps_to_default_stage(self):
        from proxy import _resolve_stage_v13

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("simple", "MiniMax-M3"),
        )

        result = _resolve_stage_v13(self.stage_path)
        self.assertEqual(result, "default",
                         "task_complexity=simple 应映射到 default stage")

    def test_deepseek_v4_flash_maps_to_brainstorm_stage(self):
        from proxy import _resolve_stage_v13

        _write_new_state(
            self.root, self.sid,
            decision=_sample_decision("medium", "deepseek-v4-flash"),
        )

        result = _resolve_stage_v13(self.stage_path)
        self.assertEqual(result, "brainstorm",
                         "deepseek-v4-flash 唯一对应 brainstorm stage")

    def test_no_new_state_returns_none(self):
        """无 model_router_state_ 文件 → 不反推(避免猜测),返回 None 让 read_stage 走 default。"""
        from proxy import _resolve_stage_v13

        # 不写任何文件
        result = _resolve_stage_v13(self.stage_path)
        self.assertIsNone(result,
                          "无 v1.3 决策时不应反推,让 read_stage 走兜底 default")

    def test_new_state_empty_decision_returns_none(self):
        """decision 字段为空 dict → 不反推,返回 None。"""
        from proxy import _resolve_stage_v13

        _write_new_state(self.root, self.sid, decision={})

        result = _resolve_stage_v13(self.stage_path)
        self.assertIsNone(result,
                          "decision 为空时不应反推,让 read_stage 走兜底 default")


if __name__ == "__main__":
    unittest.main()
