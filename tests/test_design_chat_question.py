import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.workspace import init_workspace
from training.design_question import (
    design_context,
    route_design_message,
    route_design_request,
)
from webui.design_chat import DesignChatManager


class DesignChatQuestionTests(unittest.TestCase):
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

    def test_question_answers_without_touching_files_or_generators(self):
        before = self._write_concept()
        answer = "The worldview is thin on factions."
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "question", "points": []},
        ), patch(
            "training.design_question.answer_design_question", return_value=answer,
        ), patch(
            "training.adaptive_builder.refine_design_concept",
        ) as refine, patch(
            "training.adaptive_builder.gen_design_concept",
        ) as gen:
            self.manager.start_message("book", "concept", "What is missing from the worldview?")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["message"], "Question answered")
        self.assertEqual((status.get("result") or {}).get("mode"), "answer")
        refine.assert_not_called()
        gen.assert_not_called()

        conv_path = self.root / "book" / "file_system" / "story_design" / "conversation" / "concept.json"
        turns = json.loads(conv_path.read_text(encoding="utf-8"))["turns"]
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["content"], "What is missing from the worldview?")
        self.assertEqual(turns[1]["content"], answer)

        base = self._design_dir()
        for name, data in before.items():
            self.assertEqual((base / name).read_bytes(), data)

    def test_change_calls_the_existing_generator(self):
        self._write_concept()
        refined = {
            "worldview": "# Worldview\n\nUpdated.",
            "rough_outline": "# Rough outline\n\nUpdated.",
            "stage_outline": "# Phase outline\n\n## Phase 1: Arrival\n",
            "adjustment_note": "Updated from the instruction.",
        }
        with patch(
            "training.design_question.route_design_request",
            return_value={"mode": "change", "points": []},
        ) as router, patch(
            "training.adaptive_builder.refine_design_concept", return_value=refined,
        ) as refine, patch(
            "training.adaptive_builder.gen_design_concept",
        ) as gen:
            self.manager.start_message("book", "concept", "Add a rival faction to the worldview.")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        self.assertEqual((status.get("result") or {}).get("mode"), "refine")
        router.assert_called_once()
        refine.assert_called_once()
        gen.assert_not_called()

    def test_routing_skipped_when_no_design_files(self):
        generated = {
            "worldview": "# Worldview\n",
            "rough_outline": "# Rough outline\n",
            "stage_outline": "# Phase outline\n",
            "adjustment_note": "First draft generated.",
        }
        with patch(
            "training.design_question.route_design_request",
        ) as router, patch(
            "training.adaptive_builder.gen_design_concept", return_value=generated,
        ) as gen:
            self.manager.start_message("book", "concept", "a story about tides")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        gen.assert_called_once()
        router.assert_not_called()

    def test_routing_skipped_when_use_new_reference(self):
        self._write_concept()
        synced = {
            "stage_outline": "# Phase outline\n\n## Phase 1: Arrival\n",
            "adjustment_note": "Synced the last phase.",
        }
        with patch(
            "training.design_question.route_design_request",
        ) as router, patch(
            "training.adaptive_builder.sync_stage_outline_from_new_reference",
            return_value=synced,
        ) as sync, patch(
            "training.adaptive_builder.refine_design_concept",
        ) as refine:
            self.manager.start_message(
                "book", "concept", "sync this", use_new_reference=True,
            )
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        router.assert_not_called()
        sync.assert_called_once()
        refine.assert_not_called()

    def test_routing_prompt_omits_design_file_text(self):
        self._write_concept()
        ws = init_workspace("book")
        with patch("training.design_question._get_llm", return_value=object()), patch(
            "training.design_question._call_design_llm",
            return_value='{"mode": "question"}',
        ) as call_llm:
            route_design_message(ws, "concept", "What is missing from the worldview?")
        prompt = call_llm.call_args[0][1]
        self.assertNotIn("Tides and moons.", prompt)
        self.assertNotIn("A tide-born hero.", prompt)
        self.assertNotIn("## Phase 1: Arrival", prompt)

    def test_router_junk_value_is_treated_as_change(self):
        self._write_concept()
        ws = init_workspace("book")
        with patch("training.design_question._get_llm", return_value=object()), patch(
            "training.design_question._call_design_llm",
            return_value='{"mode": "maybe"}',
        ):
            self.assertEqual(
                route_design_message(ws, "concept", "Could the ending be stronger?"),
                "change",
            )

    def _route(self, raw, message="apply 2 and 4"):
        ws = init_workspace("book")
        with patch("training.design_question._get_llm", return_value=object()), patch(
            "training.design_question._call_design_llm",
            return_value=raw,
        ):
            request = route_design_request(ws, "concept", message)
            mode = route_design_message(ws, "concept", message)
        return request, mode

    def test_router_returns_question_critique_and_change(self):
        question, question_mode = self._route(
            '{"mode": "question", "points": []}',
            "How many moons does the worldview name?",
        )
        self.assertEqual(question, {"mode": "question", "points": []})
        self.assertEqual(question_mode, "question")

        critique, critique_mode = self._route(
            '{"mode": "critique", "points": [2]}',
            "What is weak across the design?",
        )
        self.assertEqual(critique, {"mode": "critique", "points": []})
        self.assertEqual(critique_mode, "critique")

        change, change_mode = self._route(
            '{"mode": "change", "points": [2, 4]}',
            "apply 2 and 4",
        )
        self.assertEqual(change, {"mode": "change", "points": [2, 4]})
        self.assertEqual(change_mode, "change")

    def test_router_point_parsing_junk_negatives_and_duplicates(self):
        request, mode = self._route(
            '{"mode": "change", "points": [2, "nope", -1, 0, 2, 4, 3.5, true, null, 1]}',
        )
        self.assertEqual(mode, "change")
        self.assertEqual(request["points"], [2, 4, 1])

    def test_router_points_ignored_unless_mode_is_change(self):
        question, _mode = self._route(
            '{"mode": "question", "points": [2, 4]}',
            "What is the opening hook?",
        )
        self.assertEqual(question["points"], [])
        critique, _mode = self._route(
            '{"mode": "critique", "points": [3]}',
            "Critique the design.",
        )
        self.assertEqual(critique["points"], [])


    def test_design_context_stage_includes_mainline_and_roadmap_when_present(self):
        base = self._design_dir()
        (base / "worldview.md").write_text("WV-BODY", encoding="utf-8")
        (base / "rough_outline.md").write_text("RO-BODY", encoding="utf-8")
        (base / "stage_outline.md").write_text("SO-BODY", encoding="utf-8")
        (base / "long_mainline.md").write_text("LM-BODY", encoding="utf-8")
        (base / "stage_roadmap.md").write_text("SR-BODY", encoding="utf-8")
        ws = init_workspace("book")
        text = design_context(ws, "stage")
        self.assertIn("Long mainline", text)
        self.assertIn("Stage roadmap", text)
        self.assertIn("LM-BODY", text)
        self.assertIn("SR-BODY", text)
        self.assertIn("WV-BODY", text)
        (base / "stage_roadmap.md").unlink()
        text = design_context(ws, "stage")
        self.assertIn("Long mainline", text)
        self.assertIn("LM-BODY", text)
        self.assertNotIn("Stage roadmap", text)
        self.assertNotIn("SR-BODY", text)


if __name__ == "__main__":
    unittest.main()
