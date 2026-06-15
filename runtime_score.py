"""
runtime_score.py — v1.3 Runtime Complexity Score 累积器
==========================================================

V1.3 §7 Runtime Complexity Score / §3.4 权重可自定义。

纯内存计分器，零 I/O。PostToolUse hook 每次触发时调用
`accumulate(event)` 累积复杂度分。Stage 2 提供核心逻辑，
Stage 4 接入实际 hook。

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
    """纯内存运行时复杂度计分器（V1.3 §7）。"""

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

    @property
    def score(self) -> int:
        return self._score

    @property
    def events(self) -> list[dict[str, Any]]:
        """返回事件日志的浅拷贝，防止外部修改内部状态。"""
        return list(self._events)

    # ── 公开 API ──────────────────────────────────────────────────────────

    def accumulate(self, event: dict[str, Any]) -> int:
        """累积一次工具事件，返回本次 delta。

        调用方（PostToolUse hook）应直接使用返回值，
        也可用 `.score` 获取当前总分。

        Args:
            event: {
                "tool": str,             # 工具名（Read/Edit/Write/...）
                "file_type": str = "",   # 编辑文件的扩展名
                "file_lines": str = "",  # small / medium / large
                "runtime_signal": str = "",  # bash_nonzero_exit / test_failure / ...
            }

        Returns:
            int: 本次事件的增量分。
        """
        delta = self._compute_delta(event)
        self._score += delta
        # V1.3 §13.2 Runtime Event 增加 timestamp 字段
        event_with_ts = {**event, "delta": delta, "timestamp": int(time.time())}
        self._events.append(event_with_ts)
        return delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self._score,
            "events": copy.deepcopy(self._events),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuntimeScore":
        rs = cls()
        rs._score = int(d.get("score", 0))
        rs._events = copy.deepcopy(list(d.get("events", [])))
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
