"""
test_scenario_03_todowrite_implementation.py — E2E 场景 3: 含 TodoWrite 实施
============================================================================

V1.3 §6.4 端到端验证：TodoWrite is_implementation=True 触发强制锁定。

场景：用户 prompt 启动一个功能实施 → decide() 给 medium →
PostToolUse TodoWrite（todos 含 implementation 关键词）→
maybe_redecide 锁定（即使 runtime_score 不足也锁）。
"""

import json
import os
import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from _helpers import (
    HOOKS_DIR_STR,
    assert_decision_shape,
    read_state,
    run_post_tool_handler,
    run_stage_detector,
    setup_temp_project,
)

if HOOKS_DIR_STR not in sys.path:
    sys.path.insert(0, HOOKS_DIR_STR)


class TestScenario03TodoWriteImplementation(unittest.TestCase):
    """TodoWrite is_implementation → force-lock。"""

    def setUp(self):
        self.project_root, self.sid = setup_temp_project()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project_root, ignore_errors=True)

    def test_todowrite_implementation_forces_lock_and_upgrade(self):
        """TodoWrite 含 implementation 关键词 → locked=True + 至少 medium。"""
        # 一个故意模糊的 prompt（预期落到 medium）
        prompt = "帮我新增一个工具函数"

        # 1) 初始 decide()
        stdout, stderr, rc = run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )
        self.assertEqual(rc, 0, f"stage_detector 退出码非零: {rc}\nstderr: {stderr}")

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state)
        decision = state["decision"]
        assert_decision_shape(self, decision)
        self.assertFalse(decision["locked"], "首次 decide() 必须 locked=False")

        # 2) 发一个 TodoWrite，todos 包含 implementation 关键词
        todo_event = {
            "session_id": self.sid,
            "cwd": self.project_root,
            "hook_event_name": "PostToolUse",
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "implement the new utility function", "status": "pending"},
                    {"content": "refactor existing callers", "status": "pending"},
                    {"content": "read the codebase structure", "status": "completed"},
                ]
            },
        }
        _, _, _ = run_post_tool_handler(self.sid, self.project_root, todo_event)

        # 3) 验证：locked=True + decision_source=todowrite
        final_state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(final_state)
        final_decision = final_state["decision"]
        assert_decision_shape(self, final_decision)

        self.assertTrue(
            final_decision["locked"],
            "TodoWrite is_implementation 必须触发 lock",
        )
        self.assertEqual(
            final_decision["decision_source"], "todowrite",
            f"锁来源应是 todowrite，实际: {final_decision['decision_source']}",
        )
        self.assertIn(
            final_decision["task_complexity"],
            ("medium", "complex"),
            f"实施类 TodoWrite 应至少 medium，实际: {final_decision['task_complexity']}",
        )

    def test_non_implementation_todowrite_does_not_force_lock(self):
        """非实施类 TodoWrite（无 implementation 关键词）不应 lock。"""
        prompt = "帮我分析下现有代码结构"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        # TodoWrite 全 completed + 无 implementation 关键词
        todo_event = {
            "session_id": self.sid,
            "cwd": self.project_root,
            "hook_event_name": "PostToolUse",
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "understand the architecture", "status": "completed"},
                    {"content": "review the data flow", "status": "completed"},
                ]
            },
        }
        _, _, _ = run_post_tool_handler(self.sid, self.project_root, todo_event)

        state = read_state(self.sid, self.project_root)
        decision = state["decision"]

        # 已完成的 todo 不含 implementation 关键词 → 不应 lock
        self.assertFalse(
            decision.get("locked", False),
            "非实施类 TodoWrite（无 implementation 关键词）不应 lock",
        )
        self.assertEqual(
            decision.get("decision_source"), "prompt",
            "decision_source 应保持 prompt（未被 todowrite 覆盖）",
        )

    def test_todowrite_signal_persisted_in_state(self):
        """TodoWrite 分析结果应写入 todowrite_signal 字段。"""
        prompt = "改一下代码里那个 bug"
        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        todo_event = {
            "session_id": self.sid,
            "cwd": self.project_root,
            "hook_event_name": "PostToolUse",
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "fix the login bug", "status": "pending"},
                    {"content": "add unit tests", "status": "pending"},
                ]
            },
        }
        run_post_tool_handler(self.sid, self.project_root, todo_event)

        state = read_state(self.sid, self.project_root)
        signal = state.get("todowrite_signal")

        self.assertIsNotNone(signal, "state 应包含 todowrite_signal")
        self.assertIsInstance(signal, dict)
        self.assertTrue(
            signal.get("is_implementation"),
            f"'fix'/'add' 是 implementation 关键词，实际: {signal}",
        )
        self.assertEqual(signal.get("total"), 2)
        self.assertEqual(signal.get("pending"), 2)
        self.assertEqual(signal.get("completed"), 0)
        self.assertGreater(signal.get("complexity_signal", 0), 0)


if __name__ == "__main__":
    unittest.main()
