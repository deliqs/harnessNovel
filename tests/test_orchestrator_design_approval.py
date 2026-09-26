"""Design chat through a real agent run: approval only for modes that change files, and tags read server-side."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.registry import build_agent
from webui.task_runner import UploadStore


def calling(args):
    """A model that calls design_chat with `args` on each new prompt, then answers "ok" once the tool returns."""

    def respond(messages, info):
        if not any(isinstance(part, UserPromptPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(parts=[ToolCallPart("design_chat", args, tool_call_id=f"call-{len(messages)}")])

    return FunctionModel(respond)


class DesignChatApprovalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.uploads = UploadStore(Path(self._tmp.name))
        runtime = SimpleNamespace(design_chat=MagicMock(), uploads=self.uploads)
        self.start = runtime.design_chat.start_message
        self.runtime = runtime

    def deps(self, phase):
        return OrchestratorDeps(runtime=self.runtime, workspace="book", phase=phase)

    def tearDown(self):
        self._tmp.cleanup()

    def run_agent(self, args, prompt="go", phase="stage", **kwargs):
        agent = build_agent(phase, calling(args))
        return asyncio.run(agent.run(prompt, deps=self.deps(phase), **kwargs))

    def upload(self, name, content):
        path = Path(self._tmp.name) / f"0a1b2c3d4e5f6a7b_{name}"
        path.write_text(content, encoding="utf-8")
        return self.uploads.register(path)

    def test_extend_is_deferred_until_approved_and_then_runs(self):
        first = self.run_agent({"message": "Add stage 4.", "mode": "extend"})

        self.assertIsInstance(first.output, DeferredToolRequests)
        self.assertEqual([call.tool_name for call in first.output.approvals], ["design_chat"])
        self.start.assert_not_called()

        approvals = DeferredToolResults(approvals={first.output.approvals[0].tool_call_id: True})
        second = self.run_agent(
            {"message": "Add stage 4.", "mode": "extend"}, prompt=None,
            message_history=first.all_messages(), deferred_tool_results=approvals,
        )
        self.assertEqual(second.output, "ok")
        self.assertEqual(self.start.call_args.kwargs["chat_mode"], "extend")

    def test_question_runs_without_approval(self):
        result = self.run_agent({"message": "Is stage 2 slow?", "mode": "question"})

        self.assertEqual(result.output, "ok")
        self.assertEqual(self.start.call_args.kwargs["chat_mode"], "question")

    def test_tags_reach_the_tool_after_an_approval_resume_although_the_model_omits_them(self):
        upload_id = self.upload("notes.md", "Tide notes.")
        tagged = f"[use_new_reference] [attached upload {upload_id}: notes.md]\nSync the new chapters."
        args = {"message": "Sync the new chapters."}
        first = self.run_agent(args, prompt=tagged, phase="design")
        self.assertIsInstance(first.output, DeferredToolRequests)

        approvals = DeferredToolResults(approvals={first.output.approvals[0].tool_call_id: True})
        second = self.run_agent(
            args, prompt=None, phase="design", message_history=first.all_messages(), deferred_tool_results=approvals,
        )

        self.assertEqual(second.output, "ok")
        call = self.start.call_args
        self.assertEqual(call.args[3], [{"name": "notes.md", "content": "Tide notes."}])
        self.assertTrue(call.kwargs["use_new_reference"])

    def test_tags_of_an_earlier_turn_are_not_reapplied(self):
        upload_id = self.upload("notes.md", "Tide notes.")
        args = {"message": "What is missing?", "mode": "question"}
        first = self.run_agent(args, prompt=f"[attached upload {upload_id}: notes.md]\nWhat is missing?", phase="design")
        self.assertEqual(len(self.start.call_args.args[3]), 1)

        self.run_agent(args, prompt="What is missing now?", phase="design", message_history=first.all_messages())

        self.assertEqual(self.start.call_args.args[3], [])

    def test_auto_turns_use_the_latest_author_prompt(self):
        upload_id = self.upload("notes.md", "Tide notes.")
        args = {"message": "Review it.", "mode": "critique"}
        first = self.run_agent(args, prompt=f"[attached upload {upload_id}: notes.md]\nReview it.", phase="design")

        self.run_agent(args, prompt="[auto] The job finished.", phase="design", message_history=first.all_messages())

        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.start.call_args.args[3], [{"name": "notes.md", "content": "Tide notes."}])


if __name__ == "__main__":
    unittest.main()
