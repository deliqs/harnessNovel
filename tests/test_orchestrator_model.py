"""Offline tests for the orchestrator model factory and per-turn thinking switch."""
import unittest
from unittest.mock import patch

from pydantic_ai.models.openai import OpenAIChatModel

from tests.orchestrator_fakes import OrchestratorAppCase, ScriptedModel, run_body, user
from webui.orchestrator import model as orchestrator_model

LITE = "core.config.ConfigLoader.get_adaptive_builder_lite_config"


def _thinking(settings):
    return settings["extra_body"]["chat_template_kwargs"]["enable_thinking"]


class ModelFactoryTests(unittest.TestCase):
    def setUp(self):
        orchestrator_model._cache.clear()

    def tearDown(self):
        orchestrator_model._cache.clear()

    def test_builds_the_lite_slot_with_the_orca_profile_once_per_config(self):
        config = {"model": "orca", "base_url": "http://127.0.0.1:8091/v1", "api_key": ""}
        with patch(LITE, return_value=config):
            model = orchestrator_model.build_model()
            again = orchestrator_model.build_model()

        self.assertIsInstance(model, OpenAIChatModel)
        self.assertIs(model, again)
        self.assertEqual(model.model_name, "orca")
        profile = model.profile
        self.assertEqual(profile["openai_chat_thinking_field"], "reasoning_content")
        self.assertFalse(profile["openai_chat_send_back_thinking_parts"])
        self.assertFalse(profile["openai_chat_supports_multiple_system_messages"])
        self.assertFalse(profile["openai_supports_strict_tool_definition"])
        self.assertFalse(profile["openai_supports_tool_choice_required"])

    def test_missing_model_or_base_url_is_refused(self):
        for config in ({"model": "", "base_url": "http://x/v1"}, {"model": "orca", "base_url": ""}):
            with patch(LITE, return_value=config), self.assertRaises(ValueError):
                orchestrator_model.build_model()

    def test_thinking_settings(self):
        on = orchestrator_model.model_settings(True)
        off = orchestrator_model.model_settings(False)
        self.assertEqual(orchestrator_model.THINKING_MAX_TOKENS, 32768)
        self.assertTrue(_thinking(on))
        self.assertEqual(on["max_tokens"], orchestrator_model.THINKING_MAX_TOKENS)
        self.assertFalse(_thinking(off))
        self.assertNotIn("max_tokens", off)
        self.assertIs(off, orchestrator_model.THINK_OFF)


class ThinkingSwitchTests(OrchestratorAppCase):
    def test_forwarded_thinking_turns_reasoning_on_for_one_turn(self):
        scripted = self.use_model(ScriptedModel(["Plain.", "Thought through.", "Plain again."]))

        self.post_turn(run_body([user("One")]))
        self.post_turn(run_body([user("Two")], thinking=True))
        self.post_turn(run_body([user("Three")], thinking=False))

        self.assertEqual([_thinking(settings) for settings in scripted.settings], [False, True, False])
        self.assertEqual(
            [settings.get("max_tokens") for settings in scripted.settings],
            [None, orchestrator_model.THINKING_MAX_TOKENS, None],
        )


if __name__ == "__main__":
    unittest.main()
