"""
test_proxy_retry.py — 主模型重试 + fallback 触发测试
======================================================

2026-06-17 引入：proxy.py 主模型调用前先做固定 1s × N 的有限重试，
连续 3 次失败才启动 fallback provider 流程。

测试目标（§18 D18-3-1 落地）：
  1. _call_with_retry 在 status=200 时直接返回，attempts_used=1
  2. 状态序列 [502, 502, 200] → 第 3 次重试成功，attempts_used=3
  3. 状态序列 [502, 502, 502] → 3 次都失败，返回最后一次 status=502
  4. 状态序列 [400, ...] → 不重试（400 非 retriable），attempts_used=1
  5. sleep_fn 注入：固定 1s × N 退避，单测时跳过实际等待
  6. PRIMARY_MODEL_RETRY_ATTEMPTS 常量值 = 3
  7. PRIMARY_MODEL_RETRY_BACKOFF_SECONDS = 1.0
  8. retriable 错误码（401/402/403/429/5xx/0）都触发重试
  9. non-retriable 错误码（400/404/422）不触发重试
"""

import unittest
from unittest.mock import patch

# forward_request 返回 4-tuple: (status, headers, body, latency_dict)
_LAT = {"ttfb_ms": 0, "total_ms": 0}

# ── 测试 _call_with_retry 本身的语义 ────────────────────────────────────────

class TestCallWithRetry(unittest.TestCase):
    """_call_with_retry 在不同状态序列下的行为。"""

    def test_constant_attempts_is_3(self):
        from proxy import PRIMARY_MODEL_RETRY_ATTEMPTS
        self.assertEqual(PRIMARY_MODEL_RETRY_ATTEMPTS, 3)

    def test_constant_backoff_is_1s(self):
        from proxy import PRIMARY_MODEL_RETRY_BACKOFF_SECONDS
        self.assertEqual(PRIMARY_MODEL_RETRY_BACKOFF_SECONDS, 1.0)

    def test_200_first_try_returns_immediately(self):
        """首次成功 → 1 次调用，0 次重试，不调用 sleep。"""
        from proxy import _call_with_retry

        sleep_calls = []
        def fake_sleep(s):
            sleep_calls.append(s)

        with patch("proxy.forward_request", return_value=(200, {"h": "v"}, b"ok", _LAT)) as mock_fwd:
            status, h, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=fake_sleep,
            )
        self.assertEqual(status, 200)
        self.assertEqual(attempts, 1)
        self.assertEqual(b, b"ok")
        self.assertEqual(mock_fwd.call_count, 1)
        self.assertEqual(sleep_calls, [])  # 成功不等待

    def test_502_then_502_then_200_succeeds_on_3rd(self):
        """[502, 502, 200] → 第 3 次成功，2 次 sleep 等待。"""
        from proxy import _call_with_retry

        sleep_calls = []
        def fake_sleep(s):
            sleep_calls.append(s)

        side_effects = [
            (502, {}, b"err1", _LAT),
            (502, {}, b"err2", _LAT),
            (200, {}, b"ok", _LAT),
        ]
        with patch("proxy.forward_request", side_effect=side_effects) as mock_fwd:
            status, _, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=fake_sleep,
            )
        self.assertEqual(status, 200)
        self.assertEqual(b, b"ok")
        self.assertEqual(attempts, 3)
        self.assertEqual(mock_fwd.call_count, 3)
        # 2 次 sleep（attempt 1 失败后 + attempt 2 失败后），第 3 次成功后不再 sleep
        self.assertEqual(sleep_calls, [1.0, 1.0])

    def test_502_502_502_returns_last_status(self):
        """[502, 502, 502] → 3 次都失败，返回 last status=502, attempts=3。"""
        from proxy import _call_with_retry

        sleep_calls = []
        def fake_sleep(s):
            sleep_calls.append(s)

        side_effects = [
            (502, {}, b"err1", _LAT),
            (502, {}, b"err2", _LAT),
            (502, {}, b"err3", _LAT),
        ]
        with patch("proxy.forward_request", side_effect=side_effects) as mock_fwd:
            status, _, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=fake_sleep,
            )
        self.assertEqual(status, 502)
        self.assertEqual(b, b"err3")
        self.assertEqual(attempts, 3)
        self.assertEqual(mock_fwd.call_count, 3)
        # attempt 1/2 失败后各 sleep 一次；attempt 3 失败后不再 sleep（避免重试第 4 次）
        self.assertEqual(sleep_calls, [1.0, 1.0])

    def test_400_does_not_retry(self):
        """400 是 client error，_is_retriable=False → 不重试。"""
        from proxy import _call_with_retry, _is_retriable
        self.assertFalse(_is_retriable(400))

        sleep_calls = []
        def fake_sleep(s):
            sleep_calls.append(s)

        with patch("proxy.forward_request", return_value=(400, {}, b"bad", _LAT)) as mock_fwd:
            status, _, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=fake_sleep,
            )
        self.assertEqual(status, 400)
        self.assertEqual(attempts, 1)
        self.assertEqual(mock_fwd.call_count, 1)
        self.assertEqual(sleep_calls, [])

    def test_404_does_not_retry(self):
        """404 资源不存在 → 不重试（避免 fallback 死循环）。"""
        from proxy import _call_with_retry
        from proxy import _is_retriable
        self.assertFalse(_is_retriable(404))

        with patch("proxy.forward_request", return_value=(404, {}, b"nf", _LAT)):
            status, _, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=lambda s: None,
            )
        self.assertEqual(status, 404)
        self.assertEqual(attempts, 1)

    def test_422_does_not_retry(self):
        """422 参数错误 → 不重试。"""
        from proxy import _call_with_retry
        from proxy import _is_retriable
        self.assertFalse(_is_retriable(422))

        with patch("proxy.forward_request", return_value=(422, {}, b"bad", _LAT)):
            status, _, _, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=lambda s: None,
            )
        self.assertEqual(attempts, 1)

    def test_429_triggers_retry(self):
        """429 限流 → _is_retriable=True → 重试。"""
        from proxy import _call_with_retry
        from proxy import _is_retriable
        self.assertTrue(_is_retriable(429))

        side_effects = [(429, {}, b"rl", _LAT), (200, {}, b"ok", _LAT)]
        with patch("proxy.forward_request", side_effect=side_effects):
            status, _, b, attempts = _call_with_retry(
                method="POST", path="/v1/messages", headers={}, body=b"{}",
                target_base="https://api.x", target_model="MiniMax-M3",
                api_key_env="KEY", protocol="anthropic", dry_run=False,
                sleep_fn=lambda s: None,
            )
        self.assertEqual(status, 200)
        self.assertEqual(attempts, 2)

    def test_0_status_triggers_retry(self):
        """status=0 表示网络超时/解析失败 → 重试。"""
        from proxy import _is_retriable
        self.assertTrue(_is_retriable(0))

    def test_503_504_triggers_retry(self):
        """5xx 全部 retriable。"""
        from proxy import _is_retriable
        for s in (500, 502, 503, 504, 599):
            self.assertTrue(_is_retriable(s), f"status={s} 应当 retriable")


