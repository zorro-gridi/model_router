"""
test_yaml_weights.py — config/decision_weights.yaml 加载器契约
=============================================================

V1.3 §7 决策权重配置化。

`stage_config.load_yaml_weights()` 行为契约：
  1. YAML 文件存在且合法 → 加载并返回 YAML 数据
  2. YAML 文件不存在 → 降级为 _PLACEHOLDER_WEIGHTS
  3. YAML 文件损坏 / schema 不匹配 → 降级为 _PLACEHOLDER_WEIGHTS
  4. `get_weights()` 模块级缓存，重复调用应返回同一对象
  5. YAML 顶层必须有 4 类：tool / file_type / file_lines / runtime_signal
"""

import os
import tempfile
import unittest
from pathlib import Path


class TestYamlWeightsLoading(unittest.TestCase):
    """load_yaml_weights() 加载行为契约。"""

    def setUp(self):
        # 重置模块级缓存（避免 test 顺序污染）
        import stage_config
        stage_config._YAML_WEIGHTS = None

    def test_returns_placeholder_when_yaml_missing(self):
        """YAML 不存在 → 降级为 _PLACEHOLDER_WEIGHTS。"""
        import stage_config
        from stage_config import load_yaml_weights, _WEIGHTS_YAML_PATH

        # 临时备份并删除 YAML（如果存在）
        backup = None
        if _WEIGHTS_YAML_PATH.exists():
            backup = _WEIGHTS_YAML_PATH.read_bytes()
            _WEIGHTS_YAML_PATH.unlink()
        try:
            weights = load_yaml_weights()
            self.assertEqual(weights, stage_config._PLACEHOLDER_WEIGHTS)
        finally:
            if backup is not None:
                _WEIGHTS_YAML_PATH.write_bytes(backup)

    def test_returns_placeholder_when_yaml_corrupt(self):
        """YAML 损坏 → 降级为 _PLACEHOLDER_WEIGHTS。"""
        import stage_config
        from stage_config import load_yaml_weights, _WEIGHTS_YAML_PATH

        # 临时写入非法 YAML
        backup = None
        if _WEIGHTS_YAML_PATH.exists():
            backup = _WEIGHTS_YAML_PATH.read_bytes()
        _WEIGHTS_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WEIGHTS_YAML_PATH.write_text(": : not valid yaml [\n", encoding="utf-8")
        try:
            weights = load_yaml_weights()
            self.assertEqual(weights, stage_config._PLACEHOLDER_WEIGHTS)
        finally:
            if backup is not None:
                _WEIGHTS_YAML_PATH.write_bytes(backup)
            elif _WEIGHTS_YAML_PATH.exists():
                _WEIGHTS_YAML_PATH.unlink()

    def test_loaded_yaml_has_four_categories(self):
        """合法 YAML 加载后必须含 4 大类。"""
        import stage_config
        from stage_config import load_yaml_weights, _WEIGHTS_YAML_PATH

        backup = None
        if _WEIGHTS_YAML_PATH.exists():
            backup = _WEIGHTS_YAML_PATH.read_bytes()
        _WEIGHTS_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WEIGHTS_YAML_PATH.write_text("""
tool:
  Edit: 4
  Bash: 2
file_type:
  .py: 3
file_lines:
  small: 1
runtime_signal:
  test_failure: 5
""", encoding="utf-8")
        try:
            weights = load_yaml_weights()
            self.assertIn("tool", weights)
            self.assertIn("file_type", weights)
            self.assertIn("file_lines", weights)
            self.assertIn("runtime_signal", weights)
            self.assertEqual(weights["tool"]["Edit"], 4)
            self.assertEqual(weights["file_type"][".py"], 3)
        finally:
            if backup is not None:
                _WEIGHTS_YAML_PATH.write_bytes(backup)
            elif _WEIGHTS_YAML_PATH.exists():
                _WEIGHTS_YAML_PATH.unlink()


class TestGetWeightsCaching(unittest.TestCase):
    """get_weights() 模块级缓存契约。"""

    def setUp(self):
        import stage_config
        stage_config._YAML_WEIGHTS = None

    def test_get_weights_returns_cached_object(self):
        """get_weights() 多次调用应返回同一对象（缓存生效）。"""
        from stage_config import get_weights

        w1 = get_weights()
        w2 = get_weights()
        self.assertIs(w1, w2, "get_weights() 应返回缓存对象")

    def test_get_weights_shape_matches_placeholder(self):
        """get_weights() 返回的 shape 必须与 _PLACEHOLDER_WEIGHTS 一致。"""
        from stage_config import get_weights, _PLACEHOLDER_WEIGHTS

        weights = get_weights()
        for key in _PLACEHOLDER_WEIGHTS:
            self.assertIn(key, weights, f"get_weights() 缺 {key} 类")
        # 至少 tool 类的核心工具键在
        for tool_name in ("Read", "Edit", "Write", "TodoWrite"):
            self.assertIn(tool_name, weights["tool"],
                          f"tool 权重缺 {tool_name}")


if __name__ == "__main__":
    unittest.main()
