#!/usr/bin/env python3
"""
stage_detector.py — UserPromptSubmit Hook
==========================================
在用户每次提交 prompt 前触发，分析关键词，自动推断并写入当前阶段。
阶段一旦写入，后续所有请求都会被代理路由到对应模型。

支持两种写入方式：
  1. 关键词自动检测（本文件负责）
  2. 用户显式前缀，如：~stage implement → 写入 implement

阶段文件存储位置：
  分 session 阶段文件 stage_<session_id> 存放在 <项目根目录>/.claude/ 下，
  与 session_state_<session_id>.json 路径规则一致。
  active_session 指针文件存放在 ~/.claude/hooks/model_router/，存储的是
  阶段文件的完整绝对路径，供 proxy.py（无 stdin 上下文）直接读取。

Operation-type（第二维度，2026-06-13 引入）：
  与 stage 并列独立信号，由关键词或显式前缀触发：
    ~write / ~read / ~search / ~refactor
  op 文件位置：<project_root>/.claude/op_<session_id>，与 stage_<sid> 同目录，
  仅前缀不同。proxy.py 端在 stage 路由之上叠加：检出 op → 完全覆盖 stage。

Model-override（用户显式指定模型，2026-06-13 引入，最高优先级）：
  用户可通过 ~model / ~m 前缀或自然语言（use / 用）指定模型简称：
    ~model ds-v4-pro   ~m mm3   use deepseek-v4-flash   用 mm3
  model 文件位置：<project_root>/.claude/model_<session_id>，与 stage_<sid>
  同目录，仅前缀不同。proxy.py 端路由优先级：model_override > op > stage。

Task-Pattern（任务模式识别，2026-06-14 引入，Shadow Mode）：
  识别 prompt 属于 feature / bugfix / refactor / test / research /
  migration / architecture / docs / audit 中的哪种，**仅作为标注数据**
  写入 pattern_<session_id>（JSON：`{"prediction","confidence","ts"}`），
  **不影响路由决策**。这是阶段 A：积累标注 → 阶段 B：启用 Adaptive Routing。
  显式 ~pattern <name> 指令可强制标注。

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

# 将同目录加入 sys.path，确保 `from stage_config import ...` 与
# `from model_alias import ...` 在 Hook 直接执行时也能 import 到。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_alias import detect_model_override  # 用户模型覆盖（最高路由优先级）

# 阶段复杂度阈值（simple ≤ X，medium ≤ Y，> Y = complex）
# 见 stage_config.COMPLEXITY_THRESHOLDS
try:
    from stage_config import COMPLEXITY_THRESHOLDS  # noqa: E402
except Exception:
    COMPLEXITY_THRESHOLDS = {"simple": 30, "medium": 70}

# ── 分 session 阶段管理 ──
# 每个 session 独立管理阶段，避免多会话互相覆盖。
# 命名规则：stage_<session_id>（参照 hooks/session 的 session_state_<session_id> 模式）
# 存放位置：<project_root>/.claude/stage_<session_id>
# active_session 指针文件固定在 ~/.claude/hooks/model_router/，存储阶段文件的完整绝对路径。
HOME_CLAUDE         = Path.home() / ".claude"
HOOK_DIR            = HOME_CLAUDE / "hooks" / "model_router"
ACTIVE_SESSION_FILE = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE   = HOOK_DIR / "current_stage"   # 全局后备（无 session_id 时使用）
STATE_INDEX_FILE    = HOOK_DIR / "state_index.json"  # 设计文档 §13 Project Binding
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
    # explore 放最前：读代码/追调用链/理解现状是高频任务
    # 2026-06-14 §7 D7-1 修复：补全 explore stage（设计文档第 7 章）
    ("explore",    [
        "读代码", "看代码", "理解", "追调用", "调用链", "看日志", "分析现状",
        "定位", "了解一下", "搞清楚", "read code", "understand", "trace",
        "investigate", "explore", "调研", "排查", "现状", "调用栈",
        "哪里调", "怎么实现的", "梳理",
    ]),
    ("brainstorm", [
        "头脑风暴", "brainstorm", "想法", "创意", "idea",
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
        "实现", "实施", "implement", "写代码", "开发", "develop",
        "写", "修", "build", "create", "fix", "修复", "add", "添加",
    ]),
    # test 放 audit 之前：测试任务独立识别，避免被 audit 吞掉
    # 2026-06-14 §7 D7-2 修复：补全 test stage
    ("test",       [
        "跑测试", "跑一下测试", "跑用例", "写测试", "测试覆盖率",
        "unit test", "单元测试", "回归测试", "run test", "run tests",
        "run the test", "execute test", "覆盖率", "回归验证",
        "pytest", "jest", "mocha",
    ]),
    ("audit",      [
        "审计", "audit", "review", "检查", "code review", "安全",
        "security", "漏洞", "验证", "verify",
        "质量", "quality", "检验",
    ]),
]

# 显式命令前缀（优先级最高）
EXPLICIT_PREFIX_RE = re.compile(
    r"(?:^|\s)~stage\s+(brainstorm|decide|design|plan|implement|audit|default|explore|test)\b",
    re.IGNORECASE,
)


def detect_stage(prompt: str) -> str | None:
    """
    返回检测到的阶段名，或 None（表示不更改当前阶段）。
    优先级：显式命令 > 关键词匹配 > 不变
    """
    # 显式命令
    m = EXPLICIT_PREFIX_RE.search(prompt.strip())
    if m:
        return m.group(1).lower()

    # 关键词匹配（遍历顺序即优先级）
    prompt_lower = prompt.lower()
    for stage, keywords in STAGE_KEYWORDS:
        if any(kw in prompt_lower for kw in keywords):
            return stage

    return None  # 不更改阶段


# ────────────────────────────────────────────────────────────────────
# Operation-type — [已废弃 2026-06-14]
# ────────────────────────────────────────────────────────────────────
# 废弃原因：write/read/search 只是"动作"不是"路由维度"。
# Complexity 分类器（设计文档 §6.4）已吞掉 op 的原始职责。
# 下方所有 op 相关函数均已替换为 no-op stub，调用方不会出错。
# 原有实现以注释形式保留，便于未来参考或回退。
#
# OPERATION_KEYWORDS: list[tuple[str, list[str]]] = [
#     ("write", [
#         "写,", "写。", "写 ", "写入", "更新", "改写", "改", "编辑", "写代码", "创建",
#         "添加", "删除", "write", "update", "edit", "create", "delete", "add", "fix", "修改",
#     ]),
#     ("read", [
#         "读一下", "阅读", "查看", "看一下", "理解", "解释", "分析", "总结", "概括",
#         "read", "view", "explain", "summarize", "understand", "分析一下",
#     ]),
#     ("search", [
#         "搜索", "查找", "找一下", "检索", "搜一下", "grep", "find", "search", "locate", "找找",
#     ]),
#     ("refactor", [
#         "重构", "整理", "优化结构", "改结构", "refactor", "restructure", "reorganize",
#         "clean up", "清理",
#     ]),
# ]
#
# OPERATION_PREFIX_RE = re.compile(
#     r"(?:^|\s)~(write|read|search|refactor)\b",
#     re.IGNORECASE,
# )
# ────────────────────────────────────────────────────────────────────


# detect_operation — [已废弃 2026-06-14]
# 原实现以注释保留：
# def detect_operation(prompt: str) -> str | None:
#     """
#     返回检测到的 op 名，或 None（表示不更改当前 op）。
#     优先级：显式命令 > 关键词匹配 > 不变
#     与 detect_stage() 平行独立——两侧关键词可独立命中，proxy 端按 op 优先。
#     """
#     # 显式命令
#     m = OPERATION_PREFIX_RE.search(prompt.strip())
#     if m:
#         return m.group(1).lower()
#
#     # 关键词匹配（遍历顺序即优先级）
#     prompt_lower = prompt.lower()
#     for op, keywords in OPERATION_KEYWORDS:
#         if any(kw in prompt_lower for kw in keywords):
#             return op
#
#     return None  # 不更改 op


