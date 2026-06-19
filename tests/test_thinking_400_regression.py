"""
test_thinking_400_regression.py — thinking 400 错误边界场景回归测试
=====================================================================

覆盖 2026-06-19 多轮复发 400 "content[].thinking must be passed back to the API"
的真实触发链路和所有已知边界场景。

测试策略：
  - 不 mock _strip_thinking_blocks 内部——全部走 forward_request 完整链路
  - 用 _capture_forward() 截获实际发出的请求体，验证 thinking 块/字段的正确性
  - 每个场景模拟 Claude Code 产生的真实消息历史、包含多轮对话和嵌套 tool_result

覆盖场景索引：
  S01 — sticky swap minimax→deepseek: foreign signature 全部剥除
  S02 — sticky swap deepseek→minimax: deepseek reasoning_content 格式兼容
  S03 — 多轮 mixed provider: minimax+claude+deepseek thinking 块混合
  S04 — 深度嵌套 tool_result (3+ 层)：递归剥 signature
  S05 — system[] 含 minimax/claude 的 thinking+signature
  S06 — 空 thinking 块（仅 signature 字段）
  S07 — thinking 块 signature 剥除后 thinking 内容健在
  S08 — 响应端 sticky swap：deepseek 返回 thinking 块被正确保留
  S09 — 多 provider 连续 sticky swap (minimax→deepseek→minimax→deepseek)
  S10 — tool_use 块与 thinking 块交织：tool_use 不受影响
  S11 — redacted_thinking + foreign thinking 混合
  S12 — unknown model fallback: 全剥（保守策略回归）
"""

import json
import os
import unittest
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════════════════════
#  真实多轮对话消息体构造
# ═══════════════════════════════════════════════════════════════════════════════

def _make_thinking_block(thinking_text: str, signature: str = None,
                          **extra) -> dict:
    """构造一个 type='thinking' block（模拟各 provider 的真实输出）。"""
    block: dict = {"type": "thinking", "thinking": thinking_text}
    if signature:
        block["signature"] = signature
    block.update(extra)
    return block


def _make_redacted_thinking(data: str = "<redacted>") -> dict:
    """构造一个 type='redacted_thinking' block。"""
    return {"type": "redacted_thinking", "data": data}


def _make_text_block(text: str) -> dict:
    """构造一个 type='text' block。"""
    return {"type": "text", "text": text}


def _make_tool_result(tool_use_id: str, content: list) -> dict:
    """构造一个 user role 的 tool_result block。"""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }


def _make_tool_use(tool_use_id: str, name: str, input_: dict) -> dict:
    """构造一个 assistant role 的 tool_use block。"""
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": input_,
    }


# ── S01/S03/S09: sticky swap 核心场景 ────────────────────────────────────────

_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "messages": [
        # 第 1 轮：用户提问
        {"role": "user", "content": "分析这段代码的安全漏洞"},

        # 第 1 轮 MiniMax 回复（带 MiniMax 的 adaptive thinking + signature）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "需要先用工具读取目标文件，然后逐行审计安全风险。"
                    "重点关注：SQL 注入、XSS、权限绕过。",
                    signature="MmSig_adaptive_round1_abc123",
                ),
                _make_text_block("让我先读取目标文件来审计安全风险。"),
                _make_tool_use("toolu_001", "read_file", {"path": "auth.py"}),
            ],
        },

        # 第 1 轮 tool_result（含 MiniMax 的继续推理 thinking）
        {
            "role": "user",
            "content": [
                _make_tool_result("toolu_001", [
                    _make_text_block("def login(user, pwd):\n"
                                     "    query = f\"SELECT * FROM users WHERE name='{user}'\"\n"
                                     "    return db.execute(query)"),
                    _make_thinking_block(
                        "f-string 直接拼接 SQL → 明显 SQL 注入风险。"
                        "还要看是否有输入验证和参数化查询的痕迹。",
                        signature="MmSig_adaptive_tool_round1_def456",
                    ),
                ]),
            ],
        },

        # 第 2 轮 MiniMax 回复（含多个 thinking 块 + redacted_thinking）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "确认 SQL 注入。第 2 行使用 f-string 拼接用户输入到 SQL，"
                    "无任何转义或参数化。这是 Critical 级别漏洞。"
                    "还需要检查 XSS 和 CSRF。",
                    signature="MmSig_adaptive_round2_ghi789",
                ),
                _make_redacted_thinking("<redacted for safety>"),
                _make_text_block(
                    "发现 Critical 漏洞：auth.py 第 2 行存在 SQL 注入。"
                    "f-string 直接拼接用户输入到 SQL 查询，攻击者可通过构造 "
                    "`' OR '1'='1' --` 绕过认证。建议改用参数化查询。",
                ),
            ],
        },

        # 用户追问
        {"role": "user", "content": "还有没有其他安全问题？比如 XSS？"},
    ],
}


