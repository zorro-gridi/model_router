#!/usr/bin/env python3
"""
stage_detector.py — UserPromptSubmit Hook
==========================================
在用户每次提交 prompt 前触发，分析关键词，自动推断并写入当前阶段。
阶段一旦写入，后续所有请求都会被代理路由到对应模型。

支持两种写入方式：
  1. 关键词自动检测（本文件负责）
  2. 用户显式前缀，如：/stage implement → 写入 implement

阶段文件存储位置：
  分 session 阶段文件 stage_<session_id> 存放在 <项目根目录>/.claude/ 下，
  与 session_state_<session_id>.json 路径规则一致。
  active_session 指针文件存放在 ~/.claude/hooks/model_router/，存储的是
  阶段文件的完整绝对路径，供 proxy.py（无 stdin 上下文）直接读取。

Operation-type（第二维度，2026-06-13 引入）：
  与 stage 并列独立信号，由关键词或显式前缀触发：
    /op write /op read /op search /op refactor
  op 文件位置：<project_root>/.claude/op_<session_id>，与 stage_<sid> 同目录，
  仅前缀不同。proxy.py 端在 stage 路由之上叠加：检出 op → 完全覆盖 stage。

Model-override（用户显式指定模型，2026-06-13 引入，最高优先级）：
  用户可通过 !model / !m 前缀或自然语言（use / 用）指定模型简称：
    !model ds-v4-pro   !m mm3   use deepseek-v4-flash   用 mm3
  model 文件位置：<project_root>/.claude/model_<session_id>，与 stage_<sid>
  同目录，仅前缀不同。proxy.py 端路由优先级：model_override > op > stage。

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
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from model_alias import detect_model_override  # 用户模型覆盖（最高路由优先级）

# ── 分 session 阶段管理 ──
# 每个 session 独立管理阶段，避免多会话互相覆盖。
# 命名规则：stage_<session_id>（参照 hooks/session 的 session_state_<session_id> 模式）
# 存放位置：<project_root>/.claude/stage_<session_id>
# active_session 指针文件固定在 ~/.claude/hooks/model_router/，存储阶段文件的完整绝对路径。
HOME_CLAUDE         = Path.home() / ".claude"
HOOK_DIR            = HOME_CLAUDE / "hooks" / "model_router"
ACTIVE_SESSION_FILE = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE   = HOOK_DIR / "current_stage"   # 全局后备（无 session_id 时使用）
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


# ────────────────────────────────────────────────────────────────────
# Operation-type（与 stage 并列的第二维度路由信号）
# ────────────────────────────────────────────────────────────────────

# 关键词 → op 映射（按优先级排列，先匹配先赢；与 STAGE_KEYWORDS 平行独立运行）
OPERATION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("write", [
        "写,", "写。", "写 ", "写入", "更新", "改写", "改", "编辑", "写代码", "创建",
        "添加", "删除", "write", "update", "edit", "create", "delete", "add", "fix", "修改",
    ]),
    ("read", [
        "读一下", "阅读", "查看", "看一下", "理解", "解释", "分析", "总结", "概括",
        "read", "view", "explain", "summarize", "understand", "分析一下",
    ]),
    ("search", [
        "搜索", "查找", "找一下", "检索", "搜一下", "grep", "find", "search", "locate", "找找",
    ]),
    ("refactor", [
        "重构", "整理", "优化结构", "改结构", "refactor", "restructure", "reorganize",
        "clean up", "清理",
    ]),
]

# 显式命令前缀（优先级最高，与 EXPLICIT_PREFIX_RE 同风格但只接 op 名）
OPERATION_PREFIX_RE = re.compile(
    r"^/op\s+(write|read|search|refactor)\b",
    re.IGNORECASE,
)


def detect_operation(prompt: str) -> str | None:
    """
    返回检测到的 op 名，或 None（表示不更改当前 op）。
    优先级：显式命令 > 关键词匹配 > 不变
    与 detect_stage() 平行独立——两侧关键词可独立命中，proxy 端按 op 优先。
    """
    # 显式命令
    m = OPERATION_PREFIX_RE.match(prompt.strip())
    if m:
        return m.group(1).lower()

    # 关键词匹配（遍历顺序即优先级）
    prompt_lower = prompt.lower()
    for op, keywords in OPERATION_KEYWORDS:
        if any(kw in prompt_lower for kw in keywords):
            return op

    return None  # 不更改 op


# ── 项目根目录查找（参照 compact/utils.py 的 _find_project_root）──

def _find_project_root(start: Path, session_id: str | None = None) -> Path:
    """Walk up from ``start`` looking for a project boundary marker.

    Anchor strategy (the location of the first per-session file IS the lock):
      1. If session_id is known, walk up looking for an existing
         ``stage_<session_id>`` or ``session_state_<session_id>.json`` under
         ``.claude/``.  The directory containing it is the locked project root.
      2. Walk up looking for a ``.claude/`` config directory (skipping the
         global ``~/.claude`` unless we started inside it).
      3. Walk up looking for a ``.git/`` toplevel as fallback.
      4. Fall back to ``~/.claude`` so stage files always land under a known
         location instead of polluting the current working directory.

    Walks at most 20 levels.
    """
    p = start

    # 1. If session_id is available, walk up looking for an existing per-session
    #    file.  Its parent directory IS the project-root anchor.
    if session_id:
        anchor_p = start
        for _ in range(20):
            claude_dir = anchor_p / ".claude"
            if (claude_dir / f"stage_{session_id}").exists() or \
               (claude_dir / f"session_state_{session_id}.json").exists():
                return anchor_p
            parent = anchor_p.parent
            if parent == anchor_p:  # reached filesystem root
                break
            anchor_p = parent

    # 2. No existing per-session file — walk up looking for a boundary marker.
    git_root = None
    for _ in range(20):
        cand = p / ".claude"
        if cand.is_dir():
            # Skip the global ~/.claude unless we started inside it
            if cand != HOME_CLAUDE or str(start).startswith(str(HOME_CLAUDE) + os.sep):
                return p
        if git_root is None and (p / ".git").exists():
            git_root = p
        parent = p.parent
        if parent == p:  # reached filesystem root
            break
        p = parent

    if git_root is not None:
        return git_root
    return HOME_CLAUDE if HOME_CLAUDE.is_dir() else start


def _stage_file_path(cwd: str | Path, session_id: str) -> Path:
    """
    返回指定 session 的阶段文件路径：
    <project_root>/.claude/stage_<session_id>
    """
    cwd = Path(cwd) if isinstance(cwd, str) else cwd
    project_root = _find_project_root(cwd, session_id)
    claude_dir = project_root / ".claude"
    if not claude_dir.is_dir():
        claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / f"stage_{session_id}"


def _op_file_path(stage_file: Path) -> Path:
    """
    从 stage_<sid> 路径派生 op_<sid> 路径（同目录、仅前缀替换）。
    proxy.py 用同一规则从 active_session 指向的 stage_<sid> 路径派生。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "op_", 1))


