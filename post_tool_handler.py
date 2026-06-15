"""
post_tool_handler.py — v1.3 PostToolUse Hook 入口（dispatcher）
=================================================================

V1.3 §8 PostToolUse 接入 / dispatcher + 2 worker。

从 stdin 读取 PostToolUse hook 原始 JSON → 按 tool_name
路由到对应 worker → 写入 session_state_<sid>.json：

  - TodoWrite → todowrite_analyzer + runtime_tracker（双写）
  - 其他工具 → runtime_tracker（仅 runtime_score）

入口函数：
  - main() — CLI 入口，从 stdin 读 JSON，提取 sid/cwd
  - dispatch(sid, project_root, raw_event) — 可供测试调用的纯逻辑

Feature Flag:
  MODEL_ROUTER_V13_OBSERVE=0 → 完全 no-op

设计约束：
  - 所有异常静默吞掉（不阻塞 PostToolUse hook）
  - 零依赖（除标准库 + 同目录模块）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 确保可以 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_tracker import RuntimeTracker
from todowrite_analyzer import TodoWriteAnalyzer


# ── Feature Flag ──────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    """MODEL_ROUTER_V13_OBSERVE flag：默认 True（开启观测）。"""
    flag = os.environ.get("MODEL_ROUTER_V13_OBSERVE", "1")
    return flag.lower() not in ("0", "false", "no", "off")


# ── Dispatch ──────────────────────────────────────────────────────────────

# 模块级单例（避免每次 dispatch 都创建实例）
_tracker = RuntimeTracker()
_analyzer = TodoWriteAnalyzer()


def dispatch(sid: str, project_root: str, raw_event: dict) -> None:
    """按 tool_name 路由 PostToolUse 事件到对应 worker。

    Args:
        sid: Session ID。
        project_root: 项目根目录。
        raw_event: PostToolUse hook 原始事件 dict。
    """
    if not _is_enabled():
        return

    try:
        if not isinstance(raw_event, dict):
            return

        tool_name = raw_event.get("tool_name", "")

        # ── 所有工具都走 RuntimeTracker（累积 runtime_score） ──
        _tracker.track(sid, project_root, raw_event)

        # ── TodoWrite 额外走 TodoWriteAnalyzer（写 todowrite_signal） ──
        if tool_name == "TodoWrite":
            _handle_todowrite(sid, project_root, raw_event)

    except Exception:
        # 静默吞掉所有异常，永不阻塞 PostToolUse hook
        pass


def _handle_todowrite(sid: str, project_root: str, raw_event: dict) -> None:
    """处理 TodoWrite 事件：分析 todos → 写入 todowrite_signal。"""
    tool_input = raw_event.get("tool_input", {}) or {}
    todos = tool_input.get("todos")

    signal = _analyzer.analyze(todos)

    # 写入 session_state（追加 todowrite_signal 字段）
    _write_todowrite_signal(sid, project_root, signal)


def _write_todowrite_signal(sid: str, project_root: str, signal: dict) -> None:
    """将 todowrite_signal 合并写入 session_state 文件。"""
    claude_dir = Path(project_root) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / f"session_state_{sid}.json"

    # 读取现有数据（保留其他字段如 runtime_score）
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["todowrite_signal"] = signal

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


# ── CLI Entry Point ───────────────────────────────────────────────────────

def main() -> None:
    """PostToolUse hook CLI 入口。

    从 stdin 读取 JSON → 提取 session_id 和 cwd → dispatch。
    """
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        # 非法输入 → 静默退出
        return

    try:
        sid = event.get("session_id", "")
        cwd = event.get("cwd", "")

        if sid and cwd:
            dispatch(sid, cwd, event)
    except Exception:
        # 最外层兜底
        pass


if __name__ == "__main__":
    main()
