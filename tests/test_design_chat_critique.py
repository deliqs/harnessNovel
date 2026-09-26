import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui.app import create_app
from webui.design_chat import DesignChatManager


_ANSWER = "1. **Name the opposition** — No rival is named. *(Depth)*\n\nYou can say e.g. \"apply 2 and 4\" to apply those points."
_POINTS = [
    {"number": 1, "title": "Name the opposition", "detail": "No rival is named.", "lenses": ["Depth"]},
    {"number": 2, "title": "Hook the opening", "detail": "The first phase explains weather.", "lenses": ["Engagement"]},
    {"number": 4, "title": "Raise the cost", "detail": "The hero can walk away.", "lenses": ["Character stakes"]},
]


class DesignChatCritiqueTests(unittest.TestCase):
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

    def _design_dir(self):
        path = self.root / "book" / "file_system" / "story_design"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_concept(self):
        base = self._design_dir()
        files = {
            "worldview.md": "# Worldview\n\nTides and moons.\n",
            "rough_outline.md": "# Rough outline\n\nA tide-born hero.\n",
            "stage_outline.md": "# Phase outline\n\n## Phase 1: Arrival\n",
        }
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")
        return {name: (base / name).read_bytes() for name in files}

    def _turns(self):
        path = self.root / "book" / "file_system" / "story_design" / "conversation" / "concept.json"
        return json.loads(path.read_text(encoding="utf-8"))["turns"]

    def _write_points(self):
        (self._design_dir() / "critique_points_concept.json").write_text(
            json.dumps({"scope": "concept", "points": _POINTS}), encoding="utf-8",
        )

    def _client(self):
        web_home = self.root / "web-home"
        web_home.mkdir(exist_ok=True)
        with patch("webui.app.WEB_HOME", web_home), patch(
            "webui.app.WEB_SETTINGS_PATH", web_home / "settings.json",
        ):
            app = create_app(str(self.root))
            return TestClient(app), app.state.runtime, web_home

    def test_critique_posts_points_without_touching_files_or_generators(self):
        before = self._write_concept()
        critique = {
            "points": _POINTS[:1], "failed_lenses": ["Engagement"], "answer_md": _ANSWER,
        }
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "critique", "points": []},
        ), patch(
            "training.design_critique.run_critique", return_value=critique,
        ) as run, patch(
            "training.adaptive_builder.refine_design_concept",
        ) as refine, patch(
            "training.adaptive_builder.gen_design_concept",
        ) as gen, patch(
            "training.adaptive_builder.record_creative_direction",
        ) as record:
            self.manager.start_message("book", "concept", "What is weak across the design?")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["message"], "Critique ready")
        self.assertEqual((status.get("result") or {}).get("mode"), "critique")
        run.assert_called_once()
        refine.assert_not_called()
        gen.assert_not_called()
        record.assert_not_called()
        turns = self._turns()
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["content"], "What is weak across the design?")
        self.assertEqual(turns[1]["content"], _ANSWER)
        base = self._design_dir()
        for name, data in before.items():
            self.assertEqual((base / name).read_bytes(), data)

    def test_change_with_points_injects_saved_text_into_instruction(self):
        self._write_concept()
        self._write_points()
        refined = {
            "worldview": "# Worldview\n\nUpdated.",
            "rough_outline": "# Rough outline\n\nUpdated.",
            "stage_outline": "# Phase outline\n\n## Phase 1: Arrival\n",
            "adjustment_note": "Updated from the instruction.",
        }
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "change", "points": [2, 4]},
        ), patch(
            "training.adaptive_builder.refine_design_concept", return_value=refined,
        ) as refine, patch(
            "training.adaptive_builder.gen_design_concept",
        ) as gen:
            self.manager.start_message("book", "concept", "apply 2 and 4")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        gen.assert_not_called()
        refine.assert_called_once()
        instruction = refine.call_args.kwargs.get("instruction") or refine.call_args[1].get("instruction")
        self.assertIn("[Critique points to apply]", instruction)
        self.assertIn("Hook the opening", instruction)
        self.assertIn("The first phase explains weather.", instruction)
        self.assertIn("Raise the cost", instruction)
        self.assertEqual(self._turns()[0]["content"], "apply 2 and 4")
        self.assertNotIn("Hook the opening", self._turns()[0]["content"])

    def test_change_with_missing_point_fails_without_generator(self):
        self._write_concept()
        self._write_points()
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "change", "points": [2, 9]},
        ), patch(
            "training.adaptive_builder.refine_design_concept",
        ) as refine, patch(
            "training.adaptive_builder.gen_design_concept",
        ) as gen:
            self.manager.start_message("book", "concept", "apply 2 and 9")
            status = self._wait()
        self.assertEqual(status["status"], "failed")
        self.assertIn("9", status.get("error") or "")
        self.assertIn("critique", (status.get("error") or "").lower())
        refine.assert_not_called()
        gen.assert_not_called()

    def test_stopped_critique(self):
        self._write_concept()
        stopped = {"stopped": True, "points": [], "failed_lenses": [], "answer_md": ""}
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "critique", "points": []},
        ), patch(
            "training.design_critique.run_critique", return_value=stopped,
        ), patch(
            "training.adaptive_builder.refine_design_concept",
        ) as refine:
            self.manager.start_message("book", "concept", "Review the design.")
            status = self._wait()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual((status.get("result") or {}).get("mode"), "critique")
        refine.assert_not_called()
        self.assertEqual(self._turns()[1]["content"], "Critique stopped.")

    def test_reset_deletes_scope_points_file(self):
        self._write_concept()
        path = self._design_dir() / "critique_points_concept.json"
        path.write_text("{}", encoding="utf-8")
        self.manager.reset("book", "concept")
        self.assertFalse(path.is_file())

    def test_lens_routes_status_save_reject_and_reset(self):
        client, runtime, web_home = self._client()
        status = client.get("/api/workspaces/book/design/lenses")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["path"], "file_system/story_design/critic_lenses.md")
        self.assertIn("Depth", body["lenses"])

        uploads = web_home / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        good = uploads / "lenses.md"
        good.write_text("## Voice\nTrack distinctive narration.\n", encoding="utf-8")
        saved = client.post(
            "/api/workspaces/book/design/lenses",
            json={"upload_id": runtime.uploads.register(good)},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["exists"])
        self.assertIn("Voice", saved.json()["lenses"])

        bad = uploads / "empty.txt"
        bad.write_text("no headings here\n", encoding="utf-8")
        rejected = client.post(
            "/api/workspaces/book/design/lenses",
            json={"upload_id": runtime.uploads.register(bad)},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("##", rejected.json()["detail"])

        reset = client.delete("/api/workspaces/book/design/lenses")
        self.assertEqual(reset.status_code, 200)
        self.assertFalse(reset.json()["exists"])
        self.assertNotIn("Voice", reset.json()["lenses"])


class DesignChatCritiqueFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.index = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        cls.wizard = (root / "webui" / "static" / "wizard-v0.js").read_text(encoding="utf-8")
        cls.lenses = (root / "webui" / "static" / "design-lenses.js").read_text(encoding="utf-8")

    def test_lens_bar_toast_and_route(self):
        self.assertIn("Critic lenses", self.lenses)
        self.assertIn("Upload lenses", self.lenses)
        self.assertIn("Restore default", self.lenses)
        self.assertIn("writing-guide-bar", self.lenses)
        self.assertIn("/design/lenses", self.lenses)
        self.assertIn("Critique ready.", self.wizard)
        self.assertIn("/assets/design-lenses.js?v=1", self.index)
        wizard = self.index.find("/assets/wizard-v0.js?")
        lenses = self.index.find("/assets/design-lenses.js?")
        self.assertNotEqual(wizard, -1)
        self.assertGreater(lenses, wizard)


if __name__ == "__main__":
    unittest.main()
