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
  与 model_router_state_<session_id>.json 路径规则一致。
  active_session 指针文件存放在 ~/.claude/hooks/model_router/，存储的是
  阶段文件的完整绝对路径，供 proxy.py（无 stdin 上下文）直接读取。

Model-override（用户显式指定模型，最高优先级，2026-06-13 引入）：
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

# Make `from hooks.compact.utils import ...` work regardless of CWD
# (Claude Code runs hooks as standalone scripts)
sys.path.insert(0, os.path.expanduser('~/.claude'))

# 将同目录加入 sys.path，确保 `from stage_config import ...` 与
# `from model_alias import ...` 在 Hook 直接执行时也能 import 到。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_alias import (  # noqa: E402
    detect_model_override,   # 用户模型覆盖（最高路由优先级，2-tuple 便捷封装）
    detect_provider_override,  # provider 级 fallback：~provider reset
    parse_model_override,    # 4-tuple 全量版（含 persist 标志，2026-06-18 行为变更用）
)
# 2026-06-16：~model reset / ~provider reset 现在对所有 session 生效，
# 导入全局清除函数。延迟到函数体调用以避免 proxy 模块顶层副作用
# （load_plugin_env 等）影响 hook 启动。
_clear_fallback_all = None  # type: ignore[var-annotated]

# §17 架构简化（2026-06-17）：COMPLEXITY_THRESHOLDS 不再导入，complexity 由 LLM 分类。
try:
    from stage_config import (  # noqa: E402  配置单源化派生
        MODEL_TO_PROVIDER,            # provider 级 fallback：model→provider 映射
        DEFAULT_FALLBACK_PROVIDER,  # provider 级 fallback：provider→备选 provider
        KNOWN_PROVIDER_NAMES,       # provider 级 fallback：已知 provider 名集合
        PROVIDER_COMPLEXITY_MODELS, # §16 complexity→model 映射（complexity 覆盖 stage）
    )
except Exception:
    MODEL_TO_PROVIDER = {}
    DEFAULT_FALLBACK_PROVIDER = {}
    KNOWN_PROVIDER_NAMES = frozenset()
    PROVIDER_COMPLEXITY_MODELS: dict = {}  # §16 complexity→model 映射（import 失败兜底）

# ── 分 session 阶段管理 ──
# 每个 session 独立管理阶段，避免多会话互相覆盖。
# 命名规则：stage_<session_id>（参照 hooks/session 的 model_router_state_<session_id> 模式）
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


# §17 架构简化（2026-06-17）：STAGE_KEYWORDS / PATTERN_KEYWORDS 不再导入。
# detect_stage() 仅处理显式 ~stage 指令，LLM classifier 是唯一分类源。
#
# 显式命令前缀（优先级最高）
EXPLICIT_PREFIX_RE = re.compile(
    r"(?:^|\s)~stage\s+(brainstorm|decide|design|plan|implement|audit|default|explore|test)\b",
    re.IGNORECASE,
)


def detect_stage(prompt: str) -> str | None:
    """
    检测显式 ~stage 指令。

    返回检测到的阶段名，或 None（表示不更改当前阶段）。
    仅处理显式用户命令，不再使用关键词匹配（§17 架构简化 2026-06-17）：
    stage 的主力分类来源已迁移到 LLM classifier。
    """
    m = EXPLICIT_PREFIX_RE.search(prompt.strip())
    if m:
        return m.group(1).lower()
    return None




# ────────────────────────────────────────────────────────────────────
# Task Pattern（设计文档第 6.2 / 8 章）—— 2026-06-14 引入，Shadow Mode
#
# 与 stage/op 完全并列的**第三维度**信息，专门描述"任务是什么类型"。
# 阶段 A（当前）：仅记录 pattern + confidence 到 pattern_<sid> 文件 + log，
#                 **不影响路由决策**。积累标注数据后，进入阶段 B 再启用
#                 Adaptive Routing。
#
# §17 架构简化（2026-06-17）：关键词匹配已移除，pattern 仅来源于
# LLM classifier 或用户显式 ~pattern 指令。不再保留关键词 fallback。
# ────────────────────────────────────────────────────────────────────

