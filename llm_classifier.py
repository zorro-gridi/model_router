#!/usr/bin/env python3
"""
llm_classifier.py — LLM 轻量分类器（设计文档 §6.2 / §6.4 / §10 合并实现）
======================================================================

将原来三次独立的关键词分类（stage / pattern / complexity）合并为**一次 LLM 调用**，
一轮提交获取所有分类和评分。

设计目标：
  1. 单次 LLM 调用 → 统一返回 stage + pattern + complexity 三维分类结果
  2. 模型可配置（默认 MiniMax-M3，可切换为 deepseek-v4-flash 等）
  3. 如果 LLM 调用失败（网络/超时/解析），抛异常让调用方回退到 V1 关键词启发式
  4. 使用 Anthropic SDK（底层 httpx），自动走系统代理（HTTPS_PROXY / ALL_PROXY），
     不裸调 urllib.request（避免 GFW 阻断超时）

用法：
  from llm_classifier import classify

  result = classify("帮我写一个用户登录功能", classifier_config)
  # → {
  #     "stage": "implement",
  #     "pattern": "feature",
  #     "pattern_confidence": 0.92,
  #     "complexity_score": 45,
  #     "complexity_label": "medium",
  #     "complexity_confidence": 0.88,
  #     "reasoning": "实现新功能，中等复杂度",
  #     "source": "llm",
  #   }

配置来源：
  优先级：调用方传入 config > stage_config.LLM_CLASSIFIER_CONFIG > 内置默认

代理支持：
  - httpx 自动读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 环境变量
  - 支持 socks5h://127.0.0.1:7890（需要 pip install socksio）
  - 也支持显式 proxy 字段：cfg["proxy"] = "socks5h://127.0.0.1:7890"
"""

import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Optional

# 将同目录加入 sys.path，确保直接执行或 Hook 调用都能 import stage_config
sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402
import httpx      # noqa: E402

# ── .env 自动加载（共享双层 loader）──
# Claude Code hook 子进程不会继承 shell export 的 env，必须从 .env 读 key 并
# 注入 os.environ。统一用 _load_env.load_plugin_env：先读共享层 hooks/.env，
# 再读 plugin-private 层 hooks/model_router/.env。已设置的 env 变量优先级
# 更高（不覆盖）。
sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
from _load_env import load_plugin_env  # noqa: E402
load_plugin_env(__file__)  # noqa: E402

# ── 日志（设计文档 §15 D15-6：脱敏后再写，避免 password/api_key 落盘）──
# 日志路径从 __file__ 的目录结构镜像到 /tmp/ 下：
#   __file__ = /Users/zorro/.claude/hooks/model_router/llm_classifier.py
#   LOG_FILE = /tmp/hooks/model_router/llm_classifier.log
# 这样多项目/多模块同名文件不会互相覆盖，从日志路径就能反推出源文件位置。
# 两种调用上下文都安全：
#   1) 在 proxy 进程内被 import —— 走 named logger "llm_classifier"，与 proxy 的
#      "stage-router" logger 完全隔离，handler 也不会重复挂载
#   2) 在独立子进程（hook / CLI 调试）被 import —— 给 llm_classifier logger
#      挂一个 FileHandler 兜底
_MODULE_PATH = Path(__file__).resolve()
try:
    _IDX = _MODULE_PATH.parts.index("hooks")
    _LOG_REL = Path(*_MODULE_PATH.parts[_IDX:]).with_suffix(".log")
except ValueError:
    _LOG_REL = Path("llm_classifier.log")
LOG_FILE = Path("/tmp") / _LOG_REL
_log = logging.getLogger("llm_classifier")
# 按 logger name 去重（不同 logger 命名空间天然隔离，
# 同 logger 在同一进程多次 import 或 reload 也只挂一次）
if not any(
    isinstance(h, logging.FileHandler)
    and getattr(h, "_llm_classifier_owned", False)
    for h in _log.handlers
):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _log.setLevel(logging.INFO)
        _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        _fh._llm_classifier_owned = True  # 标记为本模块挂的，便于去重
        _log.addHandler(_fh)
        # 不 propagate 到 root，避免在 proxy 进程里和 root handler 重复打印
        _log.propagate = False
    except OSError:
        # 日志文件不可写时静默降级（不影响分类主流程）
        pass


