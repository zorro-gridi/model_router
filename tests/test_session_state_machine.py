"""
test_session_state_machine.py — v1.3 7 态状态机单测
=====================================================

V1.3 §4.1 状态定义 / §6.2 转移规则。

7 态：
  INITIAL → PROMPT_SEEN → PROMPT_PREDICTED → RUNTIME_ACCUMULATING
                                                   ↓              ↓
                                          TODOWRITE_SIGNAL ────→ LOCKED → COMPLETED

合法转移：
  INITIAL       + prompt_received     → PROMPT_SEEN
  PROMPT_SEEN      + decision_made       → PROMPT_PREDICTED
  PROMPT_PREDICTED + tool_started        → RUNTIME_ACCUMULATING
  RUNTIME_ACCUMULATING + tool_used          → RUNTIME_ACCUMULATING  (自循环)
  RUNTIME_ACCUMULATING + todowrite_detected → TODOWRITE_SIGNAL
  RUNTIME_ACCUMULATING + threshold_reached   → LOCKED
  TODOWRITE_SIGNAL + threshold_reached   → LOCKED
  LOCKED         + session_ended        → COMPLETED

非法转移：任何不在上述表中的 (state, event) 组合 → 抛 StateTransitionError。

测试目标（TDD）：
  1. 所有 7 条合法转移全部通过
  2. 非法转移 ≥ 20 用例抛异常（覆盖：跨态跳跃 / 反向 / 终态后 / 事件不存在）
  3. 自循环（RUNTIME_ACCUMULATING + tool_used）可连续多次
  4. 状态常量值不可变（防后续 stage 误改）
"""

import unittest

# TDD: import 即将创建的模块
from session_state_machine import (
    INITIAL,
    PROMPT_SEEN,
    PROMPT_PREDICTED,
    RUNTIME_ACCUMULATING,
    TODOWRITE_SIGNAL,
    LOCKED,
    COMPLETED,
    StateTransitionError,
    transition,
    ALL_STATES,
    VALID_TRANSITIONS,
)


class TestStateConstants(unittest.TestCase):
    """状态常量存在且不可变（防止后续误改）。"""

    def test_all_seven_states_defined(self):
        self.assertEqual(len(ALL_STATES), 7)
        for s in (
            INITIAL,
            PROMPT_SEEN,
            PROMPT_PREDICTED,
            RUNTIME_ACCUMULATING,
            TODOWRITE_SIGNAL,
            LOCKED,
            COMPLETED,
        ):
            self.assertIn(s, ALL_STATES)

    def test_state_values_are_strings(self):
        for s in ALL_STATES:
            self.assertIsInstance(s, str)

    def test_states_are_distinct(self):
        self.assertEqual(len(ALL_STATES), len(set(ALL_STATES)))


class TestValidTransitions(unittest.TestCase):
    """所有 7 条合法转移路径。"""

    # ── 主流程 ──

    def test_initial_to_prompt_seen(self):
        self.assertEqual(
            transition(INITIAL, "prompt_received"), PROMPT_SEEN
        )

    def test_prompt_seen_to_prompt_predicted(self):
        self.assertEqual(
            transition(PROMPT_SEEN, "decision_made"), PROMPT_PREDICTED
        )

    def test_prompt_predicted_to_runtime_accumulating(self):
        self.assertEqual(
            transition(PROMPT_PREDICTED, "tool_started"), RUNTIME_ACCUMULATING
        )

    # ── RUNTIME_ACCUMULATING 分支 ──

    def test_runtime_accumulating_self_loop_on_tool_used(self):
        self.assertEqual(
            transition(RUNTIME_ACCUMULATING, "tool_used"), RUNTIME_ACCUMULATING
        )

    def test_runtime_accumulating_self_loop_idempotent(self):
        """自循环可以连续多次（每次 PostToolUse 触发一次）"""
        state = RUNTIME_ACCUMULATING
        for _ in range(10):
            state = transition(state, "tool_used")
        self.assertEqual(state, RUNTIME_ACCUMULATING)

    def test_runtime_accumulating_to_todowrite_signal(self):
        self.assertEqual(
            transition(RUNTIME_ACCUMULATING, "todowrite_detected"),
            TODOWRITE_SIGNAL,
        )

    def test_runtime_accumulating_to_locked(self):
        self.assertEqual(
            transition(RUNTIME_ACCUMULATING, "threshold_reached"), LOCKED
        )

    # ── TODOWRITE_SIGNAL → LOCKED ──

    def test_todowrite_signal_to_locked(self):
        self.assertEqual(
            transition(TODOWRITE_SIGNAL, "threshold_reached"), LOCKED
        )

    # ── LOCKED → COMPLETED ──

    def test_locked_to_completed(self):
        self.assertEqual(
            transition(LOCKED, "session_ended"), COMPLETED
        )


