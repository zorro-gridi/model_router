"""
test_thinking_strip.py — thinking block 降级策略单元测试
==========================================================

验证 _strip_thinking_blocks / _clean_headers_for_non_anthropic 的行为：

1. 消息历史中 type=thinking / redacted_thinking block 被剥离
2. tool_result 内嵌 content[] 的递归清理
3. 字符串 content 不受影响
4. 空列表 case
5. 请求头清理
6. is_anthropic_model 判定（claude- 前缀 → 跳过降级）
"""

import json
import unittest


class TestStripThinkingBlocks(unittest.TestCase):
    """_strip_thinking_blocks 的消息清洗行为。"""

    def test_strips_thinking_and_redacted_thinking(self):
        """普通 content[] 中的 thinking / redacted_thinking block 被过滤。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "内部推理", "signature": "sig1"},
                        {"type": "redacted_thinking", "data": "xxx"},
                        {"type": "text", "text": "回复文本"},
                    ],
                },
            ]
        }
        stripped, redacted = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        self.assertEqual(redacted, 1)
        # assistant 只剩 text block
        self.assertEqual(len(body["messages"][1]["content"]), 1)
        self.assertEqual(body["messages"][1]["content"][0]["type"], "text")

    def test_recursive_tool_result(self):
        """tool_result.content[] 内嵌 thinking 块被递归清除。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": [
                                {"type": "text", "text": "ok"},
                                {"type": "thinking", "thinking": "嵌套推理"},
                            ],
                        }
                    ],
                }
            ]
        }
        stripped, _ = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        tc = body["messages"][0]["content"][0]
        self.assertEqual(len(tc["content"]), 1)
        self.assertEqual(tc["content"][0]["type"], "text")

    def test_string_content_untouched(self):
        """content 是字符串时不报错，原样保留。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {"role": "user", "content": "plain text"},
                {"role": "assistant", "content": "reply text"},
            ]
        }
        stripped, _ = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 0)
        self.assertEqual(body["messages"][0]["content"], "plain text")

    def test_empty_content_list_preserved(self):
        """过滤后内容为空列表时保留空 list（不删除整条 message）。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "全部推理"},
                        {"type": "redacted_thinking", "data": "x"},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                    ],
                },
            ]
        }
        _strip_thinking_blocks(body)
        # assistant content 保留为 []
        self.assertEqual(body["messages"][0]["content"], [])
        # 整条 message 还在
        self.assertEqual(len(body["messages"]), 2)

    def test_non_dict_blocks_preserved(self):
        """非 dict 类型的 block（如纯字符串）也被原样保留。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "ok"},
                        "a plain string block",
                        {"type": "thinking", "thinking": "x"},
                    ],
                }
            ]
        }
        stripped, _ = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        c = body["messages"][0]["content"]
        self.assertEqual(len(c), 2)
        self.assertEqual(c[0]["type"], "text")
        self.assertEqual(c[1], "a plain string block")


class TestDeepSeekKeepThinking(unittest.TestCase):
    """keep_thinking=True（DeepSeek 策略）：保留 thinking 块，仅剥 redacted_thinking。"""

    def test_preserves_thinking_blocks(self):
        """keep_thinking=True：type='thinking' 块原样保留（DeepSeek 支持）。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "内部推理", "signature": "sig1"},
                        {"type": "redacted_thinking", "data": "xxx"},
                        {"type": "text", "text": "回复文本"},
                    ],
                },
            ]
        }
        stripped, redacted = _strip_thinking_blocks(body, keep_thinking=True)
        self.assertEqual(stripped, 0)  # thinking 保留
        self.assertEqual(redacted, 1)  # redacted 剥离
        # assistant 剩 thinking + text 两个
        self.assertEqual(len(body["messages"][1]["content"]), 2)

    def test_strips_redacted_thinking_only(self):
        """keep_thinking=True：redacted_thinking 仍被剥离。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "xxx"},
                    ],
                },
            ]
        }
        stripped, redacted = _strip_thinking_blocks(body, keep_thinking=True)
        self.assertEqual(stripped, 0)
        self.assertEqual(redacted, 1)
        self.assertEqual(body["messages"][0]["content"], [])

    def test_keep_thinking_recursive_tool_result(self):
        """keep_thinking=True + tool_result 内嵌：保留 thinking，剥 redacted。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": [
                                {"type": "text", "text": "ok"},
                                {"type": "thinking", "thinking": "嵌套推理"},
                                {"type": "redacted_thinking", "data": "x"},
                            ],
                        }
                    ],
                }
            ]
        }
        stripped, redacted = _strip_thinking_blocks(body, keep_thinking=True)
        self.assertEqual(stripped, 0)  # thinking 保留
        self.assertEqual(redacted, 1)  # redacted 剥离
        tc = body["messages"][0]["content"][0]
        # text + thinking 保留，redacted 已剥
        self.assertEqual(len(tc["content"]), 2)
        types = [b["type"] for b in tc["content"] if isinstance(b, dict)]
        self.assertIn("thinking", types)
        self.assertNotIn("redacted_thinking", types)

    def test_default_keep_thinking_false(self):
        """默认 keep_thinking=False → 两者全剥。"""
        from proxy import _strip_thinking_blocks
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "x"},
                        {"type": "redacted_thinking", "data": "y"},
                        {"type": "text", "text": "z"},
                    ],
                },
            ]
        }
        stripped, redacted = _strip_thinking_blocks(body)
        self.assertEqual(stripped, 1)
        self.assertEqual(redacted, 1)
        self.assertEqual(len(body["messages"][0]["content"]), 1)


