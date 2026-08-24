"""Web UI heading parsers: Arc titles and Phase vs Stage."""
import inspect
import re
import unittest

from webui.design_chat import PHASE_HEADING_RE, _design_files_exist, _stage_resume_status
from webui.task_runner import PHASE_HEADING_RE as TASK_PHASE_HEADING_RE
from webui.task_runner import story_arc_title


class TestStoryArcTitle(unittest.TestCase):
    def test_english_arc_heading_does_not_require_qingjie(self):
        self.assertEqual(
            story_arc_title("【Arc1: Chapters 1-5 | The Hook】"),
            "The Hook",
        )

    def test_chinese_qingjie_heading_still_extracts_title(self):
        self.assertEqual(
            story_arc_title("【情节1：第1-5章｜钩子】"),
            "钩子",
        )

    def test_story_arc_title_accepts_arc_alias(self):
        source = inspect.getsource(story_arc_title)
        self.assertIn("Arc", source)


class TestDesignChatPhaseRegex(unittest.TestCase):
    def test_phase_pattern_is_pasted_into_design_chat(self):
        expected = r"^#{1,6}\s*(?:第\s*)?(?:阶段|phase)\s*0*(\d+)\b"
        self.assertEqual(PHASE_HEADING_RE.pattern, expected)
        self.assertTrue(PHASE_HEADING_RE.flags & re.IGNORECASE)
        self.assertTrue(PHASE_HEADING_RE.flags & re.MULTILINE)
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(_design_files_exist))
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(_stage_resume_status))

    def test_phase_regex_matches_phase_not_stage(self):
        self.assertEqual(PHASE_HEADING_RE.findall("## Phase 1: Name"), ["1"])
        self.assertEqual(PHASE_HEADING_RE.findall("## 阶段1：名称"), ["1"])
        self.assertIsNone(PHASE_HEADING_RE.search("# Stage 1: Name"))


class TestTaskRunnerStageOutlineCount(unittest.TestCase):
    def test_stage_outline_count_pattern_matches_phase_headings(self):
        from webui.task_runner import WorkspaceStore

        expected = r"^#{1,6}\s*(?:第\s*)?(?:阶段|phase)\s*0*(\d+)\b"
        self.assertEqual(TASK_PHASE_HEADING_RE.pattern, expected)
        self.assertTrue(TASK_PHASE_HEADING_RE.flags & re.IGNORECASE)
        self.assertTrue(TASK_PHASE_HEADING_RE.flags & re.MULTILINE)
        self.assertIn("PHASE_HEADING_RE", inspect.getsource(WorkspaceStore.summary))
        self.assertEqual(TASK_PHASE_HEADING_RE.findall("## Phase 1: Name"), ["1"])
        self.assertEqual(TASK_PHASE_HEADING_RE.findall("## 阶段1：名称"), ["1"])
        self.assertIsNone(TASK_PHASE_HEADING_RE.search("# Stage 1: Name"))


if __name__ == "__main__":
    unittest.main()
