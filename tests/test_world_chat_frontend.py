"""Offline tests for the target-world chat web frontend wiring."""
import unittest
from pathlib import Path


class WorldChatFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.index = (cls.root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        cls.wizard = (cls.root / "webui" / "static" / "wizard-v0.js").read_text(encoding="utf-8")
        cls.chat = (cls.root / "webui" / "static" / "world-chat.js").read_text(encoding="utf-8")
        cls.setup = (cls.root / "setup.py").read_text(encoding="utf-8")

    def test_index_loads_world_chat_after_wizard(self):
        wizard = self.index.find("/assets/wizard-v0.js?")
        chat = self.index.find("/assets/world-chat.js?")
        self.assertNotEqual(wizard, -1)
        self.assertGreater(chat, wizard)

    def test_world_chat_js_stays_under_300_lines(self):
        self.assertLessEqual(len(self.chat.splitlines()), 300)
        self.assertTrue((self.root / "webui" / "static" / "world-chat.js").is_file())

    def test_package_data_covers_static_assets(self):
        self.assertIn('"webui": ["static/*"]', self.setup)

    def test_wizard_mounts_world_chat_on_world_step(self):
        self.assertIn('id="world-chat-host"', self.wizard)
        self.assertIn("loadWorldChat", self.wizard)
        self.assertIn("bindWorldSource", self.wizard)

    def test_world_step_copy_mentions_chat(self):
        self.assertIn('id: "world", title: "Target world", short: "Sources, chat, or both"', self.wizard)
        self.assertIn("heading: \"Build the target-world knowledge base\"", self.wizard)
        self.assertIn("through chat, or both", self.wizard)
        self.assertIn("describe the world in chat", self.wizard)
        self.assertIn('reviewPrefixes: ["file_system/world_knowledge/worlds/_final"]', self.wizard)

    def test_enable_toggle_shows_for_chat_only_world(self):
        self.assertIn("final_section_count", self.wizard)
        self.assertIn("sources.length || sectionCount", self.wizard)
        world_form = self.wizard[self.wizard.index("function worldForm()") : self.wizard.index("function mechanicsForm()")]
        self.assertIn("All 7 knowledge-base sections are ready.", world_form)
        self.assertIn("Turn it off to stop injecting these sections into later design", world_form)

    def test_rebuild_confirms_when_chat_edited(self):
        self.assertIn("chat_edited", self.wizard)
        self.assertIn("_final_backup_<timestamp>", self.wizard)
        submit = self.wizard[self.wizard.index("async function submitWorldStep()") : self.wizard.index("async function submitMechanicsStep()")]
        self.assertIn("if (!confirm(", submit)
        self.assertLess(submit.index("confirm("), submit.index("startTask"))
        self.assertIn("/api/workspaces/", submit)
        self.assertLess(submit.index("api("), submit.index("confirm("))
        self.assertNotIn("wizardState.summary?.world_knowledge?.chat_edited", submit)
        self.assertIn("summary?.world_knowledge?.chat_edited", submit)
        after_upload = submit[submit.index("files.map(uploadFile)"):]
        self.assertIn("wizardState.workspace !== workspace", after_upload)
        self.assertLess(after_upload.index("wizardState.workspace !== workspace"), after_upload.index("startTask"))

    def test_world_chat_uses_contract_routes_and_helpers(self):
        for fragment in (
            "/chat", "/job", "/conversation", "/guide", "/stop",
            "chat-composer", "chat-input-row", "chat-send-btn",
            "uploadFile", "escapeHtml", "refreshWorkspaceArtifacts",
            "openReviewFile", "chatMessageMarkup", "Ctrl",
        ):
            self.assertIn(fragment, self.chat)

    def test_world_chat_js_has_no_cjk(self):
        hits = [
            index for index, char in enumerate(self.chat)
            if 0x4E00 <= ord(char) <= 0x9FFF
        ]
        self.assertEqual(hits, [])

    def test_async_handlers_pin_initiating_workspace(self):
        self.assertIn("function worldChatBase(name)", self.chat)
        self.assertIn("function worldChatOn(name)", self.chat)
        self.assertNotRegex(self.chat, r"worldChatBase\(\s*\)")
        names = (
            "loadWorldChat", "sendWorldMessage", "stopWorldJob",
            "clearWorldConversation", "saveWorldGuide", "resetWorldGuide",
        )
        for name in names:
            start = self.chat.index("async function %s(" % name)
            rest = self.chat[start:]
            body = rest[: rest.index("\nasync function ") if "\nasync function " in rest else rest.index("\nfunction pollWorldJob")]
            self.assertIn("const workspace = wizardState.workspace", body, name)
            self.assertIn("worldChatBase(workspace)", body, name)
            self.assertIn("worldChatOn(workspace)", body, name)
        save = self.chat[self.chat.index("async function saveWorldGuide()") : self.chat.index("async function resetWorldGuide()")]
        self.assertLess(save.index("worldChatBase(workspace)"), save.index("uploadFile"))
        self.assertLess(save.index("await uploadFile"), save.index("worldChatOn(workspace)"))
        poll = self.chat[self.chat.index("function pollWorldJob()") :]
        self.assertIn("const workspace = wizardState.workspace", poll)
        self.assertIn("worldChatBase(workspace)", poll)
        self.assertLess(poll.index("worldChatBase(workspace)"), poll.index("await api"))


if __name__ == "__main__":
    unittest.main()
