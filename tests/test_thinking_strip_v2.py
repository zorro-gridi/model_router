"""
test_thinking_strip_v2.py — thinking block 加固方案回归测试
===========================================================

针对 2026-06-18 加固后的边界场景做回归测试。补齐 test_thinking_strip.py 覆盖盲点：

  1. system[] 数组型 system prompt 中的 thinking 块（MiniMax 收到会 400）
  2. 响应端 SSE 流（Anthropic streaming 协议）的 content_block_start/delta/stop
     屏蔽 — MiniMax 真实返回就是 SSE
  3. 响应端 error envelope（{"error": {"content": [...]}}）的 thinking 块
  4. 请求头 anthropic-version 在 anthropic-beta 不在场时保留
     （MiniMax 兼容端点要求 version 头存在）

与 test_thinking_strip.py / test_integration_thinking_strip.py 的区别：
  - 单元测试：直接调 _strip_thinking_blocks / _strip_thinking_from_response /
    _clean_headers_for_non_anthropic / _looks_like_sse / _strip_thinking_from_sse
  - 不走 forward_request（避免 urlopen mock 干扰）
"""

import json
import unittest


class TestSystemFieldStrip(unittest.TestCase):
    """system 字段为 array of blocks 时，thinking 块也要剥离。"""

    def test_system_array_thinking_stripped(self):
        from proxy import _strip_thinking_blocks
        body = {
            "system": [
                {"type": "text", "text": "You are a helpful assistant."},
                {"type": "thinking", "thinking": "（不应在 system 出现）"},
                {"type": "redacted_thinking", "data": "x"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        stripped, redacted = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        self.assertEqual(redacted, 1)
        # system 只剩 text
        types = [b.get("type") for b in body["system"]]
        self.assertEqual(types, ["text"])

    def test_system_string_untouched(self):
        """system 为字符串时不动（最常见格式）。"""
        from proxy import _strip_thinking_blocks
        body = {
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        stripped, _ = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 0)
        self.assertEqual(body["system"], "You are a helpful assistant.")

    def test_system_block_with_nested_thinking(self):
        """system[] 中嵌套 tool_result 内嵌 thinking（边缘 case）也要递归清。"""
        from proxy import _strip_thinking_blocks
        body = {
            "system": [
                {
                    "type": "tool_result",
                    "tool_use_id": "sys_t1",
                    "content": [
                        {"type": "text", "text": "system context"},
                        {"type": "thinking", "thinking": "嵌套"},
                    ],
                }
            ],
            "messages": [],
        }
        stripped, _ = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        sys_tc = body["system"][0]
        self.assertEqual(len(sys_tc["content"]), 1)
        self.assertEqual(sys_tc["content"][0]["type"], "text")


class TestResponseSSEStrip(unittest.TestCase):
    """响应端 SSE 流（Anthropic streaming 协议）中的 thinking content_block 屏蔽。"""

    SSE_MESSAGES = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_1","content":[]}}\n'
        '\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","thinking":""}}\n'
        '\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"let me think..."}}\n'
        '\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":" more"}}\n'
        '\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n'
        '\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":1,'
        '"content_block":{"type":"text","text":""}}\n'
        '\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"hello user"}}\n'
        '\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":1}\n'
        '\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n'
        '\n'
    )

    def test_looks_like_sse_true(self):
        from proxy import _looks_like_sse
        self.assertTrue(_looks_like_sse(self.SSE_MESSAGES.encode()))

    def test_looks_like_sse_false_on_json(self):
        from proxy import _looks_like_sse
        plain_json = json.dumps({"content": [{"type": "text", "text": "hi"}]}).encode()
        self.assertFalse(_looks_like_sse(plain_json))

    def test_sse_strips_thinking_blocks_keep_text(self):
        from proxy import _strip_thinking_from_response
        out = _strip_thinking_from_response(
            self.SSE_MESSAGES.encode(), keep_thinking=False
        )
        out_str = out.decode()
        # thinking 相关的 3 帧（start + 2 delta）应被剥离，content_block_stop(index=0) 也应剥离
        self.assertNotIn('"index":0', out_str,
                         f"index=0 的 thinking 帧残留:\n{out_str}")
        # text 帧应保留
        self.assertIn('"index":1', out_str)
        self.assertIn("hello user", out_str)
        # message_start / message_stop / ping 等元事件应保留
        self.assertIn("message_start", out_str)
        self.assertIn("message_stop", out_str)

    def test_sse_keep_thinking_for_deepseek(self):
        """keep_thinking=True 时（SDeepSeek 策略），thinking 帧保留。"""
        from proxy import _strip_thinking_from_response
        out = _strip_thinking_from_response(
            self.SSE_MESSAGES.encode(), keep_thinking=True
        )
        out_str = out.decode()
        # thinking 帧保留
        self.assertIn('"index":0', out_str)
        self.assertIn("let me think...", out_str)
        # redacted_thinking 不在样本里——再构造一个验证 DeepSeek 也保留 thinking
        # 仅确认没有把 thinking 误剥

    def test_sse_sse_with_redacted_thinking_deepseek(self):
        """DeepSeek 策略：保留 thinking，剥 redacted_thinking。"""
        sse = (
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}\n'
            '\n'
            'event: content_block_delta\n'
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"ok"}}\n'
            '\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":0}\n'
            '\n'
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":1,'
            '"content_block":{"type":"redacted_thinking","data":"xxx"}}\n'
            '\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":1}\n'
            '\n'
        )
        from proxy import _strip_thinking_from_response
        # DeepSeek 策略：keep_thinking=True
        out = _strip_thinking_from_response(sse.encode(), keep_thinking=True)
        out_str = out.decode()
        # index=0 (thinking) 应保留
        self.assertIn('"index":0', out_str)
        # index=1 (redacted_thinking) 应被剥
        self.assertNotIn('"index":1', out_str)

    def test_sse_minimax_strips_both(self):
        """MiniMax 策略：thinking + redacted_thinking 全剥。"""
        sse = (
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}\n'
            '\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":0}\n'
            '\n'
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":1,'
            '"content_block":{"type":"redacted_thinking","data":"x"}}\n'
            '\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":1}\n'
            '\n'
        )
        from proxy import _strip_thinking_from_response
        out = _strip_thinking_from_response(sse.encode(), keep_thinking=False)
        out_str = out.decode()
        self.assertNotIn('"index":0', out_str)
        self.assertNotIn('"index":1', out_str)