def detect_operation(prompt: str) -> str | None:
    """[已废弃 2026-06-14] 始终返回 None。
    op 路由已由 Complexity（§6.4）替代。
    """
    return None



# ────────────────────────────────────────────────────────────────────
# Task Pattern（设计文档第 6.2 / 8 章）—— 2026-06-14 引入，Shadow Mode
#
# 与 stage/op 完全并列的**第三维度**信息，专门描述"任务是什么类型"。
# 阶段 A（当前）：仅记录 pattern + confidence 到 pattern_<sid> 文件 + log，
#                 **不影响路由决策**。积累标注数据后，进入阶段 B 再启用
#                 Adaptive Routing。
#
# 与 stage/op 的关键区别：
#   - 多个 pattern 可以并存（用 confidence 排序），不像 stage/op 是单值覆盖
#   - 走 JSON 格式而非纯文本：{"prediction": "...", "confidence": 0.73}
#   - 永远不阻塞 stage/op 路由（Shadow Mode）
# ────────────────────────────────────────────────────────────────────

# 关键词 → pattern 映射（每个 pattern 是 (关键词, 权重) 列表；权重越高越确定）
# 遍历顺序 = 同等权重时的优先级。
PATTERN_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "feature": [
        ("新增功能", 3), ("添加功能", 3), ("加个功能", 2), ("新增字段", 2),
        ("新功能", 2), ("做一个", 1), ("实现一个", 1),
        ("new feature", 3), ("add feature", 3), ("implement feature", 3),
        ("support ", 1), ("支持 ", 1), ("实现", 1), ("加", 1),
    ],
    "bugfix": [
        ("bug", 3), ("fix", 3), ("修复", 3), ("defect", 3),
        ("崩溃", 3), ("crash", 3), ("异常", 2), ("报错", 2), ("error", 2),
        ("修", 1),
    ],
    "refactor": [
        ("refactor", 3), ("重构", 3), ("整理", 2), ("优化结构", 3),
        ("restructure", 3), ("reorganize", 2), ("改结构", 3), ("清理", 1),
    ],
    "test": [
        ("写测试", 3), ("补测试", 3), ("单元测试", 3), ("unit test", 3),
        ("integration test", 3), ("test case", 2), ("测试", 1),
    ],
    "research": [
        ("调研", 3), ("research", 3), ("比较方案", 2), ("对比", 1),
        ("evaluate", 2), ("哪个好", 1), ("选哪个", 1), ("查一下", 1),
    ],
    "migration": [
        ("migration", 3), ("migrate", 3), ("迁移", 3), ("迁到", 2),
        ("迁过去", 2), ("升级", 2), ("upgrade", 2),
        ("迁移到", 3), ("升级到", 2), ("改造", 2),
    ],
    "architecture": [
        ("架构", 3), ("architecture", 3), ("系统设计", 3), ("顶层设计", 3),
        ("整体方案", 2), ("技术选型", 2), ("模块划分", 3),
    ],
    "docs": [
        ("写文档", 3), ("写说明", 3), ("readme", 3), ("comment", 2),
        ("注释", 1), ("注释一下", 2), ("documentation", 3), ("docs", 2),
    ],
    "audit": [
        ("code review", 3), ("安全审查", 3), ("安全审计", 3), ("security review", 3),
        ("审计", 3), ("漏洞", 2), ("vulnerability", 3), ("性能审查", 2),
    ],
}

