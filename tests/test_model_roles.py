import os
import tempfile
import unittest
from unittest.mock import patch

from core.config import ConfigLoader
from core.llm_provider import LLMProvider


class ModelRoleConfigTests(unittest.TestCase):
    def setUp(self):
        self.environment = os.environ.copy()
        for key in list(os.environ):
            if key.startswith(("DATA_BUILDER_", "ADAPTIVE_BUILDER_", "DRAFT_", "EDITOR_", "CRITIC_")):
                os.environ.pop(key, None)
        self._env_file = patch("core.config._load_env", return_value={})
        self._env_file.start()
        ConfigLoader.reload()

    def tearDown(self):
        self._env_file.stop()
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
            {
                "model": "draft-model",
                "base_url": "https://lite.example/v1",
                "api_key": "lite-key",
                "max_tokens": None,
            },
        )
        self.assertEqual(
            ConfigLoader.get_editor_config(),
            {
                "model": "lite-model",
                "base_url": "https://lite.example/v1",
                "api_key": "editor-key",
                "max_tokens": None,
            },
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

    def test_max_tokens_prefers_process_env_then_dotenv(self):
        self._env_file.stop()
        try:
            with tempfile.TemporaryDirectory() as directory:
                env_path = os.path.join(directory, ".env")
                with open(env_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        "# comment\n"
                        "ADAPTIVE_BUILDER_MAX_TOKENS=4096\n"
                        "DATA_BUILDER_MAX_TOKENS=not-a-number\n"
                        "ADAPTIVE_BUILDER_LITE_MAX_TOKENS= 2048 \n"
                    )
                with patch("core.config._GLOBAL_ENV_PATH", env_path):
                    os.environ["ADAPTIVE_BUILDER_MAX_TOKENS"] = "32768"
                    ConfigLoader.reload()
                    self.assertEqual(ConfigLoader.get_adaptive_builder_config()["max_tokens"], 32768)

                    os.environ["ADAPTIVE_BUILDER_MAX_TOKENS"] = ""
                    ConfigLoader.reload()
                    self.assertEqual(ConfigLoader.get_adaptive_builder_config()["max_tokens"], 4096)

                    os.environ.pop("ADAPTIVE_BUILDER_MAX_TOKENS", None)
                    ConfigLoader.reload()
                    self.assertEqual(ConfigLoader.get_adaptive_builder_config()["max_tokens"], 4096)
                    self.assertIsNone(ConfigLoader.get_data_builder_config()["max_tokens"])
                    self.assertEqual(ConfigLoader.get_adaptive_builder_lite_config()["max_tokens"], 2048)
                    self.assertIsNone(ConfigLoader._build_config("DRAFT")["max_tokens"])
        finally:
            self._env_file.start()
            ConfigLoader.reload()

    def test_invalid_or_blank_max_tokens_is_none(self):
        for raw in ("", "0", "-1", "abc", "8.5"):
            with self.subTest(raw=raw):
                os.environ["ADAPTIVE_BUILDER_MAX_TOKENS"] = raw
                ConfigLoader.reload()
                self.assertIsNone(ConfigLoader.get_adaptive_builder_config()["max_tokens"])
        os.environ.pop("ADAPTIVE_BUILDER_MAX_TOKENS", None)
        ConfigLoader.reload()
        self.assertIsNone(ConfigLoader.get_adaptive_builder_config()["max_tokens"])
        os.environ["ADAPTIVE_BUILDER_MAX_TOKENS"] = " 512 "
        ConfigLoader.reload()
        self.assertEqual(ConfigLoader.get_adaptive_builder_config()["max_tokens"], 512)

    def test_role_inherits_lite_max_tokens_fieldwise(self):
        os.environ.update({
            "ADAPTIVE_BUILDER_LITE_MODEL": "lite-model",
            "ADAPTIVE_BUILDER_LITE_BASE_URL": "https://lite.example/v1",
            "ADAPTIVE_BUILDER_LITE_API_KEY": "lite-key",
            "ADAPTIVE_BUILDER_LITE_MAX_TOKENS": "4096",
            "DRAFT_MODEL": "draft-model",
            "EDITOR_MAX_TOKENS": "2048",
        })
        draft = ConfigLoader.get_draft_config()
        self.assertEqual(draft["model"], "draft-model")
        self.assertEqual(draft["base_url"], "https://lite.example/v1")
        self.assertEqual(draft["max_tokens"], 4096)
        self.assertEqual(ConfigLoader.get_editor_config()["max_tokens"], 2048)
        self.assertEqual(ConfigLoader.get_editor_config()["model"], "lite-model")
        os.environ["CRITIC_MAX_TOKENS"] = "nope"
        self.assertEqual(ConfigLoader.get_critic_config(), ConfigLoader.get_adaptive_builder_lite_config())
        self.assertEqual(ConfigLoader.get_critic_config()["max_tokens"], 4096)

    def test_templates_set_adaptive_builder_max_tokens(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        needle = "# Book and stage design re-emits whole files.\nADAPTIVE_BUILDER_MAX_TOKENS=32768"
        note = "`*_MAX_TOKENS` is optional per slot; unset means the provider or server default."
        for name in (".env.example", "README.md", "novel_cli.py"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn(needle, text)
            if name == "README.md":
                self.assertIn(note, text)

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
