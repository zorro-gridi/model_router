#!/usr/bin/env python3
"""
Stage-Aware Model Router
========================
本地代理服务，监听 CC 的 API 请求，读取 ~/.claude/hooks/model_router/current_stage 文件，
按当前工作流阶段将请求转发到最合适的模型。

阶段 → 模型映射：
  brainstorm  → 头脑风暴：便宜快速模型（DeepSeek / Haiku）
  decide      → 决策分析：强推理模型（Opus）
  design      → 方案设计：Opus
  plan        → 任务拆解：Sonnet（结构化输出）
  implement   → 工程实施：Sonnet（主力编码）
  audit       → 工程审计：Opus（漏洞最贵）
  default     → 未指定：Sonnet

Operation-type 路由 — [已废弃 2026-06-14]
  废弃原因：write/read/search 只是"动作"，不是"任务属性"。
  真正影响模型选择的是"任务类型 + 任务复杂度 + 当前阶段"。
  Complexity 分类器（设计文档 §6.4）已吞掉 op 的原始职责。
  兼容策略：OPERATION_CONFIG = {}，所有 `if op in OPERATION_MODELS` 自然为 False，
  自动退化到 stage 路由，无需逐一修改下游消费代码。
  下方 _op_file_path() / read_operation() 及相关路由分支已注释保留，用于追溯。

Model-override 路由（2026-06-13 引入，最高优先级）：
  检出 model 覆盖时完全覆盖 stage 路由。
  model 文件位置：<project_root>/.claude/model_<sid>（与 stage_<sid> 同目录、仅前缀替换）。
  路由优先级: model_override > stage > default[+workflow+batch]。

用法：
  python3 proxy.py                  # 启动代理（默认 :7878）
  python3 proxy.py --port 7878      # 自定义端口
  python3 proxy.py --dry-run        # 只打印路由决策，不转发
"""

import argparse
import http.server
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── 配置 ───────────────────────────────────────────────────────────────────────

# ── 分 session 阶段管理 ──
# proxy.py 是无 stdin 上下文的 HTTP 服务器，无法直接拿到 session_id。
# 它依赖 stage_detector.py（UserPromptSubmit hook）维护的 active_session 指针。
# active_session 存储的是阶段文件的**完整绝对路径**，proxy 直接读取即可，
# 无需再拼接 STAGE_DIR。
HOOK_DIR            = Path.home() / ".claude" / "hooks" / "model_router"
ACTIVE_SESSION_FILE  = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE    = HOOK_DIR / "current_stage"   # 全局后备
STATE_INDEX_FILE     = HOOK_DIR / "state_index.json"  # 设计文档 §13 Project Binding
LOG_FILE             = Path.home() / ".claude" / "stage-router.log"
PORT                 = 7878
ENV_FILE             = Path(__file__).parent / ".env"   # hooks/model_router/.env

# 用户服务的"内部请求"标记 header（防止 5xx 误触发 fallback）
# 详见 _is_internal_request() 注释。Claude Code 的请求不会带这个 header。
INTERNAL_SOURCE_HEADER = os.environ.get("STAGE_ROUTER_INTERNAL_HEADER", "X-Stage-Router-Source")

# 阶段 → (provider_base_url, model, api_key_env, protocol)
#
# 协议方向（默认端到端都是 Anthropic Messages API）：
#   Claude Code (Anthropic 协议)
#     → 本地代理 (Anthropic 协议，仅做 model 改写 + 转发)
#       → 上游 (Anthropic 协议：https://api.minimaxi.com/anthropic、
#              https://api.deepseek.com/anthropic)
#
# protocol 字段：
#   "anthropic" — 默认。上游兼容 Anthropic Messages API，透明转发，
#                 不做请求/响应格式转换。绝大多数第三方 provider 都用这个。
#   "openai"    — opt-in。上游是 OpenAI Chat Completions 兼容端点：
#                   • MiniMax：  https://api.minimaxi.com/v1
#                   • DeepSeek： https://api.deepseek.com
#                 代理会自动做 Anthropic ↔ OpenAI 协议转换。
#
# 环境变量：每个 provider 一个独立 key，按 stage 路由时互不污染。
#   MINIMAX_API_KEY  → MiniMax（https://api.minimaxi.com/anthropic）
#   DEEPSEEK_API_KEY → DeepSeek（https://api.deepseek.com/anthropic）
#
# 模型分配策略：
#   - brainstorm → deepseek-v4-flash（便宜快速，发散探索）
#   - plan / implement / default → deepseek-v4-pro（结构化主力编码）
#   - decide / design / audit → MiniMax-M3（深度推理、架构、审计）
# 从统一配置文件导入（hooks/model_router/stage_config.py）
from stage_config import (
    STAGE_MODELS, FALLBACK_MODELS,
    OPERATION_MODELS, OPERATION_FALLBACK_MODELS,
    STAGE_CONFIG, OPERATION_CONFIG,
    MODEL_TO_CONFIG,
)

# 模型覆盖指令解析（~model / ~m / 自然语言）
# proxy 当前回合检测 prompt 内嵌指令——不等 stage_detector 写入 model_<sid>，
# 避免"用户发 ~model 时当前回合仍是旧模型，下回合才生效"的一回合延迟。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_alias import detect_model_override  # noqa: E402

# 复用 @hooks/compact/utils.py 的 project_root 查找逻辑（设计文档 §13 4 级查找
# 的 Level 1 "Project Binding" 需要按 project_root 索引 state_index.json）。
# 跟 stage_detector.py 写入端用同一份查找器，保持一致性。
sys.path.insert(0, os.path.expanduser("~/.claude"))
from hooks.compact.utils import _find_project_root  # noqa: E402

# ── 原生 Anthropic 端点白名单 ──
# 这些端点的 extended thinking 是**真实现**的（合法 signature、再次回传能校验过），
# 代理不应剥离请求里的 thinking 字段。
# 其它端点（即使是 anthropic 协议兼容的）也走降级——实测 deepseek / MiniMax 的
# signature 都是 message id 假装的，会间歇性触发 400。
NATIVE_ANTHROPIC_DOMAINS: tuple[str, ...] = (
    "api.anthropic.com",
)


def _is_native_anthropic(target_base: str) -> bool:
    """判断目标 base URL 是否为原生 Anthropic 端点（白名单匹配）。"""
    return any(domain in target_base for domain in NATIVE_ANTHROPIC_DOMAINS)

