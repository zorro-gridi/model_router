"""
runtime_score.py — v1.3 Runtime Complexity Score 累积器（Per-Prompt）
======================================================================

V1.3 §7 Runtime Complexity Score / §4.2 Per-Prompt 追踪。

纯内存计分器，零 I/O。PostToolUse hook 每次触发时调用
`accumulate(event)` 累积复杂度分。Stage 2 提供核心逻辑，
Stage 4 接入实际 hook。

V1.3 §4.2 设计规则：
  - 用户发出 prompt 后，以首次 tool call 开始计时
  - 1 分钟窗口内累积所有 tool call 的加权分数
  - 窗口过期后不再累积（window_expired=True）
  - 跟踪每 prompt 的 raw tool 调用次数（tool_counts）
  - 切换 prompt 时存档旧数据到 prompt_history

设计约束：
  - 纯函数：不读文件、不写文件、不发起网络
  - 权重可注入：`RuntimeScore(weights=custom)` 覆盖默认值
  - 事件日志：记录每次 accumulate 的 delta，可序列化
  - 幂等：相同 event 始终产生相同 delta
"""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# V1.3 §4.2：tool call 统计窗口（秒）
_WINDOW_SECONDS: int = 60


# 延迟导入避免 stage_config 缺失时连环崩
# V1.3 §3.4 / §7.1：权重支持 YAML 自定义配置，YAML 优先，硬编码兜底
def _load_default_weights() -> dict[str, dict[str, int]]:
    try:
        from stage_config import get_weights
        return get_weights()
    except (ImportError, Exception):
        from stage_config import _PLACEHOLDER_WEIGHTS
        return _PLACEHOLDER_WEIGHTS


