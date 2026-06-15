"""
test_maybe_redecide.py — v1.3 maybe_redecide() 单测
===================================================

V1.3 §6.4 决策链路端到端：PostToolUse 累积 → maybe_redecide → lock 阈值。

`maybe_redecide(sid, project_root, runtime_score, todowrite_signal)` 语义：
  - 已锁（locked=True）→ 不重决策，返回 None
  - 未锁 + runtime_score < 阈值 → 不重决策，返回 None
  - 未锁 + runtime_score ≥ 阈值 → 重决策（只升不降），写 session_state
  - 未锁 + todowrite_signal.is_implementation=True → 立即锁 + 评估升级
  - 升级规则：只升不降（complex 不降 medium；medium 不降 simple）
  - flag MODEL_ROUTER_V13_DECIDE=0 → no-op，返回 None

TDD：本阶段写失败测试，Stage 5.2 才会让它通过。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ── helpers ────────────────────────────────────────────────────────────────

def _write_seed_decision(
    project_root: Path,
    sid: str,
    *,
    task_complexity: str = "simple",
    final_model: str = "MiniMax-M3",
    locked: bool = False,
) -> None:
    """预先在 model_router_state_<sid>.json 写入一条 decide() 记录，
    模拟 UserPromptSubmit 已经发生过。
    """
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "session_id": sid,
        "prompt_id": "p-seed",
        "task_pattern": "feature",
        "task_complexity": task_complexity,
        "prompt_confidence": 0.9,
        "runtime_score": 0,
        "todo_score": 0,
        "final_model": final_model,
        "locked": locked,
        "decision_source": "prompt",
        "last_update": 1700000000,
    }
    (claude_dir / f"model_router_state_{sid}.json").write_text(
        json.dumps({"version": "1.3", "session_id": sid, "decision": rec},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_decision(project_root: Path, sid: str) -> dict:
    """读 model_router_state_<sid>.json 的 decision 字段。"""
    p = project_root / ".claude" / f"model_router_state_{sid}.json"
    return json.loads(p.read_text(encoding="utf-8"))["decision"]


# ── 已锁场景 ────────────────────────────────────────────────────────────────

class TestAlreadyLocked(unittest.TestCase):
    """已锁 → maybe_redecide 不重决策，返回 None。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-locked-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_record_returns_none(self):
        """已锁记录 → 返回 None，不重决策。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(
            self.root, self.sid,
            task_complexity="medium", locked=True,
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid,
                project_root=str(self.root),
                runtime_score=100,  # 即使高分也不重决策
                todowrite_signal=None,
            )

        self.assertIsNone(result, "已锁必须返回 None")

    def test_locked_complexity_not_demoted_even_with_low_score(self):
        """已锁 + low runtime_score → 不降级（与 locked=False 路径一致）。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(
            self.root, self.sid,
            task_complexity="complex", locked=True,
        )

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            maybe_redecide(
                sid=self.sid,
                project_root=str(self.root),
                runtime_score=0,  # 低分不触发降级
                todowrite_signal=None,
            )

        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "complex",
                         "已锁 record 的 complexity 字段不应被改")


# ── 未锁 + runtime_score 不足 ──────────────────────────────────────────────

class TestBelowThreshold(unittest.TestCase):
    """未锁 + runtime_score < 阈值 → 不重决策，返回 None。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-below-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_score_returns_none(self):
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=0, todowrite_signal=None,
            )

        self.assertIsNone(result)

    def test_low_score_does_not_promote(self):
        """runtime_score=10 远低于 medium 阈值（30+）→ simple 不升级。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=10, todowrite_signal=None,
            )

        self.assertIsNone(result)
        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "simple",
                         "低分不应触发升级")


# ── 未锁 + runtime_score 达阈值 ─────────────────────────────────────────────