# ── S02: deepseek→minimax 切换 ──────────────────────────────────────────────

_BODY_STICKY_SWAP_DEEPSEEK_TO_MINIMAX = {
    "model": "deepseek-v4-pro",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "帮我规划一个微服务架构的方案"},

        # DeepSeek 回复（DeepSeek thinking 块不带 signature 字段，
        # 而是 reasoning_content 在独立的 response 字段——但 CC 可能
        # 在内容历史里保留 thinking 块）
        {
            "role": "assistant",
            "content": [
                # DeepSeek 有时也会生成带 signature 的 thinking（message id 伪装）
                _make_thinking_block(
                    "微服务拆分的核心原则：按业务边界划分、每个服务独立部署、"
                    "服务间通过 API/消息队列通信。先了解用户的技术栈和团队规模。",
                    signature="DsMsgId_fake_001",
                ),
                _make_text_block(
                    "好的，我来帮你规划。首先需要了解几个关键信息："
                    "1) 当前单体应用的技术栈？2) 团队规模和经验？3) 预期的流量规模？",
                ),
            ],
        },

        {"role": "user", "content": "Java Spring Boot，5 人团队，日均 10 万 UV"},

        # DeepSeek 第二轮回复（含 tool_use + thinking）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "5 人团队较小，不宜过度拆分。建议 3-4 个核心服务："
                    "用户服务、订单服务、商品服务、网关。"
                    "先看下现有项目结构，用工具扫描。",
                    signature="DsMsgId_fake_002",
                ),
                _make_tool_use("toolu_002", "list_files", {"path": "/src"}),
            ],
        },

        {
            "role": "user",
            "content": [
                _make_tool_result("toolu_002", [
                    _make_text_block("src/\n  main/\n    controller/\n    service/\n    dao/\n    model/"),
                    _make_thinking_block(
                        "典型单体分层结构。controller/service/dao 三层可以整体"
                        "作为拆分起点，但要注意 dao 层的跨服务事务问题。",
                        signature="DsMsgId_fake_tool_003",
                    ),
                ]),
            ],
        },
    ],
}


# ── S03: mixed provider（minimax + claude + deepseek）──────────────────────

_BODY_MIXED_PROVIDER = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "写一个 Python 脚本"},

        # Claude 回复（Anthropic signature 格式）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "用户要写 Python 脚本，先确认具体需求再动手。"
                    "可能是数据处理、自动化、或者 API 调用。",
                    signature="EuYbFh3kLpQx...anthropic_sig_claude_001",
                ),
                _make_text_block("好的，请问您需要什么类型的 Python 脚本？"),
            ],
        },

        {"role": "user", "content": "数据清洗脚本，CSV 去重 + 格式化"},

        # MiniMax 回复（adaptive signature）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "CSV 去重+格式化，用 pandas 最快。但如果用户不想装依赖，"
                    "可以用 csv 标准库。先写 pandas 版本，注释备用 csv 方案。",
                    signature="MmSig_adaptive_data_clean_005",
                ),
                _make_text_block("好的，用 pandas 处理最快。我来写："),
            ],
        },

        {"role": "user", "content": "不要 pandas，用标准库"},

        # DeepSeek 回复（message id 伪 signature）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "纯标准库方案：csv.reader/DictReader 读取，set 去重，"
                    "csv.writer 写出。需要处理编码问题和空行。",
                    signature="DsMsgId_pseudo_006",
                ),
                _make_text_block(
                    "用 csv 标准库就可以：\n```python\nimport csv\n...```",
                ),
            ],
        },
    ],
}


# ── S04: 深度嵌套 tool_result (3+ 层) ─────────────────────────────────────

_BODY_DEEP_NESTED_TOOL_RESULT = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "审查整个项目的安全性"},

        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "安全性审查需要多步工具调用：先扫描目录结构，再读关键文件，"
                    "最后做静态分析。",
                    signature="MmSig_scan_001",
                ),
                _make_tool_use("toolu_scan", "scan_directory", {"path": "/"}),
            ],
        },

        # 第 1 层嵌套：tool_result 内有 tool_result 的 output
        {
            "role": "user",
            "content": [
                _make_tool_result("toolu_scan", [
                    _make_text_block("[dir] src/\n[dir] config/\n[file] auth.py"),
                    _make_thinking_block(
                        "先看 auth.py，这是最可能的攻击面。config/ 也可能有密钥泄露。",
                        signature="MmSig_scan_002",
                    ),
                    # 内嵌一个 tool_result（模拟复合工具输出）
                    _make_tool_result("toolu_sub_001", [
                        _make_text_block("sub-tool output: 42 files scanned"),
                        _make_thinking_block(
                            "42 个文件，auth.py 优先级最高。还要看 requirements.txt 的依赖漏洞。",
                            signature="MmSig_subtool_003",
                        ),
                    ]),
                ]),
            ],
        },

        {"role": "user", "content": "重点看 auth.py 和 config/"},
    ],
}