# 简易脱敏：D15-6 — 拦截 password / api_key / token / Authorization / Bearer
# 这些关键词附近的明文，避免分类回写的 reasoning 字段意外把密钥也带进日志。
_SECRET_PATTERNS = [
    re.compile(r'(?i)(password\s*[=:]\s*)[^\s,;}\'"]+'),
    re.compile(r'(?i)(api[_-]?key\s*[=:]\s*)[^\s,;}\'"]+'),
    re.compile(r'(?i)(token\s*[=:]\s*)[^\s,;}\'"]+'),
    re.compile(r'(?i)(authorization\s*:\s*bearer\s+)[^\s,;}\'"]+'),
    re.compile(r'(?i)(sk-[A-Za-z0-9_-]{16,})'),
]


def _scrub_secrets(text: str) -> str:
    """脱敏 password/api_key/token/Authorization/Bearer 片段。"""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(r"\1***REDACTED***", out)
    return out


def _log_classify_result(result: dict) -> None:
    """把一次成功的分类结果写入 /tmp/… 镜像路径（LOG_FILE）。"""
    try:
        _log.info(
            "[llm_classifier] pattern=%s(p=%.2f) complexity=%s(%d,p=%.2f) "
            "stage=%s reasoning=%s",
            result.get("pattern"),
            result.get("pattern_confidence", 0.0),
            result.get("complexity_label"),
            result.get("complexity_score", 0),
            result.get("complexity_confidence", 0.0),
            result.get("stage"),
            _scrub_secrets(str(result.get("reasoning", ""))),
        )
    except Exception:
        # 日志失败不影响分类返回值
        pass


def _log_classify_failure(stage: str, err: BaseException) -> None:
    """把一次分类失败（含 reason）写入 /tmp/… 镜像路径（LOG_FILE）。"""
    try:
        _log.warning(
            "[llm_classifier] %s failed: %s: %s",
            stage,
            type(err).__name__,
            _scrub_secrets(str(err))[:300],
        )
    except Exception:
        pass

# ── 默认配置（deepseek-v4-flash，Anthropic 协议）──
DEFAULT_CLASSIFIER_CONFIG: dict = {
    "model":       "deepseek-v4-flash",
    "base_url":    "https://api.deepseek.com/anthropic",
    "api_key_env": "DEEPSEEK_API_KEY",
    "protocol":    "anthropic",
    "max_tokens":  512,      # 分类只需要很短的回答
    "temperature": 0.0,      # 零温度确保确定性
    "timeout":     15,       # 分类超时上限（秒），超时则回退 V1
    "max_prompt_chars": 8000,  # §6 D6.1-2 修复 2026-06-14：长 prompt 截断阈值
    "proxy":       None,     # 可选：显式代理地址，如 "socks5h://127.0.0.1:7890"
                             # 不设置则自动走 HTTPS_PROXY / ALL_PROXY 环境变量
}

