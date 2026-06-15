"""
test_runtime_tracker.py — v1.3 Runtime Tracker 单测
======================================================

V1.3 §8 PostToolUse 接入 / §7 Runtime Complexity Score。

`RuntimeTracker` 是 PostToolUse hook 的 runtime_score 封装：
  - track(tool_event) → 转换事件 + 累积 + 持久化
  - 从 session_state 文件读取当前 score，累积后写回
  - 受 MODEL_ROUTER_V13_OBSERVE flag 控制（关闭时 no-op）
  - 不抛异常阻塞 hook（异常静默 fallthrough）

与 RuntimeScore（纯计算）的关系：
  RuntimeScore 负责计分（纯函数），RuntimeTracker 负责
  I/O（读/写 session_state）+ 事件格式转换。

测试目标（TDD）：
  1. track() 将原始 PostToolUse event 转为 RuntimeScore event
  2. track() 正确累积到 session_state 文件
  3. track() 在 flag 关闭时 no-op（零 I/O）
  4. session_state 文件缺失/损坏 → 从零开始（不抛异常）
  5. track() 返回本次 delta
  6. 事件格式转换：tool_name → tool, file extension → file_type
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestTrackConvertsEvent(unittest.TestCase):
    """track() 将原始 PostToolUse event 转为 RuntimeScore event 格式。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-rt-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_track_converts_tool_name(self):
        """PostToolUse tool_name → RuntimeScore tool 字段。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        raw_event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        }
        delta = tracker.track(self.sid, str(self.project_root), raw_event)
        # Edit=4 + (no file_type / file_lines) = 4
        self.assertGreater(delta, 0)

    def test_track_extracts_file_extension(self):
        """从 tool_input.file_path 提取扩展名。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        raw_event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/app/src/utils.ts"},
        }
        delta = tracker.track(self.sid, str(self.project_root), raw_event)
        # Write=3 + .ts=3 = 6
        self.assertEqual(delta, 6)

    def test_track_missing_file_path(self):
        """无 file_path 时 file_type 为空字符串。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        raw_event = {
            "tool_name": "Read",
            "tool_input": {},
        }
        delta = tracker.track(self.sid, str(self.project_root), raw_event)
        # Read=2 + file_type=""=0 + file_lines=""=0 = 2
        self.assertEqual(delta, 2)


class TestTrackAccumulatesToFile(unittest.TestCase):
    """track() 将累积结果持久化到 session_state 文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-rt-002"

    def tearDown(self):
        self.tmp.cleanup()

    def test_track_writes_runtime_score_to_session_state(self):
        """track() 应将 runtime_score 写入 model_router_state_<sid>.json。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        raw_event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        }
        tracker.track(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists(), "track 后应存在 session_state 文件")

        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data)
        self.assertGreater(data["runtime_score"]["score"], 0)

    def test_track_accumulates_existing_score(self):
        """已有 runtime_score 时应在其基础上累加。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()

        # 第一次 track
        tracker.track(self.sid, str(self.project_root), {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/main.py"},
        })
        # 第二次 track
        tracker.track(self.sid, str(self.project_root), {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        # Read=2+py=3=5, Edit=4+py=3=7, total=12
        self.assertEqual(data["runtime_score"]["score"], 12)

    def test_track_multiple_events_preserves_history(self):
        """多次 track 应保留事件日志。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()

        for _ in range(3):
            tracker.track(self.sid, str(self.project_root), {
                "tool_name": "Grep",
                "tool_input": {},
            })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("events", data["runtime_score"])
        self.assertEqual(len(data["runtime_score"]["events"]), 3)


class TestFlagOffNoOp(unittest.TestCase):
    """MODEL_ROUTER_V13_OBSERVE=0 时 track() 为 no-op。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-rt-003"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_no_file_created(self):
        """flag 关闭时不应写 session_state 文件。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        raw_event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        }
        with patch.dict(os.environ, {"MODEL_ROUTER_V13_OBSERVE": "0"}):
            delta = tracker.track(self.sid, str(self.project_root), raw_event)

        self.assertEqual(delta, 0, "flag 关闭时 delta 应为 0")
        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertFalse(state_file.exists(), "flag 关闭时不应创建 session_state 文件")

    def test_flag_off_returns_zero(self):
        """flag 关闭时 track() 返回 0。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        with patch.dict(os.environ, {"MODEL_ROUTER_V13_OBSERVE": "0"}):
            delta = tracker.track(self.sid, str(self.project_root), {
                "tool_name": "Bash",
                "tool_input": {"command": "make test"},
            })
        self.assertEqual(delta, 0)

    @patch.dict(os.environ, {"MODEL_ROUTER_V13_OBSERVE": "1"})
    def test_flag_on_writes_file(self):
        """flag 开启时正常写文件。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        tracker.track(self.sid, str(self.project_root), {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/readme.md"},
        })
        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists(), "flag on 时应创建 session_state 文件")


class TestGracefulDegradation(unittest.TestCase):
    """异常情况不应抛错阻塞 hook。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-rt-004"

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupted_session_state_does_not_throw(self):
        """已损坏的 session_state 文件应被静默处理（从零开始）。"""
        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        state_file.write_text("{ corrupted json !!!")

        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        try:
            delta = tracker.track(self.sid, str(self.project_root), {
                "tool_name": "Read",
                "tool_input": {"file_path": "/app/main.py"},
            })
        except Exception as e:
            self.fail(f"track() 不应因损坏文件抛异常: {e}")

        # 应从零开始累积（文件被重新写入）
        self.assertGreater(delta, 0)

    def test_missing_claude_dir_creates_it(self):
        """.claude 目录缺失时自动创建。"""
        import shutil
        shutil.rmtree(str(self.claude_dir))

        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        # 不应抛异常
        tracker.track(self.sid, str(self.project_root), {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/main.py"},
        })
        self.assertTrue(self.claude_dir.exists(), ".claude 目录应被自动创建")

    def test_bare_minimum_event_no_tool_input(self):
        """完全不包含 tool_input 的事件也不应抛异常。"""
        from runtime_tracker import RuntimeTracker
        tracker = RuntimeTracker()
        try:
            delta = tracker.track(self.sid, str(self.project_root), {
                "tool_name": "Bash",
            })
        except Exception as e:
            self.fail(f"track() 不应因缺失 tool_input 抛异常: {e}")
        self.assertIsInstance(delta, int)


class TestTodoWriteDetection(unittest.TestCase):
    """TodoWrite 工具应产生强信号（高 delta）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-rt-005"

    def tearDown(self):
        self.tmp.cleanup()

    def test_todowrite_high_delta(self):
        """TodoWrite 工具的 delta 应显著高于普通 Read 工具。"""
        from runtime_tracker import RuntimeTracker
        tracker_read = RuntimeTracker()
        tracker_todo = RuntimeTracker()

        sid_r = "test-sid-read"
        sid_t = "test-sid-todo"

        delta_read = tracker_read.track(sid_r, str(self.project_root), {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/main.py"},
        })
        delta_todo = tracker_todo.track(sid_t, str(self.project_root), {
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"content": "Implement login", "status": "pending"}]},
        })

        self.assertGreater(delta_todo, delta_read,
                           "TodoWrite delta 应高于普通 Read")


if __name__ == "__main__":
    unittest.main()
