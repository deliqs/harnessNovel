"""Offline fixtures for the orchestrator tests: a scripted model, an SSE parser and an app."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx2
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from webui.app import create_app
from webui.orchestrator.model import ORCA_PROFILE


class ScriptedModel:
    """A FunctionModel whose streamed replies follow a script, one step per model request.

    A step is reply text, a `(tool_name, args)` tool call, a `Thought(reasoning, text)`, or an
    exception to raise. Once the script runs out the model answers "Done.". Non-streamed
    requests are compaction summaries. With a `gate`, request number `gate_from` and later ones
    wait until the gate is set.
    """

    def __init__(self, steps, summary="SUMMARY of the earlier turns.", gate=None, gate_from=1):
        self.steps = list(steps)
        self.summary = summary
        self.gate = gate
        self.gate_from = gate_from
        self.requests = []
        self.tools = []
        self.instructions = []
        self.settings = []
        self.summaries = 0
        self.model = FunctionModel(function=self._summarize, stream_function=self._stream)

    def _summarize(self, messages, info):
        self.summaries += 1
        return ModelResponse(parts=[TextPart("%s #%d" % (self.summary, self.summaries))])

    async def _stream(self, messages, info):
        self.requests.append(messages)
        self.instructions.append(info.instructions)
        self.settings.append(info.model_settings)
        self.tools.append([tool.name for tool in info.function_tools])
        while self.gate is not None and len(self.requests) >= self.gate_from and not self.gate.is_set():
            await asyncio.sleep(0.01)
        step = self.steps.pop(0) if self.steps else "Done."
        if isinstance(step, Exception):
            raise step
        if isinstance(step, Thought):
            yield {0: DeltaThinkingPart(content=step.reasoning)}
            yield step.text
            return
        if isinstance(step, tuple):
            name, args = step
            call_id = "call_%d" % len(self.requests)
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}
            return
        half = len(step) // 2
        yield step[:half]
        yield step[half:]


class FakeOrca:
    """An in-process OpenAI-compatible server behind the real OpenAIChatModel and ORCA_PROFILE.

    Streamed requests get a long reply and plain ones (compaction) a numbered summary. Usage is
    reported as a quarter of the request's characters, as a real server would roughly count it,
    since compaction measures the history from the last reported usage.
    """

    def __init__(self, summary="SUMMARY-OF-EARLIER-TURNS", reply_words=60):
        self.summary = summary
        self.reply_words = reply_words
        self.streamed = []
        self.prompt_tokens = []
        self.plain = []

    def model(self):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        provider = OpenAIProvider(base_url="http://orca.test/v1", api_key="local-only", http_client=client)
        return OpenAIChatModel("orca", provider=provider, profile=ORCA_PROFILE)

    def handle(self, request):
        body = json.loads(request.content)
        usage = {"prompt_tokens": len(request.content) // 4, "completion_tokens": 10, "total_tokens": 0}
        if not body.get("stream"):
            self.plain.append(body)
            text = "%s #%d" % (self.summary, len(self.plain))
            return httpx2.Response(200, json=_completion(text, usage))
        self.streamed.append(body)
        self.prompt_tokens.append(usage["prompt_tokens"])
        reply = "Answer %d. %s" % (len(self.streamed), "words " * self.reply_words)
        chunks = [_chunk({"role": "assistant", "content": reply}, None), {**_chunk({}, "stop"), "usage": usage}]
        sse = "".join("data: %s\n\n" % json.dumps(chunk) for chunk in chunks) + "data: [DONE]\n\n"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())


def _chunk(delta, finish_reason):
    return {
        "id": "chunk", "object": "chat.completion.chunk", "created": 0, "model": "orca",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _completion(text, usage):
    return {
        "id": "completion", "object": "chat.completion", "created": 0, "model": "orca", "usage": usage,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


class Thought:
    """A scripted step that reasons first, then answers."""

    def __init__(self, reasoning, text):
        self.reasoning = reasoning
        self.text = text


def sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def event_types(events):
    return [event["type"] for event in events]


def first(events, event_type):
    return next(event for event in events if event["type"] == event_type)


def run_body(messages=None, resume=None, state=None, thinking=None):
    body = {
        "threadId": "thread-1",
        "runId": "run-%s" % uuid.uuid4().hex[:8],
        "state": state or {},
        "messages": messages or [],
        "tools": [],
        "context": [],
        "forwardedProps": {} if thinking is None else {"thinking": thinking},
    }
    if resume:
        body["resume"] = resume
    return body


def user(text):
    return {"id": "user-%s" % uuid.uuid4().hex[:8], "role": "user", "content": text}


class OrchestratorAppCase(unittest.TestCase):
    """A web app on a temporary workspace root with one workspace, `book`."""

    phase = "world"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {key: os.environ.get(key) for key in (
            "HARNESS_NOVEL_HOME", "HARNESS_NOVEL_PROMPT_TRACE_MODE", "HARNESS_NOVEL_ORCHESTRATOR_COMPACT_TARGET",
        )}
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)
        web_home = self.root / "web-home"
        web_home.mkdir()
        self._patches = [
            patch("webui.app.WEB_HOME", web_home),
            patch("webui.app.WEB_SETTINGS_PATH", web_home / "settings.json"),
        ]
        for item in self._patches:
            item.start()
        (self.root / "book" / "file_system").mkdir(parents=True)
        self._gates = []
        self.app = create_app(str(self.root))
        self.runtime = self.app.state.runtime
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        # A failed assertion must not leave a gated model, and so the app, waiting forever.
        for gate in self._gates:
            gate.set()
        self.client.__exit__(None, None, None)
        for item in reversed(self._patches):
            item.stop()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def use_model(self, scripted):
        if scripted.gate is not None:
            self._gates.append(scripted.gate)
        self.runtime.orchestrator.model_factory = lambda: scripted.model
        return scripted

    def url(self, suffix="", phase=None, workspace="book"):
        return "/api/workspaces/%s/orchestrator/%s%s" % (workspace, phase or self.phase, suffix)

    def post_turn(self, body, phase=None):
        response = self.client.post(self.url(phase=phase), json=body, headers={"accept": "text/event-stream"})
        self.assertEqual(response.status_code, 200, response.text)
        return sse_events(response.text)

    def history(self, phase=None):
        response = self.client.get(self.url("/history", phase=phase))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_until(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition not reached in time")


def background(function):
    """Run `function` in a thread; the returned dict gets its `result` when it finishes."""
    box = {}

    def target():
        box["result"] = function()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    box["thread"] = thread
    return box
