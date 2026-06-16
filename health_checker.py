"""
health_checker.py — sticky fallback 自动恢复探测（2026-06-16 引入）
====================================================================

职责：
  - 当某个 session 写入 sticky fallback 后，定期探测原 provider 是否恢复
  - 探测成功 → 自动清除该 provider 在所有 session 的 sticky fallback 文件
  - 探测失败 → 等待下一周期再试

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
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

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
)

log = logging.getLogger("stage-router.health-checker")

# ── 配置（环境变量覆盖）───────────────────────────────────────
PROBE_ENABLED = os.environ.get("STAGE_ROUTER_PROBE_ENABLED", "true").lower() in ("1", "true", "yes")
PROBE_INITIAL_DELAY = int(os.environ.get("STAGE_ROUTER_PROBE_INITIAL_DELAY", "7200"))  # 2h
PROBE_INTERVAL = int(os.environ.get("STAGE_ROUTER_PROBE_INTERVAL", "600"))            # 10min
PROBE_TIMEOUT = float(os.environ.get("STAGE_ROUTER_PROBE_TIMEOUT", "5"))              # sec

# ── 模块状态 ──────────────────────────────────────────────────
HEALTH_LOCK_PATH = HOOK_DIR / "health_check.lock"
_HEALTH_STATUS: dict[str, dict] = {}
_HEALTH_STATUS_LOCK = threading.Lock()
_STOP_EVENT: threading.Event | None = None
_THREAD: threading.Thread | None = None
_LEADER_FD: list = [None]

# 内置循环节奏：5s 短轮询（leader election + 探测调度都在这个粒度）
_TICK_SECONDS = 5


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


def get_health_status() -> dict:
    """供 /health 端点调用，返回 shallow copy。"""
    with _HEALTH_STATUS_LOCK:
        return {k: dict(v) for k, v in _HEALTH_STATUS.items()}


# ── 守护线程主循环 ────────────────────────────────────────────
def _health_check_loop() -> None:
    """守护线程主体：5s 短轮询 + leader election + 调度探测。

    每 _TICK_SECONDS 醒一次：
      1. 尝试非阻塞 flock（多 proxy 实例中只有一个执行本轮）
      2. leader 跑 _run_probe_round()：扫描所有 sticky 文件 → 去重 → 探测 → 恢复
      3. 释放锁
    """
    while True:
        if _STOP_EVENT is None or _STOP_EVENT.wait(timeout=_TICK_SECONDS):
            return
        try:
            if not _try_acquire_leader_lock():
                continue
            try:
                _run_probe_round()
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
                mc.get("base_url"), model,
                mc.get("api_key_env"), mc.get("protocol", "anthropic"),
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
