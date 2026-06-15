"""
session_state_machine.py — v1.3 7 态决策状态机
================================================

V1.3 §4.1 状态定义 / §6.2 转移规则。

全程纯函数，零 I/O。由 decision_engine / decision_lock /
PostToolUse hook 调用，确保决策生命周期不可逆。

7 态（有序）：
  INITIAL → PROMPT_SEEN → PROMPT_PREDICTED → RUNTIME_ACCUMULATING
                                                   ↓              ↓
                                          TODOWRITE_SIGNAL ────→ LOCKED → COMPLETED

合法转移表：见 VALID_TRANSITIONS。
非法转移一律抛 StateTransitionError（包含 state/event 上下文）。
"""

from __future__ import annotations

# ── 状态常量 ─────────────────────────────────────────────────────────────────

INITIAL = "INITIAL"
PROMPT_SEEN = "PROMPT_SEEN"
PROMPT_PREDICTED = "PROMPT_PREDICTED"
RUNTIME_ACCUMULATING = "RUNTIME_ACCUMULATING"
TODOWRITE_SIGNAL = "TODOWRITE_SIGNAL"
LOCKED = "LOCKED"
COMPLETED = "COMPLETED"

ALL_STATES: tuple[str, ...] = (
    INITIAL,
    PROMPT_SEEN,
    PROMPT_PREDICTED,
    RUNTIME_ACCUMULATING,
    TODOWRITE_SIGNAL,
    LOCKED,
    COMPLETED,
)

# ── 转移表 ───────────────────────────────────────────────────────────────────
#
# dict[state, dict[event, next_state]]
# 不在表中的 (state, event) 组合一律视为非法转移。

_VALID: dict[str, dict[str, str]] = {
    INITIAL: {
        "prompt_received": PROMPT_SEEN,
    },
    PROMPT_SEEN: {
        "decision_made": PROMPT_PREDICTED,
    },
    PROMPT_PREDICTED: {
        "tool_started": RUNTIME_ACCUMULATING,
    },
    RUNTIME_ACCUMULATING: {
        "tool_used": RUNTIME_ACCUMULATING,
        "todowrite_detected": TODOWRITE_SIGNAL,
        "threshold_reached": LOCKED,
    },
    TODOWRITE_SIGNAL: {
        "threshold_reached": LOCKED,
    },
    LOCKED: {
        "session_ended": COMPLETED,
    },
    # COMPLETED 是终态，无出边
}


def _build_public_transitions() -> dict[str, dict[str, str]]:
    """构造公开的只读转移表（深拷贝以防外部误改）。"""
    return {s: dict(events) for s, events in _VALID.items()}


VALID_TRANSITIONS: dict[str, dict[str, str]] = _build_public_transitions()


# ── 异常 ─────────────────────────────────────────────────────────────────────

class StateTransitionError(ValueError):
    """非法状态转移。

    当 transition() 收到不在转移表中的 (state, event) 时抛出。
    """

    def __init__(self, state: str, event: str) -> None:
        self.state = state
        self.event = event
        super().__init__(
            f"非法状态转移：{state!r} + {event!r}。"
            f"当前状态 {state!r} 不接受事件 {event!r}。"
        )


# ── 公开 API ─────────────────────────────────────────────────────────────────

def transition(state: str, event: str) -> str:
    """执行状态转移，返回新状态。

    Args:
        state: 当前状态（必须是 ALL_STATES 之一）。
        event: 触发事件（字符串）。

    Returns:
        新状态。

    Raises:
        StateTransitionError: 非法的 (state, event) 组合。
    """
    next_map = _VALID.get(state)
    if next_map is None:
        raise StateTransitionError(state, event)
    next_state = next_map.get(event)
    if next_state is None:
        raise StateTransitionError(state, event)
    return next_state
