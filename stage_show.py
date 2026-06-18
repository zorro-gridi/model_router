#!/usr/bin/env python3
"""
stage_show.py — Stop Hook（PostToolBatch 也可用）
=================================================
每次 Claude 完成一轮回复后，在终端打印当前阶段和路由目标，
让用户始终知道"现在用的是哪个模型"。

阶段文件位置：
  分 session 阶段文件存于 <project_root>/.claude/stage_<session_id>，
  与 model_router_state_<session_id>.json 同目录。
  active_session 指针存于 ~/.claude/hooks/model_router/，内容为
  阶段文件的完整绝对路径。

显示内容（按优先级）：
  🎯 <model>          用户显式 ~model 覆盖（最高路由优先级）
  <emoji> <label> → <model>  当前阶段 + 主模型
  📐 模式: <pattern> (conf=0.x) [shadow]  Shadow Mode 任务模式标注

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

# Make `from hooks.compact.utils import ...` work regardless of CWD
# (Claude Code runs hooks as standalone scripts)
sys.path.insert(0, os.path.expanduser('~/.claude'))

# ── 分 session 阶段管理 ──
# 存放位置：<project_root>/.claude/stage_<session_id>
# active_session 指针：~/.claude/hooks/model_router/active_session → 完整路径
HOME_CLAUDE         = Path.home() / ".claude"
# 测试隔离：允许通过 MODEL_ROUTER_HOOK_DIR 环境变量覆盖 HOOK_DIR 路径，
# 使单测可在 tempdir 中独立维护 active_session，避免与并发 Claude Code 进程
# 共享 ~/.claude/hooks/model_router/active_session 时产生 TOCTOU 竞态。
_HOOK_DIR_OVERRIDE = os.environ.get("MODEL_ROUTER_HOOK_DIR")
HOOK_DIR            = Path(_HOOK_DIR_OVERRIDE) if _HOOK_DIR_OVERRIDE else (HOME_CLAUDE / "hooks" / "model_router")
ACTIVE_SESSION_FILE = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE   = HOOK_DIR / "current_stage"

# 直接执行时把本目录加进 sys.path，确保 stage_config / model_alias 可导入
sys.path.insert(0, str(HOOK_DIR))

# 从统一配置文件导入（hooks/model_router/stage_config.py）
from stage_config import STAGE_DISPLAY, PATTERN_INFO, PATTERN_CONFIG  # noqa: E402
from stage_config import COMPLEXITY_LEVELS  # 用于显示复杂度档位
from stage_config import MODEL_TO_PROVIDER, KNOWN_PROVIDER_NAMES  # provider fallback 显示
from model_alias import resolve_model  # 用于显示模型简称

# V1.3 §5.1 Task Pattern 中文 label 映射（与 llm_classifier.py 系统 prompt 对齐）
# 优先于 PATTERN_CONFIG 的 legacy label。stage_config 可能尚未提供
# get_pattern_label_v13（向后兼容旧版本），本地兜底实现一份。
try:
    from stage_config import get_pattern_label_v13  # noqa: E402
except ImportError:
    _PATTERN_LABEL_V13_FALLBACK: dict[str, str] = {
        "explore":      "探索与调研",
        "architecture": "架构设计",
        "feature":      "新功能需求",
        "audit":        "审计系统功能",
        "implement":    "功能实现",
        "debug":        "调试异常",
        "refactor":     "模块重构",
        "test":         "测试相关",
        "research":     "调查研究",
        "migration":    "模块迁移",
        "docs":         "文档处理",
        "ops":          "运维、脚本、配置类任务",
    }

    def get_pattern_label_v13(pattern: str) -> str:  # noqa: F811
        """V1.3 风格中文 label 兜底实现（stage_config 缺失时使用）。"""
        if not pattern:
            return ""
        if pattern in _PATTERN_LABEL_V13_FALLBACK:
            return _PATTERN_LABEL_V13_FALLBACK[pattern]
        cfg = PATTERN_CONFIG.get(pattern, {})
        return cfg.get("label", pattern) or pattern


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
         model_router_state_<sid>.json under .claude/ — its parent IS the project root.
      2. Walk up looking for .claude/ (skip global ~/.claude unless started there).
      3. Walk up looking for .git/ as fallback.
      4. Fall back to ~/.claude.
    """
    p = start

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


def _model_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 model_<sid> 路径（同目录、仅前缀替换）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "model_", 1))


