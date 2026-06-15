"""
runtime_tracker.py — v1.3 Runtime Score Tracker（PostToolUse 接入）
====================================================================

V1.3 §8 PostToolUse 接入 / §7 Runtime Complexity Score。

RuntimeTracker 是 RuntimeScore 的 PostToolUse 封装层：
  - track(sid, project_root, raw_event) → 转换 + 累积 + 持久化
  - 从 session_state 文件读取当前 score，累积后写回
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
        """将 PostToolUse 原始事件转为 RuntimeScore 事件格式（V1.3 §7）。

        提取规则（V1.3 §7 全 4 维度）：
          - tool_name → tool
          - tool_input.file_path → 提取扩展名作为 file_type
          - file_lines：small(<200) / medium(200-800) / large(>800) — 基于行数估计
          - runtime_signal：bash_nonzero_exit / test_failure / retry / many_grep_hits
        """
        tool_input = raw_event.get("tool_input", {}) or {}
        tool_output = raw_event.get("tool_output", "") or ""

        # 提取文件扩展名
        file_path = tool_input.get("file_path", "")
        file_type = ""
        if file_path:
            file_type = Path(file_path).suffix

        # ── file_lines 维度：基于行数估计（V1.3 §7 file_lines 权重已激活）──
        file_lines = self._extract_file_lines(tool_name=raw_event.get("tool_name", ""),
                                              file_path=file_path,
                                              content=tool_input.get("content", ""))

        # ── runtime_signal 维度：检测异常/失败信号（V1.3 §7 runtime_signal 权重）──
        runtime_signal = self._extract_runtime_signal(
            tool_name=raw_event.get("tool_name", ""),
            tool_output=tool_output,
            tool_input=tool_input,
        )

        return {
            "tool": raw_event.get("tool_name", ""),
            "file_type": file_type,
            "file_lines": file_lines,
            "runtime_signal": runtime_signal,
        }

    # ── Signal Extractors ────────────────────────────────────────────────

    def _extract_file_lines(self, tool_name: str, file_path: str, content: str) -> str:
        """估计文件/内容的行数规模。

        阈值（与 stage_config._PLACEHOLDER_WEIGHTS["file_lines"] 一致）：
          - small: < 200 行
          - medium: 200-800 行
          - large: > 800 行
        """
        if not file_path and not content:
            return ""

        line_count = 0
        if content:
            # Write/Edit/MultiEdit 工具的 content 字段直接反映改动行数
            line_count = content.count("\n") + 1 if content else 0
        elif file_path:
            # 尝试从已读取文件估计（优先使用 tool_output，缺失时跳过）
            line_count = 0

        if line_count == 0:
            return ""
        if line_count < 200:
            return "small"
        if line_count <= 800:
            return "medium"
        return "large"

    def _extract_runtime_signal(self, tool_name: str, tool_output: str, tool_input: dict) -> str:
        """从工具输出中提取 runtime 信号。

        识别以下信号（与 _PLACEHOLDER_WEIGHTS["runtime_signal"] 一致）：
          - bash_nonzero_exit：Bash 退出码非零
          - test_failure：测试相关输出包含失败标记
          - grep_many_hits：Grep 返回大量命中
          - file_not_found：文件操作失败
          - large_diff：Edit/Write 涉及大改动
        """
        out = str(tool_output) if tool_output else ""
        out_lower = out.lower()

        # ── Bash 退出码检测 ──
        if tool_name == "Bash":
            if any(mark in out for mark in ("exit code 1", "exit code 2", "Error:", "FATAL", "fatal:")):
                return "bash_nonzero_exit"

        # ── 测试失败检测 ──
        if any(mark in out_lower for mark in (
            "test failed", "tests failed", "failed:", "failure:", "assertion error",
            "✗", "✘", "test errors", "test failures",
        )):
            return "test_failure"

        # ── Grep/Glob 大量命中 ──
        if tool_name in ("Grep", "Glob"):
            # 简单启发式：输出行数 > 50 视为"大量命中"
            if out.count("\n") > 50:
                return "grep_many_hits"

        # ── 文件未找到 ──
        if any(mark in out_lower for mark in (
            "no such file", "file not found", "does not exist", "enoent",
        )):
            return "file_not_found"

        # ── 大改动检测（Edit/Write/MultiEdit 一次性改动超过 100 行）──
        if tool_name in ("Edit", "Write", "MultiEdit"):
            content = tool_input.get("content", "") or ""
            if isinstance(content, str) and content.count("\n") > 100:
                return "large_diff"

        return ""

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
