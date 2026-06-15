"""
test_scenario_04_model_override.py — E2E 场景 4: ~model 显式覆盖
=================================================================

V1.3 §6.4 端到端验证：用户 `~model` 指令覆盖决策引擎路由。

场景：
  1. 用户 prompt 含 `~model ds-v4-pro` → final_model 被覆写为 deepseek-v4-pro
  2. 用户 prompt 含 `~model reset` → 清除覆盖，回到自动路由
  3. model_override 字段持久化在 model_router_state_<sid>.json

路由优先级：prompt ~model > model_override(file) > op > stage > default
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
    run_stage_detector,
    setup_temp_project,
)

if HOOKS_DIR_STR not in sys.path:
    sys.path.insert(0, HOOKS_DIR_STR)


class TestScenario04ModelOverride(unittest.TestCase):
    """~model 显式覆盖：prompt 优先级 > 决策引擎。"""

    def setUp(self):
        self.project_root, self.sid = setup_temp_project()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project_root, ignore_errors=True)

    # ── Test 1: ~model 覆盖 final_model ─────────────────────────────────

    def test_model_override_overrides_final_model(self):
        """prompt 含 ~model ds-v4-pro → final_model 必须是 deepseek-v4-pro。"""
        prompt = "~model ds-v4-pro 帮我写一个 API endpoint"

        stdout, stderr, rc = run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )
        self.assertEqual(rc, 0, f"stage_detector 退出码非零: {rc}\nstderr: {stderr}")

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state, f"未写 state 文件\nstderr: {stderr}")

        # 验证 decision 字段
        decision = state["decision"]
        assert_decision_shape(self, decision)
        self.assertEqual(
            decision["final_model"], "deepseek-v4-pro",
            f"~model ds-v4-pro 必须覆写 final_model，实际: {decision['final_model']}",
        )
        self.assertEqual(
            decision["decision_source"], "explicit",
            f"~model 覆盖的 decision_source 必须是 'explicit'，实际: {decision['decision_source']}",
        )

    # ── Test 2: model_override 持久化 ───────────────────────────────────

    def test_model_override_persisted_in_state(self):
        """~model 指令应写入 model_override 字段到 state JSON。"""
        prompt = "~model mm3 分析下这段代码"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state)

        # model_override 字段应存在且为规范名
        mo = state.get("model_override")
        self.assertIsNotNone(
            mo, f"state 应包含 model_override 字段，实际 keys: {sorted(state.keys())}",
        )
        self.assertEqual(
            mo, "MiniMax-M3",
            f"~model mm3 应解析为 MiniMax-M3，实际: {mo}",
        )

    # ── Test 3: ~model reset 清除覆盖 ───────────────────────────────────

    def test_model_reset_clears_override(self):
        """~model reset 应清除 model_override，恢复自动路由。"""
        # Step 1: 设置覆盖
        prompt_set = "~model ds-v4-pro 先设置模型"
        run_stage_detector(
            prompt=prompt_set, sid=self.sid, project_root=self.project_root,
        )
        state1 = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state1)
        self.assertEqual(state1.get("model_override"), "deepseek-v4-pro")

        # Step 2: 清除覆盖
        prompt_reset = "~model reset 好了回到自动"
        run_stage_detector(
            prompt=prompt_reset, sid=self.sid, project_root=self.project_root,
        )
        state2 = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state2)

        # model_override 应为 None 或不存在
        mo = state2.get("model_override")
        self.assertTrue(
            mo is None or mo == "",
            f"~model reset 后 model_override 应为 None/空，实际: {mo!r}",
        )

        # decision_source 应回到自动路由（prompt，而非 explicit）
        decision = state2.get("decision", {})
        if decision:
            self.assertNotEqual(
                decision.get("decision_source"), "explicit",
                "reset 后 decision_source 不应仍是 'explicit'",
            )

    # ── Test 4: 无 ~model 时走普通路由 ─────────────────────────────────

    def test_no_model_override_uses_normal_routing(self):
        """无 ~model 指令时，model_override 应为 None，走决策引擎路由。"""
        prompt = "帮我写一个 hello world 函数"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state)

        # 无 ~model 指令时 model_override 应为 None 或不存在
        mo = state.get("model_override")
        self.assertTrue(
            mo is None or mo == "",
            f"无 ~model 指令时 model_override 应为 None，实际: {mo!r}",
        )

        # decision 应按正常流程（prompt 来源）
        decision = state.get("decision", {})
        if decision:
            self.assertEqual(
                decision.get("decision_source"), "prompt",
                f"无 ~model 时应为 prompt 来源，实际: {decision.get('decision_source')}",
            )

    # ── Test 5: 未知 alias 不覆盖 ─────────────────────────────────────

    def test_unknown_alias_does_not_override(self):
        """未识别的 ~model alias 不应覆写 final_model。"""
        prompt = "~model none-such-model-xyz 帮我看看代码"

        run_stage_detector(
            prompt=prompt, sid=self.sid, project_root=self.project_root,
        )

        state = read_state(self.sid, self.project_root)
        self.assertIsNotNone(state)

        # 未知 alias 不应写 model_override
        mo = state.get("model_override")
        self.assertTrue(
            mo is None or mo == "",
            f"未知 alias 不应写 model_override，实际: {mo!r}",
        )

        # decision_source 不应是 explicit
        decision = state.get("decision", {})
        if decision:
            self.assertNotEqual(
                decision.get("decision_source"), "explicit",
                "未知 alias 不应设 decision_source=explicit",
            )


if __name__ == "__main__":
    unittest.main()
