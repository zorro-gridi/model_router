"""
test_stage_config_weights.py — stage_config._PLACEHOLDER_WEIGHTS 形状契约
=========================================================================

V1.3 §7 Runtime Complexity Score 设计 / §3.4 权重可自定义。

Stage 1 阶段：`_PLACEHOLDER_WEIGHTS` 作为**硬编码兜底**留在 stage_config.py，
Stage 7 才会抽到 `config/decision_weights.yaml`。本测试锁定其**当前形状**，
避免后续 stage 误改 schema 引爆 decision_engine/runtime_score 调用点。

本文件测的不是数值（数值是经验调参项），而是**结构契约**：
  1. 必含 4 大类权重：tool / file_type / file_lines / runtime_signal
  2. tool 权重表覆盖至少：Read / Edit / Write / Grep / Bash / TodoWrite
  3. file_type 权重表覆盖至少：.py / .json / .md
  4. 权重值必须为 int（Stage 7 才会扩展为可调 float）
  5. 单调性：TodoWrite 权重必须 > Bash 权重（强信号 > 普通信号）
"""

import unittest

from stage_config import _PLACEHOLDER_WEIGHTS


class TestPlaceholderWeightsShape(unittest.TestCase):
    def test_top_level_has_four_categories(self):
        self.assertIn("tool", _PLACEHOLDER_WEIGHTS)
        self.assertIn("file_type", _PLACEHOLDER_WEIGHTS)
        self.assertIn("file_lines", _PLACEHOLDER_WEIGHTS)
        self.assertIn("runtime_signal", _PLACEHOLDER_WEIGHTS)

    def test_tool_covers_core_tools(self):
        tool = _PLACEHOLDER_WEIGHTS["tool"]
        for required in ("Read", "Edit", "Write", "Grep", "Bash", "TodoWrite"):
            self.assertIn(required, tool, f"tool 权重缺 {required}")
            self.assertIsInstance(tool[required], int)

    def test_file_type_covers_common_extensions(self):
        ft = _PLACEHOLDER_WEIGHTS["file_type"]
        for required in (".py", ".json", ".md"):
            self.assertIn(required, ft, f"file_type 权重缺 {required}")
            self.assertIsInstance(ft[required], int)

    def test_file_lines_layered(self):
        lines = _PLACEHOLDER_WEIGHTS["file_lines"]
        # 至少要分层：小/中/大
        for required in ("small", "medium", "large"):
            self.assertIn(required, lines, f"file_lines 缺 {required} 分层")

    def test_todowrite_is_strongest_signal(self):
        """V1.3 §9：TodoWrite 是早期强信号，权重必须大于普通 Bash。"""
        tool = _PLACEHOLDER_WEIGHTS["tool"]
        self.assertGreater(
            tool["TodoWrite"], tool["Bash"],
            "TodoWrite 权重应大于 Bash（强信号 vs 普通信号）",
        )

    def test_all_values_are_int(self):
        """Stage 1 约定权重都是 int，便于 Stage 7 抽 YAML 后用 int 字段。"""
        for cat_name, cat in _PLACEHOLDER_WEIGHTS.items():
            for k, v in cat.items():
                self.assertIsInstance(
                    v, int,
                    f"{cat_name}.{k} 不是 int: {type(v).__name__}",
                )


if __name__ == "__main__":
    unittest.main()
