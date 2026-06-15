"""
test_post_tool_handler.py — v1.3 PostToolUse Dispatcher 单测
===============================================================

V1.3 §8 PostToolUse 接入 / dispatcher + 2 worker。

`post_tool_handler` 是 PostToolUse hook 的入口 dispatcher：
  - dispatch(sid, project_root, raw_event) → 按 tool_name 路由
  - TodoWrite → todowrite_analyzer（分析后写 session_state）
  - 其他工具 → runtime_tracker（累积 score 后写 session_state）
  - main() 从 stdin 读 JSON → 解析 → dispatch
  - 受 MODEL_ROUTER_V13_OBSERVE flag 控制
  - 所有异常静默吞掉（不阻塞 hook）

测试目标（TDD）：
  1. dispatch() 正确路由 TodoWrite → todowrite_signal
  2. dispatch() 正确路由 Edit/Write → runtime_score
  3. flag 关闭时 dispatch() no-op
  4. 异常输入不抛错
  5. main() 从 stdin 读 JSON
  6. 所有工具类型都有路由（不丢事件）
"""

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class TestDispatchTodoWrite(unittest.TestCase):
    """dispatch() 将 TodoWrite 事件路由到 TodoWriteAnalyzer。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-001"

    def tearDown(self):
        self.tmp.cleanup()

    def test_todowrite_writes_todowrite_signal(self):
        """TodoWrite 事件应写入 todowrite_signal 到 session_state。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "Implement login page", "status": "pending"},
                    {"content": "Fix header bug", "status": "pending"},
                ]
            },
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists(), "dispatch 后应存在 session_state 文件")

        data = json.loads(state_file.read_text())
        self.assertIn("todowrite_signal", data,
                      "TodoWrite 事件应写入 todowrite_signal 字段")
        self.assertTrue(data["todowrite_signal"]["is_implementation"])

    def test_todowrite_also_writes_runtime_score(self):
        """TodoWrite 事件应同时更新 runtime_score（计分 + 信号双写）。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "Build API", "status": "pending"},
                ]
            },
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data,
                      "TodoWrite 也应累积 runtime_score")
        self.assertGreater(data["runtime_score"]["score"], 0)


class TestDispatchOtherTools(unittest.TestCase):
    """dispatch() 将非 TodoWrite 事件路由到 RuntimeTracker。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-002"

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_writes_runtime_score(self):
        """Edit 事件应写入 runtime_score。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data)
        self.assertGreater(data["runtime_score"]["score"], 0)
        # 非 TodoWrite 不应写 todowrite_signal
        self.assertNotIn("todowrite_signal", data)

    def test_write_writes_runtime_score(self):
        """Write 事件应写入 runtime_score。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/app/test.py"},
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data)

    def test_read_writes_runtime_score(self):
        """Read 事件应写入 runtime_score（低分但仍有记录）。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/docs.md"},
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data)

    def test_bash_writes_runtime_score(self):
        """Bash 事件应写入 runtime_score。"""
        from post_tool_handler import dispatch

        raw_event = {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
        }
        dispatch(self.sid, str(self.project_root), raw_event)

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        self.assertIn("runtime_score", data)

    def test_accumulation_across_multiple_tools(self):
        """多次 dispatch 不同工具应累积 score。"""
        from post_tool_handler import dispatch

        dispatch(self.sid, str(self.project_root), {
            "tool_name": "Read",
            "tool_input": {"file_path": "/app/main.py"},
        })
        dispatch(self.sid, str(self.project_root), {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        # Read=2+py=3=5, Edit=4+py=3=7, total=12
        self.assertGreaterEqual(data["runtime_score"]["score"], 12)


class TestFlagOffNoOp(unittest.TestCase):
    """MODEL_ROUTER_V13_OBSERVE=0 时 dispatch() 为 no-op。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-003"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_off_no_file_created(self):
        """flag 关闭时不应创建 session_state 文件。"""
        from post_tool_handler import dispatch

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_OBSERVE": "0"}):
            dispatch(self.sid, str(self.project_root), {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/app/main.py"},
            })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertFalse(state_file.exists(),
                         "flag 关闭时不应创建 session_state 文件")

    def test_flag_off_todowrite_no_file(self):
        """flag 关闭时 TodoWrite 也不应写文件。"""
        from post_tool_handler import dispatch

        with patch.dict(os.environ, {"MODEL_ROUTER_V13_OBSERVE": "0"}):
            dispatch(self.sid, str(self.project_root), {
                "tool_name": "TodoWrite",
                "tool_input": {"todos": [{"content": "Fix bug", "status": "pending"}]},
            })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertFalse(state_file.exists())