class TestCleanHeaders(unittest.TestCase):
    """_clean_headers_for_non_anthropic 的请求头清理行为。"""

    def test_strips_anthropic_beta(self):
        from proxy import _clean_headers_for_non_anthropic
        h = {
            "content-type": "application/json",
            "anthropic-beta": "interleaved-thinking-2025",
            "x-api-key": "sk-test",
        }
        cleaned = _clean_headers_for_non_anthropic(h)
        self.assertNotIn("anthropic-beta", cleaned)
        self.assertIn("content-type", cleaned)
        self.assertIn("x-api-key", cleaned)

    def test_strips_anthropic_version(self):
        from proxy import _clean_headers_for_non_anthropic
        h = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        cleaned = _clean_headers_for_non_anthropic(h)
        self.assertNotIn("anthropic-version", cleaned)
        self.assertIn("content-type", cleaned)

    def test_case_insensitive_strip(self):
        from proxy import _clean_headers_for_non_anthropic
        h = {"Anthropic-Beta": "v1", "Anthropic-Version": "2023"}
        cleaned = _clean_headers_for_non_anthropic(h)
        self.assertEqual(cleaned, {})

    def test_unrelated_headers_preserved(self):
        from proxy import _clean_headers_for_non_anthropic
        h = {"x-custom": "val", "authorization": "bearer x"}
        cleaned = _clean_headers_for_non_anthropic(h)
        self.assertEqual(cleaned, h)


class TestModelTierClassification(unittest.TestCase):
    """三层模型分类：claude-* (全透传) / deepseek-* (保留thinking) / 其它 (全剥)。"""

    def test_claude_models_are_tier1(self):
        """claude-* 前缀 → 全透传，不剥任何内容。"""
        for name in [
            "claude-sonnet-4-6",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
            "claude-fable-5",
        ]:
            self.assertTrue(name.startswith("claude-"), f"{name} should be claude tier")

    def test_deepseek_models_are_tier2(self):
        """deepseek-* 前缀 → 保留 thinking，仅剥 redacted_thinking。"""
        for name in [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ]:
            self.assertTrue(name.startswith("deepseek-"), f"{name} should be deepseek tier")
            self.assertFalse(name.startswith("claude-"), f"{name} should NOT be claude tier")

    def test_other_models_are_tier3(self):
        """非 claude-* 非 deepseek-* → 全剥。"""
        for name in [
            "minimax-m3",
            "MiniMax-M3",
            "gpt-4o",
            "",
        ]:
            is_claude = name.startswith("claude-")
            is_deepseek = name.startswith("deepseek-")
            self.assertFalse(is_claude or is_deepseek,
                             f"{name!r} should be tier 3 (full strip)")


class TestTopLevelThinkingAndBetasPop(unittest.TestCase):
    """forward_request 中对非 claude-* 模型删除 thinking / betas 顶层字段。

    注意：DeepSeek 虽保留 thinking 块，但顶层 thinking/betas 仍 pop——
    DeepSeek 文档明确这两个字段被忽略（不生效），pop 无害。
    """

    def test_thinking_and_betas_removed_for_minimax(self):
        """minimax-m3 → thinking + betas 被 pop。"""
        body = {
            "model": "minimax-m3",
            "thinking": {"type": "enabled", "budget_tokens": 16000},
            "betas": ["interleaved-thinking-2025-05-08"],
            "messages": [{"role": "user", "content": "hi"}],
        }
        is_claude = body["model"].startswith("claude-")
        self.assertFalse(is_claude)
        if not is_claude:
            body.pop("thinking", None)
            body.pop("betas", None)
        self.assertNotIn("thinking", body)
        self.assertNotIn("betas", body)

    def test_thinking_and_betas_removed_for_deepseek(self):
        """deepseek-v4-pro → thinking + betas 也被 pop（DeepSeek 忽略这些字段）。"""
        body = {
            "model": "deepseek-v4-pro",
            "thinking": {"type": "enabled", "budget_tokens": 16000},
            "betas": ["interleaved-thinking-2025-05-08"],
            "messages": [{"role": "user", "content": "hi"}],
        }
        is_claude = body["model"].startswith("claude-")
        self.assertFalse(is_claude)
        if not is_claude:
            body.pop("thinking", None)
            body.pop("betas", None)
        self.assertNotIn("thinking", body)
        self.assertNotIn("betas", body)

    def test_thinking_and_betas_kept_for_claude_model(self):
        """claude-sonnet-4-6 → thinking + betas 保留。"""
        body = {
            "model": "claude-sonnet-4-6",
            "thinking": {"type": "enabled", "budget_tokens": 16000},
            "betas": ["interleaved-thinking"],
            "messages": [{"role": "user", "content": "hi"}],
        }
        is_claude = body["model"].startswith("claude-")
        self.assertTrue(is_claude)
        self.assertIn("thinking", body)
        self.assertIn("betas", body)


if __name__ == "__main__":
    unittest.main()
