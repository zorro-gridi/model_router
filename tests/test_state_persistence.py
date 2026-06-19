"""
test_state_persistence.py — v1.3 SessionStateStore 单测
=========================================================

V1.3 §5 适配层：model_router_state_<sid>.json 单写 + 兼容读。

SessionStateStore 职责：
  - write(): 单写 — 仅新 model_router_state_<sid>.json（Stage 7.1）
  - read_new(): 读新格式
  - read_legacy(): 从旧 9 文件聚合读（仅作为 read fallback）
  - migrate(): 旧→新 一次性迁移（仅冷启动一次性）


旧 9 文件（v1.2）：stage_, model_, pattern_, complexity_, batch_,
  fallback_, reqcnt_, workflow_step_, op_（已废弃）

测试目标（TDD）：
  1. write() 仅创建新格式文件（Stage 7.1 单写，不再双写）
  2. 新格式 schema 正确（version, decision, state, stage 等）
  3. read_new() 正确反序列化
  4. read_legacy() 从旧文件聚合（仅兼容读，不在写侧出现）
  5. migrate() 一次性迁移
  6. 旧 9 文件在全新 session 中不生成
  7. 并发写入不损坏文件
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# 把 model_router/ 加到 sys.path 以便 import state_persistence
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))



class TestWriteCreatesFiles(unittest.TestCase):
    """write() 单写：仅新格式文件（Stage 7.1 清理旧写侧）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-001"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _sample_decision(self):
        return {
            "session_id": self.sid,
            "prompt_id": "p-1",
            "task_pattern": "feature",
            "task_complexity": "medium",
            "prompt_confidence": 0.85,
            "runtime_score": 0,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": True,
            "decision_source": "prompt",
            "last_update": int(time.time()),
        }

    def test_write_creates_new_format_file(self):
        """write() 应创建 model_router_state_<sid>.json。"""
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision())
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(new_file.exists(), f"新格式文件应存在: {new_file}")

    def test_write_does_not_create_legacy_files(self):
        """write() 不应再生成旧 9 文件（Stage 7.1 单写，清理旧写侧）。"""
        store = self._store()
        store.write(
            self.sid, str(self.project_root),
            decision=self._sample_decision(),
            stage="implement",
            pattern={"prediction": "feature", "confidence": 0.8, "ts": "2026-06-15T00:00:00"},
            complexity={"score": 45, "label": "medium", "confidence": 0.85, "source": "llm", "ts": "2026-06-15T00:00:00"},
        )
        # 旧 9 文件不应再生成（Stage 7.1 之后全新 session 只生新格式）
        stage_file = self.claude_dir / f"stage_{self.sid}"
        pattern_file = self.claude_dir / f"pattern_{self.sid}"
        self.assertFalse(stage_file.exists(), f"Stage 7.1: 旧 stage 文件不应再生成: {stage_file}")
        self.assertFalse(pattern_file.exists(), f"Stage 7.1: 旧 pattern 文件不应再生成: {pattern_file}")
        # 新格式文件应正常生成
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(new_file.exists(), f"新格式文件应生成: {new_file}")