class TestInvalidTransitions(unittest.TestCase):
    """非法转移必须抛 StateTransitionError。"""

    def _assert_invalid(self, state: str, event: str):
        with self.assertRaises(
            StateTransitionError,
            msg=f"({state!r}, {event!r}) 应该抛 StateTransitionError",
        ):
            transition(state, event)

    # ── INITIAL 不可跳过 prompt_received ──

    def test_initial_rejects_decision_made(self):
        self._assert_invalid(INITIAL, "decision_made")

    def test_initial_rejects_tool_started(self):
        self._assert_invalid(INITIAL, "tool_started")

    def test_initial_rejects_tool_used(self):
        self._assert_invalid(INITIAL, "tool_used")

    def test_initial_rejects_todowrite_detected(self):
        self._assert_invalid(INITIAL, "todowrite_detected")

    def test_initial_rejects_threshold_reached(self):
        self._assert_invalid(INITIAL, "threshold_reached")

    def test_initial_rejects_session_ended(self):
        self._assert_invalid(INITIAL, "session_ended")

    # ── PROMPT_SEEN 必须等 decision ──

    def test_prompt_seen_rejects_tool_started(self):
        self._assert_invalid(PROMPT_SEEN, "tool_started")

    def test_prompt_seen_rejects_threshold_reached(self):
        self._assert_invalid(PROMPT_SEEN, "threshold_reached")

    # ── PROMPT_PREDICTED 不可直接跳 LOCKED/COMPLETED ──

    def test_prompt_predicted_rejects_threshold_reached(self):
        self._assert_invalid(PROMPT_PREDICTED, "threshold_reached")

    def test_prompt_predicted_rejects_session_ended(self):
        self._assert_invalid(PROMPT_PREDICTED, "session_ended")

    # ── RUNTIME_ACCUMULATING 不可逆流 ──

    def test_runtime_accumulating_rejects_prompt_received(self):
        self._assert_invalid(RUNTIME_ACCUMULATING, "prompt_received")

    def test_runtime_accumulating_rejects_decision_made(self):
        self._assert_invalid(RUNTIME_ACCUMULATING, "decision_made")

    # ── TODOWRITE_SIGNAL 不可直接 session_ended ──

    def test_todowrite_signal_rejects_session_ended(self):
        self._assert_invalid(TODOWRITE_SIGNAL, "session_ended")

    # ── LOCKED 不可再做任何操作（除 session_ended）──

    def test_locked_rejects_tool_used(self):
        self._assert_invalid(LOCKED, "tool_used")

    def test_locked_rejects_todowrite_detected(self):
        self._assert_invalid(LOCKED, "todowrite_detected")

    def test_locked_rejects_threshold_reached(self):
        self._assert_invalid(LOCKED, "threshold_reached")

    def test_locked_rejects_prompt_received(self):
        self._assert_invalid(LOCKED, "prompt_received")

    # ── COMPLETED 是终态，拒绝一切事件 ──

    def test_completed_rejects_prompt_received(self):
        self._assert_invalid(COMPLETED, "prompt_received")

    def test_completed_rejects_tool_used(self):
        self._assert_invalid(COMPLETED, "tool_used")

    def test_completed_rejects_threshold_reached(self):
        self._assert_invalid(COMPLETED, "threshold_reached")

    def test_completed_rejects_session_ended(self):
        self._assert_invalid(COMPLETED, "session_ended")

    # ── 不存在的事件 ──

    def test_unknown_event_raises(self):
        self._assert_invalid(INITIAL, "nonexistent_event")

    def test_empty_event_raises(self):
        self._assert_invalid(INITIAL, "")


class TestTransitionIsPure(unittest.TestCase):
    """transition() 必须是纯函数：不读写文件、无副作用。"""

    def test_transition_does_not_import_io(self):
        import sys
        self.assertNotIn("open", transition.__code__.co_names)


class TestErrorContext(unittest.TestCase):
    """StateTransitionError 应包含足够定位信息。"""

    def test_error_message_contains_state_and_event(self):
        with self.assertRaises(StateTransitionError) as ctx:
            transition(INITIAL, "tool_used")
        msg = str(ctx.exception)
        self.assertIn(INITIAL, msg)
        self.assertIn("tool_used", msg)


if __name__ == "__main__":
    unittest.main()