# 显式命令前缀（最高优先级）
PATTERN_PREFIX_RE = re.compile(
    r"(?:^|\s)~pattern\s+(feature|bugfix|refactor|test|research|migration|architecture|docs|audit)\b",
    re.IGNORECASE,
)


# ── 项目根目录查找（参照 compact/utils.py 的 _find_project_root）──

def _find_project_root(start: Path, session_id: str | None = None) -> Path:
    """Walk up from ``start`` looking for a project boundary marker.

    Anchor strategy (the location of the first per-session file IS the lock):
      1. If session_id is known, walk up looking for an existing
         ``stage_<session_id>`` or ``model_router_state_<session_id>.json`` under
         ``.claude/``.  The directory containing it is the locked project root.
      2. Walk up looking for a ``.claude/`` config directory (skipping the
         global ``~/.claude`` unless we started inside it).
      3. Walk up looking for a ``.git/`` toplevel as fallback.
      4. Fall back to ``start/.claude/`` (CWD-local) — only use HOME_CLAUDE
         when the user is working inside the Claude config repo itself.

    Walks at most 20 levels.
    """
    p = start

    # 1. If session_id is available, walk up looking for an existing per-session
    #    file.  Its parent directory IS the project-root anchor.
    if session_id:
        # Priority 0: session anchor file (survives cwd drift)
        from hooks.compact.utils import read_session_anchor
        anchored = read_session_anchor(session_id)
        if anchored is not None:
            return anchored

        anchor_p = start
        for _ in range(20):
            claude_dir = anchor_p / ".claude"
            if (claude_dir / f"stage_{session_id}").exists() or \
               (claude_dir / f"model_router_state_{session_id}.json").exists():
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
    # Step 4: 无 .claude/ 也无 .git/ → 写 CWD 本地目录，不污染 HOME_CLAUDE。
    # 旧实现在此无条件返回 HOME_CLAUDE，导致所有非项目目录 session 的状态文件
    # 全落盘到 ~/.claude/.claude/，积攒了 500+ 孤立文件。
    # 现在：仅当 cwd 自身在 ~/.claude 内部（用户在编辑 Claude Code 配置仓库自身）
    # 时才退到 HOME_CLAUDE，否则直接以 start 为项目根。
    if str(start).startswith(str(HOME_CLAUDE) + os.sep) or start == HOME_CLAUDE:
        return HOME_CLAUDE
    return start


