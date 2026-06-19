import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic  # noqa: E402
import llm_classifier  # noqa: E402
import stage_config  # noqa: E402


class _FakeStatusError(anthropic.APIStatusError):
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.response = None
        self.request = None


class TestStageConfigLlmClassifierFallback(unittest.TestCase):
    def test_yaml_loads_classifier_fallback_route(self):
        cfg = stage_config.LLM_CLASSIFIER_CONFIG
        self.assertEqual(cfg["model"], "deepseek-v4-flash")
        self.assertEqual(cfg["fallback_model"], "MiniMax-M3")
        self.assertEqual(cfg["fallback_base_url"], "https://api.minimaxi.com/anthropic")
        self.assertEqual(cfg["fallback_api_key_env"], "MINIMAX_API_KEY")


class TestClassifierFallbackBehavior(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key, value in {
            "DEEPSEEK_API_KEY": "deepseek-test",
            "MINIMAX_API_KEY": "minimax-test",
        }.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_status_error_falls_back_to_secondary_model(self):
        calls = []

        def fake_invoke(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "deepseek-v4-flash":
                raise _FakeStatusError(402, {"error": {"message": "insufficient balance"}})
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=(
                            '{"stage":"implement","pattern":"feature",'
                            '"pattern_confidence":0.9,"complexity_score":20,'
                            '"complexity_label":"simple","complexity_confidence":0.9,'
                            '"task_field":"backend","task_field_confidence":0.8,'
                            '"reasoning":"ok","is_valid_prompt":true}'
                        ),
                    )
                ]
            )

        with patch.object(llm_classifier, "_invoke_classifier", side_effect=fake_invoke):
            result = llm_classifier.classify("写一个登录接口")

        self.assertEqual(calls, ["deepseek-v4-flash", "MiniMax-M3"])
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["pattern"], "feature")

    def test_non_retryable_status_does_not_fallback(self):
        calls = []

        def fake_invoke(**kwargs):
            calls.append(kwargs["model"])
            raise _FakeStatusError(400, {"error": {"message": "bad request"}})

        with patch.object(llm_classifier, "_invoke_classifier", side_effect=fake_invoke):
            with self.assertRaises(RuntimeError) as ctx:
                llm_classifier.classify("写一个登录接口")

        self.assertEqual(calls, ["deepseek-v4-flash"])
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_insufficient_text_triggers_fallback_even_without_402(self):
        calls = []

        def fake_invoke(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "deepseek-v4-flash":
                raise _FakeStatusError(422, {"error": {"message": "insufficient quota"}})
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=(
                            '{"stage":"implement","pattern":"feature",'
                            '"pattern_confidence":0.9,"complexity_score":20,'
                            '"complexity_label":"simple","complexity_confidence":0.9,'
                            '"task_field":"backend","task_field_confidence":0.8,'
                            '"reasoning":"ok","is_valid_prompt":true}'
                        ),
                    )
                ]
            )

        with patch.object(llm_classifier, "_invoke_classifier", side_effect=fake_invoke):
            llm_classifier.classify("写一个登录接口")

        self.assertEqual(calls, ["deepseek-v4-flash", "MiniMax-M3"])


if __name__ == "__main__":
    unittest.main()
