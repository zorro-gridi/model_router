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

Operation-type 路由 — [已删除 2026-06-15 v1.3 Stage 7]
  废弃原因：write/read/search 只是"动作"，不是"任务属性"。
  真正影响模型选择的是"任务类型 + 任务复杂度 + 当前阶段"。
  Complexity 分类器（设计文档 §6.4）已吞掉 op 的原始职责。

Model-override 路由（2026-06-13 引入，最高优先级）：
  检出 model 覆盖时完全覆盖 stage 路由。
  model 文件位置：<project_root>/.claude/model_<sid>（与 stage_<sid> 同目录、仅前缀替换）。
  路由优先级: model_override > stage > default[+workflow+batch]。

用法：
  python3 proxy.py                  # 启动代理（默认 :7878）
  python3 proxy.py --port 7878      # 自定义端口
  python3 proxy.py --dry-run        # 只打印路由决策，不转发

端口配置优先级：--port 命令行参数 > STAGE_ROUTER_PORT 环境变量 > 默认 7878
"""

import argparse
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional

# ── .env 自动加载（必须在所有读 env 的代码之前）──
# Claude Code hook 子进程不会继承 shell export 的 env，必须从 .env 读 key 并
# 注入 os.environ。统一用 _load_env.load_plugin_env：先读共享层 hooks/.env，
# 再读 plugin-private 层 hooks/model_router/.env。已设置的 env 变量优先级
# 更高（不覆盖）。
sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
from _load_env import load_plugin_env  # noqa: E402
load_plugin_env(__file__)  # noqa: E402

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
# STAGE_ROUTER_PORT 环境变量可覆盖默认端口，优先级：--port > env > 默认值
PORT                 = int(os.environ.get("STAGE_ROUTER_PORT", "7878"))

# 用户服务的"内部请求"标记 header（防止 5xx 误触发 fallback）
# 详见 _is_internal_request() 注释。Claude Code 的请求不会带这个 header。
INTERNAL_SOURCE_HEADER = os.environ.get("STAGE_ROUTER_INTERNAL_HEADER", "X-Stage-Router-Source")

# ── Sticky Fallback TTL（2026-06-16 引入）─────────────────────
# sticky fallback 文件的总有效期。TTL 到期后 read_fallback() 自动 unlink，
# 回到主 provider 路由。最坏情况 3h 自动恢复。
STICKY_TTL_SECONDS = int(os.environ.get("STAGE_ROUTER_STICKY_TTL_SECONDS", "10800"))  # 默认 3h

# auto-recovery 清 sticky 时的 grace period：探测发现恢复时若 sticky failed_at
# 在最近 N 秒内写入，跳过清除，避免清掉探测期间用户新写的 sticky（罕见但可能）。
AUTO_RECOVERY_GRACE_SECONDS = int(os.environ.get("STAGE_ROUTER_AUTO_RECOVERY_GRACE_SECONDS", "30"))

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
    STAGE_CONFIG,
    PATTERN_CONFIG,
    MODEL_TO_CONFIG,
    STRONG_MODEL,   # 设计文档 §10 路由算法：全局强模型
    RECLASSIFY_INTERVAL,          # per-API-request 动态分类间隔
    MODEL_TO_PROVIDER,            # provider 级 fallback：model → provider
    DEFAULT_FALLBACK_PROVIDER,    # provider 级 fallback：失败 provider → 替代 provider
    PROVIDER_COMPLEXITY_MODELS,   # provider 级 fallback：provider → {complexity → model}
    KNOWN_PROVIDER_NAMES,         # provider 级 fallback：已知 provider 名集合
)

# 模型覆盖指令解析（~model / ~m / 自然语言）
# proxy 当前回合检测 prompt 内嵌指令——不等 stage_detector 写入 model_<sid>，
# 避免"用户发 ~model 时当前回合仍是旧模型，下回合才生效"的一回合延迟。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_alias import detect_model_override, parse_model_override  # noqa: E402
# per-API-request LLM 重新分类（设计文档 §6.2 / §6.4 间隔触发）
from llm_classifier import classify as _proxy_llm_classify  # noqa: E402
# 高阶模型 rate limit（设计文档 §18 R18-3 / D18-3-1 修复 2026-06-14）：
# complex 任务强制跳到 STRONG_MODEL 前，先查配额；超额则降级回原 stage 主模型。
from rate_limit import check_rate_limit, consume as rate_limit_consume      # noqa: E402

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

# ── Per-Session 路由绑定（2026-06-17）─────────────────────────────────────────
# 多 session 并发时，通过模型名后缀 _s<sid> 将 API 请求绑定到特定 session 的
# stage 配置，消除 state_index.json 的竞态（设计文档 §13 Per-Session Routing）。
#
# 机制：
#   - 首次请求（model 无后缀）→ 仍用 _active_stage_path() MPRA → 响应注入 _s<sid>
#   - 后续请求（model 带后缀）→ 解析 sid → 直查 stage_<sid> → 跳过 MPRA
#   - thread-local 缓存：同一 HTTP 处理线程内 _active_stage_path() 返回缓存值

_routing_state = threading.local()
# _routing_state.stage_path  — 本请求绑定的 stage_<sid> 绝对路径
# _routing_state.sid         — 本请求绑定的 session_id（UUID），用于响应注入

_SID_SUFFIX_RE = re.compile(
    r'_s([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$'
)

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

# ── V1.3 读侧（Stage 7.4 清理 feature flag）───────────────────────────────────

def _v13_resolve_decision(sid: str, project_root: str) -> dict | None:
    """V1.3 读侧：从 model_router_state_<sid>.json（首选）→ 旧 9 文件（fallback）。

    Returns:
        - 新格式胜出:返回 `state["decision"]`(可能为 `{}` 表示决策未初始化)
        - 旧格式 fallback:返回 read_legacy 聚合 dict（含 stage/model_override/...）
        - 都没有:None
    """
    # 延迟导入：state_persistence 启动期不可用时不影响 proxy 启动
    try:
        from state_persistence import SessionStateStore
    except Exception:
        return None

    store = SessionStateStore()

    new_state = store.read_new(sid, project_root)
    if isinstance(new_state, dict):
        decision = new_state.get("decision")
        # decision 字段缺失或 None → 视为未初始化 → 返回 {}（proxy 据此判空）
        if decision is None:
            return {}
        if isinstance(decision, dict):
            return decision
        # 决策字段类型异常 → 回退到 legacy
    # 新文件不存在或损坏 → fallback 到 legacy
    return store.read_legacy(sid, project_root)


# ── V1.3 决策反推 v1.2 stage（Stage 6.2 渐进期兜底）─────────────────────────

def _v13_model_to_stage(final_model: str, task_complexity: str) -> str | None:
    """v1.3 final_model + task_complexity → v1.2 stage 字符串（STAGE_MODELS key）。

    唯一性约束（Stage 6.2a 设计）：
      - deepseek-v4-pro  → decide（1-1 映射，唯一）
      - deepseek-v4-flash → brainstorm（1-1 映射，唯一）
      - MiniMax-M3 + complex → decide（升档语义）
      - MiniMax-M3 + medium  → implement
      - MiniMax-M3 + simple  → default

    Args:
        final_model: DecisionRecord.final_model 字段。
        task_complexity: DecisionRecord.task_complexity 字段（"complex"/"medium"/"simple"）。

    Returns:
        STAGE_MODELS 字典中存在的 stage 字符串；无法映射时返回 None。
    """
    if final_model == "deepseek-v4-pro":
        return "decide"
    if final_model == "deepseek-v4-flash":
        return "brainstorm"
    if final_model == "MiniMax-M3":
        if task_complexity == "complex":
            return "decide"
        if task_complexity == "medium":
            return "implement"
        return "default"
    return None


def _resolve_stage_v13(active_path: Path) -> str | None:
    """v1.3 final_model → v1.2 stage 字符串（Stage 6.2 渐进期兜底）。

    触发场景：Stage 7 完成后旧 stage_<sid> 文件已被删除,但 proxy/STAGE_MODELS
    查表仍需要 stage 字符串做键。此函数从 model_router_state_<sid>.json 的
    decision.final_model + decision.task_complexity 反推等价 stage。

    永不抛错（所有异常静默吞掉），让 read_stage() 在反推失败时仍能兜底 "default"。

    Args:
        active_path: _active_stage_path() 返回的 stage_<sid> 路径（不一定真实存在）。

    Returns:
        v1.2 阶段字符串（"decide"/"brainstorm"/"implement"/"default" 等），
        或 None（无 v1.3 决策 / 反推失败）。
    """
    try:
        if not isinstance(active_path, Path):
            return None
        sid = _extract_session_id_from_stage_path(active_path)
        if not sid:
            return None
        project_root = str(_find_project_root_for_stage_path(active_path))
        resolved = _v13_resolve_decision(sid, project_root)
        if not isinstance(resolved, dict) or not resolved:
            return None
        fm = resolved.get("final_model")
        tc = resolved.get("task_complexity", "simple")
        if not isinstance(fm, str) or not fm:
            return None
        return _v13_model_to_stage(fm, tc)
    except Exception:
        return None


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
    # 多 session 并发修复（2026-06-14）：
    # active_path 不再从全局 ACTIVE_SESSION_FILE 读取，改为由
    # _active_stage_path() 从 state_index.json 解析最近活跃 session。
    # 这样多 session 并发时每个请求独立拿到自己的 stage，互不覆盖。
    active_path: Path | None = _active_stage_path()

    # Level 1: Project Binding — state_index.json[project_root]
    # project_root 通过复用 @hooks/compact/utils.py 的 _find_project_root 算得
    # （沿 .claude/ 优先、.git/ 备选的规则，跟 stage_detector 写入端保持一致）
    current_sid: str | None = None
    if active_path is not None:
        current_sid = _extract_session_id_from_stage_path(active_path)
        project_root = str(_find_project_root_for_stage_path(active_path))
        state_via_index = _read_state_index_for_project(project_root)
        # Level 1 真正匹配需「project_root 命中 + session_id 一致」
        # sid 不一致时让位给 Level 2/3（设计文档 §13 D13-1）
        if state_via_index and current_sid and \
                state_via_index.get("session_id") == current_sid:
            stage_via_index = state_via_index.get("stage")
            if stage_via_index and stage_via_index in STAGE_MODELS:
                return stage_via_index
            if stage_via_index:
                log.warning(
                    f"state_index[{project_root}] 阶段值 '{stage_via_index}' "
                    f"未知，回退到 Level 2/3"
                )

        # Level 2: session_id 全局匹配（设计文档 §13 Level 2）
        # 同一 session 可能在不同 cwd 写过 state_index（多窗口工作区），按 sid 全局找。
        if current_sid:
            all_entries = _read_state_index_all()
            for path, entry in all_entries.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("session_id") == current_sid:
                    stage_via_sid = entry.get("stage")
                    if stage_via_sid and stage_via_sid in STAGE_MODELS:
                        return stage_via_sid
                    break  # 找到 sid 匹配但 stage 值不合法，不再继续

        # Level 3: timestamp 最近活跃（设计文档 §13 D13-1）
        # 同 project_root（或其祖先/后代）下多 session 并发时，新 session
        # 复用最近活跃的 stage——避免每次新开窗口都从 default 起步。
        if current_sid:
            ts_match = _find_state_by_timestamp(project_root, current_sid)
            if ts_match:
                _path, ts_entry = ts_match
                stage_via_ts = ts_entry.get("stage")
                if stage_via_ts and stage_via_ts in STAGE_MODELS:
                    return stage_via_ts

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

    # Level 5: v1.3 决策反推（Stage 6.2 渐进期兜底 — 旧 stage_<sid> 文件可能已
    # 被 Stage 7 删除，但 model_router_state_<sid>.json 还在）。仅在 flag 开启且
    # 前 4 级全 miss 时尝试，避免无谓 IO。
    if active_path is not None:
        try:
            v13_stage = _resolve_stage_v13(active_path)
            if v13_stage and v13_stage in STAGE_MODELS:
                return v13_stage
        except Exception:
            pass

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


def _read_state_index_all() -> dict[str, dict]:
    """读取整个 state_index.json（设计文档 §13 Level 2/3 用）。

    返回 {project_root: entry}；缺失/损坏时返回空 dict。
    """
    try:
        content = STATE_INDEX_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _extract_session_id_from_stage_path(stage_path: Path) -> str | None:
    """从 stage_<sid> 路径中提取 session_id。

    例: /Users/zorro/.claude/.claude/stage_0818b13d-... → "0818b13d-..."
    """
    name = stage_path.name  # stage_<sid>
    if not name.startswith("stage_"):
        return None
    return name[len("stage_"):] or None


def _parse_model_sid(model_str: str) -> tuple[str, str | None]:
    """从模型名中提取 (clean_model, sid)。

    例: "MiniMax-M3_s0818b13d-4f55-4abe-9d68-769b24d4d342"
        → ("MiniMax-M3", "0818b13d-4f55-4abe-9d68-769b24d4d342")

    无后缀时返回 (model_str, None)。
    """
    if not model_str:
        return model_str, None
    m = _SID_SUFFIX_RE.search(model_str)
    if m:
        return model_str[:m.start()], m.group(1)
    return model_str, None


def _resolve_stage_path_for_sid(sid: str) -> Path | None:
    """在 state_index.json 中按 session_id 查找，返回 stage_<sid> 路径。

    与 _active_stage_path() 不同：此函数按 sid 精确匹配，不受 last_active 时间戳
    竞态影响。用于 model 后缀绑定的 session 路由。
    """
    if not sid:
        return None
    all_entries = _read_state_index_all()
    for path_key, entry in all_entries.items():
        if isinstance(entry, dict) and entry.get("session_id") == sid:
            stage_path = Path(path_key) / ".claude" / f"stage_{sid}"
            if stage_path.exists():
                return stage_path
    return None


def _find_state_by_timestamp(project_root: str, current_sid: str) -> tuple[str, dict] | None:
    """Level 3 timestamp 查找：在 state_index 中找与 project_root 同前缀且
    last_active 最新的 entry（排除当前 sid）。

    设计文档 §13 D13-1：同 project_root 下多 session 并发时，新 session 应自动
    复用最近活跃 session 的 stage。
    """
    all_entries = _read_state_index_all()
    if not all_entries:
        return None
    candidates: list[tuple[str, dict]] = []
    for path, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        sid = entry.get("session_id", "")
        if sid == current_sid:
            continue
        # 同 project_root 或 project_root 是 path 的子路径
        if path == project_root or path.startswith(project_root + "/") \
           or project_root.startswith(path + "/"):
            candidates.append((path, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[1].get("last_active", 0), reverse=True)
    return candidates[0]


# ── Model-override 读取（最高路由优先级）───────────────────────────────────────

def _model_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 model_<sid> 路径（同目录、仅前缀替换）。
    与 stage_detector._model_file_path 保持完全相同的派生规则。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "model_", 1))


def read_model_override() -> str | None:
    """
    读取当前 model 覆盖，路径解析复用 stage_detector 的派生规则。
    多 session 并发修复（2026-06-14）：通过 _active_stage_path() 从
    state_index.json 解析 session，不再依赖全局 active_session 指针。
    返回 None 表示"无 model 覆盖"——proxy 按 op > stage 路由。
    """
    p = _active_stage_path()
    if p:
        content = _read_stage_file(_model_file_path(p))
        if content:
            return content
    return None


def _extract_prompt_model_override(body: bytes) -> tuple[Optional[str], bool, Optional[str]]:
    """
    从 Anthropic Messages API 请求 body 中提取"最近一条 user message"的内容，
    喂给 model_alias.parse_model_override()，返回 (canonical_model, is_reset, unknown_alias)。

    仅解析请求 body 里的最后一条 user 消息——因为 user 可能在中途改模型。
    请求/响应都是 JSON。body 可能是：{"messages": [{"role": "user", "content": "..."}]}
    content 可能是字符串，也可能是 [{"type": "text", "text": "..."}] 数组。

    解析失败（非 JSON、空 body、无 user message）时返回 (None, False, None)，
    让 proxy 继续走 op/stage 默认路由。

    unknown_alias 非空时表示用户输入了显式 `~model <name>` 但 alias 未识别——
    设计文档 §12 D12-3：必须给 warning 提示，避免静默失效。
    """
    if not body:
        return (None, False, None)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (None, False, None)

    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return (None, False, None)

    # 反向找最近一条 user 消息
    user_msg = None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_msg = m
            break
    if user_msg is None:
        return (None, False, None)

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
        return (None, False, None)

    user_text = "\n".join(text_parts)
    if not user_text.strip():
        return (None, False, None)

    return parse_model_override(user_text)


# ── Prompt 文本提取 + 脱敏（设计文档 §15 D15-6）─────────────────────
# 日志写完整 prompt 有泄露风险（密码 / API key / token 可能被用户塞进 prompt）。
# 但完全脱敏又让排错困难——所以只对敏感键名作值替换，其它内容原样写。

# 敏感键名单：出现在 key= 形式或 "key": "value" JSON 形式时整段值替换为 [REDACTED]
# 注意：authorization / Authorization 由下面的 _BEARER_RE 单独处理（避免两种规则
# 同时命中同一行导致 token 残留在外面）。
# 用 re.split 抓 key，再单独处理 val，比 re.sub 替换整段更精确（保留 key 名作为提示）。
_SECRET_KEY_NAMES = (
    r'password|passwd|pwd|'
    r'api[_-]?key|access[_-]?key|secret[_-]?key|'
    r'token|access[_-]?token|refresh[_-]?token|jwt|'
    r'private[_-]?key|client[_-]?secret|'
    r'sk-[A-Za-z0-9]{8,}|'   # OpenAI/Anthropic 风格 key
    r'sk-ant-[A-Za-z0-9_-]+|'
    r'sk-or-[A-Za-z0-9_-]+'
)
_SECRET_KV_RE = re.compile(
    r'(?ix)(?P<key>' + _SECRET_KEY_NAMES + r')'
    r'\s*[:=]\s*'
    r'(?P<val>"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)

# Bearer xxx 形式的 Authorization 头
_BEARER_RE = re.compile(
    r'(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-+/=]+',
)


def _scrub_secrets(text: str, max_len: int = 4000) -> str:
    """
    把 prompt 里的敏感字段值替换为 [REDACTED]，并截断到 max_len。

    设计文档 §15 D15-6：避免在 stage_router.log 中持久化 password / api_key /
    token / secret_key 等敏感值。其它内容原样写以保留排错信号。

    规则：
      - 匹配 "key=value" / "key: value" / "key":"value" → **仅 val** 部分替换，
        保留 key 名作为视觉提示（"password=[REDACTED]"）
      - 匹配 "Authorization: Bearer xxx" → xxx 替换
      - 文本超过 max_len 截断（按 §6 D6-1 防止日志爆盘）
    """
    if not text:
        return text
    # 先处理 Authorization Bearer（必须在 SECRET_KV_RE 之前，避免被它误吞）
    out = _BEARER_RE.sub(r'\1[REDACTED]', text)

    def _replace_val(m: "re.Match[str]") -> str:
        val = m.group("val")
        if val[:1] in ('"', "'"):
            # 引号包裹：保留首字符引号 + [REDACTED] + 收尾引号（如 "m[REDACTED]"）
            return m.group("key") + "=" + val[:1] + "[REDACTED]" + val[-1:]
        return m.group("key") + "=[REDACTED]"

    out = _SECRET_KV_RE.sub(_replace_val, out)
    if len(out) > max_len:
        out = out[:max_len] + f"…[truncated {len(text) - max_len} chars]"
    return out


def _extract_prompt_text(body: bytes) -> str:
    """
    从 Anthropic Messages API 请求体里提取最后一条 user message 的纯文本。
    用于日志脱敏。失败/无 user message 时返回空串。
    """
    if not body:
        return ""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    user_msg = None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_msg = m
            break
    if user_msg is None:
        return ""
    content = user_msg.get("content")
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    return "\n".join(text_parts)


# ── ~provider reset 检测（provider 级 fallback，2026-06-16）──────

_PROVIDER_RESET_RE = re.compile(
    r"(?:^|\s)~(?:provider|prov)\s+(?:reset|clear|default|auto|off)",
    re.IGNORECASE,
)


def _detect_provider_reset(body: bytes) -> bool:
    """检测用户 prompt 中是否包含 ~provider reset 指令。"""
    prompt_text = _extract_prompt_text(body)
    if not prompt_text:
        return False
    return bool(_PROVIDER_RESET_RE.search(prompt_text))


def resolve_model_routing(model_name: str) -> tuple[str, str, str, str, str, str, str] | None:
    """
    搜索 STAGE_CONFIG 查找 model_name 对应的路由参数。

    返回 (base_url, model, api_key_env, protocol,
          fb_base_url, fb_model, fb_api_key_env, fb_protocol)
    或 None（未找到该 model 的配置）。
    """
    # 搜索所有配置，找到 model_name 作为 primary 或 fallback 的条目
    for cfg in STAGE_CONFIG.values():
        if cfg["model"] == model_name:
            return (
                cfg["base_url"], cfg["model"], cfg["api_key_env"], cfg["protocol"],
                cfg["fb_base_url"], cfg["fb_model"], cfg["fb_api_key_env"], cfg["fb_protocol"],
            )
    # 也可作为 fallback model 匹配（用户可能想直接用备选模型）
    for cfg in STAGE_CONFIG.values():
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
    读取当前 session 的 sticky fallback provider 名。

    多 session 并发修复（2026-06-14）：通过 _active_stage_path() 解析 session。
    格式演进：
      - v3（2026-06-16）：fallback_<sid> 是 JSON `{"provider": ..., "failed_at": ts,
        "expire_ts": ts}`。TTL 到期自动 unlink 并返回 None。
      - v2：content 是 provider 名（"minimax"/"deepseek"）→ 直接返回
      - v1：content 是 model 名（"deepseek-v4-flash"）→ 通过 MODEL_TO_PROVIDER
        自动转换为 provider 名（视为过期，会自动清除）

    返回 None 表示"无 sticky fallback"——正常走主模型路由。
    """
    p = _active_stage_path()
    if not p:
        return None
    fb_path = _fallback_file_path(p)
    content = _read_stage_file(fb_path)
    if not content:
        return None

    # ── v3 JSON 格式 + TTL 校验 ──
    if content.startswith("{"):
        try:
            data = json.loads(content)
            provider = data.get("provider")
            expire_ts = int(data.get("expire_ts", 0))
            now = int(time.time())
            if not provider or provider not in KNOWN_PROVIDER_NAMES:
                log.warning(f"fallback_<sid> JSON 内容 provider 无效: {data!r}，清除")
                _safe_unlink(fb_path)
                return None
            if expire_ts and now >= expire_ts:
                log.info(
                    f"sticky fallback 已过期 (provider={provider}, "
                    f"expire_ts={expire_ts}, now={now})，清除并回到主 provider 路由"
                )
                _safe_unlink(fb_path)
                return None
            return provider
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            log.warning(f"fallback_<sid> JSON 解析失败（视为过期清除）: {e}")
            _safe_unlink(fb_path)
            return None

    # ── v2 旧格式：provider 名 ──
    if content in KNOWN_PROVIDER_NAMES:
        log.debug(f"fallback_<sid> 旧格式（v2 provider={content}），"
                  f"将由下次 write 自动升级到 v3")
        return content

    # ── v1 旧格式：model 名 → 映射到 provider（视为过期，自动清除）──
    prov = MODEL_TO_PROVIDER.get(content)
    if prov:
        log.info(
            f"fallback_<sid> 旧格式（v1 model={content}）→ 自动映射到 provider={prov}，"
            f"并清除旧文件（避免卡住 session）"
        )
        _safe_unlink(fb_path)
        return prov

    # ── 无法识别：视为损坏，清除 ──
    log.warning(f"fallback_<sid> 内容无法识别: {content!r}，清除")
    _safe_unlink(fb_path)
    return None


