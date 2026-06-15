"""
context_summary.py — v1.3 Context Summary Injector（§11 锦上添花）
====================================================================

V1.3 §11：当模型从低配升级到高配时，生成简洁上下文摘要并注入 session state。

§11.2 摘要内容：
  - 当前任务目标（来自原始 prompt）
  - 已读文件数量和类型
  - 已发生的关键编辑
  - 测试结果
  - 当前推断的任务复杂度
  - 已完成的里程碑

§11.3 注入时机：
  - 只在复杂度从低档跃迁到高档那一刻注入一次
  - 之后固定模型，不重复注入

设计约束：
  - 零 I/O（纯函数，读取传入的 state 即可）
  - 幂等：同一升级只生成一次（写入 session state 标记）
  - 异常静默吞掉（不影响核心路由）
"""

from __future__ import annotations

import time
from typing import Any, Optional


# ── 复杂度跃迁判定（V1.3 §11.3）────────────────────────────────────────
_COMPLEXITY_RANK: dict[str, int] = {
    "simple": 0,
    "medium": 1,
    "complex": 2,
}


def _is_upgrade(prev: Optional[str], curr: Optional[str]) -> bool:
    """判断是否发生"复杂度从低档跃迁到高档"。

    升级窗口：
      - simple → medium
      - simple → complex
      - medium → complex

    同级保持或降级 → 不算升级。
    """
    if not curr:
        return False
    if not prev:
        # 首次决策 → 不算升级（避免冷启动就注入）
        return False
    return _COMPLEXITY_RANK.get(curr, 0) > _COMPLEXITY_RANK.get(prev, 0)


def _is_jump_upgrade(prev: Optional[str], curr: Optional[str]) -> bool:
    """判断是否"显著跃迁"（跨档升级，如 simple→complex 或 medium→complex）。

    §11.3 "复杂度从低档跃迁到高档" 暗示跨档跃迁，单档提升（simple→medium）
    视为常规升级，仅跨档才注入。
    """
    if not _is_upgrade(prev, curr):
        return False
    return _COMPLEXITY_RANK.get(curr, 0) - _COMPLEXITY_RANK.get(prev, 0) >= 2


class ContextSummaryInjector:
    """V1.3 §11 Context Summary Injector。

    在模型升级（特别是跨档升级）那一刻生成上下文摘要。
    同一 session 同一升级窗口内仅注入一次。
    """

    # ── Public API ─────────────────────────────────────────────────────

    def build_summary(
        self,
        state: dict[str, Any],
        prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """根据 session state 构建上下文摘要。

        Args:
            state: model_router_state_<sid>.json 的内容。
            prompt: 原始用户 prompt（可选，用于摘要"当前任务目标"）。

        Returns:
            dict — 摘要内容，含 §11.2 所有字段。
        """
        try:
            return self._build(state, prompt)
        except Exception:
            return self._empty_summary()

    def should_inject(
        self,
        state: dict[str, Any],
        new_complexity: str,
    ) -> bool:
        """判断是否应当注入摘要（V1.3 §11.3）。

        条件：
          1. 发生跨档升级（simple→complex 或 medium→complex）
          2. session 尚未注入过摘要（避免重复）
        """
        try:
            decision = state.get("decision", {}) or {}
            prev_complexity = decision.get("task_complexity")
            already_injected = state.get("context_summary", {}).get("injected", False)

            if already_injected:
                return False
            return _is_jump_upgrade(prev_complexity, new_complexity)
        except Exception:
            return False

    def mark_injected(
        self,
        state: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        """标记摘要已注入，避免重复。

        Args:
            state: 当前 session state（会就地更新 context_summary 字段）。
            summary: build_summary() 返回的摘要内容。

        Returns:
            更新后的 state dict。
        """
        summary["injected"] = True
        summary["injected_at"] = int(time.time())
        state["context_summary"] = summary
        return state

    # ── Internal ───────────────────────────────────────────────────────

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "task_goal": "",
            "files_read": {"count": 0, "types": []},
            "key_edits": [],
            "test_results": "",
            "current_complexity": "simple",
            "milestones": [],
            "injected": False,
        }

    def _build(
        self,
        state: dict[str, Any],
        prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        # ── 当前任务目标 ──
        task_goal = ""
        if prompt:
            # 取 prompt 前 100 字符作为目标摘要
            task_goal = prompt.strip().replace("\n", " ")[:100]
        if not task_goal:
            task_goal = state.get("task_goal", "")

        # ── 已读文件数量和类型 ──
        runtime_events = state.get("runtime_score", {}).get("events", []) or []
        files_read_types: dict[str, int] = {}
        key_edits: list[str] = []
        for evt in runtime_events:
            tool = evt.get("tool", "")
            file_type = evt.get("file_type", "")
            if tool == "Read" and file_type:
                files_read_types[file_type] = files_read_types.get(file_type, 0) + 1
            elif tool in ("Edit", "Write", "MultiEdit"):
                # 收集关键编辑（仅前 5 个避免膨胀）
                if len(key_edits) < 5:
                    file_type = file_type or "?"
                    key_edits.append(f"{tool}({file_type})")

        # ── 测试结果（从 runtime_signal 推断）──
        test_results = "未运行测试"
        for evt in runtime_events:
            signal = evt.get("runtime_signal", "")
            if signal == "test_failure":
                test_results = "测试失败"
                break
            elif signal == "bash_nonzero_exit":
                test_results = "检测到非零退出码"
                break

        # ── 当前推断的任务复杂度 ──
        decision = state.get("decision", {}) or {}
        current_complexity = decision.get("task_complexity", "simple")

        # ── 已完成的里程碑（从 runtime_score.events 推断）──
        milestones: list[str] = []
        edit_count = sum(1 for e in runtime_events if e.get("tool") in ("Edit", "Write", "MultiEdit"))
        read_count = sum(1 for e in runtime_events if e.get("tool") == "Read")
        if read_count > 0:
            milestones.append(f"已读取 {read_count} 个文件")
        if edit_count > 0:
            milestones.append(f"已编辑 {edit_count} 次")
        # 检测 TodoWrite 完成
        todo_signal = state.get("todowrite_signal", {}) or {}
        if todo_signal.get("completed", 0) > 0:
            milestones.append(f"TodoWrite 已完成 {todo_signal['completed']} 项")

        return {
            "task_goal": task_goal,
            "files_read": {
                "count": sum(files_read_types.values()),
                "types": sorted(files_read_types.keys()),
            },
            "key_edits": key_edits,
            "test_results": test_results,
            "current_complexity": current_complexity,
            "milestones": milestones,
            "injected": False,
        }
