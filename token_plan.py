#!/usr/bin/env python3
"""
MiniMax Token Plan 余量查询 + Provider Fallback 触发
====================================================

背景（2026-06-17）：
  MiniMax（provider 名 "minimax"）的 token 套餐分两套配额：
    1. 5 小时滚动窗口：current_interval_remaining_percent
    2. 周窗口：       current_weekly_remaining_percent
  当任一窗口剩余百分比 < 2%（即用量 > 98%）时，意味着账户已几乎打满该窗口配额，
  继续往 minimax 路由只会 429 阻塞整个 session。

目标：
  - proxy.py 路由决策时：当主模型 provider 是 minimax 且余量超阈值，
    主动写 sticky fallback（"minimax"），与 _is_retriable 错误码 fallback 并行。
  - smart_precompact.py 每次启动压缩前：同样做一次 precheck，
    避免压缩大 prompt 时被 429 阻塞 → 任务长时间卡住。

判断字段（来自 /v1/token_plan/remains 响应）：
  model_remains: list[dict]，每条是一个套餐；按 model_name 区分（"general"/"video"/...）
  每条字段：
    - model_name: 套餐名
    - current_interval_status: 1=正常, 2=已耗尽, 3=未使用
    - current_interval_remaining_percent: 0~100
    - current_weekly_status: 1/2/3 同上
    - current_weekly_remaining_percent: 0~100
    - remains_time: 剩余可用时间（毫秒）

阈值策略：
  - 默认阈值 98%（用量），可通过 STAGE_ROUTER_TOKEN_PLAN_THRESHOLD_PERCENT 调整
  - 任一窗口剩余百分比 < (100 - 阈值) → 触发 sticky fallback
    例：阈值 98 → 剩余 < 2% 触发；阈值 90 → 剩余 < 10% 触发
  - 已耗尽（status=2）也直接触发（与 status=2 + percent=0 行为一致）

调用方接口：
    from token_plan import precheck_and_fallback
    precheck_and_fallback(reason="proxy-do_post")

  返回 (triggered: bool, info: dict)：
    - triggered=True 表示本次 precheck 触发了 sticky fallback
    - info 含原始 API 响应 + 阈值判断结果，便于日志/metrics

设计约束：
  - API 调用失败（网络/auth/解析）一律视为「无法判定」，不触发 fallback。
  - 缓存 TTL 60s（in-process），避免每个 CC 请求都打 minimax API 拉新数据。
  - 缓存 key 固定为 "minimax_general"（不按 session 区分）—— token 余量是
    账户级指标，与 session 无关；按 session 缓存反而会产生「A session 缓存
    命中后 B session 用了过期数据」的不一致。

与 sticky fallback 系统的关系：
  - 本模块不直接 new 写 sticky 文件，统一调用 proxy.try_write_fallback(provider)。
  - 复用 O_CREAT|O_EXCL 原子写：并发 N 个请求首次失败时仅首个触发 fallback retry。
  - 用户 ~provider reset / ~model reset 仍可清除本模块写入的 sticky。
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

# 使用与 proxy.py 相同的 env 加载机制（绝对路径解析），
# 避免 PreCompact hook 子进程因 CWD 不在 ~/.claude 而找不到 .env。
# 注意：必须在文件顶部的其他 import 之前执行，确保后续代码读到正确的 env。
import sys as _tp_sys
_tp_sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
from _load_env import load_plugin_env as _load_plugin_env  # noqa: E402
_load_plugin_env(__file__)  # noqa: E402
del _tp_sys, _load_plugin_env

log = logging.getLogger("token-plan")

# ── 配置 ───────────────────────────────────────────────────────────────────────

# API endpoint（用户提供的查询接口）
TOKEN_PLAN_API_URL = os.environ.get(
    "MINIMAX_TOKEN_PLAN_URL",
    "https://www.minimaxi.com/v1/token_plan/remains",
)

# 触发 fallback 的阈值：任一窗口余量 > 阈值 → 触发。
# 用户指定 98%——意味着已几乎打满。调小会更激进，调大会更保守。
THRESHOLD_PERCENT = float(
    os.environ.get("STAGE_ROUTER_TOKEN_PLAN_THRESHOLD_PERCENT", "98")
)

# 缓存 TTL（秒）。in-process 内存缓存，避免每个 CC 请求都打一次 minimax API。
CACHE_TTL_SECONDS = int(
    os.environ.get("STAGE_ROUTER_TOKEN_PLAN_CACHE_TTL_SECONDS", "60")
)

# 单次 API 请求超时。minimax API 一般几百 ms 返回，5s 足够；超时就放弃不阻塞。
API_TIMEOUT_SECONDS = float(
    os.environ.get("STAGE_ROUTER_TOKEN_PLAN_TIMEOUT_SECONDS", "5")
)

# Provider 名（与 stage_config.MODEL_TO_PROVIDER 保持一致）
PROVIDER_NAME = "minimax"

# 要检查的套餐名。minimax 账户用 "general" 套餐；其他套餐（如 video）不在
# minimax LLM API 路由路径上，不参与 fallback 决策。
PLAN_NAME = "general"


# ── 缓存 ───────────────────────────────────────────────────────────────────────
# 全局 in-process 缓存：key=固定 "minimax_general"，value=(fetched_at, payload)
# 不按 session 区分：token 余量是账户级指标，所有 session 共享同一份最新数据。
#
# thread-lock 原因：proxy.py 是 ThreadingMixIn HTTP server，多线程并发 do_POST
# 时若同时走到 precheck → 无锁会出现重复 HTTP 请求 + 重复解析。
_cache_lock = threading.Lock()
_cache_fetched_at: float = 0.0
_cache_payload: Optional[dict] = None
_cache_error: Optional[str] = None  # 最近一次失败的错误信息（仅日志用）


def _cache_get() -> Optional[dict]:
    """读缓存。命中且未过期 → 返回 payload；否则 None（让调用方重新拉）。"""
    with _cache_lock:
        if _cache_payload is None:
            return None
        if (time.time() - _cache_fetched_at) > CACHE_TTL_SECONDS:
            return None
        return _cache_payload


def _cache_set(payload: Optional[dict], error: Optional[str] = None) -> None:
    """写缓存。即使失败也缓存（用短 TTL 也行；这里统一 60s 减少失败风暴）。"""
    global _cache_fetched_at, _cache_payload, _cache_error
    with _cache_lock:
        _cache_fetched_at = time.time()
        _cache_payload = payload
        _cache_error = error


def _cache_clear() -> None:
    """清缓存（用户 ~provider reset 时调用，便于立即重新探测）。"""
    global _cache_fetched_at, _cache_payload, _cache_error
    with _cache_lock:
        _cache_fetched_at = 0.0
        _cache_payload = None
        _cache_error = None


# ── API 调用 ───────────────────────────────────────────────────────────────────

def _api_key() -> str:
    """从 env 读 minimax API key（与 stage_config 共用 MINIMAX_API_KEY）。"""
    return os.environ.get("MINIMAX_API_KEY", "").strip()


def _fetch_token_plan_raw() -> dict:
    """
    调 minimax /v1/token_plan/remains 接口，返回 parsed JSON dict。

    Raises:
        RuntimeError: API key 缺失 / HTTP 非 2xx / 响应解析失败。
    """
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY 未设置，跳过 token plan 探测")
    req = urllib.request.Request(
        TOKEN_PLAN_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token_plan API HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"token_plan API 网络错误: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"token_plan API 未知错误: {e}") from e

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"token_plan API 响应非 JSON: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"token_plan API 响应根不是 dict: {type(data).__name__}")

    return data


def _extract_plan_data(payload: dict, plan_name: str = PLAN_NAME) -> dict:
    """
    从 API 响应里提取指定套餐（默认 "general"）的余量数据。

    真实 API 响应（2026-06-17 实测）：
        {
          "model_remains": [
            {"model_name": "general", "current_interval_remaining_percent": 2, ...},
            {"model_name": "video",   "current_interval_remaining_percent": 100, ...},
            ...
          ],
          "base_resp": {...}
        }

    `model_remains` 是 list（不是 dict），每条记录的 `model_name` 字段
    标识套餐名。需要在 list 里线性扫描匹配 `plan_name` 的那条。

    Returns:
        dict 含 current_interval_remaining_percent / current_weekly_remaining_percent
              / current_interval_status / current_weekly_status 等字段。

    Raises:
        KeyError: 响应结构不符合预期（缺 model_remains 或指定套餐名）。
    """
    model_remains = payload.get("model_remains")
    if not isinstance(model_remains, list):
        raise KeyError("response.model_remains 缺失或不是 list")
    for plan in model_remains:
        if isinstance(plan, dict) and plan.get("model_name") == plan_name:
            return plan
    raise KeyError(f"response.model_remains 中未找到 model_name={plan_name!r} 的套餐")


def _should_trigger_fallback(plan: dict, threshold: float = THRESHOLD_PERCENT) -> tuple[bool, dict]:
    """
    根据套餐余量数据判断是否触发 sticky fallback。

    触发条件（任一满足即触发）：
      1. 5h 窗口 current_interval_remaining_percent ≤ (100 - threshold)
      2. 周窗口 current_weekly_remaining_percent ≤ (100 - threshold)
      3. 任一窗口 status=2（已耗尽）—— 等价于 percent=0 但语义更明确

    Args:
        plan: _extract_plan_data() 返回的 dict。
        threshold: 触发阈值（百分比），默认从 THRESHOLD_PERCENT 来。

    Returns:
        (triggered, reason_dict)
        - triggered: bool，是否触发 fallback
        - reason_dict: 含各字段值 + 触发原因（便于日志/metrics）
    """
    interval_pct = plan.get("current_interval_remaining_percent")
    weekly_pct = plan.get("current_weekly_remaining_percent")
    interval_status = plan.get("current_interval_status")
    weekly_status = plan.get("current_weekly_status")

    triggered = False
    reasons: list[str] = []

    # 条件 1: 5h 窗口剩余不足（剩余百分比低于安全水位）
    # 字段 current_interval_remaining_percent 是**剩余**百分比（0=耗尽, 100=满额）。
    # threshold 的语义是"用量阈值"（如 98 → 用量 >98% 即剩余 <2%），
    # 因此比较方向是 remaining < (100 - threshold)。
    safe_remaining = 100.0 - threshold
    if isinstance(interval_pct, (int, float)) and interval_pct <= safe_remaining:
        triggered = True
        reasons.append(f"5h_window_remaining={interval_pct}%≤{safe_remaining}%")

    # 条件 2: 周窗口剩余不足
    if isinstance(weekly_pct, (int, float)) and weekly_pct <= safe_remaining:
        triggered = True
        reasons.append(f"weekly_remaining={weekly_pct}%≤{safe_remaining}%")

    # 条件 3: 任一窗口状态=已耗尽（兜底——status=2 显式语义更稳）
    if interval_status == 2:
        triggered = True
        reasons.append("5h_window_status=exhausted")
    if weekly_status == 2:
        triggered = True
        reasons.append("weekly_status=exhausted")

    return triggered, {
        "plan_name":              plan.get("model_name", PLAN_NAME),
        "interval_remaining_pct": interval_pct,
        "weekly_remaining_pct":   weekly_pct,
        "interval_status":        interval_status,
        "weekly_status":          weekly_status,
        "threshold":              threshold,
        "triggered":              triggered,
        "reasons":                reasons,
    }


# ── 公开 API ───────────────────────────────────────────────────────────────────

def get_token_plan_status(
    *,
    use_cache: bool = True,
    threshold: float = THRESHOLD_PERCENT,
) -> dict:
    """
    拉一次（可能命中缓存）token plan 状态，返回判定结果。

    Returns:
        dict: {
            "ok": bool,           # True 表示成功拉到数据
            "error": str | None,  # 失败时的错误信息
            "plan": dict | None,  # 原始套餐数据
            "judgment": dict,     # _should_trigger_fallback 返回的 reason_dict
        }
    """
    if use_cache:
        cached = _cache_get()
        if cached is not None:
            try:
                plan = _extract_plan_data(cached)
                triggered, judgment = _should_trigger_fallback(plan, threshold)
                judgment["cache_hit"] = True
                return {"ok": True, "error": None, "plan": plan, "judgment": judgment}
            except (KeyError, TypeError):
                # 缓存结构异常（schema 变化？）→ 当作 cache miss 重新拉
                pass

    # 真实拉一次
    try:
        payload = _fetch_token_plan_raw()
    except Exception as e:
        err = str(e)
        # 失败也缓存（避免瞬时故障时所有请求都打 API 拉新）
        # 缓存短一点（用 CACHE_TTL_SECONDS 一致即可——失败 TTL 和成功 TTL 同）
        _cache_set(None, error=err)
        log.warning(f"token_plan 探测失败（不触发 fallback）: {err}")
        return {
            "ok": False, "error": err, "plan": None,
            "judgment": {"triggered": False, "reasons": ["api_error"]},
        }

    # 写缓存
    _cache_set(payload, error=None)

    try:
        plan = _extract_plan_data(payload)
    except KeyError as e:
        log.warning(f"token_plan 响应结构异常（不触发 fallback）: {e}")
        return {
            "ok": False, "error": f"schema error: {e}", "plan": None,
            "judgment": {"triggered": False, "reasons": ["schema_error"]},
        }

    triggered, judgment = _should_trigger_fallback(plan, threshold)
    judgment["cache_hit"] = False
    return {"ok": True, "error": None, "plan": plan, "judgment": judgment}


def precheck_and_fallback(*, reason: str = "unspecified") -> dict:
    """
    一站式：拉 token plan → 判断阈值 → 触发 sticky fallback（若超阈值）。

    这是给 proxy.py / smart_precompact.py 用的主入口。

    Args:
        reason: 调用方上下文（"proxy-do_post" / "smart_precompact" / "manual"），
                写入日志便于回溯。

    Returns:
        dict: {
            "triggered":   bool,         # 本次调用是否触发了 sticky fallback 写入
            "ok":          bool,         # API 探测是否成功
            "error":       str | None,
            "judgment":    dict,         # _should_trigger_fallback 的结果
            "i_am_first":  bool,         # 触发了 sticky 时，是否是首个写入者
                                        # （首个 → 应在本请求执行 fb retry；
                                        #   非首个 → CC SDK 下一请求会读 sticky）
            "reason":      str,          # 入参 reason 回填
        }
    """
    status = get_token_plan_status()
    judgment = status["judgment"]

    if not status["ok"]:
        log.info(
            f"[token-plan:{reason}] API 探测失败，跳过 fallback: {status['error']}"
        )
        return {
            "triggered": False, "ok": False, "error": status["error"],
            "judgment": judgment, "i_am_first": False, "reason": reason,
        }

    if not judgment.get("triggered"):
        # 不触发——记录一行 debug 日志（不污染 INFO 流）
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                f"[token-plan:{reason}] 余量充足，不触发 fallback: "
                f"5h={judgment.get('interval_remaining_pct')}% "
                f"weekly={judgment.get('weekly_remaining_pct')}%"
            )
        return {
            "triggered": False, "ok": True, "error": None,
            "judgment": judgment, "i_am_first": False, "reason": reason,
        }

    # 触发 sticky fallback
    reasons = ", ".join(judgment.get("reasons", []))
    log.warning(
        f"[token-plan:{reason}] minimax 配额超阈值，触发 sticky fallback: "
        f"5h={judgment.get('interval_remaining_pct')}% "
        f"weekly={judgment.get('weekly_remaining_pct')}% "
        f"reasons=[{reasons}]"
    )

    # 调用 proxy 的 try_write_fallback（原子写 O_CREAT|O_EXCL）
    i_am_first = False
    try:
        # 延迟 import：避免 token_plan 被 smart_precompact 等非 proxy 上下文 import 时
        # 触发 proxy 的全局初始化（_load_env / 日志 / 路由表）。
        from proxy import try_write_fallback  # noqa: E402
        i_am_first = try_write_fallback(PROVIDER_NAME)
    except Exception as e:
        log.error(f"[token-plan:{reason}] 写入 sticky fallback 失败: {e}")
        return {
            "triggered": True, "ok": True, "error": None,
            "judgment": judgment, "i_am_first": False, "reason": reason,
        }

    if i_am_first:
        log.info(
            f"[token-plan:{reason}] sticky fallback 已原子写入，"
            f"后续请求自动走替代 provider"
        )
    else:
        log.info(
            f"[token-plan:{reason}] sticky 已被其他并发请求写入，"
            f"本请求跳过 fb retry（由 CC SDK 决定重试）"
        )

    return {
        "triggered": True, "ok": True, "error": None,
        "judgment": judgment, "i_am_first": i_am_first, "reason": reason,
    }


def clear_cache() -> None:
    """清缓存。~provider reset 时可调用，让下一次 precheck 立即重新拉。"""
    _cache_clear()
    log.info("token_plan 缓存已清空")


__all__ = [
    "get_token_plan_status",
    "precheck_and_fallback",
    "clear_cache",
    "THRESHOLD_PERCENT",
    "CACHE_TTL_SECONDS",
    "PROVIDER_NAME",
    "PLAN_NAME",
]



if __name__ == '__main__':
    get_token_plan_status()