def write_fallback(provider: str) -> None:
    """写入 sticky fallback provider 名到 fallback_<sid>（向后兼容 thin wrapper）。

    推荐使用 try_write_fallback() 以获知"是否是首个写入者"。
    本函数保留是为了其他调用点（如 stage_detector）无需改签名。
    """
    try_write_fallback(provider)


def try_write_fallback(provider: str) -> bool:
    """原子写入 sticky fallback（O_CREAT|O_EXCL）。

    并发 N 个请求首次失败时，仅首个调用会成功创建文件并返回 True；
    其余返回 False。调用方据此决定是否执行 fb retry——避免并发放大
    对替代 provider 的瞬时 N 倍流量冲击。

    Args:
        provider: 失败的 provider 名（如 "minimax"）。

    Returns:
        True  — 当前进程/线程是首个写入者，应执行 fb retry
        False — sticky 已被其他并发请求写入，或写入失败；跳过 fb retry
    """
    p = _active_stage_path()
    if not p:
        return False
    fb_path = _fallback_file_path(p)
    try:
        fb_path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        payload = json.dumps({
            "provider": provider,
            "failed_at": now,
            "expire_ts": now + STICKY_TTL_SECONDS,
        }, ensure_ascii=False)
        # O_CREAT|O_EXCL：原子创建，失败 → FileExistsError
        fd = os.open(str(fb_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        log.info(
            f"sticky provider fallback 已激活: {provider} 不可用，"
            f"TTL={STICKY_TTL_SECONDS}s（到 {now + STICKY_TTL_SECONDS}），"
            f"后续请求将路由到替代 provider"
        )
        return True
    except FileExistsError:
        # 并发竞争：其他请求已写入
        log.debug(f"sticky fallback 已被其他请求写入（O_EXCL 失败），跳过 fb retry")
        return False
    except Exception as e:
        log.error(f"写入 fallback_<sid> 失败: {e}")
        return False


def _safe_unlink(path: Path) -> None:
    """静默 unlink（缺失文件不报错）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        log.debug(f"unlink {path} 失败（已忽略）: {e}")


def clear_fallback() -> None:
    """清除当前 session 的 sticky fallback 文件。

    用户执行 ~provider reset / ~model reset 时调用：主 provider（如 minimax）
    网络恢复后，用户手动解除 sticky，后续请求回到正常 stage 路由。
    多 session 并发安全（通过 _active_stage_path() 解析当前 session）。

    注意：reset 命令现在调用的是 clear_fallback_all()（全局清），
    本函数保留供 stage_detector / 单 session 清理场景使用。
    """
    p = _active_stage_path()
    if p:
        try:
            fb_path = _fallback_file_path(p)
            if fb_path.exists():
                fb_path.unlink()
                log.info("sticky fallback 已清除: ~model reset，后续请求回到正常路由")
            else:
                log.debug("sticky fallback 文件不存在，无需清除")
        except Exception as e:
            log.error(f"清除 fallback_<sid> 失败: {e}")


def clear_fallback_all() -> int:
    """~model reset 全局生效：清除所有 session 的 sticky fallback 文件。

    背景（2026-06-16 修复）：
      原实现 clear_fallback() 只清当前 session 的 fallback_<sid>。
      但 sticky 触发条件是「主模型 API 失败」——这是**进程/网络级问题**，
      不分 session。多 session 并发时所有 session 都会被同一波失败触发 sticky，
      用户执行 ~model reset 时只清一个 session 显然不够。

    实现：
      1. 主路径：从 state_index.json 读所有活跃 session 入口
         （每个 entry 含 session_id + project_root）→ 派生 fallback_<sid> 删除。
      2. 兜底：再扫一遍每个 project_root/.claude/ 目录里的 fallback_* glob，
         防止 state_index 缺失/损坏时漏掉。

    Returns:
        实际删除的 fallback_<sid> 文件数。
    """
    removed = 0
    seen_claude_dirs: set[Path] = set()

    # ── 主路径：state_index.json → 所有活跃 session ──
    all_entries = _read_state_index_all()
    for path_key, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        sid = entry.get("session_id")
        if not sid:
            continue
        try:
            claude_dir = Path(path_key) / ".claude"
        except (TypeError, ValueError):
            continue
        seen_claude_dirs.add(claude_dir)
        fb_path = claude_dir / f"fallback_{sid}"
        try:
            if fb_path.exists():
                fb_path.unlink()
                removed += 1
                log.info(f"  - 已清除: {fb_path}")
        except Exception as e:
            log.error(f"清除 {fb_path} 失败: {e}")

    # ── 兜底：扫已知 .claude 目录里所有 fallback_*（防止 state_index 漏报）──
    for claude_dir in seen_claude_dirs:
        if not claude_dir.is_dir():
            continue
        try:
            for fb_path in claude_dir.glob("fallback_*"):
                if not fb_path.is_file():
                    continue
                try:
                    fb_path.unlink()
                    removed += 1
                    log.info(f"  - 已清除（兜底扫描）: {fb_path}")
                except Exception as e:
                    log.error(f"清除 {fb_path} 失败: {e}")
        except Exception as e:
            log.debug(f"扫描 {claude_dir} 失败: {e}")

    return removed


# ── Pattern / Complexity / Batch / State-Index 读取（设计文档 §6.2-6.4 / §13）──

def _pattern_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "pattern_", 1))


def _complexity_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "complexity_", 1))


def _batch_file_path(stage_file: Path) -> Path:
    return stage_file.with_name(stage_file.name.replace("stage_", "batch_", 1))


def _active_stage_path() -> Path | None:
    """Resolve the active session's stage file path（multi-session aware）。

    设计文档 §13 + 多 session 并发修复（2026-06-14）：
    - 主路径：state_index.json → last_active 最新者 → 派生 stage_<sid> 路径
      （多 session 并发时每个请求独立解析，不再依赖全局 active_session 指针互踩）
    - 回退：ACTIVE_SESSION_FILE 指针（state_index 为空/损坏时的兼容路径）
    - stage 文件不存在则返回 None

    2026-06-17 Per-Session 路由绑定：
    - 优先使用 thread-local 缓存（do_POST 开头从 model 后缀解析并设置）。
      有此缓存时跳过 state_index.json 扫描，彻底消除多 session 竞态。
    """
    # ★ Per-Session 路由绑定：优先 thread-local 缓存
    cached = getattr(_routing_state, 'stage_path', None)
    if cached is not None:
        return cached

    # 主路径：state_index.json → 最新活跃 session
    all_entries = _read_state_index_all()
    if all_entries:
        best_ts = 0
        best_project_root = ""
        best_sid = ""
        for path_key, entry in all_entries.items():
            if not isinstance(entry, dict):
                continue
            ts = entry.get("last_active", 0)
            sid = entry.get("session_id", "")
            if not sid:
                continue
            if ts > best_ts:
                best_ts = ts
                best_project_root = path_key
                best_sid = sid
        if best_sid:
            stage_path = Path(best_project_root) / ".claude" / f"stage_{best_sid}"
            if stage_path.exists():
                return stage_path
            # stage 文件尚未创建（新 session 的 Hook 还没写完）→ 回退
            log.debug(
                f"state_index 指向 {stage_path} 但文件不存在，回退到 active_session 指针"
            )

    # 回退：active_session 指针（兼容 state_index 缺失/损坏的场景）
    try:
        ap = ACTIVE_SESSION_FILE.read_text().strip()
        if ap:
            p = Path(ap)
            if p.exists():
                return p
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


# ── 分类策略（2026-06-16 简化：仅在 UserPromptSubmit 时分类一次）────────────
#
# 旧策略（已弃用，保留代码以备回滚——见下方被注释的 _increment_and_should_classify
# 和 do_POST 中的 per-api-classify 块）：
#   1. Hook (UserPromptSubmit) → 分类 + 写入 stage/pattern/complexity + 重置 counter=0
#   2. Proxy (per API request)  → counter++；若 counter >= interval → 重新分类
#   3. 重新分类结果立即更新 stage/pattern/complexity 文件
#   目的：感知复杂任务中途的阶段跳变（plan → implement → audit）。
#
# 新策略（2026-06-16 起生效）：
#   仅在 UserPromptSubmit Hook 触发时分类一次，写入 stage/pattern/complexity 文件。
#   Proxy 只读取不再重分类——避免了 per-API-request 频繁触发 LLM 调用造成的
#   额外延迟 / cost / 误判（中途跳变在实际使用中带来的收益低于其代价）。
#   如需重新分类，用户提交下一条 prompt 时 Hook 会自动跑一次。
#
# 计数文件: <project_root>/.claude/reqcnt_<sid>.json
#   文件结构保留，但 proxy 不再自增；Hook 在 UserPromptSubmit 后会调用
#   reset_reqcnt() 把 counter 重置为 0（接口保留，避免破坏未来切换）。
# 间隔可通过 STAGE_ROUTER_RECLASSIFY_INTERVAL 环境变量配置，默认 3
#   （当前已无效，仅为兼容旧 hook / 测试代码保留）。

RECLASSIFY_INTERVAL = int(os.environ.get(
    "STAGE_ROUTER_RECLASSIFY_INTERVAL",
    str(RECLASSIFY_INTERVAL),
))


def _reqcnt_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 reqcnt_<sid> 路径。"""
    return stage_file.with_name(stage_file.name.replace("stage_", "reqcnt_", 1))


def _read_reqcnt_raw() -> dict:
    """读取当前 session 的 API 请求计数器（原始值）。"""
    p = _active_stage_path()
    if not p:
        return {"count": 0, "interval": RECLASSIFY_INTERVAL}
    cnt_path = _reqcnt_file_path(p)
    try:
        if cnt_path.exists():
            content = cnt_path.read_text().strip()
            if content:
                data = json.loads(content)
                data.setdefault("interval", RECLASSIFY_INTERVAL)
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"count": 0, "interval": RECLASSIFY_INTERVAL}


def _write_reqcnt(data: dict) -> None:
    """写入当前 session 的 API 请求计数器。"""
    p = _active_stage_path()
    if not p:
        return
    cnt_path = _reqcnt_file_path(p)
    try:
        cnt_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写 .tmp 再 rename
        tmp = cnt_path.with_suffix(cnt_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, cnt_path)
    except OSError:
        pass


def _increment_and_should_classify() -> bool:
    """递增请求计数器，返回是否需要触发重新分类（2026-06-16 起已禁用）。

    旧行为：每次 proxy 接收到 CC API 请求时调用；计数器到达阈值时返回 True，
    触发 LLM 重新分类。详见模块顶部"分类策略"注释。

    新行为（2026-06-16 起）：始终返回 False，不再自增计数器、不再触发 per-API
    重新分类。仅保留为接口占位，便于未来切换回旧策略或供测试使用。

    Returns:
        永远 False（不再触发 per-API 分类）。
    """
    # === 旧逻辑（2026-06-16 弃用，保留以便回滚）===
    # data = _read_reqcnt_raw()
    # interval = int(data.get("interval", RECLASSIFY_INTERVAL))
    # if interval <= 0:
    #     return False  # 间隔为 0 表示禁用 per-request 分类
    # count = int(data.get("count", 0)) + 1
    # data["count"] = count
    # if count >= interval:
    #     data["count"] = 0  # 重置
    #     _write_reqcnt(data)
    #     return True
    # _write_reqcnt(data)
    # return False
    # === 旧逻辑结束 ===
    return False


def reset_reqcnt() -> None:
    """重置请求计数器为 0（由 Hook 在 UserPromptSubmit 分类后调用）。

    2026-06-16 简化后：Proxy 端不再自增 / 不再触发 per-API 分类，本函数仍保留
    给 Hook 端调用——Hook 在每次 UserPromptSubmit 分类完成后调用一次，把
    counter 重置为 0，保持旧文件的初始状态不变，便于未来切回旧策略时
    counter 从 0 开始自增。接口签名 / 文件结构都不变。"""
    p = _active_stage_path()
    if not p:
        return
    cnt_path = _reqcnt_file_path(p)
    try:
        cnt_path.parent.mkdir(parents=True, exist_ok=True)
        cnt_path.write_text(json.dumps(
            {"count": 0, "interval": RECLASSIFY_INTERVAL},
            ensure_ascii=False,
        ))
    except OSError:
        pass


def _extract_classification_context(body: bytes) -> str:
    """从请求体中提取用于 LLM 分类的上下文。

    返回包含以下内容的拼接文本（优先保留头部和尾部，中段截断）：
    - 系统提示（前 600 字符）
    - 最后一条 user message（完整）
    - 倒数 2 条 assistant message 的摘要（各前 300 字符）

    对超长 prompt 做截断：头 60% + 尾 40%，确保分类器不会因超长输入超时。
    拼接后整体最前加上截断说明，让 LLM 知道这是不完整的上下文片段。
    """
    if not body:
        return ""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""

    parts: list[str] = []
    max_total = int(os.environ.get("STAGE_ROUTER_CLASSIFY_MAX_CHARS", "6000"))

    # 系统提示
    system = data.get("system")
    if isinstance(system, str) and system.strip():
        parts.append("[SYSTEM]\n" + system.strip()[:600])
    elif isinstance(system, list):
        sys_texts = []
        for s in system:
            if isinstance(s, dict) and s.get("type") == "text":
                sys_texts.append(s.get("text", ""))
        sys_combined = "\n".join(sys_texts).strip()
        if sys_combined:
            parts.append("[SYSTEM]\n" + sys_combined[:600])

    # 消息历史
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return "\n".join(parts)

    # 找到最后一条 user message 和最近的 assistant messages
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx >= 0:
        user_msg = messages[last_user_idx]
        user_text = _extract_text_from_content(user_msg.get("content"))
        if user_text:
            parts.append("[LAST_USER]\n" + user_text)

    # 最近的 assistant 响应摘要
    assistant_texts: list[str] = []
    for i in range(last_user_idx - 1, max(last_user_idx - 4, -1), -1):
        if i < 0:
            break
        if isinstance(messages[i], dict) and messages[i].get("role") == "assistant":
            text = _extract_text_from_content(messages[i].get("content"))
            if text:
                assistant_texts.insert(0, text[:300])
    if assistant_texts:
        parts.append("[RECENT_ASSISTANT]\n" + "\n---\n".join(assistant_texts))

    combined = "\n\n".join(parts)

    # ── 截断说明（让 LLM 知道这是不完整的上下文片段）──
    # 放在最前面，因为 LLM 在阅读后续截断内容时如果先看到"截断"说明，
    # 会自发加上安全边界（"可能还有更多上下文没看到，不过推断应是..."）。
    truncated_notice = (
        "[注意：以下上下文是从 Claude Code 当前 API 请求中提取的片段，"
        "并非完整对话历史。\n"
        "SYSTEM 提示截取前 600 字符，RECENT_ASSISTANT 回复各摘要前 300 字符，"
        "LAST_USER 消息完整保留。\n"
        "此片段用于进度分类 —— "
        "请据此推断当前所处阶段(stage)、任务类型(pattern)和复杂度(complexity)。]"
    )

    # 超长截断：头 60% + 尾 40%
    if len(combined) > max_total:
        head_chars = int(max_total * 0.6)
        tail_chars = max_total - head_chars
        truncated_chars = len(combined) - head_chars - tail_chars
        combined = (
            combined[:head_chars]
            + f"\n\n... [已截断 {truncated_chars} 字符] ...\n\n"
            + combined[-tail_chars:]
        )

    return truncated_notice + "\n\n" + combined


def _extract_text_from_content(content) -> str:
    """从 Anthropic content 字段提取纯文本。"""
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    return "\n".join(text_parts)


def _write_stage_from_proxy(stage: str) -> None:
    """Proxy 端写入 stage（覆盖当前 session 的 stage_<sid>）。"""
    p = _active_stage_path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(stage + "\n")
        # 同步更新 state_index.json（使用 proxy.py 自身的 STATE_INDEX_FILE）
        try:
            from stage_detector import _update_state_index  # noqa: E402
            project_root = str(_find_project_root_for_stage_path(p))
            sid = _extract_session_id_from_stage_path(p)
            _update_state_index(project_root, sid, p)
        except Exception:
            pass
    except OSError:
        pass


def _write_pattern_from_proxy(prediction: str, confidence: float) -> None:
    """Proxy 端写入 task pattern。"""
    p = _active_stage_path()
    if not p:
        return
    try:
        pp = _pattern_file_path(p)
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(json.dumps({
            "prediction": prediction,
            "confidence": confidence,
            "ts": time.time(),
        }, ensure_ascii=False))
    except OSError:
        pass


def _write_complexity_from_proxy(score: int, label: str, confidence: float,
                                  source: str = "proxy") -> None:
    """Proxy 端写入 complexity 评估。"""
    p = _active_stage_path()
    if not p:
        return
    try:
        cp = _complexity_file_path(p)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({
            "score": score,
            "label": label,
            "confidence": confidence,
            "source": source,
            "ts": time.time(),
        }, ensure_ascii=False))
    except OSError:
        pass


