"""Offline tests for the chapters-step orchestrator tools."""
import threading
import unittest

from pydantic_ai import ModelRetry

from tests.scoped_tool_case import ScopedToolCase
from webui.orchestrator.tools.chapters import (
    chapters_continue,
    chapters_generate,
    chapters_overview,
    chapters_reset,
    set_system_panel_mode,
    system_panel_status,
)


class ChapterJobToolTests(ScopedToolCase):
    phase = "chapters"

    def setUp(self):
        super().setUp()
        for volume in (1, 2):
            for arc in range(1, 6):
                self.write_arc(volume, arc, arc * 10 - 9, arc * 10)

    def test_generate_starts_the_job_for_the_selected_arc(self):
        result = chapters_generate(self.ctx({"volume": 1, "arc": 3}), " Tighter pacing. ")

        self.runtime.chapters_chat.start_message.assert_called_once_with("book", 1, 3, "Tighter pacing.")
        data = self.assert_started(result, "/api/workspaces/book/chapters/1/3/job")
        self.assertEqual(data["job"]["kind"], "chapters")

    def test_explicit_volume_and_arc_win_over_the_ui_selection(self):
        chapters_generate(self.ctx({"volume": 1, "arc": 3}), "Go", volume=2, arc=5)
        self.runtime.chapters_chat.start_message.assert_called_once_with("book", 2, 5, "Go")

    def test_missing_volume_arc_or_message_asks_the_model_to_retry(self):
        for call in (
            lambda: chapters_generate(self.ctx({"volume": 1}), "Go"),
            lambda: chapters_generate(self.ctx({"arc": 1}), "Go"),
            lambda: chapters_generate(self.ctx({"volume": 1, "arc": 1}), ""),
            lambda: chapters_continue(self.ctx({"volume": 1})),
            lambda: chapters_reset(self.ctx()),
            lambda: chapters_overview(self.ctx({"arc": 2})),
        ):
            with self.assertRaises(ModelRetry):
                call()
        self.runtime.chapters_chat.start_message.assert_not_called()

    def test_a_running_job_is_refused_not_raised(self):
        self.runtime.chapters_chat.start_message.side_effect = ValueError("already has a chapter-outline task running")
        self.assert_status(chapters_generate(self.ctx({"volume": 1, "arc": 1}), "Go"), "refused")

    def test_continue_and_its_guard(self):
        result = chapters_continue(self.ctx({"volume": 1, "arc": 2}))
        self.runtime.chapters_chat.continue_incomplete.assert_called_once_with("book", 1, 2)
        self.assert_started(result, "/api/workspaces/book/chapters/1/2/job")
        self.runtime.chapters_chat.continue_incomplete.side_effect = ValueError("no unfinished chapter outlines")
        self.assert_status(chapters_continue(self.ctx({"volume": 1, "arc": 2})), "refused")

    def test_reset_and_its_guard(self):
        message = self.assert_status(chapters_reset(self.ctx({"volume": 1, "arc": 2})), "done")
        self.runtime.chapters_chat.reset.assert_called_once_with("book", 1, 2)
        self.assertIn("arc 2", message)
        self.runtime.chapters_chat.reset.side_effect = ValueError("still generating")
        self.assert_status(chapters_reset(self.ctx({"volume": 1, "arc": 2})), "refused")

    def test_a_missing_arc_is_refused_before_any_job_starts(self):
        for call in (
            lambda: chapters_generate(self.ctx({"volume": 1, "arc": 9}), "Go"),
            lambda: chapters_continue(self.ctx({"volume": 3, "arc": 1})),
        ):
            self.assertIn("Arcs on disk", self.assert_status(call(), "refused"))
        self.runtime.chapters_chat.start_message.assert_not_called()
        self.runtime.chapters_chat.continue_incomplete.assert_not_called()

    def test_unexpected_errors_are_refused_and_redacted(self):
        self.runtime.chapters_chat.start_message.side_effect = RuntimeError("boom api_key=sk-secretsecretsecret1234")
        message = self.assert_status(chapters_generate(self.ctx({"volume": 1, "arc": 1}), "Go"), "refused")
        self.assertIn("RuntimeError: boom", message)
        self.assertNotIn("sk-secretsecretsecret1234", message)
        self.runtime.chapters_chat.reset.side_effect = OSError("disk full")
        self.assertIn("disk full", self.assert_status(chapters_reset(self.ctx({"volume": 1, "arc": 1})), "refused"))

    def test_overview_lists_outline_paths_of_the_arc(self):
        self._clear_arcs(1)
        self.write_arc(1, 1, 1, 3)
        self.write_arc(1, 2, 4, 6)
        self.write("chapter_outlines/vol_01/chapter_004.md", "Outline four.")
        self.write("chapter_outlines/vol_01/chapter_001.md", "Outline one.")

        message = self.assert_status(chapters_overview(self.ctx({"volume": 1, "arc": 2})), "done")

        self.runtime.chapters_chat.job_status.assert_called_once_with("book", 1, 2)
        self.assertIn("chapters 4-6", message)
        self.assertIn("file_system/chapter_outlines/vol_01/chapter_004.md", message)
        self.assertNotIn("chapter_001.md", message)
        self.assertNotIn("Outline four.", message)

    def test_overview_of_an_unknown_arc_is_refused(self):
        message = self.assert_status(chapters_overview(self.ctx({"volume": 1, "arc": 9})), "refused")
        self.assertIn("Arcs on disk: 1, 2, 3, 4, 5", message)

    def _clear_arcs(self, volume):
        for path in (self.fs / "story_arcs" / f"vol_{volume:02d}").iterdir():
            path.unlink()

    def test_results_are_capped(self):
        self._clear_arcs(1)
        self.write_arc(1, 1, 1, 400)
        for chapter in range(1, 401):
            self.write(f"chapter_outlines/vol_01/chapter_{chapter:03d}.md", "Outline.")
        self.assertLessEqual(len(chapters_overview(self.ctx({"volume": 1, "arc": 1}))), 2200)
        self.runtime.chapters_chat.start_message.side_effect = ValueError("x" * 10000)
        self.assertLessEqual(len(chapters_generate(self.ctx({"volume": 1, "arc": 1}), "Go")), 700)


