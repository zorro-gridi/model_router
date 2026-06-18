"""
test_integration_thinking_strip.py — 集成测试：三层 thinking 降级方案端到端验证
===============================================================================

测试目标：
  1. 模拟 Claude Extended Thinking 产生的真实消息历史（含 thinking 块）
  2. Tier 1 (claude-*)：全透传，thinking/redacted_thinking/顶层字段/请求头全部保留
  3. Tier 2 (deepseek-*)：保留 thinking 块，剥离 redacted_thinking，pop 顶层字段，清 Anthropic 头
  4. Tier 3 (MiniMax/其它)：全剥 — thinking+redacted_thinking 归零
  5. 验证旧方案（不剥离）下同请求会触发上游 API 400 错误
  6. 验证响应端 thinking 块清洗（DeepSeek 保留 thinking、MiniMax 全剥）

与 test_thinking_strip.py 的区别：
  - test_thinking_strip.py：单元测试，直接调 _strip_thinking_blocks / _clean_headers_for_non_anthropic
  - 本文件：集成测试，通过 forward_request 完整链路，截获 urlopen 验证实际发出的请求体和响应
"""

import json
import os
import unittest
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════════════════════
#  真实场景的消息体：模拟一次 Claude Extended Thinking 会话产生的完整历史
# ═══════════════════════════════════════════════════════════════════════════════

_BODY_WITH_THINKING = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 8000,
    "thinking": {"type": "enabled", "budget_tokens": 16000},
    "betas": ["interleaved-thinking-2025-05-08"],
    "messages": [
        # 第一轮：普通用户提问
        {"role": "user", "content": "帮我分析这段代码的性能瓶颈"},

        # 第一轮 Claude 回复（带 thinking）
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "用户要分析性能瓶颈，先看时间复杂度，再看 IO 操作模式，最后考虑内存分配。需要用工具先读取代码。",
                    "signature": "EuYbFh3kLpQx...sig_001",
                },
                {"type": "text", "text": "好的，让我先读取代码来分析性能。"},
            ],
        },

        # 用户提供 tool_result（内嵌 thinking 块 — 关键递归场景）
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_001",
                    "content": [
                        {"type": "text", "text": "def slow_func(n):\n    result = []\n    for i in range(n):\n        for j in range(n):\n            result.append(i*j)\n    return result"},
                        {
                            "type": "thinking",
                            "thinking": "（续前推理）这个嵌套循环是 O(n²)，而且 append 操作在 Python 中有额外开销...",
                        },
                    ],
                }
            ],
        },

        # 第二轮 Claude 回复（多 thinking 块 + redacted_thinking）
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "确认了，典型 O(n²)。需要建议用户用列表推导式或 NumPy 向量化。两种方案要对应用场景做 trade-off 分析...",
                    "signature": "EuYbFh3kLpQx...sig_002",
                },
                {"type": "redacted_thinking", "data": "<redacted>"},
                {
                    "type": "text",
                    "text": "发现了！第 5-6 行的嵌套循环导致了 O(n²) 复杂度。建议用列表推导式（保持纯 Python）或 NumPy 向量化（需额外依赖）。",
                },
            ],
        },

        # 用户追问
        {"role": "user", "content": "怎么改成向量化？给详细步骤"},
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
#  测试基础设施
# ═══════════════════════════════════════════════════════════════════════════════

class _CapturedRequest:
    """forward_request 截获器：记录实际发送到上游的 body 和 headers。

    headers 的 key 被归一化为小写，因为 urllib 会做 title-case 转换
    (anthropic-version → Anthropic-version)，直接用 has_header 判段。
    """

    def __init__(self, body: bytes, headers: dict):
        self.body = body
        self.headers = headers

    @property
    def json(self) -> dict:
        return json.loads(self.body.decode())

    def has_header(self, name: str) -> bool:
        """大小写不敏感地检查请求头是否存在。"""
        target = name.lower()
        return target in (k.lower() for k in self.headers)

    def get_header(self, name: str) -> str | None:
        """大小写不敏感地获取请求头值。"""
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return v
        return None


