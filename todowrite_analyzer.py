"""
todowrite_analyzer.py — v1.3 TodoWrite Analyzer（PostToolUse worker）
======================================================================

V1.3 §8.3 / §9 TodoWrite Analyzer — 首个强信号检测器。

TodoWriteAnalyzer 分析 TodoWrite 工具的输出内容，检测：
  - 是否为"真实实施"信号（新增非 trivial items）
  - 任务复杂度等级（基于 todo 内容语义 + 结构特征）
  - 跨文件/跨模块/测试/迁移等多维特征（§9.2 7 维分析）
  - 是否触发 todowrite_detected 状态转移信号

V1.3 §13.3 输出 schema：
  - is_implementation, is_first_todo_write
  - total, pending, completed
  - complexity_signal (float 0~1)
  - cross_file, has_tests, has_migration
  - todo_complexity (simple/medium/complex)
  - confidence (float 0~1)

设计约束：
  - 零 I/O（纯计算 — 关键词启发式）
  - analyze_with_llm() 可选 LLM 深度分析（复用 llm_classifier 基础设施）
  - 所有异常静默吞掉（返回空结果）
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


# ── 实现信号关键词 ──────────────────────────────────────────────────────
# 这些动词表示"正在写代码/改代码"，不是"正在读/理解"
_IMPLEMENTATION_KEYWORDS = (
    "implement", "fix", "refactor", "build", "add", "create",
    "write", "debug", "modify", "update", "change", "remove",
    "delete", "replace", "extract", "rename", "move", "merge",
    "optimize", "upgrade", "migrate", "rewrite", "patch",
)

# ── 跨文件信号 ──────────────────────────────────────────────────────────
_CROSS_FILE_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\b(extract|move|split|separate)\s+(to|into)\s',
        r'\bcreate\s+(new\s+)?file\b',
        r'\badd\s+(new\s+)?file\b',
        r'\bmultiple\s+files?\b',
        r'\bacross\s+files?\b',
        r'\b跨文件\b',
        r'\b(cross.file|multi.file)\b',
        r'\.(py|js|ts|go|java|rs|rb|php)\b.*\b(and|,).*\.(py|js|ts|go|java|rs|rb|php)\b',
        r'\b(每个|各个|different|separate)\s+(文件|file)',
        r'\b(controller|service|model|util|helper|component|module)\b',
    ]
]

# ── 跨模块信号 ──────────────────────────────────────────────────────────
_CROSS_MODULE_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\b(cross.module|跨模块)\b',
        r'\b(多个模块|multiple\s+modules?)\b',
        r'\b(api|database|frontend|backend|auth|storage|cache|queue)\b.*\b(and|与|、).*\b(api|database|frontend|backend|auth|storage|cache|queue)\b',
        r'\b(integration|集成)\b',
        r'\b(end.to.end|e2e)\b',
        r'\b(system.test|系统测试)\b',
        r'\b(package|library|dependency|framework)\s+(update|upgrade|change)',
    ]
]

# ── 测试信号 ────────────────────────────────────────────────────────────
_TEST_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\btest(s|ing)?\b',
        r'\b(spec|specification)\b',
        r'\bassert(ion)?s?\b',
        r'\bverify|验证\b',
        r'\b(unit|单元)\s*(test|测试)',
        r'\b(integration|集成)\s*(test|测试)',
        r'\b(coverage|覆盖率)\b',
        r'\b(回归|regression)\s*(test|测试)?',
        r'\b(mock|stub|fixture)\b',
        r'\b(TODO|FIXME).*test',
    ]
]

# ── 迁移/兼容/重构信号 ──────────────────────────────────────────────────
_MIGRATION_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\b(migrate|migration|迁移)\b',
        r'\b(upgrade|升级)\s+(version|版本|dependency|依赖)',
        r'\b(breaking\s+change|破坏性|不兼容)',
        r'\b(backward|向后)\s*(compatib|兼容)',
        r'\b(deprecated|废弃|过时)\b',
        r'\b(compatibility|兼容性)\b',
        r'\brewrite|重写\b',
        r'\b(port|移植)\s+(to|到)\b',
        r'\breplace\s+(library|dependency|framework|库|框架)',
    ]
]

# ── 重构信号 ────────────────────────────────────────────────────────────
_REFACTOR_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\brefactor|重构\b',
        r'\b(restructure|重组)\b',
        r'\b(clean\s*up|清理)\b',
        r'\b(extract|提取)\s+(method|function|class|module|方法|函数|类)',
        r'\b(rename|重命名)\b',
        r'\b(拆分|合并|decouple|解耦)\b',
    ]
]

# ── 高复杂度依赖信号 ────────────────────────────────────────────────────
_HIGH_DEPENDENCY_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r'\b(before|after|前置|后置|依赖|depends\s+on)\b',
        r'\b(step|步骤)\s*[1-9]\b',
        r'\b(first|首先|then|然后|finally|最后)\b.*\b(first|首先|then|然后|finally|最后)\b',
        r'\b(prerequisite|先决条件)\b',
        r'\b(blocked|阻塞)\s+(by|于)',
    ]
]


# ── LLM 配置解析辅助 ──────────────────────────────────────────────────
# 把"从 stage_config.LLM_CLASSIFIER_CONFIG 取配置 + import 兜底"抽成独立函数：
#   1. 让 _llm_analyze 不再每次重复 sys.path 黑魔法
#   2. 让 import 失败时显式返回异常类型，调用方用 warnings.warn 暴露而不是静默 cfg={}
#   3. 未来若 stage_config 改名或迁移，单独改这一处即可
#
# 注意：保留 sys.path 黑魔法是因为 caller (post_tool_handler.py) 的 inline import
# 不能保证 cwd 是 hooks/model_router/，从 test 目录或 worktree 根跑都会失败。
def _resolve_llm_config() -> tuple[Dict[str, Any], Optional[str]]:
    """加载 LLM_CLASSIFIER_CONFIG，失败时返回 ({}, error_msg)。

    Returns:
        (cfg_dict, error_msg_or_None)。cfg_dict 始终为 dict（成功时为 stage_config
        的拷贝，失败时为空 dict，调用方继续用内置默认）；error_msg 在 import 失败
        时为短字符串，便于上层诊断。
    """
    import sys
    from pathlib import Path

    try:
        # 同目录 sys.path 注入（参考 llm_classifier.py 同一处理）
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from stage_config import LLM_CLASSIFIER_CONFIG  # noqa: E402
        return dict(LLM_CLASSIFIER_CONFIG), None
    except (ImportError, AttributeError) as e:
        return {}, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 — 兜底任何意外
        return {}, f"{type(e).__name__}: {e}"


class TodoWriteAnalyzer:
    """TodoWrite 工具输出分析器（V1.3 §9）。

    纯文本解析 + 可选 LLM 深度分析。分析 todos 列表，判断是否为
    "真实实施"信号并计算多维复杂度信号。
    """

    def analyze(self, todos: Any, *, is_first: bool = False) -> Dict[str, Any]:
        """分析 TodoWrite 输出的 todos 列表（关键词启发式）。

        Args:
            todos: TodoWrite tool_input.todos 的值，应为
                   list[dict]，每个 dict 含 content 和 status 字段。
                   容错：接受 None / str / 非标格式。
            is_first: 是否为本 session 第一次 TodoWrite。

        Returns:
            dict — V1.3 §13.3 schema + 向后兼容字段：
                - is_implementation: bool
                - is_first_todo_write: bool
                - total: int
                - pending: int
                - completed: int
                - complexity_signal: float — [0, 1]
                - todo_complexity: str — "simple"/"medium"/"complex"
                - cross_file: bool
                - has_tests: bool
                - has_migration: bool
                - confidence: float — [0, 1]
        """
        try:
            return self._analyze(todos, is_first=is_first)
        except Exception:
            return self._empty_result()

    def analyze_with_llm(self, todos: Any, *, is_first: bool = False) -> Dict[str, Any]:
        """LLM 深度分析 TodoWrite 内容（V1.3 §9.2）。

        先尝试 LLM 分类，失败时回退到关键词启发式分析。
        仅在首次 TodoWrite 时建议调用（is_first=True）。

        失败回退时会在 result 中附加 ``_llm_fallback_reason`` 字段（短字符串），
        便于上游观测回退原因（避免"import 失败 / 缺 env / LLM 网络错 / JSON 解析错"
        全被静默吞掉后无法区分）。该字段以下划线开头，表示非标准 schema。
        """
        try:
            todos_text = self._todos_to_text(todos)
            if not todos_text:
                return self._empty_result()

            result = self._llm_analyze(todos_text)
            result["is_first_todo_write"] = is_first
            # 补充 is_implementation（LLM 不直接输出该字段）
            if "is_implementation" not in result:
                result["is_implementation"] = (
                    result.get("todo_complexity", "simple") != "simple"
                    or result.get("pending", 0) > 0
                )
            return result
        except Exception as e:
            # LLM 失败 → 回退关键词，并把异常原因透传给上层做诊断
            fallback = self.analyze(todos, is_first=is_first)
            fallback["_llm_fallback_reason"] = f"{type(e).__name__}: {e}"
            return fallback

    # ── Internal: Keyword Analysis ────────────────────────────────────────

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        return {
            "is_implementation": False,
            "is_first_todo_write": False,
            "total": 0,
            "pending": 0,
            "completed": 0,
            "complexity_signal": 0.0,
            "todo_complexity": "simple",
            "cross_file": False,
            "has_tests": False,
            "has_migration": False,
            "confidence": 0.0,
        }

    def _analyze(self, todos: Any, *, is_first: bool = False) -> Dict[str, Any]:
        if not isinstance(todos, list):
            result = self._empty_result()
            result["is_first_todo_write"] = is_first
            return result

        total = 0
        pending = 0
        completed = 0
        has_impl = False
        all_content: list[str] = []

        # ── 内容特征累积 ──
        cross_file = False
        has_tests = False
        has_migration = False
        has_refactor = False
        has_dependency_chain = False

        for item in todos:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not content or not isinstance(content, str):
                continue

            total += 1
            all_content.append(content)
            status = item.get("status", "pending")

            if status in ("pending", "in_progress"):
                pending += 1
                if self._is_implementation(content):
                    has_impl = True

                # ── 检测跨文件 ──
                if not cross_file and self._match_any(content, _CROSS_FILE_PATTERNS):
                    cross_file = True

                # ── 检测测试 ──
                if not has_tests and self._match_any(content, _TEST_PATTERNS):
                    has_tests = True

                # ── 检测迁移/兼容/重构 ──
                if not has_migration and self._match_any(content, _MIGRATION_PATTERNS):
                    has_migration = True

                # ── 检测重构 ──
                if not has_refactor and self._match_any(content, _REFACTOR_PATTERNS):
                    has_refactor = True

                # ── 检测依赖链 ──
                if not has_dependency_chain and self._match_any(content, _HIGH_DEPENDENCY_PATTERNS):
                    has_dependency_chain = True

            elif status == "completed":
                completed += 1

        # ── 综合复杂度信号（V1.3 §9.2 多维融合）──
        complexity_score = 0.0
        dims_active = 0

        # 维度 1: pending 数量（dominant signal — 10+ pending 直接驱动 high complexity）
        # 阈值 0.5 在 5 个 pending 时跨越，对应"中等到复杂"任务
        count_signal = min(pending / 5.0, 1.0) if pending > 0 else 0.0
        complexity_score += count_signal * 0.5
        dims_active += 1

        # 维度 2: 跨文件
        if cross_file:
            complexity_score += 0.20
            dims_active += 1

        # 维度 3: 包含测试
        if has_tests:
            complexity_score += 0.10
            dims_active += 1

        # 维度 4: 包含迁移/兼容
        if has_migration:
            complexity_score += 0.20
            dims_active += 1

        # 维度 5: 包含重构
        if has_refactor:
            complexity_score += 0.10
            dims_active += 1

        # 维度 6: 依赖链
        if has_dependency_chain:
            complexity_score += 0.10
            dims_active += 1

        # 维度 7: pending todo 文本长度特征（复杂任务通常描述更详细）
        avg_content_len = sum(len(c) for c in all_content) / max(len(all_content), 1)
        if avg_content_len > 80:
            complexity_score += 0.05
            dims_active += 1

        # 归一化到 [0, 1]
        complexity_signal = min(complexity_score, 1.0)

        # ── 确定 todo_complexity label ──
        if complexity_signal >= 0.65:
            todo_complexity = "complex"
        elif complexity_signal >= 0.30:
            todo_complexity = "medium"
        else:
            todo_complexity = "simple"

        # ── 置信度：基于活跃维度数 / 总维度数 ──
        confidence = min(dims_active / 7.0, 1.0)

        # ── is_implementation：有未完成的实施类 todo ──
        is_impl = has_impl and pending > 0

        return {
            "is_implementation": is_impl,
            "is_first_todo_write": is_first,
            "total": total,
            "pending": pending,
            "completed": completed,
            "complexity_signal": round(complexity_signal, 2),
            "todo_complexity": todo_complexity,
            "cross_file": cross_file,
            "has_tests": has_tests,
            "has_migration": has_migration or has_refactor,
            "confidence": round(confidence, 2),
        }

    # ── Internal: Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _is_implementation(content: str) -> bool:
        """检查 todo 内容是否包含实现关键词（大小写不敏感）。"""
        lowered = content.lower()
        return any(kw in lowered for kw in _IMPLEMENTATION_KEYWORDS)

    @staticmethod
    def _match_any(text: str, patterns: list) -> bool:
        """检查文本是否匹配任一模式。"""
        return any(p.search(text) for p in patterns)

    @staticmethod
    def _todos_to_text(todos: Any) -> str:
        """将 todos 列表转为适合 LLM 分析的文本。"""
        if not isinstance(todos, list):
            return ""
        lines = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                continue
            content = item.get("content", "")
            status = item.get("status", "pending")
            if content and isinstance(content, str):
                lines.append(f"{i + 1}. [{status}] {content}")
        return "\n".join(lines)

    # ── Internal: LLM Analysis ────────────────────────────────────────────

    _TODO_ANALYSIS_SYSTEM_PROMPT = """\