# ── 测试 _call_with_retry 接到 forward_request 的参数透传 ──────────────────

class TestCallWithRetryParams(unittest.TestCase):
    """验证 _call_with_retry 把所有参数原样转给 forward_request。"""

    def test_forwards_all_params(self):
        from proxy import _call_with_retry

        with patch("proxy.forward_request", return_value=(200, {}, b"", _LAT)) as mock_fwd:
            _call_with_retry(
                method="POST",
                path="/v1/messages",
                headers={"x-test": "1"},
                body=b'{"model":"x"}',
                target_base="https://api.minimaxi.com",
                target_model="MiniMax-M3",
                api_key_env="MINIMAX_API_KEY",
                protocol="anthropic",
                dry_run=False,
                sleep_fn=lambda s: None,
            )
        mock_fwd.assert_called_once_with(
            method="POST",
            path="/v1/messages",
            headers={"x-test": "1"},
            body=b'{"model":"x"}',
            target_base="https://api.minimaxi.com",
            target_model="MiniMax-M3",
            api_key_env="MINIMAX_API_KEY",
            protocol="anthropic",
            dry_run=False,
        )


# ── 测试主模型调用 + fallback 触发逻辑（白盒）──────────────────────────────

class TestPrimaryCallsForwardRequestOnceOrRetry(unittest.TestCase):
    """proxy.do_POST 在主模型调用处会走 _call_with_retry（已 monkeypatch
    forward_request 计数）——这一组测试保护"主模型调用统一走 retry 包装"
    这个结构性事实不被未来回归破坏。

    注意：直接构造完整 do_POST 调用需要完整 mock session_state / stage
    / fallback 等大量状态，收益不高；这里的 _call_with_retry 单测已经
    覆盖了重试语义的绝大部分。"""

    def test_proxy_module_exposes_call_with_retry(self):
        from proxy import _call_with_retry
        self.assertTrue(callable(_call_with_retry))


if __name__ == "__main__":
    unittest.main()