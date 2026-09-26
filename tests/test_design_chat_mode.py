"""The explicit design-chat `mode` decides the path; without it the routing and keyword heuristic stay."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.design_chat import DesignChatManager

EXTENDED = {"stage_roadmap": "# Stage roadmap\n\n## Stage 1\n\n## Stage 2\n", "adjustment_note": "Appended."}
REFINED = {"stage_roadmap": "# Stage roadmap\n\n## Stage 1\n", "adjustment_note": "Refined."}


class DesignChatModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        self.root = Path(self._tmp.name)
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        self.manager = DesignChatManager(self.root)
        base = self.root / "book" / "file_system" / "story_design"
        base.mkdir(parents=True)
        files = {
            "worldview.md": "# Worldview\n\nTides.\n",
            "rough_outline.md": "# Rough outline\n\nA hero.\n",
            "stage_outline.md": "# Phase outline\n\n## Phase 1: Arrival\n",
            "long_mainline.md": "# Long mainline\n\nThe tide war.\n",
            "stage_roadmap.md": "# Stage roadmap\n\n## Stage 1: Arrival\n",
            "design_state.json": json.dumps({"stage_pipeline_version": 2}),
        }
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def run_stage(self, message, mode, route="change", points=()):
        with patch(
            "training.design_question.route_design_request", return_value={"mode": route, "points": list(points)},
        ) as router, patch(
            "training.adaptive_builder.extend_stage_design", return_value=EXTENDED,
        ) as extend, patch(
            "training.adaptive_builder.refine_stage_design", return_value=REFINED,
        ) as refine:
            response = self.manager.run_message("book", "stage", message, chat_mode=mode)
        return response, router, extend, refine

    def write_points(self):
        points = [{"number": 2, "title": "Hook the opening", "detail": "Stage 1 explains weather.", "lenses": ["Pace"]}]
        path = self.root / "book" / "file_system" / "story_design" / "critique_points_stage.json"
        path.write_text(json.dumps({"scope": "stage", "points": points}), encoding="utf-8")

    def test_explicit_extend_extends_even_when_the_router_says_question(self):
        response, router, extend, refine = self.run_stage("Add one more stage please.", "extend", route="question")
        self.assertEqual(response["mode"], "extend")
        extend.assert_called_once()
        refine.assert_not_called()
        router.assert_called_once()

    def test_explicit_chat_refines_even_when_the_text_has_an_extend_keyword(self):
        response, router, extend, refine = self.run_stage("Please continue the tension in stage 1.", "chat")
        self.assertEqual(response["mode"], "refine")
        refine.assert_called_once()
        extend.assert_not_called()
        router.assert_called_once()

    def test_explicit_chat_never_diverts_to_answer_or_critique(self):
        for route in ("question", "critique"):
            with patch("training.design_question.answer_design_question") as answer, patch(
                "training.design_critique.run_critique",
            ) as critique:
                response, _router, _extend, refine = self.run_stage("Is stage 1 slow?", "chat", route=route)
            self.assertEqual(response["mode"], "refine")
            refine.assert_called_once()
            answer.assert_not_called()
            critique.assert_not_called()

    def test_explicit_chat_injects_saved_critique_points(self):
        self.write_points()
        response, _router, _extend, refine = self.run_stage("apply 2", "chat", points=[2])
        self.assertEqual(response["mode"], "refine")
        instruction = refine.call_args.kwargs["instruction"]
        self.assertIn("[Critique points to apply]", instruction)
        self.assertIn("Stage 1 explains weather.", instruction)

    def test_explicit_extend_injects_saved_critique_points(self):
        self.write_points()
        _response, _router, extend, _refine = self.run_stage("apply 2", "extend", points=[2])
        self.assertIn("Stage 1 explains weather.", extend.call_args.kwargs["instruction"])

    def test_no_mode_keeps_routing_and_the_keyword_heuristic(self):
        response, router, extend, refine = self.run_stage("Please continue the tension in stage 1.", None)
        self.assertEqual(response["mode"], "extend")
        router.assert_called_once()
        extend.assert_called_once()
        refine.assert_not_called()

    def test_explicit_question_answers_without_routing(self):
        with patch("training.design_question.route_design_request") as router, patch(
            "training.design_question.answer_design_question", return_value="It is thin.",
        ) as answer:
            response = self.manager.run_message("book", "concept", "Change the worldview?", chat_mode="question")
        self.assertEqual(response["mode"], "answer")
        answer.assert_called_once()
        router.assert_not_called()

    def test_explicit_critique_runs_the_critics_without_routing(self):
        critique = {"answer_md": "1. Pacing is slow.", "points": [{"number": 1}], "failed_lenses": []}
        with patch("training.design_question.route_design_request") as router, patch(
            "training.design_critique.run_critique", return_value=critique,
        ) as run:
            response = self.manager.run_message("book", "stage", "Add more stages.", chat_mode="critique")
        self.assertEqual(response["mode"], "critique")
        run.assert_called_once()
        router.assert_not_called()

    def test_start_message_hands_the_mode_to_the_background_job(self):
        with patch(
            "training.design_question.route_design_request", return_value={"mode": "question", "points": []},
        ) as router, patch(
            "training.adaptive_builder.extend_stage_design", return_value=EXTENDED,
        ) as extend:
            self.manager.start_message("book", "stage", "One more.", chat_mode="extend")
            deadline = time.time() + 5
            while self.manager.job_status("book", "stage")["status"] == "running" and time.time() < deadline:
                time.sleep(0.02)
        status = self.manager.job_status("book", "stage")
        self.assertEqual(status["status"], "completed", status.get("error"))
        self.assertEqual(status["result"], {"mode": "extend"})
        extend.assert_called_once()
        router.assert_called_once()

    def test_start_message_rejects_modes_without_a_path(self):
        with self.assertRaises(ValueError):
            self.manager.start_message("book", "concept", "more", chat_mode="extend")
        with self.assertRaises(ValueError):
            self.manager.start_message("book", "stage", "more", chat_mode="banter")
        with self.assertRaises(ValueError):
            self.manager.start_message("fresh", "concept", "What is missing?", chat_mode="question")
        self.assertEqual(self.manager.job_status("book", "concept")["status"], "idle")


if __name__ == "__main__":
    unittest.main()
