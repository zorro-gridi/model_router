#!/usr/bin/env python3
"""
workflow_orchestrator.py — 工作流编排器（设计文档 §6.5 / §10 步骤 6-8）
==========================================================================

职责
----
把"Workflow Planner 打算怎么走"变成"实际怎么走"。

与 stage_config.WORKFLOW_PLANNER 的关系：
  WORKFLOW_PLANNER 是**配置真源**（决定 simple/medium/complex 各自的 steps/models）。
  workflow_orchestrator 是**运行时管理**（激活 plan / 推进 step / 失效 plan）。
  两者解耦：修改 step 顺序只改 stage_config；运行时推进逻辑只改本文件。

激活与推进
----
1. stage_detector 写完 complexity 后，若 label in ("medium","complex")
   且 workflow_step_<sid> 文件不存在 → 调用 activate() 写入初始 step=1
2. proxy 端 do_POST 路由前读 read_state() 拿 current_step，按 models[step-1] 路由
3. 路由后调用 advance() 把 current_step+1 写回；越界（> len(steps)）时
   自动 deactivate（删除文件）落回 stage 路由

文件格式（JSON）
----------------
<project_root>/.claude/workflow_step_<sid>:
  {
    "plan_type":      "triple",
    "complexity":     "complex",
    "models":         ["deepseek-v4-pro", "MiniMax-M3", "deepseek-v4-pro"],
    "steps":          ["plan", "execute", "audit"],
    "step_stages":    ["plan", "implement", "audit"],
    "current_step":   1,                     # 1-based；越界时 advance() 会 deactivate
    "activated_at":   1234567890,
    "ts":             1234567890
  }

并发安全
----
复用 fcntl.flock 咨询锁（与 rate_limit.py 同模式）。lock 失败时退化为无锁 best-effort。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 stage_config.WORKFLOW_PLANNER 与 STRONG/NORMAL 常量（避免双源漂移）
try:
    from stage_config import WORKFLOW_PLANNER  # noqa: E402
except ImportError:
    # 极端情况下 stage_config 加载失败 → 用本文件内置兜底
    WORKFLOW_PLANNER = {
        "simple":  {"type": "single", "steps": ["execute"],
                    "models": ["MiniMax-M3"], "step_stages": ["default"]},
        "medium":  {"type": "double", "steps": ["plan", "execute"],
                    "models": ["deepseek-v4-pro", "MiniMax-M3"],
                    "step_stages": ["plan", "implement"]},
        "complex": {"type": "triple", "steps": ["plan", "execute", "audit"],
                    "models": ["deepseek-v4-pro", "MiniMax-M3", "deepseek-v4-pro"],
                    "step_stages": ["plan", "implement", "audit"]},
    }

try:
    import fcntl  # macOS / Linux 都内置
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows fallback


# ── 路径解析 ───────────────────────────────────────────────────────────────
def _workflow_file_path(project_root: str | Path, session_id: str) -> Path:
    """workflow_step_<sid> 落盘文件：<project_root>/.claude/workflow_step_<sid>"""
    root = Path(project_root)
    claude_dir = root / ".claude"
    if not claude_dir.is_dir():
        claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / f"workflow_step_{session_id}"


# ── 文件 I/O ───────────────────────────────────────────────────────────────
def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return json.loads(content)
    except (OSError, json.JSONDecodeError):
        return None


def _write(path: Path, data: dict) -> bool:
    """原子写：先写 .tmp 再 rename，避免半写状态被并发读到。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _delete(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _with_flock(path: Path, modifier):
    """带锁的 read-modify-write。modifier 接收 (data_or_None, now) -> data_or_None。
    data_or_None 为 None 时 modifier 应返回新 dict（不存在时初始化）。
    返回 modifier 写入的最终 data。
    """
    now = int(time.time())
    data = _read(path)
    try:
        # 用 .tmp 文件做 lock（lock 跟 path 关联，避免影响业务文件并发读）
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_fd = open(lock_path, "w")
        if _HAS_FCNTL:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # 锁失败时退化为无锁 best-effort
        try:
            new_data = modifier(data, now)
            if new_data is None:
                _delete(path)
                return None
            _write(path, new_data)
            return new_data
        finally:
            try:
                lock_fd.close()
            except OSError:
                pass
    except OSError:
        return data  # 锁 / 文件失败时返回原 data，best-effort


# ── 对外 API ───────────────────────────────────────────────────────────────
def activate(complexity_label: str,
             session_id: str,
             project_root: str | Path) -> dict | None:
    """
    激活一个 plan。返回写入的 plan 详情（dict）；失败 / 不适用时返回 None。

    适用条件：
      - complexity_label in {"medium", "complex"} —— simple 走单模型，不激活
      - workflow_step_<sid> 不存在（避免中途重新激活导致步骤错位）
    """
    if not session_id:
        return None
    if complexity_label not in WORKFLOW_PLANNER:
        return None
    if complexity_label == "simple":
        return None

    plan_rule = WORKFLOW_PLANNER[complexity_label]
    path = _workflow_file_path(project_root, session_id)

    def _modifier(data, now):
        if data is not None:
            # 已被激活（中途被 LLM 复判 simple→complex）；保留 current_step，
            # 不重置（用户已经在 plan 中途）。
            return data
        return {
            "plan_type":    plan_rule["type"],
            "complexity":   complexity_label,
            "models":       list(plan_rule["models"]),
            "steps":        list(plan_rule["steps"]),
            "step_stages":  list(plan_rule["step_stages"]),
            "current_step": 1,
            "activated_at": now,
            "ts":           now,
        }

    return _with_flock(path, _modifier)


def read_state(session_id: str,
               project_root: str | Path) -> dict | None:
    """读取当前 plan 状态（无文件 / 解析失败返回 None）。"""
    if not session_id:
        return None
    return _read(_workflow_file_path(project_root, session_id))


def advance(session_id: str,
            project_root: str | Path) -> dict | None:
    """
    推进 step 计数。返回推进后的 state（dict）；越界时自动 deactivate 并返回 None。

    推进策略：
      - current_step+1 <= len(models)：写回新 state
      - current_step+1 >  len(models)：删除文件（plan 完成，落回 stage 路由）
    """
    if not session_id:
        return None
    path = _workflow_file_path(project_root, session_id)

    def _modifier(data, now):
        if data is None:
            return None  # 已被外部 deactivate
        cur = int(data.get("current_step", 1))
        n = len(data.get("models", []))
        if cur >= n:
            # 越界（理应不会发生，但防御）：清除
            return None
        new_step = cur + 1
        if new_step > n:
            # 已完成 → 清除文件
            return None
        data["current_step"] = new_step
        data["ts"] = now
        return data

    return _with_flock(path, _modifier)


def deactivate(session_id: str,
               project_root: str | Path) -> None:
    """清除 plan 状态（用户 ~model / ~reset 时调用）。"""
    if not session_id:
        return
    _delete(_workflow_file_path(project_root, session_id))


# ── CLI（调试）─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Workflow orchestrator debug CLI")
    p.add_argument("action", choices=["activate", "advance", "read", "deactivate"],
                   help="action to perform")
    p.add_argument("--label", default="complex",
                   help="complexity label for activate (simple/medium/complex)")
    p.add_argument("--session", default="cli-test", help="session id")
    p.add_argument("--project", default=".", help="project root")
    args = p.parse_args()

    if args.action == "activate":
        result = activate(args.label, args.session, args.project)
        print(f"activate: {result}")
    elif args.action == "advance":
        result = advance(args.session, args.project)
        print(f"advance: {result}")
    elif args.action == "read":
        result = read_state(args.session, args.project)
        print(f"read: {result}")
    elif args.action == "deactivate":
        deactivate(args.session, args.project)
        print("deactivate: ok")