# ── 分类 System Prompt（V1.3 设计 §5 任务分类体系）──
# 要求 LLM 在一次回复中返回三维分类结果，纯 JSON，无额外文字。
# V1.3 不再保留 Stage 作为路由维度，仅保留 Task Pattern + Task Complexity。
# 兼容期：仍输出 stage 字段，但已不参与路由决策（V1.3 §16 推荐删除项）。
CLASSIFIER_SYSTEM_PROMPT = """\
You are a task classifier for a code assistant model routing system.
Analyze the user's request and classify it along THREE dimensions.
Return ONLY valid JSON (no markdown fences, no extra text).

## Dimension 1 — stage (current work phase, legacy/display only):
- "explore": reading code, tracing call chains, understanding current state, investigating logs
- "brainstorm": exploring ideas, creative thinking, possibilities, "what if"
- "decide": making decisions, comparing options, evaluating trade-offs
- "design": system architecture, designing solutions, data models, interfaces
- "plan": breaking down tasks, creating roadmaps, step-by-step planning
- "implement": coding, building, fixing bugs, developing, refactoring
- "test": writing tests, running tests, analyzing coverage, regression verification
- "audit": reviewing, security checking, code review, quality assurance (non-test review)
- "default": none of the above clearly matches / general chat

## Dimension 2 — pattern (V1.3 §5.1 Task Pattern, 12 types):
- "explore": 探索与调研
- "architecture": 架构设计
- "feature": 新功能需求
- "audit": 审计系统功能
- "implement": 功能实现
- "debug": 调试异常
- "refactor": 模块重构
- "test": 测试相关
- "research": 调查研究
- "migration": 模块迁移
- "docs": 文档处理
- "ops": 运维、脚本、配置类任务

## Dimension 3 — complexity (V1.3 §5 Task Complexity):
- score: integer 0-100
  - 0-10: trivial (typo fix, one-line change, simple question)
  - 11-30: simple (single file, clear requirements)
  - 31-70: medium (multiple files, some design needed)
  - 71-100: complex (cross-module, architectural, high risk)
- label: "simple" (0-30), "medium" (31-70), "complex" (71-100)

## Confidence scoring:
- pattern_confidence: 0.0-1.0 (how sure you are about the pattern)
- complexity_confidence: 0.0-1.0 (how sure you are about the complexity)

## Response format (JSON only, no fences):
{
  "stage": "implement",
  "pattern": "implement",
  "pattern_confidence": 0.92,
  "complexity_score": 45,
  "complexity_label": "medium",
  "complexity_confidence": 0.88,
  "reasoning": "brief one-line explanation in Chinese"
}

NOTE: pattern must be exactly one of the 12 values listed above."""


def _build_http_client(proxy: Optional[str] = None, timeout: int = 15) -> httpx.Client:
    """构建带代理和超时配置的 httpx.Client。

    Args:
        proxy: 显式代理地址（如 "socks5h://127.0.0.1:7890"）。
               为 None 时自动走 HTTPS_PROXY / ALL_PROXY 环境变量。
        timeout: 请求超时秒数。

    Returns:
        配置好的 httpx.Client 实例。
    """
    transport_kwargs = {}

    if proxy:
        # 显式代理：构造 httpx.Proxy 传给 transport
        proxy_url = httpx.URL(proxy)
        transport = httpx.HTTPTransport(proxy=httpx.Proxy(url=proxy_url))
    else:
        # 不设显式代理：httpx 自动读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY
        # 当 socksio 安装时，socks5h:// 开头的环境变量也会被识别
        transport = httpx.HTTPTransport()

    client = httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=10.0),
    )
    return client


def _load_config(override: Optional[dict] = None) -> dict:
    """加载分类器配置：调用方传入 > stage_config > 内置默认。"""
    cfg = dict(DEFAULT_CLASSIFIER_CONFIG)  # shallow copy

    # 尝试从 stage_config 导入
    try:
        from stage_config import LLM_CLASSIFIER_CONFIG  # noqa: E402
        cfg.update(LLM_CLASSIFIER_CONFIG)
    except ImportError:
        pass  # stage_config 中还没定义这个字段，用默认值

    if override:
        cfg.update(override)

    return cfg


