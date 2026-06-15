"""
test_decision_pipeline.py — v1.3 决策链路端到端集成测试
========================================================

V1.3 §6.4 决策链路：UserPromptSubmit → PostToolUse 累积 → TodoWrite 强信号 → lock。

测试目标（Stage 5.3 / 5.5）：
  1. dispatch(Edit×3) → 累积 runtime_score → 触发升级 simple→medium 并 lock
  2. dispatch(Edit+TodoWrite) → TodoWrite 强信号立即 lock
  3. 锁后再 dispatch → 决策不再变化（lock-after-decision-doesn't-re-decide）
  4. 缺 seed record → dispatch 不当场重决策
  5. MODEL_ROUTER_V13_DECIDE=0 → dispatch 不触发重决策
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _write_seed_decision(
    project_root: Path,
    sid: str,
    *,
    task_complexity: str = "simple",
    final_model: str = "MiniMax-M3",
    locked: bool = False,
) -> None:
    """预先在 model_router_state_<sid>.json 写入一条 decide() 记录。"""
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
        json.dumps(
            {"version": "1.3", "session_id": sid, "decision": rec},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_decision(project_root: Path, sid: str) -> dict:
    p = project_root / ".claude" / f"model_router_state_{sid}.json"
    return json.loads(p.read_text(encoding="utf-8"))["decision"]


def _read_state(project_root: Path, sid: str) -> dict:
    p = project_root / ".claude" / f"model_router_state_{sid}.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ── 多次 Edit 触发 runtime 升级 ──────────────────────────────────────────

class TestRuntimeAccumulationTriggersUpgrade(unittest.TestCase):
    """多次 Edit 累积 runtime_score → 升级 simple→medium 并 lock。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-pipe-runtime-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_edits_promote_simple_to_medium(self):
        from post_tool_handler import dispatch

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            # 多次 Edit（每次得分足以抬到 medium 阈值）
            for _ in range(15):
                dispatch(self.sid, str(self.root), {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/app/main.py"},
                })

        d = _read_decision(self.root, self.sid)
        # 累积分 > 30 → medium
        self.assertEqual(d["task_complexity"], "medium",
                         f"runtime 累积应升级到 medium，实际={d['task_complexity']}")
        self.assertTrue(d["locked"], "升级后必须 lock")
        self.assertEqual(d["decision_source"], "runtime")


# ── TodoWrite 强信号立即 lock ──────────────────────────────────────────

class TestTodoWriteImmediateLock(unittest.TestCase):
    """TodoWrite 强信号 → 立即 lock（无论 runtime_score）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-pipe-todo-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_implementation_todo_locks_immediately(self):
        from post_tool_handler import dispatch

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            dispatch(self.sid, str(self.root), {
                "tool_name": "TodoWrite",
                "tool_input": {
                    "todos": [
                        {"content": "Implement login", "status": "pending"},
                        {"content": "Add tests", "status": "pending"},
                    ],
                },
            })

        d = _read_decision(self.root, self.sid)
        # 实施类 todo → 至少 medium + 锁
        self.assertIn(d["task_complexity"], ("medium", "complex"),
                      f"实施类 todo 至少抬到 medium，实际={d['task_complexity']}")
        self.assertTrue(d["locked"], "实施类 todo 必须 lock")
        self.assertEqual(d["decision_source"], "todowrite")


# ── Lock 后不再重决策 ──────────────────────────────────────────────────

class TestLockedDoesNotRedecide(unittest.TestCase):
    """已 lock → 后续 dispatch 不再修改 decision。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-pipe-locked-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_complex_survives_subsequent_dispatches(self):
        from post_tool_handler import dispatch

        # 预写一个已锁的 complex record
        _write_seed_decision(
            self.root, self.sid,
            task_complexity="complex",
            final_model="deepseek-v4-pro",
            locked=True,
        )

        d_before = _read_decision(self.root, self.sid)
        ts_before = d_before["last_update"]

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            # 一堆 Edit + TodoWrite 风暴
            for _ in range(20):
                dispatch(self.sid, str(self.root), {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/app/x.py"},
                })
            dispatch(self.sid, str(self.root), {
                "tool_name": "TodoWrite",
                "tool_input": {
                    "todos": [{"content": "Refactor everything", "status": "pending"}],
                },
            })

        d_after = _read_decision(self.root, self.sid)
        self.assertEqual(d_after["task_complexity"], "complex",
                         "已锁 record 不应被降级")
        self.assertEqual(d_after["final_model"], "deepseek-v4-pro")
        self.assertTrue(d_after["locked"])
        # last_update 不应被重写（可能也未变，但 locked 状态必须保留）
        # 不能用 ts 比较：ts 来自 seed，是固定值；lock 后不应改它
        self.assertEqual(d_after["last_update"], ts_before,
                         "lock 后 record 不应被改写")


# ── 缺 seed 时不重决策 ────────────────────────────────────────────────

class TestNoSeedNoRedecide(unittest.TestCase):
    """无 seed record（UserPromptSubmit 还没发生）→ 不当场重决策。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-pipe-noseed-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_session_state_file_no_decision_change(self):
        from post_tool_handler import dispatch

        # 不预写 seed
        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "1"}):
            for _ in range(20):
                dispatch(self.sid, str(self.root), {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/app/x.py"},
                })

        # runtime_score 应被累积（这部分是 Stage 4 行为）
        state = _read_state(self.root, self.sid)
        self.assertIn("runtime_score", state)
        self.assertGreater(state["runtime_score"]["score"], 0)

        # decision 字段应为空或缺失（无 seed → maybe_redecide 不写）
        decision = state.get("decision", {})
        # 空 dict 或缺字段
        is_empty = (
            not decision
            or "task_complexity" not in decision
            or decision.get("task_complexity") is None
        )
        self.assertTrue(is_empty,
                        f"无 seed 时 decision 应当场未初始化，实际={decision}")


# ── Flag 关闭时不重决策 ──────────────────────────────────────────────

class TestDecideFlagOffNoOp(unittest.TestCase):
    """MODEL_ROUTER_V13_DECIDE=0 → 仍观测 runtime_score，但不动 decision。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "sid-pipe-flagoff-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_preserves_decision(self):
        from post_tool_handler import dispatch

        _write_seed_decision(self.root, self.sid, task_complexity="simple")

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_DECIDE": "0"}):
            for _ in range(20):
                dispatch(self.sid, str(self.root), {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/app/x.py"},
                })
            dispatch(self.sid, str(self.root), {
                "tool_name": "TodoWrite",
                "tool_input": {
                    "todos": [{"content": "Implement X", "status": "pending"}],
                },
            })

        d = _read_decision(self.root, self.sid)
        self.assertEqual(d["task_complexity"], "simple",
                         "flag 关闭时 decision 不应被改")
        self.assertFalse(d["locked"], "flag 关闭时不应 lock")
        # 但 runtime_score 仍被观测累积
        state = _read_state(self.root, self.sid)
        self.assertGreater(state["runtime_score"]["score"], 0,
                           "flag 关闭 runtime_score 仍应被观测累积")


if __name__ == "__main__":
    unittest.main()