def _model_file_path(stage_file: Path) -> Path:
    """
    从 stage_<sid> 路径派生 model_<sid> 路径（同目录、仅前缀替换）。
    model 文件存储用户显式指定的模型覆盖（最高路由优先级）。
    proxy.py 用同一规则从 active_session 指向的 stage_<sid> 路径派生。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "model_", 1))


def _read_stage_file(path: Path) -> str | None:
    """读取指定阶段文件，不存在时返回 None。"""
    try:
        content = path.read_text().strip()
        return content if content else None
    except FileNotFoundError:
        return None


def _ensure_session_stage(session_id: str, cwd: str | Path) -> str:
    """
    确保 stage_<session_id> 文件存在并更新 active_session 指针。

    文件不存在时从 current_stage 全局后备拷贝初始值，都没有则初始化为 "default"。
    每次 hook 触发都调用，保证 proxy 随时能找到当前 session 的阶段。

    active_session 存储的是阶段文件的**完整绝对路径**，proxy 可直接读取。

    Returns: 当前 session 的阶段名。
    """
    stage_path = _stage_file_path(cwd, session_id)
    if not stage_path.exists():
        # 继承全局后备的值作为 session 初始阶段
        initial = _read_stage_file(GLOBAL_STAGE_FILE) or "default"
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text(initial + "\n")
        log("INFO", f"初始化 stage_{session_id} = {initial} → {stage_path}")
    # 始终刷新 active_session 指针（存储完整路径，多 session 时最后活跃的获胜）
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_SESSION_FILE.write_text(str(stage_path))
    content = stage_path.read_text().strip()
    return content if content else "default"


def write_stage(stage: str, session_id: str | None = None,
                cwd: str | Path | None = None) -> None:
    """写入阶段。
    有 session_id+cwd → 写入 <project_root>/.claude/stage_<session_id> 并更新指针；
    无 → 写入全局后备文件 current_stage。
    """
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text(stage + "\n")
        HOOK_DIR.mkdir(parents=True, exist_ok=True)
        ACTIVE_SESSION_FILE.write_text(str(stage_path))
    else:
        GLOBAL_STAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_STAGE_FILE.write_text(stage + "\n")


def read_stage(session_id: str | None = None,
               cwd: str | Path | None = None) -> str:
    """
    读取当前阶段，优先级：
      1. 传入的 session_id+cwd → <project_root>/.claude/stage_<session_id>
      2. active_session 指针 → 读取其存储的完整路径文件
      3. 全局后备文件 → current_stage
      4. default
    """
    # 1. 指定 session（hook 场景：有 stdin 中的 session_id 和 cwd）
    if session_id and cwd:
        content = _read_stage_file(_stage_file_path(cwd, session_id))
        if content:
            return content

    # 2. active_session 指针 → 存储的是完整路径，直接读取
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

    # 4. 兜底
    return "default"


def read_operation(session_id: str | None = None,
                   cwd: str | Path | None = None) -> str | None:
    """
    读取当前 op，路径解析复用 _stage_file_path() 派生 op_<sid>。
    优先级：
      1. 传入的 session_id+cwd → 派生 op_<sid>
      2. active_session 指针 → 读取其指向的 stage_<sid>，再派生 op_<sid>
    返回 None 表示"无 op 信号"（与"未检测到 op"等价，proxy 走 stage 路由）。
    """
    # 1. hook 场景：有 session_id+cwd
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        content = _read_stage_file(_op_file_path(stage_path))
        if content:
            return content

    # 2. proxy / CLI 场景：从 active_session 指针拿到 stage_<sid> 路径再派生
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_op_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass

    return None  # 关键：返回 None 而非 "default"，让 proxy 知道"无 op 信号"


def write_operation(op: str, session_id: str | None = None,
                    cwd: str | Path | None = None) -> None:
    """写入 op 文件（op_<sid>）。与 stage 写文件同模式。
    无 session_id+cwd 时不写入（op 不设全局后备——op 是 per-prompt 临时态）。
    """
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        _op_file_path(stage_path).write_text(op + "\n")


def write_model_override(model: str, session_id: str | None = None,
                         cwd: str | Path | None = None) -> None:
    """写入 model 覆盖文件（model_<sid>）。与 op 写文件同模式。
    无 session_id+cwd 时不写入。
    """
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        _model_file_path(stage_path).write_text(model + "\n")


def clear_model_override(session_id: str | None = None,
                         cwd: str | Path | None = None) -> None:
    """清除 model 覆盖文件（model_<sid>），回到自动路由。"""
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        model_file = _model_file_path(stage_path)
        try:
            model_file.unlink(missing_ok=True)
        except Exception:
            pass  # 清理失败不阻塞 hook


def _fallback_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 fallback_<sid> 路径（同目录、仅前缀替换）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "fallback_", 1))


def read_fallback(session_id: str | None = None,
                  cwd: str | Path | None = None) -> str | None:
    """
    读取当前 session 的 sticky fallback 模型名。
    优先级：
      1. 传入的 session_id+cwd → 派生 fallback_<sid>
      2. active_session 指针 → 读取其指向的 stage_<sid>，再派生 fallback_<sid>
    返回 None 表示"无 sticky fallback"。
    """
    # 1. hook 场景：有 session_id+cwd
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        content = _read_stage_file(_fallback_file_path(stage_path))
        if content:
            return content

    # 2. proxy / CLI 场景：从 active_session 指针拿到 stage_<sid> 路径再派生
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_fallback_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass

    return None


def clear_fallback(session_id: str | None = None,
                   cwd: str | Path | None = None) -> None:
    """清除 sticky fallback 文件（fallback_<sid>），恢复主模型优先路由。"""
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        fb_file = _fallback_file_path(stage_path)
        try:
            fb_file.unlink(missing_ok=True)
        except Exception:
            pass  # 清理失败不阻塞 hook


def read_model_override(session_id: str | None = None,
                        cwd: str | Path | None = None) -> str | None:
    """
    读取当前 model 覆盖，路径解析复用 _stage_file_path() 派生 model_<sid>。
    优先级：
      1. 传入的 session_id+cwd → 派生 model_<sid>
      2. active_session 指针 → 读取其指向的 stage_<sid>，再派生 model_<sid>
    返回 None 表示"无 model 覆盖"——proxy 走 op/stage 路由。
    """
    # 1. hook 场景：有 session_id+cwd
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        content = _read_stage_file(_model_file_path(stage_path))
        if content:
            return content

    # 2. proxy / CLI 场景：从 active_session 指针拿到 stage_<sid> 路径再派生
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_model_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass

    return None


def main():
    log("INFO", "hook triggered")
    try:
        try:
            event = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError) as exc:
            log("WARN", f"stdin JSON parse failed: {exc!r}; passthrough")
            sys.exit(0)

        # ── 提取 session_id 和 cwd（项目根目录解析 + 分 session 管理的关键）──
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str = event.get("cwd", str(Path.cwd()))

        # ── 会话初始化：确保 stage_<session_id> 在 <project_root>/.claude/ 已创建 ──
        if session_id:
            old_stage = _ensure_session_stage(session_id, cwd)
        else:
            old_stage = read_stage()

        # UserPromptSubmit 的 prompt 字段
        prompt: str = event.get("prompt", "")
        if not prompt:
            log("INFO", "empty prompt, ensure stage file exists")
            sys.exit(0)

        # ── Model-override 检测（最高优先级，在 stage/op 之前）──
        new_model, is_reset = detect_model_override(prompt)
        old_model = read_model_override(session_id, cwd)

        model_msg: str | None = None
        if is_reset and old_model:
            clear_model_override(session_id, cwd)
            clear_fallback(session_id, cwd)  # 同时清除 sticky fallback
            log("INFO", f"model override cleared (was: {old_model})")
            model_msg = f"模型覆盖已清除（原: {old_model}），恢复自动路由"
        elif new_model and new_model != old_model:
            write_model_override(new_model, session_id, cwd)
            clear_fallback(session_id, cwd)  # 显式指定模型时清除 sticky fallback
            log("INFO", f"model override: {old_model} → {new_model}")
            model_msg = f"模型覆盖: {(old_model or 'none')} → {new_model}"
        elif new_model == old_model and new_model:
            log("INFO", f"model override unchanged: {new_model}")

        # ── Stage 检测 ──
        new_stage = detect_stage(prompt)

        # ── Operation-type 检测（与 stage 平行独立，proxy 端按 op 优先）──
        new_op = detect_operation(prompt)
        old_op = read_operation(session_id, cwd)

        # ── Stage 写入/通知 ──
        stage_msg: str | None = None
        if new_stage and new_stage != old_stage:
            write_stage(new_stage, session_id, cwd)
            log("INFO", f"stage: {old_stage} → {new_stage}")
            from stage_config import STAGE_INFO
            info = STAGE_INFO.get(new_stage, "")
            stage_msg = (
                f"阶段已切换: {old_stage} → {new_stage}"
                + (f"（{info}）" if info else "")
            )
        elif new_stage == old_stage:
            log("INFO", f"stage unchanged: {old_stage}")
        else:
            log("INFO", "no stage signal, passthrough")

        # ── Op 写入/通知（与 stage 完全独立的 if-else 链）──
        op_msg: str | None = None
        if new_op and new_op != old_op:
            write_operation(new_op, session_id, cwd)
            log("INFO", f"op: {old_op} → {new_op}")
            from stage_config import OPERATION_INFO
            info = OPERATION_INFO.get(new_op, "")
            op_msg = (
                f"操作类型: {(old_op or 'none')} → {new_op}"
                + (f"（{info}）" if info else "")
            )
        elif new_op == old_op:
            log("INFO", f"op unchanged: {old_op}")
        else:
            log("INFO", "no op signal, passthrough")

        # ── Sticky Fallback 通知（用户未显式覆盖模型时提示）──
        fb_msg: str | None = None
        if not new_model and not is_reset:
            fb_model = read_fallback(session_id, cwd)
            if fb_model:
                log("INFO", f"sticky fallback active: {fb_model}")
                fb_msg = (
                    f"主模型曾不可用，已自动切换至备用 {fb_model}"
                )

        # ── 输出 additionalContext（model/stage/op/fallback 各自命中时合并提示）──
        msgs = [m for m in (model_msg, stage_msg, op_msg, fb_msg) if m]
        if msgs:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n[Stage Router] " + "；".join(msgs) + "\n",
                }
            }
            print(json.dumps(output))
    except Exception as exc:
        # 兜底：任何未捕获异常都吞掉，绝不让 hook 退非零码
        log("ERROR", f"unexpected: {exc!r}; passthrough")
    sys.exit(0)


if __name__ == "__main__":
    main()