def _write_skip_signal(sid: str, project_root: str) -> None:
    """当 is_valid_prompt=False 时，写 skip_post_tool_analysis 标记到 state 文件。

    PostToolUse hook 的 dispatch() 在入口检查此标记，若为 true 则跳过
    所有运行时分析（RuntimeTracker / TodoWriteAnalyzer / maybe_redecide）。
    下一个有效 prompt 的 RuntimeTracker.init_prompt() 会清除此标记。
    """
    try:
        from state_persistence import SessionStateStore
        SessionStateStore().update_fields(
            sid,
            project_root,
            {"skip_post_tool_analysis": True},
        )
    except Exception:
        pass  # 静默吞错，不阻塞 UserPromptSubmit hook


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
        # 锚点：首次 stage 落盘时同步写入，后续 prompt 直接命中 Priority 0
        try:
            from hooks.compact.utils import write_session_anchor
            _pr = _find_project_root(Path(cwd) if isinstance(cwd, str) else cwd, session_id)
            write_session_anchor(session_id, _pr, Path(cwd) if isinstance(cwd, str) else cwd)
        except Exception:
            pass  # 静默吞错 — 锚点写入不能阻塞 stage 落盘
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

    V1.5 by_session 索引：
      state_index.json 新增顶层键 _by_session: {session_id: {project_root, stage, last_active}}
      旧顶层 {project_root: {...}} 保留为反向索引（backward-compat for proxy.py 旧读端）。
      跨项目时直接 by_session[sid] 查 O(1) 拿到 project_root，不再依赖全局指针。
    """
    idx = _load_state_index()
    try:
        stage_name = stage_path.read_text().strip() or "default"
    except FileNotFoundError:
        stage_name = "default"
    now_ts = int(datetime.now(timezone.utc).timestamp())
    entry = {
        "session_id":  session_id,
        "stage":       stage_name,
        "last_active": now_ts,
    }
    # 反向索引（按 project_root）— 兼容旧读端
    idx[project_root] = dict(entry)
    # 正向索引（按 session_id）— 跨项目 / statusline O(1) 查
    by_session = idx.get("_by_session")
    if not isinstance(by_session, dict):
        by_session = {}
    by_session[session_id] = {
        "project_root": project_root,
        "stage":        stage_name,
        "last_active":  now_ts,
    }
    idx["_by_session"] = by_session
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
    读取当前 session 的 sticky fallback provider 名。

    优先级：
      1. 传入的 session_id+cwd → 派生 fallback_<sid>
      2. active_session 指针 → 读取其指向的 stage_<sid>，再派生 fallback_<sid>

    向后兼容（2026-06-16）：
      - 新格式：fallback_<sid> 存 provider 名（"minimax"/"deepseek"）→ 直接返回
      - 旧格式：fallback_<sid> 存 model 名（"deepseek-v4-flash"）→ 通过
        MODEL_TO_PROVIDER 自动转换为 provider 名

    返回 None 表示"无 sticky fallback"。
    """

    def _resolve_provider(raw: str) -> str | None:
        """将 fallback 文件内容解析为 provider 名（兼容新旧格式）。"""
        if raw in KNOWN_PROVIDER_NAMES:
            return raw  # 新格式：已经是 provider 名
        prov = MODEL_TO_PROVIDER.get(raw)
        if prov:
            log("INFO", f"fallback_<sid> 旧格式（model={raw}）→ 自动映射到 provider={prov}")
            return prov
        log("WARN", f"fallback_<sid> 内容无法识别: {raw!r}")
        return None

    # 1. hook 场景：有 session_id+cwd
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        content = _read_stage_file(_fallback_file_path(stage_path))
        if content:
            return _resolve_provider(content)

    # 2. proxy / CLI 场景：从 active_session 指针拿到 stage_<sid> 路径再派生
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_fallback_file_path(Path(active_path)))
            if content:
                return _resolve_provider(content)
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
        # 2026-06-18 行为变更（撤销 2026-06-16 的一次性逻辑）：
        #   - 默认 `~model <alias>`         → persist=True，写回 model_<sid> 让整个
        #                                       session 持续生效（直到 ~model reset）
        #   - `~model <alias> 0`             → persist=False，仅本回合一次性覆盖
        #   - `~model <alias> 1`             → persist=True（显式声明，与默认等价）
        # 这里主要承担「写盘」职责；proxy 端 do_POST 在请求到达时会再扫一次 body
        # 里的 ~model 并立刻应用到当前请求（消除一回合延迟），并按相同规则写盘。
        # 用 parse_model_override 拿 4-tuple（含 persist），detect_model_override
        # 是 2-tuple 便捷封装，不能区分「一次性 vs 持久化」。
        new_model, is_reset, _unknown_alias, persist = parse_model_override(prompt)

        model_msg: str | None = None
        if is_reset:
            # 2026-06-16 行为变更：~model reset 现在对**所有 session** 生效。
            # 原实现只清当前 session 的 fallback_<sid>，但 sticky 触发条件
            # 是「主模型 API 失败」——进程/网络级问题，不分 session。
            # 多 session 并发时所有 session 都会被同一波失败触发 sticky，
            # reset 必须全局清才能让用户「主模型恢复后回到正常路由」的语义对齐。
            # 2026-06-18 补充：reset 同时清当前 session 的 model_<sid>（持久化覆盖），
            # 让整个 model 覆盖链路完整重置。
            global _clear_fallback_all
            if _clear_fallback_all is None:
                # 延迟 import：避免 proxy 顶层副作用（load_plugin_env 等）拖慢 hook
                from proxy import clear_fallback_all as _cfa
                _clear_fallback_all = _cfa
            try:
                # 1) 清当前 session 的 model_<sid>（如果有）
                clear_model_override(session_id=session_id, cwd=cwd)
                # 2) 全局清 sticky fallback
                n = _clear_fallback_all()
                log("INFO", f"prompt ~model reset: 清 model_<sid> + 全局清除 sticky fallback ({n} 个文件)")
                model_msg = f"~model reset：已清 model_<sid> + 清除 {n} 个 session 的 sticky fallback"
            except Exception as _e:
                log("WARN", f"~model reset 失败: {_e!r}")
                model_msg = "~model reset 失败（fallback 清除异常）"
            # 3) 清 state JSON 的 model_override 字段（与 1/2 对称）：
            #    此前 SET 步骤会把 override 写进 model_router_state_<sid>.json
            #    （store.write(model_override=new_model)），如果只删 model_<sid>
            #    文件，state JSON 里的 model_override 仍是 stale 值，statusline
            #    与决策引擎（decision.decision_source='explicit'）继续误指代。
            #    store.write(model_override=None) 显式清零，按 store.write 语义
            #    （kwargs[key] is None → 直接写）会真正写入 None 而非继承。
            try:
                from state_persistence import SessionStateStore
                from stage_config import STAGE_CONFIG  # noqa: F401  与正常路径一致
                _root_reset = str(_find_project_root(
                    Path(cwd) if not isinstance(cwd, Path) else cwd, session_id))
                SessionStateStore().write(
                    sid=session_id,
                    project_root=_root_reset,
                    model_override=None,
                )
                log("INFO", "~model reset: 清 state JSON 的 model_override 字段")
            except Exception as _e:
                log("WARN", f"~model reset 清 state JSON model_override 失败（已忽略）: {_e!r}")
            # 4) 清 in-process 状态（防御性双写：与 proxy 端 do_POST 的 ~model reset
            #    对称执行，即便 hook 阶段先于 proxy 命中也能立刻让 provider 回到可用）。
            #    复用 proxy._clear_provider_state_on_reset，里面已用 try/except
            #    隔离单点失败，不影响 reset 主流程。
            try:
                from proxy import _clear_provider_state_on_reset as _reset_clear
                _reset_clear(reset_kind="model")
            except Exception as _e:
                log("WARN", f"~model reset 清 in-process 状态失败（已忽略）: {_e!r}")
        elif new_model:
            # 2026-06-18 行为变更：默认写回 model_<sid>（与 2026-06-16 的"一次性"行为相反）。
            # proxy 端 do_POST 会按相同规则写盘并应用到当前请求；
            # stage_detector 这里也同步写盘，双向冗余防御：
            #   - 即便 proxy 端写盘失败（state_index 异常），stage_detector 也会兜底写
            #   - 即便用户没真的发起请求（只发 hook），下一次请求也能读到这个覆盖
            if persist:
                write_model_override(new_model, session_id=session_id, cwd=cwd)
                log("INFO", f"prompt ~model 持久化覆盖: {new_model}（写 model_<sid>，整个 session 生效）")
                model_msg = f"model 覆盖: {new_model}（整个 session 持续生效；~model reset 解除）"
            else:
                log("INFO", f"prompt ~model 一次性覆盖: {new_model}（不写 model_<sid>）")
                model_msg = f"model 覆盖: {new_model}（一次性，本回合生效）"

        # ── ~provider reset（provider 级 fallback，2026-06-16）─────────
        prov_override, prov_is_reset = detect_provider_override(prompt)
        if prov_is_reset:
            if _clear_fallback_all is None:
                from proxy import clear_fallback_all as _cfa
                _clear_fallback_all = _cfa
            try:
                n = _clear_fallback_all()
                log("INFO", f"prompt ~provider reset: 全局清除 sticky fallback ({n} 个文件)")
                prov_msg = f"~provider reset：已清除 {n} 个 session 的 sticky fallback"
            except Exception as _e:
                log("WARN", f"~provider reset clear_fallback_all 失败: {_e!r}")
                prov_msg = "~provider reset 失败（fallback 清除异常）"
            # 清 in-process 状态：与 proxy 端对称执行，让 provider 真正回到「可用」。
            try:
                from proxy import _clear_provider_state_on_reset as _reset_clear
                _reset_clear(reset_kind="provider")
            except Exception as _e:
                log("WARN", f"~provider reset 清 in-process 状态失败（已忽略）: {_e!r}")
        else:
            prov_msg = None

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

            # ── is_valid_prompt 守卫 ──
            # LLM 判定 prompt 为任务续接指令（如 "go ahead" / "continue" / "stop"），
            # 不应触发路由状态变更。跳过后续所有 stage/pattern/complexity/decision 覆写，
            # 保持 session 上一次的路由状态不变。
            if llm_result and llm_result.get("is_valid_prompt") is False:
                log("INFO",
                    f"LLM classifier: is_valid_prompt=False, "
                    f"跳过路由状态更新，保持 session 现有路由不变 "
                    f"(reason={llm_result.get('reasoning', '')!r})"
                )
                # ── V1.4 is_valid_prompt 穿透 ──
                # 1. 写 skip_post_tool_analysis 标记到 state 文件，
                #    通知 PostToolUse 链路本 prompt 不参与运行时分析。
                # 2. 归档 RuntimeTracker 当前窗口，防止后续工具调用
                #    分数污染上一个 prompt 的 runtime_score。
                _root = str(_find_project_root(
                    Path(cwd) if not isinstance(cwd, Path) else cwd, session_id))
                _write_skip_signal(session_id, _root)
                try:
                    from runtime_tracker import RuntimeTracker
                    RuntimeTracker().init_prompt(
                        session_id, _root,
                        f"{session_id[-8:]}-continuation")
                except Exception:
                    pass
                # 提前返回，不写任何 state 文件
                return None

        # ── Stage 检测 ──
        # 优先级：显式 ~stage > LLM 分类器
        # §17 架构简化（2026-06-17）：LLM 是唯一分类源，不再保留关键词回退
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

        # ── Sticky Fallback 通知（用户未显式覆盖模型时提示）──
        fb_msg: str | None = None
        if not new_model and not is_reset:
            fb_provider = read_fallback(session_id, cwd)
            if fb_provider:
                # fb_provider 是 FAILED 的 provider（如 "minimax"），
                # 实际备用 provider 是 DEFAULT_FALLBACK_PROVIDER 的映射值
                actual_fb = DEFAULT_FALLBACK_PROVIDER.get(fb_provider, "deepseek")
                log("INFO",
                    f"sticky fallback active: {fb_provider}→{actual_fb}")
                fb_msg = (
                    f"⚠️ {fb_provider} 不可用，已自动切换至 {actual_fb}"
                )

        # ── Task Pattern 检测（Shadow Mode，2026-06-14 引入）──
        #   - 仅记录到 pattern_<sid> + 日志，**不影响路由**
        #   - 阶段 B 启用 Adaptive Routing 后才进入 proxy 决策
        # 优先级：显式 ~pattern > LLM 分类器
        # §17 架构简化（2026-06-17）：移除关键词回退，LLM 是唯一分类源
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
                new_pattern, new_conf = None, None
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

            # ── Task Field 落盘（V1.4：业务领域，statusline 展示用，不参与路由）──
            if llm_result and llm_result.get("task_field"):
                write_task_field(
                    llm_result["task_field"],
                    llm_result.get("task_field_confidence", 0.5),
                    session_id, cwd,
                )
                log("INFO",
                    f"task_field (shadow): {llm_result['task_field']} "
                    f"(conf={llm_result.get('task_field_confidence', 0.5)})")

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
                    # 没有现成评估 → 用 LLM 结果（§17 架构简化 2026-06-17）
                    if llm_result and llm_result.get("complexity_label"):
                        cur_label = llm_result["complexity_label"]
                    else:
                        cur_label = "medium"
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
                # ~batch <template> — 载入预定义任务模式（D16-D-1 修复 2026-06-14）
                # 不光写 batch 文件，**还要把 stage 持久化到 flow[0]**，
                # 否则下次路由 read_stage() 拿到的还是旧 stage，
                # flow 强制只对当次请求生效，流程"形同虚设"。
                from stage_config import PATTERN_CONFIG
                template = batch_m.group(1).lower()
                flow = PATTERN_CONFIG.get(template, {}).get("default_flow", [])
                if flow:
                    write_batch(template, flow, session_id, cwd)
                    log("INFO", f"~batch loaded: {template} → {flow}")
                    # ── 同步把 stage 推到 flow 起点（持久化）──
                    flow_start = flow[0]
                    if flow_start != old_stage:
                        write_stage(flow_start, session_id, cwd)
                        log("INFO",
                            f"~batch: stage forced to flow[0] "
                            f"({old_stage} → {flow_start})")
                        stage_msg = (
                            f"~batch: 已载入 {template} 流程 "
                            f"{'→'.join(flow)}，阶段已切到起点 {flow_start}"
                        )
                    else:
                        stage_msg = (
                            f"~batch: 已载入 {template} 流程 "
                            f"{'→'.join(flow)}（当前阶段 {flow_start} 即为起点）"
                        )
                    complexity_msg = (
                        f"~batch: 已载入 {template} 流程 {'→'.join(flow)}"
                    )
                else:
                    log("WARN", f"~batch: unknown template {template}")

            else:
                # auto 检测：LLM 分类器是唯一来源（§17 架构简化 2026-06-17）
                if llm_result:
                    auto_score = llm_result["complexity_score"]
                    auto_label = llm_result["complexity_label"]
                    auto_conf = llm_result["complexity_confidence"]
                    log("INFO", "complexity from LLM")
                else:
                    # LLM 不可用时，维持当前 complexity 不变
                    cur = read_complexity(session_id, cwd)
                    if cur:
                        auto_score = cur.get("score", 50)
                        auto_label = cur.get("label", "medium")
                        auto_conf = cur.get("confidence", 0.3)
                    else:
                        auto_score = 50
                        auto_label = "medium"
                        auto_conf = 0.3
                    log("INFO", "complexity: LLM unavailable, keep existing")
                write_complexity(
                    auto_score, auto_label,
                    confidence=auto_conf, source="auto",
                    session_id=session_id, cwd=cwd,
                )
                log("INFO",
                    f"complexity (auto): score={auto_score} "
                    f"label={auto_label} conf={auto_conf}")

        # ── V1.3 决策核心（V1.3 §6.4）：调 decision_engine.decide() 拿权威 DecisionRecord ──
        # Stage 5/7 production bug 修复：之前 stage_detector 直接写 state 但
        # decision 字段为 {}。E2E Scenario 1 (RED) 暴露后，这里调 decide()
        # 拿到 locked=True DecisionRecord，写到 model_router_state_<sid>.json。
        #
        # Stage 8 lock-respect 修复（Scenario 5 RED）：之前每次 UserPromptSubmit
        # 都无条件调 decide() 覆写 locked decision。现在先读已有 state，
        # locked=True 且无 ~model 显式覆盖 → 保留原 decision 不重算。
        decision_dict: dict | None = None
        prompt_id: str | None = None  # V1.3 §4.2 per-prompt ID
        if session_id and cwd and prompt:
            try:
                from decision_engine import decide as _v13_decide
                from state_persistence import SessionStateStore as _V13Store
                import time as _t

                _root = str(_find_project_root(
                    Path(cwd) if not isinstance(cwd, Path) else cwd, session_id))

                # ── 检查已有 locked 决策 ──
                _existing_state = _V13Store().read_new(session_id, _root)
                _existing_decision = _existing_state.get("decision") if _existing_state else None
                _is_locked = bool(_existing_decision and _existing_decision.get("locked"))

                if _is_locked and not new_model:
                    # locked 且无 ~model 显式覆盖 → 保留原决策，不重算
                    decision_dict = dict(_existing_decision)
                    log("INFO",
                        f"v1.3 decide: SKIPPED (locked={_is_locked}, "
                        f"model={decision_dict.get('final_model')})")
                else:
                    # 复用本 hook 内已经拿到的 llm_result（避免二次 LLM 调用）；
                    # §17 架构简化（2026-06-17）：移除关键词回退，LLM 是唯一分类源。
                    # LLM 不可用时返回保守默认值。
                    def _v13_classifier(_p: str) -> dict:
                        if llm_result:
                            return dict(llm_result)
                        return {
                            "stage": new_stage or "",
                            "pattern": new_pattern or "feature",
                            "pattern_confidence": new_conf or 0.5,
                            "complexity_label": "medium",
                            "complexity_score": 50,
                            "complexity_confidence": 0.3,
                            "reasoning": "llm unavailable (no classification)",
                        }

                    prompt_id = f"{session_id[-8:]}-p{int(_t.time())}"

                    # ── V1.3 §4.2 Per-Prompt Runtime 初始化 ──
                    # 归档上一个 prompt 的 runtime_score 到 prompt_history，
                    # 重置计分器为新 prompt 准备。
                    try:
                        from runtime_tracker import RuntimeTracker
                        _rt = RuntimeTracker()
                        _rt.init_prompt(session_id, _root, prompt_id)
                        log("INFO",
                            f"v1.3 runtime init: prompt_id={prompt_id}")
                    except Exception as _rte:
                        log("WARN", f"runtime_tracker.init_prompt failed: {_rte!r}")

                    # ── 从 prompt_history 聚合 session 级历史分数 ──
                    _session_runtime_score = 0
                    try:
                        _state_after_init = _V13Store().read_new(session_id, _root)
                        if _state_after_init:
                            _rs_data = _state_after_init.get("runtime_score", {})
                            if isinstance(_rs_data, dict):
                                _ph = _rs_data.get("prompt_history", {})
                                if isinstance(_ph, dict) and _ph:
                                    _scores = [
                                        v.get("score", 0)
                                        for v in _ph.values()
                                        if isinstance(v, dict)
                                    ]
                                    _session_runtime_score = max(_scores) if _scores else 0
                                    log("INFO",
                                        f"v1.3 session_runtime_score={_session_runtime_score} "
                                        f"(from {len(_ph)} previous prompts)")
                    except Exception as _sre:
                        log("WARN", f"session_runtime_score compute failed: {_sre!r}")

                    rec = _v13_decide(
                        prompt, session_id, prompt_id,
                        classifier=_v13_classifier,
                        session_runtime_score=_session_runtime_score,
                    )
                    decision_dict = dict(rec.to_dict())
                    # ~model 显式覆盖：覆写 final_model（V1.3 §6.4 优先级最高）
                    if new_model:
                        decision_dict["final_model"] = new_model
                        decision_dict["decision_source"] = "explicit"
                    log("INFO",
                        f"v1.3 decide: complexity={decision_dict['task_complexity']} "
                        f"model={decision_dict['final_model']} "
                        f"source={decision_dict['decision_source']} "
                        f"locked={decision_dict['locked']}")
            except Exception as _de:
                log("WARN", f"decision_engine.decide() failed: {_de!r}; "
                            f"decision 字段留空（向后兼容）")
                decision_dict = None

        # ── V1.3 状态快照 ──────────────────────────────────────────────
        # 每次 hook 触发末尾，将当前 session 的完整状态写入
        # model_router_state_<sid>.json。v1.3 单文件为唯一持久化路径。
        if session_id and cwd:
            try:
                from state_persistence import SessionStateStore
                from stage_config import STAGE_CONFIG
                store = SessionStateStore()
                _root = str(_find_project_root(
                    Path(cwd) if not isinstance(cwd, Path) else cwd, session_id))

                # 初始化 route_model（最终实际路由模型），优先级：
                #   1. model_override（显式 ~model 覆盖）
                #   2. decision.final_model（LLM 分类器决策）
                #   3. complexity-aware 模型选择（§16 核心原则）
                #   4. stage 默认主模型（STAGE_CONFIG）
                #   5. 硬编码兜底
                #
                # §16 核心原则：Complexity > Stage（析取关系）
                #   Task Complexity 是模型选择的主导方：
                #     - complexity=simple  → 无论 stage 判为什么，用 provider 低成本模型
                #     - complexity=medium  → 用 provider 的 medium-tier 模型
                #     - complexity=complex → 用 provider 的强模型（pro）
                #   Stage 默认 model 只在 complexity 未知或无法解析时作为兜底。
                #
                # proxy.py 在每次请求后会用 sticky swap / fallback retry 后的
                # 最终模型回填此字段，保持 route_model 始终为最新路由状态。
                resolved_stage = new_stage if new_stage else old_stage
                stage_default_model = STAGE_CONFIG.get(resolved_stage, {}).get("model")

                # §16: complexity 覆盖 stage 默认模型
                _task_complexity = (
                    (decision_dict.get("task_complexity") if decision_dict else None)
                    or (read_complexity(session_id, cwd) or {}).get("label")
                    or "medium"
                )
                _complexity_model = None
                if _task_complexity in ("simple", "medium"):
                    _provider = MODEL_TO_PROVIDER.get(stage_default_model, "")
                    _complexity_model = PROVIDER_COMPLEXITY_MODELS.get(
                        _provider, {}).get(_task_complexity)
                    if _complexity_model == stage_default_model:
                        _complexity_model = None  # 无需覆盖，stage 默认模型已正确

                init_route_model = (
                    new_model
                    or (decision_dict.get("final_model") if decision_dict else None)
                    or _complexity_model          # §16: complexity 优先于 stage
                    or stage_default_model
                    or "MiniMax-M3"
                )

                store.write(
                    sid=session_id,
                    project_root=_root,
                    stage=resolved_stage,
                    model_override=new_model,  # 本回合一次性 override（无持久文件）
                    pattern=read_pattern(session_id, cwd),
                    complexity=read_complexity(session_id, cwd),
                    batch=read_batch(session_id, cwd),
                    fallback=read_fallback(session_id, cwd),
                    route_model=init_route_model,
                    decision=decision_dict,
                    current_prompt_id=prompt_id,
                )
                log("INFO", f"v1.3 dual-write snapshot → {_root}/.claude/model_router_state_{session_id}.json")
            except Exception as _e:
                log("WARN", f"v1.3 dual-write failed (non-blocking): {_e!r}")

        # ── 输出 additionalContext（model/stage/op/fallback/pattern/complexity 各自命中时合并提示）──
        msgs = [m for m in (model_msg, prov_msg, stage_msg, fb_msg, pattern_msg, complexity_msg) if m]
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
# §17 架构简化（2026-06-17）：detect_complexity() / detect_task_pattern() /
# _score_to_label() 关键词评分已全部移除。COMPLEXITY_KEYWORDS /
# PATTERN_BASE_SCORE / STAGE_COMPLEXITY_MULTIPLIER / COMPLEXITY_THRESHOLDS
# 不再从 stage_config 导入。
# Complexity 的唯一来源现在是 LLM classifier，辅以用户 ~careful/~quick 显式调档。
#
# shift_complexity 保留：~careful/~quick 调档逻辑仍需要它。
from stage_config import shift_complexity  # noqa: E402

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


