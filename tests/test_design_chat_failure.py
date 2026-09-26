import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.design_chat import DesignChatManager


class DesignChatFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        self.root = Path(self._tmp.name)
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        self.manager = DesignChatManager(self.root)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def _wait(self, timeout=5.0):
        deadline = time.time() + timeout
        status = self.manager.job_status("book", "concept")
        while time.time() < deadline:
            status = self.manager.job_status("book", "concept")
            if status.get("status") in {"completed", "failed", "stopped"}:
                return status
            time.sleep(0.02)
        self.fail("job did not finish: %s" % status)

    def test_failed_job_is_recorded_in_the_conversation(self):
        with patch(
            "training.adaptive_builder.gen_design_concept",
            side_effect=RuntimeError("model returned broken JSON"),
        ):
            self.manager.start_message("book", "concept", "a story about tides")
            status = self._wait()
        self.assertEqual(status["status"], "failed")

        conv_path = self.root / "book" / "file_system" / "story_design" / "conversation" / "concept.json"
        turns = json.loads(conv_path.read_text(encoding="utf-8"))["turns"]
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["content"], "a story about tides")
        self.assertIn("Generation failed", turns[1]["content"])
        self.assertIn("model returned broken JSON", turns[1]["content"])


if __name__ == "__main__":
    unittest.main()