# ── 日志 ───────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stage-router")

# ── 阶段读取（分 session 管理）─────────────────────────────────────────────────

def _read_stage_file(path: Path) -> str | None:
    """读取指定阶段文件，不存在或为空时返回 None。"""
    try:
        content = path.read_text().strip().lower()
        return content if content else None
    except FileNotFoundError:
        return None


def read_stage() -> str:
    """
    读取当前阶段（设计文档 §13 4 级查找）：
      Level 1: Project Binding — 从 state_index.json 按 project_root 查
      Level 2: session_id       — 从 state_index.json 查匹配 session
      Level 3: timestamp        — state_index 中 last_active 最新者
      Level 4: active_session   — 兼容旧版单点指针

    proxy.py 是无 stdin 的 HTTP 服务器，无法直接拿到 session_id 或 project_root。
    它依赖 stage_detector.py（UserPromptSubmit hook）维护的 active_session 指针 +
    state_index.json。

    active_session 存储的是阶段文件的完整路径（如
    /Users/zorro/project/.claude/stage_aaa-bbb），从中可提取 project_root =
    /Users/zorro/project（路径去掉末尾 .claude/stage_<sid> 两段）。
    """
    # 先解析 active_session 路径 → 拿到 project_root（作为 Level 1 查找键）
    active_path: Path | None = None
    try:
        ap = ACTIVE_SESSION_FILE.read_text().strip()
        if ap:
            active_path = Path(ap)
    except FileNotFoundError:
        pass

    # Level 1: Project Binding — state_index.json[project_root]
    # project_root 通过复用 @hooks/compact/utils.py 的 _find_project_root 算得
    # （沿 .claude/ 优先、.git/ 备选的规则，跟 stage_detector 写入端保持一致）
    if active_path is not None:
        project_root = str(_find_project_root_for_stage_path(active_path))
        state_via_index = _read_state_index_for_project(project_root)
        if state_via_index:
            stage_via_index = state_via_index.get("stage")
            if stage_via_index and stage_via_index in STAGE_MODELS:
                return stage_via_index
            if stage_via_index:
                log.warning(
                    f"state_index[{project_root}] 阶段值 '{stage_via_index}' "
                    f"未知，回退到 active_session"
                )

    # Level 4: active_session 指针（兼容旧版 / state_index 缺失时回退）
    if active_path is not None:
        content = _read_stage_file(active_path)
        if content and content in STAGE_MODELS:
            return content
        if content:
            log.warning(
                f"active_session 指向 {active_path} 未知阶段值 '{content}'，"
                f"回退到 default"
            )

    # 兜底
    return "default"


def _find_project_root_for_stage_path(stage_path: Path) -> Path:
    """从 stage_<sid> 路径反推 project_root。

    实现：复用 @hooks/compact/utils.py::_find_project_root（设计文档 §13 的
    查找规则：.claude/ 优先、.git/ 备选、最多 20 层）— 但要先把"起点"设到
    stage_path 的父目录的父目录（去掉 .claude/stage_<sid> 两段），
    否则会停在自己这一层 .claude/ 上，得出 project_root=stage_path.parent。

    例:
      /Users/zorro/project/.claude/stage_aaa
        → 起点 /Users/zorro/project
        → _find_project_root 在 /Users/zorro/project 发现 .claude/
        → 返回 /Users/zorro/project
    """
    # 把"起点"设为去掉 .claude/stage_<sid> 后的目录
    p = stage_path
    if p.name.startswith("stage_"):
        p = p.parent  # .claude/
    if p.name == ".claude":
        p = p.parent  # project_root/
    return _find_project_root(p)


def _read_state_index_for_project(project_root: str) -> dict | None:
    """从 state_index.json 读取指定 project_root 的会话条目。

    设计文档 §13 Level 1: Project Binding。
    缺失/损坏/无匹配时返回 None（让调用方走下一级）。
    """
    try:
        content = STATE_INDEX_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get(project_root)


# ── Operation-type 读取 — [已废弃 2026-06-14] ──
# 废弃原因：write/read/search 只是"动作"不是路由维度。
# Complexity 分类器（设计文档 §6.4）已吞掉 op 的原始职责。
# OPERATION_CONFIG = {} 使得 `op in OPERATION_MODELS` 永远为 False，
# 下游分支自然退化到 stage 路由。
# 函数体注释保留以备未来参考或回退。

# def _op_file_path(stage_file: Path) -> Path:
#     """从 stage_<sid> 路径派生 op_<sid> 路径（同目录、仅前缀替换）。
#     与 stage_detector._op_file_path 保持完全相同的派生规则。
#     """
#     return stage_file.with_name(stage_file.name.replace("stage_", "op_", 1))
#
#
# def read_operation() -> str | None:
#     """
#     读取当前 op，路径解析复用 stage_detector 的派生规则。
#     proxy.py 是无 stdin 的 HTTP 服务器：从 active_session 指针拿到
#     stage_<sid> 完整路径，再派生 op_<sid>。
#     返回 None 表示"无 op 信号"——proxy 走 stage 路由（与升级前行为一致）。
#     """
#     try:
#         active_path = ACTIVE_SESSION_FILE.read_text().strip()
#         if active_path:
#             content = _read_stage_file(_op_file_path(Path(active_path)))
#             if content and content in OPERATION_MODELS:
#                 return content
#             if content:
#                 log.warning(
#                     f"op_<sid> 未知 op 值 '{content}'，忽略 op 走 stage 路由"
#                 )
#     except FileNotFoundError:
#         pass
#     return None


def read_operation() -> Optional[str]:
    """[已废弃 2026-06-14] 始终返回 None。
    write/read/search 只是动作不是路由维度，
    模型选择现由 Complexity 分类器（§6.4）接管。
    """
    return None


# ── Model-override 读取（最高路由优先级）───────────────────────────────────────

def _model_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 model_<sid> 路径（同目录、仅前缀替换）。
    与 stage_detector._model_file_path 保持完全相同的派生规则。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "model_", 1))


def read_model_override() -> str | None:
    """
    读取当前 model 覆盖，路径解析复用 stage_detector 的派生规则。
    proxy.py 是无 stdin 的 HTTP 服务器：从 active_session 指针拿到
    stage_<sid> 完整路径，再派生 model_<sid>。
    返回 None 表示"无 model 覆盖"——proxy 按 op > stage 路由。
    """
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_model_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass
    return None


