"""
test_todowrite_analyzer.py — v1.3 TodoWrite Analyzer 单测
=============================================================

V1.3 §8.3 TodoWrite Analyzer / §6 首次 TodoWrite 强信号。

`TodoWriteAnalyzer` 分析 TodoWrite 工具的输出内容，检测：
  - 是否为"真实实施"信号（新增非 trivial items）
  - 任务模式的复杂度等级
  - 是否触发 todowrite_detected 状态转移信号

与 RuntimeScore 的关系：
  TodoWriteAnalyzer 只负责**分析**（纯文本解析），
  不负责计分。分析结果供 RuntimeTracker 使用。

测试目标（TDD）：
  1. analyze() 检测到实质性 todos → is_implementation=True
  2. analyze() 无 todos 或仅 trivial → is_implementation=False
  3. analyze() 返回 todo 数量和状态分布
  4. 损坏/空输入不抛异常
  5. implementation 检测应识别关键词（implement, fix, refactor 等）
"""

import unittest


class TestAnalyzeReturnsSignal(unittest.TestCase):
    """analyze() 返回强信号检测结果。"""

    def _analyzer(self):
        from todowrite_analyzer import TodoWriteAnalyzer
        return TodoWriteAnalyzer()

    def test_implementation_todos_detected(self):
        """包含 implement/fix/build 等关键词的 todos 应被检测为实现信号。"""
        az = self._analyzer()
        result = az.analyze([
            {"content": "Implement user authentication", "status": "pending"},
            {"content": "Fix login bug", "status": "pending"},
            {"content": "Build API endpoint", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"],
                        "包含 implement/fix/build 应被识别为实现信号")

    def test_trivial_todos_not_implementation(self):
        """仅文档/阅读/描述类 todos 不应触发实现信号。"""
        az = self._analyzer()
        result = az.analyze([
            {"content": "Read the codebase", "status": "pending"},
            {"content": "Understand the architecture", "status": "pending"},
            {"content": "Document the API", "status": "pending"},
        ])
        self.assertFalse(result["is_implementation"],
                         "纯阅读/文档任务不应触发实现信号")

    def test_empty_todos_not_implementation(self):
        """空 todo 列表不应触发实现信号。"""
        az = self._analyzer()
        result = az.analyze([])
        self.assertFalse(result["is_implementation"])
        self.assertEqual(result["total"], 0)

    def test_all_completed_not_implementation(self):
        """所有 todos 已完成时不触发实现信号（已无事可做）。"""
        az = self._analyzer()
        result = az.analyze([
            {"content": "Implement login", "status": "completed"},
            {"content": "Fix navbar", "status": "completed"},
        ])
        self.assertFalse(result["is_implementation"],
                         "全部已完成不应触发新信号")


class TestTodoCounts(unittest.TestCase):
    """analyze() 返回 todo 统计数据。"""

    def _analyzer(self):
        from todowrite_analyzer import TodoWriteAnalyzer
        return TodoWriteAnalyzer()

    def test_counts_total_pending_completed(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Task A", "status": "pending"},
            {"content": "Task B", "status": "in_progress"},
            {"content": "Task C", "status": "completed"},
            {"content": "Task D", "status": "pending"},
        ])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["pending"], 3)  # pending + in_progress
        self.assertEqual(result["completed"], 1)

    def test_counts_no_status_field_defaults_pending(self):
        """无 status 字段的 todo 默认视为 pending。"""
        az = self._analyzer()
        result = az.analyze([
            {"content": "Do something"},
        ])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pending"], 1)


class TestGracefulDegradation(unittest.TestCase):
    """异常输入不抛错。"""

    def _analyzer(self):
        from todowrite_analyzer import TodoWriteAnalyzer
        return TodoWriteAnalyzer()

    def test_none_input(self):
        az = self._analyzer()
        try:
            result = az.analyze(None)
        except Exception as e:
            self.fail(f"analyze(None) 不应抛异常: {e}")
        self.assertFalse(result["is_implementation"])
        self.assertEqual(result["total"], 0)

    def test_malformed_todos(self):
        """非标准格式的 todos 不应抛异常。"""
        az = self._analyzer()
        try:
            result = az.analyze([{"not_content": "missing content field"}])
        except Exception as e:
            self.fail(f"analyze() 不应因格式异常抛错: {e}")
        self.assertEqual(result["total"], 0)

    def test_string_instead_of_list(self):
        """如果传入字符串而非列表，应安全降级。"""
        az = self._analyzer()
        try:
            result = az.analyze("not a list")
        except Exception as e:
            self.fail(f"analyze(str) 不应抛异常: {e}")
        self.assertFalse(result["is_implementation"])


class TestImplementationKeywords(unittest.TestCase):
    """实现信号关键词覆盖。"""

    def _analyzer(self):
        from todowrite_analyzer import TodoWriteAnalyzer
        return TodoWriteAnalyzer()

    def test_keyword_implement(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Implement the cache layer", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])

    def test_keyword_fix(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Fix the memory leak", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])

    def test_keyword_refactor(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Refactor the auth module", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])

    def test_keyword_build(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Build the dashboard UI", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])

    def test_keyword_add_create_write(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Add error handling", "status": "pending"},
            {"content": "Create database migration", "status": "pending"},
            {"content": "Write unit tests", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])

    def test_keyword_debug(self):
        az = self._analyzer()
        result = az.analyze([
            {"content": "Debug the race condition", "status": "pending"},
        ])
        self.assertTrue(result["is_implementation"])


class TestComplexitySignal(unittest.TestCase):
    """analyze() 返回复杂度相关信号。"""

    def _analyzer(self):
        from todowrite_analyzer import TodoWriteAnalyzer
        return TodoWriteAnalyzer()

    def test_many_pending_todos_higher_complexity(self):
        """pending + in_progress 数量多 → 复杂度信号更高。"""
        az = self._analyzer()
        todos = [{"content": f"Task {i}", "status": "pending"} for i in range(10)]
        result = az.analyze(todos)
        self.assertGreaterEqual(result["complexity_signal"], 0.5,
                                "大量 pending todos 应有较高的复杂度信号")

    def test_few_pending_todos_lower_complexity(self):
        """少量 pending todos → 复杂度信号低。"""
        az = self._analyzer()
        todos = [{"content": "One task", "status": "pending"}]
        result = az.analyze(todos)
        self.assertLessEqual(result["complexity_signal"], 0.5,
                             "少量 pending todos 应有较低的复杂度信号")

    def test_no_pending_zero_complexity(self):
        """无 pending todos → 复杂度信号为 0。"""
        az = self._analyzer()
        todos = [{"content": "Done", "status": "completed"}]
        result = az.analyze(todos)
        self.assertEqual(result["complexity_signal"], 0.0)


if __name__ == "__main__":
    unittest.main()