class ChapterSettingToolTests(ScopedToolCase):
    phase = "chapters"

    def test_system_panel_status_starts_on_auto(self):
        message = self.assert_status(system_panel_status(self.ctx()), "done")
        self.assertIn("auto", message)

    def test_set_system_panel_mode_writes_the_mode(self):
        message = self.assert_status(set_system_panel_mode(self.ctx(), "enabled"), "done")
        self.assertIn("enabled", message)
        self.assertIn('"selection_mode": "enabled"', self.assert_status(system_panel_status(self.ctx()), "done"))

    def test_set_system_panel_mode_refuses_unknown_modes(self):
        self.assert_status(set_system_panel_mode(self.ctx(), "sometimes"), "refused")

    def test_set_system_panel_mode_is_refused_while_a_chapter_job_runs(self):
        self.runtime.chapters_chat._jobs_lock = threading.Lock()
        self.runtime.chapters_chat._jobs = {("other", 1, 1): {"status": "running"}, ("book", 1, 2): {"status": "done"}}
        self.assert_status(set_system_panel_mode(self.ctx(), "enabled"), "done")
        self.runtime.chapters_chat._jobs[("book", 2, 1)] = {"status": "paused"}
        message = self.assert_status(set_system_panel_mode(self.ctx(), "disabled"), "refused")
        self.assertIn("chapter-outline job is running", message)
        self.assertIn('"selection_mode": "enabled"', self.assert_status(system_panel_status(self.ctx()), "done"))

    def test_approvals(self):
        approvals = self.approvals()
        self.assertNotIn("set_finalized_chapters", approvals)
        for name in ("chapters_generate", "chapters_continue", "chapters_reset"):
            self.assertTrue(approvals[name], name)
        for name in ("chapters_overview", "system_panel_status", "set_system_panel_mode"):
            self.assertFalse(approvals[name], name)
        self.assertLessEqual(len(approvals), 10)


if __name__ == "__main__":
    unittest.main()
