"""Chapter draft filenames: English emit, Chinese read/delete aliases."""
import inspect
import os
import re
import tempfile
import unittest

from core.chapter_utils import (
    chapter_draft_basename,
    chapter_draft_delete_paths,
    chapter_draft_write_path,
    remove_legacy_chapter_draft,
    resolve_chapter_draft_path,
)
from core.workspace import NovelWorkspace
from training.adaptive_builder import (
    _backup_raw_chapter,
    _draft_chapter_path,
    _raw_chapter_backup_path,
    _write_draft_chapter,
    _write_file,
)
from webui.draft_chat import DraftChatManager
import training.adaptive_builder as adaptive_builder
import webui.draft_chat as draft_chat


class ChapterFilenameHelpersTests(unittest.TestCase):
    def test_english_basenames(self):
        self.assertEqual(chapter_draft_basename(1), "001_chapter_1.md")
        self.assertEqual(chapter_draft_basename(12), "012_chapter_12.md")
        self.assertEqual(chapter_draft_basename(1, raw=True), "001_chapter_1.raw.md")
        self.assertEqual(chapter_draft_basename(12, raw=True), "012_chapter_12.raw.md")

    def test_legacy_basenames_are_aliases_only(self):
        self.assertEqual(chapter_draft_basename(1, legacy=True), "001_第1章.md")
        self.assertEqual(chapter_draft_basename(1, raw=True, legacy=True), "001_第1章.raw.md")

    def test_wizard_path_parser_reads_english_name(self):
        filename = "001_chapter_1.md"
        matched = (
            re.match(r"^chapter_0*(\d+)", filename, re.I)
            or re.match(r"^0*(\d+)(?:[_\-.]|$)", filename)
            or re.search(r"第\s*(\d+)\s*章", filename)
        )
        self.assertIsNotNone(matched)
        self.assertEqual(int(matched.group(1)), 1)


class ChapterFilenameResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self._tmp.name
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()
        self.out_dir = os.path.join(self.ws.file_system, "chapters", "vol_01")
        os.makedirs(self.out_dir, exist_ok=True)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_missing_file_resolves_to_english_write_path(self):
        expected = chapter_draft_write_path(self.out_dir, 1)
        self.assertEqual(_draft_chapter_path(self.ws, 1, 1), expected)
        self.assertTrue(_draft_chapter_path(self.ws, 1, 1).endswith("001_chapter_1.md"))
        raw = _raw_chapter_backup_path(self.ws, 1, 1)
        self.assertTrue(raw.endswith("001_chapter_1.raw.md"))

    def test_resolves_chinese_alias_when_only_legacy_exists(self):
        legacy = os.path.join(self.out_dir, chapter_draft_basename(1, legacy=True))
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("old draft\n")
        resolved = resolve_chapter_draft_path(self.out_dir, 1)
        self.assertEqual(resolved, legacy)
        self.assertEqual(_draft_chapter_path(self.ws, 1, 1), legacy)

    def test_prefers_english_when_both_exist(self):
        english = chapter_draft_write_path(self.out_dir, 1)
        legacy = os.path.join(self.out_dir, chapter_draft_basename(1, legacy=True))
        with open(english, "w", encoding="utf-8") as handle:
            handle.write("english\n")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("chinese\n")
        self.assertEqual(resolve_chapter_draft_path(self.out_dir, 1), english)

    def test_write_removes_chinese_sibling(self):
        legacy = os.path.join(self.out_dir, chapter_draft_basename(2, legacy=True))
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("legacy\n")
        written = _write_draft_chapter(self.out_dir, 2, "Chapter 2: New")
        self.assertTrue(written.endswith("002_chapter_2.md"))
        self.assertTrue(os.path.isfile(written))
        self.assertFalse(os.path.isfile(legacy))

    def test_raw_backup_writes_english_and_drops_legacy(self):
        raw_dir = os.path.join(self.ws.file_system, "drafts", "vol_01", "raw_chapters")
        os.makedirs(raw_dir, exist_ok=True)
        legacy = os.path.join(raw_dir, chapter_draft_basename(3, raw=True, legacy=True))
        _write_file(legacy, "old raw")
        path = _backup_raw_chapter(self.ws, 1, 3, "new raw")
        self.assertTrue(path.endswith("003_chapter_3.raw.md"))
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.isfile(legacy))


class ChapterFilenameResetTests(unittest.TestCase):
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

    def test_delete_paths_include_english_and_chinese(self):
        refined = os.path.join(self.ws.file_system, "chapters", "vol_01")
        raw = os.path.join(self.ws.file_system, "drafts", "vol_01", "raw_chapters")
        os.makedirs(os.path.join(refined, "versions"), exist_ok=True)
        os.makedirs(os.path.join(raw, "versions"), exist_ok=True)
        files = [
            os.path.join(refined, "001_chapter_1.md"),
            os.path.join(refined, "001_第1章.md"),
            os.path.join(raw, "001_chapter_1.raw.md"),
            os.path.join(raw, "001_第1章.raw.md"),
            os.path.join(refined, "versions", "001_chapter_1.md_20260101_000000"),
            os.path.join(refined, "versions", "001_第1章.md_20260101_000000"),
            os.path.join(raw, "versions", "001_chapter_1_20260101.raw.md"),
            os.path.join(raw, "versions", "001_第1章_20260101.raw.md"),
        ]
        for path in files:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x\n")
        targets = {os.path.abspath(p) for p in chapter_draft_delete_paths(refined, raw, 1)}
        for path in files:
            self.assertIn(os.path.abspath(path), targets)

    def test_draft_chat_reset_deletes_both_names(self):
        arc_dir = os.path.join(self.ws.file_system, "story_arcs", "vol_01")
        os.makedirs(arc_dir, exist_ok=True)
        with open(os.path.join(arc_dir, "arc_001_ch001_001.md"), "w", encoding="utf-8") as handle:
            handle.write("arc body\n")
        refined = os.path.join(self.ws.file_system, "chapters", "vol_01")
        raw = os.path.join(self.ws.file_system, "drafts", "vol_01", "raw_chapters")
        os.makedirs(refined, exist_ok=True)
        os.makedirs(raw, exist_ok=True)
        english = os.path.join(refined, "001_chapter_1.md")
        chinese = os.path.join(refined, "001_第1章.md")
        english_raw = os.path.join(raw, "001_chapter_1.raw.md")
        chinese_raw = os.path.join(raw, "001_第1章.raw.md")
        for path in (english, chinese, english_raw, chinese_raw):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("draft\n")
        manager = DraftChatManager(self._tmp.name)
        result = manager.reset("book", 1, 1)
        self.assertGreaterEqual(result["deleted"], 4)
        for path in (english, chinese, english_raw, chinese_raw):
            self.assertFalse(os.path.isfile(path))


class ChapterFilenameSourceTests(unittest.TestCase):
    def test_builders_do_not_emit_chinese_write_names(self):
        fragments = (
            "第{chapter}章",
            "第{chapter_num}章",
            "第{ch_num}章",
            "第{ch}章",
            "第{i}章",
        )
        builder_src = inspect.getsource(adaptive_builder)
        chat_src = inspect.getsource(draft_chat)
        for fragment in fragments:
            self.assertNotIn(fragment, builder_src)
            self.assertNotIn(fragment, chat_src)
