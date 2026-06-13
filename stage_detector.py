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

# ── 分 session 阶段管理 ──
# 每个 session 独立管理阶段，避免多会话互相覆盖。
# 命名规则：stage_<session_id>（参照 hooks/session 的 session_state_<session_id> 模式）
# 同时维护 active_session 指针文件，供 proxy.py 等无 stdin 上下文的组件查询当前活跃 session。
STAGE_DIR         = Path.home() / ".claude" / "hooks" / "model_router"
ACTIVE_SESSION_FILE = STAGE_DIR / "active_session"
GLOBAL_STAGE_FILE   = STAGE_DIR / "current_stage"   # 全局后备（无 session_id 时使用）
LOG_FILE            = Path("/tmp/stage_detector.log")


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


def _stage_path(session_id: str | None) -> Path:
    """返回指定 session 的阶段文件路径。无 session_id 时返回全局后备文件。"""
    if session_id:
        return STAGE_DIR / f"stage_{session_id}"
    return GLOBAL_STAGE_FILE


def _read_stage_file(path: Path) -> str | None:
    """读取指定阶段文件，不存在时返回 None。"""
    try:
        content = path.read_text().strip()
        return content if content else None
    except FileNotFoundError:
        return None


def write_stage(stage: str, session_id: str | None = None) -> None:
    """写入阶段。有 session_id → 写入 stage_<session_id> 并更新 active_session 指针；
    无 session_id → 写入全局后备文件。"""
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    if session_id:
        _stage_path(session_id).write_text(stage + "\n")
        ACTIVE_SESSION_FILE.write_text(session_id)
    else:
        GLOBAL_STAGE_FILE.write_text(stage + "\n")


def read_stage(session_id: str | None = None) -> str:
    """
    读取当前阶段，优先级：
      1. 传入的 session_id → stage_<session_id>
      2. active_session 指针 → stage_<session_id>
      3. 全局后备文件 → current_stage
      4. default
    """
    # 1. 指定 session
    if session_id:
        content = _read_stage_file(_stage_path(session_id))
        if content:
            return content

    # 2. active_session 指针
    try:
        active = ACTIVE_SESSION_FILE.read_text().strip()
        if active:
            content = _read_stage_file(_stage_path(active))
            if content:
                return content
    except FileNotFoundError:
        pass

    # 3. 全局后备
    content = _read_stage_file(GLOBAL_STAGE_FILE)
    if content:
        return content

    # 4. 兜底
    return "default"


def main():
    log("INFO", "hook triggered")
    try:
        try:
            event = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError) as exc:
            log("WARN", f"stdin JSON parse failed: {exc!r}; passthrough")
            sys.exit(0)

        # ── 提取 session_id（分 session 管理的关键）──
        session_id: str | None = (event.get("session_id") or "").strip() or None

        # UserPromptSubmit 的 prompt 字段
        prompt: str = event.get("prompt", "")
        if not prompt:
            # prompt 为空时也确保文件存在
            log("INFO", "empty prompt, ensure stage file exists")
            read_stage(session_id)
            sys.exit(0)

        new_stage = detect_stage(prompt)
        old_stage = read_stage(session_id)

        if new_stage and new_stage != old_stage:
            write_stage(new_stage, session_id)
            log("INFO", f"stage: {old_stage} → {new_stage}")
            # 从统一配置文件获取阶段描述
            from stage_config import STAGE_INFO
            info = STAGE_INFO.get(new_stage, "")
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
