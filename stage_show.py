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

显示内容（按优先级）：
  🎯 <model>          用户显式 ~model 覆盖（最高路由优先级）
  <emoji> <label> → <model>  当前阶段 + 主模型
  <emoji> <label> → <model> (op 覆盖 stage)  当 op 覆盖时
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

# ── 分 session 阶段管理 ──
# 存放位置：<project_root>/.claude/stage_<session_id>
# active_session 指针：~/.claude/hooks/model_router/active_session → 完整路径
HOME_CLAUDE         = Path.home() / ".claude"
HOOK_DIR            = HOME_CLAUDE / "hooks" / "model_router"
ACTIVE_SESSION_FILE = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE   = HOOK_DIR / "current_stage"

# 直接执行时把本目录加进 sys.path，确保 stage_config / model_alias 可导入
sys.path.insert(0, str(HOOK_DIR))

# 从统一配置文件导入（hooks/model_router/stage_config.py）
from stage_config import STAGE_DISPLAY, OPERATION_DISPLAY, PATTERN_INFO  # noqa: E402
from stage_config import COMPLEXITY_LEVELS  # 用于显示复杂度档位
from model_alias import resolve_model  # 用于显示模型简称


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


def _op_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 op_<sid> 路径（同目录、仅前缀替换）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "op_", 1))


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


def read_operation(event: dict | None = None) -> str | None:
    """
    读取当前 op，路径解析复用 _stage_file_path() 派生 op_<sid>。
    返回 None 表示"无 op 信号"（与"未检测到 op"等价）。
    """
    if event:
        session_id: str | None = (event.get("session_id") or "").strip() or None
        cwd: str | None = event.get("cwd")
        if session_id and cwd:
            stage_path = _stage_file_path(cwd, session_id)
            content = _read_stage_file(_op_file_path(stage_path))
            if content:
                return content

    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_op_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass

    return None


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


def _pattern_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 pattern_<sid> 路径（Shadow Mode 标注文件）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "pattern_", 1))


def _complexity_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 派生 complexity_<sid> 路径（复杂度评估 JSON）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "complexity_", 1))


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


def main():
    event = None
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass  # stdin 可能为空或非 JSON（兼容老版本）

    # 输出到 stderr（终端可见，不影响 CC 的 stdout 解析）
    parts: list[str] = []

    # ── Model override（最高优先级）──
    model_override = read_model_override(event)
    if model_override:
        parts.append(f"🎯 {model_override}")

    stage = read_stage(event)
    emoji, label, model = STAGE_DISPLAY.get(stage, STAGE_DISPLAY["default"])
    parts.append(f"{emoji} {label} → {model}")

    op = read_operation(event)
    if op and op in OPERATION_DISPLAY:
        op_emoji, op_label, op_model = OPERATION_DISPLAY[op]
        parts.append(f"{op_emoji} {op_label} → {op_model} (op 覆盖 stage)")
    # op 为 None 时不显示——保持与升级前相同的输出长度

    # ── Task Pattern（Shadow Mode，2026-06-14 引入）──
    #   只在已有标注时显示，明确标注 [shadow] 后缀让用户知道这是非路由信息。
    pattern_data = read_pattern(event)
    if pattern_data and pattern_data.get("prediction"):
        p_pred = pattern_data["prediction"]
        p_conf = pattern_data.get("confidence", 0.0)
        p_label = PATTERN_INFO.get(p_pred, p_pred)
        parts.append(f"📐 模式: {p_pred} (conf={p_conf}) [shadow]")

    # ── Stage Complexity（设计文档 §6.4，2026-06-14 引入）──
    #   标注当前任务复杂度档位（simple/medium/complex）和分数。
    complexity_data = read_complexity(event)
    if complexity_data and complexity_data.get("label"):
        c_label = complexity_data["label"]
        c_score = complexity_data.get("score", 0)
        c_source = complexity_data.get("source", "auto")
        c_conf = complexity_data.get("confidence", 0.0)
        # 简单档用绿色 emoji，中等黄色，复杂红色
        c_emoji = {"simple": "🟢", "medium": "🟡", "complex": "🔴"}.get(
            c_label, "⚪"
        )
        parts.append(
            f"{c_emoji} 复杂度: {c_label} (score={c_score}, "
            f"conf={c_conf}, src={c_source})"
        )

    print(
        f"\r\033[90m[Stage Router] {' │ '.join(parts)}\033[0m",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