def _extract_prompt_model_override(body: bytes) -> tuple[Optional[str], bool]:
    """
    从 Anthropic Messages API 请求 body 中提取"最近一条 user message"的内容，
    喂给 model_alias.detect_model_override()，返回 (canonical_model, is_reset)。

    仅解析请求 body 里的最后一条 user 消息——因为 user 可能在中途改模型。
    请求/响应都是 JSON。body 可能是：{"messages": [{"role": "user", "content": "..."}]}
    content 可能是字符串，也可能是 [{"type": "text", "text": "..."}] 数组。

    解析失败（非 JSON、空 body、无 user message）时返回 (None, False)，
    让 proxy 继续走 op/stage 默认路由。
    """
    if not body:
        return (None, False)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (None, False)

    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return (None, False)

    # 反向找最近一条 user 消息
    user_msg = None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_msg = m
            break
    if user_msg is None:
        return (None, False)

    content = user_msg.get("content")
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                # text block
                if part.get("type") in ("text", None) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                # tool_result 也带内容（用户工具返回的"文本"），不参与 ~model 解析——跳过
    else:
        return (None, False)

    user_text = "\n".join(text_parts)
    if not user_text.strip():
        return (None, False)

    return detect_model_override(user_text)


def resolve_model_routing(model_name: str) -> tuple[str, str, str, str, str, str, str] | None:
    """
    搜索 STAGE_CONFIG + OPERATION_CONFIG 查找 model_name 对应的路由参数。

    返回 (base_url, model, api_key_env, protocol,
          fb_base_url, fb_model, fb_api_key_env, fb_protocol)
    或 None（未找到该 model 的配置）。
    """
    # 搜索所有配置，找到 model_name 作为 primary 或 fallback 的条目
    for cfg in list(STAGE_CONFIG.values()) + list(OPERATION_CONFIG.values()):
        if cfg["model"] == model_name:
            return (
                cfg["base_url"], cfg["model"], cfg["api_key_env"], cfg["protocol"],
                cfg["fb_base_url"], cfg["fb_model"], cfg["fb_api_key_env"], cfg["fb_protocol"],
            )
    # 也可作为 fallback model 匹配（用户可能想直接用备选模型）
    for cfg in list(STAGE_CONFIG.values()) + list(OPERATION_CONFIG.values()):
        if cfg["fb_model"] == model_name:
            # 反向：把 fb 当作 primary，原 primary 当作 fb
            return (
                cfg["fb_base_url"], cfg["fb_model"], cfg["fb_api_key_env"], cfg["fb_protocol"],
                cfg["base_url"], cfg["model"], cfg["api_key_env"], cfg["protocol"],
            )
    return None

# ── Sticky Fallback（per-session 主模型降级记忆）───────────────────────────────
# 当主模型调用失败、fallback 成功后，写入 fallback_<sid> 文件。
# 后续该 session 的所有请求默认使用 fallback 模型，避免反复重试已失败的主模型。

def _fallback_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 fallback_<sid> 路径（同目录、仅前缀替换）。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "fallback_", 1))


def read_fallback() -> str | None:
    """
    读取当前 session 的 sticky fallback 模型名。
    从 active_session 指针拿到 stage_<sid> 完整路径，再派生 fallback_<sid>。
    返回 None 表示"无 sticky fallback"——正常走主模型路由。
    """
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_fallback_file_path(Path(active_path)))
            if content:
                return content
    except FileNotFoundError:
        pass
    return None


def write_fallback(model: str) -> None:
    """写入 sticky fallback 模型名到 fallback_<sid>。"""
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            fb_path = _fallback_file_path(Path(active_path))
            fb_path.parent.mkdir(parents=True, exist_ok=True)
            fb_path.write_text(model + "\n")
            log.info(
                f"sticky fallback 已激活: 主模型不可用，"
                f"后续请求将默认使用 {model}"
            )
    except Exception as e:
        log.error(f"写入 fallback_<sid> 失败: {e}")


# ── Pattern / Complexity / Batch / State-Index 读取（设计文档 §6.2-6.4 / §13）──

def _pattern_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "pattern_", 1))


def _complexity_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "complexity_", 1))


def _batch_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "batch_", 1))


def _active_stage_path() -> Path | None:
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            return Path(active_path)
    except FileNotFoundError:
        pass
    return None


def _session_id_from_active() -> str:
    """从 active_session 指针中提取 session_id（UUID 前 8 位）。

    active_session 存的是 stage_<sid> 完整路径，例如：
      /Users/zorro/project/.claude/stage_301e00d0-5a51-4674-9234-93ae806ccc57
    → 提取 'stage_' 之后到下一个 '.'/结尾的段，即 sid：
      301e00d0-5a51-4674-9234-93ae806ccc57
    日志统一截取前 8 位（与 statusline.sh 中的 session_id 灰底标记保持一致）。

    无 active_session 指针时返回 'none'——避免日志里出现 '未知' 这种模棱两可的标记。
    """
    p = _active_stage_path()
    if not p:
        return "none"
    name = p.name  # 例如 'stage_301e00d0-...'
    if name.startswith("stage_"):
        sid = name[len("stage_"):]
    else:
        sid = name
    return sid[:8] if sid else "none"


def read_pattern() -> dict | None:
    """读取当前 session 的 task pattern 标注（Shadow Mode JSON）。"""
    p = _active_stage_path()
    if not p:
        return None
    try:
        content = _pattern_file_path(p).read_text().strip()
        if content:
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def read_complexity() -> dict | None:
    """读取当前 session 的 complexity 评估（§6.4）。"""
    p = _active_stage_path()
    if not p:
        return None
    try:
        content = _complexity_file_path(p).read_text().strip()
        if content:
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def read_batch() -> dict | None:
    """读取当前 session 的 batch 模板（~batch 载入）。"""
    p = _active_stage_path()
    if not p:
        return None
    try:
        content = _batch_file_path(p).read_text().strip()
        if content:
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


# ── Workflow Planner（设计文档 §6.5）───────────────────────────────────────────
# 简单/中等/复杂 → single/double/triple 模型序列。
#   simple  : 单模型（当前 stage/op 的主模型）
#   medium  : 双步（normal 主模型 + strong 审计模型；normal 先节约 token）
#   complex : 三步（strong 规划 + normal 执行 + strong 审计）
# 当前实现：plan 在 proxy 主线程中只取"第一步模型"（执行模型），后续 step
# 留待编排层或多 agent 层消费 plan。本期目标是"按复杂度挑对模型"，plan 完整
# 序列写到路由日志供追溯。

