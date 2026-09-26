"""Offline tests for the target-world chat web backend."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.world_knowledge import _section_file_name
from core.workspace import NovelWorkspace
from webui.app import WebRuntime
from webui.task_runner import TaskRecord, WorkspaceStore
from webui.world_chat import WorldChatManager


NO_MODEL = "No usable model is configured. Set the LLM API in the top-right first."


class ScriptedLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.outputs.pop(0) if self.outputs else ""


class BlockingLLM(ScriptedLLM):
    def __init__(self, outputs, started, release):
        super().__init__(outputs)
        self.started = started
        self.release = release

    def generate(self, prompt):
        self.started.set()
        self.release.wait(timeout=5)
        return super().generate(prompt)


def _plan(reply, changes=None):
    parts = ["# Reply\n\n%s\n\n# Section changes\n" % reply]
    if not changes:
        return "".join(parts) + "\nNone\n"
    for name, instruction in changes:
        parts.append("\n## %s\n\n%s\n" % (name, instruction))
    return "".join(parts)


def _sec(name, body):
    return "# %s\n\n%s\n" % (name, body)


class WorldChatWebTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        self.root = Path(self._tmp.name)
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        self.web_home = self.root / "web-home"
        self.web_home.mkdir()
        self._settings_patch = patch(
            "webui.app.WEB_SETTINGS_PATH", self.web_home / "settings.json",
        )
        self._home_patch = patch("webui.app.WEB_HOME", self.web_home)
        self._settings_patch.start()
        self._home_patch.start()
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()
        self.manager = WorldChatManager(self.root)

    def tearDown(self):
        self._home_patch.stop()
        self._settings_patch.stop()
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def _final_dir(self):
        return Path(self.ws.file_system) / "world_knowledge" / "worlds" / "_final"

    def _listed(self):
        directory = self._final_dir()
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def _wait(self, manager=None, timeout=5.0):
        manager = manager or self.manager
        deadline = time.time() + timeout
        status = manager.job_status("book")
        while time.time() < deadline:
            status = manager.job_status("book")
            if status.get("status") in {"completed", "failed", "stopped"}:
                return status
            time.sleep(0.02)
        self.fail("job did not finish: %s" % status)

    def _runtime(self):
        return WebRuntime(str(self.root))

    def test_write_turn_completes_with_artifacts_and_section_files(self):
        llm = ScriptedLLM([
            _plan("Wrote two sections.", [
                ("Worldview", "Record the locked sky."),
                ("Key characters", "Add the Warden."),
            ]),
            _sec("Worldview", "The sky is locked."),
            _sec("Key characters", "The Warden keeps the gate."),
        ])
        with patch("webui.world_chat._get_llm", return_value=llm):
            job = self.manager.start_message("book", "Locked sky. The Warden keeps the only gate.")
            self.assertEqual(job["status"], "running")
            self.assertNotIn("stop_event", job)
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        history = self.manager.history("book")
        self.assertEqual([turn["role"] for turn in history["turns"]], ["user", "assistant"])
        self.assertIn("guide", history)
        assistant = history["turns"][1]
        self.assertEqual(assistant["content"], "Wrote two sections.")
        self.assertEqual([item["path"] for item in assistant["artifacts"]], [
            "file_system/world_knowledge/worlds/_final/worldview.md",
            "file_system/world_knowledge/worlds/_final/key_characters.md",
        ])
        self.assertIn(_section_file_name("Worldview"), self._listed())
        self.assertIn(_section_file_name("Key characters"), self._listed())
        conv_path = self.root / "book" / "file_system" / "world_knowledge" / "conversation.json"
        self.assertTrue(conv_path.is_file())

    def test_interview_turn_adds_reply_without_artifacts(self):
        llm = ScriptedLLM([_plan("What is the core conflict?")])
        with patch("webui.world_chat._get_llm", return_value=llm):
            self.manager.start_message("book", "Build a sealed-sky world.")
            status = self._wait()
        self.assertEqual(status["status"], "completed")
        history = self.manager.history("book")
        self.assertEqual(history["turns"][1]["content"], "What is the core conflict?")
        self.assertEqual(history["turns"][1].get("artifacts") or [], [])
        self.assertEqual(self._listed(), [])

    def test_failing_model_keeps_user_turn_and_writes_nothing(self):
        with patch("webui.world_chat._get_llm", return_value=ScriptedLLM([""])):
            self.manager.start_message("book", "Build the world.")
            status = self._wait()
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status.get("error"))
        history = self.manager.history("book")
        self.assertEqual([turn["role"] for turn in history["turns"]], ["user"])
        self.assertEqual(history["turns"][0]["content"], "Build the world.")
        self.assertEqual(self._listed(), [])

    def test_no_usable_model_uses_arc_chat_error_text(self):
        with patch("webui.world_chat._get_llm", return_value=None):
            self.manager.start_message("book", "Hello.")
            status = self._wait()
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], NO_MODEL)

    def test_stop_ends_job_without_writing(self):
        started = threading.Event()
        release = threading.Event()
        llm = BlockingLLM([_plan("What is the core conflict?")], started, release)
        try:
            with patch("webui.world_chat._get_llm", return_value=llm):
                self.manager.start_message("book", "Build a sealed-sky world.")
                self.assertTrue(started.wait(timeout=5))
                stopping = self.manager.stop("book")
                self.assertEqual(stopping["status"], "stopping")
                self.assertNotIn("stop_event", stopping)
                release.set()
                status = self._wait()
        finally:
            release.set()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual([turn["role"] for turn in self.manager.history("book")["turns"]], ["user"])
        self.assertEqual(self._listed(), [])

    def test_clear_refused_while_running(self):
        started = threading.Event()
        release = threading.Event()
        llm = BlockingLLM([_plan("What is the core conflict?")], started, release)
        try:
            with patch("webui.world_chat._get_llm", return_value=llm):
                self.manager.start_message("book", "Build a sealed-sky world.")
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaises(ValueError):
                    self.manager.clear("book")
        finally:
            release.set()
            self._wait()

    def test_guide_save_and_reset_through_manager(self):
        status = self.manager.guide_status("book")
        self.assertFalse(status["exists"])
        self.assertEqual(status["path"], "file_system/world_knowledge/chat_guide.md")
        saved = self.manager.save_guide("book", "Prefer sealed skies.")
        self.assertTrue(saved["exists"])
        path = Path(self.ws.file_system) / "world_knowledge" / "chat_guide.md"
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "Prefer sealed skies.")
        history = self.manager.history("book")
        self.assertTrue(history["guide"]["exists"])
        with self.assertRaises(ValueError):
            self.manager.save_guide("book", "   ")
        reset = self.manager.reset_guide("book")
        self.assertFalse(reset["exists"])
        self.assertFalse(path.is_file())

    def test_busy_detection_uses_tuple_workspace_key(self):
        started = threading.Event()
        release = threading.Event()
        llm = BlockingLLM([_plan("What is the core conflict?")], started, release)
        try:
            with patch("webui.world_chat._get_llm", return_value=llm):
                self.manager.start_message("book", "Build a sealed-sky world.")
                self.assertTrue(started.wait(timeout=5))
                self.assertEqual(list(self.manager._jobs), [("book",)])
                self.assertTrue(WebRuntime._chat_manager_busy(self.manager, "book"))
                self.assertFalse(WebRuntime._chat_manager_busy(self.manager, "other"))
        finally:
            release.set()
            self._wait()

    def test_mutual_exclusion_blocks_world_task_while_chat_runs(self):
        runtime = self._runtime()
        runtime.world_chat._jobs[("book",)] = {"status": "running"}
        with self.assertRaises(ValueError) as ctx:
            runtime.ensure_world_task_can_start("book")
        self.assertIn("chat", str(ctx.exception).lower())
        runtime.ensure_world_task_can_start("other")

    def test_mutual_exclusion_blocks_chat_while_world_task_runs(self):
        runtime = self._runtime()
        record = TaskRecord(
            id="task1", type="world_build", label="Build",
            workspace="book", status="running",
        )
        runtime.tasks._tasks[record.id] = record
        with self.assertRaises(ValueError) as ctx:
            runtime.ensure_world_chat_can_start("book")
        self.assertIn("build", str(ctx.exception).lower())
        queued = TaskRecord(
            id="task2", type="world_import", label="Import",
            workspace="book", status="queued",
        )
        runtime.tasks._tasks[record.id] = queued
        with self.assertRaises(ValueError):
            runtime.ensure_world_chat_can_start("book")
        runtime.ensure_world_chat_can_start("other")

    def test_summary_includes_chat_edited(self):
        summary = WorkspaceStore(self.root).summary("book")
        self.assertIn("chat_edited", summary["world_knowledge"])
        self.assertFalse(summary["world_knowledge"]["chat_edited"])

    def test_route_wiring_and_runtime_managers(self):
        import inspect
        from webui import app as web_app
        source = inspect.getsource(web_app.create_app)
        for path in (
            "/world-knowledge/chat",
            "/world-knowledge/job",
            "/world-knowledge/prompts",
            "/world-knowledge/stop",
            "/world-knowledge/conversation",
            "/world-knowledge/guide",
        ):
            self.assertIn(path, source)
        self.assertIn("ensure_world_task_can_start", source)
        self.assertIn("ensure_world_chat_can_start", source)
        runtime = self._runtime()
        self.assertIsInstance(runtime.world_chat, WorldChatManager)
        self.assertEqual(runtime.world_chat.root, Path(self.root))
        runtime.set_workspace_root(str(self.root))
        self.assertEqual(runtime.world_chat.root, Path(str(self.root)))
        managers = inspect.getsource(WebRuntime.delete_workspace)
        self.assertIn("self.world_chat", managers)


if __name__ == "__main__":
    unittest.main()
