"""Offline tests for build_world_knowledge vs a chat-edited _final."""
import os
import tempfile
import unittest

from core.world_knowledge import (
    CANON_INDEX_SECTIONS,
    WORLD_SECTIONS,
    _canon_index_path,
    _cards_dir,
    _section_file_name,
    _source_records,
    _source_world_dir,
    build_world_knowledge,
    get_chat_edited,
    import_world_sources,
    set_chat_edited,
)
from core.workspace import NovelWorkspace


class RepeatingLLM:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.text


def _seven(prefix):
    return "\n\n".join(
        "# %s\n\n%s %s." % (name, prefix, name) for name, _ in WORLD_SECTIONS
    ) + "\n"


def _llm_text():
    parts = ["# %s\n\nRebuild %s." % (name, name) for name, _ in WORLD_SECTIONS]
    parts.extend("# %s\n\nIndex %s." % (name, name) for name in CANON_INDEX_SECTIONS)
    return "\n\n".join(parts) + "\n"


class WorldChatRebuildTests(unittest.TestCase):
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

    def _final_dir(self):
        return os.path.join(self.ws.file_system, "world_knowledge", "worlds", "_final")

    def _write(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _write_named(self, directory, prefix):
        os.makedirs(directory, exist_ok=True)
        for name, _ in WORLD_SECTIONS:
            self._write(
                os.path.join(directory, _section_file_name(name)),
                "# %s\n\n%s %s.\n" % (name, prefix, name),
            )

    def _import(self, filename, text):
        src = os.path.join(self._tmp.name, filename)
        self._write(src, text)
        import_world_sources(self.ws, [src])
        for record in _source_records(self.ws):
            if record.get("file_name") == filename:
                return record
        raise AssertionError("imported source not in manifest: %s" % filename)

    def _seed_chat_world(self):
        record = self._import("primary.md", "Primary locked-sky source. " * 40)
        self._write(
            os.path.join(_cards_dir(self.ws), "%s_part_001.md" % record["id"]),
            _seven("Card"),
        )
        canon_parts = ["# %s\n\nIndex %s." % (name, name) for name in CANON_INDEX_SECTIONS]
        self._write(_canon_index_path(self.ws), "\n\n".join(canon_parts) + "\n")
        self._write_named(_source_world_dir(self.ws, record), "Source body for")
        self._write_named(self._final_dir(), "Chat body for")
        set_chat_edited(self.ws, True)
        return record

    def _backups(self):
        worlds = os.path.join(self.ws.file_system, "world_knowledge", "worlds")
        return [name for name in os.listdir(worlds) if name.startswith("_final_backup_")]

    def _backup_worldview(self):
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        path = os.path.join(
            self.ws.file_system, "world_knowledge", "worlds", backups[0], "worldview.md",
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _final_bytes(self):
        data = {}
        for name in os.listdir(self._final_dir()):
            with open(os.path.join(self._final_dir(), name), "rb") as handle:
                data[name] = handle.read()
        return data

    def _read_final(self, name):
        path = os.path.join(self._final_dir(), _section_file_name(name))
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _build(self, **kwargs):
        kwargs.setdefault("max_workers", 1)
        return build_world_knowledge(self.ws, RepeatingLLM(_llm_text()), **kwargs)

    def test_merge_only_backs_up_chat_and_clears_flag(self):
        self._seed_chat_world()
        result = self._build(merge_only=True)
        self.assertTrue(result)
        self.assertIn("Chat body for Worldview", self._backup_worldview())
        self.assertFalse(get_chat_edited(self.ws))
        self.assertNotIn("Chat body for Worldview", self._read_final("Worldview"))

    def test_force_backs_up_chat_and_clears_flag(self):
        self._seed_chat_world()
        result = self._build(force=True)
        self.assertTrue(result)
        self.assertIn("Chat body for Worldview", self._backup_worldview())
        self.assertFalse(get_chat_edited(self.ws))
        self.assertNotIn("Chat body for Worldview", self._read_final("Worldview"))

    def test_new_source_backs_up_chat_and_clears_flag(self):
        self._seed_chat_world()
        self._import("supp.md", "Short supplement.")
        result = self._build(force=False)
        self.assertTrue(result)
        self.assertIn("Chat body for Worldview", self._backup_worldview())
        self.assertFalse(get_chat_edited(self.ws))

    def test_unchanged_non_force_keeps_chat_files(self):
        self._seed_chat_world()
        before = self._final_bytes()
        result = self._build(force=False)
        self.assertTrue(result)
        self.assertEqual(self._backups(), [])
        self.assertTrue(get_chat_edited(self.ws))
        self.assertEqual(self._final_bytes(), before)
        self.assertIn("Chat body for Worldview", self._read_final("Worldview"))


if __name__ == "__main__":
    unittest.main()
