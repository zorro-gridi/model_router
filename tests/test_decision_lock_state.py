"""
test_decision_lock_state.py — DecisionLock + 状态机集成单测
==============================================================

V1.3 §4.5 / §6.2：DecisionLock 扩展 per-sid 状态追踪，
通过 session_state_machine.transition() 验证转移合法性。

新增能力（附加到现有 DecisionLock 上）：
  - transition(sid, event) → 转移成功返回新状态
  - get_state(sid) → 当前状态（默认 INITIAL）
  - 非法转移抛 StateTransitionError
  - 状态与 lock record 独立（lock 不影响 state transition，
    state transition 也不影响 lock）
"""

import unittest
from unittest.mock import MagicMock

from decision_lock import DecisionLock
from session_state_machine import (
    INITIAL,
    PROMPT_SEEN,
    PROMPT_PREDICTED,
    RUNTIME_ACCUMULATING,
    TODOWRITE_SIGNAL,
    LOCKED,
    COMPLETED,
    StateTransitionError,
)


class TestDecisionLockStateTracking(unittest.TestCase):
    """DecisionLock 上的状态追踪。"""

    def setUp(self):
        self.dl = DecisionLock()

    # ── 初始状态 ──

    def test_get_state_defaults_to_initial(self):
        self.assertEqual(self.dl.get_state("sid-new"), INITIAL)

    def test_get_state_never_returns_none(self):
        self.assertIsNotNone(self.dl.get_state("any-sid"))

    # ── 正常状态流转 ──

    def test_transition_initial_to_prompt_seen(self):
        new_state = self.dl.transition("sid-1", "prompt_received")
        self.assertEqual(new_state, PROMPT_SEEN)
        self.assertEqual(self.dl.get_state("sid-1"), PROMPT_SEEN)

    def test_full_happy_path(self):
        """完整 happy path: INITIAL → ... → COMPLETED。"""
        dl = self.dl
        sid = "sid-happy"
        self.assertEqual(dl.transition(sid, "prompt_received"), PROMPT_SEEN)
        self.assertEqual(dl.transition(sid, "decision_made"), PROMPT_PREDICTED)
        self.assertEqual(dl.transition(sid, "tool_started"), RUNTIME_ACCUMULATING)
        # 几次工具调用
        for _ in range(3):
            self.assertEqual(dl.transition(sid, "tool_used"), RUNTIME_ACCUMULATING)
        self.assertEqual(dl.transition(sid, "todowrite_detected"), TODOWRITE_SIGNAL)
        self.assertEqual(dl.transition(sid, "threshold_reached"), LOCKED)
        self.assertEqual(dl.transition(sid, "session_ended"), COMPLETED)

    def test_runtime_to_locked_direct(self):
        """不经过 TODOWRITE_SIGNAL，直接 threshold_reached。"""
        dl = self.dl
        sid = "sid-direct"
        dl.transition(sid, "prompt_received")
        dl.transition(sid, "decision_made")
        dl.transition(sid, "tool_started")
        self.assertEqual(dl.transition(sid, "threshold_reached"), LOCKED)

    # ── 非法转移 ──

    def test_illegal_transition_raises(self):
        with self.assertRaises(StateTransitionError):
            self.dl.transition("sid-1", "threshold_reached")  # INITIAL → LOCKED 非法

    def test_error_includes_sid_context(self):
        """错误消息应包含 sid 以便排查。"""
        with self.assertRaises(StateTransitionError) as ctx:
            self.dl.transition("my-sid-123", "threshold_reached")
        msg = str(ctx.exception)
        self.assertIn("my-sid-123", msg or "")

    # ── 状态与 lock 解耦 ──

    def test_state_transition_does_not_lock(self):
        """状态转移不触发 lock；lock 需要显式 try_acquire。"""
        self.dl.transition("sid-1", "prompt_received")
        self.assertFalse(self.dl.is_locked("sid-1"))

    def test_lock_does_not_block_state_transition(self):
        """lock 后状态仍可推进（L→COMPLETED 等合法转移）。"""
        self.dl.try_acquire("sid-1", MagicMock())
        self.dl.transition("sid-1", "prompt_received")
        self.assertEqual(self.dl.get_state("sid-1"), PROMPT_SEEN)

    def test_locked_state_still_prevents_illegal_transition(self):
        """锁定了不代表所有转移都合法——只接受合法事件。"""
        dl = self.dl
        sid = "sid-locked"
        dl.try_acquire(sid, MagicMock())
        dl.transition(sid, "prompt_received")
        dl.transition(sid, "decision_made")
        dl.transition(sid, "tool_started")
        dl.transition(sid, "threshold_reached")
        self.assertEqual(dl.get_state(sid), LOCKED)
        # LOCKED 状态下 tool_used 非法
        with self.assertRaises(StateTransitionError):
            dl.transition(sid, "tool_used")

    # ── 独立 sid ──

    def test_different_sids_have_independent_states(self):
        dl = self.dl
        dl.transition("sid-A", "prompt_received")
        dl.transition("sid-A", "decision_made")
        self.assertEqual(dl.get_state("sid-A"), PROMPT_PREDICTED)
        self.assertEqual(dl.get_state("sid-B"), INITIAL)

    # ── force_unlock 不影响状态 ──

    def test_force_unlock_preserves_state(self):
        dl = self.dl
        dl.try_acquire("sid-1", MagicMock())
        dl.transition("sid-1", "prompt_received")
        dl.force_unlock("sid-1")
        self.assertFalse(dl.is_locked("sid-1"))
        # 状态不受 force_unlock 影响
        self.assertEqual(dl.get_state("sid-1"), PROMPT_SEEN)


if __name__ == "__main__":
    unittest.main()