WORKFLOW_PLANNER: dict[str, dict] = {
    # complexity label → 模型选择策略（按 stage 上下文细分）
    # simple → 用主模型
    # medium → 先 strong 规划（fb_model），再 normal 主模型执行
    # complex → strong 规划 → normal 执行 → strong 审计
    "simple": {
        "type":  "single",
        "steps": ["execute"],
        # 选模型规则：直接用 stage/op 的主模型
        "model_rule": "primary",
    },
    "medium": {
        "type":  "double",
        "steps": ["plan", "execute"],
        # 选模型规则：执行仍用 primary（normal），但 plan 步骤可用 strong
        "model_rule": "primary",
    },
    "complex": {
        "type":  "triple",
        "steps": ["plan", "execute", "audit"],
        "model_rule": "primary",
    },
}


def build_workflow_plan(stage_or_op: str, is_op: bool,
                        primary_model: str,
                        strong_model: str,
                        complexity_label: str) -> dict:
    """
    根据 complexity 标签生成 workflow_plan。
    返回 dict：{"type": "single|double|triple", "steps": [...], "models": [...]}
    本期只用于日志与 /trace 返回值；实际执行仍按 primary_model 路由。
    """
    rule = WORKFLOW_PLANNER.get(complexity_label, WORKFLOW_PLANNER["medium"])
    if rule["type"] == "single":
        return {
            "type":   "single",
            "steps":  ["execute"],
            "models": [primary_model],
        }
    if rule["type"] == "double":
        return {
            "type":   "double",
            "steps":  ["plan", "execute"],
            "models": [strong_model, primary_model],
        }
    # triple
    return {
        "type":   "triple",
        "steps":  ["plan", "execute", "audit"],
        "models": [strong_model, primary_model, strong_model],
    }


# ── Metrics / Trace（设计文档 §6.8 / §15）────────────────────────────────────
# 每次路由决策写一条 JSONL 到 /tmp/stage_metrics.jsonl，供 /metrics /trace 查询。
METRICS_LOG_FILE = Path("/tmp/stage_metrics.jsonl")
METRICS_MAX_RECORDS = 500  # 环形缓冲：最多保留最近 500 条


def _append_metric(record: dict) -> None:
    """追加一条路由指标（best-effort，失败不阻塞代理）。"""
    try:
        with METRICS_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_metrics(limit: int = 50) -> list[dict]:
    """读取最近 N 条路由指标。"""
    try:
        lines = METRICS_LOG_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _summarize_metrics(records: list[dict]) -> dict:
    """汇总指标：按 model/pattern/complexity 分组计数 + fallback 总数。"""
    from collections import Counter
    model_counter: Counter = Counter()
    pattern_counter: Counter = Counter()
    complexity_counter: Counter = Counter()
    routing_source_counter: Counter = Counter()
    fallback_total = 0
    status_counter: Counter = Counter()
    for r in records:
        if r.get("target_model"):
            model_counter[r["target_model"]] += 1
        if r.get("pattern"):
            pattern_counter[r["pattern"]] += 1
        if r.get("complexity_label"):
            complexity_counter[r["complexity_label"]] += 1
        if r.get("routing_source"):
            routing_source_counter[r["routing_source"]] += 1
        if r.get("used_fallback"):
            fallback_total += 1
        s = r.get("status", 0)
        status_counter[s] += 1
    return {
        "total":              len(records),
        "fallback_total":     fallback_total,
        "by_model":           dict(model_counter),
        "by_pattern":         dict(pattern_counter),
        "by_complexity":      dict(complexity_counter),
        "by_routing_source":  dict(routing_source_counter),
        "by_status":          dict(status_counter),
    }

def _rewrite_response_model(resp_body: bytes, display_model: str) -> bytes:
    """
    将响应体中的 model 字段改写为 CC 能识别的模型名。

    当 proxy 使用内部别名（如 deepseek-v4-flash）作为 target_model 转发时，
    上游返回的响应体中 model 字段也是这个别名。CC 会把它记录到 session state，
    重启后恢复 session 时 CC 无法识别该别名而报 warning。

    此函数在 anthropic 协议响应成功时执行改写：将 model 字段替换为 session-level
    的原始模型名（如 MiniMax-M3），避免 CC 记录不可识别的别名。
    """
    if not resp_body or not display_model:
        return resp_body
    try:
        data = json.loads(resp_body)
        if isinstance(data, dict) and "model" in data and data["model"] != display_model:
            original = data["model"]
            data["model"] = display_model
            return json.dumps(data).encode()
    except (json.JSONDecodeError, TypeError):
        pass
    return resp_body


# ── 请求转发 ───────────────────────────────────────────────────────────────────

