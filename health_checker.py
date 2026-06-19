"""
health_checker.py — sticky fallback 自动恢复探测 + minimax 配额恢复监控
========================================================================

职责：
  - 当某个 session 写入 sticky fallback 后，定期探测原 provider 是否恢复
  - 探测成功 → 自动清除该 provider 在所有 session 的 sticky fallback 文件
  - 探测失败 → 等待下一周期再试
  - [2026-06-18] minimax 配额恢复监控：轮询 /v1/token_plan/remains API，
    检测周窗口重置 → 自动清除所有 minimax sticky fallback，路由回到 minimax

设计要点：
  - 跑在 proxy.py 守护线程里（proxy 是唯一常驻进程，状态 in-process 可读）
  - 多 proxy 实例通过 fcntl.flock leader election 互斥，避免重复探测
  - 每轮每 provider 最多 1 次探测（节省 API quota）
  - 探测用 max_tokens=1 的最小 POST，绕开主链路 300s 超时
  - 探测成功后清除 sticky 时有 30s grace period，避免清掉用户新写的 sticky

环境变量：
  STAGE_ROUTER_PROBE_ENABLED              (default true)
  STAGE_ROUTER_PROBE_INITIAL_DELAY         (default 7200  = 2h)
  STAGE_ROUTER_PROBE_INTERVAL              (default 600   = 10min)
  STAGE_ROUTER_PROBE_TIMEOUT               (default 5     sec)
  STAGE_ROUTER_QUOTA_RECOVERY_ENABLED      (default true)
  STAGE_ROUTER_QUOTA_CHECK_INTERVAL        (default 60    sec)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# 把 hook_dir 加到 sys.path 以便 import proxy 模块常量
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy import (  # noqa: E402
    AUTO_RECOVERY_GRACE_SECONDS,
    HOOK_DIR,
    KNOWN_PROVIDER_NAMES,
    MODEL_TO_CONFIG,
    MODEL_TO_PROVIDER,
    STAGE_CONFIG,
    forward_request,
    try_write_fallback,
)


def _feed_token_plan_cache(payload, error=None):
    """把 minimax API 结果喂给 token_plan 的 in-process cache（[2026-06-19] 引入）。

    用途：让 health_checker 后台线程成为 token plan 状态的「单一事实源」。
    请求路径上 proxy.do_POST 不再直接调 minimax API，而是调
    token_plan.peek_cached_status()（纯内存读）。本函数确保请求线程
    读到的总是后台线程最近一次 API 调用的结果。

    注意：token_plan 在 health_checker 之后才被 import，且 token_plan 也
    会 import proxy（try_write_fallback），所以这里用包裹函数 + 延迟 import
    避免循环依赖 / 启动期失败。
    """
    try:
        from token_plan import set_external_payload
        set_external_payload(payload, error=error)
    except Exception as e:
        log.debug(f"feed token_plan cache 失败（已忽略，不影响主流程）: {e}")

log = logging.getLogger("stage-router.health-checker")

# ── 配置（环境变量覆盖）───────────────────────────────────────
PROBE_ENABLED = os.environ.get("STAGE_ROUTER_PROBE_ENABLED", "true").lower() in ("1", "true", "yes")
PROBE_INITIAL_DELAY = int(os.environ.get("STAGE_ROUTER_PROBE_INITIAL_DELAY", "7200"))  # 2h
PROBE_INTERVAL = int(os.environ.get("STAGE_ROUTER_PROBE_INTERVAL", "600"))            # 10min
PROBE_TIMEOUT = float(os.environ.get("STAGE_ROUTER_PROBE_TIMEOUT", "5"))              # sec

# ── minimax 配额恢复监控配置 ─────────────────────────────────
QUOTA_RECOVERY_ENABLED = os.environ.get(
    "STAGE_ROUTER_QUOTA_RECOVERY_ENABLED", "true"
).lower() in ("1", "true", "yes")
QUOTA_CHECK_INTERVAL = int(os.environ.get("STAGE_ROUTER_QUOTA_CHECK_INTERVAL", "60"))  # sec
# API 超时（配额 API 一般几百 ms 返回，5s 足够）
QUOTA_API_TIMEOUT_SECONDS = float(
    os.environ.get("STAGE_ROUTER_QUOTA_API_TIMEOUT_SECONDS", "5")
)
# 预恢复窗口：恢复前 10 分钟进入密集轮询，之外完全跳过 API 调用
QUOTA_PRE_RECOVERY_WINDOW_S = int(os.environ.get(
    "STAGE_ROUTER_QUOTA_PRE_RECOVERY_WINDOW_S", "600"
))  # 10 min
# 预恢复窗口内的轮询间隔
QUOTA_PRE_RECOVERY_POLL_S = int(os.environ.get(
    "STAGE_ROUTER_QUOTA_PRE_RECOVERY_POLL_S", "60"
))  # 1 min

# ── 模块状态 ──────────────────────────────────────────────────
HEALTH_LOCK_PATH = HOOK_DIR / "health_check.lock"
_HEALTH_STATUS: dict[str, dict] = {}
_HEALTH_STATUS_LOCK = threading.Lock()
_STOP_EVENT: threading.Event | None = None
_THREAD: threading.Thread | None = None
_LEADER_FD: list = [None]

# 内置循环节奏：5s 短轮询（leader election + 探测调度都在这个粒度）
_TICK_SECONDS = 5

# ── minimax 配额恢复监控状态 ──────────────────────────────────
# 追踪 minimax API 返回的双窗口状态，检测配额恢复并自动清除 sticky。
# 采用 one-shot timer 模式：不可路由时根据 API 返回的 remains_time 设置
# 下次检查时间，避免在漫长的耗尽期内频繁轮询（节省 API 调用）。
_QUOTA_STATE_LOCK = threading.Lock()
_QUOTA_STATE: dict = {
    "last_weekly_end_time": None,     # int|None: 上次的 weekly_end_time (ms unix)
    "last_weekly_status": None,       # int|None: 上次的 current_weekly_status
    "last_interval_status": None,     # int|None: 上次的 current_interval_status
    "last_check_ts": 0.0,             # float: 上次 API 调用时间
    "last_weekly_remains_time": None, # int|None: 上次的 weekly_remains_time (ms)
    "next_check_after": None,         # float|None: 在此时间戳之前跳过 API 调用（one-shot timer）
}


# ── 对外 API ──────────────────────────────────────────────────
def start_health_checker() -> None:
    """proxy main() 调用。PROBE_ENABLED=false 时不启动。"""
    global _STOP_EVENT, _THREAD
    if not PROBE_ENABLED:
        log.info("健康探测已禁用 (STAGE_ROUTER_PROBE_ENABLED=false)")
        return
    _STOP_EVENT = threading.Event()
    _THREAD = threading.Thread(
        target=_health_check_loop,
        name="sticky-health-checker",
        daemon=True,
    )
    _THREAD.start()
    log.info(
        f"健康探测线程已启动: initial_delay={PROBE_INITIAL_DELAY}s, "
        f"interval={PROBE_INTERVAL}s, timeout={PROBE_TIMEOUT}s"
    )
    if QUOTA_RECOVERY_ENABLED:
        log.info(
            f"minimax 配额恢复监控已启用: check_interval={QUOTA_CHECK_INTERVAL}s"
        )


def get_health_status() -> dict:
    """供 /health 端点调用，返回 shallow copy。"""
    with _HEALTH_STATUS_LOCK:
        return {k: dict(v) for k, v in _HEALTH_STATUS.items()}


def get_quota_status() -> dict:
    """供 /health 端点调用，返回配额监控状态 shallow copy。"""
    with _QUOTA_STATE_LOCK:
        return dict(_QUOTA_STATE)


def clear_quota_state() -> None:
    """重置配额监控状态（测试/手动重置用）。"""
    global _QUOTA_STATE
    with _QUOTA_STATE_LOCK:
        _QUOTA_STATE = {
            "last_weekly_end_time": None,
            "last_weekly_status": None,
            "last_interval_status": None,
            "last_check_ts": 0.0,
            "last_weekly_remains_time": None,
            "next_check_after": None,
        }
    log.info("配额监控状态已重置")


# ── 守护线程主循环 ────────────────────────────────────────────
def _health_check_loop() -> None:
    """守护线程主体：5s 短轮询 + leader election + 调度探测。

    每 _TICK_SECONDS 醒一次：
      1. 尝试非阻塞 flock（多 proxy 实例中只有一个执行本轮）
      2. leader 跑 _run_probe_round()：扫描所有 sticky 文件 → 去重 → 探测 → 恢复
      3. leader 跑 _quota_recovery_check()：检测 minimax 配额恢复 → 清除 sticky
      4. 释放锁
    """
    while True:
        if _STOP_EVENT is None or _STOP_EVENT.wait(timeout=_TICK_SECONDS):
            return
        try:
            if not _try_acquire_leader_lock():
                continue
            try:
                _run_probe_round()
                _quota_recovery_check()
            finally:
                _release_leader_lock()
        except Exception as e:
            log.exception(f"健康探测循环异常（已捕获）: {e}")


# ── 单轮探测调度 ──────────────────────────────────────────────
def _run_probe_round() -> None:
    """扫描所有 sticky 文件，按 provider 去重，对到期 provider 探测。

    next_probe_at = failed_at + PROBE_INITIAL_DELAY + k*PROBE_INTERVAL
    （k 取决于已经历多少个探测周期，由 _collect_probe_targets 自行计算）

    探测成功 → 对所有匹配该 provider 的 session 清除 sticky。
    """
    targets = _collect_probe_targets()
    now = time.time()
    providers_to_probe: set[str] = {p for p, _, _, t in targets if now >= t}
    if not providers_to_probe:
        return

    probe_results: dict[str, bool] = {}
    for provider in providers_to_probe:
        if _STOP_EVENT is not None and _STOP_EVENT.is_set():
            break
        probe_results[provider] = _probe_provider(provider)

    for provider, proj, sid, _ in targets:
        if probe_results.get(provider) is True:
            _try_clear_sticky_for_session(proj, sid, provider)


# ── 探测单个 provider ────────────────────────────────────────
def _probe_provider(provider: str) -> bool:
    """构造 max_tokens=1 的最小 POST，复用 forward_request 探测连通性。

    判定：
      200-299    → True  （恢复）
      4xx 非 429 → True  （网络/鉴权可达，业务错误不阻塞路由）
      429        → False （限流，下次再探）
      5xx/timeout → False （未恢复）
    """
    cfg = _find_provider_config(provider)
    if not cfg:
        log.debug(f"provider={provider} 无配置，跳过探测")
        return False
    base_url, target_model, api_key_env, protocol = cfg
    if not os.environ.get(api_key_env):
        log.debug(f"provider={provider} 缺 API key env={api_key_env}，跳过探测")
        return False

    ping_body = json.dumps({
        "model": target_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()

    started = time.time()
    try:
        status, _, _ = forward_request(
            method="POST",
            path="/v1/messages",
            headers={"Content-Type": "application/json"},
            body=ping_body,
            target_base=base_url,
            target_model=target_model,
            api_key_env=api_key_env,
            protocol=protocol,
            timeout=PROBE_TIMEOUT,
        )
    except Exception as e:
        _update_health_status(
            provider, ok=False, status=0,
            latency_ms=int((time.time() - started) * 1000),
            error=str(e),
        )
        return False

    latency_ms = int((time.time() - started) * 1000)
    if 200 <= status < 300:
        _update_health_status(provider, ok=True, status=status, latency_ms=latency_ms)
        return True
    if status == 429:
        _update_health_status(
            provider, ok=False, status=status, latency_ms=latency_ms,
            error="rate_limited",
        )
        return False
    if 400 <= status < 500:
        # 4xx 非 429：网络可达但请求被业务拒绝（如无效 prompt）。
        # 视为可路由，触发 auto-recovery（避免一直被 stuck 在 fb）。
        _update_health_status(
            provider, ok=True, status=status, latency_ms=latency_ms,
            error="4xx_reachable",
        )
        return True
    _update_health_status(provider, ok=False, status=status, latency_ms=latency_ms)
    return False


# ── Auto-recovery：清除 sticky ─────────────────────────────────
def _try_clear_sticky_for_session(
    project_root: Path,
    session_id: str,
    recovered_provider: str,
) -> None:
    """仅当 sticky 仍指向已恢复的 provider 且 failed_at 超过 grace period，清除之。

    grace period 目的：探测发现恢复时若 sticky 刚写入，跳过清除——避免清掉
    探测期间用户新触发的 sticky（罕见但可能）。
    """
    fb_path = project_root / ".claude" / f"fallback_{session_id}"
    if not fb_path.exists():
        return
    try:
        raw = fb_path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            if data.get("provider") != recovered_provider:
                return
            if int(time.time()) - int(data.get("failed_at", 0)) < AUTO_RECOVERY_GRACE_SECONDS:
                log.debug(
                    f"session={session_id} sticky 刚写 < "
                    f"{AUTO_RECOVERY_GRACE_SECONDS}s，跳过 auto-recovery"
                )
                return
        fb_path.unlink()
        log.info(
            f"auto-recovery: provider {recovered_provider} 已恢复，"
            f"已自动清除 session={session_id} 的 sticky fallback"
        )
    except OSError as e:
        log.error(f"auto-recovery 清除 sticky 失败: {e}")


# ── minimax 配额恢复监控 ──────────────────────────────────────
def _quota_recovery_check() -> None:
    """检测 minimax 配额是否恢复（双窗口联合判断 → 清除所有 minimax sticky）。

    设计依据（2026-06-18）：
      minimax 有双重配额限制——5h 滚动窗口 + 周窗口，必须同时可用才能路由。
      /v1/token_plan/remains API 返回两个窗口的 status 和 remains_time，
      据此判断是否可路由，并在恢复时自动清除 sticky。

    提前密集轮询模式（2026-06-18）：
      当 minimax 不可路由时，API 返回的 remains_time（5h 恢复剩余 ms）和
      weekly_remains_time（周恢复剩余 ms）告知确切的恢复时间点。
      据此设置 next_check_after：
        - 距恢复 >10 分钟：休眠到恢复前 10 分钟（next_check_after = recovery - 10min），
          完全跳过 API 调用，避免漫长耗尽期（如 76h）的无意义轮询
        - 距恢复 ≤10 分钟：next_check_after = now + 60s，进入每 60s 轮询模式，
          一旦检测到恢复立即清除 sticky，感知延迟 ≤ 60s

      可路由时回退到 QUOTA_CHECK_INTERVAL 正常频率（检测新的耗尽）。

    触发条件（双窗口同时可用时触发恢复）：
      routable = (interval_status==1 AND weekly_status==1)
      recovery = was_routable==False → routable==True

    安全性：
      - API 调用失败 → 静默跳过（fail-safe：不误清 sticky）
      - 仅清除 provider=="minimax" 的 sticky（不误删 deepseek 等）
      - 复用 try_clear_sticky_for_session 的 grace period 校验
    """
    if not QUOTA_RECOVERY_ENABLED:
        return

    with _QUOTA_STATE_LOCK:
        now = time.time()
        # One-shot timer：在预定恢复时间之前，跳过所有 API 调用
        next_check = _QUOTA_STATE["next_check_after"]
        if next_check is not None and now < next_check:
            return
        # 正常频率控制（可路由时或 timer 到期后）
        if now - _QUOTA_STATE["last_check_ts"] < QUOTA_CHECK_INTERVAL:
            return
        _QUOTA_STATE["last_check_ts"] = now

    # 调用 minimax token plan API
    try:
        api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key:
            log.debug("配额恢复检查: MINIMAX_API_KEY 未设置，跳过")
            return

        api_url = os.environ.get(
            "MINIMAX_TOKEN_PLAN_URL",
            "https://www.minimaxi.com/v1/token_plan/remains",
        )
        req = urllib.request.Request(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=QUOTA_API_TIMEOUT_SECONDS) as resp:
            body = resp.read()
        payload = json.loads(body)
    except Exception as e:
        log.debug(f"配额恢复检查 API 失败（已忽略）: {e}")
        # [2026-06-19] 即使 API 失败也把失败状态喂给 token_plan cache，
        # 让请求路径 peek_cached_status() 返回 None（与"无数据"等价），
        # 而不是去触发另一次独立 IO（修复前架构的痛点）。
        _feed_token_plan_cache(payload=None, error=str(e))
        return

    if not isinstance(payload, dict):
        log.debug("配额恢复检查: API 响应非 dict，跳过")
        _feed_token_plan_cache(payload=None, error="non_dict_response")
        return

    # [2026-06-19] 单一事实源：API 拿到 payload 后立即喂 token_plan cache。
    # 请求路径上的 peek_cached_status() 就能零 IO 命中——这是修复
    # 「轮询进程阻塞主 proxy」的关键。schema 校验放在喂缓存之后，
    # 让 cache 总是反映「最近一次 API 调用结果」，便于日志/metrics 追溯。
    _feed_token_plan_cache(payload=payload)

    # 提取 general 套餐数据（minimax LLM 路由只用 general 套餐）
    model_remains = payload.get("model_remains")
    if not isinstance(model_remains, list):
        log.debug("配额恢复检查: model_remains 缺失或非 list，跳过")
        return

    plan = None
    for entry in model_remains:
        if isinstance(entry, dict) and entry.get("model_name") == "general":
            plan = entry
            break
    if plan is None:
        log.debug("配额恢复检查: 未找到 general 套餐，跳过")
        return

    weekly_end_time = plan.get("weekly_end_time")
    weekly_status = plan.get("current_weekly_status")
    interval_status = plan.get("current_interval_status")
    weekly_remains_time = plan.get("weekly_remains_time")
    interval_remains_time = plan.get("remains_time")      # 5h 滚动窗口恢复剩余 ms

    # 读取旧状态 + 更新
    with _QUOTA_STATE_LOCK:
        prev_end_time = _QUOTA_STATE["last_weekly_end_time"]
        prev_weekly = _QUOTA_STATE["last_weekly_status"]
        prev_interval = _QUOTA_STATE["last_interval_status"]

        # ── 核心判断：minimax 是否"可路由" ──
        routable = (
            interval_status == 1 and weekly_status == 1
        )

        is_first_run = prev_weekly is None or prev_interval is None
        was_routable = (
            prev_interval == 1 and prev_weekly == 1
        ) if not is_first_run else None

        recovered = was_routable is False and routable is True

        reasons: list[str] = []
        if recovered:
            interval_recovered = (
                prev_interval is not None and prev_interval != 1 and interval_status == 1
            )
            weekly_recovered = (
                prev_weekly is not None and prev_weekly != 1 and weekly_status == 1
            )
            if weekly_end_time is not None and prev_end_time is not None and weekly_end_time != prev_end_time:
                reasons.append(
                    f"weekly_end_time changed: {prev_end_time} → {weekly_end_time}"
                )
            if interval_recovered:
                reasons.append(
                    f"interval_status recovered: {prev_interval} → {interval_status}"
                )
            if weekly_recovered:
                reasons.append(
                    f"weekly_status recovered: {prev_weekly} → {weekly_status}"
                )

        # ── 提前密集轮询调度：恢复前 10min 每 60s 轮询，之外完全跳过 ──
        if routable:
            # 可路由：清除定时器，回退到正常 QUOTA_CHECK_INTERVAL 频率
            if _QUOTA_STATE["next_check_after"] is not None:
                log.debug("minimax 已可路由，清除提前轮询 timer")
            _QUOTA_STATE["next_check_after"] = None
        else:
            # 不可路由：取最近恢复时间（毫秒 → 秒）
            recovery_times: list[float] = []
            if interval_status == 2 and isinstance(interval_remains_time, (int, float)) and interval_remains_time > 0:
                recovery_times.append(interval_remains_time / 1000.0)
            if weekly_status == 2 and isinstance(weekly_remains_time, (int, float)) and weekly_remains_time > 0:
                recovery_times.append(weekly_remains_time / 1000.0)

            if recovery_times:
                shortest_remain_s = min(recovery_times)
                if shortest_remain_s > QUOTA_PRE_RECOVERY_WINDOW_S:
                    # 距恢复 > 10min → 休眠到 recovery - 10min
                    wait_s = shortest_remain_s - QUOTA_PRE_RECOVERY_WINDOW_S
                    _QUOTA_STATE["next_check_after"] = now + wait_s
                    log.info(
                        f"minimax 不可路由，距恢复 {shortest_remain_s/60:.1f}min："
                        f"休眠到恢复前 10min（{wait_s/60:.1f} min 后唤醒）"
                    )
                else:
                    # 距恢复 ≤ 10min → 进入每 60s 轮询模式
                    _QUOTA_STATE["next_check_after"] = now + QUOTA_PRE_RECOVERY_POLL_S
                    log.info(
                        f"minimax 不可路由，距恢复 {shortest_remain_s/60:.1f}min："
                        f"进入预恢复轮询（每 {QUOTA_PRE_RECOVERY_POLL_S}s 一次）"
                    )
            else:
                # 无法确定恢复时间（status=3 饱和状态等）→ 保持 QUOTA_CHECK_INTERVAL 轮询
                _QUOTA_STATE["next_check_after"] = None

        # 更新跟踪状态
        _QUOTA_STATE["last_weekly_end_time"] = weekly_end_time
        _QUOTA_STATE["last_weekly_status"] = weekly_status
        _QUOTA_STATE["last_interval_status"] = interval_status
        _QUOTA_STATE["last_weekly_remains_time"] = weekly_remains_time

    if recovered:
        log.warning(
            f"minimax 配额已恢复（5h+周双窗口均可用）"
            + (f": {'; '.join(reasons)}" if reasons else "")
        )
        cleared = _clear_all_minimax_stickies()
        log.info(f"配额恢复 auto-clean: 已清除 {cleared} 个 minimax sticky 文件")
    elif routable and is_first_run:
        log.debug(
            f"配额恢复监控基线已记录: interval_status={interval_status}, "
            f"weekly_status={weekly_status}, weekly_end_time={weekly_end_time}"
        )
    elif not routable:
        # [2026-06-19] 主动写 sticky（替代修复前「请求路径 precheck 写 sticky」）。
        # 修复前：每次请求都跑 precheck_and_fallback() → 同步 5s urllib +
        #         try_write_fallback()。precheck 已删除，改由后台线程负责写。
        # 行为：minimax 不可路由时，对当前 active session 写 sticky；O_EXCL
        #       保证并发安全（多个 health_checker 实例也不会重复写）。
        # 注意：try_write_fallback 走 _active_stage_path()，仅影响当前
        #       active session；其他 session 走原始 429 → _is_retriable 链路。
        #       这与修复前 precheck_and_fallback 的行为一致（修复前也是
        #       每个 session 在自己的 do_POST 里调 precheck 才写自己的 sticky）。
        try:
            wrote = try_write_fallback("minimax")
            if wrote:
                log.info(
                    f"配额耗尽 auto-pre-fallback: 已为 active session 写 minimax sticky，"
                    f"后续请求自动走替代 provider（无需等 429）"
                )
        except Exception as e:
            log.debug(f"配额耗尽 proactive sticky 写入失败（不影响主流程）: {e}")

        parts: list[str] = []
        if interval_status == 2:
            if isinstance(interval_remains_time, (int, float)) and interval_remains_time > 0:
                parts.append(f"5h_window=exhausted(reset_in_{interval_remains_time/60000:.0f}min)")
            else:
                parts.append("5h_window=exhausted")
        elif interval_status == 3:
            parts.append("5h_window=unused/saturated")
        if weekly_status == 2:
            remaining_s = (weekly_remains_time or 0) / 1000.0
            parts.append(f"weekly=exhausted(reset_in_{remaining_s/3600:.1f}h)")
        elif weekly_status == 3:
            parts.append("weekly=unused/saturated")
        if parts:
            log.debug(f"minimax 不可路由: {', '.join(parts)}")
        elif not is_first_run:
            log.debug(
                f"minimax 不可路由: interval_status={interval_status}, "
                f"weekly_status={weekly_status}"
            )


def _clear_all_minimax_stickies() -> int:
    """扫描所有 session 的 sticky fallback 文件，仅清除 provider=="minimax" 的。

    扫描路径（与 _collect_probe_targets 对齐）：
      1. state_index.json → 所有活跃 session → fallback_<sid>
      2. 兜底：globl HOOK_DIR/../*.claude/fallback_*

    每个 sticky 清除前校验 grace period（AUTO_RECOVERY_GRACE_SECONDS），
    避免删除刚写入的 sticky。

    Returns:
        int: 实际清除的 sticky 文件数。
    """
    cleared = 0
    seen_claude_dirs: set[Path] = set()

    # ── 主路径：state_index.json → 所有活跃 session ──
    state_index_path = HOOK_DIR / "state_index.json"
    try:
        if state_index_path.exists():
            data = json.loads(state_index_path.read_text(encoding="utf-8"))
            for path_key, entry in data.items():
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
                if not fb_path.exists():
                    continue
                if _is_minimax_sticky(fb_path):
                    try:
                        fb_path.unlink()
                        cleared += 1
                        log.info(
                            f"配额恢复: 已清除 minimax sticky "
                            f"session={sid} ({fb_path})"
                        )
                    except OSError as e:
                        log.error(f"配额恢复: 清除 {fb_path} 失败: {e}")
    except (OSError, json.JSONDecodeError) as e:
        log.debug(f"配额恢复: 扫描 state_index.json 失败: {e}")

    # ── 兜底：扫已知 .claude 目录里所有 fallback_* ──
    for claude_dir in seen_claude_dirs:
        if not claude_dir.is_dir():
            continue
        try:
            for fb_path in claude_dir.glob("fallback_*"):
                if not fb_path.is_file():
                    continue
                if _is_minimax_sticky(fb_path):
                    try:
                        fb_path.unlink()
                        cleared += 1
                        log.info(
                            f"配额恢复(兜底): 已清除 minimax sticky ({fb_path})"
                        )
                    except OSError as e:
                        log.error(f"配额恢复(兜底): 清除 {fb_path} 失败: {e}")
        except OSError as e:
            log.debug(f"配额恢复: 扫描 {claude_dir} 失败: {e}")

    return cleared


def _is_minimax_sticky(fb_path: Path) -> bool:
    """判断 sticky 文件是否指向 minimax（且满足 grace period 要求）。

    只清除确认是 minimax sticky 且超过 grace period 的文件，
    避免误删 deepseek 等其他 provider 的 sticky。
    """
    try:
        raw = fb_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return False

    provider = None
    failed_at = 0
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            provider = data.get("provider")
            failed_at = int(data.get("failed_at", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    elif raw in KNOWN_PROVIDER_NAMES:
        provider = raw
        failed_at = int(fb_path.stat().st_mtime)
    else:
        return False

    if provider != "minimax":
        return False

    # Grace period：仅 JSON 格式（有显式 failed_at 字段）做校验。
    # v2 纯文本格式无 failed_at，用 st_mtime 代替，但旧格式文件本质已存在较久，
    # 不应用 grace period（否则测试/首次扫描会误保留）。
    if raw.startswith("{") and int(time.time()) - failed_at < AUTO_RECOVERY_GRACE_SECONDS:
        log.debug(
            f"minimax sticky {fb_path} 刚写 < "
            f"{AUTO_RECOVERY_GRACE_SECONDS}s，跳过配额恢复清除"
        )
        return False

    return True


# ── Leader election（fcntl flock，非阻塞） ─────────────────────
def _try_acquire_leader_lock() -> bool:
    """非阻塞 flock。返回 True=本轮 leader，False=其他实例在跑。"""
    try:
        fd = os.open(str(HEALTH_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LEADER_FD[0] = fd
        return True
    except (BlockingIOError, OSError):
        return False


def _release_leader_lock() -> None:
    fd = _LEADER_FD[0]
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _LEADER_FD[0] = None


# ── 辅助函数 ──────────────────────────────────────────────────
def _find_provider_config(provider: str):
    """遍历 STAGE_CONFIG 找第一个 MODEL_TO_PROVIDER[model] == provider 的条目。

    Returns:
        (base_url, model, api_key_env, protocol) 或 None
    """
    for cfg in STAGE_CONFIG.values():
        if MODEL_TO_PROVIDER.get(cfg["model"]) == provider:
            return (
                cfg["base_url"], cfg["model"],
                cfg["api_key_env"], cfg["protocol"],
            )
    # 兜底：用 MODEL_TO_CONFIG（独立 dict）
    for model, mc in MODEL_TO_CONFIG.items():
        if MODEL_TO_PROVIDER.get(model) == provider:
            return (
                mc[0], model,
                mc[2], mc[3],
            )
    return None


def _collect_probe_targets():
    """扫描所有活跃 sticky 文件，返回探测目标列表。

    Returns:
        [(provider, project_root, session_id, next_probe_at), ...]

    next_probe_at 计算：
      failed_at + PROBE_INITIAL_DELAY（首次）
      failed_at + PROBE_INITIAL_DELAY + k * PROBE_INTERVAL（k 由
        _probe_state(provider, project_root) 跟踪；当前实现简化为
        "failed_at + INITIAL_DELAY"——每轮只要 time>=next_probe_at 就
        再探一次，相当于节流到 PROBE_INTERVAL 周期）
    """
    targets: list[tuple[str, Path, str, float]] = []
    seen: set[str] = set()  # (project_root|session_id) 去重

    # 主路径：state_index.json
    state_index_path = HOOK_DIR / "state_index.json"
    try:
        if state_index_path.exists():
            data = json.loads(state_index_path.read_text(encoding="utf-8"))
            for path_key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("session_id")
                if not sid:
                    continue
                try:
                    proj = Path(path_key)
                except (TypeError, ValueError):
                    continue
                key = f"{proj}|{sid}"
                if key in seen:
                    continue
                seen.add(key)
                fb_path = proj / ".claude" / f"fallback_{sid}"
                if not fb_path.exists():
                    continue
                target = _parse_sticky_target(fb_path, proj, sid)
                if target:
                    targets.append(target)
    except (OSError, json.JSONDecodeError) as e:
        log.debug(f"扫描 state_index.json 失败: {e}")

    # 兜底：扫 HOOK_DIR 同级所有 .claude/fallback_*（防止 state_index 漏报）
    for fb_path in HOOK_DIR.parent.glob(".claude/fallback_*"):
        pass  # 已由主路径覆盖；这里只兜底跨 project_root 的 sticky
    return targets


def _parse_sticky_target(fb_path: Path, project_root: Path, session_id: str):
    """解析单个 sticky 文件，返回 (provider, project_root, session_id, next_probe_at) 或 None。"""
    try:
        raw = fb_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None

    # JSON 格式（v3）
    provider = None
    failed_at = 0
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            provider = data.get("provider")
            failed_at = int(data.get("failed_at", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
    elif raw in KNOWN_PROVIDER_NAMES:
        # v2 旧格式
        provider = raw
        failed_at = int(fb_path.stat().st_mtime)
    else:
        # v1 旧格式或其他不可识别 → 跳过
        return None

    if not provider or provider not in KNOWN_PROVIDER_NAMES or failed_at <= 0:
        return None

    next_probe_at = failed_at + PROBE_INITIAL_DELAY
    return (provider, project_root, session_id, float(next_probe_at))


def _update_health_status(
    provider: str,
    ok: bool,
    status: int,
    latency_ms: int,
    error: str | None = None,
) -> None:
    """更新 _HEALTH_STATUS 字典（线程安全）。"""
    now = int(time.time())
    with _HEALTH_STATUS_LOCK:
        entry = _HEALTH_STATUS.setdefault(provider, {
            "consecutive_failures": 0,
            "last_error": None,
        })
        if ok:
            entry["last_ok_ts"] = now
            entry["consecutive_failures"] = 0
        else:
            entry["last_fail_ts"] = now
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        entry["last_status"] = status
        entry["last_latency_ms"] = latency_ms
        if error is not None:
            entry["last_error"] = error
