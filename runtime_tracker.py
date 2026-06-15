"""
runtime_tracker.py — v1.3 Runtime Score Tracker（PostToolUse 接入）
====================================================================

V1.3 §8 PostToolUse 接入 / §7 Runtime Complexity Score。

RuntimeTracker 是 RuntimeScore 的 PostToolUse 封装层：
  - track(sid, project_root, raw_event) → 转换 + 累积 + 持久化
  - 从 session_state 文件读取当前 score，累积后写回
  - 受 MODEL_ROUTER_V13_OBSERVE flag 控制（关闭时 no-op）
  - 所有异常静默吞噬（不阻塞 hook）

与 RuntimeScore（纯计算引擎）的关系：
  RuntimeScore 负责计分（纯函数，零 I/O），RuntimeTracker 负责
  I/O（读/写 session_state）+ 事件格式转换。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_score import RuntimeScore


class RuntimeTracker:
    """PostToolUse hook 的 RuntimeScore 跟踪器（Stage 4）。"""

    # ── Feature Flag ──────────────────────────────────────────────────────

    @staticmethod
    def _is_enabled() -> bool:
        """MODEL_ROUTER_V13_OBSERVE flag：默认 True（开启观测）。"""
        flag = os.environ.get("MODEL_ROUTER_V13_OBSERVE", "1")
        return flag.lower() not in ("0", "false", "no", "off")

    # ── Public API ────────────────────────────────────────────────────────

    def track(self, sid: str, project_root: str, raw_event: dict) -> int:
        """处理一次 PostToolUse 事件：转换 → 累积 → 持久化。

        Args:
            sid: Session ID。
            project_root: 项目根目录。
            raw_event: PostToolUse hook 的原始事件 dict，至少包含
                       `tool_name`，可选 `tool_input`。

        Returns:
            int: 本次事件的 delta 分。flag 关闭时返回 0。
        """
        if not self._is_enabled():
            return 0

        try:
            # 1. 转换原始事件为 RuntimeScore event 格式
            event = self._convert(raw_event)

            # 2. 加载当前 RuntimeScore
            rs = self._load(sid, project_root)

            # 3. 累积
            delta = rs.accumulate(event)

            # 4. 持久化
            self._save(sid, project_root, rs)

            return delta
        except Exception:
            # 静默吞噬所有异常，不阻塞 PostToolUse hook
            return 0

    # ── Event Conversion ──────────────────────────────────────────────────

    def _convert(self, raw_event: dict) -> dict:
        """将 PostToolUse 原始事件转为 RuntimeScore 事件格式。

        提取规则：
          - tool_name → tool
          - tool_input.file_path → 提取扩展名作为 file_type
          - file_lines 暂不计算（Stage 7 引入 diff 分析）
          - runtime_signal 暂不提取（Stage 5 引入）
        """
        tool_input = raw_event.get("tool_input", {}) or {}

        # 提取文件扩展名
        file_path = tool_input.get("file_path", "")
        file_type = ""
        if file_path:
            file_type = Path(file_path).suffix

        return {
            "tool": raw_event.get("tool_name", ""),
            "file_type": file_type,
            "file_lines": "",  # Stage 7 引入
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def _state_path(self, sid: str, project_root: str) -> Path:
        """model_router_state_<sid>.json 的路径。"""
        return Path(project_root) / ".claude" / f"model_router_state_{sid}.json"

    def _load(self, sid: str, project_root: str) -> RuntimeScore:
        """从 session_state 文件加载 RuntimeScore。文件缺失/损坏 → 返回空实例。"""
        path = self._state_path(sid, project_root)
        if not path.exists():
            return RuntimeScore()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rs_data = data.get("runtime_score", {})
            if rs_data:
                return RuntimeScore.from_dict(rs_data)
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
        return RuntimeScore()

    def _save(self, sid: str, project_root: str, rs: RuntimeScore) -> None:
        """将 RuntimeScore 写回 session_state 文件。

        仅更新 runtime_score 字段，保留文件中已有的其他字段。
        """
        claude_dir = Path(project_root) / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        path = claude_dir / f"model_router_state_{sid}.json"

        # 读取现有数据（保留其他字段）
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}

        # 更新 runtime_score
        existing["runtime_score"] = rs.to_dict()

        # 原子写入
        import threading
        suffix = f".{os.getpid()}.{id(threading.current_thread())}.tmp"
        tmp_path = path.with_suffix(suffix)
        try:
            tmp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(path))
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except TypeError:
                if tmp_path.exists():
                    tmp_path.unlink()