def classify(prompt: str, config_override: Optional[dict] = None) -> dict:
    """
    单次 LLM 调用的轻量分类器。

    Args:
        prompt: 用户原始 prompt 文本
        config_override: 可选配置覆盖（model / base_url / api_key_env / ...）

    Returns:
        {
            "stage": str,                    # 阶段名
            "pattern": str,                  # 任务模式
            "pattern_confidence": float,     # 模式置信度 0~1
            "complexity_score": int,         # 复杂度分数 0~100
            "complexity_label": str,         # simple | medium | complex
            "complexity_confidence": float,  # 复杂度置信度 0~1
            "reasoning": str,                # 分类理由简述
            "source": "llm",                 # 标记来源
        }

    Raises:
        RuntimeError: LLM 调用失败（网络/超时/API 错误/JSON 解析失败），
                      调用方应回退到 V1 关键词启发式。
    """
    cfg = _load_config(config_override)

    model = cfg["model"]
    base_url = cfg["base_url"]
    api_key_env = cfg["api_key_env"]
    max_tokens = cfg.get("max_tokens", 512)
    temperature = cfg.get("temperature", 0.0)
    timeout = cfg.get("timeout", 15)
    proxy = cfg.get("proxy", None)
    max_prompt_chars = int(cfg.get("max_prompt_chars", 8000))

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(
            f"LLM 分类器：环境变量 {api_key_env} 未设置，无法调用 {model}"
        )

    # ── §6 D6.1-2 长 prompt 截断 ──
    if len(prompt) > max_prompt_chars:
        head_chars = int(max_prompt_chars * 0.6)
        tail_chars = max_prompt_chars - head_chars
        truncated_chars = len(prompt) - head_chars - tail_chars
        prompt = (
            prompt[:head_chars]
            + f"\n\n... [已截断 {truncated_chars} 字符] ...\n\n"
            + prompt[-tail_chars:]
        )

    # ── 构造 Anthropic SDK 客户端 ──
    # SDK 底层 httpx 会自动走 HTTPS_PROXY / ALL_PROXY 环境变量。
    # 如果显式配置了 proxy 字段，则用自定义 httpx.Client 注入代理。
    http_client = _build_http_client(proxy=proxy, timeout=timeout) if proxy else None

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=float(timeout),
        max_retries=0,   # 分类器不重试，失败就回退 V1
        http_client=http_client,
    )

    # ── 调用 LLM ──
    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=CLASSIFIER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APITimeoutError as e:
        _log_classify_failure("api_timeout", e)
        raise RuntimeError(
            f"LLM 分类器超时（{timeout}s）: {e!r}"
        ) from e
    except anthropic.APIStatusError as e:
        _log_classify_failure(f"api_status_{e.status_code}", e)
        raise RuntimeError(
            f"LLM 分类器 HTTP {e.status_code}: {e.body!r}"
        ) from e
    except anthropic.APIConnectionError as e:
        _log_classify_failure("api_connect", e)
        raise RuntimeError(
            f"LLM 分类器连接失败: {e!r}"
        ) from e
    except Exception as e:
        _log_classify_failure(f"api_unknown({type(e).__name__})", e)
        raise RuntimeError(
            f"LLM 分类器未知错误: {type(e).__name__}: {e!r}"
        ) from e

    # ── 提取 text 内容 ──
    text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    text = text.strip()

    if not text:
        _log_classify_failure("empty_text", RuntimeError(f"model={model}"))
        raise RuntimeError(
            f"LLM 分类器返回空文本: model={model}"
        )

    # ── 解析 LLM 返回的 JSON ──
    try:
        result = _parse_classifier_json(text)
    except RuntimeError as e:
        _log_classify_failure("json_parse", e)
        raise

    # ── 校验 & 规范化 ──
    result = _validate_and_normalize(result, prompt)

    # ── is_valid_prompt 透传 ──
    # LLM 返回 is_valid_prompt=False 表示 prompt 是任务续接指令
    # （如 "go ahead" / "continue" / "stop"），不应触发路由状态变更。
    # 调用方（stage_detector / proxy）据此跳过 stage/pattern/complexity 覆写。
    result["is_valid_prompt"] = raw.get("is_valid_prompt", True)

    result["source"] = "llm"
    _log_classify_result(result)
    return result


