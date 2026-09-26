"""Offline tests for the orchestrator AG-UI routes: SSE sequences, approvals, conflicts."""
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from pydantic_ai import RunContext, Tool

from tests.orchestrator_fakes import (
    OrchestratorAppCase,
    ScriptedModel,
    background,
    event_types,
    first,
    run_body,
    sse_events,
    user,
)
from webui.orchestrator.registry import TOOL_RULE
from webui.orchestrator.tools import world


class OrchestratorRouteTests(OrchestratorAppCase):
    def test_text_turn_streams_run_and_text_events(self):
        self.use_model(ScriptedModel(["Hello, author."]))

        events = self.post_turn(run_body([user("Hi")]))

        self.assertEqual(
            event_types(events),
            ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CONTENT",
             "TEXT_MESSAGE_END", "RUN_FINISHED"],
        )
        text = "".join(event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT")
        self.assertEqual(text, "Hello, author.")
        self.assertEqual(events[-1]["outcome"]["type"], "success")

    def test_tool_call_turn_runs_list_artifacts_and_streams_its_result(self):
        (self.root / "book" / "file_system" / "notes.md").write_text("# Notes\n", encoding="utf-8")
        scripted = self.use_model(ScriptedModel([("list_artifacts", {"prefix": "file_system"}), "One file."]))

        events = self.post_turn(run_body([user("What files are there?")]))

        self.assertEqual(
            event_types(events),
            # The empty text message is the parent the adapter gives the tool call, as live.
            ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_END", "TOOL_CALL_START", "TOOL_CALL_ARGS",
             "TOOL_CALL_END", "TOOL_CALL_RESULT", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
             "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED"],
        )
        self.assertEqual(first(events, "TOOL_CALL_START")["toolCallName"], "list_artifacts")
        self.assertIn("file_system/notes.md", first(events, "TOOL_CALL_RESULT")["content"])
        self.assertEqual(len(scripted.requests), 2)

    def test_agent_gets_the_phase_instructions_and_ui_state(self):
        seen = {}

        def remember(ctx: RunContext, key: str) -> str:
            """Report one ui_state value."""
            seen["state"] = dict(ctx.deps.ui_state)
            return str(ctx.deps.ui_state.get(key))

        scripted = self.use_model(ScriptedModel([("remember", {"key": "volume"}), "Volume 2."]))
        with patch.object(world, "TOOLS", [Tool(remember)]):
            events = self.post_turn(run_body([user("Which volume?")], state={"volume": 2, "arc": 3}))

        self.assertEqual(seen["state"], {"volume": 2, "arc": 3})
        self.assertEqual(first(events, "TOOL_CALL_RESULT")["content"], "2")
        self.assertIn(world.INSTRUCTIONS, scripted.instructions[0])
        self.assertIn(TOOL_RULE, scripted.instructions[0])

    def test_client_declared_tools_are_not_offered_to_the_model(self):
        scripted = self.use_model(ScriptedModel(["Hi."]))
        body = run_body([user("Hi")])
        body["tools"] = [{"name": "ask_author", "description": "Ask.", "parameters": {"type": "object"}}]

        self.post_turn(body)

        self.assertNotIn("ask_author", scripted.tools[0])
        self.assertIn("list_artifacts", scripted.tools[0])

    def test_approval_tool_interrupts_and_resumes_after_approval(self):
        erased = []

        def erase_notes(path: str) -> str:
            """Erase a notes file."""
            erased.append(path)
            return "Erased %s." % path

        self.use_model(ScriptedModel([("erase_notes", {"path": "notes.md"}), "Erased it."]))
        with patch.object(world, "TOOLS", [Tool(erase_notes, requires_approval=True)]):
            first = self.post_turn(run_body([user("Erase my notes.")]))
            outcome = first[-1]["outcome"]
            self.assertEqual(outcome["type"], "interrupt")
            self.assertEqual(erased, [])
            self.assertFalse(self.history()["running"])

            resume = [{"interruptId": outcome["interrupts"][0]["id"], "status": "resolved",
                       "payload": {"approved": True}}]
            second = self.post_turn(run_body(resume=resume))

        self.assertEqual(
            event_types(first),
            ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_END", "TOOL_CALL_START", "TOOL_CALL_ARGS",
             "TOOL_CALL_END", "RUN_FINISHED"],
        )
        self.assertEqual(event_types(second)[:2], ["RUN_STARTED", "TOOL_CALL_RESULT"])
        self.assertEqual(second[1]["content"], "Erased notes.md.")
        self.assertEqual(second[-1]["outcome"]["type"], "success")
        self.assertEqual(erased, ["notes.md"])
        messages = self.history()["messages"]
        kinds = [message.get("activityType", message["role"]) for message in messages]
        self.assertEqual(kinds, ["user", "assistant", "interrupt", "approval_decision", "tool", "assistant"])
        call_id = outcome["interrupts"][0]["toolCallId"]
        self.assertEqual(messages[3]["content"], {"toolCallId": call_id, "approved": True, "reason": None})

    def test_second_run_on_a_busy_thread_is_refused(self):
        gate = threading.Event()
        self.use_model(ScriptedModel(["Slow answer."], gate=gate))
        first = background(lambda: self.client.post(self.url(), json=run_body([user("One")])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))

        second = self.client.post(self.url(), json=run_body([user("Two")]))
        cleared = self.client.delete(self.url("/history"))
        self.assertTrue(self.history()["running"])
        gate.set()
        first["thread"].join(timeout=5)

        self.assertEqual(second.status_code, 409)
        self.assertEqual(cleared.status_code, 409)
        self.assertEqual(first["result"].status_code, 200)
        self.assertFalse(self.history()["running"])

    def test_workspace_root_and_delete_are_refused_while_a_run_is_active(self):
        gate = threading.Event()
        self.use_model(ScriptedModel(["Slow answer."], gate=gate))
        first = background(lambda: self.client.post(self.url(), json=run_body([user("One")])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))

        with self.assertRaises(ValueError):
            self.runtime.set_workspace_root(str(self.root / "elsewhere"))
        with self.assertRaises(ValueError):
            self.runtime.delete_workspace("book")
        gate.set()
        first["thread"].join(timeout=5)

        self.assertTrue((self.root / "book").is_dir())

    def test_client_disconnect_does_not_cancel_or_lose_the_turn(self):
        gate = threading.Event()
        self.use_model(ScriptedModel(["Finished anyway."], gate=gate))
        disconnect = asyncio.Event()
        requests = [{"type": "http.request", "body": json.dumps(run_body([user("Keep going")])).encode()}]
        sent = []

        async def receive():
            if requests:
                return requests.pop()
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnect.set()  # hang up after the first chunk, while the model is still gated

        async def scenario():
            await self.app(_asgi_post(self.url()), receive, send)
            self.assertTrue(self.runtime.orchestrator.is_running("book", "world"))
            gate.set()
            while self.runtime.orchestrator.is_running("book", "world"):
                await asyncio.sleep(0.01)

        asyncio.run(scenario())

        messages = self.history()["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["content"], "Finished anyway.")

    def test_stream_replays_the_last_run(self):
        self.use_model(ScriptedModel(["Replay me."]))
        posted = self.post_turn(run_body([user("Hi")]))

        replayed = self.client.get(self.url("/stream"))

        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(sse_events(replayed.text), posted)
        self.assertEqual(self.client.get(self.url("/stream", phase="arcs")).status_code, 404)

    def test_unknown_phase_is_not_found(self):
        self.use_model(ScriptedModel(["Never."]))
        for phase in ("reference", "World", "worlds"):
            self.assertEqual(self.client.post(self.url(phase=phase), json=run_body([user("Hi")])).status_code, 404)
            self.assertEqual(self.client.get(self.url("/history", phase=phase)).status_code, 404)
            self.assertEqual(self.client.delete(self.url("/history", phase=phase)).status_code, 404)

    def test_missing_workspace_is_not_found(self):
        self.use_model(ScriptedModel(["Never."]))
        response = self.client.post(self.url(workspace="ghost"), json=run_body([user("Hi")]))
        self.assertEqual(response.status_code, 404)

    def test_missing_model_config_is_a_bad_request(self):
        def no_model():
            raise ValueError("No usable model is configured.")

        self.runtime.orchestrator.model_factory = no_model
        response = self.client.post(self.url(), json=run_body([user("Hi")]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.history()["messages"], [])


def _asgi_post(path):
    headers = [(b"content-type", b"application/json"), (b"accept", b"text/event-stream")]
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": headers, "client": ("test", 1), "server": ("test", 80),
    }


if __name__ == "__main__":
    unittest.main()
