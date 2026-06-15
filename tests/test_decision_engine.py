"""
test_decision_engine.py — v1.3 决策引擎单测
==============================================

V1.3 §13.1 DecisionRecord schema：
  {
    "session_id": str,
    "prompt_id": str,
    "task_pattern": str,           # feature/bugfix/refactor/test/research/migration/architecture/docs/audit
    "task_complexity": str,        # simple/medium/complex
    "prompt_confidence": float,    # 0~1
    "runtime_score": int,          # 0~100+
    "todo_score": int,             # 0~100
    "final_model": str,            # 路由到的具体模型名
    "locked": bool,                # 是否锁定
    "decision_source": str,        # prompt / runtime / todowrite / manual
    "last_update": int,            # unix ts
  }

测试目标：
  1. DecisionRecord.to_dict / from_dict 双向无损
  2. 必填字段缺失 → from_dict 抛 ValueError（不要静默吞掉）
  3. decide() 纯函数：
     - 显式高复杂 prompt（"迁移整个支付系统"）→ complexity=complex
     - 显式低复杂 prompt（"改一个 typo"）→ complexity=simple
     - 模糊 prompt → 保守偏置（不低于 medium）
     - 输入相同 prompt → 输出 deterministic（无随机）
  4. decide() 不做 I/O（mock llm_classifier.classify 不应被真实调用）
  5. decide() 接受 injected classifier（依赖注入，便于单测）
"""

import unittest
from unittest.mock import patch

from decision_engine import DecisionRecord, decide, _label_from_score


# ── helpers ────────────────────────────────────────────────────────────────

def _mock_classify_result(
    *,
    pattern: str = "feature",
    pattern_confidence: float = 0.9,
    complexity_label: str = "medium",
    complexity_score: int = 50,
    complexity_confidence: float = 0.9,
) -> dict:
    return {
        "stage": "implement",  # legacy field — v1.3 保留兼容，但不再用于路由
        "pattern": pattern,
        "pattern_confidence": pattern_confidence,
        "complexity_score": complexity_score,
        "complexity_label": complexity_label,
        "complexity_confidence": complexity_confidence,
        "reasoning": "mock",
        "source": "llm",
    }


# ── DecisionRecord schema ──────────────────────────────────────────────────

class TestDecisionRecordSchema(unittest.TestCase):
    def test_to_dict_contains_all_required_fields(self):
        r = DecisionRecord(
            session_id="s1",
            prompt_id="p1",
            task_pattern="feature",
            task_complexity="medium",
            prompt_confidence=0.9,
            runtime_score=10,
            todo_score=0,
            final_model="MiniMax-M3",
            locked=False,
            decision_source="prompt",
            last_update=1700000000,
        )
        d = r.to_dict()
        for k in (
            "session_id", "prompt_id", "task_pattern", "task_complexity",
            "prompt_confidence", "runtime_score", "todo_score",
            "final_model", "locked", "decision_source", "last_update",
        ):
            self.assertIn(k, d)
        self.assertEqual(d["session_id"], "s1")
        self.assertEqual(d["final_model"], "MiniMax-M3")
        self.assertFalse(d["locked"])

    def test_from_dict_roundtrip(self):
        original = DecisionRecord(
            session_id="s2",
            prompt_id="p2",
            task_pattern="refactor",
            task_complexity="complex",
            prompt_confidence=0.8,
            runtime_score=42,
            todo_score=70,
            final_model="deepseek-v4-pro",
            locked=True,
            decision_source="todowrite",
            last_update=1700000001,
        )
        restored = DecisionRecord.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_from_dict_rejects_missing_required_field(self):
        bad = {
            "session_id": "s3",
            "prompt_id": "p3",
            # 缺 task_pattern
            "task_complexity": "simple",
            "prompt_confidence": 0.5,
            "runtime_score": 0,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": False,
            "decision_source": "prompt",
            "last_update": 0,
        }
        with self.assertRaises(ValueError):
            DecisionRecord.from_dict(bad)