# 显式命令前缀（最高优先级）
PATTERN_PREFIX_RE = re.compile(
    r"(?:^|\s)~pattern\s+(feature|bugfix|refactor|test|research|migration|architecture|docs|audit)\b",
    re.IGNORECASE,
)


def detect_task_pattern(prompt: str) -> tuple[str | None, float]:
    """
    Shadow-Mode 模式检测。

    返回 (pattern_name, confidence)：
      - pattern_name=None  → 未识别到任何 pattern
      - confidence ∈ [0, 1]：累计权重归一化后的置信度

    多个 pattern 可并存时，返回权重最高的一个作为主 pattern。
    整体行为：**不修改任何路由**——只读取 prompt、产出标注、由调用方决定
    是否写入日志/文件/上报 ROC。
    """
    if not prompt:
        return (None, 0.0)

    # 1. 显式 ~pattern 指令
    m = PATTERN_PREFIX_RE.search(prompt.strip())
    if m:
        return (m.group(1).lower(), 1.0)

    # 2. 关键词加权计票
    prompt_lower = prompt.lower()
    scores: dict[str, int] = {}
    for pattern, kw_weights in PATTERN_KEYWORDS.items():
        score = sum(w for kw, w in kw_weights if kw in prompt_lower)
        if score > 0:
            scores[pattern] = score

    if not scores:
        return (None, 0.0)

    # 取权重最高的 pattern
    best = max(scores.items(), key=lambda kv: kv[1])
    # 归一化：score / (score + 4) → 大致把 score 4 当作 confidence 0.8
    confidence = best[1] / (best[1] + 4)
    return (best[0], round(confidence, 2))


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


# _op_file_path — [已废弃 2026-06-14]
# 原实现：
# def _op_file_path(stage_file: Path) -> Path:
#     """从 stage_<sid> 路径派生 op_<sid> 路径。"""
#     return stage_file.with_name(stage_file.name.replace("stage_", "op_", 1))


