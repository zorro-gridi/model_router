#!/usr/bin/env python3
"""
stage_detector.py — UserPromptSubmit Hook
==========================================
在用户每次提交 prompt 前触发，分析关键词，自动推断并写入当前阶段。
阶段一旦写入，后续所有请求都会被代理路由到对应模型。

支持两种写入方式：
  1. 关键词自动检测（本文件负责）
  2. 用户显式前缀，如：/stage implement → 写入 implement

Claude Code settings.json 配置：
  {
    "hooks": {
      "UserPromptSubmit": [
        {
            "hooks": [
                {
                "type": "command",
                "command": "python3 ~/.claude/hooks/model_router/stage_detector.py"
                }
            ]
        }
      ]
    }
  }
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 注意：数据文件 current_stage 和 stage CLI 源同目录不同名
STAGE_FILE = Path.home() / ".claude" / "hooks" / "model_router" / "current_stage"
LOG_FILE   = Path("/tmp/stage_detector.log")


def log(level: str, msg: str) -> None:
    """best-effort 日志：写入 /tmp/stage_detector.log。
    失败静默——日志绝不能成为 hook 退非零码的原因。
    不记 prompt 原文，避免敏感信息泄漏到 /tmp。
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} | {level:<5} | {msg}\n")
    except Exception:
        pass

# 关键词 → 阶段映射（按优先级排列，先匹配先赢）
STAGE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("brainstorm", [
        "头脑风暴", "brainstorm", "想法", "创意", "idea", "explore",
        "可能性", "方向", "possibilities", "脑暴", "随便想想",
    ]),
    ("decide",     [
        "决策", "选择", "compare", "对比", "权衡", "trade-off", "pros and cons",
        "哪个好", "怎么选", "evaluate", "评估", "analysis", "分析",
    ]),
    ("design",     [
        "设计", "架构", "design", "architect", "方案", "schema", "structure",
        "模块", "接口", "interface", "系统设计", "数据模型",
    ]),
    ("plan",       [
        "计划", "plan", "拆分", "breakdown", "步骤", "task list",
        "todo", "roadmap", "分解", "milestone", "任务清单", "怎么做",
    ]),
    ("implement",  [
        "实现", "implement", "编码", "coding", "写代码", "开发", "develop",
        "写", "build", "create", "fix", "修复", "add", "添加",
    ]),
    ("audit",      [
        "审计", "audit", "review", "检查", "code review", "安全",
        "security", "漏洞", "bug", "测试", "test", "验证", "verify",
        "质量", "quality", "检验",
    ]),
]

# 显式命令前缀（优先级最高）
EXPLICIT_PREFIX_RE = re.compile(
    r"^/stage\s+(brainstorm|decide|design|plan|implement|audit|default)\b",
    re.IGNORECASE,
)


def detect_stage(prompt: str) -> str | None:
    """
    返回检测到的阶段名，或 None（表示不更改当前阶段）。
    优先级：显式命令 > 关键词匹配 > 不变
    """
    # 显式命令
    m = EXPLICIT_PREFIX_RE.match(prompt.strip())
    if m:
        return m.group(1).lower()

    # 关键词匹配（遍历顺序即优先级）
    prompt_lower = prompt.lower()
    for stage, keywords in STAGE_KEYWORDS:
        if any(kw in prompt_lower for kw in keywords):
            return stage

    return None  # 不更改阶段


def write_stage(stage: str) -> None:
    STAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAGE_FILE.write_text(stage + "\n")  # 统一带换行，与 echo 一致


def read_stage() -> str:
    try:
        content = STAGE_FILE.read_text().strip()
        return content if content else "default"
    except FileNotFoundError:
        # 文件不存在时初始化写入，避免"看起来是空的"
        write_stage("default")
        return "default"


def main():
    log("INFO", "hook triggered")
    try:
        try:
            event = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError) as exc:
            log("WARN", f"stdin JSON parse failed: {exc!r}; passthrough")
            sys.exit(0)

        # UserPromptSubmit 的 prompt 字段
        prompt: str = event.get("prompt", "")
        if not prompt:
            # prompt 为空时也确保文件存在
            log("INFO", "empty prompt, ensure stage file exists")
            read_stage()
            sys.exit(0)

        new_stage = detect_stage(prompt)
        old_stage = read_stage()

        if new_stage and new_stage != old_stage:
            write_stage(new_stage)
            log("INFO", f"stage: {old_stage} → {new_stage}")
            # 注入上下文给 Claude，告知当前阶段和对应模型
            stage_info = {
                "brainstorm": "快速探索阶段 → deepseek-v4-flash（便宜快速），随意发散",
                "decide":     "决策分析阶段 → MiniMax-M3，深度推理",
                "design":     "方案设计阶段 → MiniMax-M3，系统架构",
                "plan":       "任务拆解阶段 → deepseek-v4-pro，结构化输出",
                "implement":  "工程实施阶段 → deepseek-v4-pro，主力编码",
                "audit":      "工程审计阶段 → MiniMax-M3，严格检查",
            }
            info = stage_info.get(new_stage, "")
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    # additionalContext 会注入到本次 prompt 的上下文里
                    "additionalContext": (
                        f"\n[Stage Router] 阶段已切换: {old_stage} → {new_stage}"
                        + (f"（{info}）" if info else "")
                        + "\n"
                    ),
                }
            }
            print(json.dumps(output))
        elif new_stage == old_stage:
            # 阶段未变，静默通过
            log("INFO", f"stage unchanged: {old_stage}")
        else:
            # 未检测到阶段信号，静默通过
            log("INFO", "no stage signal, passthrough")
    except Exception as exc:
        # 兜底：任何未捕获异常都吞掉，绝不让 hook 退非零码
        log("ERROR", f"unexpected: {exc!r}; passthrough")
    sys.exit(0)


if __name__ == "__main__":
    main()
