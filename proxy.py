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

# 注意：数据文件 current_stage 和 stage CLI 源同目录不同名
STAGE_FILE = Path.home() / ".claude" / "hooks" / "model_router" / "current_stage"
LOG_FILE   = Path.home() / ".claude" / "stage-router.log"
PORT       = 7878
ENV_FILE   = Path(__file__).parent / ".env"   # hooks/model_router/.env

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
STAGE_MODELS: dict[str, tuple[str, str, str, str]] = {
    "brainstorm": (
        "https://api.deepseek.com/anthropic",
        "deepseek-v4-flash",
        "DEEPSEEK_API_KEY",
        "anthropic",
    ),
    "decide": (
        "https://api.minimaxi.com/anthropic",
        "MiniMax-M3",
        "MINIMAX_API_KEY",
        "anthropic",
    ),
    "design": (
        "https://api.minimaxi.com/anthropic",
        "MiniMax-M3",
        "MINIMAX_API_KEY",
        "anthropic",
    ),
    "plan": (
        "https://api.deepseek.com/anthropic",
        "deepseek-v4-pro",
        "DEEPSEEK_API_KEY",
        "anthropic",
    ),
    "implement": (
        "https://api.deepseek.com/anthropic",
        "deepseek-v4-pro",
        "DEEPSEEK_API_KEY",
        "anthropic",
    ),
    "audit": (
        "https://api.minimaxi.com/anthropic",
        "MiniMax-M3",
        "MINIMAX_API_KEY",
        "anthropic",
    ),
    "default": (
        "https://api.deepseek.com/anthropic",
        "deepseek-v4-pro",
        "DEEPSEEK_API_KEY",
        "anthropic",
    ),
}

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

# ── 阶段读取 ───────────────────────────────────────────────────────────────────

def read_stage() -> str:
    """读取当前阶段，文件不存在或内容为空则返回 'default'。"""
    try:
        content = STAGE_FILE.read_text().strip().lower()
        if not content:
            log.warning("stage 文件为空，使用 default")
            return "default"
        if content in STAGE_MODELS:
            return content
        log.warning(f"未知阶段值 '{content}'，回退到 default")
        return "default"
    except FileNotFoundError:
        log.info(f"stage 文件不存在 ({STAGE_FILE})，使用 default")
        return "default"

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

    log.info(
        f"路由: 阶段={read_stage()!r} "
        f"原模型={original_model} → 目标={target_model} "
        f"provider={target_base} protocol={protocol}"
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
    """扫描 STAGE_MODELS，收集所有需要的 API key 环境变量名，报告缺失项。"""
    needed = {entry[2] for entry in STAGE_MODELS.values()}
    missing = [name for name in sorted(needed) if not os.environ.get(name, "").strip()]
    return missing


# ── HTTP 服务器 ────────────────────────────────────────────────────────────────

class RouterHandler(http.server.BaseHTTPRequestHandler):
    dry_run: bool = False

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 访问日志，用自己的 logger

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        headers = {k.lower(): v for k, v in self.headers.items()}

        stage = read_stage()
        base_url, model, key_env, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])

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

        self.send_response(status)
        self.send_header("Content-Type", resp_headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        # 健康检查
        if self.path == "/health":
            stage = read_stage()
            _, model, _, protocol = STAGE_MODELS.get(stage, STAGE_MODELS["default"])
            payload = json.dumps({
                "status": "ok",
                "stage": stage,
                "model": model,
                "protocol": protocol,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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
    log.info(f"阶段文件: {STAGE_FILE}")
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