# ── Metrics / Trace（设计文档 §6.8 / §15）────────────────────────────────────
# 每次路由决策写一条 JSONL 到 /tmp/stage_metrics.jsonl，供 /metrics /trace 查询。
METRICS_LOG_FILE = Path("/tmp/stage_metrics.jsonl")
METRICS_MAX_RECORDS = 500  # 环形缓冲：最多保留最近 500 条


def _read_workflow_state_safe() -> dict | None:
    """v1.3: workflow_orchestrator 已删除，保留空壳供 /health /trace 兼容。"""
    return None


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
    """汇总指标（设计文档 §15）：
    - 全局统计：模型调用量 / 强模型占比 / 平均 token / 复杂任务成功率 / 返工率
    - 维度聚合：项目 / 会话 / pattern
    """
    from collections import Counter
    model_counter: Counter = Counter()
    pattern_counter: Counter = Counter()
    complexity_counter: Counter = Counter()
    routing_source_counter: Counter = Counter()
    project_counter: Counter = Counter()
    session_counter: Counter = Counter()
    status_counter: Counter = Counter()
    strong_call_count = 0           # target_model_is_strong=True 的请求数
    complex_total = 0               # 标记为 complex 的请求数
    complex_success = 0             # 其中 status 2xx 的请求数
    retry_total = 0                 # retry_count > 0 的请求数
    token_sum = 0
    token_count = 0
    fallback_total = 0
    for r in records:
        if r.get("target_model"):
            model_counter[r["target_model"]] += 1
        if r.get("pattern"):
            pattern_counter[r["pattern"]] += 1
        if r.get("complexity_label"):
            complexity_counter[r["complexity_label"]] += 1
        if r.get("routing_source"):
            routing_source_counter[r["routing_source"]] += 1
        if r.get("project_root"):
            project_counter[r["project_root"]] += 1
        if r.get("session_id"):
            session_counter[r["session_id"]] += 1
        if r.get("used_fallback"):
            fallback_total += 1
        if r.get("target_model_is_strong"):
            strong_call_count += 1
        if r.get("complexity_label") == "complex":
            complex_total += 1
            if 200 <= r.get("status", 0) < 300:
                complex_success += 1
        if (r.get("retry_count") or 0) > 0:
            retry_total += 1
        if r.get("token_estimate") is not None:
            token_sum += int(r["token_estimate"])
            token_count += 1
        s = r.get("status", 0)
        status_counter[s] += 1

    total = len(records)
    return {
        # ── 基础计数（兼容旧版）──
        "total":                total,
        "fallback_total":       fallback_total,
        "by_model":             dict(model_counter),
        "by_pattern":           dict(pattern_counter),
        "by_complexity":        dict(complexity_counter),
        "by_routing_source":    dict(routing_source_counter),
        "by_status":            dict(status_counter),
        # ── §15 D15-1 必须指标 ──
        # 强模型占比：target_model == STRONG_MODEL（deepseek-v4-pro）的请求占比
        "strong_model_ratio":   round(strong_call_count / total, 4) if total else 0.0,
        "strong_call_count":    strong_call_count,
        # 复杂任务成功率：complex 任务中 2xx 占比
        "complex_task_success_rate": round(complex_success / complex_total, 4)
                                    if complex_total else 0.0,
        "complex_total":        complex_total,
        "complex_success":      complex_success,
        # 返工率：retry_count > 0 的请求占比（§18 D18-3-1 retry 落地后才有非零值）
        "retry_rate":           round(retry_total / total, 4) if total else 0.0,
        "retry_total":          retry_total,
        # 平均每任务 token
        "avg_tokens_per_task":  round(token_sum / token_count, 1) if token_count else 0,
        # ── §15 D15-2 维度聚合 ──
        "by_project":           dict(project_counter),
        "by_session":           dict(session_counter),
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


def _inject_sid_to_response(resp_body: bytes, sid: str) -> bytes:
    """在响应体的 model 字段值后追加 _s<sid> 后缀。

    与 _rewrite_response_model() 配合：先执行 model 名回写（_rewrite），再执行 sid
    注入（本函数）。仅当 model 字段尚未带 sid 后缀时才注入（幂等）。
    """
    if not resp_body or not sid:
        return resp_body
    try:
        data = json.loads(resp_body)
        if isinstance(data, dict) and "model" in data:
            if not _SID_SUFFIX_RE.search(data["model"]):
                data["model"] = f"{data['model']}_s{sid}"
                return json.dumps(data).encode()
    except (json.JSONDecodeError, TypeError):
        pass
    return resp_body


# ── Thinking block 剥离（非原生 Anthropic 端点）────────────────────────────────

_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def _strip_thinking_blocks(body: dict) -> tuple[int, int]:
    """
    从请求体的所有 messages[].content[] 里删除 thinking / redacted_thinking block。

    策略（2026-06 修正）：
      非 Anthropic 端点（DeepSeek, MiniMax）看到历史中的 type: "thinking" block
      会隐式进入 thinking mode，但顶层 thinking 参数已被代理移除 → 400 错误。
      "保留 thinking block 原样透传是安全的"已被运行证伪；正确做法是直接删除——
      DeepSeek 看不到 thinking block 就不会进入 thinking mode。

    content 为字符串时跳过（无 block 可剥）;
    content 为列表时过滤掉这两个 type 的 dict;
    过滤后保留空列表（避免删整条 message 破坏 tool_result 顺序）。

    Returns:
        (thinking_stripped, redacted_stripped) — 计数供日志使用。
      参数 body 被原地修改。
    """
    _stripped = _redacted = 0
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        before = len(content)
        filtered: list = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES:
                if block.get("type") == "thinking":
                    _stripped += 1
                else:
                    _redacted += 1
            else:
                filtered.append(block)
        if len(filtered) != before:
            msg["content"] = filtered
    return _stripped, _redacted


def _strip_thinking_from_response(resp_body: bytes) -> bytes:
    """
    从上游 Anthropic Messages 响应体顶层 content[] 中删除 thinking block。

    非原生端点（DeepSeek/MiniMax）偶尔在 Anthropic 协议响应中返回 thinking block。
    若不剥离，CC 会将其存入会话历史——下一轮请求携带 thinking block → 400 死循环。
    解析失败或 content 不是 list 时原样返回。
    """
    try:
        data = json.loads(resp_body)
        if not isinstance(data, dict):
            return resp_body
        content = data.get("content")
        if not isinstance(content, list):
            return resp_body
        before = len(content)
        data["content"] = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") in _THINKING_BLOCK_TYPES)
        ]
        if len(data["content"]) != before:
            return json.dumps(data).encode()
    except (json.JSONDecodeError, TypeError):
        return resp_body
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
    timeout: float = 300,
) -> tuple[int, dict, bytes]:
    """
    将 CC 发来的 Anthropic Messages 请求转发到目标 provider。

    protocol:
      "anthropic" (默认) — 上游是 Anthropic Messages 兼容端点，透明转发：
                           model 改写 + x-api-key 注入 + 透传 /v1/messages，
                           不做请求/响应格式转换（端到端都是 Anthropic 协议）。
      "openai"           — 上游是 OpenAI Chat Completions 兼容（如硅基流动）：
                           自动做 Anthropic ↔ OpenAI 协议转换。

    timeout（2026-06-16 引入）：
      单次 HTTP 请求超时秒数，默认 300s 保持向后兼容。
      健康探测（health_checker.py）会传 5s 实现快速失败。

    注意：协议判断基于 STAGE_MODELS 中显式的 `protocol` 字段，
    不要再用 URL 启发式（如 "deepseek" in target_base）判断——
    DeepSeek 的 Anthropic SDK 端点（https://api.deepseek.com/anthropic）
    就是 Anthropic 协议，必须走 anthropic 分支。
    """
    # dry_run 模式提前返回（不检查 API key，不发起 HTTP 请求）
    if dry_run:
        log.info(f"[DRY-RUN] 将转发到: {target_model}")
        mock = {"content": [{"type": "text", "text": f"[dry-run] routed to {target_model}"}]}
        return 200, {"content-type": "application/json"}, json.dumps(mock).encode()

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
    # 策略（2026-06 修正）：
    #   1. 删除顶层 thinking 参数（阻止上游进入 extended thinking 模式）
    #   2. 删除消息历史中所有 type: "thinking" / "redacted_thinking" content block
    #
    # 根因：非 Anthropic 端点（DeepSeek, MiniMax）看到历史中的 thinking block
    #   会隐式进入 thinking mode，但顶层 thinking 参数已被移除 → 400 错误。
    #   "保留 thinking block 原样透传是安全的"已被运行证伪。
    #   详见 _strip_thinking_blocks() 和 _strip_thinking_from_response() 注释。
    #
    # 注意：原生 Anthropic 端点（api.anthropic.com）的 signature 校验是真实的——
    #   删除 thinking block 会破坏签名验证导致 400，因此不做任何降级。
    if not _is_native_anthropic(target_base):
        if "thinking" in body_json:
            del body_json["thinking"]
        _stripped, _redacted = _strip_thinking_blocks(body_json)
        if _stripped or _redacted:
            log.info(
                f"thinking降级: 剥离 thinking={_stripped} redacted={_redacted} "
                f"目标={target_model} provider={target_base}"
            )

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = dict(resp.headers)
            if protocol == "openai":
                resp_body = _from_openai_response(resp_body)
            elif protocol == "anthropic" and not _is_native_anthropic(target_base):
                # 防御：剥离上游响应中的 thinking block（防止进入 CC 会话历史死循环）
                resp_body = _strip_thinking_from_response(resp_body)
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