# ── 内部纯函数 ─────────────────────────────────────────────────────────────

class TestLabelFromScore(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(_label_from_score(0), "simple")
        self.assertEqual(_label_from_score(30), "simple")

    def test_medium(self):
        self.assertEqual(_label_from_score(31), "medium")
        self.assertEqual(_label_from_score(70), "medium")

    def test_complex(self):
        self.assertEqual(_label_from_score(71), "complex")
        self.assertEqual(_label_from_score(100), "complex")


# ── decide() 纯函数 ────────────────────────────────────────────────────────

class TestDecidePureFunction(unittest.TestCase):
    """decide() 必须纯：mock 掉 llm_classifier.classify 避免真实网络。"""

    def test_explicit_complex_prompt_yields_complex_decision(self):
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(
                pattern="migration",
                complexity_label="complex",
                complexity_score=85,
            ),
        ) as mocked:
            rec = decide(
                prompt="迁移整个支付系统到新架构",
                sid="sid-c1",
                prompt_id="p-c1",
            )
        mocked.assert_called_once()
        self.assertEqual(rec.task_pattern, "migration")
        self.assertEqual(rec.task_complexity, "complex")
        self.assertEqual(rec.final_model, "deepseek-v4-pro")  # 升级模型
        self.assertFalse(rec.locked)  # 首次决策可改：locked=False 由 maybe_redecide 锁定
        self.assertEqual(rec.decision_source, "prompt")

    def test_explicit_simple_prompt_yields_simple_decision(self):
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(
                pattern="docs",
                complexity_label="simple",
                complexity_score=10,
            ),
        ):
            rec = decide(
                prompt="改一下 README 第 3 行的 typo",
                sid="sid-s1",
                prompt_id="p-s1",
            )
        self.assertEqual(rec.task_complexity, "simple")
        self.assertEqual(rec.final_model, "MiniMax-M3")  # 默认基线
        self.assertFalse(rec.locked)  # 首次决策可改：locked=False 由 maybe_redecide 锁定

    def test_ambiguous_prompt_is_biased_towards_medium(self):
        """模糊 prompt → 保守偏置：不低于 medium。"""
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(
                pattern="feature",
                complexity_label="simple",
                complexity_score=15,  # LLM 说 simple
            ),
        ):
            rec = decide(
                prompt="帮我优化一下",
                sid="sid-amb",
                prompt_id="p-amb",
            )
        # 模糊 prompt 关键词未触发 + 保守偏置
        self.assertIn(rec.task_complexity, ("medium", "complex"))

    def test_decide_is_deterministic(self):
        """相同输入 → 相同输出（无随机）。"""
        kwargs = dict(
            prompt="写一个 hello world",
            sid="sid-det",
            prompt_id="p-det",
        )
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(
                pattern="docs", complexity_label="simple", complexity_score=5,
            ),
        ):
            r1 = decide(**kwargs)
            r2 = decide(**kwargs)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_decide_does_not_perform_io(self):
        """决定时不应触碰文件系统。"""
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(),
        ):
            with patch("decision_engine.open", create=True) as open_mock:
                rec = decide(prompt="x", sid="sid-io", prompt_id="p-io")
                # 任何对 open 的实际调用都不该发生
                self.assertFalse(open_mock.called)
        # sanity
        self.assertEqual(rec.session_id, "sid-io")

    def test_decide_sets_runtime_score_zero_at_prompt_time(self):
        """decide() 仅在 prompt 阶段；runtime_score 此时必为 0。"""
        with patch(
            "llm_classifier.classify",
            return_value=_mock_classify_result(),
        ):
            rec = decide(prompt="y", sid="s", prompt_id="p")
        self.assertEqual(rec.runtime_score, 0)
        self.assertEqual(rec.todo_score, 0)


if __name__ == "__main__":
    unittest.main()
