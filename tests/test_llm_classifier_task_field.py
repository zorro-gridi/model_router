"""
test_llm_classifier_task_field.py — V1.4 task_field 分类维度单测
================================================================

V1.4 新增第 4 维分类：task_field（业务领域）。
- 5 个合法枚举：frontend / backend / ops / product / unknown
- 不参与路由决策，仅在 statusline 第三行展示
- 非法值 / 缺失值 → 兜底 "unknown"（保留 schema 形状契约）

本测试覆盖 _validate_and_normalize 端到端的 task_field 处理，
不需要发起 LLM 真实调用，纯函数行为验证。
"""

import os
import sys
import unittest

# 与被测模块同目录的 import 约定一致
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm_classifier import (  # noqa: E402
    VALID_TASK_FIELDS,
    _validate_and_normalize,
)


def _base_raw() -> dict:
    """构造一个最小合法 raw 输入（V1.3 三个维度都有值）。"""
    return {
        "stage": "implement",
        "pattern": "feature",
        "pattern_confidence": 0.9,
        "complexity_score": 45,
        "complexity_label": "medium",
        "complexity_confidence": 0.8,
        "reasoning": "test",
    }


class TestTaskFieldValidation(unittest.TestCase):
    """task_field 字段在 _validate_and_normalize 中的归一化行为。"""

    # ── 5 个合法枚举都能透传 ──

    def test_frontend_valid(self):
        raw = _base_raw() | {"task_field": "frontend", "task_field_confidence": 0.91}
        out = _validate_and_normalize(raw, "实现一个登录页面")
        self.assertEqual(out["task_field"], "frontend")
        self.assertEqual(out["task_field_confidence"], 0.91)

    def test_backend_valid(self):
        raw = _base_raw() | {"task_field": "backend", "task_field_confidence": 0.85}
        out = _validate_and_normalize(raw, "写一个用户接口")
        self.assertEqual(out["task_field"], "backend")
        self.assertEqual(out["task_field_confidence"], 0.85)

    def test_ops_valid(self):
        raw = _base_raw() | {"task_field": "ops", "task_field_confidence": 0.7}
        out = _validate_and_normalize(raw, "配置 nginx 反向代理")
        self.assertEqual(out["task_field"], "ops")
        self.assertEqual(out["task_field_confidence"], 0.7)

    def test_product_valid(self):
        raw = _base_raw() | {"task_field": "product", "task_field_confidence": 0.65}
        out = _validate_and_normalize(raw, "写一份 PRD")
        self.assertEqual(out["task_field"], "product")
        self.assertEqual(out["task_field_confidence"], 0.65)

    def test_unknown_valid(self):
        raw = _base_raw() | {"task_field": "unknown", "task_field_confidence": 0.5}
        out = _validate_and_normalize(raw, "你好")
        self.assertEqual(out["task_field"], "unknown")
        self.assertEqual(out["task_field_confidence"], 0.5)

    # ── 大小写 / 空白归一化 ──

    def test_task_field_uppercase_normalized(self):
        """LLM 偶尔返回大写或前后空白 → 应归一为 lowercase。"""
        raw = _base_raw() | {"task_field": "  Frontend  ", "task_field_confidence": 0.9}
        out = _validate_and_normalize(raw, "fix UI bug")
        self.assertEqual(out["task_field"], "frontend")

    # ── 非法值兜底 ──

    def test_task_field_invalid_falls_back_to_unknown(self):
        """未在白名单的 task_field → 兜底 unknown，schema 形状契约不破。"""
        raw = _base_raw() | {"task_field": "data_science", "task_field_confidence": 0.6}
        out = _validate_and_normalize(raw, "训练一个分类模型")
        self.assertEqual(out["task_field"], "unknown")
        # 置信度仍然保留（合法 float）
        self.assertEqual(out["task_field_confidence"], 0.6)

    def test_task_field_empty_falls_back_to_unknown(self):
        raw = _base_raw() | {"task_field": "", "task_field_confidence": 0.5}
        out = _validate_and_normalize(raw, "anything")
        self.assertEqual(out["task_field"], "unknown")

    def test_task_field_missing_falls_back_to_unknown(self):
        """完全缺失 task_field 字段 → 兜底 unknown，置信度默认值 0.5。"""
        raw = _base_raw()  # 没有 task_field
        out = _validate_and_normalize(raw, "anything")
        self.assertEqual(out["task_field"], "unknown")
        self.assertEqual(out["task_field_confidence"], 0.5)

    def test_task_field_none_falls_back_to_unknown(self):
        raw = _base_raw() | {"task_field": None, "task_field_confidence": 0.5}
        out = _validate_and_normalize(raw, "anything")
        self.assertEqual(out["task_field"], "unknown")

    # ── 置信度夹紧 ──

    def test_task_field_confidence_clamped_to_0_1(self):
        """置信度超出 [0, 1] → 夹紧。"""
        raw_too_high = _base_raw() | {
            "task_field": "backend",
            "task_field_confidence": 1.5,
        }
        out = _validate_and_normalize(raw_too_high, "fix API")
        self.assertEqual(out["task_field_confidence"], 1.0)

        raw_negative = _base_raw() | {
            "task_field": "backend",
            "task_field_confidence": -0.3,
        }
        out = _validate_and_normalize(raw_negative, "fix API")
        self.assertEqual(out["task_field_confidence"], 0.0)

    def test_task_field_confidence_invalid_float_falls_back_to_0_5(self):
        """非 float 的置信度 → 兜底 0.5。"""
        raw = _base_raw() | {
            "task_field": "backend",
            "task_field_confidence": "high",
        }
        out = _validate_and_normalize(raw, "fix API")
        self.assertEqual(out["task_field_confidence"], 0.5)

    def test_task_field_confidence_rounded_to_2_decimals(self):
        raw = _base_raw() | {
            "task_field": "backend",
            "task_field_confidence": 0.8765,
        }
        out = _validate_and_normalize(raw, "fix API")
        self.assertEqual(out["task_field_confidence"], 0.88)

    # ── 形状契约：不影响其它字段 ──

    def test_other_fields_unaffected(self):
        """task_field 新增字段不破坏原有 schema 字段。"""
        raw = _base_raw() | {"task_field": "frontend", "task_field_confidence": 0.9}
        out = _validate_and_normalize(raw, "test")
        # 原有字段全部保留
        self.assertEqual(out["stage"], "implement")
        self.assertEqual(out["pattern"], "feature")
        self.assertEqual(out["pattern_confidence"], 0.9)
        self.assertEqual(out["complexity_score"], 45)
        self.assertEqual(out["complexity_label"], "medium")
        self.assertEqual(out["complexity_confidence"], 0.8)
        self.assertEqual(out["reasoning"], "test")
        # 新增字段
        self.assertEqual(out["task_field"], "frontend")
        self.assertEqual(out["task_field_confidence"], 0.9)


class TestValidTaskFieldsContract(unittest.TestCase):
    """白名单契约校验 —— 一旦白名单被改，测试会立即发现。"""

    def test_white_list_exact_five_values(self):
        """白名单必须严格 5 个值：frontend/backend/ops/product/unknown。"""
        self.assertEqual(
            VALID_TASK_FIELDS,
            frozenset({"frontend", "backend", "ops", "product", "unknown"}),
        )

    def test_white_list_is_frozenset(self):
        """白名单类型是 frozenset（防运行时被改）。"""
        self.assertIsInstance(VALID_TASK_FIELDS, frozenset)


if __name__ == "__main__":
    unittest.main()
