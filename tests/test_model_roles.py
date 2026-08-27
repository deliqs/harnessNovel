import os
import unittest
from unittest.mock import patch

from core.config import ConfigLoader
from core.llm_provider import LLMProvider


class ModelRoleConfigTests(unittest.TestCase):
    def setUp(self):
        self.environment = os.environ.copy()
        for key in list(os.environ):
            if key.startswith(("ADAPTIVE_BUILDER_LITE_", "DRAFT_", "EDITOR_", "CRITIC_")):
                os.environ.pop(key, None)
        ConfigLoader.reload()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)
        ConfigLoader.reload()

    def test_optional_roles_fall_back_to_lite_field_by_field(self):
        os.environ.update({
            "ADAPTIVE_BUILDER_LITE_MODEL": "lite-model",
            "ADAPTIVE_BUILDER_LITE_BASE_URL": "https://lite.example/v1",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "lite-key",
            "DRAFT_MODEL": "draft-model",
            "EDITOR_API_KEY": "editor-key",
        })

        self.assertEqual(
            ConfigLoader.get_draft_config(),
            {"model": "draft-model", "base_url": "https://lite.example/v1", "api_key": "lite-key"},
        )
        self.assertEqual(
            ConfigLoader.get_editor_config(),
            {"model": "lite-model", "base_url": "https://lite.example/v1", "api_key": "editor-key"},
        )
        self.assertEqual(ConfigLoader.get_critic_config(), ConfigLoader.get_adaptive_builder_lite_config())

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            ConfigLoader.get_model_role_config("planner")

    def test_deactivating_an_optional_role_restores_lite_fallback(self):
        os.environ.update({
            "ADAPTIVE_BUILDER_LITE_MODEL": "lite-model",
            "ADAPTIVE_BUILDER_LITE_BASE_URL": "https://lite.example/v1",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "lite-key",
            "DRAFT_MODEL": "draft-model",
        })
        self.assertEqual(ConfigLoader.get_draft_config()["model"], "draft-model")
        ConfigLoader.deactivate(["DRAFT_MODEL"])
        self.assertEqual(ConfigLoader.get_draft_config()["model"], "lite-model")

    def test_generate_with_metadata_preserves_string_generate_contract(self):
        provider = LLMProvider(model="draft-model", base_url="https://token@api.example/v1?api_key=hidden", api_key="key")
        with patch.object(provider, "generate", return_value="generated text") as generate:
            result = provider.generate_with_metadata("untrusted input", temperature=0.2, max_tokens=123)

        generate.assert_called_once()
        self.assertEqual(result["content"], "generated text")
        self.assertEqual(result["model"], "draft-model")
        self.assertEqual(result["base_url"], "https://api.example/v1")
        self.assertTrue(result["succeeded"])
        self.assertNotIn("key", repr(result))


if __name__ == "__main__":
    unittest.main()
