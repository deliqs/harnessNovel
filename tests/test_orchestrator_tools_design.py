"""Offline tests for the design- and stage-step orchestrator tools."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic_ai import ApprovalRequired, ModelRetry
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.registry import build_agent
from webui.orchestrator.tools import design, design_ops, stage
from webui.task_runner import TaskRecord, UploadStore, WorkspaceStore


def payload(result):
    return json.loads(result)


class DesignToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        self.runtime = SimpleNamespace(
            design_chat=MagicMock(), tasks=MagicMock(), uploads=UploadStore(self.root / "uploads"),
            store=WorkspaceStore(self.root),
        )
        self.runtime.design_chat.job_status.return_value = {"status": "idle"}

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def ctx(self, phase="design", approved=True, prompt="Go."):
        deps = OrchestratorDeps(runtime=self.runtime, workspace="book", phase=phase)
        messages = [ModelRequest(parts=[UserPromptPart(prompt)])]
        return SimpleNamespace(deps=deps, tool_call_approved=approved, messages=messages)

    def upload(self, name, content):
        path = self.root / "uploads" / name
        path.write_text(content, encoding="utf-8")
        return self.runtime.uploads.register(path)

    def write_stage_design(self):
        base = self.root / "book" / "file_system" / "story_design"
        base.mkdir(parents=True, exist_ok=True)
        for name in ("long_mainline.md", "stage_roadmap.md"):
            (base / name).write_text("# Stage 1\n\nText.\n", encoding="utf-8")

    def test_design_chat_starts_a_concept_job(self):
        result = payload(design.design_chat(self.ctx(), "A tide-born hero."))

        self.runtime.design_chat.start_message.assert_called_once_with(
            "book", "concept", "A tide-born hero.", [],
            use_new_reference=False, sync_updated_design=False, chat_mode="chat",
        )
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["job"]["url"], "/api/workspaces/book/design/concept/job")

    def test_stage_design_chat_passes_extend_and_the_stage_scope(self):
        result = payload(stage.design_chat(self.ctx("stage"), "Add stage 4.", mode="extend"))

        args, kwargs = self.runtime.design_chat.start_message.call_args
        self.assertEqual(args[:2], ("book", "stage"))
        self.assertEqual(kwargs["chat_mode"], "extend")
        self.assertEqual(result["job"], {
            "kind": "design", "workspace": "book", "scope": "stage", "url": "/api/workspaces/book/design/stage/job",
        })

    def test_design_chat_reads_attachments_server_side(self):
        upload_id = self.upload("0a1b2c3d4e5f6a7b_notes.md", "SECRET NOTES " * 400)

        result = design.design_chat(self.ctx(), "", attachment_upload_ids=[upload_id])

        attachments = self.runtime.design_chat.start_message.call_args.args[3]
        self.assertEqual(attachments, [{"name": "notes.md", "content": "SECRET NOTES " * 400}])
        self.assertNotIn("SECRET", result)

    def test_design_chat_refuses_an_expired_tagged_upload(self):
        ctx = self.ctx(prompt="[attached upload 0123456789abcdef0123456789abcdef: gone.md]\nhi")
        self.assertEqual(payload(design.design_chat(ctx, "hi"))["status"], "refused")
        self.runtime.design_chat.start_message.assert_not_called()

    def test_design_chat_merges_valid_model_ids_and_drops_invalid_ones(self):
        tagged = self.upload("0a1b2c3d4e5f6a7b_tagged.md", "T")
        passed = self.upload("0a1b2c3d4e5f6a7c_passed.md", "P")
        ctx = self.ctx(prompt=f"[attached upload {tagged}: tagged.md]\nhi")
        design.design_chat(ctx, f"[attached upload {tagged}: tagged.md]\nhi",
                           attachment_upload_ids=[passed, "0123abc", tagged])
        args = self.runtime.design_chat.start_message.call_args.args
        self.assertEqual(args[2], "hi")
        self.assertEqual([att["name"] for att in args[3]], ["tagged.md", "passed.md"])

    def test_tag_flags_force_the_arguments_and_still_refuse_read_only_modes(self):
        ctx = self.ctx(prompt="[sync_updated_design]\nSync.")
        self.assertEqual(payload(stage.design_chat(ctx, "Is it slow?", mode="question"))["status"], "refused")
        stage.design_chat(ctx, "Sync.")
        self.assertTrue(self.runtime.design_chat.start_message.call_args.kwargs["sync_updated_design"])

    def test_design_chat_asks_the_model_for_content(self):
        with self.assertRaises(ModelRetry):
            design.design_chat(self.ctx(), "  ")
        empty = self.upload("0a1b2c3d4e5f6a7b_empty.md", "   ")
        with self.assertRaises(ModelRetry):
            design.design_chat(self.ctx(), "", attachment_upload_ids=[empty])
        design.design_chat(self.ctx(), "", use_new_reference=True)
        self.runtime.design_chat.start_message.assert_called_once()

    def test_design_chat_refuses_when_busy_and_caps_the_message(self):
        self.runtime.design_chat.start_message.side_effect = ValueError("already running " + "x" * 5000)
        result = design.design_chat(self.ctx(), "hi")
        self.assertEqual(payload(result)["status"], "refused")
        self.assertLess(len(result), 700)

    def test_design_chat_caps_attachment_sizes(self):
        big = self.upload("0a1b2c3d4e5f6a7b_big.md", "x" * (2 * 1024 * 1024 + 1))
        result = payload(design.design_chat(self.ctx(), "hi", attachment_upload_ids=[big]))
        self.assertEqual(result["status"], "refused")
        self.assertIn("2 MB", result["message"])
        parts = [self.upload(f"0a1b2c3d4e5f6a7{n}_part.md", "y" * (1536 * 1024)) for n in range(3)]
        result = payload(design.design_chat(self.ctx(), "hi", attachment_upload_ids=parts))
        self.assertEqual(result["status"], "refused")
        self.assertIn("4 MB", result["message"])
        self.runtime.design_chat.start_message.assert_not_called()

    def test_changing_modes_need_approval_and_read_only_modes_do_not(self):
        for mode in ("chat", "extend"):
            with self.assertRaises(ApprovalRequired):
                stage.design_chat(self.ctx("stage", approved=False), "More stages.", mode=mode)
        self.runtime.design_chat.start_message.assert_not_called()
        for mode in ("question", "critique"):
            result = payload(stage.design_chat(self.ctx("stage", approved=False), "Is it slow?", mode=mode))
            self.assertEqual(result["status"], "started")
            self.assertEqual(self.runtime.design_chat.start_message.call_args.kwargs["chat_mode"], mode)

    def test_read_only_modes_refuse_the_sync_flags(self):
        for flags in ({"use_new_reference": True}, {"sync_updated_design": True}):
            result = payload(design.design_chat(self.ctx(), "Is it slow?", mode="question", **flags))
            self.assertEqual(result["status"], "refused")
        self.runtime.design_chat.start_message.assert_not_called()

    def test_unexpected_errors_are_refused_and_redacted(self):
        self.runtime.design_chat.start_message.side_effect = RuntimeError("boom api_key=sk-abcdefghijklmnopqrstuvwxyz")
        result = payload(design.design_chat(self.ctx(), "hi"))
        self.assertEqual(result["status"], "refused")
        self.assertIn("boom", result["message"])
        self.assertNotIn("abcdefghijklmnop", result["message"])

    def test_design_continue_starts_a_stage_job(self):
        result = payload(design_ops.design_continue(self.ctx("stage")))
        self.runtime.design_chat.continue_incomplete.assert_called_once_with("book", "stage")
        self.assertEqual(result["job"]["url"], "/api/workspaces/book/design/stage/job")
        self.runtime.design_chat.continue_incomplete.side_effect = ValueError("nothing to continue")
        self.assertEqual(payload(design_ops.design_continue(self.ctx("stage")))["status"], "refused")

    def test_design_stop_and_reset(self):
        self.runtime.design_chat.stop.return_value = {"status": "stopping"}
        self.assertEqual(payload(design_ops.design_stop(self.ctx("stage")))["status"], "done")
        self.runtime.design_chat.stop.side_effect = ValueError("no task")
        self.assertEqual(payload(design_ops.design_stop(self.ctx("stage")))["status"], "refused")

        self.assertEqual(payload(design_ops.design_reset(self.ctx()))["status"], "done")
        self.runtime.design_chat.reset.assert_called_once_with("book", "concept")
        self.runtime.design_chat.reset.side_effect = ValueError("still running")
        self.assertEqual(payload(design_ops.design_reset(self.ctx()))["status"], "refused")

    def test_design_lenses_status(self):
        self.runtime.design_chat.lens_status.return_value = {
            "exists": True, "path": "file_system/story_design/lenses.md", "lenses": ["Pacing", "Stakes"],
        }
        message = payload(design_ops.design_lenses_status(self.ctx()))["message"]
        self.assertIn("Pacing, Stakes", message)
        self.assertIn("file_system/story_design/lenses.md", message)

    def test_regenerate_title_synopsis_needs_the_stage_design(self):
        self.assertEqual(payload(stage.regenerate_title_synopsis(self.ctx("stage")))["status"], "refused")
        self.write_stage_design()
        self.runtime.design_chat.job_status.return_value = {"status": "paused"}
        self.assertEqual(payload(stage.regenerate_title_synopsis(self.ctx("stage")))["status"], "refused")
        self.runtime.design_chat.job_status.assert_called_with("book", "stage")
        self.runtime.tasks.create.assert_not_called()

    def test_regenerate_title_synopsis_starts_a_task(self):
        self.write_stage_design()
        self.runtime.tasks.create.return_value = TaskRecord(id="t9", type="novel_name_synopsis", label="T", workspace="book")
        result = payload(stage.regenerate_title_synopsis(self.ctx("stage")))
        self.runtime.tasks.create.assert_called_once_with("novel_name_synopsis", "book", {"force": True})
        self.assertEqual(result["job"]["url"], "/api/tasks/t9")
        self.runtime.tasks.create.side_effect = ValueError("busy")
        self.assertEqual(payload(stage.regenerate_title_synopsis(self.ctx("stage")))["status"], "refused")

    def test_approval_flags(self):
        def approvals(phase):
            tools = build_agent(phase, TestModel())._function_toolset.tools
            return {name: tool.requires_approval for name, tool in tools.items()}

        shared = {"list_artifacts": False, "read_artifact": False, "job_status": False}
        self.assertEqual(approvals("design"), {
            **shared, "design_chat": False, "design_reset": True, "design_lenses_status": False,
        })
        self.assertEqual(approvals("stage"), {
            **shared, "design_chat": False, "design_continue": True,
            "design_stop": False, "design_reset": True, "design_lenses_status": False,
            "regenerate_title_synopsis": True,
        })

    def test_only_the_stage_tool_offers_extend(self):
        def modes(phase):
            tool = build_agent(phase, TestModel())._function_toolset.tools["design_chat"]
            return tool.function_schema.json_schema["properties"]["mode"]["enum"]

        self.assertEqual(modes("design"), ["chat", "question", "critique"])
        self.assertEqual(modes("stage"), ["chat", "extend", "question", "critique"])


if __name__ == "__main__":
    unittest.main()