class TestGracefulDegradation(unittest.TestCase):
    """异常输入永不抛错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-004"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_tool_name(self):
        """缺少 tool_name 字段不抛异常。"""
        from post_tool_handler import dispatch

        try:
            dispatch(self.sid, str(self.project_root), {"tool_input": {}})
        except Exception as e:
            self.fail(f"dispatch() 不应因缺失 tool_name 抛异常: {e}")

    def test_empty_event(self):
        """空 event dict 不抛异常。"""
        from post_tool_handler import dispatch

        try:
            dispatch(self.sid, str(self.project_root), {})
        except Exception as e:
            self.fail(f"dispatch() 不应因空 event 抛异常: {e}")

    def test_none_event(self):
        """None event 不抛异常。"""
        from post_tool_handler import dispatch

        try:
            dispatch(self.sid, str(self.project_root), None)
        except Exception as e:
            self.fail(f"dispatch(None) 不应抛异常: {e}")

    def test_todowrite_missing_todos(self):
        """TodoWrite 但缺少 todos 字段不抛异常。"""
        from post_tool_handler import dispatch

        try:
            dispatch(self.sid, str(self.project_root), {
                "tool_name": "TodoWrite",
                "tool_input": {},
            })
        except Exception as e:
            self.fail(f"dispatch() TodoWrite missing todos 不应抛异常: {e}")


class TestMainReadsStdin(unittest.TestCase):
    """main() 从 stdin 读取 JSON 并 dispatch。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-005"
        # 保存原始 stdin
        self.orig_stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self.orig_stdin
        self.tmp.cleanup()

    def test_main_reads_stdin_and_dispatches(self):
        """main() 应读取 stdin JSON 并写入 session_state。"""
        from post_tool_handler import main

        event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
            "session_id": self.sid,
            "cwd": str(self.project_root),
        }
        sys.stdin = StringIO(json.dumps(event))

        main()

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists(),
                        "main() 应读取 stdin 并写入 session_state")

    def test_main_handles_invalid_json(self):
        """stdin 为非法 JSON 时 main() 不抛异常。"""
        from post_tool_handler import main

        sys.stdin = StringIO("not valid json {{{")

        try:
            main()
        except Exception as e:
            self.fail(f"main() 不应因非法 JSON 抛异常: {e}")

    def test_main_uses_cwd_as_project_root(self):
        """main() 应从 event 中提取 cwd 作为 project_root。"""
        from post_tool_handler import main

        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/app/src/lib.py"},
            "session_id": self.sid,
            "cwd": str(self.project_root),
        }
        sys.stdin = StringIO(json.dumps(event))

        main()

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists(),
                        "main() 应使用 cwd 作为 project_root")


class TestRouterTableCoverage(unittest.TestCase):
    """dispatcher 路由表覆盖常见 tool_name。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-006"

    def tearDown(self):
        self.tmp.cleanup()

    def _dispatch(self, tool_name):
        from post_tool_handler import dispatch
        dispatch(self.sid, str(self.project_root), {
            "tool_name": tool_name,
            "tool_input": {"file_path": "/app/main.py"},
        })

    def test_known_tools_all_routed(self):
        """所有已知 tool_name 路由不抛异常。"""
        known_tools = [
            "Read", "Write", "Edit", "Bash", "Grep", "Glob",
            "TodoWrite", "TaskCreate", "TaskUpdate", "WebFetch",
            "WebSearch", "NotebookEdit",
        ]
        for tool in known_tools:
            try:
                self._dispatch(tool)
            except Exception as e:
                self.fail(f"dispatch({tool}) 不应抛异常: {e}")

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(state_file.exists())
        data = json.loads(state_file.read_text())
        # 每个工具都累积了 score
        self.assertGreater(data["runtime_score"]["score"], 0)


class TestSessionStateFields(unittest.TestCase):
    """session_state 文件格式验证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-pth-007"

    def tearDown(self):
        self.tmp.cleanup()

    def test_todowrite_signal_structure(self):
        """todowrite_signal 应有完整的分析字段。"""
        from post_tool_handler import dispatch

        dispatch(self.sid, str(self.project_root), {
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "Implement auth", "status": "pending"},
                    {"content": "Read docs", "status": "completed"},
                ]
            },
        })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())
        signal = data["todowrite_signal"]

        self.assertIn("is_implementation", signal)
        self.assertIn("total", signal)
        self.assertIn("pending", signal)
        self.assertIn("completed", signal)
        self.assertIn("complexity_signal", signal)

    def test_runtime_score_preserved_across_routing(self):
        """runtime_score 和 todowrite_signal 应共存不覆盖。"""
        from post_tool_handler import dispatch

        # 先写 Edit → runtime_score
        dispatch(self.sid, str(self.project_root), {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/app/main.py"},
        })
        # 再写 TodoWrite → todowrite_signal + 累积 runtime_score
        dispatch(self.sid, str(self.project_root), {
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [{"content": "Fix bug", "status": "pending"}],
            },
        })

        state_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(state_file.read_text())

        # 两个字段都存在
        self.assertIn("runtime_score", data)
        self.assertIn("todowrite_signal", data)

        # runtime_score 应累积（Edit + TodoWrite 两次）
        self.assertGreater(data["runtime_score"]["score"], 0)
        self.assertGreaterEqual(len(data["runtime_score"].get("events", [])), 2)


if __name__ == "__main__":
    unittest.main()
