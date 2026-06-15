"""
todowrite_analyzer.py — v1.3 TodoWrite Analyzer（PostToolUse worker）
======================================================================

V1.3 §8.3 TodoWrite Analyzer / §6 首次 TodoWrite 强信号。

TodoWriteAnalyzer 分析 TodoWrite 工具的输出内容，检测：
  - 是否为"真实实施"信号（新增非 trivial items）
  - 任务模式的复杂度等级
  - 是否触发 todowrite_detected 状态转移信号

与 RuntimeScore 的关系：
  TodoWriteAnalyzer 只负责**分析**（纯文本解析），
  不负责计分。分析结果供 RuntimeTracker 使用。

设计约束：
  - 零 I/O（纯计算）
  - 零依赖（除标准库）
  - 所有异常静默吞掉（返回空结果）
"""

from __future__ import annotations

from typing import Any, Dict


# ── 实现信号关键词 ──────────────────────────────────────────────────────
# 这些动词表示"正在写代码/改代码"，不是"正在读/理解"
_IMPLEMENTATION_KEYWORDS = (
    "implement", "fix", "refactor", "build", "add", "create",
    "write", "debug", "modify", "update", "change", "remove",
    "delete", "replace", "extract", "rename", "move", "merge",
    "optimize", "upgrade", "migrate", "rewrite", "patch",
)


class TodoWriteAnalyzer:
    """TodoWrite 工具输出分析器（Stage 4.2）。

    纯文本解析，零 I/O。分析 todos 列表，判断是否为
    "真实实施"信号并计算复杂度信号。
    """

    def analyze(self, todos: Any) -> Dict[str, Any]:
        """分析 TodoWrite 输出的 todos 列表。

        Args:
            todos: TodoWrite tool_input.todos 的值，应为
                   list[dict]，每个 dict 含 content 和 status 字段。
                   容错：接受 None / str / 非标格式。

        Returns:
            dict:
                - is_implementation: bool — 是否有未完成的实施类 todo
                - total: int — todo 总数
                - pending: int — pending + in_progress 数
                - completed: int — completed 状态数
                - complexity_signal: float — [0, 1] 复杂度信号
        """
        try:
            return self._analyze(todos)
        except Exception:
            return self._empty_result()

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        return {
            "is_implementation": False,
            "total": 0,
            "pending": 0,
            "completed": 0,
            "complexity_signal": 0.0,
        }

    def _analyze(self, todos: Any) -> Dict[str, Any]:
        if not isinstance(todos, list):
            return self._empty_result()

        total = 0
        pending = 0
        completed = 0
        has_impl = False

        for item in todos:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not content or not isinstance(content, str):
                continue

            total += 1
            status = item.get("status", "pending")

            if status in ("pending", "in_progress"):
                pending += 1
                if self._is_implementation(content):
                    has_impl = True
            elif status == "completed":
                completed += 1

        # 复杂度信号：pending 数映射到 [0, 1]
        # 10+ pending → 1.0, 1 pending → 0.1, 0 pending → 0.0
        complexity_signal = min(pending / 10.0, 1.0) if pending > 0 else 0.0

        # 只有存在未完成的实施类 todo 才触发 is_implementation
        is_impl = has_impl and pending > 0

        return {
            "is_implementation": is_impl,
            "total": total,
            "pending": pending,
            "completed": completed,
            "complexity_signal": complexity_signal,
        }

    @staticmethod
    def _is_implementation(content: str) -> bool:
        """检查 todo 内容是否包含实现关键词（大小写不敏感）。"""
        lowered = content.lower()
        return any(kw in lowered for kw in _IMPLEMENTATION_KEYWORDS)