def forward_request(
    method: str,
    path: str,
    headers: dict,
    body: bytes,
    target_base: str,
    target_model: str,
    api_key_env: str,
    protocol: str = "anthropic",
    dry_run: bool = False,
) -> tuple[int, dict, bytes]:
    """
    将 CC 发来的 Anthropic Messages 请求转发到目标 provider。

    protocol:
      "anthropic" (默认) — 上游是 Anthropic Messages 兼容端点，透明转发：
                           model 改写 + x-api-key 注入 + 透传 /v1/messages，
                           不做请求/响应格式转换（端到端都是 Anthropic 协议）。
      "openai"           — 上游是 OpenAI Chat Completions 兼容（如硅基流动）：
                           自动做 Anthropic ↔ OpenAI 协议转换。

    注意：协议判断基于 STAGE_MODELS 中显式的 `protocol` 字段，
    不要再用 URL 启发式（如 "deepseek" in target_base）判断——
    DeepSeek 的 Anthropic SDK 端点（https://api.deepseek.com/anthropic）
    就是 Anthropic 协议，必须走 anthropic 分支。
    """
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        log.error(f"环境变量 {api_key_env} 未设置")
        return 500, {}, b'{"error":"API key not set"}'

    # 解析并改写请求体中的 model 字段
    try:
        body_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        body_json = {}

    original_model = body_json.get("model", "unknown")
    body_json["model"] = target_model

    # ── 防御式：thinking 字段降级（非原生 Anthropic 端点）──
    #
    # 策略：
    #   只删除顶层 thinking 参数（阻止上游进入 extended thinking 模式），
    #   但保留历史消息中的 thinking block 不做转换。
    #
    # 原因：deepseek 等 provider 在 thinking 模式下会要求"content[].thinking
    #   must be passed back to the API"——如果把它转成 text block，上游报 400。
    #   而 Anthropic 原生端点的 signature 校验对非原生端点不生效（deepseek/MiniMax
    #   的 signature 是 message id 假装的，它们自己的端点不校验自己生成的签名），
    #   所以保留 thinking block 原样透传是安全的。
    #
    # 白名单：原生 Anthropic（api.anthropic.com）不降级，保留完整 thinking 能力。
    if not _is_native_anthropic(target_base):
        if "thinking" in body_json:
            del body_json["thinking"]

    if protocol == "openai":
        # OpenAI 兼容路径：路径改写 + 请求/响应格式转换
        target_path = "/v1/chat/completions"
        body_json = _to_openai_format(body_json)
    elif protocol == "anthropic":
        # Anthropic 兼容路径：透明转发，路径保持 /v1/messages
        target_path = path
    else:
        log.error(f"未知 protocol: {protocol!r}，回退到 anthropic")
        target_path = path
        protocol = "anthropic"

    new_body = json.dumps(body_json).encode()
    url = target_base.rstrip("/") + target_path

    # ── 调试：扫描请求体结构，辅助排查 400 ──
    msgs = body_json.get("messages", [])
    has_thinking_param = "thinking" in body_json
    thinking_block_total = 0
    thinking_with_sig = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "thinking":
                    thinking_block_total += 1
                    if "signature" in b:
                        thinking_with_sig += 1
    log.info(
        f"路由: session={_session_id_from_active()} "
        f"task_pattern={(read_pattern() or {}).get('prediction', 'none')!r} "
        f"task_complexity={(read_complexity() or {}).get('label', 'none')!r} "
        f"task_stage={read_stage()!r} "
        f"原模型={original_model} → 目标={target_model} "
        f"provider={target_base} protocol={protocol} "
        f"| msgs={len(msgs)} thinking_param={has_thinking_param} "
        f"thinking_blocks={thinking_block_total}(有sig={thinking_with_sig})"
        f" hdrs={list(headers.keys())}"
    )

    if dry_run:
        log.info(f"[DRY-RUN] 将转发到: {url}")
        mock = {"content": [{"type": "text", "text": f"[dry-run] routed to {target_model}"}]}
        return 200, {"content-type": "application/json"}, json.dumps(mock).encode()

    # 构造请求头
    fwd_headers = {"Content-Type": "application/json"}
    if protocol == "openai":
        # OpenAI 用 Bearer Authorization
        fwd_headers["Authorization"] = f"Bearer {api_key}"
    else:
        # Anthropic 用 x-api-key + anthropic-version
        fwd_headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
        fwd_headers["x-api-key"] = api_key
        # 透传 beta 头（CC 会附加）
        if "anthropic-beta" in headers:
            fwd_headers["anthropic-beta"] = headers["anthropic-beta"]

    req = urllib.request.Request(url, data=new_body, headers=fwd_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp_body = resp.read()
            resp_headers = dict(resp.headers)
            if protocol == "openai":
                resp_body = _from_openai_response(resp_body)
            return resp.status, resp_headers, resp_body
    except urllib.error.HTTPError as e:
        body_err = e.read()
        log.error(f"上游错误 {e.code}: {body_err[:200]}")
        return e.code, {}, body_err
    except Exception as e:
        log.error(f"转发失败: {e}")
        return 502, {}, json.dumps({"error": str(e)}).encode()


def _to_openai_format(body: dict) -> dict:
    """Anthropic Messages → OpenAI Chat Completions 格式转换（简化版）。"""
    messages = []

    # system prompt
    if "system" in body:
        sys_content = body["system"]
        if isinstance(sys_content, list):
            # Anthropic 的 system 可以是 content blocks
            text = " ".join(b.get("text", "") for b in sys_content if isinstance(b, dict))
        else:
            text = str(sys_content)
        messages.append({"role": "system", "content": text})

    # user / assistant messages
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # content blocks → 拼接文本
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        text_parts.append(json.dumps(block))
                else:
                    text_parts.append(str(block))
            content = "\n".join(text_parts)
        messages.append({"role": role, "content": content})

    return {
        "model": body.get("model", "deepseek-chat"),
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "temperature": body.get("temperature", 1.0),
        "stream": body.get("stream", False),
    }


def _from_openai_response(body: bytes) -> bytes:
    """OpenAI Chat Completions → Anthropic Messages 响应格式转换。"""
    try:
        data = json.loads(body)
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        anthropic_resp = {
            "id": data.get("id", "msg_router"),
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": data.get("model", "unknown"),
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            },
        }
        return json.dumps(anthropic_resp).encode()
    except Exception as e:
        log.error(f"响应格式转换失败: {e}")
        return body

# ── .env 加载 + 启动校验 ───────────────────────────────────────────────────────

_DOTENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _load_dotenv(env_path: Path) -> int:
    """
    从 .env 文件加载变量到 os.environ（仅在变量未设置时填入，避免覆盖）。

    极简实现：跳过空行和 `#` 注释；值不带引号时去尾随空白；带引号时保留内部空白。
    不依赖 python-dotenv，避免引入外部依赖。

    Returns:
        成功加载的变量数量。
    """
    if not env_path.exists():
        return 0
    loaded = 0
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _DOTENV_LINE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            # 去掉匹配的引号
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            # 已设置的环境变量优先级更高（shell export 覆盖 .env）
            if key not in os.environ:
                os.environ[key] = value
                loaded += 1
    except OSError as e:
        log.error(f"读取 {env_path} 失败: {e}")
    return loaded


def _check_required_keys() -> list[str]:
    """扫描 STAGE_MODELS + OPERATION_MODELS，收集所有需要的 API key 环境变量名，报告缺失项。"""
    needed = {entry[2] for entry in STAGE_MODELS.values()}
    needed |= {entry[2] for entry in OPERATION_MODELS.values()}
    missing = [name for name in sorted(needed) if not os.environ.get(name, "").strip()]
    return missing


# ── HTTP 服务器 ────────────────────────────────────────────────────────────────

