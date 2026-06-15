"""
post_tool_handler.py — v1.3 PostToolUse Hook 入口（dispatcher）
=================================================================

V1.3 §8 PostToolUse 接入 / dispatcher + 2 worker。

从 stdin 读取 PostToolUse hook 原始 JSON → 按 tool_name
路由到对应 worker → 写入 model_router_state_<sid>.json：

  - TodoWrite → todowrite_analyzer + runtime_tracker（双写）
  - 其他工具 → runtime_tracker（仅 runtime_score）

Stage 5 扩展（V1.3 §6.4 决策链路端到端）：
  - track() 之后从 session_state 读 runtime_score + todowrite_signal
  - 调 decision_engine.maybe_redecide() 检查 lock 阈值

入口函数：
  - main() — CLI 入口，从 stdin 读 JSON，提取 sid/cwd
  - dispatch(sid, project_root, raw_event) — 可供测试调用的纯逻辑

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

# 跨包复用：与 hooks.compact.todowrite_sync 共用 TodoWrite payload 解析逻辑
# 路径从 __file__ 推导而非 hardcode ~/.claude，确保 worktree / 异机部署也能解析
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks.common.todo_payload import extract_todos_from_payload


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
    try:
        if not isinstance(raw_event, dict):
            return

        tool_name = raw_event.get("tool_name", "")

        # ── 所有工具都走 RuntimeTracker（累积 runtime_score） ──
        _tracker.track(sid, project_root, raw_event)

        # ── TodoWrite 额外走 TodoWriteAnalyzer（写 todowrite_signal） ──
        if tool_name == "TodoWrite":
            _handle_todowrite(sid, project_root, raw_event)

        # ── Stage 5.3: 调 maybe_redecide 检查 lock 阈值 ──
        # 读 session_state 取最新 runtime_score + todowrite_signal
        runtime_score, todowrite_signal = _read_latest_signals(sid, project_root)
        if runtime_score > 0 or todowrite_signal:
            try:
                from decision_engine import maybe_redecide
                maybe_redecide(
                    sid=sid,
                    project_root=project_root,
                    runtime_score=runtime_score,
                    todowrite_signal=todowrite_signal,
                )
            except Exception:
                # maybe_redecide 内部已静默吞错；这里再兜一层
                pass

    except Exception:
        # 静默吞掉所有异常，永不阻塞 PostToolUse hook
        pass


def _read_latest_signals(sid: str, project_root: str) -> tuple[int, dict | None]:
    """从 model_router_state_<sid>.json 读最新 runtime_score + todowrite_signal。

    Returns:
        (runtime_score, todowrite_signal)；文件缺失/字段缺失时
        返回 (0, None)。
    """
    path = Path(project_root) / ".claude" / f"model_router_state_{sid}.json"
    if not path.exists():
        return 0, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0, None

    rs = data.get("runtime_score") or {}
    runtime_score = int(rs.get("score", 0)) if isinstance(rs, dict) else 0

    todo = data.get("todowrite_signal")
    if not isinstance(todo, dict):
        todo = None

    return runtime_score, todo


def _handle_todowrite(sid: str, project_root: str, raw_event: dict) -> None:
    """处理 TodoWrite 事件：分析 todos → 写入 todowrite_signal。

    注：todos 可能为 None（payload 中无 todos 字段）。analyzer.analyze 内部
    对 None 有兜底（_analyze 入口会返回 _empty_result），故无需在此判空。
    """
    todos = extract_todos_from_payload(raw_event) or []

    signal = _analyzer.analyze(todos)

    # 写入 session_state（追加 todowrite_signal 字段）
    _write_todowrite_signal(sid, project_root, signal)


def _write_todowrite_signal(sid: str, project_root: str, signal: dict) -> None:
    """将 todowrite_signal 合并写入 session_state 文件。"""
    claude_dir = Path(project_root) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / f"model_router_state_{sid}.json"

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
