"""
test_scenario_02_tool_heavy_research.py — E2E 场景 2: 工具密集调研
==================================================================

V1.3 §6.4 端到端验证：runtime_score 累积触发 maybe_redecide 升级。

场景：用户模糊调研请求 → decide() 给 medium → 多个 PostToolUse
累积 runtime_score → maybe_redecide 升级到 complex → 模型切到
deepseek-v4-pro。

TDD: 本测试是 RED 起点 — 暴露 decide() 锁定后 maybe_redecide
永远短路的 Stage 5 设计冲突（line 202-203）。
"""

import json
import os
import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from _helpers import (
    HOOKS_DIR_STR,
    assert_decision_shape,
    read_state,
    run_post_tool_handler,
    run_stage_detector,
    setup_temp_project,
)

if HOOKS_DIR_STR not in sys.path:
    sys.path.insert(0, HOOKS_DIR_STR)


# 权重参考（config/decision_weights.yaml）：
#   Read=2, Edit=4, Write=3, MultiEdit=5, Grep=3, Glob=2,
#   WebSearch=4, WebFetch=3, Bash=2, TodoWrite=8
# runtime_score 必须 > 70 才能把 score 抬到 complex 档（_label_from_score 阈值）。
# 下面 20 工具累加 = 4+4+5+4+3+4+4+4+5+4+4+4+3+3+4+4+5+4+4+4 = 80 → 进入 complex 档
_TOOL_HEAVY_SEQUENCE: list[tuple[str, dict]] = [
    ("WebSearch",   {"query": "model_router v1.3 decision_engine design"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit",   {"edits": [{"old_string": "a", "new_string": "b"}]}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Grep",        {"pattern": "decide\\(", "path": "/Users/zorro/.claude/hooks/model_router"}),
    ("WebSearch",   {"query": "complexity grading threshold YAML"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit",   {"edits": [{"old_string": "a", "new_string": "b"}]}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Grep",        {"pattern": "runtime_score", "path": "/Users/zorro/.claude/hooks/model_router"}),
    ("Grep",        {"pattern": "DecisionRecord", "path": "/Users/zorro/.claude/hooks/model_router"}),
    ("WebSearch",   {"query": "Decision Lock semantics"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit",   {"edits": [{"old_string": "a", "new_string": "b"}]}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    ("Edit",        {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
]


class TestScenario02ToolHeavyResearch(unittest.TestCase):
    """工具密集调研：runtime_score 累积触发复杂度升级。"""

    def setUp(self):
        self.project_root, self.sid = setup_temp_project()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project_root, ignore_errors=True)

    def test_runtime_score_accumulation_upgrades_complexity(self):
        """模糊调研 prompt → 多个 PostToolUse → 升级到 complex。"""
        # 模糊调研 prompt（命中 _AMBIGUOUS_PROMPT_HINTS 的 "帮我" / "怎么"）→ 预期 medium
        prompt = "帮我调研下 model_router v1.3 的决策链路是怎么设计的"

        # 1) 初始 decide() — 模糊 prompt 应落到 medium（保守偏置）
        stdout, stderr, rc = run_stage_detector(
            prompt=prompt,
            sid=self.sid,
            project_root=self.project_root,
        )
        self.assertEqual(rc, 0, f"stage_detector 退出码非零: {rc}\nstderr: {stderr}")

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state, "未写 state 文件")
        decision = state.get("decision")
        self.assertIsNotNone(decision, "state 缺 decision 字段")
        assert_decision_shape(self, decision)

        # 初始决策：模糊 prompt → medium
        initial_complexity = decision["task_complexity"]
        self.assertEqual(
            initial_complexity, "medium",
            f"模糊调研 prompt 应落 medium，实际: {initial_complexity}",
        )
        # 首次 decide() 不应锁（maybe_redecide 才是终裁）
        self.assertFalse(
            decision["locked"],
            "首次 decide() 必须 locked=False（maybe_redecide 升级时才锁定）",
        )

        # 2) 模拟 20 个工具调用累积 runtime_score（总分 80 → complex 档）
        for tool_name, tool_input in _TOOL_HEAVY_SEQUENCE:
            event = {
                "session_id": self.sid,
                "cwd": self.project_root,
                "hook_event_name": "PostToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            _, _, _ = run_post_tool_handler(self.sid, self.project_root, event)

        # 3) 验证：累积后复杂度应升级到 complex，模型切到 deepseek-v4-pro
        final_state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(final_state)
        final_decision = final_state["decision"]
        assert_decision_shape(self, final_decision)

        self.assertEqual(
            final_decision["task_complexity"], "complex",
            f"20 个工具累积后应升级到 complex，实际: {final_decision['task_complexity']}",
        )
        self.assertEqual(
            final_decision["final_model"], "deepseek-v4-pro",
            f"complex 应切到 deepseek-v4-pro，实际: {final_decision['final_model']}",
        )
        self.assertTrue(
            final_decision["locked"],
            "升级后必须 locked=True（maybe_redecide 决定锁）",
        )
        self.assertEqual(
            final_decision["decision_source"], "runtime",
            f"升级来源应是 runtime（不是 prompt/todowrite），实际: {final_decision['decision_source']}",
        )

    def test_runtime_score_actually_accumulates_in_state(self):
        """runtime_score 字段必须真实累积（不能永远是 0）。"""
        prompt = "看下 hooks 的整体结构"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        # 跑前 3 个工具
        for tool_name, tool_input in _TOOL_HEAVY_SEQUENCE[:3]:
            event = {
                "session_id": self.sid,
                "cwd": self.project_root,
                "hook_event_name": "PostToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            run_post_tool_handler(self.sid, self.project_root, event)

        state = read_state(self.sid, self.project_root)
        rs = state.get("runtime_score") or {}
        actual_score = rs.get("score", 0) if isinstance(rs, dict) else 0

        # 3 个工具至少累积到非零得分（WebSearch 4 + Edit 4 + MultiEdit 5 = 13）
        self.assertGreater(
            actual_score, 0,
            f"3 个工具调用后 runtime_score 必须 > 0，实际: {actual_score}",
        )


if __name__ == "__main__":
    unittest.main()
