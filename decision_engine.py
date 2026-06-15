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
  - 首次决策可改：decide() 返回的 record 默认 `locked=False`（首次只是
    "暂定"，后续 PostToolUse 累积 runtime_score 或命中 TodoWrite 强信号，
    maybe_redecide() 才会升级并 lock；一旦 locked=True 永不变）
  - 这样分工：decide() 给"prompt 先验"，maybe_redecide() 才是
    "Runtime 实证后的终裁"

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

# V1.3 §15.4 路由理由（细粒度人类可读）
OPTIONAL_FIELDS = (
    "reasoning",         # 路由理由（人类可读）
    "reason_code",       # 升级原因代码（machine-readable）
    "created_at",        # 决策创建时间戳
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
    # V1.3 §15.4 可选细粒度字段（向后兼容）
    reasoning: str = ""         # 路由理由（人类可读）
    reason_code: str = ""       # 升级原因代码
    created_at: int = 0         # 决策创建时间戳

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DecisionRecord":
        missing = [k for k in REQUIRED_FIELDS if k not in d]
        if missing:
            raise ValueError(f"DecisionRecord 缺少必填字段: {missing}")
        # 兼容老 dict 缺可选字段的情况
        kwargs = dict(d)
        for opt in OPTIONAL_FIELDS:
            kwargs.setdefault(opt, "" if opt != "created_at" else 0)
        return cls(**kwargs)


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

# ── 复杂度比较（only-upgrade-never-downgrade 语义）─────────────────────

_COMPLEXITY_RANK: dict[str, int] = {
    "simple": 0,
    "medium": 1,
    "complex": 2,
}


def _max_complexity(a: str, b: str) -> str:
    """返回 a、b 中较高的复杂度（只升不降）。"""
    ra = _COMPLEXITY_RANK.get(a, 0)
    rb = _COMPLEXITY_RANK.get(b, 0)
    return a if ra >= rb else b


# ── maybe_redecide ─────────────────────────────────────────────────────────

def maybe_redecide(
    sid: str,
    project_root: str,
    runtime_score: int,
    todowrite_signal: Optional[dict] = None,
) -> Optional[DecisionRecord]:
    """V1.3 §6.4 PostToolUse 决策重算：runtime_score 累积 + TodoWrite 强信号。

    行为契约（Stage 5.1 / 5.2）：
      1. model_router_state_<sid>.json 缺失或 decision 为空 → 不当场重决策，
         返回 None（避免在未初始化 session 上空跑）。
      3. decision.locked=True → 不重决策，返回 None。
      4. 计算候选 complexity：
           - runtime_score > 70 → complex
           - 31 <= runtime_score <= 70 → medium
           - runtime_score <= 30 → simple
         与当前 task_complexity 取 max（只升不降）。
      5. todowrite_signal.is_implementation=True → 强制至少 medium，
         立即 lock（即使 runtime_score 不足）。
      6. 实际有变化（new_complexity > current 或 todowrite 触发锁）才
         写回 session_state 并返回新 DecisionRecord；否则返回 None。

    Args:
        sid: session id。
        project_root: 项目根目录。
        runtime_score: RuntimeTracker 累积分数（0~100）。
        todowrite_signal: TodoWriteAnalyzer.analyze() 的结果，可为 None。

    Returns:
        新 DecisionRecord（升级/锁定时）或 None（无需重决策）。
    """
    # 延迟导入避免循环 + 路径问题
    from state_persistence import SessionStateStore

    store = SessionStateStore()
    state = store.read_new(sid, project_root)
    if not state:
        return None

    decision = state.get("decision") or {}
    if not decision:
        return None

    # 已锁 → 不重决策
    if decision.get("locked"):
        return None

    current_label = decision.get("task_complexity", "simple")
    current_rank = _COMPLEXITY_RANK.get(current_label, 0)

    # ── 计算 runtime_score 候选 label ──
    runtime_label = _label_from_score(runtime_score)

    # ── 计算 todowrite 强信号候选 label ──
    todo_force_lock = False
    todo_label = current_label
    if isinstance(todowrite_signal, dict) and todowrite_signal.get("is_implementation"):
        todo_force_lock = True
        # 实施类 todo → 强制至少 medium
        if _COMPLEXITY_RANK.get(current_label, 0) < _COMPLEXITY_RANK["medium"]:
            todo_label = "medium"

    # ── 融合：max(current, runtime, todo) ──
    merged = _max_complexity(_max_complexity(current_label, runtime_label), todo_label)
    merged_rank = _COMPLEXITY_RANK.get(merged, 0)

    # ── 决定是否升级 / 锁 ──
    promoted = merged_rank > current_rank
    need_lock = todo_force_lock

    if not (promoted or need_lock):
        # 无变化且不需要锁 → 不写
        return None

    # ── 写回 ──
    new_label = merged
    source = "todowrite" if todo_force_lock else "runtime"

    # V1.3 §15.4 路由理由：细粒度人类可读
    if todo_force_lock:
        reasoning = (
            f"TodoWrite 触发实施信号，强制至少 medium（{current_label} → {new_label}）"
        )
        reason_code = "todowrite_force"
    elif promoted:
        if runtime_label == "complex" and new_label == "complex":
            reasoning = f"runtime_score 快速上升至 {runtime_score}（>70），升级到 complex"
            reason_code = "runtime_threshold_70"
        elif runtime_label == "medium" and new_label == "medium":
            reasoning = f"runtime_score 中等累积（{runtime_score}），从 {current_label} 升至 medium"
            reason_code = "runtime_medium"
        else:
            reasoning = f"runtime 累积触发升级（{current_label} → {new_label}，score={runtime_score}）"
            reason_code = "runtime_general"
    else:
        reasoning = f"无变化（{current_label}，score={runtime_score}）"
        reason_code = "no_change"

    new_decision = dict(decision)
    new_decision.update({
        "task_complexity": new_label,
        "runtime_score": runtime_score,
        "locked": True,
        "decision_source": source,
        "last_update": int(time.time()),
        "reasoning": reasoning,
        "reason_code": reason_code,
    })
    # 兼容创建时间戳
    if "created_at" not in new_decision or not new_decision.get("created_at"):
        new_decision["created_at"] = new_decision["last_update"]

    # 仅当复杂度确实升级到更高 tier 时才更新 final_model；
    # 否则保留现有模型（特别是 ~model 显式覆盖不应被 maybe_redecide 降级）
    if promoted:
        new_decision["final_model"] = _COMPLEXITY_TO_MODEL.get(
            new_label, "MiniMax-M3"
        )

    # 写回：仅写新格式（不再双写旧 9 文件 — Stage 5 不动 stage_detector）
    store.write(sid, project_root, decision=new_decision)

    # ── V1.3 §11 Context Summary 注入：跨档升级时生成摘要 ──
    try:
        from context_summary import ContextSummaryInjector
        _injector = ContextSummaryInjector()
        full_state = store.read_new(sid, project_root) or {}
        if _injector.should_inject(full_state, new_label):
            summary = _injector.build_summary(full_state, prompt=full_state.get("prompt"))
            _injector.mark_injected(full_state, summary)
            # 写回 context_summary 字段
            store.write(sid, project_root, decision=new_decision,
                        context_summary=summary)
    except Exception:
        # 摘要生成失败不影响核心路由
        pass

    return DecisionRecord.from_dict(new_decision)


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
        DecisionRecord（locked=False — 首次决策只是暂定；
        runtime_score 累积或 TodoWrite 强信号由 maybe_redecide() 升级并 lock）。
    """
    classify = classifier or _default_classifier
    raw = classify(prompt)

    pattern = raw.get("pattern", "implement")
    pattern_confidence = float(raw.get("pattern_confidence", 0.0))
    raw_label = raw.get("complexity_label") or _label_from_score(
        int(raw.get("complexity_score", 50))
    )
    raw_score = int(raw.get("complexity_score", 50))
    complexity_confidence = float(raw.get("complexity_confidence", 0.0))
    reasoning = str(raw.get("reasoning", "")).strip()

    # raw_score 经 _apply_conservative_bias 重新评估（保守偏置可能抬升），
    # 本阶段只关心最终 label，不直接使用 score
    label, _score = _apply_conservative_bias(raw_label, raw_score, prompt)
    del _score
    final_model = _COMPLEXITY_TO_MODEL.get(label, "MiniMax-M3")

    # V1.3 §15.4 路由理由：人类可读
    biased = label != raw_label
    if biased:
        prompt_reasoning = f"prompt 模糊，保守偏置抬升 {raw_label} → {label}"
        if reasoning:
            prompt_reasoning = f"{reasoning}；{prompt_reasoning}"
    else:
        prompt_reasoning = reasoning or f"prompt 显式 {label}"

    now = int(time.time())
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
        locked=False,       # 首次决策可改：maybe_redecide 升级时才锁定
        decision_source="prompt",
        last_update=now,
        reasoning=prompt_reasoning,
        reason_code="prompt_classify",
        created_at=now,
    )