def _is_retriable(status: int) -> bool:
    """判断 HTTP 状态是否可重试（账号/限流/服务端故障 → 切备用有意义）。

    参考 DeepSeek / MiniMax 错误码注释（短时不可恢复的硬错误才纳入）：

    纳入的状态码：
      401  — 认证失败（API key 错），CSDN 实测明确标"Non-retryable"
             → 切到 key 正常的备用 provider 有意义
      402  — 上游余额不足（Insufficient Balance），立刻切备用
      403  — 权限禁止（key 无权访问该模型/端点）
             → 备用 provider 的权限边界不同，切备用可能有效
      429  — 限流，主模型配额耗尽
      5xx  — 服务端故障（500/502/503/504 等）
      0    — 网络超时 / 解析失败

    不纳入（"重试 body 不会变好" / "切备用也无效"）:
      400  — 请求体格式错（client bug，body 不会因 provider 不同而变好）
      404  — 资源不存在（主 provider 没有的模型/路径，备用大概率也没有，避免 fallback 死循环）
      422  — 参数错误（同 400）
    """
    return status in (401, 402, 403, 429) or (500 <= status < 600) or status == 0


def _is_internal_request(headers: dict) -> bool:
    """判断当前请求是否来自用户自己的服务（而非 Claude Code）。

    用户服务需要在请求中显式携带 ``X-Stage-Router-Source`` 头（值任意非空），
    proxy 见到此 header 时将 5xx 视为"业务错误"而非"模型故障"——
    直接透传给调用方，不触发任何 fallback 切换、也不写 sticky fallback。
    目的：避免用户的业务 5xx 误触发模型 SDK 调用、污染 fallback 状态、
    浪费 budget。header 名可通过 .env 的 STAGE_ROUTER_INTERNAL_HEADER 自定义。

    CC 发出的请求天然不带此 header（CC 的 SDK 不认识），所以 CC 走原 fallback 逻辑。
    """
    if not headers:
        return False
    # 自愈：即使 header 被配置改了大小写也兼容
    for k, v in headers.items():
        if k.lower() == INTERNAL_SOURCE_HEADER.lower() and v:
            return True
    return False


