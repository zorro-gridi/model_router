#!/usr/bin/env python3
"""
stage_show.py — Stop Hook（PostToolBatch 也可用）
=================================================
每次 Claude 完成一轮回复后，在终端打印当前阶段和路由目标，
让用户始终知道"现在用的是哪个模型"。

阶段文件位置：
  分 session 阶段文件存于 <project_root>/.claude/stage_<session_id>，
  与 session_state_<session_id>.json 同目录。
  active_session 指针存于 ~/.claude/hooks/model_router/，内容为
  阶段文件的完整绝对路径。

Claude Code settings.json 配置：
  {
    "hooks": {
      "Stop": [
        {
          "hooks": [
            {
              "type": "command",
              "command": "python3 ~/.claude/hooks/model_router/stage_show.py"
            }
          ]
        }
      ]
    }
  }
"""

import json
import os
import sys
from pathlib import Path

# ── 分 session 阶段管理 ──
# 存放位置：<project_root>/.claude/stage_<session_id>
# active_session 指针：~/.claude/hooks/model_router/active_session → 完整路径
HOME_CLAUDE         = Path.home() / ".claude"
HOOK_DIR            = HOME_CLAUDE / "hooks" / "model_router"
ACTIVE_SESSION_FILE = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE   = HOOK_DIR / "current_stage"

# 从统一配置文件导入（hooks/model_router/stage_config.py）
from stage_config import STAGE_DISPLAY


def _read_stage_file(path: Path) -> str | None:
    """读取指定阶段文件，不存在或为空时返回 None。"""
    try:
        content = path.read_text().strip()
        return content if content else None
    except FileNotFoundError:
        return None


# ── 项目根目录查找（参照 compact/utils.py 的 _find_project_root）──

def _find_project_root(start: Path, session_id: str | None = None) -> Path:
    """Walk up from ``start`` looking for a project boundary marker.

    Anchor strategy:
      1. If session_id is known, walk up looking for stage_<sid> or
         session_state_<sid>.json under .claude/ — its parent IS the project root.
      2. Walk up looking for .claude/ (skip global ~/.claude unless started there).
      3. Walk up looking for .git/ as fallback.
      4. Fall back to ~/.claude.
    """
    p = start

    if session_id:
        anchor_p = start
        for _ in range(20):
            claude_dir = anchor_p / ".claude"
            if (claude_dir / f"stage_{session_id}").exists() or \
               (claude_dir / f"session_state_{session_id}.json").exists():
                return anchor_p
            parent = anchor_p.parent
            if parent == anchor_p:
                break
            anchor_p = parent

    git_root = None
    for _ in range(20):
        cand = p / ".claude"
        if cand.is_dir():
            if cand != HOME_CLAUDE or str(start).startswith(str(HOME_CLAUDE) + os.sep):
                return p
        if git_root is None and (p / ".git").exists():
            git_root = p
        parent = p.parent
        if parent == p:
            break
        p = parent

    if git_root is not None:
        return git_root
    return HOME_CLAUDE if HOME_CLAUDE.is_dir() else start


def _stage_file_path(cwd: str | Path, session_id: str) -> Path:
    """返回 <project_root>/.claude/stage_<session_id> 路径。"""
    cwd = Path(cwd) if isinstance(cwd, str) else cwd
    project_root = _find_project_root(cwd, session_id)
    return project_root / ".claude" / f"stage_{session_id}"


def read_stage(event: dict | None = None) -> str:
    """
    读取当前阶段，优先级：
      1. stdin 中的 session_id+cwd → <project_root>/.claude/stage_<session_id>
      2. active_session 指针 → 读取其存储的完整路径文件
      3. 全局后备文件 → current_stage
      4. default
    """
    # 1. 从 event 中解析 session_id + cwd
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            content = _read_stage_file(_stage_file_path(cwd, session_id))
            if content:
                return content

    # 2. active_session 指针 → 完整路径直接读取
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(Path(active_path))
            if content:
                return content
    except FileNotFoundError:
        pass

    # 3. 全局后备
    content = _read_stage_file(GLOBAL_STAGE_FILE)
    if content:
        return content

    return "default"


def main():
    event = None
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass  # stdin 可能为空或非 JSON（兼容老版本）

    stage = read_stage(event)
    emoji, label, model = STAGE_DISPLAY.get(stage, STAGE_DISPLAY["default"])

    # 输出到 stderr（终端可见，不影响 CC 的 stdout 解析）
    print(
        f"\r\033[90m[Stage Router] {emoji} {label} → {model}\033[0m",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
