"""Offline tests for target-world chat."""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from core.world_chat import (
    apply_world_chat_message, chat_guide_status, load_chat_guide,
    reset_chat_guide, save_chat_guide,
)
from core.world_knowledge import (
    WORLD_SECTIONS, _backup_chat_edited_final, _integrate_final_sections,
    _section_file_name, get_chat_edited, set_chat_edited, world_knowledge_status,
)
from core.workspace import NovelWorkspace


class ScriptedLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.outputs.pop(0) if self.outputs else ""


def _plan(reply, changes=None):
    parts = ["# Reply\n\n%s\n\n# Section changes\n" % reply]
    if not changes:
        return "".join(parts) + "\nNone\n"
    for name, instruction in changes:
        parts.append("\n## %s\n\n%s\n" % (name, instruction))
    return "".join(parts)


def _sec(name, body):
    return "# %s\n\n%s\n" % (name, body)


class WorldChatTests(unittest.TestCase):
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

    def _listed(self):
        directory = self._final_dir()
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def _read(self, name):
        with open(os.path.join(self._final_dir(), _section_file_name(name)), encoding="utf-8") as handle:
            return handle.read()

    def _bytes(self):
        data = {}
        for name in os.listdir(self._final_dir()):
            with open(os.path.join(self._final_dir(), name), "rb") as handle:
                data[name] = handle.read()
        return data

    def _apply(self, llm, message, should_stop=None):
        return apply_world_chat_message(self.ws, llm, message, [], should_stop=should_stop)

    def _write_named(self, directory, body_for):
        os.makedirs(directory, exist_ok=True)
        paths = {}
        for name, _ in WORLD_SECTIONS:
            path = os.path.join(directory, _section_file_name(name))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# %s\n\n%s\n" % (name, body_for(name)))
            paths[name] = path
        return paths

    def _seed_rebuild(self, supplement=False):
        worlds = os.path.join(self.ws.file_system, "world_knowledge", "worlds")
        items = [{
            "record": {"id": "p1", "role": "primary", "file_name": "p.md"},
            "sections": self._write_named(os.path.join(worlds, "primary"), lambda n: "Source body for %s." % n),
        }]
        if supplement:
            items.append({
                "record": {"id": "s1", "role": "supplement", "file_name": "s.md"},
                "sections": self._write_named(os.path.join(worlds, "supp"), lambda n: "Supp body for %s." % n),
            })
        self._write_named(os.path.join(worlds, "_final"), lambda n: "Chat body for %s." % n)
        set_chat_edited(self.ws, True)
        return items

    def _backups(self):
        worlds = os.path.join(self.ws.file_system, "world_knowledge", "worlds")
        return [name for name in os.listdir(worlds) if name.startswith("_final_backup_")]

    def _seed_worldview(self):
        self._apply(ScriptedLLM([
            _plan("w", [("Worldview", "Record the sky.")]),
            _sec("Worldview", "The sky is locked."),
        ]), "Sky.")

    def test_interview_write_refine_fail_and_ready(self):
        result = self._apply(ScriptedLLM([_plan("What is the core conflict?")]), "Build a sealed-sky world.")
        self.assertEqual((result["reply"], result["changed_sections"], result["artifacts"], self._listed()),
                         ("What is the core conflict?", [], [], []))
        self.assertIn("chat_edited", world_knowledge_status(self.ws))
        self.assertFalse(world_knowledge_status(self.ws)["chat_edited"])
        written = self._apply(ScriptedLLM([
            _plan("Wrote two sections.", [
                ("Worldview", "Record the locked sky."),
                ("Key characters", "Add the Warden."),
            ]),
            _sec("Worldview", "The sky is locked."),
            _sec("Key characters", "The Warden keeps the gate."),
        ]), "Locked sky. The Warden keeps the only gate.")
        self.assertEqual(written["changed_sections"], ["Worldview", "Key characters"])
        self.assertEqual([item["path"] for item in written["artifacts"]], [
            "file_system/world_knowledge/worlds/_final/worldview.md",
            "file_system/world_knowledge/worlds/_final/key_characters.md",
        ])
        self.assertEqual(self._listed(), sorted(_section_file_name(n) for n, _ in WORLD_SECTIONS))
        self.assertIn("The sky is locked.", self._read("Worldview"))
        self.assertIn("None", self._read("Factions"))
        snapshot = self._bytes()
        self._apply(ScriptedLLM([
            _plan("Updated the sky.", [("Worldview", "Add a sealed sea.")]),
            _sec("Worldview", "The sky is locked and the sea is sealed."),
        ]), "Also seal the sea.")
        after = self._bytes()
        for name, data in snapshot.items():
            (self.assertNotEqual if name == "worldview.md" else self.assertEqual)(data, after[name])
        flag_before = get_chat_edited(self.ws)
        with self.assertRaises(RuntimeError):
            self._apply(ScriptedLLM([
                _plan("Trying factions.", [("Factions", "Add a church.")]), "",
            ]), "Add a church.")
        self.assertEqual(self._bytes(), after)
        self.assertEqual(get_chat_edited(self.ws), flag_before)
        status = world_knowledge_status(self.ws)
        self.assertTrue(status["ready"] and status["chat_edited"])
        self.assertEqual(status["final_section_count"], 7)

    def test_unknown_section_name_ignored(self):
        result = self._apply(ScriptedLLM([
            _plan("ok", [("Not a section", "drop me"), ("Worldview", "Record the sky.")]),
            _sec("Worldview", "Sky locked."),
        ]), "Sky.")
        self.assertEqual(result["changed_sections"], ["Worldview"])
        self.assertIn("Sky locked.", self._read("Worldview"))

    def test_none_body_does_not_replace_meaningful_section(self):
        self._seed_worldview()
        snapshot, flag = self._bytes(), get_chat_edited(self.ws)
        with self.assertRaises(RuntimeError):
            self._apply(ScriptedLLM([
                _plan("clear", [("Worldview", "Clear it.")]), _sec("Worldview", "None"),
            ]), "Clear worldview.")
        self.assertEqual(self._bytes(), snapshot)
        self.assertEqual(get_chat_edited(self.ws), flag)

    def test_malformed_section_output_writes_nothing(self):
        self._seed_worldview()
        snapshot, flag = self._bytes(), get_chat_edited(self.ws)
        bad = [
            "Here is the replacement:\n# Worldview\n\nNone\n",
            "# Worldview\n\nSky.\n\n# Factions\n\nA church.\n",
            "# Worldview\n\nSky.\n\n# Notes\n\nA side note.\n",
        ]
        for text in bad:
            with self.assertRaises(RuntimeError):
                self._apply(ScriptedLLM([_plan("x", [("Worldview", "Replace.")]), text]), "x")
            self.assertEqual(self._bytes(), snapshot)
            self.assertEqual(get_chat_edited(self.ws), flag)

    def test_subheading_body_is_kept(self):
        self._apply(ScriptedLLM([
            _plan("w", [("Worldview", "Record the ban.")]),
            _sec("Worldview", "## Magic is forbidden\nViolations incur exile."),
        ]), "Ban magic.")
        stored = self._read("Worldview")
        self.assertIn("## Magic is forbidden", stored)
        self.assertIn("Violations incur exile.", stored)

    def test_second_section_failure_writes_nothing(self):
        with self.assertRaises(RuntimeError):
            self._apply(ScriptedLLM([
                _plan("w", [("Worldview", "Record the sky."), ("Key characters", "Add the Warden.")]),
                _sec("Worldview", "The sky is locked."), "",
            ]), "Sky.")
        self.assertEqual(self._listed(), [])
        self.assertFalse(get_chat_edited(self.ws))

    def test_stop_writes_nothing(self):
        plan = _plan("w", [("Worldview", "Record the sky."), ("Factions", "Add a church.")])
        with self.assertRaises(RuntimeError):
            self._apply(ScriptedLLM([plan, _sec("Worldview", "The sky is locked.")]),
                        "Sky.", should_stop=lambda: True)
        self.assertEqual(self._listed(), [])
        halt = {"on": False}

        class Trip(ScriptedLLM):
            def generate(self, prompt):
                text = ScriptedLLM.generate(self, prompt)
                if len(self.prompts) >= self.trip:
                    halt["on"] = True
                return text

        for trip in (1, 2):
            halt["on"] = False
            llm = Trip([plan, _sec("Worldview", "The sky is locked."), _sec("Factions", "A church.")])
            llm.trip = trip
            with self.assertRaises(RuntimeError):
                self._apply(llm, "Sky.", should_stop=lambda: halt["on"])
            self.assertEqual(self._listed(), [])
        self.assertFalse(get_chat_edited(self.ws))

    def test_flag_failure_does_not_write_sections(self):
        with patch("core.world_chat.set_chat_edited", side_effect=RuntimeError("manifest")):
            with self.assertRaises(RuntimeError):
                self._apply(ScriptedLLM([
                    _plan("w", [("Worldview", "Record the sky.")]),
                    _sec("Worldview", "The sky is locked."),
                ]), "Sky.")
        self.assertEqual(self._listed(), [])
        self.assertFalse(get_chat_edited(self.ws))

    def test_guide_save_load_reset_and_prompt_text(self):
        for empty in ("", " \n\t"):
            with self.assertRaises(ValueError):
                save_chat_guide(self.ws, empty)
        self.assertFalse(chat_guide_status(self.ws)["exists"])
        save_chat_guide(self.ws, "Always mention silver trees.")
        self.assertEqual(load_chat_guide(self.ws).strip(), "Always mention silver trees.")
        self.assertEqual(chat_guide_status(self.ws)["path"], "file_system/world_knowledge/chat_guide.md")
        llm = ScriptedLLM([
            _plan("w", [("Worldview", "Record the sky.")]),
            _sec("Worldview", "The sky is locked. Silver trees line the rim."),
        ])
        self._apply(llm, "Sky.")
        self.assertEqual(len(llm.prompts), 2)
        for prompt in llm.prompts:
            self.assertIn("Always mention silver trees.", prompt)
        reset_chat_guide(self.ws)
        self.assertEqual(load_chat_guide(self.ws), "")
        self.assertFalse(chat_guide_status(self.ws)["exists"])

    def test_backup_on_overwrite_clears_flag(self):
        _integrate_final_sections(self.ws, self._seed_rebuild(), llm=None, force=True)
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^_final_backup_\d{8}_\d{6}$")
        path = os.path.join(self.ws.file_system, "world_knowledge", "worlds", backups[0], "worldview.md")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("Chat body for Worldview", handle.read())
        self.assertFalse(get_chat_edited(self.ws))

    def test_backup_skipped_on_non_force_fresh_final(self):
        _integrate_final_sections(self.ws, self._seed_rebuild(), llm=None, force=False)
        self.assertEqual(self._backups(), [])
        self.assertTrue(get_chat_edited(self.ws))
        self.assertIn("Chat body for Worldview", self._read("Worldview"))

    def test_backup_when_sources_newer_than_final(self):
        items = self._seed_rebuild()
        later = max(os.path.getmtime(p) for item in items for p in item["sections"].values()) + 10
        for item in items:
            for path in item["sections"].values():
                os.utime(path, (later, later))
        _integrate_final_sections(self.ws, items, llm=None, force=False)
        self.assertEqual(len(self._backups()), 1)
        self.assertFalse(get_chat_edited(self.ws))

    def test_failed_rebuild_keeps_flag_and_backup(self):
        items = self._seed_rebuild(supplement=True)
        with self.assertRaises(RuntimeError):
            _integrate_final_sections(self.ws, items, llm=ScriptedLLM([]), force=True)
        self.assertTrue(get_chat_edited(self.ws))
        self.assertEqual(len(self._backups()), 1)
        self.assertIn("Chat body for Worldview", self._read("Worldview"))

    def test_second_backup_same_second_does_not_fail(self):
        self._seed_rebuild()
        frozen = datetime(2026, 9, 19, 12, 0, 0)
        with patch("core.world_knowledge.datetime") as mocked:
            mocked.now.return_value = frozen
            _backup_chat_edited_final(self.ws)
            set_chat_edited(self.ws, True)
            _backup_chat_edited_final(self.ws)
        self.assertEqual(len(self._backups()), 2)
