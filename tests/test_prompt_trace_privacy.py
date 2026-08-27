import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from core import config as config_module
from core.config import ConfigLoader
from core.prompt_trace import capture_prompts, record_prompt


class PromptTracePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.environment = os.environ.copy()
        self.temporary = tempfile.TemporaryDirectory()
        self.trace_path = os.path.join(self.temporary.name, "trace.jsonl")
        for key in list(os.environ):
            if key.startswith("HARNESS_NOVEL_PROMPT_TRACE"):
                os.environ.pop(key, None)
        os.environ["HARNESS_NOVEL_PROMPT_TRACE_FILE"] = self.trace_path

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)
        ConfigLoader.reload()
        self.temporary.cleanup()

    def _events(self):
        with open(self.trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_default_trace_records_metadata_without_prompt_content(self):
        event = record_prompt("reference text\nAPI_KEY=secret-value", "model-a")

        self.assertEqual(event["trace_mode"], "metadata")
        self.assertFalse(event["content_recorded"])
        self.assertNotIn("prompt", event)
        self.assertEqual(self._events(), [event])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.trace_path).st_mode), 0o600)

    def test_full_trace_is_redacted_and_bounded(self):
        os.environ["HARNESS_NOVEL_PROMPT_TRACE_MODE"] = "full"
        os.environ["HARNESS_NOVEL_PROMPT_TRACE_MAX_CHARS"] = "80"
        event = record_prompt("Authorization: Bearer very-secret-token\nsk-abcdefghijk\nAPI_KEY=another-secret", "model-a")

        self.assertTrue(event["content_recorded"])
        self.assertNotIn("very-secret-token", event["prompt"])
        self.assertNotIn("another-secret", event["prompt"])
        self.assertNotIn("sk-abcdefghijk", event["prompt"])
        self.assertLessEqual(len(event["prompt"]), 80)

    def test_trace_mode_uses_saved_config_when_no_environment_override_exists(self):
        config_path = os.path.join(self.temporary.name, ".env")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("HARNESS_NOVEL_PROMPT_TRACE_MODE=full\n")
        ConfigLoader.reload()
        with patch.object(config_module, "_GLOBAL_ENV_PATH", config_path):
            event = record_prompt("debug prompt", "model-a")

        self.assertTrue(event["content_recorded"])
        self.assertEqual(event["trace_mode"], "full")

    def test_retention_and_callback_keep_safe_events(self):
        os.environ["HARNESS_NOVEL_PROMPT_TRACE_MAX_EVENTS"] = "2"
        captured = []
        with capture_prompts(captured.append):
            record_prompt("first", "model-a")
            record_prompt("second", "model-a")
            record_prompt("third", "model-a")

        self.assertEqual(len(captured), 3)
        self.assertEqual([event["prompt_chars"] for event in self._events()], [6, 5])

    def test_retention_bound_of_one_keeps_only_the_latest_event(self):
        os.environ["HARNESS_NOVEL_PROMPT_TRACE_MAX_EVENTS"] = "1"
        record_prompt("first", "model-a")
        record_prompt("second", "model-a")
        record_prompt("third", "model-a")

        self.assertEqual([event["prompt_chars"] for event in self._events()], [5])


if __name__ == "__main__":
    unittest.main()