class TestAboveThreshold(unittest.TestCase):
    """未锁 + runtime_score 达阈值 → 升级（只升不降）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-above-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_high_score_promotes_simple_to_medium(self):
        """simple + runtime_score ≥ 30 → 升级到 medium。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=50, todowrite_signal=None,
            )

        self.assertIsNotNone(result, "高 runtime_score 应触发重决策")
        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "medium")
        self.assertEqual(d["final_model"], "MiniMax-M3")  # medium → 基线
        self.assertTrue(d["locked"], "升级后应锁定")
        self.assertEqual(d["decision_source"], "runtime")

    def test_high_score_promotes_medium_to_complex(self):
        """medium + runtime_score ≥ 70 → 升级到 complex。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="medium")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=85, todowrite_signal=None,
            )

        self.assertIsNotNone(result)
        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "complex")
        self.assertEqual(d["final_model"], "deepseek-v4-pro")
        self.assertTrue(d["locked"])
        self.assertEqual(d["decision_source"], "runtime")

    def test_does_not_demote_complex(self):
        """complex + 任何 runtime_score 都不应降级。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="complex")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=0, todowrite_signal=None,
            )

        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "complex",
                         "complex 不应被降级")


# ── TodoWrite 强信号 ────────────────────────────────────────────────────────

class TestTodoWriteStrongSignal(unittest.TestCase):
    """TodoWrite 强信号 → 立即 lock + 评估升级。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-todo-001"

    def tearDown(self):
        self.tmp.cleanup()

    def _todo_signal(self, is_impl: bool = True, pending: int = 3) -> dict:
        return {
            "is_implementation": is_impl,
            "total": pending,
            "pending": pending,
            "completed": 0,
            "complexity_signal": min(pending / 10.0, 1.0),
        }

    def test_implementation_todo_promotes_and_locks(self):
        """is_implementation=True → 立即 lock，且至少升级到 medium。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=0,  # 0 分也锁
                todowrite_signal=self._todo_signal(is_impl=True, pending=3),
            )

        self.assertIsNotNone(result)
        d = _read_decision(self.root, self.sid)
        self.assertIn(d["task_complexity"], ("medium", "complex"),
                      "实施类 todo 至少抬升到 medium")
        self.assertTrue(d["locked"])
        self.assertEqual(d["decision_source"], "todowrite")

    def test_non_implementation_todo_does_not_lock(self):
        """is_implementation=False → 不锁（仍允许 runtime 累积升级）。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=0,
                todowrite_signal=self._todo_signal(is_impl=False, pending=3),
            )

        # 没 runtime 升级 + todo 不是实施 → 应返回 None（不锁）
        self.assertIsNone(result)
        d = _read_decision(self.root, self.sid)
        self.assertFalse(d["locked"])
        self.assertEqual(d["task_complexity"], "simple")

    def test_implementation_todo_does_not_demote_complex(self):
        """已 complex + 实施类 todo → 保持 complex（只升不降）。"""
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="complex")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=0,
                todowrite_signal=self._todo_signal(is_impl=True, pending=3),
            )

        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "complex")
        self.assertTrue(d["locked"])


# ── flag 关闭 ──────────────────────────────────────────────────────────────

class TestFlagOffNoOp(unittest.TestCase):
    """MODEL_ROUTER_V13_DECIDE=0 → 整体 no-op。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-flagoff-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_returns_none(self):
        from decision_engine import maybe_redecide

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "0"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=1000,
                todowrite_signal={
                    "is_implementation": True, "total": 3, "pending": 3,
                    "completed": 0, "complexity_signal": 0.3,
                },
            )

        self.assertIsNone(result)
        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "simple",
                         "flag 关闭时 record 不应被改")


# ── session_state 缺失 ─────────────────────────────────────────────────────

class TestMissingSessionState(unittest.TestCase):
    """session_state 文件不存在 → maybe_redecide 优雅降级，返回 None。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-missing-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_session_state_file_returns_none(self):
        from decision_engine import maybe_redecide

        # 不预写 seed
        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            result = maybe_redecide(
                sid=self.sid, project_root=str(self.root),
                runtime_score=100, todowrite_signal=None,
            )

        self.assertIsNone(result,
                          "无 seed record → 不重决策（不应当场 decide）")


if __name__ == "__main__":
    unittest.main()