def read_stage(event: dict | None = None) -> str:
    """
    读取当前阶段，优先级：
      1. stdin 中的 session_id+cwd → <project_root>/.claude/stage_<session_id>
      2. active_session 指针 → 读取其存储的完整路径文件
      3. default（无全局后备，每个 session 独立维护 stage_<sid>）
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

    return "default"


def read_model_override(event: dict | None = None) -> str | None:
    """
    读取当前 model 覆盖，路径解析复用 stage_show 的路径逻辑。
    返回 None 表示"无 model 覆盖"。
    """
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            content = _read_stage_file(_model_file_path(stage_path))
            if content:
                return content

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_model_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass

    return None


def _fallback_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 fallback_<sid> 路径（同目录、仅前缀替换）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "fallback_", 1))


def read_fallback(event: dict | None = None) -> str | None:
    """
    读取当前 session 的 sticky fallback provider 名。

    向后兼容（2026-06-16）：旧格式 model 名自动映射为 provider 名。
    返回 None 表示"无 sticky fallback"。
    """
    def _resolve(raw: str) -> str | None:
        if raw in KNOWN_PROVIDER_NAMES:
            return raw
        prov = MODEL_TO_PROVIDER.get(raw)
        if prov:
            return prov
        return None

    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            content = _read_stage_file(_fallback_file_path(stage_path))
            if content:
                return _resolve(content)

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_fallback_file_path(Path(active_path)))
            if content:
                return _resolve(content)
    except FileNotFoundError:
        pass

    return None


def _pattern_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 pattern_<sid> 路径（Shadow Mode 标注文件）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "pattern_", 1))


def _complexity_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 complexity_<sid> 路径（复杂度评估 JSON）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "complexity_", 1))


def _task_field_file_path(stage_file: Path) -> Path:
    """V1.4：从 stage_<sid> 派生 task_field_<sid> 路径（业务领域分类）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "task_field_", 1))


def read_pattern(event: dict | None = None) -> dict | None:
    """
    读取当前 session 的 task pattern 标注（Shadow Mode 专用）。
    返回 dict：{"prediction": str, "confidence": float, "ts": str} 或 None。
    """
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            pattern_file = _pattern_file_path(stage_path)
            try:
                content = pattern_file.read_text().strip()
                if content:
                    return json.loads(content)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            pattern_file = _pattern_file_path(Path(active_path))
            content = pattern_file.read_text().strip()
            if content:
                return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return None


def read_complexity(event: dict | None = None) -> dict | None:
    """
    读取当前 session 的 complexity 评估（§6.4）。
    返回 dict：{"score": int, "label": str, "confidence": float, "source": str, "ts": str} 或 None。
    """
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            complexity_file = _complexity_file_path(stage_path)
            try:
                content = complexity_file.read_text().strip()
                if content:
                    return json.loads(content)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            complexity_file = _complexity_file_path(Path(active_path))
            content = complexity_file.read_text().strip()
            if content:
                return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return None


def read_task_field(event: dict | None = None) -> dict | None:
    """
    读取当前 session 的 task_field 业务领域分类（V1.4）。
    返回 dict：{"prediction": str, "confidence": float, "ts": str} 或 None。
    """
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            task_field_file = _task_field_file_path(stage_path)
            try:
                content = task_field_file.read_text().strip()
                if content:
                    return json.loads(content)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            task_field_file = _task_field_file_path(Path(active_path))
            content = task_field_file.read_text().strip()
            if content:
                return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return None


