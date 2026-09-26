"""Offline tests for the draft-step orchestrator tools."""
import json
import unittest

from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from tests.scoped_tool_case import ScopedToolCase
from webui.orchestrator.registry import build_agent
from webui.orchestrator.tools.draft import (
    draft_continue,
    draft_generate,
    draft_overview,
    draft_reset,
    set_finalized_chapters,
    writing_guide_status,
)


class DraftToolTests(ScopedToolCase):
    phase = "draft"

    def setUp(self):
        super().setUp()
        for volume in (1, 2, 3):
            for arc in range(1, 5):
                self.write_arc(volume, arc, arc * 10 - 9, arc * 10)

    def _clear_arcs(self, volume):
        for path in (self.fs / "story_arcs" / f"vol_{volume:02d}").iterdir():
            path.unlink()

    def test_generate_starts_the_job_and_humanizes_by_default(self):
        result = draft_generate(self.ctx({"volume": 1, "arc": 2}), "Write the arc.")

        self.runtime.draft_chat.start_message.assert_called_once_with("book", 1, 2, "Write the arc.", humanize=True)
        data = self.assert_started(result, "/api/workspaces/book/drafts/1/2/job")
        self.assertEqual(data["job"]["kind"], "drafts")

    def test_generate_can_skip_humanizing_and_override_the_selection(self):
        draft_generate(self.ctx({"volume": 1, "arc": 2}), "Go", volume=3, arc=4, humanize=False)
        self.runtime.draft_chat.start_message.assert_called_once_with("book", 3, 4, "Go", humanize=False)

    def test_missing_volume_arc_or_message_asks_the_model_to_retry(self):
        for call in (
            lambda: draft_generate(self.ctx({"volume": 1}), "Go"),
            lambda: draft_generate(self.ctx({"volume": 1, "arc": 1}), " "),
            lambda: draft_continue(self.ctx({"arc": 1})),
            lambda: draft_reset(self.ctx()),
            lambda: draft_overview(self.ctx({"volume": 1, "arc": "two"})),
        ):
            with self.assertRaises(ModelRetry):
                call()
        self.runtime.draft_chat.start_message.assert_not_called()

    def test_a_running_job_is_refused_not_raised(self):
        self.runtime.draft_chat.start_message.side_effect = ValueError("This story arc already has a draft task running.")
        message = self.assert_status(draft_generate(self.ctx({"volume": 1, "arc": 1}), "Go"), "refused")
        self.assertIn("already has a draft task", message)

    def test_a_missing_arc_is_refused_before_any_job_starts(self):
        for call in (
            lambda: draft_generate(self.ctx({"volume": 1, "arc": 9}), "Go"),
            lambda: draft_continue(self.ctx({"volume": 5, "arc": 1})),
        ):
            self.assertIn("Arcs on disk", self.assert_status(call(), "refused"))
        self.runtime.draft_chat.start_message.assert_not_called()
        self.runtime.draft_chat.continue_incomplete.assert_not_called()

    def test_unexpected_errors_are_refused_not_raised(self):
        self.runtime.draft_chat.continue_incomplete.side_effect = KeyError("volume")
        message = self.assert_status(draft_continue(self.ctx({"volume": 1, "arc": 1})), "refused")
        self.assertIn("KeyError", message)

    def test_continue_and_its_guard(self):
        result = draft_continue(self.ctx({"volume": 2, "arc": 1}))
        self.runtime.draft_chat.continue_incomplete.assert_called_once_with("book", 2, 1)
        self.assert_started(result, "/api/workspaces/book/drafts/2/1/job")
        self.runtime.draft_chat.continue_incomplete.side_effect = ValueError("no unfinished draft")
        self.assert_status(draft_continue(self.ctx({"volume": 2, "arc": 1})), "refused")

    def test_reset_reports_what_was_deleted(self):
        self.runtime.draft_chat.reset.return_value = {"reset": True, "deleted": 3, "start_chapter": 1, "end_chapter": 5}
        message = self.assert_status(draft_reset(self.ctx({"volume": 1, "arc": 1})), "done")
        self.runtime.draft_chat.reset.assert_called_once_with("book", 1, 1)
        self.assertIn("3 files", message)
        self.assertIn("chapters 1-5", message)

    def test_reset_guard_is_refused(self):
        self.runtime.draft_chat.reset.side_effect = ValueError("Current story arc not found.")
        self.assert_status(draft_reset(self.ctx({"volume": 1, "arc": 9})), "refused")

    def test_overview_lists_draft_paths_and_final_chapters(self):
        self._clear_arcs(1)
        self.write_arc(1, 1, 1, 3)
        self.write("chapters/vol_01/001_chapter_1.md", "Chapter one text.")
        self.write("chapters/vol_01/002_chapter_2.md", "Chapter two text.")
        self.write("finalized_chapters.json", '{"version": 2, "drafts": {"vol_01": {"1": {"finalized": true}}}}')

        message = self.assert_status(draft_overview(self.ctx({"volume": 1, "arc": 1})), "done")

        self.runtime.draft_chat.job_status.assert_called_once_with("book", 1, 1)
        self.assertIn("file_system/chapters/vol_01/002_chapter_2.md", message)
        self.assertIn("Final: 1.", message)
        self.assertIn("2 of 3 chapter drafts done; an incomplete run can be continued", message)
        self.assertNotIn("Chapter one text.", message)

    def test_overview_of_an_unknown_arc_is_refused(self):
        self.assert_status(draft_overview(self.ctx({"volume": 4, "arc": 1})), "refused")

    def test_writing_guide_status(self):
        self.runtime.draft_chat.writing_guide_status.return_value = {"custom": True, "name": "Custom writing guide"}
        message = self.assert_status(writing_guide_status(self.ctx()), "done")
        self.runtime.draft_chat.writing_guide_status.assert_called_once_with("book")
        self.assertIn("Custom writing guide", message)
        self.assertIn("file_system/writing/system_prompt.md", message)

    def test_results_are_capped(self):
        self._clear_arcs(1)
        self.write_arc(1, 1, 1, 400)
        for chapter in range(1, 401):
            self.write(f"chapters/vol_01/{chapter:03d}_chapter_{chapter}.md", "Text.")
        self.assertLessEqual(len(draft_overview(self.ctx({"volume": 1, "arc": 1}))), 2200)
        self.runtime.draft_chat.start_message.side_effect = ValueError("x" * 10000)
        self.assertLessEqual(len(draft_generate(self.ctx({"volume": 1, "arc": 1}), "Go")), 700)

    def test_set_finalized_chapters_marks_drafts_final(self):
        self.write("chapters/vol_01/002_chapter_2.md", "Draft two.")
        message = self.assert_status(set_finalized_chapters(self.ctx({"volume": 1}), [2], True), "done")
        self.assertIn("Final drafts now: 2.", message)
        stored = json.loads((self.fs / "finalized_chapters.json").read_text(encoding="utf-8"))
        self.assertIn("2", stored["drafts"]["vol_01"])
        self.assert_status(set_finalized_chapters(self.ctx({"volume": 1}), [2], False), "done")
        stored = json.loads((self.fs / "finalized_chapters.json").read_text(encoding="utf-8"))
        self.assertNotIn("vol_01", stored["drafts"])

    def test_set_finalized_chapters_refuses_a_missing_draft(self):
        message = self.assert_status(set_finalized_chapters(self.ctx({"volume": 1}), [7], True), "refused")
        self.assertIn("chapter 7", message)

    def test_set_finalized_chapters_needs_a_volume_and_chapters(self):
        with self.assertRaises(ModelRetry):
            set_finalized_chapters(self.ctx(), [1], True)
        with self.assertRaises(ModelRetry):
            set_finalized_chapters(self.ctx({"volume": 1}), [], True)

    def test_the_humanize_off_tag_is_explained_to_the_model(self):
        from webui.orchestrator.registry import instructions_for
        from webui.orchestrator.tools.draft import INSTRUCTIONS
        self.assertIn("`[humanize: off]` tag", INSTRUCTIONS)
        self.assertIn("humanize=false to draft_generate", instructions_for("draft"))
        description = build_agent("draft", TestModel())._function_toolset.tools["draft_generate"]
        schema = description.tool_def.parameters_json_schema["properties"]["humanize"]
        self.assertIn("[humanize: off]", schema["description"])

    def test_approvals(self):
        approvals = self.approvals()
        for name in ("draft_generate", "draft_continue", "draft_reset", "set_finalized_chapters"):
            self.assertTrue(approvals[name], name)
        for name in ("draft_overview", "writing_guide_status"):
            self.assertFalse(approvals[name], name)
        self.assertFalse(any("writing_guide" in name and name != "writing_guide_status" for name in approvals))


if __name__ == "__main__":
    unittest.main()