class TestResponseErrorEnvelopeStrip(unittest.TestCase):
    """上游错误响应 envelope 中的 thinking 块也要清。"""

    def test_error_envelope_with_thinking(self):
        from proxy import _strip_thinking_from_response
        body = json.dumps({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "thinking block invalid",
                "content": [
                    {"type": "thinking", "thinking": "leaked"},
                    {"type": "text", "text": "error details"},
                ],
            }
        }).encode()
        out = _strip_thinking_from_response(body, keep_thinking=False)
        out_data = json.loads(out.decode())
        ec = out_data["error"]["content"]
        types = [b.get("type") for b in ec]
        self.assertNotIn("thinking", types)
        self.assertIn("text", types)

    def test_error_message_nested_content(self):
        """error.message.content[] 嵌套路径。"""
        from proxy import _strip_thinking_from_response
        body = json.dumps({
            "error": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "deep"},
                        {"type": "redacted_thinking", "data": "r"},
                        {"type": "text", "text": "ok"},
                    ],
                }
            }
        }).encode()
        out = _strip_thinking_from_response(body, keep_thinking=False)
        out_data = json.loads(out.decode())
        types = [b.get("type") for b in out_data["error"]["message"]["content"]]
        self.assertEqual(types, ["text"])


class TestVersionHeaderRetention(unittest.TestCase):
    """anthropic-version 在 anthropic-beta 不在场时必须保留（MiniMax 要求）。"""

    def test_version_kept_when_no_beta(self):
        from proxy import _clean_headers_for_non_anthropic
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        cleaned = _clean_headers_for_non_anthropic(headers)
        self.assertIn("anthropic-version", cleaned,
                      "无 anthropic-beta 时应保留 version 头（MiniMax 要求）")
        self.assertEqual(cleaned["anthropic-version"], "2023-06-01")

    def test_version_dropped_when_beta_present(self):
        """anthropic-beta 同时存在时两者都剥（避免 interleaved-thinking 触发 400）。"""
        from proxy import _clean_headers_for_non_anthropic
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-08",
        }
        cleaned = _clean_headers_for_non_anthropic(headers)
        self.assertNotIn("anthropic-beta", cleaned)
        self.assertNotIn("anthropic-version", cleaned)

    def test_case_insensitive(self):
        from proxy import _clean_headers_for_non_anthropic
        headers = {
            "Content-Type": "application/json",
            "Anthropic-Version": "2023-06-01",
        }
        cleaned = _clean_headers_for_non_anthropic(headers)
        # 关键：version 在 + 无 beta → 保留
        self.assertTrue(
            any(k.lower() == "anthropic-version" for k in cleaned),
            f"version 应保留：cleaned={cleaned}"
        )


class TestIntegrationForwardToMiniMaxVersionHeader(unittest.TestCase):
    """端到端验证：转发给 MiniMax 时 anthropic-version 头存在（无 anthropic-beta 场景）。"""

    def test_minimax_keeps_version_when_no_beta(self):
        import os
        from unittest.mock import patch, MagicMock
        from proxy import forward_request

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}]
            }).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        body = json.dumps({
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        # 关键：headers 里只有 version，没有 beta（用户没启用 thinking）
        headers_in = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            # 注意：没有 anthropic-beta
        }

        with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}):
            forward_request(
                method="POST",
                path="/v1/messages",
                headers=headers_in,
                body=body,
                target_base="https://api.minimaxi.com",
                target_model="MiniMax-M3",
                api_key_env="MINIMAX_API_KEY",
                protocol="anthropic",
            )

        fwd = captured["headers"]
        # 必须有 version
        version_keys = [k for k in fwd if k.lower() == "anthropic-version"]
        self.assertTrue(len(version_keys) == 1,
                        f"MiniMax 转发必须带 anthropic-version，实际: {fwd}")
        # 不应有 beta（无启用 thinking）
        beta_keys = [k for k in fwd if k.lower() == "anthropic-beta"]
        self.assertEqual(len(beta_keys), 0,
                         f"无启用 thinking 时不应带 anthropic-beta，实际: {fwd}")


if __name__ == "__main__":
    unittest.main()
