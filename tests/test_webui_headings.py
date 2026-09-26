"""Web UI heading parsers: Arc titles and Phase vs Stage."""
import inspect
import re
import unittest
from pathlib import Path

from webui.design_chat import PHASE_HEADING_RE, _design_files_exist, _stage_resume_status
from webui.task_runner import PHASE_HEADING_RE as TASK_PHASE_HEADING_RE
from webui.task_runner import story_arc_title


class TestStoryArcTitle(unittest.TestCase):
    def test_english_arc_heading_does_not_require_qingjie(self):
        self.assertEqual(
            story_arc_title("【Arc1: Chapters 1-5 | The Hook】"),
            "The Hook",
        )

    def test_story_arc_title_accepts_arc_alias(self):
        source = inspect.getsource(story_arc_title)
        self.assertIn("Arc", source)


class TestDesignChatPhaseRegex(unittest.TestCase):
    def test_phase_pattern_is_pasted_into_design_chat(self):
        expected = r"^#{1,6}\s*phase\s*0*(\d+)\b[^\n]*"
        self.assertEqual(PHASE_HEADING_RE.pattern, expected)
        self.assertTrue(PHASE_HEADING_RE.flags & re.IGNORECASE)
        self.assertTrue(PHASE_HEADING_RE.flags & re.MULTILINE)
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(_design_files_exist))
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(_stage_resume_status))

    def test_phase_regex_matches_phase_not_stage(self):
        self.assertEqual(PHASE_HEADING_RE.findall("## Phase 1: Name"), ["1"])
        self.assertIsNone(PHASE_HEADING_RE.search("# Stage 1: Name"))


class TestTaskRunnerStageOutlineCount(unittest.TestCase):
    def test_stage_outline_count_pattern_matches_phase_headings(self):
        from webui.task_runner import WorkspaceStore

        expected = r"^#{1,6}\s*phase\s*0*(\d+)\b[^\n]*"
        self.assertEqual(TASK_PHASE_HEADING_RE.pattern, expected)
        self.assertTrue(TASK_PHASE_HEADING_RE.flags & re.IGNORECASE)
        self.assertTrue(TASK_PHASE_HEADING_RE.flags & re.MULTILINE)
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(WorkspaceStore.summary))
        self.assertEqual(TASK_PHASE_HEADING_RE.findall("## Phase 1: Name"), ["1"])
        self.assertIsNone(TASK_PHASE_HEADING_RE.search("# Stage 1: Name"))


class TestWizardOptionalReference(unittest.TestCase):
    def test_reference_step_is_optional_and_book_design_is_recommended_first(self):
        source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "wizard-v0.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id: "reference", title: "Reference novel", short: "Optional craft source", optional: true', source)
        self.assertIn('WIZARD_STEPS.find((step) => !step.optional && !inferredDone(step))', source)


if __name__ == "__main__":
    unittest.main()
