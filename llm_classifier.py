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

# ── 分类 System Prompt ──
# 要求 LLM 在一次回复中返回三维分类结果，纯 JSON，无额外文字。
CLASSIFIER_SYSTEM_PROMPT = """\
You are a task classifier for a code assistant model routing system.
Analyze the user's request and classify it along THREE dimensions.
Return ONLY valid JSON (no markdown fences, no extra text).

## Dimension 1 — stage (current work phase):
- "explore": reading code, tracing call chains, understanding current state, investigating logs
- "brainstorm": exploring ideas, creative thinking, possibilities, "what if"
- "decide": making decisions, comparing options, evaluating trade-offs
- "design": system architecture, designing solutions, data models, interfaces
- "plan": breaking down tasks, creating roadmaps, step-by-step planning
- "implement": coding, building, fixing bugs, developing, refactoring
- "test": writing tests, running tests, analyzing coverage, regression verification
- "audit": reviewing, security checking, code review, quality assurance (non-test review)
- "default": none of the above clearly matches / general chat

## Dimension 2 — pattern (task type):
- "feature": adding new functionality, building new things
- "bugfix": fixing bugs, errors, crashes, unexpected behavior
- "refactor": restructuring code, cleaning up, improving structure
- "test": writing tests, analyzing test results, test infrastructure
- "research": investigating, comparing approaches, exploring options
- "migration": migrating, upgrading versions, porting to new systems
- "architecture": system-level design, technology selection, module design
- "docs": documentation, comments, README, explanations
- "audit": security review, performance review, code review

## Dimension 3 — complexity:
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
  "pattern": "feature",
  "pattern_confidence": 0.92,
  "complexity_score": 45,
  "complexity_label": "medium",
  "complexity_confidence": 0.88,
  "reasoning": "brief one-line explanation in Chinese"
}"""


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
        raise RuntimeError(
            f"LLM 分类器超时（{timeout}s）: {e!r}"
        ) from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(
            f"LLM 分类器 HTTP {e.status_code}: {e.body!r}"
        ) from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(
            f"LLM 分类器连接失败: {e!r}"
        ) from e
    except Exception as e:
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
        raise RuntimeError(
            f"LLM 分类器返回空文本: model={model}"
        )

    # ── 解析 LLM 返回的 JSON ──
    result = _parse_classifier_json(text)

    # ── 校验 & 规范化 ──
    result = _validate_and_normalize(result, prompt)

    result["source"] = "llm"
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
VALID_STAGES = {
    "explore", "brainstorm", "decide", "design", "plan",
    "implement", "test", "audit", "default",
}
VALID_PATTERNS = {
    "feature", "bugfix", "refactor", "test", "research",
    "migration", "architecture", "docs", "audit",
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
    if pattern not in VALID_PATTERNS:
        pattern = "feature"  # 最常见的默认

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
