"""Emit/parse contracts: Stage vs Phase stay distinct; English our-artifacts."""
import inspect
import os
import tempfile
import unittest
from unittest import mock

from core.workspace import NovelWorkspace
from training import adaptive_builder as ab
from training.adaptive_builder import (
    STAGE_HEADING_RE,
    STAGE_OUTLINE_HEADING_RE,
    _design_structure_counts,
    _is_volume_style_stage,
    _normalize_stage_roadmap,
    _reference_volume_stage_structure,
    _reference_volume_structure_context,
    _remove_stage_outline_section,
    gen_novel_name_synopsis,
    sync_stage_outline_from_new_reference,
)
from training.outline_builder import (
    ARC_HEADER_RE,
    _find_whole_book_dir,
    _is_whole_book_dir,
    _parse_virtual_volumes,
    _whole_book_dir_name,
    split_chapters,
)

_VOLUME_STYLE_BLOCK = """# Stage 1: The Inn
# Volume overview
Town under lock and pressure.
# Three-act structure
Act I opens at the gate.
# Character roster
The innkeeper.
# Foreshadowing tracker
The lock.
# Core payoff
The reveal.
Planned chapters: 12
"""

# Imported-novel chapter prefix "chapter N" as unicode escapes (no hanzi in source).
_ZH_CH_1 = "\u7b2c\u4e00\u7ae0"
_ZH_CH_2 = "\u7b2c\u4e8c\u7ae0"
_ZH_VOL = "\u5377"


class TestStageAndPhaseHeadings(unittest.TestCase):
    def test_stage_heading_matches_english_stage(self):
        self.assertEqual(STAGE_HEADING_RE.findall("# Stage 1: Foo"), ["1"])
        self.assertEqual(STAGE_HEADING_RE.findall("# Stage 12: Later"), ["12"])

    def test_phase_regex_does_not_match_stage_heading(self):
        self.assertFalse(STAGE_OUTLINE_HEADING_RE.search("# Stage 1: Foo"))
        self.assertFalse(STAGE_HEADING_RE.search("## Phase 1: Bar"))

    def test_phase_heading_matches_english_phase(self):
        self.assertEqual(STAGE_OUTLINE_HEADING_RE.findall("## Phase 2: Bar"), ["2"])
        self.assertEqual(STAGE_OUTLINE_HEADING_RE.findall("# Phase 8: Close"), ["8"])

    def test_normalize_stage_roadmap_emits_english_stage(self):
        self.assertIn("# Stage 1: Inn", _normalize_stage_roadmap("# Stage 1: Inn"))
        self.assertNotIn("HARNESS_NOVEL_LANG", inspect.getsource(_normalize_stage_roadmap))

    def test_remove_stage_outline_drops_phase_outline_keeps_other(self):
        text = (
            "# Rough outline\nKeep this.\n"
            "# Phase outline\nDrop English.\n"
            "## Nested under phase outline\nDrop too.\n"
            "# Worldview\nKeep worldview.\n"
        )
        result = _remove_stage_outline_section(text)
        self.assertIn("Keep this.", result)
        self.assertIn("Keep worldview.", result)
        self.assertNotIn("Drop English.", result)
        self.assertNotIn("Phase outline", result)

    def test_design_structure_counts_english_map_layers(self):
        worldview = "# 6. Maps / stage layers\n- Layer 1: Border | pressure\n"
        _, map_count = _design_structure_counts("", worldview)
        self.assertGreaterEqual(map_count, 1)

    def test_is_volume_style_stage_english_glossary_block(self):
        self.assertTrue(_is_volume_style_stage(_VOLUME_STYLE_BLOCK))


class TestVirtualVolumesAndArcs(unittest.TestCase):
    def test_parse_virtual_volumes_english_and_escaped_chinese(self):
        en = _parse_virtual_volumes("Volume 1: The Lock | Chapters 1-78")
        zh = _parse_virtual_volumes("%s1: The Lock | %s1-78\u7ae0" % (_ZH_VOL, "\u7b2c"))
        self.assertEqual(en, [(1, "The Lock", 1, 78)])
        self.assertEqual(zh, [(1, "The Lock", 1, 78)])

    def test_arc_header_re_matches_english_glossary_form(self):
        match = ARC_HEADER_RE.search("【Arc1: Chapters 1-5 | The Hook】")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "1")
        self.assertEqual(match.group(2), "5")
        self.assertEqual(match.group(3), "The Hook")


