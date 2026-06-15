#!/usr/bin/env python3
"""
test_workflow_orchestrator.py — workflow_orchestrator 单元测试
=============================================================

覆盖：
  1. activate(complex)  →  triple plan, current_step=1
  2. activate(medium)   →  triple plan（2026-06-15 升级为三模型编排）
  3. simple             →  None（不激活）
  4. advance 1→2→3      →  越界自动 deactivate
  5. 中途 re-activate   →  保留 current_step，不重置
  6. activate 失败回退  →  写失败/路径异常时返回 None
  7. read_state 无文件  →  None
  8. deactivate 后 read →  None

不依赖 pytest（用 stdlib unittest 即可），方便在 hooks 子目录直接跑。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 确保能 import 同目录模块
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
import workflow_orchestrator as wo  # noqa: E402


class TestWorkflowOrchestrator(unittest.TestCase):
    def setUp(self):
        # 每个测试一个临时 project_root，互不污染
        self.tmpdir = Path(tempfile.mkdtemp(prefix="wf_test_"))
        self.sid = "test-session-001"
        self.root = str(self.tmpdir)
        # 清残留
        wo.deactivate(self.sid, self.root)

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # ── 1. complex → triple plan ─────────────────────────────
    def test_activate_complex_writes_triple(self):
        state = wo.activate("complex", self.sid, self.root)
        self.assertIsNotNone(state)
        self.assertEqual(state["plan_type"], "triple")
        self.assertEqual(state["current_step"], 1)
        self.assertEqual(state["models"],
                         ["deepseek-v4-pro", "MiniMax-M3", "deepseek-v4-pro"])
        self.assertEqual(state["step_stages"], ["plan", "implement", "audit"])
        # 落盘文件已写入
        self.assertTrue(
            (self.tmpdir / ".claude" / f"workflow_step_{self.sid}").exists()
        )

    # ── 2. medium → triple plan（2026-06-15 升级：规划→执行→审计）──
    def test_activate_medium_writes_triple(self):
        state = wo.activate("medium", self.sid, self.root)
        self.assertIsNotNone(state)
        self.assertEqual(state["plan_type"], "triple")
        self.assertEqual(state["current_step"], 1)
        self.assertEqual(len(state["models"]), 3)
        self.assertEqual(state["models"][0], "deepseek-v4-pro")
        self.assertEqual(state["models"][1], "MiniMax-M3")
        self.assertEqual(state["models"][2], "deepseek-v4-pro")

    # ── 3. simple → 不激活 ──────────────────────────────────
    def test_activate_simple_returns_none(self):
        state = wo.activate("simple", self.sid, self.root)
        self.assertIsNone(state)
        # 文件不存在
        self.assertFalse(
            (self.tmpdir / ".claude" / f"workflow_step_{self.sid}").exists()
        )

    # ── 4. advance 1→2→3→自动 deactivate ───────────────────
    def test_advance_three_steps_deactivates(self):
        wo.activate("complex", self.sid, self.root)
        # advance 1: step 1→2
        s1 = wo.advance(self.sid, self.root)
        self.assertIsNotNone(s1)
        self.assertEqual(s1["current_step"], 2)
        # advance 2: step 2→3
        s2 = wo.advance(self.sid, self.root)
        self.assertIsNotNone(s2)
        self.assertEqual(s2["current_step"], 3)
        # advance 3: 越界，文件被删
        s3 = wo.advance(self.sid, self.root)
        self.assertIsNone(s3)
        self.assertIsNone(wo.read_state(self.sid, self.root))
        self.assertFalse(
            (self.tmpdir / ".claude" / f"workflow_step_{self.sid}").exists()
        )

    # ── 5. 中途 re-activate 保留 current_step ───────────────
    def test_reactivate_preserves_step(self):
        wo.activate("complex", self.sid, self.root)
        wo.advance(self.sid, self.root)  # → step 2
        # 用户在 step2 中途触发 simple→complex 复判，重新 activate 不应回到 1
        state = wo.activate("complex", self.sid, self.root)
        self.assertIsNotNone(state)
        self.assertEqual(state["current_step"], 2,
                         "re-activate 必须保留 current_step，不重置")

    # ── 6. 失败回退：只读 project_root ──────────────────────
    def test_activate_with_unwritable_root_falls_back_gracefully(self):
        # 把 .claude 设成只读目录 → 写文件会失败
        claude_dir = self.tmpdir / ".claude"
        claude_dir.mkdir(exist_ok=True)
        # chmod 0o555 阻止 create+write
        os.chmod(claude_dir, 0o555)
        try:
            # 不应抛异常
            state = wo.activate("complex", self.sid, self.root)
            # 写失败时 _with_flock 的 best-effort 分支会返回 data（原读到的 None）→ None
            self.assertIsNone(state)
        finally:
            os.chmod(claude_dir, 0o755)

    # ── 7. read_state 无文件 ────────────────────────────────
    def test_read_state_no_file(self):
        self.assertIsNone(wo.read_state(self.sid, self.root))

    # ── 8. deactivate 之后 read ─────────────────────────────
    def test_deactivate_clears_state(self):
        wo.activate("complex", self.sid, self.root)
        self.assertIsNotNone(wo.read_state(self.sid, self.root))
        wo.deactivate(self.sid, self.root)
        self.assertIsNone(wo.read_state(self.sid, self.root))

    # ── 9. 空 sid 不抛异常 ─────────────────────────────────
    def test_empty_sid_returns_none(self):
        self.assertIsNone(wo.activate("complex", "", self.root))
        self.assertIsNone(wo.read_state("", self.root))
        self.assertIsNone(wo.advance("", self.root))
        # deactivate 即使 sid 为空也不抛
        wo.deactivate("", self.root)

    # ── 10. advance 在无文件时返回 None（不会自动新建）───
    def test_advance_without_activate(self):
        # 没有任何 activate → advance 不应创建文件
        result = wo.advance(self.sid, self.root)
        self.assertIsNone(result)
        self.assertFalse(
            (self.tmpdir / ".claude" / f"workflow_step_{self.sid}").exists()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