def _parse_classifier_json(text: str) -> dict:
    """解析分类器返回的文本，处理可能的 markdown fence 包裹。"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 去除 ```json ... ``` 包裹
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. 尝试找到第一个 { 到最后一个 }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        f"LLM 分类器无法解析 JSON: {text[:300]!r}"
    )


# ── 合法的 stage / pattern 枚举值 ──
# V1.3 §5.1 Task Pattern 重新定义为 12 种：
#   explore / architecture / feature / audit / implement / debug /
#   refactor / test / research / migration / docs / ops
# 旧 V1 枚举（bugfix → debug、migration → migration）保留为兼容别名
# 以避免破坏 LLM 输出（_PATTERN_ALIASES 自动归一化）。
VALID_STAGES = {
    "explore", "brainstorm", "decide", "design", "plan",
    "implement", "test", "audit", "default",
}
VALID_PATTERNS = {
    "explore", "architecture", "feature", "audit", "implement",
    "debug", "refactor", "test", "research", "migration",
    "docs", "ops",
}
# V1 旧 pattern → V1.3 新 pattern 兼容映射（仅归一化旧 V1 命名变体）
_PATTERN_ALIASES: dict[str, str] = {
    "bugfix": "debug",       # 旧 V1 用 bugfix，V1.3 改用 debug
    "architecture": "architecture",  # 已是新名，保留兼容
    "migration": "migration",        # 已是新名，保留兼容
}
VALID_COMPLEXITY_LABELS = {"simple", "medium", "complex"}


def _validate_and_normalize(raw: dict, prompt: str) -> dict:
    """校验 LLM 返回的分类结果，不合法的字段回退到合理默认值。"""
    if not isinstance(raw, dict):
        raise RuntimeError(f"LLM 分类器返回非 dict: {type(raw).__name__}")

    # ── stage ──
    stage = str(raw.get("stage", "")).strip().lower()
    if stage not in VALID_STAGES:
        stage = "default"

    # ── pattern ──
    pattern = str(raw.get("pattern", "")).strip().lower()
    # V1 旧枚举 → V1.3 新枚举 兼容归一化
    if pattern in _PATTERN_ALIASES:
        pattern = _PATTERN_ALIASES[pattern]
    if pattern not in VALID_PATTERNS:
        pattern = "implement"  # V1.3 §5.1 默认（最常见的 pattern）

    # ── pattern_confidence ──
    try:
        pattern_confidence = float(raw.get("pattern_confidence", 0.5))
    except (ValueError, TypeError):
        pattern_confidence = 0.5
    pattern_confidence = max(0.0, min(1.0, round(pattern_confidence, 2)))

    # ── complexity_score ──
    try:
        complexity_score = int(raw.get("complexity_score", 50))
    except (ValueError, TypeError):
        complexity_score = 50
    complexity_score = max(0, min(100, complexity_score))

    # ── complexity_label ──
    complexity_label = str(raw.get("complexity_label", "")).strip().lower()
    if complexity_label not in VALID_COMPLEXITY_LABELS:
        # 从分数推导
        if complexity_score <= 30:
            complexity_label = "simple"
        elif complexity_score <= 70:
            complexity_label = "medium"
        else:
            complexity_label = "complex"

    # ── complexity_confidence ──
    try:
        complexity_confidence = float(raw.get("complexity_confidence", 0.5))
    except (ValueError, TypeError):
        complexity_confidence = 0.5
    complexity_confidence = max(0.0, min(1.0, round(complexity_confidence, 2)))

    # ── reasoning ──
    reasoning = str(raw.get("reasoning", "")).strip()

    return {
        "stage":                  stage,
        "pattern":                pattern,
        "pattern_confidence":     pattern_confidence,
        "complexity_score":       complexity_score,
        "complexity_label":       complexity_label,
        "complexity_confidence":  complexity_confidence,
        "reasoning":              reasoning,
    }


# ── CLI 入口（调试用）──
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 llm_classifier.py '<prompt>'", file=sys.stderr)
        sys.exit(1)

    test_prompt = sys.argv[1]
    try:
        result = classify(test_prompt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except RuntimeError as e:
        print(f"分类失败: {e}", file=sys.stderr)
        sys.exit(1)