class TestSplitChapters(unittest.TestCase):
    def _split_text(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False,
        )
        try:
            handle.write(text)
            handle.close()
            return split_chapters(handle.name)
        finally:
            os.remove(handle.name)

    def test_split_chapters_english_and_escaped_chinese_headings(self):
        body = "The traveler crossed the locked gate before dawn. " * 3
        _, english = self._split_text(
            "Chapter 1: Opening\n%s\nChapter 2: Rising\n%s\n" % (body, body)
        )
        self.assertEqual(len(english), 2)
        self.assertTrue(english[0]["title"].startswith("Chapter 1"))
        self.assertTrue(english[1]["title"].startswith("Chapter 2"))

        _, chinese = self._split_text(
            "%s Opening\n%s\n%s Rising\n%s\n" % (_ZH_CH_1, body, _ZH_CH_2, body)
        )
        self.assertEqual(len(chinese), 2)
        self.assertTrue(chinese[0]["title"].startswith(_ZH_CH_1))
        self.assertTrue(chinese[1]["title"].startswith(_ZH_CH_2))


class TestExtendGateAndOperations(unittest.TestCase):
    def test_english_stage_append_passes_extend_gate(self):
        helper = getattr(ab, "_stage_append_starts_at", None)
        self.assertIsNotNone(helper, "factor _stage_append_starts_at for the extend gate")
        normalized = _normalize_stage_roadmap("# Stage 3: New Town")
        self.assertTrue(helper(normalized, 3))
        self.assertTrue(helper("# Stage 3: New Town", 3))
        self.assertFalse(helper("# Stage 2: Old Town", 3))
        self.assertFalse(helper("# Phase 3: Not a stage", 3))

    def test_incremental_operations_match_prompt_glossary(self):
        self.assertEqual(ab.OPERATION_ADJUST_LAST_PHASE, "adjust last phase")
        self.assertEqual(ab.OPERATION_ADD_PHASE, "add phase")
        src = inspect.getsource(sync_stage_outline_from_new_reference)
        self.assertIn("OPERATION_ADJUST_LAST_PHASE", src)
        self.assertIn("OPERATION_ADD_PHASE", src)
        self.assertIn("operation=operation", src)


class TestNameSynopsisAndVolumeSections(unittest.TestCase):
    def test_gen_novel_name_synopsis_core_gameplay_fallback(self):
        source = inspect.getsource(gen_novel_name_synopsis)
        self.assertIn("core_gameplay.md", source)
        self.assertIn("novel-outline (legacy combined command)", source)
        tmp = tempfile.mkdtemp()
        old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = tmp
        try:
            ws = NovelWorkspace("fallback_ws")
            ws.ensure_dirs()
            design = os.path.join(ws.file_system, "story_design")
            os.makedirs(design, exist_ok=True)
            with open(os.path.join(design, "long_mainline.md"), "w", encoding="utf-8") as handle:
                handle.write("# Long mainline\nThe lock holds.\n")
            with open(os.path.join(design, "core_gameplay.md"), "w", encoding="utf-8") as handle:
                handle.write("# Core gameplay\nPressure and payoff.\n")
            with mock.patch.object(ab, "_get_llm", return_value=None):
                result = gen_novel_name_synopsis(ws, force=True)
            self.assertIsNone(result)
        finally:
            if old_home is None:
                os.environ.pop("HARNESS_NOVEL_HOME", None)
            else:
                os.environ["HARNESS_NOVEL_HOME"] = old_home

    def test_volume_style_sections_english_first(self):
        tmp = tempfile.mkdtemp()
        old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = tmp
        try:
            ws = NovelWorkspace("vol_sections")
            ws.ensure_dirs()
            vol_dir = os.path.join(ws.reference_outlines, "vol_01_The_Lock")
            os.makedirs(vol_dir, exist_ok=True)
            with open(os.path.join(vol_dir, "volume_outline.md"), "w", encoding="utf-8") as handle:
                handle.write(
                    "# Volume overview\nThe lock holds the city.\n\n"
                    "# Three-act structure\nAct I starts at dusk.\n"
                )
            ctx = _reference_volume_structure_context(ws)
            self.assertIn("The lock holds the city", ctx)
            self.assertIn("Act I starts at dusk", ctx)
            volume = {"vol_idx": 1, "title": "The Lock", "dir_path": vol_dir}
            staged = _reference_volume_stage_structure(ws, volume)
            self.assertIn("The lock holds the city", staged)
            self.assertIn("Act I starts at dusk", staged)
        finally:
            if old_home is None:
                os.environ.pop("HARNESS_NOVEL_HOME", None)
            else:
                os.environ["HARNESS_NOVEL_HOME"] = old_home


class TestWholeBookVolumeDir(unittest.TestCase):
    def test_write_name_is_english(self):
        self.assertEqual(_whole_book_dir_name(), "vol_01_whole_book")
        self.assertTrue(_is_whole_book_dir("vol_01_whole_book"))
        self.assertTrue(_is_whole_book_dir("vol_01_Whole_book"))
        self.assertFalse(_is_whole_book_dir("vol_01_The_Lock"))

    def test_find_uses_whole_book_token_only(self):
        with tempfile.TemporaryDirectory() as directory:
            english = os.path.join(directory, "vol_01_whole_book")
            os.makedirs(english)
            self.assertEqual(_find_whole_book_dir(directory), english)


if __name__ == "__main__":
    unittest.main()