def main():
    event = None
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass  # stdin 可能为空或非 JSON（兼容老版本）

    # ── Data gathering (same as before) ──────────────────────────────
    model_override = read_model_override(event)
    stage = read_stage(event)
    emoji, label, model = STAGE_DISPLAY.get(stage, STAGE_DISPLAY["default"])

    pattern_data = read_pattern(event)
    complexity_data = read_complexity(event)
    task_field_data = read_task_field(event)  # V1.4 业务领域

    # ── ANSI palette ────────────────────────────────────────────────
    RST  = '\033[0m'
    BLD  = '\033[1m'
    DIM  = '\033[2m'
    GRY  = '\033[90m'
    YLW  = '\033[33m'
    GRN  = '\033[32m'
    RED  = '\033[31m'
    CYN  = '\033[36m'
    BLU  = '\033[34m'
    WHT  = '\033[97m'
    MGN  = '\033[35m'

    def _stage_color(s: str) -> str:
        return {
            'brainstorm': CYN, 'decide': MGN, 'design': BLU, 'plan': WHT,
            'implement': GRN, 'audit': RED, 'test': YLW, 'explore': BLU,
            'default': GRY,
        }.get(s, WHT)

    # ── Build content lines ─────────────────────────────────────────
    lines: list[str] = []

    # Provider sticky fallback (2026-06-16)
    fallback_prov = read_fallback(event)
    if fallback_prov:
        lines.append(f"  {RED}{BLD}⚠️  fallback: {fallback_prov}{RST}")

    # Model override (highest routing priority)
    if model_override:
        lines.append(f"  {YLW}{BLD}🎯 {model_override}{RST}")

    # Stage
    s_color = _stage_color(stage)
    lines.append(f"  {s_color}{emoji} {label} → {model}{RST}")

    # Task Pattern (Shadow Mode)
    # V1.3 §5.1 Task Pattern 12 种优先：pattern 命中 V1.3 模式时显示 V1.3 中文 label
    # （如 "test" → "测试相关"、"bugfix" V1 旧名时回退 PATTERN_CONFIG legacy label）。
    # 自定义 pattern key 不在配置中时仍可见 key 名。
    if pattern_data and pattern_data.get("prediction"):
        p_pred = pattern_data["prediction"]
        p_conf = pattern_data.get("confidence", 0.0)
        # V1.3 优先 → PATTERN_CONFIG 兜底 → key 原文
        p_label = get_pattern_label_v13(p_pred)
        # label 和 key 一致时不再重复显示 (e.g. "test" -> "test")，
        # 否则显示 "label (key=xxx)" 帮助区分。
        if p_label == p_pred:
            p_display = p_pred
        else:
            p_display = f"{p_label} (key={p_pred})"
        lines.append(
            f"  {DIM}{GRY}📐 模式: {p_display}  conf={p_conf:.2f}  [shadow]{RST}"
        )

    # Stage Complexity
    if complexity_data and complexity_data.get("label"):
        c_label = complexity_data["label"]
        c_score = complexity_data.get("score", 0)
        c_emoji = {"simple": "🟢", "medium": "🟡", "complex": "🔴"}.get(
            c_label, "⚪"
        )
        c_color = {"simple": GRN, "medium": YLW, "complex": RED}.get(
            c_label, GRY
        )
        lines.append(
            f"  {c_emoji} {c_color}复杂度: {c_label}{RST}"
            f"{GRY}  score={c_score}{RST}"
        )

    # Task Field (V1.4) — 业务领域分类，仅展示不参与路由
    if task_field_data and task_field_data.get("prediction"):
        tf_pred = task_field_data["prediction"]
        tf_conf = task_field_data.get("confidence", 0.0)
        tf_label = {
            "frontend": "前端", "backend": "后端",
            "ops": "运维", "product": "产品", "unknown": "未知",
        }.get(tf_pred, tf_pred)
        tf_color = {
            "frontend": BLU, "backend": GRN,
            "ops": RED, "product": MGN,
        }.get(tf_pred, GRY)
        lines.append(
            f"  {DIM}{tf_color}🏷️ 领域: {tf_label}  conf={tf_conf:.2f}{RST}"
        )

    # ── Box drawing ─────────────────────────────────────────────────
    import re as _re
    import unicodedata as _ucd
    _ansi_re = _re.compile(r'\x1b\[[0-9;]*m')

    def _vlen(s: str) -> int:
        """Visible terminal-cell count (strip ANSI escapes, wide chars = 2)."""
        clean = _ansi_re.sub('', s)
        w = 0
        for ch in clean:
            w += 2 if _ucd.east_asian_width(ch) in ('W', 'F') else 1
        return w

    # Compute box width from longest visible line (+ padding)
    max_v = max((_vlen(ln) for ln in lines), default=40)
    BOX_W = max(max_v + 4, 44)  # minimum 44 cols

    def _pad(ln: str) -> str:
        """Right-pad a line to BOX_W visible characters."""
        need = BOX_W - _vlen(ln)
        return ln + (' ' * max(need, 0))

    # Print box to stderr (each line starts with \r for clean overwrite)
    title = f"📡 Stage Router"
    top = f"\r{GRY}╭── {WHT}{BLD}{title}{RST} {GRY}{'─' * max(BOX_W - _vlen(title) - 5, 0)}╮{RST}"
    bot = f"\r{GRY}╰{'─' * BOX_W}╯{RST}"

    print(top, file=sys.stderr)
    for ln in lines:
        print(f"\r{GRY}│{RST}{_pad(ln)}{GRY}│{RST}", file=sys.stderr)
    print(bot, file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