def _op_file_path(stage_file: Path) -> Path:
    """[已废弃 2026-06-14] op 路由已由 Complexity 替代。
    保留 stub 确保调用方（_reset_session_files）不报错。
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

    新 session 始终从 "default" 初始化，stage 应从用户 prompt 中检测。
    不继承 global current_stage，避免上一个 session 残留的责任状态污染新 session。

    active_session 存储的是阶段文件的**完整绝对路径**，proxy 可直接读取。

    Returns: 当前 session 的阶段名。
    """
    stage_path = _stage_file_path(cwd, session_id)
    if not stage_path.exists():
        # 新 session 始终从 default 开始，stage 由 detect_stage() 从 prompt 检测
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text("default\n")
        log("INFO", f"初始化 stage_{session_id} = default → {stage_path}")
    # 始终刷新 active_session 指针（存储完整路径，多 session 时最后活跃的获胜）
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_SESSION_FILE.write_text(str(stage_path))
    # 维护 state_index.json（设计文档 §13 Project Binding）：
    # 按 project_root 索引 session_id + stage + last_active，proxy 优先用此查找。
    try:
        project_root = str(_find_project_root(Path(cwd) if isinstance(cwd, str) else cwd, session_id))
        _update_state_index(project_root, session_id, stage_path)
    except Exception as exc:
        log("WARN", f"state_index 更新失败（非阻塞）: {exc!r}")
    content = stage_path.read_text().strip()
    return content if content else "default"


def _load_state_index() -> dict:
    """读取 state_index.json，文件缺失/损坏时返回空 dict。"""
    try:
        content = STATE_INDEX_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return data
        return {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state_index(idx: dict) -> None:
    """原子写 state_index.json：先写 .tmp 再 rename，避免半写。"""
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_INDEX_FILE)


def _update_state_index(project_root: str, session_id: str, stage_path: Path) -> None:
    """更新 state_index.json 中 project_root 记录的 session_id 和 last_active。
    同一 project_root 多 session 时以 last_active 最新者为准（与 active_session 语义一致）。
    """
    idx = _load_state_index()
    try:
        stage_name = stage_path.read_text().strip() or "default"
    except FileNotFoundError:
        stage_name = "default"
    idx[project_root] = {
        "session_id":  session_id,
        "stage":       stage_name,
        "last_active": int(datetime.now(timezone.utc).timestamp()),
    }
    _save_state_index(idx)


