"""Offline tests for the model gate: orchestrator runs go before in-process job requests."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.llm_provider import LLMCallCancelled, LLMProvider
from core.model_gate import ModelGate, model_gate
from tests.orchestrator_fakes import OrchestratorAppCase, ScriptedModel, background, run_body, user

# Long enough for a job request that ignores the gate to have been sent.
SETTLE_SECONDS = 0.3


def _outcome(call):
    try:
        return call()
    except Exception as exc:
        return exc


class FakeServer:
    """Stands in for the OpenAI client, recording each request the provider sends."""

    def __init__(self):
        self.sent = []
        self.client = MagicMock()
        self.client.chat.completions.create.side_effect = self._create
        self._patch = patch("core.llm_provider.OpenAI", return_value=self.client)

    def _create(self, **kwargs):
        self.sent.append(kwargs["messages"][0]["content"])
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])])

    def provider(self):
        return LLMProvider(model="m", base_url="http://model.test/v1", api_key="k")


class ModelGateTests(unittest.TestCase):
    def test_priority_is_released_when_the_run_raises(self):
        gate = ModelGate()
        with self.assertRaises(RuntimeError), gate.priority():
            self.assertTrue(gate.busy())
            raise RuntimeError("run failed")
        self.assertFalse(gate.busy())
        self.assertTrue(gate.wait_until_free())

    def test_a_set_cancel_event_ends_the_wait(self):
        gate = ModelGate()
        cancel = threading.Event()
        cancel.set()
        with gate.priority():
            self.assertFalse(gate.wait_until_free(cancel))


class JobRequestGateTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server._patch.start()
        self.addCleanup(self.server._patch.stop)

    def test_job_requests_wait_while_a_run_has_priority(self):
        provider = self.server.provider()
        with model_gate.priority():
            cancelable = background(lambda: provider.generate_cancelable("cancelable", threading.Event(), max_retries=0))
            plain = background(lambda: provider.generate("plain", max_retries=0))
            time.sleep(SETTLE_SECONDS)
            self.assertEqual(self.server.sent, [])
        cancelable["thread"].join(timeout=5)
        plain["thread"].join(timeout=5)

        self.assertEqual((cancelable["result"], plain["result"]), ("ok", "ok"))
        self.assertEqual(sorted(self.server.sent), ["cancelable", "plain"])

    def test_stopping_a_job_while_it_waits_is_honoured(self):
        provider = self.server.provider()
        cancel = threading.Event()
        with model_gate.priority():
            job = background(lambda: _outcome(lambda: provider.generate_cancelable("job", cancel, max_retries=0)))
            time.sleep(SETTLE_SECONDS)
            cancel.set()
            job["thread"].join(timeout=2)
            self.assertFalse(job["thread"].is_alive())

        self.assertIsInstance(job["result"], LLMCallCancelled)
        self.assertEqual(self.server.sent, [])


class OrchestratorGateTests(OrchestratorAppCase):
    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        self.server._patch.start()
        self.addCleanup(self.server._patch.stop)

    def test_job_request_waits_for_the_orchestrator_run_to_end(self):
        gate = threading.Event()
        self.use_model(ScriptedModel(["Answer first."], gate=gate))
        run = background(lambda: self.client.post(self.url(), json=run_body([user("Chat")])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))
        provider = self.server.provider()
        job = background(lambda: provider.generate_cancelable("job", threading.Event(), max_retries=0))

        time.sleep(SETTLE_SECONDS)
        waited = list(self.server.sent)
        gate.set()
        run["thread"].join(timeout=5)
        job["thread"].join(timeout=5)

        self.assertEqual(waited, [])
        self.assertEqual(job["result"], "ok")
        self.assertFalse(model_gate.busy())

    def test_the_gate_is_released_when_a_run_errors_or_is_stopped(self):
        self.use_model(ScriptedModel([RuntimeError("model offline")]))
        self.post_turn(run_body([user("Fail")]))
        self.assertFalse(model_gate.busy())

        self.use_model(ScriptedModel(["Never."], gate=threading.Event()))
        run = background(lambda: self.client.post(self.url(), json=run_body([user("Stop me")])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))
        self.assertTrue(model_gate.busy())
        self.client.post(self.url("/stop"))
        run["thread"].join(timeout=5)

        self.assertFalse(model_gate.busy())


if __name__ == "__main__":
    unittest.main()
