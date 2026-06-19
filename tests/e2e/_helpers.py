"""
tests/e2e/_helpers.py — Stage 8.1 端到端测试基础设施
====================================================

E2E 测试都用真实 subprocess 调 ``stage_detector.py`` /
``post_tool_handler.py``，通过临时 ``project_root`` 隔离，避免污染宿主 state。

Helpers 职责：
  1. ``setup_temp_project()`` — 创临时目录 + ``.claude/``，返回 path
  2. ``run_stage_detector(prompt, sid, project_root, *, cwd=None)`` —
     subprocess 调 stage_detector.py，喂 stdin JSON，返回 ``(stdout, stderr, returncode)``
  3. ``run_post_tool_handler(raw_event, project_root)`` —
     subprocess 调 post_tool_handler.py，喂 stdin JSON
  4. ``read_state(sid, project_root)`` — 读 model_router_state_<sid>.json
  5. ``resolve_decision(sid, project_root)`` — proxy._v13_resolve_decision() 读出 decision
  6. ``make_user_prompt_event(sid, prompt, cwd)`` — 构造 UserPromptSubmit 事件
  7. ``make_post_tool_event(sid, tool_name, tool_input, cwd)`` — 构造 PostToolUse 事件

环境要求：
  - 跑 E2E 时**必须**禁 LLM（unset MINIMAX_API_KEY / DEEPSEEK_API_KEY）
    → 让 stage_detector 走 V1 关键词启发式路径
  - PYTHONPATH 自动注入到 ``~/.claude/hooks``，让 stage_detector / post_tool_handler
    能在 subprocess 内 import 同目录的辅助模块
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional


THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
HOOKS_DIR = TESTS_DIR.parent.parent   # ~/.claude/hooks
HOOKS_DIR_STR = str(HOOKS_DIR)

STAGE_DETECTOR = HOOKS_DIR / "stage_detector.py"
POST_TOOL_HANDLER = HOOKS_DIR / "post_tool_handler.py"


# ── 临时项目 ────────────────────────────────────────────────────────────────

def setup_temp_project() -> tuple[str, str]:
    """创建隔离临时项目根目录（含 .claude/），返回 ``(project_root, sid)``。"""
    tmp = tempfile.mkdtemp(prefix="mr-e2e-")
    Path(tmp, ".claude").mkdir(parents=True, exist_ok=True)
    sid = f"e2e-{uuid.uuid4().hex[:12]}"
    return tmp, sid


# ── subprocess 启动器 ──────────────────────────────────────────────────────

def _subprocess_env() -> dict[str, str]:
    """构造无 LLM 调用的环境（避免依赖外部 API）。"""
    env = os.environ.copy()
    # 禁 LLM：删除所有 *_API_KEY，让 llm_classifier.classify 抛 RuntimeError，
    # stage_detector 走 V1 关键词回退路径。
    for k in (
        "MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        env.pop(k, None)
    # PYTHONPATH 注入 hooks 目录
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{HOOKS_DIR_STR}:{existing}" if existing else HOOKS_DIR_STR
    )
    return env


def run_stage_detector(
    prompt: str,
    sid: str,
    project_root: str,
    *,
    env_overrides: Optional[dict[str, str]] = None,
    timeout: int = 15,
) -> tuple[str, str, int]:
    """subprocess 调 stage_detector.py，喂 UserPromptSubmit 事件。

    Args:
        prompt: 用户 prompt 文本。
        sid: session id。
        project_root: 临时项目根目录。
        env_overrides: 额外环境变量覆盖。

    Returns:
        ``(stdout, stderr, returncode)``。
    """
    event = {
        "session_id": sid,
        "cwd": project_root,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    env = _subprocess_env()
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(STAGE_DETECTOR)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def run_post_tool_handler(
    sid: str,
    project_root: str,
    raw_event: dict,
    *,
    timeout: int = 15,
) -> tuple[str, str, int]:
    """subprocess 调 post_tool_handler.py，喂 PostToolUse 事件。"""
    event = dict(raw_event)
    event.setdefault("session_id", sid)
    event.setdefault("cwd", project_root)
    event.setdefault("hook_event_name", "PostToolUse")

    result = subprocess.run(
        [sys.executable, str(POST_TOOL_HANDLER)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_subprocess_env(),
    )
    return result.stdout, result.stderr, result.returncode


# ── state 读侧 ─────────────────────────────────────────────────────────────

def read_state(sid: str, project_root: str) -> Optional[dict]:
    """读 model_router_state_<sid>.json；不存在或损坏返回 None。"""
    p = Path(project_root) / ".claude" / f"model_router_state_{sid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_decision(sid: str, project_root: str) -> Optional[dict]:
    """走 proxy._v13_resolve_decision() 路径读 decision dict。"""
    sys.path.insert(0, HOOKS_DIR_STR)
    try:
        from proxy import _v13_resolve_decision
        return _v13_resolve_decision(sid, project_root)
    finally:
        # 保持 sys.path 干净
        try:
            sys.path.remove(HOOKS_DIR_STR)
        except ValueError:
            pass


# ── event 构造 ─────────────────────────────────────────────────────────────

def make_user_prompt_event(
    prompt: str,
    sid: str,
    project_root: str,
) -> dict:
    return {
        "session_id": sid,
        "cwd": project_root,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def make_post_tool_event(
    sid: str,
    tool_name: str,
    tool_input: dict,
    project_root: str,
) -> dict:
    return {
        "session_id": sid,
        "cwd": project_root,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


# ── 断言辅助 ───────────────────────────────────────────────────────────────

def assert_decision_shape(testcase: Any, decision: Optional[dict]) -> None:
    """断言 decision 是合法 DecisionRecord dict（非空 + 必要字段）。"""
    testcase.assertIsNotNone(decision, "decision 必须非 None")
    testcase.assertIsInstance(decision, dict)
    required = {
        "session_id", "prompt_id", "task_pattern", "task_complexity",
        "prompt_confidence", "runtime_score", "todo_score", "final_model",
        "locked", "decision_source", "last_update",
    }
    missing = required - set(decision.keys())
    testcase.assertFalse(
        missing,
        f"DecisionRecord 缺字段: {missing}; 实际: {sorted(decision.keys())}",
    )