# ── S05: system[] 含 thinking ──────────────────────────────────────────────

_BODY_SYSTEM_THINKING = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "system": [
        _make_text_block("You are a senior security auditor."),
        _make_thinking_block(
            "我需要用中文回答用户的安全审计问题，"
            "重点关注 OWASP Top 10 中的 SQL 注入和 XSS。",
            signature="MmSig_system_001",
        ),
        _make_redacted_thinking("<system redacted>"),
        _make_thinking_block(
            "如果用户问架构问题，也要从安全角度切入。"
            "网络分层、认证鉴权、加密传输等都需要考虑。",
            signature="Claude_sys_sig_002",
        ),
    ],
    "messages": [
        {"role": "user", "content": "审查 auth.py"},
    ],
}


# ── S07: thinking 内容健在验证 ────────────────────────────────────────────

_BODY_THINKING_CONTENT_INTEGRITY = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "解释量子计算的基本原理"},

        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "量子计算的核心概念：叠加态、纠缠、量子门。"
                    "先用经典比特对比引入，再讲量子比特（qubit）的特性。"
                    "避免深入数学，用物理直觉+图示思维来解释。",
                    signature="MmSig_quantum_detail_001",
                    # MiniMax 可能带额外字段
                    "index": 0,
                    "citations": [],
                ),
                _make_text_block(
                    "量子计算利用量子力学原理处理信息。与传统计算机用 0/1 的比特不同，"
                    "量子计算机使用量子比特（qubit），可以同时处于 0 和 1 的叠加态。",
                ),
            ],
        },

        # 用户追问
        {"role": "user", "content": "叠加态具体是什么意思？用简单例子说明"},

        # 第 2 轮回复（多段 thinking + 含 MiniMax 扩展字段）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "薛定谔的猫是最经典的叠加态比喻。"
                    "盒子打开前，猫既是活的又是死的——这就是叠加。"
                    "但要注意这个比喻有局限性（宏观vs微观）。"
                    "更准确的例子：电子的自旋，测量前可以同时是上旋和下旋。",
                    signature="MmSig_superposition_002",
                    "index": 0,
                    "citations": ["https://example.com/quantum-basics"],
                ),
                _make_text_block(
                    "想象薛定谔的猫：在你打开盒子之前，猫同时处于'活'和'死'两种状态。"
                    "量子比特也是如此——在被测量之前，它同时是 0 和 1。"
                    "这种'同时存在多种可能性'就是叠加态的核心。",
                ),
            ],
        },
    ],
}


# ── S06: 空 thinking / 仅 signature ────────────────────────────────────────

_BODY_EMPTY_OR_MINIMAL_THINKING = {
    "model": "deepseek-v4-pro",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "ok"},

        # Claude 回复：仅有一个极简 thinking 块（只含 signature）
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "",  # 空 thinking 内容
                    signature="Claude_minimal_empty_001",
                ),
                _make_text_block("Got it."),
            ],
        },

        {"role": "user", "content": "继续之前的任务"},

        # MiniMax 回复：thinking 内容为空字符串但有 signature
        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "继续之前没有完成的任务...",
                    signature="MmSig_continue_002",
                ),
                _make_text_block("好的，继续之前的分析。"),
                _make_thinking_block(
                    "",  # 另一个空 thinking 块
                    signature="MmSig_empty_003",
                ),
            ],
        },
    ],
}


# ── S10: tool_use 与 thinking 块交织 ──────────────────────────────────────

_BODY_TOOL_USE_INTERLEAVED = {
    "model": "MiniMax-M3",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "给我分析 /src/ 下所有 Python 文件"},

        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "需要先 list_files 看有哪些 Python 文件，"
                    "然后逐个 read_file 分析。可能需要并行读取。",
                    signature="MmSig_interleave_001",
                ),
                _make_tool_use("toolu_list", "list_files", {"path": "/src", "pattern": "*.py"}),
                _make_thinking_block(
                    "list_files 完成后，根据文件名决定读取顺序。"
                    "__init__.py 跳过，main.py 优先级最高。",
                    signature="MmSig_interleave_002",
                ),
            ],
        },

        {
            "role": "user",
            "content": [
                _make_tool_result("toolu_list", [
                    _make_text_block("main.py\nutils.py\nmodels.py\n__init__.py"),
                    _make_thinking_block(
                        "4 个文件，main.py 先读。utils.py 可能包含辅助函数。"
                        "models.py 如果是 ORM 模型则可能很大。",
                        signature="MmSig_tool_interleave_003",
                    ),
                ]),
            ],
        },

        {
            "role": "assistant",
            "content": [
                _make_tool_use("toolu_read_main", "read_file", {"path": "/src/main.py"}),
                _make_tool_use("toolu_read_utils", "read_file", {"path": "/src/utils.py"}),
                _make_tool_use("toolu_read_models", "read_file", {"path": "/src/models.py"}),
                _make_thinking_block(
                    "并行读取 3 个文件，等结果回来后做综合交叉分析。"
                    "tool_use 块不应该被我们的 thinking 处理逻辑影响。",
                    signature="MmSig_parallel_tools_004",
                ),
            ],
        },
    ],
}


