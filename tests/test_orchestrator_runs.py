"""Offline tests for orchestrator run control: stop, approvals, conflicts and the history contract."""
import json
import threading
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel

from tests.orchestrator_fakes import (
    OrchestratorAppCase,
    ScriptedModel,
    background,
    first,
    run_body,
    sse_events,
    user,
)
from webui.orchestrator.deps import JobRef
from webui.orchestrator.pending import AUTO_DENIED
from webui.orchestrator.runs import AWAITING_APPROVAL, BUSY, DUPLICATE_AUTO_CONTINUE, STOPPED, OrchestratorRuns
from webui.orchestrator.tools import world
from webui.orchestrator.tools.results import job_started
from webui.orchestrator.transcript import STOP_CODE


def _parts(message):
    return [(type(part).__name__, part.content) for part in message.parts]


class _LengthStop(FunctionModel):
    """A streamed reply that stops for length, so pydantic-ai raises its token-limit error."""

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with super().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            response.finish_reason = "length"
            yield response


class OrchestratorRunTests(OrchestratorAppCase):
    def setUp(self):
        super().setUp()
        self.erased = []

    def erase_notes(self, path: str) -> str:
        """Erase a notes file."""
        self.erased.append(path)
        return "Erased %s." % path

    def approval_tools(self):
        return patch.object(world, "TOOLS", [Tool(self.erase_notes, requires_approval=True)])

    def resume(self, interrupt_id, payload):
        return run_body(resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": payload}])

    def start_gated(self, scripted, text="One"):
        self.use_model(scripted)
        box = background(lambda: self.client.post(self.url(), json=run_body([user(text)])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))
        return box

    def auto_body(self, text, job=True):
        body = run_body([user(text)])
        body["forwardedProps"] = {"autoContinue": job}
        return body

    def context_prompts(self):
        messages = self.runtime.orchestrator.history.load_context("book", "world")
        return [part.content for m in messages for part in m.parts if isinstance(part, UserPromptPart)]

    def test_stop_cancels_the_run_and_keeps_the_partial_turn(self):
        box = self.start_gated(ScriptedModel(["Never sent."], gate=threading.Event()), "Stop me")

        stopped = self.client.post(self.url("/stop"))
        box["thread"].join(timeout=5)

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json(), {"running": False})
        events = sse_events(box["result"].text)
        self.assertEqual((events[-1]["type"], events[-1]["message"]), ("RUN_ERROR", STOPPED))
        self.assertEqual(events[-1]["code"], STOP_CODE)
        self.assertEqual(sse_events(self.client.get(self.url("/stream")).text), events)
        messages = self.history()["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "activity"])
        self.assertEqual(messages[1]["activityType"], STOP_CODE)
        self.assertEqual(messages[1]["content"], {"message": STOPPED})
        self.assertEqual(self.context_prompts(), ["Stop me"])
        self.assertEqual(self.client.post(self.url("/stop")).status_code, 404)

    def test_new_message_denies_a_pending_approval_and_continues(self):
        scripted = self.use_model(ScriptedModel([("erase_notes", {"path": "notes.md"}), "Doing X instead."]))
        with self.approval_tools():
            interrupted = self.post_turn(run_body([user("Erase my notes.")]))
            events = self.post_turn(run_body([user("Never mind, do X.")]))

        self.assertEqual(interrupted[-1]["outcome"]["type"], "interrupt")
        self.assertEqual(events[-1]["outcome"]["type"], "success")
        self.assertEqual(first(events, "TOOL_CALL_RESULT")["content"], AUTO_DENIED)
        self.assertEqual(self.erased, [])
        self.assertEqual(
            _parts(scripted.requests[-1][-1]),
            [("ToolReturnPart", AUTO_DENIED), ("UserPromptPart", "Never mind, do X.")],
        )
        history = self.history()
        roles = [m.get("activityType", m["role"]) for m in history["messages"]]
        self.assertEqual(roles, ["user", "assistant", "interrupt", "auto_denied", "user", "tool", "assistant"])
        self.assertEqual(history["pending_interrupts"], [])

    def test_denial_tells_the_model_the_author_denied_it_and_is_recorded(self):
        steps = [("erase_notes", {"path": "a.md"}), "Kept a.", ("erase_notes", {"path": "b.md"}), "Kept b."]
        scripted = self.use_model(ScriptedModel(steps))
        with self.approval_tools():
            outcome = self.post_turn(run_body([user("Erase a.")]))[-1]["outcome"]
            reasoned = self.post_turn(self.resume(outcome["interrupts"][0]["id"], {"approved": False, "reason": "Keep them."}))
            outcome = self.post_turn(run_body([user("Erase b.")]))[-1]["outcome"]
            bare = self.post_turn(self.resume(outcome["interrupts"][0]["id"], {"approved": False}))

        framed = "The author denied this call. Their reason: Keep them."
        self.assertEqual(self.erased, [])
        self.assertEqual(first(reasoned, "TOOL_CALL_RESULT")["content"], framed)
        self.assertEqual(first(bare, "TOOL_CALL_RESULT")["content"], "The author denied this call.")
        self.assertEqual(_parts(scripted.requests[1][-1]), [("ToolReturnPart", framed)])
        decisions = [m["content"] for m in self.history()["messages"] if m.get("activityType") == "approval_decision"]
        self.assertEqual([(d["approved"], d["reason"]) for d in decisions], [(False, "Keep them."), (False, None)])
        self.assertEqual(decisions[1]["toolCallId"], outcome["interrupts"][0]["toolCallId"])

    def test_resume_without_a_matching_pending_approval_is_refused(self):
        self.use_model(ScriptedModel([("erase_notes", {"path": "notes.md"})]))
        nothing = self.client.post(self.url(), json=self.resume("int-call_9", {"approved": True}))
        with self.approval_tools():
            self.post_turn(run_body([user("Erase my notes.")]))
            unknown = self.client.post(self.url(), json=self.resume("int-call_9", {"approved": True}))
        malformed = self.client.post(self.url(), json=self.resume("call_1", {"approved": True}))

        self.assertEqual(nothing.status_code, 409)
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(self.erased, [])
        self.assertEqual(len(self.history()["pending_interrupts"]), 1)

    def test_auto_continue_runs_once_per_job(self):
        self.use_model(ScriptedModel(["Reviewed.", "Reviewed the other."]))
        job = {"toolCallId": "call_7", "jobKey": "design:concept:abc"}

        once = self.client.post(self.url(), json=self.auto_body("Job finished.", job))
        again = self.client.post(self.url(), json=self.auto_body("Job finished.", job))
        other = self.client.post(self.url(), json=self.auto_body("Other finished.", {"jobKey": "arcs:1:def"}))

        self.assertEqual((once.status_code, again.status_code, other.status_code), (200, 409, 200))
        self.assertEqual(again.json()["detail"], DUPLICATE_AUTO_CONTINUE)
        activities = [m["content"] for m in self.history()["messages"] if m.get("activityType") == "auto_continue"]
        self.assertEqual(activities, [
            {"message": "Job finished.", "toolCallId": "call_7", "jobKey": "design:concept:abc"},
            {"message": "Other finished.", "jobKey": "arcs:1:def"},
        ])

    def test_start_itself_refuses_a_second_run(self):
        gate = threading.Event()
        box = self.start_gated(ScriptedModel(["Slow."], gate=gate))

        with patch.object(OrchestratorRuns, "is_running", return_value=False):
            second = self.client.post(self.url(), json=run_body([user("Two")]))
        gate.set()
        box["thread"].join(timeout=5)

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], BUSY)
        self.assertEqual([m["content"] for m in self.history()["messages"] if m["role"] == "user"], ["One"])

    def test_history_reports_run_offset_while_running_and_pending_interrupts_after(self):
        self.use_model(ScriptedModel(["Earlier answer."]))
        self.post_turn(run_body([user("Earlier")]))
        gate = threading.Event()
        with self.approval_tools():
            box = self.start_gated(ScriptedModel([("erase_notes", {"path": "n.md"})], gate=gate), "Erase")
            during = self.history()
            gate.set()
            box["thread"].join(timeout=5)
        after = self.history()

        self.assertTrue(during["running"])
        self.assertEqual(during["run_offset"], 3)
        self.assertEqual([m["role"] for m in during["messages"]], ["user", "assistant", "user"])
        self.assertEqual(during["pending_interrupts"], [])
        live = sse_events(box["result"].text)[-1]["outcome"]["interrupts"]
        self.assertFalse(after["running"])
        self.assertIsNone(after["run_offset"])
        self.assertEqual(after["pending_interrupts"], live)
        self.assertEqual(after["pending_interrupts"][0]["id"], "int-" + live[0]["toolCallId"])
        self.assertEqual([m["role"] for m in after["messages"][3:]], ["assistant", "activity"])

    def test_a_started_job_is_saved_before_the_follow_up_request(self):
        def start_job() -> str:
            """Start a background job."""
            return job_started(JobRef(kind="task", workspace="book", task_id="t1"), "Job started.")

        gate = threading.Event()
        scripted = ScriptedModel([("start_job", {}), "Started it."], gate=gate, gate_from=2)
        with patch.object(world, "TOOLS", [Tool(start_job)]):
            box = self.start_gated(scripted, "Start the job")
            self.wait_until(lambda: len(scripted.requests) >= 2)
            saved = (self.root / "book" / ".orchestrator" / "world" / "model_context.json").read_text()
            running = self.history()["running"]
            gate.set()
            box["thread"].join(timeout=5)

        self.assertTrue(running)
        parts = [part for message in json.loads(saved) for part in message["parts"]]
        kinds = [part["part_kind"] for part in parts]
        self.assertIn("tool-call", kinds)
        returned = [part for part in parts if part["part_kind"] == "tool-return"]
        self.assertEqual(json.loads(returned[0]["content"])["job"]["task_id"], "t1")

    def test_auto_continue_is_recorded_as_an_activity_but_prompts_the_model(self):
        scripted = self.use_model(ScriptedModel(["The job is done; next step."]))

        self.post_turn(self.auto_body("The job finished. Continue."))

        messages = self.history()["messages"]
        self.assertEqual(messages[0], {
            "id": messages[0]["id"], "role": "activity", "activityType": "auto_continue",
            "content": {"message": "The job finished. Continue."},
        })
        self.assertEqual([m["role"] for m in messages], ["activity", "assistant"])
        self.assertEqual(scripted.requests[0][-1].parts[-1].content, "The job finished. Continue.")
        self.assertEqual(self.context_prompts(), ["The job finished. Continue."])

    def test_auto_continue_never_sets_a_pending_approval_aside(self):
        self.use_model(ScriptedModel([("erase_notes", {"path": "notes.md"})]))
        with self.approval_tools():
            self.post_turn(run_body([user("Erase my notes.")]))
            response = self.client.post(self.url(), json=self.auto_body("The job finished. Continue."))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], AWAITING_APPROVAL)
        history = self.history()
        self.assertEqual(len(history["pending_interrupts"]), 1)
        self.assertNotIn("auto_continue", [m.get("activityType") for m in history["messages"]])
        self.assertEqual(self.erased, [])

    def test_a_request_limit_ends_with_a_plain_message(self):
        calls = {"n": 0}

        def summarize(messages, info):
            return ModelResponse(parts=[TextPart("SUMMARY")])

        async def always_list(messages, info):
            calls["n"] += 1
            yield {0: DeltaToolCall(name="list_artifacts", json_args="{}", tool_call_id="call_%d" % calls["n"])}

        self.runtime.orchestrator.model_factory = lambda: FunctionModel(summarize, stream_function=always_list)
        events = self.post_turn(run_body([user("Keep listing")]))

        message = "This reply used its maximum number of steps and stopped. Send a message to continue."
        self.assertGreater(calls["n"], 1)
        self.assertEqual((events[-1]["type"], events[-1]["message"]), ("RUN_ERROR", message))
        self.assertNotIn("request_limit", json.dumps(events))
        recorded = [m["content"]["message"] for m in self.history()["messages"] if m.get("activityType") == "run_error"]
        self.assertEqual(recorded, [message])
        replay = sse_events(self.client.get(self.url("/stream")).text)
        self.assertEqual(replay[-1]["message"], message)

    def test_a_token_limit_ends_with_a_plain_message(self):
        async def thinking_only(messages, info):
            yield {0: DeltaThinkingPart(content="still reasoning")}

        def summarize(messages, info):
            return ModelResponse(parts=[TextPart("SUMMARY")])

        self.runtime.orchestrator.model_factory = lambda: _LengthStop(summarize, stream_function=thinking_only)
        events = self.post_turn(run_body([user("Think hard")]))

        message = "The model ran out of room before it finished answering. Turn off Think, or ask for a shorter answer, and send again."
        self.assertEqual((events[-1]["type"], events[-1]["message"]), ("RUN_ERROR", message))
        self.assertNotIn("Model token limit", json.dumps(events))
        recorded = [m["content"]["message"] for m in self.history()["messages"] if m.get("activityType") == "run_error"]
        self.assertEqual(recorded, [message])
        replay = sse_events(self.client.get(self.url("/stream")).text)
        self.assertEqual(replay[-1]["message"], message)

    def test_other_run_errors_stay_redacted(self):
        self.use_model(ScriptedModel([RuntimeError("upstream refused api_key=sk-secretsecret123")]))

        events = self.post_turn(run_body([user("Hi")]))

        self.assertEqual(events[-1]["type"], "RUN_ERROR")
        self.assertIn("upstream refused", events[-1]["message"])
        self.assertIn("[REDACTED]", events[-1]["message"])
        self.assertNotIn("secretsecret", events[-1]["message"])
        self.assertNotIn("maximum number of steps", events[-1]["message"])
        self.assertNotIn("secretsecret", json.dumps(self.history()["messages"]))


if __name__ == "__main__":
    unittest.main()