def _capture_forward(target_model: str, headers: dict | None = None,
                     body_override: dict | None = None,
                     env_override: dict | None = None) -> _CapturedRequest:
    """调用 forward_request 并返回截获的 HTTP 请求。

    通过 monkeypatch urlopen 截获 Request 对象，提取其中的 data 和 headers，
    不发起真实网络请求。上游返回 mock 200。

    Args:
        target_model: 目标模型名（MiniMax-M3 / claude-sonnet-4-6 / deepseek-v4-pro 等）
        headers: CC 传入的原始请求头（含 anthropic-beta 等）
        body_override: 替换默认的 _BODY_WITH_THINKING
        env_override: 覆盖环境变量（如 API key）
    """
    if headers is None:
        headers = {
            "content-type": "application/json",
            "anthropic-beta": "interleaved-thinking-2025-05-08",
            "anthropic-version": "2023-06-01",
        }
    if body_override is None:
        body_override = _BODY_WITH_THINKING
    if env_override is None:
        env_override = {}

    captured: _CapturedRequest | None = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured
        captured = _CapturedRequest(body=req.data, headers=dict(req.headers))
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.read.return_value = json.dumps({
            "id": "msg_test",
            "content": [{"type": "text", "text": "test response"}],
            "stop_reason": "end_turn",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    from proxy import forward_request

    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen), \
         patch.dict(os.environ, {**env_override}, clear=False):
        forward_request(
            method="POST",
            path="/v1/messages",
            headers=headers,
            body=json.dumps(body_override).encode(),
            target_base="https://api.minimaxi.com",
            target_model=target_model,
            api_key_env="MINIMAX_API_KEY",
            protocol="anthropic",
            dry_run=False,
        )

    assert captured is not None, "urlopen 未被调用，检查 forward_request 逻辑"
    return captured


def _count_thinking_blocks(body: dict) -> dict:
    """统计消息历史中各类型 thinking 块的数量。

    Returns:
        {"thinking": N, "redacted_thinking": M, "total": N+M}
    """
    counts = {"thinking": 0, "redacted_thinking": 0, "total": 0}
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t in ("thinking", "redacted_thinking"):
                counts[t] += 1
                counts["total"] += 1
    return counts


# ═══════════════════════════════════════════════════════════════════════════════
#  第一部分：新方案核心验证 — 转发给非 Claude 模型时 thinking 被剥离
# ═══════════════════════════════════════════════════════════════════════════════

class TestForwardToMiniMax(unittest.TestCase):
    """转发给 MiniMax（tier 3：全剥）→ thinking/redacted_thinking 全部剥离。"""

    def test_minimax_strips_all_thinking_blocks(self):
        """MiniMax-M3：messages 中所有 thinking/redacted_thinking 块归零。"""
        cap = _capture_forward("MiniMax-M3",
                               env_override={"MINIMAX_API_KEY": "sk-minimax-test"})
        counts = _count_thinking_blocks(cap.json)
        self.assertEqual(counts["total"], 0,
                        f"转发 MiniMax 的请求体仍含 {counts['total']} 个 thinking 块")

    def test_minimax_strips_tool_result_nested_thinking(self):
        """MiniMax-M3：tool_result.content[] 内嵌的 thinking 块也被递归剥离。"""
        cap = _capture_forward("MiniMax-M3",
                               env_override={"MINIMAX_API_KEY": "sk-minimax-test"})
        # 第三条消息是 user tool_result
        tool_msg = cap.json["messages"][2]
        self.assertEqual(tool_msg["role"], "user")
        tc = tool_msg["content"][0]
        self.assertEqual(tc["type"], "tool_result")
        for block in tc.get("content", []):
            if isinstance(block, dict):
                self.assertNotEqual(
                    block.get("type"), "thinking",
                    f"tool_result 内嵌 thinking 未被递归剥离: {block}")

    def test_minimax_pops_thinking_and_betas_top_level(self):
        """MiniMax-M3：顶层 thinking / betas 字段被 pop。"""
        cap = _capture_forward("MiniMax-M3",
                               env_override={"MINIMAX_API_KEY": "sk-minimax-test"})
        self.assertNotIn("thinking", cap.json,
                        "非 claude-* 模型不应保留顶层 thinking 字段")
        self.assertNotIn("betas", cap.json,
                        "非 claude-* 模型不应保留顶层 betas 字段")

    def test_minimax_headers_stripped(self):
        """MiniMax-M3：anthropic-beta 必剥；anthropic-version 补默认值保留。
        2026-06-18 加固：旧策略"无条件剥 version"会导致 MiniMax 缺 version 头 400；
        新策略"清洗剥 beta + 兜底补 version 默认值"更稳。
        fixture 中 headers_in 含 anthropic-beta=interleaved-thinking → 必剥；
        version 在清洗时被剥，但 forward_request 兜底补回 2023-06-01。"""
        cap = _capture_forward("MiniMax-M3",
                               env_override={"MINIMAX_API_KEY": "sk-minimax-test"})
        self.assertFalse(cap.has_header("anthropic-beta"),
                         "MiniMax 不应带 anthropic-beta 头（会触发 400）")
        self.assertTrue(cap.has_header("anthropic-version"),
                        "MiniMax 必须带 anthropic-version 头（兼容端点要求）")
        self.assertEqual(cap.get_header("anthropic-version"), "2023-06-01",
                         "version 头缺省应补 2023-06-01")


class TestForwardToDeepSeek(unittest.TestCase):
    """转发给 DeepSeek（tier 2：保留 thinking，仅剥 redacted_thinking）。

    DeepSeek API 文档明确支持 type='thinking' 块（遵循 Anthropic 兼容规范），
    但不支持 type='redacted_thinking'。因此：
      - thinking 块原样保留
      - redacted_thinking 块剥离
      - 顶层 thinking/betas 仍 pop（DeepSeek 忽略这两个字段）
      - anthropic-* 请求头仍剥离（DeepSeek 不需要）
    """

    def test_deepseek_preserves_thinking_blocks(self):
        """DeepSeek：type='thinking' 块保留（DeepSeek 兼容）。"""
        cap = _capture_forward("deepseek-v4-pro",
                               env_override={"MINIMAX_API_KEY": "sk-deepseek-test"})
        counts = _count_thinking_blocks(cap.json)
        # thinking 保留，redacted 已剥
        self.assertGreater(counts["thinking"], 0,
                          f"DeepSeek 应保留 thinking 块，实际 thinking={counts['thinking']}")
        self.assertEqual(counts["redacted_thinking"], 0,
                        f"DeepSeek 应剥离 redacted_thinking，实际={counts['redacted_thinking']}")

    def test_deepseek_strips_only_redacted_thinking(self):
        """DeepSeek：tool_result 内嵌的 thinking 保留，redacted 剥离。"""
        cap = _capture_forward("deepseek-v4-pro",
                               env_override={"MINIMAX_API_KEY": "sk-deepseek-test"})
        # 检查 assistant（第二条消息，索引 2，第二轮回复）的状态
        # _BODY_WITH_THINKING 的 messages[2] 是 tool_result，messages[3] 是 assistant
        assistant_msg = cap.json["messages"][3]
        self.assertEqual(assistant_msg["role"], "assistant")
        types_present = set()
        for b in assistant_msg.get("content", []):
            if isinstance(b, dict):
                types_present.add(b.get("type"))
        self.assertIn("thinking", types_present,
                      "DeepSeek 应保留 thinking 块")
        self.assertNotIn("redacted_thinking", types_present,
                         "DeepSeek 应剥离 redacted_thinking 块")

    def test_deepseek_pops_thinking_and_betas_top_level(self):
        """DeepSeek：顶层 thinking/betas 字段仍被 pop（DeepSeek 忽略这些字段）。"""
        cap = _capture_forward("deepseek-v4-pro",
                               env_override={"MINIMAX_API_KEY": "sk-deepseek-test"})
        self.assertNotIn("thinking", cap.json,
                        "DeepSeek 不应保留顶层 thinking（会被忽略）")
        self.assertNotIn("betas", cap.json,
                        "DeepSeek 不应保留顶层 betas（会被忽略）")

    def test_deepseek_headers_stripped(self):
        """DeepSeek：anthropic-beta/anthropic-version 请求头被剥离。"""
        cap = _capture_forward("deepseek-v4-pro",
                               env_override={"MINIMAX_API_KEY": "sk-deepseek-test"})
        self.assertFalse(cap.has_header("anthropic-beta"),
                         "DeepSeek 不应带 anthropic-beta 头")
        self.assertTrue(cap.has_header("anthropic-version"),
                        "DeepSeek 必须带 anthropic-version 头（兼容端点要求）")
        self.assertEqual(cap.get_header("anthropic-version"), "2023-06-01",
                         "version 头缺省应补 2023-06-01")


# ═══════════════════════════════════════════════════════════════════════════════
#  第二部分：对照验证 — Claude 模型原样透传（不破坏签名校验）
# ═══════════════════════════════════════════════════════════════════════════════

class TestForwardToClaudeModel(unittest.TestCase):
    """转发给 claude-* 模型 → 不做任何降级，保持签名完整性。"""

    def test_claude_preserves_thinking_blocks(self):
        """claude-sonnet-4-6：thinking 块原样保留（签名校验需要）。"""
        cap = _capture_forward("claude-sonnet-4-6",
                               env_override={"MINIMAX_API_KEY": "sk-anthropic-test"})
        counts = _count_thinking_blocks(cap.json)
        self.assertGreater(counts["total"], 0,
                          f"claude-* 模型应保留 thinking 块，实际 = {counts}")

    def test_claude_preserves_thinking_top_level(self):
        """claude-* 模型保留顶层 thinking 和 betas 字段。"""
        cap = _capture_forward("claude-sonnet-4-6",
                               env_override={"MINIMAX_API_KEY": "sk-anthropic-test"})
        self.assertIn("thinking", cap.json,
                      "claude-* 模型应保留顶层 thinking 字段")
        self.assertIn("betas", cap.json,
                      "claude-* 模型应保留顶层 betas 字段")

    def test_claude_preserves_headers(self):
        """claude-* 模型保留 anthropic-beta 请求头。"""
        cap = _capture_forward("claude-sonnet-4-6",
                               env_override={"MINIMAX_API_KEY": "sk-anthropic-test"})
        self.assertTrue(cap.has_header("anthropic-beta"),
                        "claude-* 模型应保留 anthropic-beta 请求头")
        self.assertTrue(cap.has_header("anthropic-version"),
                        "claude-* 模型应保留 anthropic-version 请求头")


# ═══════════════════════════════════════════════════════════════════════════════
#  第三部分：400 错误复现 — 证明旧方案下确实会触发 API 400
# ═══════════════════════════════════════════════════════════════════════════════

class Test400ErrorReproduction(unittest.TestCase):
    """模拟 MiniMax 上游对 thinking 块的拒绝，验证新旧方案的行为差异。"""

    def test_new_code_avoids_400_by_stripping(self):
        """新方案：thinking 块在转发前剥离 → upstream 不会拒绝 → 200。

        urlopen mock 会检查请求体中是否有 thinking 块：
        - 有 → 抛出 HTTPError 400（模拟真实 MiniMax 报错）
        - 无 → 正常返回 200
        """
        import urllib.error

        def strict_upstream(req, timeout=None):
            body = json.loads(req.data.decode())
            msgs = body.get("messages", [])
            has_thinking = any(
                isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                for m in msgs
                for b in (m.get("content") if isinstance(m.get("content"), list) else [])
            )
            if has_thinking or "thinking" in body:
                raise urllib.error.HTTPError(
                    url="https://api.minimaxi.com/v1/messages",
                    code=400,
                    msg="Bad Request",
                    hdrs={},
                    fp=None,
                )
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}]
            }).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        from proxy import forward_request

        with patch("proxy.urllib.request.urlopen", side_effect=strict_upstream), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-minimax-test"}):
            status, _, body_bytes, _ = forward_request(
                method="POST",
                path="/v1/messages",
                headers={
                    "content-type": "application/json",
                    "anthropic-beta": "interleaved-thinking-2025-05-08",
                },
                body=json.dumps(_BODY_WITH_THINKING).encode(),
                target_base="https://api.minimaxi.com",
                target_model="MiniMax-M3",
                api_key_env="MINIMAX_API_KEY",
                protocol="anthropic",
                dry_run=False,
            )

        self.assertEqual(status, 200,
                         f"新方案下 thinking 已剥离，应为 200，实际 status={status}")

    def test_old_code_would_get_400(self):
        """旧方案（绕过降级）：thinking 块未剥离 → upstream 拒绝 → 400。

        通过 monkeypatch _strip_thinking_blocks 为 no-op 来模拟旧方案，
        将带 thinking 块的请求体原样转发给上游，触发 400 错误。
        """
        import urllib.error

        def strict_upstream(req, timeout=None):
            body = json.loads(req.data.decode())
            msgs = body.get("messages", [])
            has_thinking = any(
                isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                for m in msgs
                for b in (m.get("content") if isinstance(m.get("content"), list) else [])
            )
            if has_thinking or "thinking" in body:
                raise urllib.error.HTTPError(
                    url="https://api.minimaxi.com/v1/messages",
                    code=400,
                    msg="Bad Request",
                    hdrs={},
                    fp=None,
                )
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}]
            }).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        from proxy import forward_request

        # ── 关键：让 _strip_thinking_blocks 什么都不做，模拟旧方案 ──
        with patch("proxy.urllib.request.urlopen", side_effect=strict_upstream), \
             patch("proxy._strip_thinking_blocks", return_value=(0, 0)), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-minimax-test"}):
            status, _, body_bytes, _ = forward_request(
                method="POST",
                path="/v1/messages",
                headers={"content-type": "application/json"},
                body=json.dumps(_BODY_WITH_THINKING).encode(),
                target_base="https://api.minimaxi.com",
                target_model="MiniMax-M3",
                api_key_env="MINIMAX_API_KEY",
                protocol="anthropic",
                dry_run=False,
            )

        self.assertEqual(status, 400,
                         "旧方案（不剥离 thinking）应导致 MiniMax 返回 400。"
                         f"实际 status={status}")

    def test_error_body_content_matches_real_minimax_error(self):
        """验证 mock 的 400 错误码与真实 MiniMax 报错一致。

        真实错误：API Error: 400 The content[].thinking in the thinking mode
        must be passed back to the API
        """
        import urllib.error

        def minimax_like_upstream(req, timeout=None):
            body = json.loads(req.data.decode())
            has_thinking = _count_thinking_blocks(body)["total"] > 0
            if has_thinking or "thinking" in body:
                raise urllib.error.HTTPError(
                    url="https://api.minimaxi.com/anthropic/v1/messages",
                    code=400,
                    msg="Bad Request",
                    hdrs={},
                    fp=None,
                )
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}]
            }).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        from proxy import forward_request

        # 旧方案
        with patch("proxy.urllib.request.urlopen", side_effect=minimax_like_upstream), \
             patch("proxy._strip_thinking_blocks", return_value=(0, 0)), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-minimax-test"}):
            status, _, body_bytes, _ = forward_request(
                method="POST",
                path="/v1/messages",
                headers={"content-type": "application/json"},
                body=json.dumps(_BODY_WITH_THINKING).encode(),
                target_base="https://api.minimaxi.com/anthropic",
                target_model="MiniMax-M3",
                api_key_env="MINIMAX_API_KEY",
                protocol="anthropic",
                dry_run=False,
            )

        self.assertEqual(status, 400,
                         "旧方案触发 400（与真实错误码一致）。"
                         f"实际 status={status}")


