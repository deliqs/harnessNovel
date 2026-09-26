"""A test base for the orchestrator tools of the arcs, chapters and draft steps.

It points `HARNESS_NOVEL_HOME` at a temporary root with one workspace, `book`, and gives the
tools a runtime whose chat managers are mocks, so no job or model ever runs.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic_ai.models.test import TestModel

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.registry import build_agent

IDLE = {"status": "idle", "phase": "idle", "message": ""}


class ScopedToolCase(unittest.TestCase):
    phase = "arcs"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        self.fs = self.root / "book" / "file_system"
        self.fs.mkdir(parents=True)
        self.runtime = SimpleNamespace(arcs_chat=MagicMock(), chapters_chat=MagicMock(), draft_chat=MagicMock())
        for manager in (self.runtime.arcs_chat, self.runtime.chapters_chat, self.runtime.draft_chat):
            manager.job_status.return_value = dict(IDLE)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._home
        self._tmp.cleanup()

    def ctx(self, ui_state=None):
        deps = OrchestratorDeps(runtime=self.runtime, workspace="book", phase=self.phase, ui_state=ui_state or {})
        return SimpleNamespace(deps=deps)

    def write(self, relative, text="Some text."):
        path = self.fs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_arc(self, volume, idx, start, end):
        return self.write(f"story_arcs/vol_{volume:02d}/arc_{idx:03d}_ch{start:03d}_{end:03d}.md", "# Arc\n\nPlot.")

    def assert_started(self, result, url):
        data = json.loads(result)
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["job"]["url"], url)
        return data

    def assert_status(self, result, status):
        data = json.loads(result)
        self.assertEqual(data["status"], status, result)
        return data["message"]

    def approvals(self):
        agent = build_agent(self.phase, TestModel())
        return {name: tool.requires_approval for name, tool in agent._function_toolset.tools.items()}
