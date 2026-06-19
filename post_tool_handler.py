"""
post_tool_handler.py — v1.3 PostToolUse Hook 入口（dispatcher）
=================================================================

V1.3 §8 PostToolUse 接入 / dispatcher + 2 worker。

从 stdin 读取 PostToolUse hook 原始 JSON → 按 tool_name
路由到对应 worker → 写入 model_router_state_<sid>.json：

  - TodoWrite / Task* → todowrite_analyzer + runtime_tracker（双写）
  - 其他工具 → runtime_tracker（仅 runtime_score）

Stage 5 扩展（V1.3 §6.4 决策链路端到端）：
  - track() 之后从 session_state 读 runtime_score + todowrite_signal
  - 调 decision_engine.maybe_redecide() 检查 lock 阈值

V1.3 §4.3 / §9.3：is_first_todo_write 跟踪
  - 首次 TodoWrite / Task* 标记后才触发升级逻辑
  - 首次 TodoWrite 可选 LLM 深度分析（analyze_with_llm）

入口函数：
  - main() — CLI 入口，从 stdin 读 JSON，提取 sid/cwd
  - dispatch(sid, project_root, raw_event) — 可供测试调用的纯逻辑

设计约束：
  - 所有异常静默吞掉（不阻塞 PostToolUse hook）
  - 零依赖（除标准库 + 同目录模块）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 确保可以 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_tracker import RuntimeTracker
from state_persistence import SessionStateStore
from todowrite_analyzer import TodoWriteAnalyzer


# 共享 todos 提取器（hooks/common/todo_payload.py）：
# 把"从 hook payload 解 todos"集中到一处，post_tool_handler 与
# hooks.session.todowrite_sync 复用同一份实现，避免在两个调用方
# 各写一份容错逻辑出现分歧。
# 注意：不能在模块顶层直接 import —— post_tool_handler 作为子进程运行
# 时 hooks/ 父包未必在 sys.path；测试代码会动态把 _repo_root 加到
# sys.path，import 必须在函数体内做以走运行时路径。
def _extract_todos(payload):
    """从 PostToolUse payload 提取 todos 列表（透传到共享提取器）。

    等价于 hooks.common.todo_payload.extract_todos_from_payload(payload)，
    只是把入口名字以 _extract_todos 暴露在 post_tool_handler 命名空间内，
    方便测试和外部调用方按既有约定引用。

    共享函数自身的边界（缺字段、类型错误、JSON 异常）已在
    todo_payload 单测覆盖——本函数零额外逻辑。
    """
    from hooks.common.todo_payload import extract_todos_from_payload
    return extract_todos_from_payload(payload)


# ── Dispatch ──────────────────────────────────────────────────────────────

# 模块级单例（避免每次 dispatch 都创建实例）
_tracker = RuntimeTracker()
_analyzer = TodoWriteAnalyzer()
_store = SessionStateStore()

# 触发 analyzer 写入 todowrite_signal 的工具名集合
# v2026-06: 兼容旧 TodoWrite + 新 Task* 系列
_TASK_TOOL_NAMES = ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput")


def dispatch(sid: str, project_root: str, raw_event: dict) -> None:
    """按 tool_name 路由 PostToolUse 事件到对应 worker。

    Args:
        sid: Session ID。
        project_root: 项目根目录。
        raw_event: PostToolUse hook 原始事件 dict。
    """
    try:
        # ── V1.4 is_valid_prompt 穿透 ──
        # 前置链路（stage_detector）判定本 prompt 为续接指令
        # （is_valid_prompt=False）且已在 state 文件中写入了
        # skip_post_tool_analysis 标记 → 跳过本回合所有后置分析，
        # 避免对无新信息的 prompt 做无意义的 RuntimeTracker /
        # TodoWriteAnalyzer / maybe_redecide。
        if _should_skip_post_tool(sid, project_root):
            return

        if not isinstance(raw_event, dict):
            return

        tool_name = raw_event.get("tool_name", "")

        # ── 所有工具都走 RuntimeTracker（累积 runtime_score） ──
        _tracker.track(sid, project_root, raw_event)

        # ── TodoWrite / Task* 额外走 TodoWriteAnalyzer（写 todowrite_signal） ──
        # v2026-06: TodoWrite 已被 CC 2.1.142+ 移除，改用 Task* 系列。
        # 共享同一个 analyzer 入口：磁盘权威源对 Task* 而言等同旧 TodoWrite 的 payload。
        if tool_name in _TASK_TOOL_NAMES:
            _handle_task_tool(sid, project_root, raw_event)

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
    data = _store.read_new(sid, project_root)
    if not data:
        return 0, None

    rs = data.get("runtime_score") or {}
    runtime_score = int(rs.get("score", 0)) if isinstance(rs, dict) else 0

    todo = data.get("todowrite_signal")
    if not isinstance(todo, dict):
        todo = None

    return runtime_score, todo


def _should_skip_post_tool(sid: str, project_root: str) -> bool:
    """检查 state 文件中的 skip_post_tool_analysis 标记。

    当前置链路（stage_detector）判定当前 prompt 为续接指令
    （is_valid_prompt=False），会写入此标记。PostToolUse 的
    dispatch() 入口检查此标记，若为 true 则跳过所有运行时分析。

    Returns:
        True 表示应跳过 PostToolUse 分析，False 表示正常处理。
    """
    try:
        data = _store.read_new(sid, project_root) or {}
        return data.get("skip_post_tool_analysis", False)
    except Exception:
        return False


def _handle_task_tool(sid: str, project_root: str, raw_event: dict) -> None:
    """处理 TodoWrite / Task* 事件：分析 task 列表 → 写入 todowrite_signal。

    V1.3 §4.3 / §9.3:
      - 跟踪 is_first_todo_write（读 state 判断是否已写入过 signal）
      - 首次触发尝试 LLM 深度分析，失败回退关键词启发式
      - 非首次使用关键词启发式（轻量）

    v2026-06: 由 ``_handle_todowrite`` 改名而来，同时支持新 CC 的 Task*
    工具（TaskCreate / TaskUpdate / TaskList / TaskGet / TaskOutput）。
    委托给 ``hooks.common.todo_payload.extract_tasks_from_payload`` 统一解析。
    """
    # ── 复用 hooks.common.todo_payload 解析，与 todowrite_sync 保持一致 ──
    # 区分两种"无 todos"：None（payload 里没有 todos 字段，不该触发任何写入）vs
    # []（调过 TodoWrite 但清空了，照常走 analyze 落 empty_result）。
    todos = _extract_tasks(raw_event)
    if todos is None:
        # 缺字段 / 非 dict / tool_input 缺 todos → 不写 signal
        return

    # ── 判断 is_first_todo_write ──
    is_first = not _has_existing_todowrite_signal(sid, project_root)

    # ── 分析 ──
    if is_first:
        # 首次：尝试 LLM 深度分析（V1.3 §9.2）
        signal = _analyzer.analyze_with_llm(todos, is_first=True)
    else:
        # 非首次：关键词启发式（轻量）
        signal = _analyzer.analyze(todos, is_first=False)

    # ── 写入 session_state ──
    _write_todowrite_signal(sid, project_root, signal)


def _extract_tasks(raw_event: dict):
    """委托给 ``hooks.common.todo_payload.extract_tasks_from_payload``。

    包一层的理由：
      1. handler 不直接依赖 hooks 包路径（避免 sys.path 不一致）
      2. 与 todowrite_sync 共用同一份解析逻辑，未来字段变动只改一处
      3. ImportError 兜底：万一 hooks 包不可用（极少见）→ 退到手写 TodoWrite 解析
    """
    try:
        # 兼容 inline import 与独立脚本两种调用方式
        from hooks.common.todo_payload import extract_tasks_from_payload
    except ImportError:
        # hooks 包不可用 → 退到手写 TodoWrite 解析（保持原行为，不支持 Task*）
        tool_input = raw_event.get("tool_input", {}) or {}
        if not isinstance(tool_input, dict):
            return None
        todos = tool_input.get("todos")
        return todos if isinstance(todos, list) else None
    return extract_tasks_from_payload(raw_event)


def _has_existing_todowrite_signal(sid: str, project_root: str) -> bool:
    """检查 session_state 中是否已存在 todowrite_signal 记录。"""
    data = _store.read_new(sid, project_root) or {}
    return "todowrite_signal" in data and isinstance(data["todowrite_signal"], dict)


def _write_todowrite_signal(sid: str, project_root: str, signal: dict) -> None:
    """将 todowrite_signal 通过 SessionStateStore 合并写入 session_state。"""
    _store.update_fields(sid, project_root, {"todowrite_signal": signal})


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
            # ★ Fix: 用 _find_project_root 解析项目根，而非把 cwd 直接当 project_root
            # stage_detector._find_project_root 走 4 级查找：
            #   ① stage_<sid>/.claude/ 锚点 ② .claude/ 目录 ③ .git/ 顶层 ④ ~/.claude
            # 确保 state 文件写 <project_root>/.claude/，与 stage_detector 写入路径一致
            from stage_detector import _find_project_root as _resolve_root
            _start = Path(cwd) if not isinstance(cwd, Path) else Path(cwd)
            project_root = str(_resolve_root(_start, sid))
            dispatch(sid, project_root, event)
    except Exception:
        # 最外层兜底
        pass


if __name__ == "__main__":
    main()
