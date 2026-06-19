"""
test_runtime_score.py — v1.3 Runtime Score 累积模块单测
=========================================================

V1.3 §7 Runtime Complexity Score / §3.4 权重可自定义。

`RuntimeScore` 是纯内存计分器：
  - accumulate(event) → 返回新总分
  - 不读写文件，不发起网络
  - 事件被追加到内部日志（可导出序列化）

6 类计分信号（V1.3 §7）：
  1. tool             — 工具名权重（_PLACEHOLDER_WEIGHTS["tool"]）
  2. file_type        — 编辑文件扩展名权重（_PLACEHOLDER_WEIGHTS["file_type"]）
  3. file_lines       — 编辑行数分层（small/medium/large）
  4. runtime_signal   — 运行时特殊信号（bash_nonzero_exit 等）
  5. multi_edit_bonus — 同一文件连续编辑附加分
  6. search_large     — grep/glob 返回大量结果

测试目标（TDD）：
  1. accumulate 单事件 → score 正确
  2. accumulate 多次 → score 累加
  3. 未知 tool / file_type → delta=0（不抛异常）
  4. 不含事件的 RuntimeScore → score=0
  5. 事件序列化 to_dict / 反序列化 from_dict
  6. 纯函数，零 I/O
"""

import unittest
from unittest.mock import patch

from runtime_score import RuntimeScore


class TestSingleAccumulate(unittest.TestCase):
    """单事件计分。"""

    def setUp(self):
        self.rs = RuntimeScore()

    def test_read_tool_with_py_file(self):
        delta = self.rs.accumulate({
            "tool": "Read",
            "file_type": ".py",
            "file_lines": "small",
        })
        # tool.Read=2 + file_type.py=3 + file_lines.small=1 = 6
        self.assertEqual(delta, 6)
        self.assertEqual(self.rs.score, 6)

    def test_edit_tool_with_ts_file_medium(self):
        delta = self.rs.accumulate({
            "tool": "Edit",
            "file_type": ".ts",
            "file_lines": "medium",
        })
        # tool.Edit=4 + file_type.ts=3 + file_lines.medium=2 = 9
        self.assertEqual(delta, 9)
        self.assertEqual(self.rs.score, 9)

    def test_write_tool_with_large_file(self):
        delta = self.rs.accumulate({
            "tool": "Write",
            "file_type": ".py",
            "file_lines": "large",
        })
        # tool.Write=3 + file_type.py=3 + file_lines.large=3 = 9
        self.assertEqual(delta, 9)

    def test_todowrite_is_strong_signal(self):
        """TodoWrite 权重最高（8），应反映在分差上。"""
        delta = self.rs.accumulate({
            "tool": "TodoWrite",
            "file_type": ".md",
            "file_lines": "small",
        })
        # tool.TodoWrite=8 + file_type.md=1 + file_lines.small=1 = 10
        self.assertEqual(delta, 10)
        self.assertGreater(delta, 5)  # 显著高于普通工具


class TestAccumulateMultiple(unittest.TestCase):
    """累加多次事件，score 持续增长。"""

    def test_three_edits_score_accumulates(self):
        rs = RuntimeScore()
        total = 0
        for _ in range(3):
            total += rs.accumulate({
                "tool": "Edit",
                "file_type": ".py",
                "file_lines": "small",
            })
        # Edit=4 + py=3 + small=1 = 8 × 3 = 24
        self.assertEqual(rs.score, 24)
        self.assertEqual(rs.score, total)

    def test_mixed_tools(self):
        rs = RuntimeScore()
        rs.accumulate({"tool": "Read", "file_type": ".md", "file_lines": "small"})
        rs.accumulate({"tool": "Edit", "file_type": ".py", "file_lines": "medium"})
        rs.accumulate({"tool": "Bash", "file_type": "", "file_lines": "small"})
        # Read=2+md=1+small=1=4
        # Edit=4+py=3+medium=2=9
        # Bash=2+""=0+small=1=3
        # total = 4+9+3 = 16
        self.assertEqual(rs.score, 16)


class TestEventLog(unittest.TestCase):
    """事件日志记录。"""

    def test_events_list_starts_empty(self):
        rs = RuntimeScore()
        self.assertEqual(len(rs.events), 0)

    def test_accumulate_appends_to_events(self):
        rs = RuntimeScore()
        rs.accumulate({"tool": "Read", "file_type": ".py", "file_lines": "small"})
        self.assertEqual(len(rs.events), 1)
        ev = rs.events[0]
        self.assertIn("delta", ev)
        self.assertEqual(ev["tool"], "Read")

    def test_events_are_a_copy(self):
        """events 属性返回副本，外部不能修改内部状态。"""
        rs = RuntimeScore()
        rs.accumulate({"tool": "Read", "file_type": ".py", "file_lines": "small"})
        evs = rs.events
        evs.append({"fake": True})
        self.assertEqual(len(rs.events), 1)  # 内部不变


