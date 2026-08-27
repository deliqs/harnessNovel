import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from webui import app
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    app = None


@unittest.skipUnless(app is not None, "FastAPI is not installed in this test environment")
class WebConfigParityTests(unittest.TestCase):
    def test_web_config_exposes_roles_and_preserves_existing_env_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text("# Keep this comment\nADAPTIVE_BUILDER_LITE_MODEL=lite\nADAPTIVE_BUILDER_LITE_BASE_URL=https://lite.example/v1\nADAPTIVE_BUILDER_LITE_API_KEY=lite-key\nUNRELATED=value\n", encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", config_path):
                fallback_payload = app._config_for_client()
                app._update_env({"DRAFT_MODEL": "draft", "HARNESS_NOVEL_PROMPT_TRACE_MODE": "full"})
                payload = app._config_for_client()
                content_after_save = config_path.read_text(encoding="utf-8")
                app._update_env({}, {"DRAFT_MODEL"})
                cleared_payload = app._config_for_client()

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("# Keep this comment", content)
            self.assertIn("UNRELATED=value", content)
            self.assertIn("DRAFT_MODEL=draft", content_after_save)
            self.assertEqual(payload["groups"]["draft"]["model"], "draft")
            self.assertEqual(payload["groups"]["editor"]["model"], "")
            self.assertEqual(payload["prompt_trace_mode"], "full")
            self.assertTrue(fallback_payload["groups"]["draft"]["inherited"])
            self.assertTrue(fallback_payload["groups"]["draft"]["api_key_configured"])
            self.assertEqual(cleared_payload["groups"]["draft"]["model"], "")
            self.assertNotIn("DRAFT_MODEL=", config_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(config_path).st_mode), 0o600)

class WebConfigStaticParityTests(unittest.TestCase):
    def test_static_settings_knows_every_role_prefix(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "wizard-v0.js").read_text(encoding="utf-8")
        for group_id, prefix in {
            "data_builder": "DATA_BUILDER",
            "adaptive_builder": "ADAPTIVE_BUILDER",
            "adaptive_builder_lite": "ADAPTIVE_BUILDER_LITE",
            "draft": "DRAFT",
            "editor": "EDITOR",
            "critic": "CRITIC",
        }.items():
            self.assertIn('%s: "%s"' % (group_id, prefix), script)
        self.assertIn("HARNESS_NOVEL_PROMPT_TRACE_MODE", script)
        self.assertIn("clear_keys", script)
        self.assertIn("Using Lite fallback", script)


if __name__ == "__main__":
    unittest.main()
