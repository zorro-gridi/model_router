import unittest


class TestStageConfigYamlSources(unittest.TestCase):
    def test_stage_config_materializes_routes_from_yaml(self):
        import stage_config

        explore = stage_config.STAGE_CONFIG["explore"]
        self.assertEqual(explore["model"], "MiniMax-M3")
        self.assertEqual(explore["base_url"], "https://api.minimaxi.com/anthropic")
        self.assertEqual(explore["fb_model"], "deepseek-v4-pro")
        self.assertEqual(explore["fb_base_url"], "https://api.deepseek.com/anthropic")

    def test_model_registry_contains_openai_gpt_models(self):
        import stage_config

        self.assertIn("GPT-5.4", stage_config.MODEL_TO_CONFIG)
        self.assertIn("GPT-5.4-Mini", stage_config.MODEL_TO_CONFIG)
        self.assertEqual(stage_config.MODEL_TO_PROVIDER["GPT-5.4"], "openai")
        self.assertEqual(stage_config.MODEL_TO_PROVIDER["GPT-5.4-Mini"], "openai")

    def test_openai_provider_complexity_models_are_registered(self):
        import stage_config

        mapping = stage_config.PROVIDER_COMPLEXITY_MODELS["openai"]
        self.assertEqual(mapping["simple"], "GPT-5.4-Mini")
        self.assertEqual(mapping["medium"], "GPT-5.4")
        self.assertEqual(mapping["complex"], "GPT-5.4")

    def test_model_tiers_put_openai_models_above_existing_models(self):
        import stage_config

        self.assertGreater(
            stage_config.get_model_tier("GPT-5.4"),
            stage_config.get_model_tier("deepseek-v4-pro"),
        )
        self.assertGreater(
            stage_config.get_model_tier("GPT-5.4-Mini"),
            stage_config.get_model_tier("MiniMax-M3"),
        )


if __name__ == "__main__":
    unittest.main()