You are a task complexity analyzer. Analyze the given todo list and classify it.
Return ONLY valid JSON (no markdown fences, no extra text).

## Analysis dimensions:
1. **todo_complexity**: "simple", "medium", or "complex"
   - simple: ≤3 items, single-file changes, trivial tasks
   - medium: 4-8 items, may span multiple files, some design work
   - complex: >8 items, cross-module, architectural changes, testing required
2. **cross_file**: true if tasks span multiple files/directories
3. **has_tests**: true if any todo mentions testing, verification, or assertions
4. **has_migration**: true if tasks involve migration, version upgrades, or compatibility
5. **confidence**: 0.0-1.0 how confident you are in this analysis
6. **reasoning**: brief one-line explanation in Chinese

## Response format (JSON only):
{
  "todo_complexity": "medium",
  "cross_file": true,
  "has_tests": false,
  "has_migration": false,
  "confidence": 0.85,
  "reasoning": "中等规模实现，跨3个文件"
}"""

    def _llm_analyze(self, todos_text: str) -> Dict[str, Any]:
        """调用 LLM 对 TodoWrite 内容进行深度分析。

        复用 llm_classifier 的 Anthropic SDK 基础设施。
        失败时抛出异常（由 analyze_with_llm 兜底回退）。
        """
        import anthropic
        import httpx
        import os
        import sys
        import warnings
        from pathlib import Path

        # ── 加载配置（_resolve_llm_config 做了 import 兜底 + 显式告警）──
        cfg, import_err = _resolve_llm_config()
        if import_err is not None:
            # ImportError / AttributeError 都用 UserWarning 暴露，比静默 cfg={} 强
            # warnings 不会阻断流程，analyze_with_llm 仍会兜底回退到关键词启发式。
            warnings.warn(
                f"[todowrite_analyzer] stage_config 不可用（{import_err}），"
                f"LLM 配置将使用内置默认值",
                stacklevel=2,
            )

        # 默认配置（与 llm_classifier 保持一致）
        model = cfg.get("model", "deepseek-v4-flash")
        base_url = cfg.get("base_url", "https://api.deepseek.com/anthropic")
        api_key_env = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
        timeout = int(cfg.get("timeout", 15))
        proxy = cfg.get("proxy", None)
        max_tokens = 256

        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"环境变量 {api_key_env} 未设置")

        # ── 截断过长内容 ──
        max_chars = 4000
        if len(todos_text) > max_chars:
            todos_text = todos_text[:max_chars] + "\n... (truncated)"

        # ── 构造客户端 ──
        http_client = None
        if proxy:
            transport = httpx.HTTPTransport(proxy=httpx.Proxy(url=httpx.URL(proxy)))
            http_client = httpx.Client(transport=transport, timeout=httpx.Timeout(timeout, connect=10.0))

        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=float(timeout),
            max_retries=0,
            http_client=http_client,
        )

        # ── 调用 LLM ──
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            system=self._TODO_ANALYSIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": todos_text}],
        )

        # ── 提取文本 ──
        text = ""
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        text = text.strip()

        if not text:
            raise RuntimeError("LLM 返回空文本")

        # ── 解析 JSON ──
        import json as json_mod
        import re as re_mod

        result = None
        try:
            result = json_mod.loads(text)
        except json_mod.JSONDecodeError:
            m = re_mod.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re_mod.DOTALL)
            if m:
                result = json_mod.loads(m.group(1).strip())
            else:
                first = text.find("{")
                last = text.rfind("}")
                if first >= 0 and last > first:
                    result = json_mod.loads(text[first:last + 1])

        if not isinstance(result, dict):
            raise RuntimeError(f"LLM 返回非 dict: {type(result).__name__}")

        # ── 规范化 ──
        return {
            "is_implementation": True,
            "is_first_todo_write": True,
            "total": 0,
            "pending": 0,
            "completed": 0,
            "complexity_signal": {
                "simple": 0.2, "medium": 0.55, "complex": 0.85,
            }.get(result.get("todo_complexity", "medium"), 0.55),
            "todo_complexity": result.get("todo_complexity", "medium"),
            "cross_file": bool(result.get("cross_file", False)),
            "has_tests": bool(result.get("has_tests", False)),
            "has_migration": bool(result.get("has_migration", False)),
            "confidence": float(result.get("confidence", 0.7)),
        }