class RouterHandler(http.server.BaseHTTPRequestHandler):
    dry_run: bool = False

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 访问日志，用自己的 logger

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        headers = {k.lower(): v for k, v in self.headers.items()}

        # ── Prompt 内嵌 ~model 检测（消除一回合延迟）──────────
        # 用户在 ~model ds-v4-pro 的那一回合，stage_detector 写入 model_<sid>
        # 是在请求发起之后。当前 do_POST 收到请求时先扫一次 body 里最近的
        # user message，命中就立刻用命中的模型作为 model_override（写回
        # model_<sid> 文件让 stage_detector 后续也走同一覆盖）。这样用户
        # 发 ~model 的"当前请求"就立即生效，不再等下一回合。
        prompt_model_override, prompt_is_reset = _extract_prompt_model_override(body)

        # ── 路由决策：prompt_model_override > model_override(file) > op > stage > default ──
        # 1) prompt 内嵌 ~model 优先（消除一回合延迟）；同时把结果写回
        #    model_<sid> 文件，让 stage_detector 下一回合也能保持一致。
        # 显式初始化 model_override=None，避免下面 elif/1001 行 if 分支里
        # UnboundLocalError（命中分支才会赋值）。
        model_override = None
        if prompt_model_override:
            # 找到当前 session 的 model 文件路径（如果有就覆盖）
            try:
                active_path = ACTIVE_SESSION_FILE.read_text().strip()
                if active_path:
                    mf = _model_file_path(Path(active_path))
                    mf.parent.mkdir(parents=True, exist_ok=True)
                    mf.write_text(prompt_model_override)
            except (FileNotFoundError, OSError) as e:
                log.warning(f"prompt ~model 写回 model_<sid> 失败: {e}")
            log.info(
                f"prompt ~model 命中: {prompt_model_override!r} "
                f"（已写回 model_<sid>，当前请求立即生效）"
            )
            model_override = prompt_model_override
        elif prompt_is_reset:
            # ~model reset：删除 model_<sid> 文件（如果有）
            try:
                active_path = ACTIVE_SESSION_FILE.read_text().strip()
                if active_path:
                    mf = _model_file_path(Path(active_path))
                    if mf.exists():
                        mf.unlink()
                log.info("prompt ~model reset：清除 model 覆盖")
            except (FileNotFoundError, OSError) as e:
                log.warning(f"prompt ~model reset 删除 model_<sid> 失败: {e}")

        # 2) 读 model_<sid> 文件（stage_detector 上一回合写入的覆盖）
        if not model_override:
            model_override = read_model_override()

        if model_override:
            routing = resolve_model_routing(model_override)
            if routing:
                (base_url, model, key_env, protocol,
                 fb_base, fb_model, fb_key, fb_proto) = routing
                routing_source = f"model={model_override}"
            else:
                log.error(
                    f"model_override={model_override!r} 无法解析路由参数，"
                    f"降级到 op/stage 路由"
                )
                model_override = None  # 走下面的 op/stage 分支

        # ── Pattern / Complexity / Batch（设计文档 §6.2/6.4/§12）──
        # Shadow Mode：先读出，路由决策前先看 batch 是否压倒其他信号。
        # batch 激活时强制走 batch 模板的主模型（保留 stage/op 兜底）。
        pattern_data  = read_pattern()
        complexity    = read_complexity()
        batch         = read_batch()
        pattern_label = pattern_data.get("prediction") if pattern_data else None
        complexity_label = (
            complexity.get("label") if complexity else "medium"
        ) or "medium"
        complexity_score  = complexity.get("score", 50) if complexity else 50
        complexity_source = complexity.get("source", "auto") if complexity else "auto"

        if not model_override:
            # op 路由已废弃（2026-06-14）：OPERATION_MODELS = {}，
            # read_operation() 始终返回 None，此分支自动退化到 stage。
            op = read_operation()

            # ── Batch 强制流程覆盖（优先级 #2：设计文档 §5）──
            # ~batch 激活时直接跳到 PATTERN_CONFIG[template].default_flow[0]，
            # 绕过普通 stage 检测；同时把 PATTERN.primary_model 作为主模型来源。
            # 修复 D5-3（2026-06-14）：之前 batch 文件只写 template+flow+ts，
            # 没有 primary_model，所以下面那段 batch.get("primary_model") 是死代码。
            batch_template = batch.get("template") if batch else None
            if op and op in OPERATION_MODELS:
                base_url, model, key_env, protocol = OPERATION_MODELS[op]
                fb_base, fb_model, fb_key, fb_proto = OPERATION_FALLBACK_MODELS[op]
                routing_source = f"op={op}"
            elif batch_template and batch_template in PATTERN_CONFIG:
                # 强制 stage = PATTERN_CONFIG[template].default_flow[0]
                flow = PATTERN_CONFIG[batch_template].get("default_flow", [])
                stage = flow[0] if flow else read_stage()
                base_url, model, key_env, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                fb_base, fb_model, fb_key, fb_proto = FALLBACK_MODELS.get(
                    stage, FALLBACK_MODELS["default"]
                )
                routing_source = f"stage={stage} [batch={batch_template}]"
                # 强制主模型 = PATTERN.primary_model（如 research/docs 用 deepseek-v4-flash）
                pattern_primary = PATTERN_CONFIG[batch_template].get("primary_model")
                if pattern_primary:
                    try:
                        override_routing = resolve_model_routing(pattern_primary)
                        if override_routing:
                            (base_url, model, key_env, protocol,
                             fb_base, fb_model, fb_key, fb_proto) = override_routing
                            routing_source += f" [batch.primary={pattern_primary}]"
                    except Exception:
                        pass  # primary_model 解析失败 → 保留 stage 默认
            else:
                stage = read_stage()
                base_url, model, key_env, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                fb_base, fb_model, fb_key, fb_proto = FALLBACK_MODELS.get(
                    stage, FALLBACK_MODELS["default"]
                )
                routing_source = f"stage={stage}"

            # ── Workflow Plan（设计文档 §6.5 / §10 算法步骤 6-7）──
            # simple/medium/complex 决定 single/double/triple 模型序列。
            # 实际路由按 plan 真正落地：
            #   simple  → 单模型（主模型）
            #   medium  → 双步：normal 主模型执行，strong 模型审计（保留主执行）
            #   complex → 三步：strong 规划 + normal 执行 + strong 审计
            #            → 实际路由时把 strong 模型（= stage.fb_model）作为主模型，
            #              normal 模型（= stage.model）作为 fb，体现"高阶推理优先"。
            # 设计文档 §17 验收："复杂任务稳定触发 strong→normal→strong"——
            # 在单步 CC 转发场景下，"stable" 通过"complex 任务统一走 strong 主模型"保证。
            workflow = build_workflow_plan(
                stage_or_op=routing_source.split("=", 1)[1].split(" ")[0]
                            if "=" in routing_source else "default",
                is_op=routing_source.startswith("op="),
                primary_model=model,
                strong_model=fb_model,  # 强模型 = stage 配置中的 fb_model（升级路径）
                complexity_label=complexity_label,
            )
            # complex 任务：把主/备对调，让 strong 模型当主、normal 当 fb
            if complexity_label == "complex" and fb_model and fb_model != model:
                (base_url, model, key_env, protocol,
                 fb_base, fb_model, fb_key, fb_proto) = (
                    fb_base, fb_model, fb_key, fb_proto,
                    base_url, model, key_env, protocol,
                )
                routing_source += " [workflow=complex→strong]"
        else:
            # model_override 路径无 workflow 编排（用户已显式指定）
            workflow = {
                "type":   "single",
                "steps":  ["execute"],
                "models": [model],
            }

        # ── Sticky fallback: 主模型曾失败过，交换主/备避免重复重试 ──
        # 仅在自动路由（非 model_override）下生效——用户显式指定模型时不干预
        # 内部服务请求（X-Stage-Router-Source）也跳过 sticky 切换：
        # 用户的业务 5xx 跟"主模型曾失败"无关，不应该被静默改路由。
        internal_req = _is_internal_request(headers)
        # 标志位：本请求是否"实际切换到了备用模型"。
        # 用于 /metrics 的 used_fallback 严格判定（区分 4xx 非可重试错误）。
        fallback_invoked = False
        sticky_fb = (
            read_fallback()
            if (not model_override and not internal_req)
            else None
        )
        # 保存 session 级模型名（CC 能识别的原始模型名），用于响应体 model 字段回写
        session_model = model
        if sticky_fb:
            (base_url, model, key_env, protocol,
             fb_base, fb_model, fb_key, fb_proto) = (
                fb_base, fb_model, fb_key, fb_proto,
                base_url, model, key_env, protocol,
            )
            routing_source += f" [sticky-fb={sticky_fb}]"
            # sticky_fb 触发主备交换 → 本请求实际使用了 fallback
            fallback_invoked = True

        status, resp_headers, resp_body = forward_request(
            method="POST",
            path=self.path,
            headers=headers,
            body=body,
            target_base=base_url,
            target_model=model,
            api_key_env=key_env,
            protocol=protocol,
            dry_run=self.dry_run,
        )

        # 主模型失败且可重试 → 切换备用模型
        # 但内部服务请求（X-Stage-Router-Source）跳过此分支：
        # 用户的业务 5xx 是上游问题，不应触发模型 SDK 二次调用、
        # 也不应写入 sticky fallback（避免污染 CC 后续会话）。
        if _is_retriable(status) and fb_base and fb_model and not internal_req:
            log.warning(
                f"[{routing_source}] 主模型 {model} 返回 {status}，"
                f"切换到备用 {fb_model} [{fb_base}]"
            )
            fallback_invoked = True
            status, resp_headers, resp_body = forward_request(
                method="POST",
                path=self.path,
                headers=headers,
                body=body,
                target_base=fb_base,
                target_model=fb_model,
                api_key_env=fb_key,
                protocol=fb_proto,
                dry_run=self.dry_run,
            )
            # 备用模型成功 + 之前无 sticky + 非 model_override → 写入 sticky fallback
            if not sticky_fb and not _is_retriable(status) and not model_override:
                write_fallback(fb_model)
        elif _is_retriable(status) and internal_req:
            log.info(
                f"[{routing_source}] 内部请求主模型 {model} 返回 {status}，"
                f"按业务 5xx 处理：透传响应，不触发 fallback，不写 sticky"
            )

        # ── 响应体 model 字段回写 ──
        # 如果上游响应中的 model 是内部别名（如 deepseek-v4-flash），CC 会记录它。
        # 回写到 session_model（CC 能识别的原始模型名如 MiniMax-M3），
        # 避免 CC 重启后尝试恢复该别名时报 "not a model this version recognizes"。
        if not _is_retriable(status):
            resp_body = _rewrite_response_model(resp_body, session_model)

        # ── 结构化指标落盘（设计文档 §15）──
        # 每条请求都写一条 JSONL 记录，含 pattern/complexity/score/confidence/
        # token_estimate/fallback_count，供 /metrics /trace 读取。
        try:
            # used_fallback 严格定义：本请求"实际使用或触发了备用模型"
            #   - sticky_fb 路径：进入时主备已交换，记为 True
            #   - 主模型失败后切到 fb 模型：记为 True
            #   - 4xx 非可重试错误（400/404/422 等）：主备都没切到，记为 False
            #     之前的 `status >= 400` 会把 400/404/422 全算成"用了 fallback"，
            #     导致 /metrics 统计严重虚高。
            used_fallback = bool(sticky_fb) or fallback_invoked
            _append_metric({
                "ts":               time.time(),
                "path":             self.path,
                "routing_source":   routing_source,
                "target_model":     session_model,
                "actual_model":     model,
                "status":           status,
                "pattern":          pattern_label,
                "complexity_label": complexity_label,
                "complexity_score": complexity_score,
                "complexity_source": complexity_source,
                "workflow_type":    workflow.get("type"),
                "workflow_models":  workflow.get("models"),
                "internal_request": internal_req,
                "batch_template":   batch.get("template") if batch else None,
                "used_fallback":    used_fallback,
            })
        except Exception:
            pass

        # ── 结构化路由日志（设计文档 §15）──
        log.info(
            f"[{routing_source}] target={session_model} actual={model} "
            f"status={status} pattern={pattern_label} "
            f"complexity={complexity_label}({complexity_score},src={complexity_source}) "
            f"workflow={workflow.get('type')} batch={batch.get('template') if batch else None}"
        )

        self.send_response(status)
        self.send_header("Content-Type", resp_headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        # 健康检查
        if self.path == "/health":
            payload = {"status": "ok", "sticky_fallback": read_fallback()}

            model_override = read_model_override()
            if model_override:
                routing = resolve_model_routing(model_override)
                if routing:
                    (_, model, _, protocol, _, fb_model, _, _) = routing
                    payload.update(
                        model_override=model_override,
                        op=None, stage=None,
                        model=model, protocol=protocol, fallback=fb_model,
                        routing_source=f"model={model_override}",
                    )
                else:
                    # 无法解析 → 降级到 op/stage
                    op = read_operation()
                    if op and op in OPERATION_MODELS:
                        _, model, _, protocol = OPERATION_MODELS[op]
                        _, fb_model, _, _ = OPERATION_FALLBACK_MODELS[op]
                        payload.update(
                            model_override=model_override, op=op, stage=None,
                            model=model, protocol=protocol, fallback=fb_model,
                            routing_source=f"op={op}",
                        )
                    else:
                        stage = read_stage()
                        _, model, _, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                        _, fb_model, _, _ = FALLBACK_MODELS.get(stage, FALLBACK_MODELS["default"])
                        payload.update(
                            model_override=model_override, op=None, stage=stage,
                            model=model, protocol=protocol, fallback=fb_model,
                            routing_source=f"stage={stage}",
                        )
            else:
                op = read_operation()
                if op and op in OPERATION_MODELS:
                    _, model, _, protocol = OPERATION_MODELS[op]
                    _, fb_model, _, _ = OPERATION_FALLBACK_MODELS[op]
                    payload.update(
                        model_override=None, op=op, stage=None,
                        model=model, protocol=protocol, fallback=fb_model,
                        routing_source=f"op={op}",
                    )
                else:
                    stage = read_stage()
                    _, model, _, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                    _, fb_model, _, _ = FALLBACK_MODELS.get(stage, FALLBACK_MODELS["default"])
                    payload.update(
                        model_override=None, op=None, stage=stage,
                        model=model, protocol=protocol, fallback=fb_model,
                        routing_source=f"stage={stage}",
                    )

            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        elif self.path == "/metrics":
            # 路由指标聚合（设计文档 §6.8 / §15）
            records = _read_metrics(limit=200)
            payload = {
                "summary": _summarize_metrics(records),
                "recent":  records[-20:],  # 最近 20 条
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        elif self.path == "/trace":
            # 单条最新路由决策的完整 trace（设计文档 §6.8 / §15）
            records = _read_metrics(limit=1)
            latest = records[-1] if records else {}
            payload = {
                "current_session": {
                    "stage":     read_stage(),
                    "op":        read_operation(),
                    "model_override": read_model_override(),
                    "pattern":   read_pattern(),
                    "complexity": read_complexity(),
                    "batch":     read_batch(),
                    "sticky_fallback": read_fallback(),
                },
                "latest_request": latest,
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            self.send_response(404)
            self.end_headers()

# ── 入口 ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stage-Aware Model Router")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--dry-run", action="store_true", help="只打印路由决策，不实际转发")
    args = parser.parse_args()

    RouterHandler.dry_run = args.dry_run

    # 1) 从本目录 .env 加载（shell 环境变量优先级更高，不会被覆盖）
    n_loaded = _load_dotenv(ENV_FILE)
    if n_loaded:
        log.info(f"从 {ENV_FILE} 加载了 {n_loaded} 个环境变量")
    elif not ENV_FILE.exists():
        log.warning(
            f"未找到 {ENV_FILE}，将仅依赖 shell 环境变量。"
            f"可执行: cp {ENV_FILE}.example {ENV_FILE}  然后填入 key"
        )

    # 2) 启动期校验：缺少 API key 就直接报错退出（避免请求飞到一半才 500）
    missing = _check_required_keys()
    if missing:
        log.error("=" * 60)
        log.error(f"缺少必需的 API key 环境变量: {', '.join(missing)}")
        log.error(f"请在 {ENV_FILE} 中填入，或在 shell 中 export。")
        log.error("=" * 60)
        sys.exit(1)

    log.info(f"Stage Router 启动 → 监听 http://127.0.0.1:{args.port}")
    log.info(f"阶段目录: {HOOK_DIR}（per-session 阶段文件在 <project_root>/.claude/stage_<id>）")
    log.info(f"日志文件: {LOG_FILE}")
    log.info(
        "已配置 key: " + ", ".join(
            f"{name}=***{os.environ[name][-4:]}" if len(os.environ[name]) > 4 else f"{name}=(set)"
            for name in sorted({e[2] for e in STAGE_MODELS.values()})
        )
    )
    if args.dry_run:
        log.info("[DRY-RUN 模式] 请求不会实际转发")

    server = http.server.HTTPServer(("127.0.0.1", args.port), RouterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stage Router 已停止")


if __name__ == "__main__":
    main()