# ── 启动校验 ───────────────────────────────────────────────────────────────────────

def _check_required_keys() -> list[str]:
    """扫描 STAGE_MODELS，收集所有需要的 API key 环境变量名，报告缺失项。"""
    needed = {entry[2] for entry in STAGE_MODELS.values()}
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


# ── 主模型重试参数（2026-06-17 引入）───────────────────────────────────────────
#
# 设计要点：
#   - 接口错误/超时统一归为"主模型不可用"，先用 3 次重试吸收瞬时抖动；
#   - 3 次都失败 → 才认为"provider 真的挂了"，启动 fallback provider 流程；
#   - 退避固定 1s × N：用户明确要求"不考虑 timeout，时间太长"，固定等待最稳；
#   - 400/404/422 这类 client error 不重试（重试 body 不会变好，client bug 不归
#     provider 背锅），由调用方决定是否 fallback（当前实现是直接透传，不走 fb）；
#   - 不重试 fallback 模型：fallback 是兜底，1 次失败就透传 CC，避免对替代
#     provider 的 N 倍流量放大。
PRIMARY_MODEL_RETRY_ATTEMPTS = 3
PRIMARY_MODEL_RETRY_BACKOFF_SECONDS = 1.0


def _call_with_retry(
    *,
    method: str,
    path: str,
    headers: dict,
    body: bytes,
    target_base: str,
    target_model: str,
    api_key_env: str,
    protocol: str,
    dry_run: bool,
    sleep_fn=time.sleep,
) -> tuple[int, dict, bytes, int]:
    """对主模型做固定 1s 退避的有限重试。

    行为契约：
      1. 调 forward_request 一次；
      2. 若返回 _is_retriable(status)=True → sleep 1s 重试；
      3. 最多重试 PRIMARY_MODEL_RETRY_ATTEMPTS 次（首次 + 2 次重试 = 共 3 次）；
      4. 任何一次成功（status 非 retriable）→ 立即返回；
      5. 3 次都 retriable → 返回最后一次 status，调用方据此走 fallback 流程。

    Args:
        sleep_fn: 测试用注入点（默认 time.sleep）。单元测试用 monkeypatch 跳过实际等待。

    Returns:
        (status, resp_headers, resp_body, attempts_used)
        - attempts_used: 实际调用次数（1~3）
    """
    last_status = 0
    last_headers: dict = {}
    last_body: bytes = b""
    for attempt in range(1, PRIMARY_MODEL_RETRY_ATTEMPTS + 1):
        status, h, b = forward_request(
            method=method,
            path=path,
            headers=headers,
            body=body,
            target_base=target_base,
            target_model=target_model,
            api_key_env=api_key_env,
            protocol=protocol,
            dry_run=dry_run,
        )
        last_status, last_headers, last_body = status, h, b
        if not _is_retriable(status):
            if attempt > 1:
                log.info(
                    f"主模型 {target_model} 第 {attempt}/{PRIMARY_MODEL_RETRY_ATTEMPTS} "
                    f"次重试成功（status={status}）"
                )
            return status, h, b, attempt
        if attempt < PRIMARY_MODEL_RETRY_ATTEMPTS:
            log.warning(
                f"主模型 {target_model} 返回 {status}（attempt {attempt}/"
                f"{PRIMARY_MODEL_RETRY_ATTEMPTS}），{PRIMARY_MODEL_RETRY_BACKOFF_SECONDS}s "
                f"后重试"
            )
            sleep_fn(PRIMARY_MODEL_RETRY_BACKOFF_SECONDS)
    log.error(
        f"主模型 {target_model} 经 {PRIMARY_MODEL_RETRY_ATTEMPTS} 次重试仍失败，"
        f"last status={last_status}，启动 fallback provider 流程"
    )
    return last_status, last_headers, last_body, PRIMARY_MODEL_RETRY_ATTEMPTS


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
        prompt_model_override, prompt_is_reset, prompt_unknown_alias = _extract_prompt_model_override(body)

        # 设计文档 §12 D12-3：显式 `~model <name>` 但 alias 未识别时必须警告用户，
        # 列出合法 alias / 规范名。否则用户以为生效、实际是静默失效。
        if prompt_unknown_alias:
            log.warning(
                f"~model {prompt_unknown_alias!r} 未识别（合法 alias 示例: "
                f"ds-v4-pro / mm3 / sonnet / opus；规范名: "
                f"deepseek-v4-pro / MiniMax-M3 / claude-sonnet-4-6 / claude-opus-4-8）"
            )

        # ── 路由决策：prompt_model_override > model_override(file) > op > stage > default ──
        # 1) prompt 内嵌 ~model 优先（消除一回合延迟）。
        # 2026-06-16 行为变更：~model 改为「本请求一次性」覆盖，**不再写回** model_<sid> 文件。
        # 旧逻辑：写盘让 stage_detector 下一回合保持一致 → 整个 session 都被钉死。
        # 新逻辑：只在本请求使用 prompt_model_override，下一请求（无 ~model）回到自动路由。
        # 显式初始化 model_override=None，避免下面 elif/1001 行 if 分支里
        # UnboundLocalError（命中分支才会赋值）。
        model_override = None
        if prompt_model_override:
            log.info(
                f"prompt ~model 命中: {prompt_model_override!r} "
                f"（一次性覆盖，仅当前请求生效，不写 model_<sid>）"
            )
            model_override = prompt_model_override
        elif prompt_is_reset:
            # ~model reset 清除 sticky fallback，让后续请求回到正常 stage 路由。
            # ~model 虽然已改为一次性的 model_override（不写 model_<sid> 持久文件），
            # 但 sticky fallback 文件（fallback_<sid>）是持久存在的独立机制——
            # 主模型（如 MiniMax-M3）网络恢复后，用户通过 ~model reset 手动解除 sticky。
            #
            # 2026-06-16 行为变更：reset 现在对**所有 session** 生效。
            # 原因：sticky 触发条件是「主模型 API 失败」——进程/网络级问题，
            # 不分 session。多 session 并发时所有 session 都会被同一波失败触发 sticky，
            # 用户执行 ~model reset 时只清当前 session 显然不够，必须全局清。
            log.info("prompt ~model reset：全局清除所有 session 的 sticky fallback")
            n = clear_fallback_all()
            log.info(f"~model reset 已清除 {n} 个 sticky fallback 文件")

        # ── ~provider reset ──────────────────────────────────────────
        # provider 级 fallback（2026-06-16）：~provider reset 全局清除所有
        # session 的 sticky fallback，与 ~model reset 行为一致。
        if _detect_provider_reset(body):
            log.info("prompt ~provider reset：全局清除所有 session 的 sticky fallback")
            n = clear_fallback_all()
            log.info(f"~provider reset 已清除 {n} 个 sticky fallback 文件")

        # ── Per-API-Request 分类（2026-06-16 起已禁用，保留代码以备回滚）────
        # 旧逻辑：计数器在 UserPromptSubmit (Hook) 时重置为 0；本回合若计数器到达
        # 阈值，提取 prompt 上下文 → 调用 llm_classifier → 更新 stage/pattern/
        # complexity 文件 → 后续路由分支自动使用新分类结果。
        # 新逻辑：不再触发 per-API 重新分类，全部交给 Hook 在下次 UserPromptSubmit
        # 时跑一次；proxy 只读取已写入的 stage/pattern/complexity 文件。
        #
        # === 旧 per-api-classify 块（已注释，需要时取消注释即可恢复）===
        # _prx_ap_path = _active_stage_path()
        # if _prx_ap_path:
        #     _prx_sid = _extract_session_id_from_stage_path(_prx_ap_path)
        #     _prx_root = str(_find_project_root_for_stage_path(_prx_ap_path))
        # else:
        #     _prx_sid = _prx_root = None
        #
        # if _prx_sid and _prx_root and _increment_and_should_classify():
        #     _ctx = _extract_classification_context(body)
        #     if _ctx:
        #         log.info(
        #             f"[per-api-classify:session={_prx_sid}] "
        #             f"计数器到达阈值，触发 LLM 重新分类..."
        #         )
        #         try:
        #             _clf_result = _proxy_llm_classify(_ctx)
        #             _new_stage = _clf_result.get("stage", "default")
        #             _new_pattern = _clf_result.get("pattern", "feature")
        #             _new_score = _clf_result.get("complexity_score", 50)
        #             _new_label = _clf_result.get("complexity_label", "medium")
        #             _new_pconf = _clf_result.get("pattern_confidence", 0.5)
        #             _new_cconf = _clf_result.get("complexity_confidence", 0.5)
        #             _new_reason = _clf_result.get("reasoning", "")
        #             log.info(
        #                 f"[per-api-classify:session={_prx_sid}] "
        #                 f"结果: stage={_new_stage} pattern={_new_pattern} "
        #                 f"complexity={_new_label}({_new_score}) "
        #                 f"reason={_new_reason}"
        #             )
        #             # 写回 state 文件 —— 下一条请求的 read_stage() / read_pattern()
        #             # / read_complexity() 自动使用新分类结果
        #             _write_stage_from_proxy(_new_stage)
        #             _write_pattern_from_proxy(_new_pattern, _new_pconf)
        #             _write_complexity_from_proxy(
        #                 _new_score, _new_label, _new_cconf,
        #                 source="proxy_per_api",
        #             )
        #         except Exception as _clf_exc:
        #             log.warning(
        #                 f"[per-api-classify:session={_prx_sid}] "
        #                 f"LLM 分类失败（静默，保留现有分类）: {_clf_exc}"
        #             )
        # === 旧 per-api-classify 块结束 ===

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
            # ── Batch 强制流程覆盖（优先级 #2：设计文档 §5）──
            # ~batch 激活时直接跳到 PATTERN_CONFIG[template].default_flow[0]，
            # 绕过普通 stage 检测；同时把 PATTERN.primary_model 作为主模型来源。
            batch_template = batch.get("template") if batch else None
            if batch_template and batch_template in PATTERN_CONFIG:
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

            # ═══════════════════════════════════════════════════════════════
            # §16 核心原则：Complexity 覆盖 Stage（析取关系）
            #
            # Task Complexity 与 Task Stage 是析取关系——
            # Complexity 是主导方，覆盖 Stage 的模型决策：
            #   - complexity=simple  → 无论 stage 判为什么，用 provider 的
            #                          低成本模型（flash / M3），不再走 pro
            #   - complexity=medium  → 用 provider 的 medium-tier 模型
            #   - complexity=complex → 用 provider 的强模型（pro）
            #
            # 背景：STAGE_MODELS 只按 stage 选模型，不感知 complexity。
            #   如果 stage=decide，默认 model=pro，即使 complexity=simple
            #   也会被错误路由到 pro——浪费推理成本。
            #   此覆盖在 stage→model 解析后插入，确保 complexity 语义穿透。
            #
            # 优先级：~model 显式覆盖 > batch.primary_model >
            #          complexity 覆盖 > stage 默认模型
            # ═══════════════════════════════════════════════════════════════
            stage_model = model  # STAGE_MODELS 解析出的原始模型
            provider = MODEL_TO_PROVIDER.get(stage_model)
            if provider and complexity_label:
                complexity_model = PROVIDER_COMPLEXITY_MODELS.get(
                    provider, {}).get(complexity_label)
                if complexity_model and complexity_model != stage_model:
                    cfg = MODEL_TO_CONFIG.get(complexity_model)
                    if cfg:
                        base_url, model, key_env, protocol = cfg
                        routing_source += (
                            f" [complexity={complexity_label}→{model}"
                            f" (overrides stage={stage}→{stage_model})]"
                        )

            # v1.3: workflow_orchestrator 已删除，workflow dict 仅保留给日志行使用
            workflow = {
                "type":   "single",
                "steps":  ["execute"],
                "models": [model],
            }
        else:
            # model_override 路径无 workflow 编排（用户已显式指定）
            workflow = {
                "type":   "single",
                "steps":  ["execute"],
                "models": [model],
            }

        # ═══════════════════════════════════════════════════════════════════════
        # Provider 级 sticky fallback（2026-06-16）
        # ═══════════════════════════════════════════════════════════════════════
        # 旧策略（已移除）：先按 complexity 升级 fallback model → 再做 sticky swap。
        #   问题是 fallback 锁定在"具体 model"而非"provider"——一旦 sticky 到
        #   deepseek-v4-flash，整个 session 再复杂的任务也用 flash。
        #
        # 新策略：fallback_<sid> 存"失败的 provider 名"。
        #   当主模型的 provider == sticky provider 时，切换到替代 provider，
        #   并在替代 provider 内部按任务 complexity 动态选模型：
        #     - simple  → 对应 provider 的低成本模型（deepseek: flash）
        #     - medium/complex → 对应 provider 的强模型（deepseek: pro）
        #
        #   model_override / internal_req 路径跳过此切换。
        #   STRONG_MODEL / complexity-aware upgrade 旧代码不再需要——
        #   PROVIDER_COMPLEXITY_MODELS 已内建 complexity→model 映射。
        # ═══════════════════════════════════════════════════════════════════════
        internal_req = _is_internal_request(headers)
        # 标志位：本请求是否"实际切换到了备用模型"。
        # 用于 /metrics 的 used_fallback 严格判定（区分 4xx 非可重试错误）。
        fallback_invoked = False
        sticky_provider = (
            read_fallback()
            if (not model_override and not internal_req)
            else None
        )
        # 保存 session 级模型名（CC 能识别的原始模型名），用于响应体 model 字段回写
        session_model = model
        if sticky_provider:
            primary_provider = MODEL_TO_PROVIDER.get(model)
            if primary_provider == sticky_provider:
                # 主模型的 provider 已不可用 → 切换到替代 provider
                alt_provider = DEFAULT_FALLBACK_PROVIDER.get(
                    sticky_provider, "deepseek"
                )
                # 在替代 provider 内部按复杂度动态选模型
                provider_models = PROVIDER_COMPLEXITY_MODELS.get(alt_provider, {})
                chosen_model = provider_models.get(
                    complexity_label,
                    provider_models.get("medium", "deepseek-v4-pro"),
                )
                # 从 MODEL_TO_CONFIG 反查路由配置
                new_cfg = MODEL_TO_CONFIG.get(chosen_model)
                if new_cfg:
                    new_base, new_model, new_key, new_proto = new_cfg
                    # 交换：新主模型 = 替代 provider 的 complexity 模型，
                    #       新 fallback = 原主模型（retry 兜底）
                    (base_url, model, key_env, protocol,
                     fb_base, fb_model, fb_key, fb_proto) = (
                        new_base, new_model, new_key, new_proto,
                        base_url, session_model, key_env, protocol,
                    )
                    routing_source += (
                        f" [sticky-provider={sticky_provider}→{alt_provider},"
                        f" complexity={complexity_label}→{chosen_model}]"
                    )
                    fallback_invoked = True
                    log.info(
                        f"[{routing_source}] provider fallback 切换: "
                        f"{sticky_provider} 不可用 → {alt_provider} "
                        f"({complexity_label} → {chosen_model})"
                    )

        # 追踪本次请求实际路由到的模型（sticky swap / fallback retry 后可能改变），
        # 用于写入 model_router_state_<sid>.json 的 route_model 字段，
        # 供 statusline 第三行准确显示当前使用的模型。
        actual_route_model = model  # sticky swap 后的 model

        status, resp_headers, resp_body, primary_attempts = _call_with_retry(
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

        # 主模型重试 3 次仍失败 → 切换备用模型
        # 但内部服务请求（X-Stage-Router-Source）跳过此分支：
        # 用户的业务 5xx 是上游问题，不应触发模型 SDK 二次调用、
        # 也不应写入 sticky fallback（避免污染 CC 后续会话）。
        if _is_retriable(status) and fb_base and fb_model and not internal_req:
            log.warning(
                f"[{routing_source}] 主模型 {model} 返回 {status}，"
                f"切换到备用 {fb_model} [{fb_base}]"
            )
            fallback_invoked = True
            # ── 写入 sticky fallback：主 provider 已确认不可用 ──
            # 在发起 fallback 请求**之前**就写入 sticky，原因：
            #   1. 并发请求：避免多个并行请求都先尝试失败的主 provider
            #   2. fallback 也可能超时/5xx——若等 fallback 成功才写 sticky，
            #      两个 provider 同时出问题时 sticky 永不写入，导致无限循环：
            #      主→fail→备→fail→不写 sticky→主→fail→...
            #   3. 用户可通过 ~provider reset 或 ~model reset 随时清除
            #
            # 2026-06-16 原子写改造：try_write_fallback 用 O_CREAT|O_EXCL
            # 仅首个写入者返回 True；并发失败的请求**跳过本次 fb retry**，
            # 直接返回主模型错误（CC SDK 会触发新请求，新请求读 sticky 走
            # provider swap 路径），避免对替代 provider 的 N 倍流量放大。
            i_am_first_writer = False
            if not sticky_provider and not model_override:
                failed_provider = MODEL_TO_PROVIDER.get(session_model)
                if failed_provider:
                    i_am_first_writer = try_write_fallback(failed_provider)
                    if i_am_first_writer:
                        log.info(
                            f"[{routing_source}] 主 provider {failed_provider} "
                            f"不可用（status={status}），已原子写入 sticky fallback（TTL="
                            f"{STICKY_TTL_SECONDS}s），本请求执行 fb retry"
                        )
                    else:
                        log.debug(
                            f"[{routing_source}] sticky 已被其他并发请求写入，"
                            f"本请求跳过 fb retry，由 CC SDK 决定重试"
                        )
            if not i_am_first_writer and not sticky_provider and not model_override \
                    and failed_provider:
                # 并发写失败者：直接回写主模型的失败响应，不再向 fb 转发。
                # CC SDK 的重试会触发新请求，新请求读 sticky → 走 provider swap。
                try:
                    self.send_response(status)
                    self.send_header("Content-Type",
                                     resp_headers.get("content-type", "application/json"))
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
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
            # fallback retry 成功后，实际路由模型更新为 fb_model
            if not _is_retriable(status):
                actual_route_model = fb_model
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
            # ★ Per-Session 路由：注入 sid 后缀到响应 model 字段
            _sid = getattr(_routing_state, 'sid', None)
            if not _sid:
                _ap = _active_stage_path()
                if _ap:
                    _sid = _extract_session_id_from_stage_path(_ap)
            if _sid:
                resp_body = _inject_sid_to_response(resp_body, _sid)

        # ── 结构化指标落盘（设计文档 §15）──
        # 每条请求都写一条 JSONL 记录，含 pattern/complexity/score/confidence/
        # token_estimate/fallback_count，供 /metrics /trace 读取。
        try:
            # used_fallback 严格定义：本请求"实际使用或触发了备用模型"
            #   - sticky_provider 路径：进入时主模型已切换，记为 True
            #   - 主模型失败后切到 fb 模型：记为 True
            #   - 4xx 非可重试错误（400/404/422 等）：主备都没切到，记为 False
            #     之前的 `status >= 400` 会把 400/404/422 全算成"用了 fallback"，
            #     导致 /metrics 统计严重虚高。
            used_fallback = bool(sticky_provider) or fallback_invoked
            is_success = 200 <= status < 300

            # 设计文档 §15 D15-1：强模型标记。
            # session_model 是 CC 能识别的原始模型名（已经过 alias→canonical 解析），
            # 与 stage_config.STRONG_MODEL 字符串比对即可。
            target_model_is_strong = (session_model == STRONG_MODEL)

            # 设计文档 §15 D15-2：维度聚合（项目 / 会话）。
            # proxy 是无 stdin 的 HTTP 服务器，从 state_index.json 解析（2026-06-14
            # 多 session 并发修复）：不再读全局 active_session 指针。
            metric_session_id: str | None = None
            metric_project_root: str | None = None
            _ap_path = _active_stage_path()
            if _ap_path:
                metric_session_id = _extract_session_id_from_stage_path(_ap_path)
                metric_project_root = str(_find_project_root_for_stage_path(_ap_path))

            # retry_count：本次主模型实际调用次数 - 1（即重试次数）。
            #   - primary_attempts=1：首次成功，retry_count=0
            #   - primary_attempts=3：第 3 次重试成功（或仍失败），retry_count=2
            # 2026-06-17 引入：之前 used_fallback 二值化，丢失了"主模型重试几次
            # 才成功"的细粒度信息，/metrics D18-3-1 复盘时无法区分"瞬时抖动
            # 一次重试"和"持续 5xx 耗尽重试预算"两种场景。
            retry_count = max(0, primary_attempts - 1)

            # token_estimate：粗估，从 body 长度按 4 字符/token 算（业内常见近似）。
            token_estimate = max(1, len(body) // 4) if body else 0

            _append_metric({
                "ts":                  time.time(),
                "path":                self.path,
                "routing_source":      routing_source,
                "target_model":        session_model,
                "actual_model":        model,
                "target_model_is_strong": target_model_is_strong,  # §15 D15-1
                "status":              status,
                "is_success":          is_success,                 # §15 D15-1
                "pattern":             pattern_label,
                "complexity_label":    complexity_label,
                "complexity_score":    complexity_score,
                "complexity_source":   complexity_source,
                "workflow_type":       workflow.get("type"),
                "workflow_models":     workflow.get("models"),
                "workflow_step":       workflow.get("current_step"),
                "workflow_step_total": len(workflow.get("models") or []),
                "internal_request":    internal_req,
                "batch_template":      batch.get("template") if batch else None,
                "used_fallback":       used_fallback,
                "retry_count":         retry_count,                # §15 D15-1
                "token_estimate":      token_estimate,             # §15 D15-1
                "session_id":          metric_session_id,          # §15 D15-2
                "project_root":        metric_project_root,        # §15 D15-2
            })

            # ── 把本次最终路由态写回 session_state（route_model + task_complexity）──
            # 决策已完成（model_override / sticky-fb / fallback 链都收尾），可写入。
            # 用 SessionStateStore.write() 统一走原子写，并保留其它组件的字段
            # （runtime_score / todowrite_signal 等）。
            # sid / project_root 已在 _ap_path 解析后缓存为 metric_* 变量。
            if metric_session_id and metric_project_root:
                try:
                    from state_persistence import SessionStateStore
                    SessionStateStore().write(
                        sid=metric_session_id,
                        project_root=metric_project_root,
                        route_model=actual_route_model,
                        task_complexity=complexity_label,
                        fallback=read_fallback(),
                    )
                except Exception as _state_exc:
                    log.warning(
                        f"回写 route_model/task_complexity 到 session_state 失败: "
                        f"{_state_exc}"
                    )
        except Exception:
            pass

        # ── 结构化路由日志（设计文档 §15）──
        # D15-6：先脱敏（password / api_key / token / Authorization: Bearer ...）
        # 再写日志；脱敏后文本超 4000 字符自动截断，避免日志爆盘。
        prompt_scrubbed = _scrub_secrets(_extract_prompt_text(body))
        log.info(
            f"[{routing_source}] target={session_model} actual={model} "
            f"status={status} pattern={pattern_label} "
            f"complexity={complexity_label}({complexity_score},src={complexity_source}) "
            f"workflow={workflow.get('type')}"
            f"{('/step' + str(workflow.get('current_step')) + '/' + str(len(workflow.get('models') or []))) if workflow.get('current_step') else ''} "
            f"models={workflow.get('models')} "
            f"batch={batch.get('template') if batch else None}"
        )
        # prompt 单独行写（脱敏后），便于 grep 排错且不会让路由摘要超长
        if prompt_scrubbed:
            log.info(f"[{routing_source}] prompt(scrubbed)={prompt_scrubbed}")

        try:
            self.send_response(status)
            self.send_header("Content-Type", resp_headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        # 健康检查
        if self.path == "/health":
            from health_checker import get_health_status
            payload = {
                "status": "ok",
                "sticky_fallback": read_fallback(),
                "workflow": _read_workflow_state_safe(),
                # 2026-06-16：sticky TTL + 健康探测状态（供调试 / 监控）
                "sticky_ttl_seconds": STICKY_TTL_SECONDS,
                "health_probes": get_health_status(),
            }

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
                    # 无法解析 → 降级到 stage
                    stage = read_stage()
                    _, model, _, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                    _, fb_model, _, _ = FALLBACK_MODELS.get(stage, FALLBACK_MODELS["default"])
                    payload.update(
                        model_override=model_override, op=None, stage=stage,
                        model=model, protocol=protocol, fallback=fb_model,
                        routing_source=f"stage={stage}",
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
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/metrics":
            # 路由指标聚合（设计文档 §6.8 / §15）
            records = _read_metrics(limit=200)
            payload = {
                "summary": _summarize_metrics(records),
                "recent":  records[-20:],  # 最近 20 条
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/trace" or self.path.startswith("/trace?"):
            # 单条/多条最新路由决策的完整 trace（设计文档 §6.8 / §15）
            #
            # 支持查询参数（设计文档 §15 D15-5）：
            #   ?limit=N           — 返回最近 N 条，默认 1，最大 200
            #   ?session_id=<sid>  — 按 session_id 过滤
            #   ?project_root=<p>  — 按 project_root 过滤（字符串包含匹配）
            #
            # 返回结构：
            #   {
            #     "filter":   { "limit": N, "session_id": ..., "project_root": ... },
            #     "current_session": {
            #         "stage": ..., "op": ..., "model_override": ...,
            #         "pattern": ..., "complexity": ..., "batch": ...,
            #         "sticky_fallback": ...,
            #     },
            #     "matched":  <int>,    // 实际匹配并返回的记录数
            #     "total":    <int>,    // /tmp/stage_metrics.jsonl 中读到的总数
            #     "records":  [ {ts, routing_source, target_model, ...}, ... ]
            #   }
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = int(q.get("limit", ["1"])[0])
            except (ValueError, TypeError):
                limit = 1
            limit = max(1, min(limit, 200))
            session_filter = q.get("session_id", [None])[0]
            project_filter = q.get("project_root", [None])[0]

            records_all = _read_metrics(limit=500)
            if session_filter is not None or project_filter is not None:
                filtered: list[dict] = []
                for r in records_all:
                    if session_filter is not None and r.get("session_id") != session_filter:
                        continue
                    if project_filter is not None:
                        pr = r.get("project_root") or ""
                        if project_filter not in pr:
                            continue
                    filtered.append(r)
                records = filtered[-limit:]
                matched = len(filtered)
            else:
                records = records_all[-limit:]
                matched = len(records)

            payload = {
                "filter": {
                    "limit":        limit,
                    "session_id":   session_filter,
                    "project_root": project_filter,
                },
                "current_session": {
                    "stage":     read_stage(),
                    "op":        None,
                    "model_override": read_model_override(),
                    "pattern":   read_pattern(),
                    "complexity": read_complexity(),
                    "batch":     read_batch(),
                    "sticky_fallback": read_fallback(),
                    "workflow":  _read_workflow_state_safe(),
                },
                "matched": matched,
                "total":   len(records_all),
                "records": records,
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            try:
                self.send_response(404)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

# ── 入口 ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stage-Aware Model Router")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--dry-run", action="store_true", help="只打印路由决策，不实际转发")
    args = parser.parse_args()

    RouterHandler.dry_run = args.dry_run

    # .env 已在模块导入时通过 load_plugin_env(__file__) 加载完成（共享层 +
    # plugin-private 层）。这里只做启动期校验：缺少 API key 就直接报错退出
    # （避免请求飞到一半才 500）。
    missing = _check_required_keys()
    if missing:
        log.error("=" * 60)
        log.error(f"缺少必需的 API key 环境变量: {', '.join(missing)}")
        log.error("请在 ~/.claude/hooks/.env 或 ~/.claude/hooks/model_router/.env"
                  " 中填入，或在 shell 中 export。")
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

    # 多 session 并发修复（2026-06-14）：用 ThreadingHTTPServer 替换单线程 HTTPServer。
    # 原 HTTPServer 在 do_POST 中调用阻塞 urllib.request 做上游转发时，
    # 第二个 CC session 的请求必须排队等待——表现为"多 session 时一个能跑、其它都阻塞"。
    # ThreadingHTTPServer 给每个请求派生独立线程，互不阻塞。
    class _ThreadedRouterServer(ThreadingMixIn, http.server.HTTPServer):
        """线程化 HTTP server：每个请求一个独立线程，处理多 session 并发。"""
        daemon_threads = True  # 主线程退出时未完成的 worker 线程自动结束

    server = _ThreadedRouterServer(("127.0.0.1", args.port), RouterHandler)

    # ── 启动 sticky fallback 健康探测线程（2026-06-16 引入）──
    # 守护线程：定期探测失败 provider 是否恢复，恢复则自动清除 sticky。
    # PROBE_ENABLED=false 时不启动。
    from health_checker import start_health_checker
    start_health_checker()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stage Router 已停止")


if __name__ == "__main__":
    main()
