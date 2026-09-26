"""Offline tests for the world-step orchestrator tools."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.registry import build_agent
from webui.orchestrator.tools.world import (
    world_chat,
    world_guide_reset,
    world_rebuild,
    world_set_enabled,
    world_status_summary,
    world_stop,
)
from webui.task_runner import TaskRecord


def payload(result):
    return json.loads(result)


class WorldToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self._tmp.name
        self.runtime = SimpleNamespace(
            world_chat=MagicMock(),
            tasks=MagicMock(),
            ensure_world_chat_can_start=MagicMock(),
            ensure_world_task_can_start=MagicMock(),
        )
        self.runtime.world_chat.guide_status.return_value = {
            "exists": False, "path": "file_system/world_knowledge/chat_guide.md",
        }
        deps = OrchestratorDeps(runtime=self.runtime, workspace="my book", phase="world")
        self.ctx = SimpleNamespace(deps=deps)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_world_chat_starts_the_job_after_the_guard(self):
        result = payload(world_chat(self.ctx, "  Add a harbour district.  "))

        self.runtime.ensure_world_chat_can_start.assert_called_once_with("my book")
        self.runtime.world_chat.start_message.assert_called_once_with("my book", "Add a harbour district.")
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["job"]["kind"], "world")
        self.assertEqual(result["job"]["url"], "/api/workspaces/my%20book/world-knowledge/job")

    def test_world_chat_refuses_while_a_build_runs(self):
        self.runtime.ensure_world_chat_can_start.side_effect = ValueError("Stop the build first.")

        result = payload(world_chat(self.ctx, "hello"))

        self.assertEqual(result, {"status": "refused", "message": "Stop the build first."})
        self.runtime.world_chat.start_message.assert_not_called()

    def test_world_chat_refuses_when_a_chat_job_is_busy(self):
        self.runtime.world_chat.start_message.side_effect = ValueError("already running")
        self.assertEqual(payload(world_chat(self.ctx, "hello"))["status"], "refused")

    def test_world_chat_asks_the_model_for_a_message(self):
        with self.assertRaises(ModelRetry):
            world_chat(self.ctx, "   ")

    def test_refusals_are_capped(self):
        self.runtime.ensure_world_chat_can_start.side_effect = ValueError("x" * 5000)
        self.assertLess(len(world_chat(self.ctx, "hello")), 700)

    def test_unexpected_errors_are_refused_and_redacted(self):
        secret = RuntimeError("disk full api_key=sk-abcdefghijklmnopqrstuvwxyz")
        self.runtime.world_chat.start_message.side_effect = secret
        self.runtime.world_chat.stop.side_effect = secret
        self.runtime.world_chat.reset_guide.side_effect = secret
        self.runtime.world_chat.guide_status.side_effect = secret
        self.runtime.tasks.create.side_effect = secret
        for call in (
            lambda: world_chat(self.ctx, "hello"), lambda: world_stop(self.ctx),
            lambda: world_guide_reset(self.ctx), lambda: world_status_summary(self.ctx),
            lambda: world_rebuild(self.ctx),
        ):
            result = payload(call())
            self.assertEqual(result["status"], "refused")
            self.assertIn("disk full", result["message"])
            self.assertNotIn("abcdefghijklmnop", result["message"])

    def test_set_enabled_refuses_unexpected_errors(self):
        with patch("core.world_knowledge.set_world_knowledge_enabled", side_effect=OSError("read-only")):
            self.assertEqual(payload(world_set_enabled(self.ctx, True))["status"], "refused")

    def test_world_stop(self):
        self.runtime.world_chat.stop.return_value = {"status": "stopping"}
        self.assertEqual(payload(world_stop(self.ctx)), {"status": "done", "message": "World chat stopping."})
        self.runtime.world_chat.stop.side_effect = ValueError("There is no generation task to stop.")
        self.assertEqual(payload(world_stop(self.ctx))["status"], "refused")

    def test_set_enabled_and_status_summary(self):
        self.assertEqual(payload(world_set_enabled(self.ctx, False))["message"], "World knowledge disabled.")
        summary = payload(world_status_summary(self.ctx))["message"]
        self.assertIn("Enabled: no.", summary)
        self.assertIn("Final sections: 0 of", summary)
        self.assertIn("Chat guide: none.", summary)
        self.assertTrue((Path(self._tmp.name) / "my book" / "file_system").is_dir())

        world_set_enabled(self.ctx, True)
        self.assertIn("Enabled: yes.", payload(world_status_summary(self.ctx))["message"])

    def test_world_rebuild_starts_a_task_after_the_guard(self):
        self.runtime.tasks.create.return_value = TaskRecord(id="t1", type="world_build", label="Build", workspace="my book")

        result = payload(world_rebuild(self.ctx, force=True))

        self.runtime.ensure_world_task_can_start.assert_called_once_with("my book")
        self.runtime.tasks.create.assert_called_once_with("world_build", "my book", {"force": True})
        self.assertEqual(result["job"], {"kind": "task", "workspace": "my book", "task_id": "t1", "url": "/api/tasks/t1"})

    def test_world_rebuild_refuses_while_the_chat_runs(self):
        self.runtime.ensure_world_task_can_start.side_effect = ValueError("Stop the chat first.")
        self.assertEqual(payload(world_rebuild(self.ctx))["status"], "refused")
        self.runtime.tasks.create.assert_not_called()
        self.runtime.ensure_world_task_can_start.side_effect = None
        self.runtime.tasks.create.side_effect = ValueError("already has a running task")
        self.assertEqual(payload(world_rebuild(self.ctx))["status"], "refused")

    def test_world_guide_reset(self):
        self.assertEqual(payload(world_guide_reset(self.ctx))["status"], "done")
        self.runtime.world_chat.reset_guide.assert_called_once_with("my book")

    def test_approval_flags(self):
        tools = build_agent("world", TestModel())._function_toolset.tools
        approvals = {name: tool.requires_approval for name, tool in tools.items()}
        self.assertEqual(approvals, {
            "list_artifacts": False, "read_artifact": False, "job_status": False,
            "world_chat": True, "world_stop": False, "world_status_summary": False,
            "world_set_enabled": False, "world_rebuild": True, "world_guide_reset": True,
        })


if __name__ == "__main__":
    unittest.main()