def _task_field_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 task_field_<sid> 路径（同目录、仅前缀替换）。
    V1.4 新增：存储 task_field 业务领域分类标签（statusline 展示用，不参与路由）。
    存储 JSON：{"prediction": str, "confidence": float, "ts": str}。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "task_field_", 1))


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


def read_task_field(session_id: str | None = None,
                    cwd: str | Path | None = None) -> dict | None:
    """读取当前 session 的 task_field 业务领域分类（V1.4）。
    返回 dict：{"prediction": str, "confidence": float, "ts": str} 或 None。
    """
    if session_id and cwd:
        stage_path = _stage_file_path(cwd, session_id)
        task_field_file = _task_field_file_path(stage_path)
    else:
        try:
            active_path = ACTIVE_SESSION_FILE.read_text().strip()
            if not active_path:
                return None
            task_field_file = _task_field_file_path(Path(active_path))
        except FileNotFoundError:
            return None
    try:
        content = task_field_file.read_text().strip()
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


def write_task_field(prediction: str, confidence: float,
                     session_id: str | None = None,
                     cwd: str | Path | None = None) -> None:
    """写入 task_field 业务领域分类到 task_field_<sid>（JSON 格式，V1.4）。

    task_field 仅作为 statusline 展示标签，**不参与 model router proxy 路由决策**。
    5 种枚举：frontend / backend / ops / product / unknown。
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
    _task_field_file_path(stage_path).write_text(
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


def clear_task_field(session_id: str | None = None,
                     cwd: str | Path | None = None) -> None:
    """清除 task_field_<sid> 文件（V1.4）。"""
    if not session_id or not cwd:
        return
    stage_path = _stage_file_path(cwd, session_id)
    try:
        _task_field_file_path(stage_path).unlink(missing_ok=True)
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
    """~reset 全量清除：删除 model/pattern/fallback/complexity/batch
    全部 override 文件，stage 保留。返回删除的文件数。
    """
    if not session_id or not cwd:
        return 0
    stage_path = _stage_file_path(cwd, session_id)
    files = [
        _model_file_path(stage_path),
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
