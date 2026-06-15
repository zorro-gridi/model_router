"""
test_scenario_01_simple_qa.py — E2E 场景 1: 简单问答
====================================================

V1.3 §6.4 端到端验证：UserPromptSubmit 触发 decide() 写决策，
proxy 读侧拿回正确 model。

场景：用户问"什么是 Python 装饰器？"
  - 预期 task_complexity: simple | medium
  - 预期 final_model: MiniMax-M3（基线）
  - 预期 decision.locked: True
  - 预期 decision_source: prompt

TDD: 本测试是 RED 起点 — 暴露 stage_detector.py 没有调
decision_engine.decide() 的集成 gap（Stage 5/7 production bug）。
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
    resolve_decision,
    run_stage_detector,
    setup_temp_project,
)

if HOOKS_DIR_STR not in sys.path:
    sys.path.insert(0, HOOKS_DIR_STR)


class TestScenario01SimpleQA(unittest.TestCase):
    """简单问答：UserPromptSubmit → decide() → 写 state → proxy 读回。"""

    def setUp(self):
        self.project_root, self.sid = setup_temp_project()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project_root, ignore_errors=True)

    def test_userpromptsubmit_writes_decision_record(self):
        """跑 stage_detector 后，state 必须含完整 DecisionRecord。"""
        # 简单问答 prompt — 预期落到 simple/medium
        prompt = "什么是 Python 装饰器？"

        # 1) 跑 stage_detector.py
        stdout, stderr, rc = run_stage_detector(
            prompt=prompt,
            sid=self.sid,
            project_root=self.project_root,
        )
        self.assertEqual(rc, 0, f"stage_detector 退出码非零: {rc}\nstderr: {stderr}")

        # 2) 读 state 文件
        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state, f"未写 state 文件\nstderr: {stderr}")
        self.assertIn("decision", state, "state 缺 decision 字段")

        # 3) DecisionRecord schema 验证
        decision = state["decision"]
        assert_decision_shape(self, decision)
        self.assertEqual(decision["session_id"], self.sid)
        self.assertIn(
            decision["task_complexity"], ("simple", "medium"),
            f"简单问答应落到 simple/medium，实际: {decision['task_complexity']}",
        )
        self.assertEqual(
            decision["final_model"], "MiniMax-M3",
            f"简单问答应走基线模型，实际: {decision['final_model']}",
        )
        self.assertTrue(decision["locked"], "decide() 必须返回 locked=True")
        self.assertEqual(decision["decision_source"], "prompt")

    def test_proxy_resolve_decision_returns_real_decision(self):
        """proxy._v13_resolve_decision() 应能读回非空 decision。"""
        prompt = "解释一下闭包的概念"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        decision = resolve_decision(self.sid, self.project_root)
        self.assertIsNotNone(
            decision, "proxy 应能解析出 decision（不能是 None/空 dict）",
        )
        self.assertIsInstance(decision, dict)
        self.assertNotEqual(
            decision, {},
            "decision 不应是空 dict（暴露 stage_detector 未调 decide() 的 bug）",
        )
        assert_decision_shape(self, decision)
        self.assertIn(
            decision.get("task_complexity"), ("simple", "medium"),
        )
        self.assertEqual(decision.get("final_model"), "MiniMax-M3")


if __name__ == "__main__":
    unittest.main()