class RuntimeScore:
    """纯内存运行时复杂度计分器（V1.3 §7），per-prompt + 1 分钟窗口。

    V1.3 §4.2 新增：
      - current_prompt_id：当前活跃 prompt
      - window_start / window_expired：1 分钟计时窗口
      - tool_counts：每工具 raw 调用次数
      - prompt_history：已完结 prompt 的运行时摘要
    """

    def __init__(
        self,
        weights: Optional[dict[str, dict[str, int]]] = None,
    ) -> None:
        self._score: int = 0
        self._events: list[dict[str, Any]] = []
        defaults = _load_default_weights()
        if weights is not None:
            # 自定义权重覆盖默认值（deep merge：per-category overlay）
            merged: dict[str, dict[str, int]] = {}
            for cat in defaults:
                merged[cat] = dict(defaults[cat])
                if cat in weights:
                    merged[cat].update(weights[cat])
            # 允许自定义引入新类别
            for cat in weights:
                if cat not in merged:
                    merged[cat] = dict(weights[cat])
            self._weights = merged
        else:
            self._weights = defaults

        # V1.3 §4.2 per-prompt 属性
        self._current_prompt_id: str = ""
        self._window_start: float | None = None
        self._window_expired: bool = False
        self._tool_counts: dict[str, int] = {}
        self._prompt_history: dict[str, dict[str, Any]] = {}

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def score(self) -> int:
        return self._score

    @property
    def events(self) -> list[dict[str, Any]]:
        """返回事件日志的浅拷贝，防止外部修改内部状态。"""
        return list(self._events)

    @property
    def current_prompt_id(self) -> str:
        return self._current_prompt_id

    @current_prompt_id.setter
    def current_prompt_id(self, val: str) -> None:
        self._current_prompt_id = val

    @property
    def window_start(self) -> float | None:
        return self._window_start

    @property
    def window_expired(self) -> bool:
        return self._window_expired

    @property
    def tool_counts(self) -> dict[str, int]:
        return dict(self._tool_counts)

    @property
    def prompt_history(self) -> dict[str, dict[str, Any]]:
        return dict(self._prompt_history)

    # ── 公开 API ──────────────────────────────────────────────────────────

    def start_prompt(self, prompt_id: str) -> None:
        """开始新 prompt 追踪：归档旧数据到 prompt_history，重置当前状态。

        Args:
            prompt_id: 新 prompt 的 ID（由 stage_detector 创建）。
        """
        # 归档当前 prompt（如果有数据）
        if self._current_prompt_id and (self._score > 0 or self._events):
            complexity_label = self._label_from_score(self._score)
            self._prompt_history[self._current_prompt_id] = {
                "score": self._score,
                "tool_count": sum(self._tool_counts.values()),
                "window_expired": self._window_expired,
                "window_start": self._window_start,
                "complexity_label": complexity_label,
                "ended_at": int(time.time()),
            }

        # 重置当前状态
        self._current_prompt_id = prompt_id
        self._score = 0
        self._events = []
        self._window_start = None
        self._window_expired = False
        self._tool_counts = {}

    def is_window_expired(self, now: float | None = None) -> bool:
        """检查当前 prompt 的 1 分钟窗口是否已过期。

        Args:
            now: 当前时间戳（可选，默认 time.time()）。

        Returns:
            True 如果窗口已过期或从未启动（首次 tool call 前也算已过期）。
        """
        if self._window_expired:
            return True
        if self._window_start is None:
            return True  # 窗口未启动（首次 tool call 前）
        now = now or time.time()
        expired = (now - self._window_start) > _WINDOW_SECONDS
        if expired:
            self._window_expired = True
        return expired

    def start_window(self, now: float | None = None) -> None:
        """标记首次 tool call 时间（幂等：已有值则不重复设）。

        Args:
            now: 当前时间戳（可选，默认 time.time()）。
        """
        if self._window_start is None:
            self._window_start = now or time.time()
            self._window_expired = False

    def accumulate(
        self,
        event: dict[str, Any],
        prompt_id: str = "",
    ) -> int:
        """累积一次工具事件，返回本次 delta。

        调用方（PostToolUse hook）应直接使用返回值，
        也可用 ``.score`` 获取当前总分。

        V1.3 §4.2 行为：
          - 窗口已过期返回 0（不累积）
          - 首次 tool call 自动启动窗口
          - prompt_id 传入时自动切换 prompt（调用 start_prompt）

        Args:
            event: {
                "tool": str,             # 工具名（Read/Edit/Write/...）
                "file_type": str = "",   # 编辑文件的扩展名
                "file_lines": str = "",  # small / medium / large
                "runtime_signal": str = "",  # bash_nonzero_exit / test_failure / ...
            }
            prompt_id: 当前 prompt ID。与 current_prompt_id 不同时
                       自动切换 prompt。

        Returns:
            int: 本次事件的增量分；窗口过期时返回 0。
        """
        # 自动切换 prompt（如果传入不同 prompt_id）
        if prompt_id and prompt_id != self._current_prompt_id:
            self.start_prompt(prompt_id)

        # 窗口检查：首次 tool call 启动窗口
        if self._window_start is None:
            self.start_window()
        # 窗口已过期 → 不累积
        if self.is_window_expired():
            return 0

        delta = self._compute_delta(event)
        self._score += delta

        # 更新 raw tool 调用次数
        tool_name = str(event.get("tool", ""))
        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1

        # V1.3 §13.2 记录事件，增加 prompt_id 字段
        event_with_ts = {
            **event,
            "prompt_id": self._current_prompt_id,
            "delta": delta,
            "timestamp": int(time.time()),
        }
        self._events.append(event_with_ts)
        return delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_prompt_id": self._current_prompt_id,
            "score": self._score,
            "tool_count": sum(self._tool_counts.values()),
            "tool_counts": dict(self._tool_counts),
            "window_start": self._window_start,
            "window_expired": self._window_expired,
            "events": copy.deepcopy(self._events),
            "prompt_history": copy.deepcopy(self._prompt_history),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuntimeScore":
        rs = cls()
        rs._current_prompt_id = str(d.get("current_prompt_id", ""))
        rs._score = int(d.get("score", 0))
        rs._window_start = d.get("window_start")
        if rs._window_start is not None:
            rs._window_start = float(rs._window_start)
        rs._window_expired = bool(d.get("window_expired", False))
        rs._tool_counts = dict(d.get("tool_counts", {}))
        rs._events = copy.deepcopy(list(d.get("events", [])))
        rs._prompt_history = copy.deepcopy(dict(d.get("prompt_history", {})))
        return rs

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _compute_delta(self, event: dict[str, Any]) -> int:
        """纯函数：event → score delta。"""
        delta = 0

        tool_name = str(event.get("tool", ""))
        tool_w = self._weights.get("tool", {})
        delta += tool_w.get(tool_name, 0)

        file_type = str(event.get("file_type", ""))
        ft_w = self._weights.get("file_type", {})
        delta += ft_w.get(file_type, 0)

        file_lines = str(event.get("file_lines", ""))
        fl_w = self._weights.get("file_lines", {})
        delta += fl_w.get(file_lines, 0)

        signal = str(event.get("runtime_signal", ""))
        if signal:
            sig_w = self._weights.get("runtime_signal", {})
            delta += sig_w.get(signal, 0)

        return delta

    @staticmethod
    def _label_from_score(score: int) -> str:
        """将分数量化到复杂度标签（与 decision_engine._label_from_score 一致）。

        Args:
            score: RuntimeScore 总分。

        Returns:
            "simple" / "medium" / "complex"。
        """
        if score <= 30:
            return "simple"
        elif score <= 70:
            return "medium"
        return "complex"