def write_stage(stage: str, session_id: str | None = None,
                cwd: str | Path | None = None) -> None:
    """写入阶段。
    有 session_id+cwd → 写入 <project_root>/.claude/stage_<session_id> 并更新指针；
    无 → 写入全局后备文件 current_stage（仅 stage CLI 工具的 legacy 路径）。
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
    读取当前阶段：
      1. 传入的 session_id+cwd → <project_root>/.claude/stage_<session_id>
      2. active_session 指针 → 读取其存储的完整路径文件
      3. 返回 default（无全局后备，每个 session 独立维护 stage_<sid>）
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

    # 3. 兜底（无任何 session 信息时用 default）
    return "default"


# read_operation — [已废弃 2026-06-14]
# 原实现：
# def read_operation(session_id: str | None = None,
#                    cwd: str | Path | None = None) -> str | None:
#     """...""" (完整实现见 git history)


def read_operation(session_id: str | None = None,
                   cwd: str | Path | None = None) -> str | None:
    """[已废弃 2026-06-14] 始终返回 None。
    op 路由已由 Complexity（§6.4）替代。
    """
    return None


# write_operation — [已废弃 2026-06-14]
# 原实现：
# def write_operation(op: str, session_id: str | None = None,
#                     cwd: str | Path | None = None) -> None:
#     """...""" (完整实现见 git history)


def write_operation(op: str, session_id: str | None = None,
                    cwd: str | Path | None = None) -> None:
    """[已废弃 2026-06-14] no-op。
    op 路由已由 Complexity（§6.4）替代。
    """
    pass


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


def _pattern_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 pattern_<sid> 路径（同目录、仅前缀替换）。
    Shadow Mode 写的是 JSON：`{"prediction": "...", "confidence": 0.73}`。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "pattern_", 1))


def read_pattern(session_id: str | None = None,
                 cwd: str | Path | None = None) -> dict | None:
    """
    读取当前 session 的 task pattern 标注（Shadow Mode 专用）。

    路径解析复用 _stage_file_path() 派生 pattern_<sid>。
    返回 dict：{"prediction": str, "confidence": float, "ts": str}
    返回 None 表示"无 pattern 标注"。
    """
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        pattern_file = _pattern_file_path(stage_path)
    else:
        # 无 session_id+cwd 时从 active_session 指针反推
        try:
            active_path = ACTIVE_SESSION_FILE.read_text().strip()
            if not active_path:
                return None
            pattern_file = _pattern_file_path(Path(active_path))
        except FileNotFoundError:
            return None

    try:
        content = pattern_file.read_text().strip()
        if not content:
            return None
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_pattern(prediction: str, confidence: float,
                  session_id: str | None = None,
                  cwd: str | Path | None = None) -> None:
    """写入 pattern 标注到 pattern_<sid>（JSON 格式）。

    Shadow Mode 关键点：
      - 不影响 stage/op 路由（proxy.py 不读 pattern 文件）
      - 仅作为标注数据收集，未来阶段 B 启用 Adaptive Routing 时使用
    """
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "prediction":   prediction,
        "confidence":   round(float(confidence), 2),
        "ts":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _pattern_file_path(stage_path).write_text(json.dumps(payload, ensure_ascii=False))


def clear_pattern(session_id: str | None = None,
                  cwd: str | Path | None = None) -> None:
    """清除 pattern_<sid> 标注。"""
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    try:
        _pattern_file_path(stage_path).unlink(missing_ok=True)
    except Exception:
        pass


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

        # ── LLM 轻量分类器（设计文档 §6.2/§6.4/§10 合并实现）──
        # 一次 LLM 调用获取 stage + pattern + complexity 三维分类。
        # 网络/超时/解析失败时静默回退到 V1 关键词启发式，不阻塞 hook。
        llm_result: dict | None = None
        if session_id and cwd:
            try:
                from llm_classifier import classify  # noqa: E402
                llm_result = classify(prompt)
                log("INFO",
                    f"LLM classifier: stage={llm_result['stage']} "
                    f"pattern={llm_result['pattern']} "
                    f"(conf={llm_result['pattern_confidence']}) "
                    f"complexity={llm_result['complexity_label']} "
                    f"(score={llm_result['complexity_score']}, "
                    f"conf={llm_result['complexity_confidence']}) "
                    f"reason={llm_result.get('reasoning', '')!r}"
                )
            except Exception as e:
                log("WARN", f"LLM classifier failed, fallback to V1 heuristic: {e!r}")
                # llm_result 保持 None → 下游走 V1 关键词分支

        # ── Stage 检测 ──
        # 优先级：显式 ~stage > LLM 分类器 > V1 关键词
        new_stage = detect_stage(prompt)
        if new_stage is None and llm_result:
            # 无显式 ~stage 指令时，用 LLM 分类结果
            llm_stage = llm_result.get("stage", "")
            if llm_stage in {
                "explore", "brainstorm", "decide", "design", "plan",
                "implement", "test", "audit", "default",
            }:
                new_stage = llm_stage
                log("INFO", f"stage from LLM: {new_stage}")

        # ── Operation-type 检测 — [已废弃 2026-06-14] ──
        # write/read/search 只是动作不是路由维度，Complexity（§6.4）已接管。
        # detect_operation() 始终返回 None，write_operation() 是 no-op。
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

        # ── Op 写入/通知 — [已废弃 2026-06-14] ──
        # 原逻辑：new_op != old_op 时写 op_<sid> + 通知用户。
        # detect_operation() 始终返回 None，此块永远走 "no op signal"。
        # op_msg 固定为 None，下方用户通知拼接直接跳过。
        # 原实现：
        # op_msg: str | None = None
        # if new_op and new_op != old_op:
        #     write_operation(new_op, session_id, cwd)
        #     log("INFO", f"op: {old_op} → {new_op}")
        #     from stage_config import OPERATION_INFO
        #     info = OPERATION_INFO.get(new_op, "")
        #     op_msg = (
        #         f"操作类型: {(old_op or 'none')} → {new_op}"
        #         + (f"（{info}）" if info else "")
        #     )
        # elif new_op == old_op:
        #     log("INFO", f"op unchanged: {old_op}")
        # else:
        #     log("INFO", "no op signal, passthrough")
        op_msg: str | None = None
        log("INFO", "op detection disabled (deprecated 2026-06-14), passthrough")

        # ── Sticky Fallback 通知（用户未显式覆盖模型时提示）──
        fb_msg: str | None = None
        if not new_model and not is_reset:
            fb_model = read_fallback(session_id, cwd)
            if fb_model:
                log("INFO", f"sticky fallback active: {fb_model}")
                fb_msg = (
                    f"主模型曾不可用，已自动切换至备用 {fb_model}"
                )

        # ── Task Pattern 检测（Shadow Mode，2026-06-14 引入）──
        #   - 仅记录到 pattern_<sid> + 日志，**不影响路由**
        #   - 阶段 B 启用 Adaptive Routing 后才进入 proxy 决策
        # 优先级：显式 ~pattern > LLM 分类器 > V1 关键词
        pattern_msg: str | None = None
        if session_id and cwd:
            # 检查显式 ~pattern 指令
            pm = PATTERN_PREFIX_RE.search(prompt.strip())
            if pm:
                new_pattern, new_conf = pm.group(1).lower(), 1.0
            elif llm_result and llm_result.get("pattern"):
                new_pattern = llm_result["pattern"]
                new_conf = llm_result.get("pattern_confidence", 0.5)
                log("INFO", f"pattern from LLM: {new_pattern} (conf={new_conf})")
            else:
                new_pattern, new_conf = detect_task_pattern(prompt)
            old_pattern_data = read_pattern(session_id, cwd)
            old_pattern = old_pattern_data.get("prediction") if old_pattern_data else None
            if new_pattern:
                if new_pattern != old_pattern:
                    write_pattern(new_pattern, new_conf, session_id, cwd)
                    log("INFO",
                        f"pattern (shadow): {old_pattern or 'none'} → "
                        f"{new_pattern} (confidence={new_conf})")
                    pattern_msg = (
                        f"任务模式识别: {(old_pattern or 'none')} → "
                        f"{new_pattern} (confidence={new_conf}) [shadow]"
                    )
                else:
                    log("INFO",
                        f"pattern (shadow) unchanged: "
                        f"{new_pattern} (confidence={new_conf})")
            else:
                log("INFO", "pattern (shadow): no signal")

        # ── Stage Complexity 评估（设计文档 §6.4）──
        #   优先级：~careful/~quick 显式调档 > auto 检测 > PATTERN 默认
        complexity_msg: str | None = None
        if session_id and cwd:
            shift_m = COMPLEXITY_SHIFT_RE.search(prompt)
            batch_m = BATCH_RE.search(prompt)
            reset_m = RESET_RE.search(prompt)

            if reset_m:
                # ~reset — 全量清除 override（包括 model/op/pattern/fallback/
                #         complexity/batch），stage 保留
                removed = clear_all_overrides(session_id, cwd)
                log("INFO", f"~reset cleared {removed} override files")
                complexity_msg = f"~reset: 已清除 {removed} 个 override 文件"

            elif shift_m:
                # ~careful / ~quick — 在当前评估基础上 +/- 1 档
                action = shift_m.group(1).lower()
                delta = 1 if action == "careful" else -1
                cur = read_complexity(session_id, cwd)
                if cur is None:
                    # 没有现成评估 → 先做一次 auto 检测再调档
                    auto = detect_complexity(prompt, new_pattern)
                    cur_label = auto["label"]
                else:
                    cur_label = cur.get("label", "medium")
                new_label = shift_complexity(cur_label, delta)
                # 调档后重新映射到 0~100 分数（取该 label 的中位数）
                new_score = {
                    "simple":  20,
                    "medium":  50,
                    "complex": 80,
                }[new_label]
                write_complexity(
                    new_score, new_label,
                    confidence=0.95, source=action,
                    session_id=session_id, cwd=cwd,
                )
                log("INFO", f"~{action}: complexity {cur_label} → {new_label}")
                complexity_msg = (
                    f"~{action}: 复杂度 {cur_label} → {new_label}（强制）"
                )

            elif batch_m:
                # ~batch <template> — 载入预定义任务模式
                from stage_config import PATTERN_CONFIG
                template = batch_m.group(1).lower()
                flow = PATTERN_CONFIG.get(template, {}).get("default_flow", [])
                if flow:
                    write_batch(template, flow, session_id, cwd)
                    log("INFO", f"~batch loaded: {template} → {flow}")
                    complexity_msg = (
                        f"~batch: 已载入 {template} 流程 {'→'.join(flow)}"
                    )
                else:
                    log("WARN", f"~batch: unknown template {template}")

            else:
                # auto 检测：优先 LLM，失败回退 V1 关键词
                if llm_result:
                    auto_score = llm_result["complexity_score"]
                    auto_label = llm_result["complexity_label"]
                    auto_conf = llm_result["complexity_confidence"]
                    log("INFO", "complexity from LLM")
                elif new_pattern:
                    auto = detect_complexity(prompt, new_pattern)
                    auto_score = auto["score"]
                    auto_label = auto["label"]
                    auto_conf = auto["confidence"]
                else:
                    auto = detect_complexity(prompt, None)
                    auto_score = auto["score"]
                    auto_label = auto["label"]
                    auto_conf = auto["confidence"]
                write_complexity(
                    auto_score, auto_label,
                    confidence=auto_conf, source="auto",
                    session_id=session_id, cwd=cwd,
                )
                log("INFO",
                    f"complexity (auto): score={auto_score} "
                    f"label={auto_label} conf={auto_conf}")

        # ── 输出 additionalContext（model/stage/op/fallback/pattern/complexity 各自命中时合并提示）──
        msgs = [m for m in (model_msg, stage_msg, op_msg, fb_msg, pattern_msg, complexity_msg) if m]
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


# ────────────────────────────────────────────────────────────────────
# Stage Complexity Classifier（设计文档第 6.4 / 9 章）—— 2026-06-14 引入
#
# 复杂度按 0~100 连续分映射到 simple/medium/complex：
#   simple    — 0~30   单文件、单步骤、需求明确
#   medium    — 31~70  多步骤、轻度设计
#   complex   — 71~100 跨模块/跨系统/高风险
#
# 当前为 V1（关键词 + 长度 + pattern 加权），用于 ~careful/~quick 调档
# 与 proxy Workflow Planner 选模型序列。
#
# §14 配置单源化（D9-3 修复 2026-06-14）：COMPLEXITY_KEYWORDS / PATTERN_BASE_SCORE
# 已迁移到 stage_config.py，本文件改为派生读取。
# §9 设计原则（D9-1 修复 2026-06-14）：detect_complexity 现在接收 stage 参数，
# 引入 STAGE_COMPLEXITY_MULTIPLIER 让"探索/设计/审计"等阶段影响复杂度评分。
# ────────────────────────────────────────────────────────────────────

from stage_config import (  # noqa: E402  配置单源化派生
    COMPLEXITY_KEYWORDS,
    COMPLEXITY_THRESHOLDS,
    PATTERN_BASE_SCORE,
    STAGE_COMPLEXITY_MULTIPLIER,
)


def _score_to_label(score: int) -> str:
    """0~100 分数 → simple/medium/complex 标签。"""
    if score <= COMPLEXITY_THRESHOLDS["simple"]:
        return "simple"
    if score <= COMPLEXITY_THRESHOLDS["medium"]:
        return "medium"
    return "complex"


def detect_complexity(prompt: str, pattern: str | None = None,
                      stage: str | None = None) -> dict:
    """
    计算当前 prompt 的复杂度评分。

    返回：{"score": int, "label": "simple"|"medium"|"complex",
           "confidence": float, "signals": list[str]}

    实现思路（V1 启发式，V2 可替换为 LLM 分类器）：
      1. 基础分：若已识别 pattern，从 PATTERN_BASE_SCORE 起步；否则 medium=50
      2. Stage 倍率：基于当前阶段（设计文档 §9 原则："复杂度必须基于当前阶段判断"）
      3. 关键词加权：扫描 COMPLEXITY_KEYWORDS，累加权重
      4. 长度加成：>200 字加 5，>500 字加 10（长 prompt 通常任务更复杂）
      5. 文件提及：出现"X 个文件"/"多个"等加 5
      6. 夹紧到 [0, 100]
      7. confidence：依据触发的信号数，0 个信号 → 0.3，≥3 个 → 0.85

    参数：
      prompt  — 用户输入文本
      pattern — 已识别的任务模式（feature / bugfix / ...）
      stage   — 当前工作阶段（explore / design / audit / ...，设计文档 §9 原则）
    """
    signals: list[str] = []
    if not prompt:
        return {"score": 50, "label": "medium", "confidence": 0.3, "signals": []}

    # 1. 基础分
    if pattern and pattern in PATTERN_BASE_SCORE:
        score = PATTERN_BASE_SCORE[pattern]
        signals.append(f"pattern={pattern}({score})")
    else:
        score = 50  # medium 起步
        signals.append("base=medium(50)")

    # 2. Stage 倍率（§9 D9-1 修复）
    if stage and stage in STAGE_COMPLEXITY_MULTIPLIER:
        mult = STAGE_COMPLEXITY_MULTIPLIER[stage]
        score = int(score * mult)
        signals.append(f"stage={stage}(×{mult})")

    # 3. 关键词
    prompt_lower = prompt.lower()
    for kw, w in COMPLEXITY_KEYWORDS:
        if kw in prompt_lower:
            score += w
            signals.append(f"{kw}({w:+d})")

    # 4. 长度加成
    char_count = len(prompt)
    if char_count > 500:
        score += 10
        signals.append(f"len>500(+10)")
    elif char_count > 200:
        score += 5
        signals.append(f"len>200(+5)")

    # 5. "多个 / X 个" 加成
    if any(p in prompt for p in ("多个", "若干", "几处", "一系列")):
        score += 5
        signals.append("multi-entity(+5)")

    # 6. 夹紧
    score = max(0, min(100, score))

    # 7. confidence
    n_signals = len(signals)
    confidence = min(0.85, 0.3 + n_signals * 0.12)
    confidence = round(confidence, 2)

    return {
        "score":      score,
        "label":      _score_to_label(score),
        "confidence": confidence,
        "signals":    signals,
    }


# ────────────────────────────────────────────────────────────────────
# Complexity / Batch 文件（per-session）
# ────────────────────────────────────────────────────────────────────

def _complexity_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 complexity_<sid> 路径（同目录、仅前缀替换）。
    存储 JSON：{"score": int, "label": str, "confidence": float, "ts": str, "source": str}。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "complexity_", 1))


def _batch_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 batch_<sid> 路径（同目录、仅前缀替换）。
    存储 JSON：{"template": str, "flow": list[str], "ts": str}。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "batch_", 1))


def read_complexity(session_id: str | None = None,
                    cwd: str | Path | None = None) -> dict | None:
    """读取当前 session 的 complexity 评估结果。"""
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        complexity_file = _complexity_file_path(stage_path)
    else:
        try:
            active_path = ACTIVE_SESSION_FILE.read_text().strip()
            if not active_path:
                return None
            complexity_file = _complexity_file_path(Path(active_path))
        except FileNotFoundError:
            return None
    try:
        content = complexity_file.read_text().strip()
        if not content:
            return None
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_complexity(score: int, label: str, confidence: float,
                     source: str, session_id: str | None = None,
                     cwd: str | Path | None = None) -> None:
    """写入 complexity 评估到 complexity_<sid>（JSON 格式）。
    source: "auto" | "careful" | "quick" | "batch" 标识来源。
    """
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "score":      int(score),
        "label":      label,
        "confidence": round(float(confidence), 2),
        "source":     source,
        "ts":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _complexity_file_path(stage_path).write_text(
        json.dumps(payload, ensure_ascii=False)
    )


def clear_complexity(session_id: str | None = None,
                     cwd: str | Path | None = None) -> None:
    """清除 complexity_<sid> 文件。"""
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    try:
        _complexity_file_path(stage_path).unlink(missing_ok=True)
    except Exception:
        pass


def read_batch(session_id: str | None = None,
               cwd: str | Path | None = None) -> dict | None:
    """读取当前 session 的 batch 模板载入信息。"""
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        batch_file = _batch_file_path(stage_path)
    else:
        try:
            active_path = ACTIVE_SESSION_FILE.read_text().strip()
            if not active_path:
                return None
            batch_file = _batch_file_path(Path(active_path))
        except FileNotFoundError:
            return None
    try:
        content = batch_file.read_text().strip()
        if not content:
            return None
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_batch(template: str, flow: list[str],
                session_id: str | None = None,
                cwd: str | Path | None = None) -> None:
    """写入 batch 模板到 batch_<sid>。"""
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "template": template,
        "flow":     list(flow),
        "ts":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _batch_file_path(stage_path).write_text(
        json.dumps(payload, ensure_ascii=False)
    )


def clear_batch(session_id: str | None = None,
                cwd: str | Path | None = None) -> None:
    """清除 batch_<sid> 文件。"""
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    try:
        _batch_file_path(stage_path).unlink(missing_ok=True)
    except Exception:
        pass


def clear_all_overrides(session_id: str | None = None,
                        cwd: str | Path | None = None) -> int:
    """~reset 全量清除：删除 model/op/pattern/fallback/complexity/batch
    全部 override 文件，stage 保留。返回删除的文件数。
    """
    if not session_id or not cwd:
        return 0
    stage_path = _stage_file_path(cwd, session_id)
    files = [
        _model_file_path(stage_path),
        _op_file_path(stage_path),
        _pattern_file_path(stage_path),
        _fallback_file_path(stage_path),
        _complexity_file_path(stage_path),
        _batch_file_path(stage_path),
    ]
    removed = 0
    for f in files:
        try:
            f.unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    return removed


# ────────────────────────────────────────────────────────────────────
# 手动指令前缀（设计文档第 12 章）—— 2026-06-14 补齐 ~careful / ~quick / ~batch / ~reset
# ────────────────────────────────────────────────────────────────────

# ~careful / ~quick — 复杂度调档
COMPLEXITY_SHIFT_RE = re.compile(
    r"(?:^|\s)~(careful|quick)\b",
    re.IGNORECASE,
)

# ~batch <template> — 载入预定义任务模式
BATCH_RE = re.compile(
    r"(?:^|\s)~batch\s+(feature|bugfix|refactor|test|research|migration|architecture|docs|audit)\b",
    re.IGNORECASE,
)

# ~reset — 全量清除 override
RESET_RE = re.compile(r"(?:^|\s)~reset\b", re.IGNORECASE)


if __name__ == "__main__":
    main()
