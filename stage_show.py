#!/usr/bin/env python3
"""
stage_show.py — Stop Hook（PostToolBatch 也可用）
=================================================
每次 Claude 完成一轮回复后，在终端打印当前阶段和路由目标，
让用户始终知道"现在用的是哪个模型"。

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
import sys
from pathlib import Path

# ── 分 session 阶段管理 ──
STAGE_DIR            = Path.home() / ".claude" / "hooks" / "model_router"
ACTIVE_SESSION_FILE  = STAGE_DIR / "active_session"
GLOBAL_STAGE_FILE    = STAGE_DIR / "current_stage"

# 从统一配置文件导入（hooks/model_router/stage_config.py）
from stage_config import STAGE_DISPLAY


def _read_stage_file(path: Path) -> str | None:
    """读取指定阶段文件，不存在或为空时返回 None。"""
    try:
        content = path.read_text().strip()
        return content if content else None
    except FileNotFoundError:
        return None


def read_stage() -> str:
    """
    读取当前阶段，优先级：
      1. stdin 中的 session_id → stage_<session_id>
      2. active_session 指针 → stage_<session_id>
      3. 全局后备文件 → current_stage
      4. default
    """
    # 1. 尝试从 stdin 解析 session_id
    try:
        event = json.load(sys.stdin)
        session_id: str | None = (event.get("session_id") or "").strip() or None
        if session_id:
            content = _read_stage_file(STAGE_DIR / f"stage_{session_id}")
            if content:
                return content
    except (json.JSONDecodeError, EOFError):
        pass  # stdin 可能为空或非 JSON（兼容老版本）

    # 2. active_session 指针
    try:
        active = ACTIVE_SESSION_FILE.read_text().strip()
        if active:
            content = _read_stage_file(STAGE_DIR / f"stage_{active}")
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
    stage = read_stage()

    emoji, label, model = STAGE_DISPLAY.get(stage, STAGE_DISPLAY["default"])

    # 输出到 stderr（终端可见，不影响 CC 的 stdout 解析）
    print(
        f"\r\033[90m[Stage Router] {emoji} {label} → {model}\033[0m",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
