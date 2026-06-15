"""
decision_engine.py — v1.3 决策核心（pure-Python，零 I/O）
==========================================================

V1.3 §6.1 / §10 路由策略 + §13.1 DecisionRecord schema。

单一入口：`decide(prompt, sid, prompt_id, *, classifier=None) -> DecisionRecord`

约束（Stage 1 验收）：
  - 纯计算：不读文件、不写文件、不发起网络（除注入的 classifier）
  - 依赖注入：`classifier` 参数默认从 llm_classifier.classify 拉；
    单测时可传入自定义函数
  - 保守偏置：模糊 prompt（无明显高/低复杂度信号）→ 强制 medium 及以上
  - 一次锁定：decide() 返回的 record 默认 `locked=True`（后续 maybe_redecide
    在 Stage 5 才会被引入；本阶段只关注"首次决策即锁定"的语义）

后续阶段（Stage 2/5/6）会在此基础上：
  - 接入状态机 transition 校验
  - 暴露 maybe_redecide() 让 PostToolUse 检查 lock 阈值
  - DecisionRecord 被 SessionStateStore 持久化
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Optional

# 同目录 import（与 llm_classifier.py 保持一致的 sys.path 注入策略）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── DecisionRecord schema（V1.3 §13.1）────────────────────────────────────

REQUIRED_FIELDS = (
    "session_id", "prompt_id", "task_pattern", "task_complexity",
    "prompt_confidence", "runtime_score", "todo_score",
    "final_model", "locked", "decision_source", "last_update",
)

# v1.3 路由表：complexity label → 模型
# 简化映射：complex=升级 deepseek-v4-pro；medium/simple=基线 MiniMax-M3
_COMPLEXITY_TO_MODEL: dict[str, str] = {
    "complex": "deepseek-v4-pro",
    "medium":  "MiniMax-M3",
    "simple":  "MiniMax-M3",
}

# 模糊 prompt 关键词（v1.3 §8.3）：命中 → 强制 medium 偏置
# 注：本列表为 Stage 1 保守最小集；Stage 2/7 会与 PATTERN_CONFIG 合并扩展。
_AMBIGUOUS_PROMPT_HINTS: tuple[str, ...] = (
    "帮我",
    "看下",
    "看看",
    "怎么",
    "why",
    "how",
    "什么",
    "优化",
    "重构",
    "改进",
    "调一下",
)


@dataclass(frozen=True)
class DecisionRecord:
    """V1.3 §13.1 路由决策记录。"""

    session_id: str
    prompt_id: str
    task_pattern: str
    task_complexity: str
    prompt_confidence: float
    runtime_score: int
    todo_score: int
    final_model: str
    locked: bool
    decision_source: str
    last_update: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DecisionRecord":
        missing = [k for k in REQUIRED_FIELDS if k not in d]
        if missing:
            raise ValueError(f"DecisionRecord 缺少必填字段: {missing}")
        return cls(**d)


# ── 内部纯函数 ──────────────────────────────────────────────────────────────

def _label_from_score(score: int) -> str:
    """把 0~100 复杂度分映射到 simple/medium/complex（V1.3 §5.2 阈值）。"""
    if score <= 30:
        return "simple"
    if score <= 70:
        return "medium"
    return "complex"


def _is_ambiguous_prompt(prompt: str) -> bool:
    """判断 prompt 是否属于"模糊请求"（V1.3 §8.3）：无明确高/低信号。"""
    p = prompt.lower()
    # 显式高/低关键词未命中 + 模糊关键词命中 → 模糊
    has_explicit = any(
        hint in p
        for hint in (
            "迁移", "架构", "跨模块", "测试修复", "重构整个", "从 0 到 1",
            "typo", "改一下文案", "单行", "改名", "rename",
        )
    )
    if has_explicit:
        return False
    return any(hint in p for hint in _AMBIGUOUS_PROMPT_HINTS)


def _apply_conservative_bias(
    raw_label: str, raw_score: int, prompt: str,
) -> tuple[str, int]:
    """V1.3 §10.3 / §15.2 保守偏置：模糊 prompt → 不低于 medium。"""
    if raw_label == "simple" and _is_ambiguous_prompt(prompt):
        return "medium", max(raw_score, 31)
    return raw_label, raw_score


# ── 默认 classifier 注入 ────────────────────────────────────────────────────

def _default_classifier(prompt: str) -> dict:
    """Stage 1 默认：从 llm_classifier.classify 拉（可能抛 RuntimeError）。"""
    import llm_classifier  # 延迟导入，避免 stage_config 缺失时连环崩
    return llm_classifier.classify(prompt)


# ── 公开 API ────────────────────────────────────────────────────────────────

def decide(
    prompt: str,
    sid: str,
    prompt_id: str,
    *,
    classifier: Optional[Callable[[str], dict]] = None,
) -> DecisionRecord:
    """
    V1.3 决策核心：prompt → DecisionRecord。

    Args:
        prompt: 用户原始 prompt。
        sid: session id（用于 DecisionRecord 落字段）。
        prompt_id: 当前 prompt 的局部 id（V1.3 §15.5 不跨 session 继承）。
        classifier: 可选注入的分类函数 `Callable[[str], dict]`；
                    默认走 `llm_classifier.classify`。

    Returns:
        DecisionRecord（locked=True，runtime_score=0，todo_score=0）。
    """
    classify = classifier or _default_classifier
    raw = classify(prompt)

    pattern = raw.get("pattern", "feature")
    pattern_confidence = float(raw.get("pattern_confidence", 0.0))
    raw_label = raw.get("complexity_label") or _label_from_score(
        int(raw.get("complexity_score", 50))
    )
    raw_score = int(raw.get("complexity_score", 50))
    complexity_confidence = float(raw.get("complexity_confidence", 0.0))

    # raw_score 经 _apply_conservative_bias 重新评估（保守偏置可能抬升），
    # 本阶段只关心最终 label，不直接使用 score
    label, _score = _apply_conservative_bias(raw_label, raw_score, prompt)
    del _score
    final_model = _COMPLEXITY_TO_MODEL.get(label, "MiniMax-M3")

    return DecisionRecord(
        session_id=sid,
        prompt_id=prompt_id,
        task_pattern=pattern,
        task_complexity=label,
        prompt_confidence=min(
            pattern_confidence, complexity_confidence
        ) or max(pattern_confidence, complexity_confidence),
        runtime_score=0,    # Stage 1 决策时无 runtime 数据
        todo_score=0,       # Stage 1 决策时无 todowrite 数据
        final_model=final_model,
        locked=True,        # 一次决策，整段锁定（V1.3 §6.4）
        decision_source="prompt",
        last_update=int(time.time()),
    )
