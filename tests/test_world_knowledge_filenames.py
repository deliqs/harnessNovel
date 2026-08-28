"""World-knowledge section files: English emit, Chinese read/delete aliases."""
import os
import tempfile
import unittest
from types import SimpleNamespace

from core.world_knowledge import (
    CANON_INDEX_SECTIONS,
    WORLD_SECTION_NAMES,
    WORLD_SECTIONS,
    _final_section_path,
    _legacy_section_file_name,
    _render_section_document,
    _require_headings,
    _resolve_section_path,
    _section_file_name,
    _split_sections_from_document,
    _write_sections_to_final,
    world_knowledge_status,
)
from core.workspace import NovelWorkspace
from webui.task_runner import WorkspaceStore


def _cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


class WorldSectionNameTests(unittest.TestCase):
    def test_canonical_keys_and_files_are_english(self):
        expected = (
            ("Worldview", "worldview.md", "世界观.md"),
            ("Power system", "power_system.md", "力量体系.md"),
            ("Key characters", "key_characters.md", "关键人物.md"),
            ("Factions", "factions.md", "势力描述.md"),
            ("Story spine", "story_spine.md", "故事主线.md"),
            ("Key items", "key_items.md", "关键物品.md"),
            ("Skills and techniques", "skills_and_techniques.md", "技能体系.md"),
        )
        self.assertEqual(WORLD_SECTION_NAMES, tuple(item[0] for item in expected))
        for name, english, chinese in expected:
            self.assertFalse(_cjk(name))
            self.assertEqual(_section_file_name(name), english)
            self.assertEqual(_legacy_section_file_name(name), chinese)

    def test_canon_index_keys_are_english(self):
        self.assertTrue(CANON_INDEX_SECTIONS)
        for name in CANON_INDEX_SECTIONS:
            self.assertFalse(_cjk(name))

    def test_render_writes_english_heading(self):
        rendered = _render_section_document("Worldview", "# 世界观\n\nThe sky is locked.")
        self.assertTrue(rendered.startswith("# Worldview\n"))
        self.assertIn("The sky is locked.", rendered)
        self.assertNotIn("# 世界观", rendered.splitlines()[0])


class WorldSectionResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = os.path.join(self._tmp.name, "worlds", "_final")
        os.makedirs(self.directory, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_resolves_to_english_write_path(self):
        path = _resolve_section_path(self.directory, "Worldview")
        self.assertTrue(path.endswith("worldview.md"))

    def test_resolves_chinese_alias_when_only_legacy_exists(self):
        legacy = os.path.join(self.directory, "世界观.md")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("# 世界观\n\nOld world.\n")
        self.assertEqual(_resolve_section_path(self.directory, "Worldview"), legacy)

    def test_prefers_english_when_both_exist(self):
        english = os.path.join(self.directory, "worldview.md")
        legacy = os.path.join(self.directory, "世界观.md")
        with open(english, "w", encoding="utf-8") as handle:
            handle.write("# Worldview\n\nEnglish.\n")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("# 世界观\n\nChinese.\n")
        self.assertEqual(_resolve_section_path(self.directory, "Worldview"), english)


class WorldSectionWriteAndStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self._tmp.name
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()
        self.final_dir = os.path.join(self.ws.file_system, "world_knowledge", "worlds", "_final")
        os.makedirs(self.final_dir, exist_ok=True)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def _write_named(self, filename, heading):
        path = os.path.join(self.final_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# %s\n\nBody.\n" % heading)
        return path

    def test_write_uses_english_names_and_drops_chinese_siblings(self):
        legacy = self._write_named("世界观.md", "世界观")
        written = _write_sections_to_final(self.ws, {
            "Worldview": "# Worldview\n\nNew sky.",
        })
        worldview = written["Worldview"]
        self.assertTrue(worldview.endswith("worldview.md"))
        self.assertTrue(os.path.isfile(worldview))
        self.assertFalse(os.path.isfile(legacy))
        self.assertTrue(os.path.isfile(os.path.join(self.final_dir, "power_system.md")))

    def test_status_ready_with_chinese_files_only(self):
        for name, _ in WORLD_SECTIONS:
            self._write_named(_legacy_section_file_name(name), name)
        status = world_knowledge_status(self.ws)
        self.assertEqual(status["final_section_count"], 7)
        self.assertTrue(status["ready"])
        self.assertTrue(_final_section_path(self.ws, "Worldview").endswith("世界观.md"))

    def test_status_ready_with_english_files(self):
        for name, _ in WORLD_SECTIONS:
            self._write_named(_section_file_name(name), name)
        status = world_knowledge_status(self.ws)
        self.assertEqual(status["final_section_count"], 7)
        self.assertTrue(status["ready"])

    def test_status_counts_unique_sections_when_both_names_exist(self):
        for name, _ in WORLD_SECTIONS:
            self._write_named(_section_file_name(name), name)
            self._write_named(_legacy_section_file_name(name), name)
        status = world_knowledge_status(self.ws)
        self.assertEqual(status["final_section_count"], 7)
        self.assertTrue(status["ready"])
        summary = WorkspaceStore(self._tmp.name).summary("book")
        self.assertEqual(summary["world_knowledge"]["final_section_count"], 7)
        self.assertTrue(summary["world_knowledge"]["ready"])


class WorldSectionParseTests(unittest.TestCase):
    def test_split_accepts_english_and_chinese_headings(self):
        english = "\n\n".join("# %s\n\n%s body" % (name, name) for name, _ in WORLD_SECTIONS)
        chinese = "\n\n".join(
            "# %s\n\n%s body" % (_legacy_section_file_name(name).replace(".md", ""), name)
            for name, _ in WORLD_SECTIONS
        )
        for document in (english, chinese):
            _require_headings(document, WORLD_SECTION_NAMES, "world sections")
            sections = _split_sections_from_document(document)
            self.assertEqual(set(sections), set(WORLD_SECTION_NAMES))
            self.assertTrue(sections["Worldview"].startswith("# Worldview\n"))

    def test_wizard_labels_english_and_legacy_filenames(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(root, "webui", "static", "wizard-v0.js")
        with open(script_path, encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn('"worldview.md"', script)
        self.assertIn('"power_system.md"', script)
        self.assertIn('"skills_and_techniques.md"', script)
        self.assertIn('"世界观.md"', script)
        self.assertIn('"力量体系.md"', script)


class WorldKnowledgeStatusHelperTests(unittest.TestCase):
    def test_status_accepts_file_system_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            fs = os.path.join(directory, "file_system")
            os.makedirs(os.path.join(fs, "world_knowledge", "worlds", "_final"), exist_ok=True)
            status = world_knowledge_status(SimpleNamespace(file_system=fs))
            self.assertFalse(status["ready"])
            self.assertEqual(status["final_section_count"], 0)


if __name__ == "__main__":
    unittest.main()
