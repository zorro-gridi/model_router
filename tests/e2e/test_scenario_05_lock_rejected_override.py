"""
test_scenario_05_lock_rejected_override.py — E2E 场景 5: 锁后用户改主意应被拒
===============================================================================

V1.3 §6.4 端到端验证：Decision Lock 语义 — 一旦 `locked=True`，
新 UserPromptSubmit 不能覆写 locked 决策。

场景：
  1. 用户发 prompt → decide() → PostToolUse 累积 → maybe_redecide() lock
  2. 用户发一个**完全不同**的新 prompt（改主意了）
  3. 验证：locked 决策不受影响（final_model、decision_source、locked 全不变）

设计语义：
  - lock = "当前 session 的模型选择已确定，不再因 prompt 变化而切换"
  - 等价于"已经给这个任务分配了强模型，用户突然问另一个问题，也别降级"

TDD: 本测试是 RED 起点 — 暴露 decide() 无条件重算 + store.write()
覆写 locked decision 的生产 bug。
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


# ── Tool sequence to trigger todoWrite lock ──────────────────────────────
# 先用 ~model 锁定 deepseek-v4-pro，再发 TodoWrite implementation 强化 lock。
# 然后用户发一个完全不同的 simple prompt，验证锁不被覆写。

# Tool sequence: enough tools + TodoWrite to trigger lock
_LOCK_TOOL_SEQUENCE: list[tuple[str, dict]] = [
    ("TodoWrite", {
        "todos": [
            {"content": "implement the new API endpoint", "status": "in_progress"},
            {"content": "write unit tests for the endpoint", "status": "pending"},
            {"content": "update API documentation", "status": "pending"},
        ]
    }),
]


class TestScenario05LockRejectedOverride(unittest.TestCase):
    """锁后用户改主意：locked 决策不应被新 prompt 覆写。"""

    def setUp(self):
        self.project_root, self.sid = setup_temp_project()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project_root, ignore_errors=True)

    # ── Test 1: lock 后新 prompt 不覆写 final_model ──────────────────────

    def test_locked_decision_survives_new_prompt(self):
        """
        lock 后用户发新 prompt → final_model 仍为 locked 模型。

        Step 1: ~model ds-v4-pro + implementation TodoWrite → lock deepseek-v4-pro
        Step 2: 用户改主意发 simple QA prompt
        Step 3: 验证 final_model 仍是 deepseek-v4-pro（而非 MiniMax-M3）
        """
        # ── Step 1A: 初始 prompt ──
        prompt1 = "~model ds-v4-pro 帮我实现一个用户认证系统"
        run_stage_detector(
            prompt=prompt1, sid=self.sid, project_root=self.project_root,
        )
        state1 = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state1)
        d1 = state1["decision"]
        assert_decision_shape(self, d1)
        self.assertEqual(d1["final_model"], "deepseek-v4-pro")
        self.assertFalse(d1["locked"], "首次 decide() 不应 locked")

        # ── Step 1B: TodoWrite implementation → trigger lock ──
        for tool_name, tool_input in _LOCK_TOOL_SEQUENCE:
            event = {
                "session_id": self.sid,
                "cwd": self.project_root,
                "hook_event_name": "PostToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            run_post_tool_handler(self.sid, self.project_root, event)

        state_locked = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state_locked)
        d_locked = state_locked["decision"]
        assert_decision_shape(self, d_locked)

        # 验证 lock 已生效
        self.assertTrue(
            d_locked["locked"],
            "implementation TodoWrite 后必须 locked=True",
        )
        self.assertEqual(
            d_locked["final_model"], "deepseek-v4-pro",
            "locked 状态 final_model 应为 deepseek-v4-pro",
        )

        # ── Step 2: 用户改主意发 simple QA prompt ──
        prompt2 = "什么是 Python 装饰器？不用写代码，告诉我概念就行"
        run_stage_detector(
            prompt=prompt2, sid=self.sid, project_root=self.project_root,
        )
        state_final = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state_final)
        d_final = state_final["decision"]
        assert_decision_shape(self, d_final)

        # ── Step 3: 锁不破 ──
        self.assertEqual(
            d_final["final_model"], "deepseek-v4-pro",
            "locked 决策不应被 simple prompt 覆写为 MiniMax-M3；"
            f"实际 final_model={d_final['final_model']}",
        )
        self.assertTrue(
            d_final["locked"],
            "lock 不应被新 prompt 解除",
        )
        # decision_source 应保持 lock 来源（todowrite），不被 prompt 覆写
        self.assertNotEqual(
            d_final["decision_source"], "prompt",
            "locked 后 decision_source 不应退回到 'prompt'；"
            f"实际: {d_final['decision_source']}",
        )

    # ── Test 2: lock 后 ~model override 仍可手动切换 ────────────────────

    def test_locked_decision_can_be_manually_overridden(self):
        """
        lock 后用户发 ~model mm3 → 显式覆盖**应该**能改模型。

        这与 Test 1 的区别：~model 是用户**显式**的覆盖意图，
        lock 不应阻止用户的显式指令。
        """
        # Setup: lock deepseek-v4-pro
        prompt1 = "~model ds-v4-pro 帮我重构数据库层"
        run_stage_detector(
            prompt=prompt1, sid=self.sid, project_root=self.project_root,
        )
        for tool_name, tool_input in _LOCK_TOOL_SEQUENCE:
            event = {
                "session_id": self.sid,
                "cwd": self.project_root,
                "hook_event_name": "PostToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            run_post_tool_handler(self.sid, self.project_root, event)
        state_locked = read_state(self.sid, self.project_root)
        self.assertTrue(state_locked["decision"]["locked"])

        # User explicitly switches model
        prompt2 = "~model mm3 换回轻量模型，刚才那个先不做了"
        run_stage_detector(
            prompt=prompt2, sid=self.sid, project_root=self.project_root,
        )
        state_final = read_state(self.sid, self.project_root)
        d_final = state_final["decision"]

        # ~model 显式覆盖必须生效
        self.assertEqual(
            d_final["final_model"], "MiniMax-M3",
            "~model 显式覆盖在 locked 状态下仍应生效",
        )
        self.assertEqual(
            d_final["decision_source"], "explicit",
            "~model 覆盖后 decision_source 应为 'explicit'",
        )


if __name__ == "__main__":
    unittest.main()