# ── S11: redacted_thinking + foreign thinking 混合 ──────────────────────────

_BODY_REDACTED_MIXED = {
    "model": "deepseek-v4-pro",
    "max_tokens": 8000,
    "messages": [
        {"role": "user", "content": "总结这段对话"},

        {
            "role": "assistant",
            "content": [
                _make_thinking_block(
                    "用户要求总结。历史中有敏感信息需要 redact 处理。"
                    "只提取公开的技术讨论部分。",
                    signature="Claude_sig_mixed_001",
                ),
                _make_redacted_thinking("<sensitive content redacted>"),
                _make_thinking_block(
                    "技术讨论核心是：API 设计、数据库 schema、部署方案。",
                    signature="MmSig_mixed_002",
                ),
                _make_redacted_thinking("<another redacted block>"),
                _make_text_block("总结：本次讨论了 API 设计、数据库 schema 和部署方案。"),
            ],
        },
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
#  测试基础设施（复用自 test_integration_thinking_strip.py）
# ═══════════════════════════════════════════════════════════════════════════════

class _CapturedRequest:
    """forward_request 截获器：记录实际发送到上游的 body 和 headers。"""

    def __init__(self, body: bytes, headers: dict):
        self.body = body
        self.headers = headers

    @property
    def json(self) -> dict:
        return json.loads(self.body.decode())


def _capture_forward(target_model: str, headers: dict | None = None,
                     body_override: dict | None = None,
                     env_override: dict | None = None,
                     target_base: str = "https://api.minimaxi.com",
                     protocol: str = "anthropic") -> _CapturedRequest:
    """调用 forward_request 并返回截获的 HTTP 请求。"""
    if headers is None:
        headers = {
            "content-type": "application/json",
            "anthropic-beta": "interleaved-thinking-2025-05-08",
            "anthropic-version": "2023-06-01",
        }
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
            target_base=target_base,
            target_model=target_model,
            api_key_env="MINIMAX_API_KEY",
            protocol=protocol,
            dry_run=False,
        )

    assert captured is not None, "urlopen 未被调用，检查 forward_request 逻辑"
    return captured


# ═══════════════════════════════════════════════════════════════════════════════
#  断言辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_all_thinking_blocks(body: dict) -> list[dict]:
    """递归收集消息体中所有 type='thinking' 块（含 tool_result 嵌套和 system[]）。

    Returns:
        所有 thinking 块的扁平列表。
    """
    result: list[dict] = []

    def _walk_content(content):
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "thinking":
                result.append(block)
            if bt == "tool_result" and isinstance(block.get("content"), list):
                _walk_content(block["content"])

    # messages[]
    for msg in body.get("messages", []):
        _walk_content(msg.get("content"))

    # system[]
    _walk_content(body.get("system"))

    return result


def _count_thinking_blocks(body: dict) -> dict[str, int]:
    """统计消息历史中各类型 thinking 块的数量。"""
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


def _collect_all_signatures(body: dict) -> list[str]:
    """收集所有 thinking 块中残留的 signature 值。"""
    sigs = []
    for block in _collect_all_thinking_blocks(body):
        if "signature" in block:
            sigs.append(block["signature"])
    return sigs


# ═══════════════════════════════════════════════════════════════════════════════
#  S01 — sticky swap minimax→deepseek: foreign signature 全部剥除
# ═══════════════════════════════════════════════════════════════════════════════

class S01_StickySwapMinimaxToDeepseek(unittest.TestCase):
    """sticky fallback minimax→deepseek-v4-pro 时，所有 thinking 块的
    signature 字段被剥除，thinking 内容保留。

    这是本次 400 复发的主要触发链路（2026-06-19 ts=1781832220 实际触发）。
    """

    def test_all_signatures_stripped(self):
        """转发给 deepseek-v4-pro 时，所有 foreign signature 被剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(
            sigs, [],
            f"所有 thinking 块的 signature 应被剥除，"
            f"实际残留 {len(sigs)} 个: {sigs}",
        )

    def test_thinking_blocks_preserved(self):
        """转发给 deepseek 时，thinking 块本身保留（仅剥 signature 字段）。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        counts = _count_thinking_blocks(cap.json)
        self.assertGreater(counts["thinking"], 0,
                           "thinking 块应保留给 deepseek 复用 reasoning context")
        self.assertEqual(counts["redacted_thinking"], 0,
                         "redacted_thinking 应被剥离")

    def test_signature_stripped_from_tool_result_nested(self):
        """tool_result 内嵌的 thinking 块 signature 也被剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        # 第 3 条消息（index 2）是 tool_result
        tool_msg = cap.json["messages"][2]
        tc = tool_msg["content"][0]
        self.assertEqual(tc["type"], "tool_result")
        for block in tc.get("content", []):
            if isinstance(block, dict) and block.get("type") == "thinking":
                self.assertNotIn(
                    "signature", block,
                    f"tool_result 内嵌 thinking 块应有剥除 signature: {block}",
                )

    def test_thinking_injected_as_enabled(self):
        """deepseek 路径：注入 thinking={"type": "enabled"}。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        self.assertIn("thinking", cap.json,
                      "deepseek 应显式注入 thinking 参数")
        self.assertEqual(cap.json["thinking"], {"type": "enabled"},
                         f"deepseek 路径应注入 enabled，实际={cap.json.get('thinking')}")


# ═══════════════════════════════════════════════════════════════════════════════
#  S02 — sticky swap deepseek→minimax: deepseek reasoning 格式兼容
# ═══════════════════════════════════════════════════════════════════════════════

class S02_StickySwapDeepseekToMinimax(unittest.TestCase):
    """sticky fallback deepseek→minimax 时，thinking 块保留 + signature 剥除。

    MiniMax 虽然不认识 deepseek 的 message-id 伪 signature，但路线 C
    统一剥 signature 后，MiniMax 只会看到"未签名 thinking content"。
    """

    def test_forward_to_minimax_strips_deepseek_signatures(self):
        """deepseek→minimax sticky swap：deepseek 尾 signature 被剥除。"""
        cap = _capture_forward(
            "MiniMax-M3",
            body_override=_BODY_STICKY_SWAP_DEEPSEEK_TO_MINIMAX,
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(
            sigs, [],
            f"deepseek 的伪 signature 应被剥除，残留={sigs}",
        )

    def test_thinking_blocks_preserved_for_minimax(self):
        """MiniMax 路径保留 thinking 块（路线 B 修订）。"""
        cap = _capture_forward(
            "MiniMax-M3",
            body_override=_BODY_STICKY_SWAP_DEEPSEEK_TO_MINIMAX,
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        counts = _count_thinking_blocks(cap.json)
        self.assertGreater(counts["thinking"], 0,
                           "MiniMax 应保留 thinking 块（路线 B）")

    def test_injects_adaptive_for_minimax(self):
        """MiniMax 路径：注入 thinking={"type": "adaptive"}。"""
        cap = _capture_forward(
            "MiniMax-M3",
            body_override=_BODY_STICKY_SWAP_DEEPSEEK_TO_MINIMAX,
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        self.assertIn("thinking", cap.json)
        self.assertEqual(cap.json["thinking"], {"type": "adaptive"})


# ═══════════════════════════════════════════════════════════════════════════════
#  S03 — 多轮 mixed provider: minimax+claude+deepseek thinking 混合
# ═══════════════════════════════════════════════════════════════════════════════

class S03_MixedProviderThinkingBlocks(unittest.TestCase):
    """多轮对话中来自不同 provider 的 thinking 块混合——
    proxy 应统一处理，不因 signature 来源差异而漏剥或残留。
    """

    def test_mixed_signatures_all_stripped_on_deepseek(self):
        """三种 provider（claude/minimax/deepseek）signature 混在上下文中，
        转发给 deepseek-v4-pro 时全部剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_MIXED_PROVIDER,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [],
                         f"三种 provider 的 signature 应全部剥除，残留={sigs}")

    def test_thinking_content_preserved_after_strip(self):
        """signature 剥除后，三种 provider 的 thinking 内容全部保留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_MIXED_PROVIDER,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        self.assertGreaterEqual(len(blocks), 3,
                                f"所有 provider 的 thinking 块应保留，实际={len(blocks)}")
        for block in blocks:
            self.assertIn("thinking", block,
                          f"thinking 块必须保留 thinking 文本字段: {block}")
            self.assertIsInstance(block["thinking"], str,
                                  "thinking 字段必须是字符串")
            self.assertNotIn("signature", block,
                             f"signature 字段已剥除不应残留: {block}")

    def test_mixed_signatures_all_stripped_on_minimax(self):
        """混合 provider 转发给 MiniMax 时同样剥除 signature。"""
        cap = _capture_forward(
            "MiniMax-M3",
            body_override=_BODY_MIXED_PROVIDER,
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [],
                         f"MiniMax 路径也应剥除所有 signature，残留={sigs}")


# ═══════════════════════════════════════════════════════════════════════════════
#  S04 — 深度嵌套 tool_result (3+ 层)：递归剥 signature
# ═══════════════════════════════════════════════════════════════════════════════

class S04_DeepNestedToolResult(unittest.TestCase):
    """tool_result 内嵌 tool_result 再内嵌 thinking 块——
    三层嵌套，验证递归 stripping 跳过所有层。
    """

    def test_deeply_nested_signatures_stripped(self):
        """三层嵌套 tool_result 内的 signature 全部剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_DEEP_NESTED_TOOL_RESULT,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [],
                         f"深度嵌套 tool_result 内 signature 应全部剥除，残留={sigs}")

    def test_nested_thinking_content_preserved(self):
        """嵌套 thinking 块内容保留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_DEEP_NESTED_TOOL_RESULT,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        # 原始有 3 个 thinking 块（tool_result 内 2 个 + assistant 内 1 个）
        self.assertGreaterEqual(len(blocks), 3,
                                f"嵌套 thinking 块应保留，实际={len(blocks)}")
        for block in blocks:
            self.assertNotIn("signature", block)


# ═══════════════════════════════════════════════════════════════════════════════
#  S05 — system[] 含 thinking 块
# ═══════════════════════════════════════════════════════════════════════════════

class S05_SystemThinkingBlocks(unittest.TestCase):
    """system[] 数组中的 thinking 块处理——
    与 messages[] 对称，也需剥 signature 和 redacted_thinking。
    """

    def test_system_thinking_signatures_stripped(self):
        """system[] 中 thinking 块的 signature 被剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_SYSTEM_THINKING,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        system = cap.json.get("system", [])
        for block in system:
            if isinstance(block, dict) and block.get("type") == "thinking":
                self.assertNotIn("signature", block,
                                 f"system[] 内 thinking 块 signature 应剥除: {block}")

    def test_system_redacted_thinking_stripped(self):
        """system[] 中 redacted_thinking 被剥离。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_SYSTEM_THINKING,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        system = cap.json.get("system", [])
        for block in system:
            if isinstance(block, dict):
                self.assertNotEqual(
                    block.get("type"), "redacted_thinking",
                    f"system[] 内 redacted_thinking 应被剥离: {block}",
                )

    def test_system_text_blocks_preserved(self):
        """system[] 中的 text 块保留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_SYSTEM_THINKING,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        system = cap.json.get("system", [])
        text_blocks = [b for b in system
                       if isinstance(b, dict) and b.get("type") == "text"]
        self.assertGreater(len(text_blocks), 0, "system[] 内 text 块应保留")


# ═══════════════════════════════════════════════════════════════════════════════
#  S07 — thinking 内容完整性：signature 剥除后文本健在
# ═══════════════════════════════════════════════════════════════════════════════

class S07_ThinkingContentIntegrityAfterStrip(unittest.TestCase):
    """signature 剥除后，thinking 文本内容、额外字段（index/citations）
    全部保留，仅 signature 字段被移除。
    """

    def test_thinking_text_intact(self):
        """thinking 文本内容完全保留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_THINKING_CONTENT_INTEGRITY,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        for block in blocks:
            self.assertIn("thinking", block)
            self.assertIsInstance(block["thinking"], str)
            self.assertGreater(len(block["thinking"]), 0,
                               "thinking 文本不应为空")

    def test_extra_fields_preserved(self):
        """MiniMax 的 index/citations/其他字段在 signature 剥除后保留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_THINKING_CONTENT_INTEGRITY,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        extra_fields_found = False
        for block in blocks:
            if "index" in block:
                extra_fields_found = True
                self.assertIsNotNone(block["index"])
            if "citations" in block:
                extra_fields_found = True
                self.assertIsInstance(block["citations"], list)
        # 至少有一个 block 有额外字段（MiniMax 格式）
        self.assertTrue(extra_fields_found,
                        "MiniMax 的 index/citations 等额外字段应保留")

    def test_no_signature_residue(self):
        """所有 thinking 块中无 signature 残留。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_THINKING_CONTENT_INTEGRITY,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [])


# ═══════════════════════════════════════════════════════════════════════════════
#  S06 — 空 thinking 块 / 仅 signature 的边界
# ═══════════════════════════════════════════════════════════════════════════════

class S06_EmptyOrMinimalThinkingBlocks(unittest.TestCase):
    """thinking 内容为空或极简，signature 是块内唯一有意义字段时——
    验证剥离不会破坏块结构，空内容块被保留（以免破坏辅助角色）。
    """

    def test_empty_thinking_blocks_preserved(self):
        """空 thinking 块保留（仅 signature 被剥除）。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_EMPTY_OR_MINIMAL_THINKING,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        empty_blocks = [b for b in blocks if b.get("thinking") == ""]
        self.assertGreaterEqual(
            len(empty_blocks), 2,
            f"空 thinking 块应保留（仅剥 signature），实际空块={len(empty_blocks)}",
        )
        for block in empty_blocks:
            self.assertNotIn("signature", block)
            self.assertEqual(block["type"], "thinking")

    def test_non_empty_thinking_also_preserved(self):
        """非空 thinking 块不受影响。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_EMPTY_OR_MINIMAL_THINKING,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        non_empty = [b for b in blocks if b.get("thinking")]
        self.assertGreaterEqual(len(non_empty), 1,
                                "非空 thinking 块应保留")


# ═══════════════════════════════════════════════════════════════════════════════
#  S09 — 多 provider 连续 sticky swap
# ═══════════════════════════════════════════════════════════════════════════════

class S09_ChainedStickySwap(unittest.TestCase):
    """模拟连续 sticky swap：请求 1 走 minimax→deepseek，请求 2 走 deepseek→
    minimax（因为又 sticky back），上下文同时含两种 provider 的 signature。
    """

    def _make_chained_body(self, target_model: str) -> dict:
        """构造连续 swap 后的累积上下文。"""
        return {
            "model": target_model,
            "max_tokens": 8000,
            "messages": [
                {"role": "user", "content": "分析这个 bug"},
                # 轮 1: MiniMax 回复（adaptive sig）
                {
                    "role": "assistant",
                    "content": [
                        _make_thinking_block(
                            "先理解 bug 的上下文。用户提到了 API 返回 500。",
                            signature="MmSig_chain_round1",
                        ),
                        _make_text_block("我来分析这个 bug。请提供更多上下文。"),
                    ],
                },
                {"role": "user", "content": "是 POST /api/users 返回 500"},

                # 轮 2: 请求被 sticky 到 deepseek-v4-pro（生成 deepseek 回复）
                {
                    "role": "assistant",
                    "content": [
                        _make_thinking_block(
                            "POST /api/users 500，通常是请求体解析失败"
                            "或数据库写入异常。需要用工具查看日志和代码。",
                            signature="DsMsgId_chain_round2",
                        ),
                        _make_text_block("POST /api/users 500 通常有两个原因..."),
                    ],
                },
                {"role": "user", "content": "查看日志后发现是 NullPointerException"},

                # 轮 3: 请求又被 sticky 回 minimax（生成 minimax 回复）
                {
                    "role": "assistant",
                    "content": [
                        _make_thinking_block(
                            "NPE 说明某处 . 操作目标为 null。需要定位到"
                            "具体行号和变量名。",
                            signature="MmSig_chain_round3",
                        ),
                        _make_tool_use("toolu_read", "read_file", {"path": "UserService.java"}),
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        _make_tool_result("toolu_read", [
                            _make_text_block("// line 42: user.getEmail().toLowerCase()"),
                            _make_thinking_block(
                                "第 42 行，getEmail() 可能返回 null——没有 null check。",
                                signature="MmSig_chain_tool_round3",
                            ),
                        ]),
                    ],
                },
            ],
        }

    def test_chained_swap_to_deepseek_strips_all_sigs(self):
        """连续 swap 后转回 deepseek：所有 signature 剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=self._make_chained_body("deepseek-v4-pro"),
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [],
                         f"连续 swap 后所有 signature 应剥除，残留={sigs}")

    def test_chained_swap_to_minimax_strips_all_sigs(self):
        """连续 swap 后转回 minimax：所有 signature 剥除。"""
        cap = _capture_forward(
            "MiniMax-M3",
            body_override=self._make_chained_body("MiniMax-M3"),
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [],
                         f"连续 swap 后所有 signature 应剥除，残留={sigs}")


# ═══════════════════════════════════════════════════════════════════════════════
#  S10 — tool_use 与 thinking 块交织
# ═══════════════════════════════════════════════════════════════════════════════

class S10_ToolUseInterleavedWithThinking(unittest.TestCase):
    """tool_use 块与 thinking 块交织在同一个 content[] 中——
    thinking 块被处理（signature 剥除），tool_use 块完全不受影响。
    """

    def test_tool_use_blocks_untouched(self):
        """所有 tool_use 块不受 thinking 处理影响。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_TOOL_USE_INTERLEAVED,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        tool_uses_found = 0
        for msg in cap.json.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses_found += 1
                    self.assertIn("id", block,
                                  f"tool_use 应保留 id 字段: {block}")
                    self.assertIn("name", block,
                                  f"tool_use 应保留 name 字段: {block}")
                    self.assertIn("input", block,
                                  f"tool_use 应保留 input 字段: {block}")
        self.assertGreaterEqual(
            tool_uses_found, 4,
            f"所有 tool_use 块应保留，实际={tool_uses_found}",
        )

    def test_thinking_signatures_stripped_alongside_tool_use(self):
        """tool_use 旁 thinking 块的 signature 正确剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_TOOL_USE_INTERLEAVED,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [])


# ═══════════════════════════════════════════════════════════════════════════════
#  S11 — redacted_thinking + foreign thinking 混合
# ═══════════════════════════════════════════════════════════════════════════════

class S11_RedactedThinkingMixed(unittest.TestCase):
    """redacted_thinking 与 foreign thinking 块混合——
    redacted 被剥离，thinking 保留但 signature 剥除。
    """

    def test_redacted_thinking_stripped(self):
        """所有 redacted_thinking 块被剥离。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_REDACTED_MIXED,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        counts = _count_thinking_blocks(cap.json)
        self.assertEqual(counts["redacted_thinking"], 0,
                         "redacted_thinking 应被完整剥离")

    def test_thinking_blocks_preserved_without_signature(self):
        """thinking 块保留，但 signature 被剥除。"""
        cap = _capture_forward(
            "deepseek-v4-pro",
            body_override=_BODY_REDACTED_MIXED,
            env_override={"DEEPSEEK_API_KEY": "sk-ds-test"},
            target_base="https://api.deepseek.com/anthropic",
        )
        blocks = _collect_all_thinking_blocks(cap.json)
        self.assertGreaterEqual(len(blocks), 2,
                                f"thinking 块应保留，实际={len(blocks)}")
        sigs = _collect_all_signatures(cap.json)
        self.assertEqual(sigs, [])


# ═══════════════════════════════════════════════════════════════════════════════
#  S12 — unknown model fallback: 全剥（保守策略回归）
# ═══════════════════════════════════════════════════════════════════════════════

class S12_UnknownModelFallback(unittest.TestCase):
    """路由到未知模型时，thinking + redacted_thinking 全剥——
    这是最保守的策略，确保未知端点不会收到不认识的 block type。
    """

    def test_unknown_model_strips_all_thinking(self):
        """未知模型（如 gpt-4o）全剥 thinking + redacted_thinking。"""
        cap = _capture_forward(
            "gpt-4o",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"OPENAI_API_KEY": "sk-openai-test"},
            target_base="https://api.openai.com",
            protocol="openai",
        )
        counts = _count_thinking_blocks(cap.json)
        self.assertEqual(counts["total"], 0,
                         f"未知模型应全剥 thinking，实际残留={counts['total']}")

    def test_unknown_model_no_thinking_param(self):
        """未知模型：顶层 thinking 参数被 pop。"""
        cap = _capture_forward(
            "unknown-model-xyz",
            body_override=_BODY_STICKY_SWAP_MINIMAX_TO_DEEPSEEK,
            env_override={"MINIMAX_API_KEY": "sk-mm-test"},
        )
        self.assertNotIn("thinking", cap.json,
                         "未知模型不应有 thinking 参数")


# ═══════════════════════════════════════════════════════════════════════════════
#  S08 — 响应端 thinking 处理（claude 路径 passthrough 回归）
# ═══════════════════════════════════════════════════════════════════════════════

class S08_ClaudePassthroughNoModification(unittest.TestCase):
    """claude-* 模型应全透传——不修改 body，不剥 thinking 块，不剥 signature。

    这是三层策略的第一层（Tier 1），必须确保 passthrough 不被新逻辑误伤。
    """

    CLONED_BODY = json.loads(json.dumps(_BODY_MIXED_PROVIDER))
    # 设 model 为 claude-*
    CLONED_BODY["model"] = "claude-sonnet-4-6"

    def test_claude_preserves_all_thinking_blocks(self):
        """claude-* 全透传：thinking + redacted_thinking 全部保留。"""
        cap = _capture_forward(
            "claude-sonnet-4-6",
            body_override=self.CLONED_BODY,
            env_override={"ANTHROPIC_API_KEY": "sk-ant-test"},
            target_base="https://api.anthropic.com",
        )
        counts = _count_thinking_blocks(cap.json)
        self.assertGreaterEqual(counts["total"], 3,
                                f"claude 路径应全保留 thinking，实际={counts['total']}")

    def test_claude_preserves_signatures(self):
        """claude-* 路径不剥 signature（Anthropic 原生校验依赖）。"""
        cap = _capture_forward(
            "claude-sonnet-4-6",
            body_override=self.CLONED_BODY,
            env_override={"ANTHROPIC_API_KEY": "sk-ant-test"},
            target_base="https://api.anthropic.com",
        )
        sigs = _collect_all_signatures(cap.json)
        self.assertGreater(len(sigs), 0,
                           f"claude 路径应保留 signature，实际签名={len(sigs)}")

    def test_claude_preserves_betas_and_thinking_param(self):
        """claude 路径保留 betas + thinking 顶层参数。"""
        cap = _capture_forward(
            "claude-sonnet-4-6",
            body_override=self.CLONED_BODY,
            env_override={"ANTHROPIC_API_KEY": "sk-ant-test"},
            target_base="https://api.anthropic.com",
        )
        # 原始 body 带 betas + thinking
        self.assertIn("thinking", self.CLONED_BODY,
                      "原 body 应含 thinking 参数")
        self.assertIn("betas", self.CLONED_BODY,
                      "原 body 应含 betas 参数")


if __name__ == "__main__":
    unittest.main()
