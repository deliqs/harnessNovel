import os
import tempfile
import unittest
from unittest.mock import patch

from core.workspace import NovelWorkspace
from training.adaptive_builder import (
    STORY_ARC_TARGET_CHARS_MAX,
    _plan_story_arcs_from_reference,
    _reference_story_arc_average_chars,
    _reference_volume_story_arcs_summary,
    _simple_story_arc_context,
    _story_arc_prompt_context,
    gen_story_arcs,
)
from training.outline_builder import _join_prompt_parts
from training.reference_finder import list_reference_story_arcs


class StoryArcContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self._tmp.name
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def _plant_reference_arcs(self):
        arc_dir = os.path.join(self.ws.reference_outlines, "vol_01_test", "story_arcs")
        os.makedirs(arc_dir, exist_ok=True)
        bodies = [
            (1, 5, "SENTINEL_ARC_ONE " + ("x" * 4000)),
            (6, 10, "SENTINEL_ARC_TWO " + ("y" * 4000)),
            (11, 15, "SENTINEL_ARC_THREE " + ("z" * 4000)),
        ]
        for idx, (start, end, body) in enumerate(bodies, 1):
            path = os.path.join(
                arc_dir, f"arc_{idx:03d}_ch{start:03d}_{end:03d}.md",
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")

    def _write_stage(self, chapters=15):
        design = os.path.join(self.ws.file_system, "story_design")
        os.makedirs(design, exist_ok=True)
        with open(os.path.join(design, "long_mainline.md"), "w", encoding="utf-8") as handle:
            handle.write("long mainline\n")
        with open(os.path.join(design, "stage_roadmap.md"), "w", encoding="utf-8") as handle:
            handle.write(f"# Stage 1: Test\nPlanned chapters: {chapters}\n")

    def test_volume_summary_is_index_without_bodies(self):
        self._plant_reference_arcs()
        text = _reference_volume_story_arcs_summary(self.ws, 1)
        self.assertIn("1", text)
        self.assertIn("5", text)
        self.assertNotIn("SENTINEL_ARC_ONE", text)
        self.assertNotIn("SENTINEL_ARC_TWO", text)
        self.assertNotIn("SENTINEL_ARC_THREE", text)

    def test_unit_prompt_uses_matching_reference_only(self):
        self._plant_reference_arcs()
        self._write_stage()
        arcs = list_reference_story_arcs(self.ws.reference_outlines, 1)
        plans = _plan_story_arcs_from_reference(arcs, 15)
        shared = _simple_story_arc_context(self.ws, 1)
        self.assertNotIn("SENTINEL_ARC_TWO", shared["reference_story_arcs"])
        ctx = _story_arc_prompt_context(shared, plans[1])
        self.assertIn("SENTINEL_ARC_TWO", ctx["reference_story_arcs"])
        self.assertNotIn("SENTINEL_ARC_ONE", ctx["reference_story_arcs"])
        self.assertNotIn("SENTINEL_ARC_THREE", ctx["reference_story_arcs"])

    def test_average_chars_is_capped(self):
        self._plant_reference_arcs()
        self.assertEqual(
            _reference_story_arc_average_chars(self.ws, 1),
            STORY_ARC_TARGET_CHARS_MAX,
        )

    def test_empty_generation_does_not_write_arc_file(self):
        self._plant_reference_arcs()
        self._write_stage()

        class FakeLLM:
            def generate(self, *args, **kwargs):
                return ""

        with patch("training.adaptive_builder._get_lite_llm", return_value=FakeLLM()):
            gen_story_arcs(self.ws, volume=1, force=True)
        arc_dir = os.path.join(self.ws.file_system, "story_arcs", "vol_01")
        written = []
        if os.path.isdir(arc_dir):
            written = [name for name in os.listdir(arc_dir) if name.startswith("arc_")]
        self.assertEqual(written, [])


class PromptJoinTests(unittest.TestCase):
    def test_join_prompt_parts_caps_length(self):
        joined = _join_prompt_parts(["a" * 20000, "b" * 20000], max_chars=26000)
        uncapped = 40000
        self.assertLess(len(joined), uncapped)
        self.assertLessEqual(len(joined), 26000)
        self.assertTrue(joined.startswith("a"))
        self.assertTrue(joined.endswith("b"))
        self.assertIn("content truncated", joined)

    def test_join_prompt_parts_retains_early_middle_and_late_obligations(self):
        joined = _join_prompt_parts([
            "EARLY_REQUIRED\n" + ("a" * 12000) + "\nEARLY_TAIL",
            "MIDDLE_REQUIRED\n" + ("b" * 12000) + "\nMIDDLE_TAIL",
            "LATE_REQUIRED\n" + ("c" * 12000) + "\nLATE_TAIL",
        ], max_chars=12000)
        self.assertLessEqual(len(joined), 12000)
        for marker in (
            "EARLY_REQUIRED", "EARLY_TAIL", "MIDDLE_REQUIRED", "MIDDLE_TAIL",
            "LATE_REQUIRED", "LATE_TAIL",
        ):
            self.assertIn(marker, joined)


if __name__ == "__main__":
    unittest.main()