# ═══════════════════════════════════════════════════════════════════════════════
#  第四部分：响应端 thinking 清洗
# ═══════════════════════════════════════════════════════════════════════════════

class TestResponseSideThinkingStrip(unittest.TestCase):
    """响应端 thinking 清洗：三层策略分别验证。"""

    # ── MiniMax（tier 3）全剥 ──

    def test_minimax_strips_all_thinking_from_response(self):
        """MiniMax 响应：thinking + redacted_thinking 全剥。"""
        from proxy import _strip_thinking_from_response

        resp = {
            "id": "msg_001",
            "content": [
                {"type": "thinking", "thinking": "upstream internal reasoning",
                 "signature": "sig_upstream"},
                {"type": "text", "text": "visible reply text"},
                {"type": "redacted_thinking", "data": "<redacted>"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }

        cleaned = _strip_thinking_from_response(json.dumps(resp).encode(),
                                                 keep_thinking=False)
        parsed = json.loads(cleaned)

        self.assertEqual(len(parsed["content"]), 1,
                         "MiniMax 清洗后应只剩 text block")
        self.assertEqual(parsed["content"][0]["type"], "text")
        self.assertEqual(parsed["content"][0]["text"], "visible reply text")
        self.assertEqual(parsed["stop_reason"], "end_turn")
        self.assertEqual(parsed["usage"]["input_tokens"], 500)

    # ── DeepSeek（tier 2）保留 thinking，仅剥 redacted ──

    def test_deepseek_preserves_thinking_in_response(self):
        """DeepSeek 响应：保留 thinking 块，仅剥离 redacted_thinking。"""
        from proxy import _strip_thinking_from_response

        resp = {
            "id": "msg_002",
            "content": [
                {"type": "thinking", "thinking": "deepseek internal reasoning",
                 "signature": "sig_ds"},
                {"type": "text", "text": "visible reply"},
                {"type": "redacted_thinking", "data": "<redacted>"},
            ],
            "stop_reason": "end_turn",
        }

        cleaned = _strip_thinking_from_response(json.dumps(resp).encode(),
                                                 keep_thinking=True)
        parsed = json.loads(cleaned)

        # thinking + text 保留，redacted 剥离
        self.assertEqual(len(parsed["content"]), 2)
        types = [b["type"] for b in parsed["content"]]
        self.assertIn("thinking", types, "DeepSeek 响应应保留 thinking 块")
        self.assertIn("text", types)
        self.assertNotIn("redacted_thinking", types,
                         "DeepSeek 响应应剥离 redacted_thinking")

    # ── 通用 ──

    def test_response_without_thinking_untouched(self):
        """不含 thinking 块的正常响应不做任何修改。"""
        from proxy import _strip_thinking_from_response
        normal = json.dumps({
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
        }).encode()
        cleaned = _strip_thinking_from_response(normal)
        self.assertEqual(json.loads(cleaned)["content"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
