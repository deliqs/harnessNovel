"""Offline tests for the arcs-step orchestrator tools."""
import json
import unittest
from unittest.mock import patch

from pydantic_ai import ModelRetry

from tests.scoped_tool_case import ScopedToolCase
from webui.orchestrator.tools.arcs import arcs_continue, arcs_generate, arcs_overview, arcs_reset


class ArcsToolTests(ScopedToolCase):
    phase = "arcs"

    def test_generate_starts_the_job_for_the_selected_volume(self):
        result = arcs_generate(self.ctx({"volume": 2}), "  Five arcs, slow burn.  ")

        self.runtime.arcs_chat.start_message.assert_called_once_with("book", 2, "Five arcs, slow burn.")
        data = self.assert_started(result, "/api/workspaces/book/arcs/2/job")
        self.assertEqual(data["job"]["kind"], "arcs")

    def test_an_explicit_volume_wins_over_the_ui_selection(self):
        arcs_generate(self.ctx({"volume": 2}), "Go", volume=3)
        self.runtime.arcs_chat.start_message.assert_called_once_with("book", 3, "Go")

    def test_without_a_volume_or_message_the_model_is_asked_to_retry(self):
        for call in (
            lambda: arcs_generate(self.ctx(), "Go"),
            lambda: arcs_generate(self.ctx({"volume": 1}), "   "),
            lambda: arcs_continue(self.ctx()),
            lambda: arcs_reset(self.ctx({"arc": 1})),
            lambda: arcs_overview(self.ctx()),
            lambda: arcs_overview(self.ctx({"volume": 0})),
        ):
            with self.assertRaises(ModelRetry):
                call()
        self.runtime.arcs_chat.start_message.assert_not_called()

    def test_a_running_job_is_refused_not_raised(self):
        self.runtime.arcs_chat.start_message.side_effect = ValueError("This stage already has a generation task running.")
        message = self.assert_status(arcs_generate(self.ctx({"volume": 1}), "Go"), "refused")
        self.assertIn("already has a generation task", message)

    def test_continue_starts_the_resumed_job(self):
        result = arcs_continue(self.ctx({"volume": 1}))
        self.runtime.arcs_chat.continue_incomplete.assert_called_once_with("book", 1)
        self.assert_started(result, "/api/workspaces/book/arcs/1/job")

    def test_continue_without_unfinished_arcs_is_refused(self):
        self.runtime.arcs_chat.continue_incomplete.side_effect = ValueError("no unfinished story arcs")
        self.assert_status(arcs_continue(self.ctx({"volume": 1})), "refused")

    def test_reset_clears_the_volume(self):
        self.runtime.arcs_chat.reset.return_value = {"reset": True, "conversation": {"turns": []}}
        message = self.assert_status(arcs_reset(self.ctx({"volume": 4})), "done")
        self.runtime.arcs_chat.reset.assert_called_once_with("book", 4)
        self.assertIn("volume 4", message)

    def test_reset_while_generating_is_refused(self):
        self.runtime.arcs_chat.reset.side_effect = ValueError("This stage is still generating.")
        self.assertIn("still generating", self.assert_status(arcs_reset(self.ctx({"volume": 1})), "refused"))

    def test_overview_lists_arc_paths_and_resume_state(self):
        self.write_arc(1, 1, 1, 10)
        self.write_arc(1, 2, 11, 20)
        resume = {"can_resume": True, "completed": 2, "total": 4, "next_arc": 3}
        with patch("training.adaptive_builder.story_arc_resume_status", return_value=resume):
            message = self.assert_status(arcs_overview(self.ctx({"volume": 1})), "done")

        self.runtime.arcs_chat.job_status.assert_called_once_with("book", 1)
        self.assertIn("2 arcs", message)
        self.assertIn("file_system/story_arcs/vol_01/arc_001_ch001_010.md", message)
        self.assertIn("chapters 11-20", message)
        self.assertIn("can be continued", message)
        self.assertNotIn("Plot.", message)

    def test_overview_of_an_empty_volume(self):
        message = self.assert_status(arcs_overview(self.ctx({"volume": 3})), "done")
        self.assertIn("no arcs", message)

    def test_results_are_capped(self):
        for idx in range(1, 200):
            self.write_arc(1, idx, idx, idx)
        self.assertLessEqual(len(arcs_overview(self.ctx({"volume": 1}))), 2200)
        self.runtime.arcs_chat.reset.side_effect = ValueError("x" * 10000)
        self.assertLessEqual(len(arcs_reset(self.ctx({"volume": 1}))), 700)

    def test_job_and_destructive_tools_need_approval(self):
        approvals = self.approvals()
        self.assertTrue(approvals["arcs_generate"])
        self.assertTrue(approvals["arcs_continue"])
        self.assertTrue(approvals["arcs_reset"])
        self.assertFalse(approvals["arcs_overview"])
        self.assertFalse(any(name.endswith(("_pause", "_resume")) for name in approvals))

    def test_results_are_json(self):
        self.assertIsInstance(json.loads(arcs_overview(self.ctx({"volume": 1}))), dict)


if __name__ == "__main__":
    unittest.main()
