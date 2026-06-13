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

Operation-type 路由（2026-06-13 引入，第二维度）：
  检出 op 时完全覆盖 stage 路由，未检出时退回 stage 路由。
  op 文件位置：<project_root>/.claude/op_<sid>（与 stage_<sid> 同目录、仅前缀替换）。
  read_operation() 路径解析复用 stage_detector 的 _op_file_path() 派生规则。

Model-override 路由（2026-06-13 引入，最高优先级）：
  检出 model 覆盖时完全覆盖 op/stage 路由。
  model 文件位置：<project_root>/.claude/model_<sid>（与 stage_<sid> 同目录、仅前缀替换）。
  路由优先级: model_override > op > stage > default。

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
import urllib.request
import urllib.error
from pathlib import Path

# ── 配置 ───────────────────────────────────────────────────────────────────────

# ── 分 session 阶段管理 ──
# proxy.py 是无 stdin 上下文的 HTTP 服务器，无法直接拿到 session_id。
# 它依赖 stage_detector.py（UserPromptSubmit hook）维护的 active_session 指针。
# active_session 存储的是阶段文件的**完整绝对路径**，proxy 直接读取即可，
# 无需再拼接 STAGE_DIR。
HOOK_DIR            = Path.home() / ".claude" / "hooks" / "model_router"
ACTIVE_SESSION_FILE  = HOOK_DIR / "active_session"
GLOBAL_STAGE_FILE    = HOOK_DIR / "current_stage"   # 全局后备
LOG_FILE             = Path.home() / ".claude" / "stage-router.log"
PORT                 = 7878
ENV_FILE             = Path(__file__).parent / ".env"   # hooks/model_router/.env

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
    读取当前阶段，优先级：
      1. active_session 指针 → 读取其存储的完整路径文件
      2. 全局后备文件 → current_stage
      3. default

    proxy.py 是无 stdin 的 HTTP 服务器，无法直接拿到 session_id。
    它依赖 stage_detector.py（UserPromptSubmit hook）维护的 active_session 指针。
    active_session 存储的是阶段文件的完整绝对路径（如
    /Users/zorro/project/.claude/stage_aaa-bbb），直接读取即可。
    """
    # 1. active_session 指针 → 存储的是完整路径，直接读取
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(Path(active_path))
            if content and content in STAGE_MODELS:
                return content
            if content:
                log.warning(f"active_session 指向 {active_path} 未知阶段值 '{content}'，继续降级查找")
    except FileNotFoundError:
        pass

    # 2. 全局后备
    content = _read_stage_file(GLOBAL_STAGE_FILE)
    if content:
        if content in STAGE_MODELS:
            return content
        log.warning(f"current_stage 未知阶段值 '{content}'，回退到 default")
        return "default"

    # 3. 兜底
    log.info("无任何阶段文件（无 active_session、无 current_stage），使用 default")
    return "default"


# ── Operation-type 读取（与 stage 同构，无 stdin 时也走 active_session 指针）──

def _op_file_path(stage_file: Path) -> Path:
    """从 stage_<sid> 路径派生 op_<sid> 路径（同目录、仅前缀替换）。
    与 stage_detector._op_file_path 保持完全相同的派生规则。
    """
    return stage_file.with_name(stage_file.name.replace("stage_", "op_", 1))


def read_operation() -> str | None:
    """
    读取当前 op，路径解析复用 stage_detector 的派生规则。
    proxy.py 是无 stdin 的 HTTP 服务器：从 active_session 指针拿到
    stage_<sid> 完整路径，再派生 op_<sid>。
    返回 None 表示"无 op 信号"——proxy 走 stage 路由（与升级前行为一致）。
    """
    try:
        active_path = ACTIVE_SESSION_FILE.read_text().strip()
        if active_path:
            content = _read_stage_file(_op_file_path(Path(active_path)))
            if content and content in OPERATION_MODELS:
                return content
            if content:
                log.warning(
                    f"op_<sid> 未知 op 值 '{content}'，忽略 op 走 stage 路由"
                )
    except FileNotFoundError:
        pass
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
        f"路由: 阶段={read_stage()!r} "
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


class RouterHandler(http.server.BaseHTTPRequestHandler):
    dry_run: bool = False

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 访问日志，用自己的 logger

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        headers = {k.lower(): v for k, v in self.headers.items()}

        # ── 路由决策：model_override > op > stage > default ──
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

        if not model_override:
            op = read_operation()
            if op and op in OPERATION_MODELS:
                base_url, model, key_env, protocol = OPERATION_MODELS[op]
                fb_base, fb_model, fb_key, fb_proto = OPERATION_FALLBACK_MODELS[op]
                routing_source = f"op={op}"
            else:
                stage = read_stage()
                base_url, model, key_env, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
                fb_base, fb_model, fb_key, fb_proto = FALLBACK_MODELS.get(
                    stage, FALLBACK_MODELS["default"]
                )
                routing_source = f"stage={stage}"

        # ── Sticky fallback: 主模型曾失败过，交换主/备避免重复重试 ──
        # 仅在自动路由（非 model_override）下生效——用户显式指定模型时不干预
        sticky_fb = read_fallback() if not model_override else None
        if sticky_fb:
            (base_url, model, key_env, protocol,
             fb_base, fb_model, fb_key, fb_proto) = (
                fb_base, fb_model, fb_key, fb_proto,
                base_url, model, key_env, protocol,
            )
            routing_source += f" [sticky-fb={sticky_fb}]"

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
        if _is_retriable(status) and fb_base and fb_model:
            log.warning(
                f"[{routing_source}] 主模型 {model} 返回 {status}，"
                f"切换到备用 {fb_model} [{fb_base}]"
            )
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
