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

STAGE_FILE = Path.home() / ".claude" / "hooks/model_router/stage"

STAGE_DISPLAY = {
    "brainstorm": ("💭", "头脑风暴", "deepseek-v4-flash"),
    "decide":     ("⚖️",  "决策分析", "MiniMax-M3"),
    "design":     ("🏗️",  "方案设计", "MiniMax-M3"),
    "plan":       ("📋", "任务拆解", "deepseek-v4-pro"),
    "implement":  ("⚙️",  "工程实施", "deepseek-v4-pro"),
    "audit":      ("🔍", "工程审计", "MiniMax-M3"),
    "default":    ("🔄", "默认",     "deepseek-v4-pro"),
}


def main():
    try:
        stage = STAGE_FILE.read_text().strip()
    except FileNotFoundError:
        stage = "default"

    emoji, label, model = STAGE_DISPLAY.get(stage, STAGE_DISPLAY["default"])

    # 输出到 stderr（终端可见，不影响 CC 的 stdout 解析）
    print(
        f"\r\033[90m[Stage Router] {emoji} {label} → {model}\033[0m",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