class TestNewFormatSchema(unittest.TestCase):
    """model_router_state_<sid>.json 的 schema 验证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-002"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _sample_decision(self):
        return {
            "session_id": self.sid,
            "prompt_id": "p-2",
            "task_pattern": "bugfix",
            "task_complexity": "complex",
            "prompt_confidence": 0.92,
            "runtime_score": 120,
            "todo_score": 8,
            "final_model": "deepseek-v4-pro",
            "locked": True,
            "decision_source": "runtime",
            "last_update": int(time.time()),
        }

    def test_new_format_has_version_field(self):
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision())
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(new_file.read_text())
        self.assertEqual(data.get("version"), "1.3")

    def test_new_format_has_decision_field(self):
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision())
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(new_file.read_text())
        self.assertIn("decision", data)
        self.assertEqual(data["decision"]["task_pattern"], "bugfix")

    def test_new_format_has_session_id(self):
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision())
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(new_file.read_text())
        self.assertEqual(data.get("session_id"), self.sid)

    def test_new_format_includes_optional_stage(self):
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision(), stage="implement")
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(new_file.read_text())
        self.assertEqual(data.get("stage"), "implement")

    def test_new_format_includes_optional_pattern(self):
        store = self._store()
        pattern = {"prediction": "bugfix", "confidence": 0.9, "ts": "2026-06-15T00:00:00"}
        store.write(self.sid, str(self.project_root), decision=self._sample_decision(), pattern=pattern)
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        data = json.loads(new_file.read_text())
        self.assertEqual(data.get("pattern"), pattern)


class TestReadNew(unittest.TestCase):
    """read_new() 读新格式。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-003"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _sample_decision(self):
        return {
            "session_id": self.sid,
            "prompt_id": "p-3",
            "task_pattern": "refactor",
            "task_complexity": "medium",
            "prompt_confidence": 0.78,
            "runtime_score": 0,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": True,
            "decision_source": "prompt",
            "last_update": int(time.time()),
        }

    def test_read_new_returns_written_data(self):
        store = self._store()
        store.write(self.sid, str(self.project_root), decision=self._sample_decision(), stage="design")
        data = store.read_new(self.sid, str(self.project_root))
        self.assertIsNotNone(data)
        self.assertEqual(data["session_id"], self.sid)
        self.assertEqual(data["stage"], "design")
        self.assertEqual(data["decision"]["task_complexity"], "medium")

    def test_read_new_returns_none_when_file_missing(self):
        store = self._store()
        data = store.read_new("nonexistent-sid", str(self.project_root))
        self.assertIsNone(data)

    def test_read_new_handles_corrupted_json(self):
        """损坏的 JSON 文件应返回 None（不抛异常）。"""
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        new_file.write_text("{ not valid json ")
        store = self._store()
        data = store.read_new(self.sid, str(self.project_root))
        self.assertIsNone(data)


class TestReadLegacy(unittest.TestCase):
    """read_legacy() 从旧 9 文件聚合读。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-004"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def test_read_legacy_aggregates_stage_and_pattern(self):
        # 手动写旧文件
        (self.claude_dir / f"stage_{self.sid}").write_text("implement\n")
        pattern = {"prediction": "feature", "confidence": 0.8, "ts": "2026-06-15T00:00:00"}
        (self.claude_dir / f"pattern_{self.sid}").write_text(json.dumps(pattern))

        store = self._store()
        data = store.read_legacy(self.sid, str(self.project_root))
        self.assertIsNotNone(data)
        self.assertEqual(data.get("stage"), "implement")
        self.assertEqual(data.get("pattern"), pattern)

    def test_read_legacy_aggregates_model_override(self):
        (self.claude_dir / f"model_{self.sid}").write_text("deepseek-v4-pro\n")

        store = self._store()
        data = store.read_legacy(self.sid, str(self.project_root))
        self.assertEqual(data.get("model_override"), "deepseek-v4-pro")

    def test_read_legacy_aggregates_complexity(self):
        c = {"score": 60, "label": "complex", "confidence": 0.9, "source": "llm", "ts": "2026-06-15T00:00:00"}
        (self.claude_dir / f"complexity_{self.sid}").write_text(json.dumps(c))

        store = self._store()
        data = store.read_legacy(self.sid, str(self.project_root))
        self.assertEqual(data.get("complexity"), c)

    def test_read_legacy_returns_none_when_no_files(self):
        store = self._store()
        data = store.read_legacy(self.sid, str(self.project_root))
        self.assertIsNone(data)

    def test_read_legacy_partial_files(self):
        """只有一个旧文件存在时仍可读。"""
        (self.claude_dir / f"stage_{self.sid}").write_text("default\n")
        store = self._store()
        data = store.read_legacy(self.sid, str(self.project_root))
        self.assertIsNotNone(data)
        self.assertEqual(data.get("stage"), "default")
        self.assertIsNone(data.get("pattern"))


class TestMigrate(unittest.TestCase):
    """migrate() 旧→新 一次性迁移。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-005"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def test_migrate_creates_new_file_from_legacy(self):
        # 手动写旧文件
        (self.claude_dir / f"stage_{self.sid}").write_text("implement\n")
        pattern = {"prediction": "feature", "confidence": 0.8, "ts": "2026-06-15T00:00:00"}
        (self.claude_dir / f"pattern_{self.sid}").write_text(json.dumps(pattern))
        c = {"score": 45, "label": "medium", "confidence": 0.85, "source": "llm", "ts": "2026-06-15T00:00:00"}
        (self.claude_dir / f"complexity_{self.sid}").write_text(json.dumps(c))

        store = self._store()
        result = store.migrate(self.sid, str(self.project_root))
        self.assertTrue(result, "迁移应成功")

        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(new_file.exists(), "迁移后新文件应存在")
        data = json.loads(new_file.read_text())
        self.assertEqual(data["version"], "1.3")
        self.assertEqual(data["stage"], "implement")
        self.assertEqual(data["pattern"], pattern)
        self.assertEqual(data["complexity"], c)

    def test_migrate_returns_false_when_no_legacy_files(self):
        store = self._store()
        result = store.migrate(self.sid, str(self.project_root))
        self.assertFalse(result, "无旧文件时迁移应返回 False")