class TestSerialization(unittest.TestCase):
    """to_dict / from_dict 双向无损。"""

    def test_to_dict_structure(self):
        rs = RuntimeScore()
        rs.accumulate({"tool": "Edit", "file_type": ".py", "file_lines": "medium"})
        d = rs.to_dict()
        self.assertIn("score", d)
        self.assertIn("events", d)
        self.assertEqual(d["score"], rs.score)
        self.assertEqual(len(d["events"]), 1)

    def test_from_dict_restores_score_and_events(self):
        original = RuntimeScore()
        original.accumulate({"tool": "Edit", "file_type": ".ts", "file_lines": "large"})
        original.accumulate({"tool": "Grep", "file_type": "", "file_lines": "small"})
        d = original.to_dict()

        restored = RuntimeScore.from_dict(d)
        self.assertEqual(restored.score, original.score)
        self.assertEqual(len(restored.events), len(original.events))
        self.assertEqual(
            restored.events[0]["delta"], original.events[0]["delta"]
        )

    def test_from_dict_empty_state(self):
        rs = RuntimeScore.from_dict({"score": 0, "events": []})
        self.assertEqual(rs.score, 0)
        self.assertEqual(len(rs.events), 0)


class TestEdgeCases(unittest.TestCase):
    """边界条件。"""

    def test_empty_event_dict(self):
        rs = RuntimeScore()
        delta = rs.accumulate({})
        self.assertEqual(delta, 0)
        self.assertEqual(rs.score, 0)

    def test_unknown_tool_defaults_to_zero(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "NonExistentTool",
            "file_type": ".py",
            "file_lines": "small",
        })
        # tool=0 + py=3 + small=1 = 4
        self.assertEqual(delta, 4)

    def test_unknown_file_type_defaults_to_zero(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "Edit",
            "file_type": ".exotic",
            "file_lines": "small",
        })
        # Edit=4 + exotic=0 + small=1 = 5
        self.assertEqual(delta, 5)

    def test_unknown_file_lines_defaults_to_zero(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "Edit",
            "file_type": ".py",
            "file_lines": "enormous",
        })
        # Edit=4 + py=3 + enormous=0 = 7
        self.assertEqual(delta, 7)

    def test_initial_score_is_zero(self):
        rs = RuntimeScore()
        self.assertEqual(rs.score, 0)

    def test_score_never_negative(self):
        """即便传入异常事件，score 也不应为负。"""
        rs = RuntimeScore()
        for _ in range(3):
            rs.accumulate({})  # delta=0
        self.assertGreaterEqual(rs.score, 0)


class TestRuntimeSignal(unittest.TestCase):
    """runtime_signal 特殊事件。"""

    def test_bash_nonzero_exit_adds_weight(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "Bash",
            "runtime_signal": "bash_nonzero_exit",
        })
        # Bash=2 + signal=4 = 6
        self.assertEqual(delta, 6)

    def test_test_failure_signal(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "Bash",
            "runtime_signal": "test_failure",
        })
        # Bash=2 + test_failure=5 = 7
        self.assertEqual(delta, 7)

    def test_no_runtime_signal(self):
        rs = RuntimeScore()
        delta = rs.accumulate({
            "tool": "Bash",
        })
        # Bash=2 + 0 = 2
        self.assertEqual(delta, 2)


class TestPureFunction(unittest.TestCase):
    """RuntimeScore 应为纯内存操作。"""

    def test_accumulate_does_not_open_files(self):
        rs = RuntimeScore()
        with patch("builtins.open") as mock_open:
            rs.accumulate({"tool": "Read", "file_type": ".py", "file_lines": "small"})
        mock_open.assert_not_called()


class TestWeightInjection(unittest.TestCase):
    """支持自定义权重注入（便于测试 + Stage 7 YAML 加载）。"""

    def test_custom_weights_override_defaults(self):
        custom = {
            "tool": {"Read": 99, "CustomTool": 50},
            "file_type": {".py": 10},
            "file_lines": {"small": 5},
            "runtime_signal": {},
        }
        rs = RuntimeScore(weights=custom)
        delta = rs.accumulate({
            "tool": "Read",
            "file_type": ".py",
            "file_lines": "small",
        })
        # tool.Read=99 + file_type.py=10 + lines.small=5 = 114
        self.assertEqual(delta, 114)

    def test_partial_custom_weights_fallback_to_default(self):
        """自定义权重只覆盖传入的 key，缺失部分用默认值。"""
        custom = {"tool": {"Read": 50}}
        rs = RuntimeScore(weights=custom)
        delta = rs.accumulate({
            "tool": "Read",
            "file_type": ".py",
            "file_lines": "small",
        })
        # tool.Read=50 + file_type.py=3(default) + lines.small=1(default) = 54
        self.assertEqual(delta, 54)


if __name__ == "__main__":
    unittest.main()