class TestAtomicWrite(unittest.TestCase):
    """原子写入：.tmp + os.replace()。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-007"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _sample_decision(self):
        return {
            "session_id": self.sid,
            "prompt_id": "p-7",
            "task_pattern": "test",
            "task_complexity": "simple",
            "prompt_confidence": 0.9,
            "runtime_score": 0,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": True,
            "decision_source": "prompt",
            "last_update": int(time.time()),
        }

    def test_write_uses_tmp_and_replace(self):
        """写入应通过临时文件 + 原子替换完成。"""
        store = self._store()
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        tmp_file = self.claude_dir / f"model_router_state_{self.sid}.json.tmp"

        # 写之前都不应存在
        self.assertFalse(new_file.exists())
        self.assertFalse(tmp_file.exists())

        store.write(self.sid, str(self.project_root), decision=self._sample_decision())

        # 写之后 tmp 已清理，只有最终文件
        self.assertTrue(new_file.exists())
        self.assertFalse(tmp_file.exists(), "原子写入后 .tmp 应已清理")


class TestThreadSafety(unittest.TestCase):
    """并发写入同一 sid 不应损坏文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "test-sid-008"

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_writes_do_not_corrupt(self):
        """10 个线程同时写，最终文件应是合法 JSON。"""
        errors = []

        def writer(i: int):
            try:
                from state_persistence import SessionStateStore
                store = SessionStateStore()
                decision = {
                    "session_id": self.sid,
                    "prompt_id": f"p-writer-{i}",
                    "task_pattern": "feature",
                    "task_complexity": "medium",
                    "prompt_confidence": 0.8,
                    "runtime_score": i,
                    "todo_score": 0,
                    "final_model": "MiniMax-M3",
                    "locked": True,
                    "decision_source": "prompt",
                    "last_update": int(time.time()),
                }
                store.write(self.sid, str(self.project_root), decision=decision, stage=f"writer-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 验证文件是合法 JSON
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        self.assertTrue(new_file.exists())
        try:
            data = json.loads(new_file.read_text())
            self.assertIn("decision", data)
        except json.JSONDecodeError:
            self.fail("并发写入后文件应为合法 JSON")

        self.assertEqual(len(errors), 0, f"并发写入不应抛异常: {errors}")


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────
# BUG 3 回归测试（2026-06-18）：state.fallback 在 sticky 清除后必须清空
# ─────────────────────────────────────────────────────────────────────
#
# 根因：SessionStateStore.write() 旧实现对 optional_fields 用
#   `if key in kwargs and kwargs[key] is not None` 判定
# 导致显式传入 `fallback=None`（sticky 已被 health_checker 清除）
# 被误跳过，转而从 existing 继承 stale 值 → statusline 永久误显 fallback。
#
# 修复：判定改为 `if key in kwargs`，None 也是有效值（明确清除）。
class FallbackFieldClearTest(unittest.TestCase):
    """state.fallback 字段在显式传 None 时必须被清除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "fb-clear-867263e4"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _read_state(self):
        new_file = self.claude_dir / f"model_router_state_{self.sid}.json"
        return json.loads(new_file.read_text(encoding="utf-8"))

    def test_explicit_none_clears_stale_fallback(self):
        """【BUG 3 复现】旧 sticky=minimax 已清除 → 显式传 fallback=None
        → state.fallback 必须为 None（不能继承旧值 "minimax"）。"""
        store = self._store()
        # 第一轮：fallback=minimax（模拟 proxy 写 sticky 时同步写 state）
        store.write(
            self.sid, str(self.project_root),
            decision={},
            route_model="deepseek-v4-pro",
            fallback="minimax",
        )
        state = self._read_state()
        self.assertEqual(state["fallback"], "minimax",
                         "首轮：fallback='minimax' 应被正确写入")

        # 第二轮：sticky 已被 health_checker 清除 → fallback=None
        # 修复前：fallback 仍是 "minimax"（继承自 existing）
        # 修复后：fallback 是 None（显式传 None 清除）
        store.write(
            self.sid, str(self.project_root),
            decision={},
            route_model="MiniMax-M3",  # 已恢复
            fallback=None,
        )
        state = self._read_state()
        self.assertIsNone(
            state["fallback"],
            "【BUG 3 修复】sticky 清除后显式传 None → state.fallback 必须为 None，"
            "不能继承 stale 'minimax'（否则 statusline 永久误显 fallback 标签）",
        )
        # 其他字段仍需正确保留
        self.assertEqual(state["route_model"], "MiniMax-M3")

    def test_omitted_key_still_inherits_from_existing(self):
        """未传 fallback 时仍应从 existing 继承（保持向后兼容）。"""
        store = self._store()
        # 第一轮：写入 fallback
        store.write(
            self.sid, str(self.project_root),
            decision={},
            fallback="deepseek",
        )
        # 第二轮：完全不传 fallback 字段
        store.write(self.sid, str(self.project_root), decision={})
        state = self._read_state()
        self.assertEqual(
            state["fallback"], "deepseek",
            "未显式传 fallback → 应从 existing 继承（避免漏传关键字时误清）",
        )


class UpdateFieldsEfficiencyTest(unittest.TestCase):
    """update_fields() 的 no-op 跳过写盘与 decision 保留语义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tmp.name)
        self.claude_dir = self.project_root / ".claude"
        self.claude_dir.mkdir()
        self.sid = "upd-fields-3a0c6299"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from state_persistence import SessionStateStore
        return SessionStateStore()

    def _state_path(self):
        return self.claude_dir / f"model_router_state_{self.sid}.json"

    def test_update_fields_preserves_existing_decision(self):
        store = self._store()
        decision = {
            "session_id": self.sid,
            "prompt_id": "p-1",
            "task_pattern": "feature",
            "task_complexity": "medium",
            "prompt_confidence": 0.9,
            "runtime_score": 0,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": False,
            "decision_source": "prompt",
            "last_update": 1700000000,
        }
        store.write(self.sid, str(self.project_root), decision=decision)

        wrote = store.update_fields(
            self.sid,
            str(self.project_root),
            updates={"route_model": "MiniMax-M3", "task_complexity": "medium"},
        )
        self.assertTrue(wrote, "首次写入路由元数据应发生实际写盘")

        state = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(
            state["decision"]["final_model"], "MiniMax-M3",
            "update_fields 写路由元数据时不得覆盖既有 decision",
        )

    def test_update_fields_noop_skips_rewrite(self):
        store = self._store()
        store.update_fields(
            self.sid,
            str(self.project_root),
            updates={"route_model": "MiniMax-M3", "task_complexity": "medium"},
        )
        path = self._state_path()
        before = path.read_text(encoding="utf-8")

        # 睡眠确保若发生重写，last_update 文本几乎必然不同。
        time.sleep(1.05)
        wrote = store.update_fields(
            self.sid,
            str(self.project_root),
            updates={"route_model": "MiniMax-M3", "task_complexity": "medium"},
        )
        after = path.read_text(encoding="utf-8")

        self.assertFalse(wrote, "字段无变化时应跳过写盘")
        self.assertEqual(before, after, "no-op update_fields 不应改写文件内容")
