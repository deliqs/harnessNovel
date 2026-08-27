import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openai import Timeout

from core.llm_provider import LLMProvider


def _delta_event(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _message_response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class LLMProviderTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._old_timeout = os.environ.get("HARNESS_NOVEL_LLM_TIMEOUT")
        os.environ["HARNESS_NOVEL_LLM_TIMEOUT"] = "600"

    def tearDown(self):
        if self._old_timeout is None:
            os.environ.pop("HARNESS_NOVEL_LLM_TIMEOUT", None)
        else:
            os.environ["HARNESS_NOVEL_LLM_TIMEOUT"] = self._old_timeout

    def test_client_uses_idle_read_timeout(self):
        with patch("core.llm_provider.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            LLMProvider(model="m", api_key="k")
        timeout = mock_openai.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, Timeout)
        self.assertEqual(timeout.read, 600.0)
        self.assertLessEqual(timeout.connect, 30.0)
        self.assertLessEqual(timeout.write, 30.0)
        self.assertLessEqual(timeout.pool, 30.0)

    def test_generate_streams_and_joins_deltas(self):
        client = MagicMock()

        def create(**kwargs):
            self.assertTrue(kwargs.get("stream"))
            return iter([_delta_event("Hello"), _delta_event(None), _delta_event(" world")])

        client.chat.completions.create.side_effect = create
        with patch("core.llm_provider.OpenAI", return_value=client):
            provider = LLMProvider(model="m", api_key="k")
        self.assertEqual(provider.generate("prompt", max_retries=0), "Hello world")

    def test_generate_falls_back_when_stream_unsupported(self):
        client = MagicMock()

        def create(**kwargs):
            if kwargs.get("stream"):
                raise RuntimeError("stream is not supported by this endpoint")
            return _message_response("fallback text")

        client.chat.completions.create.side_effect = create
        with patch("core.llm_provider.OpenAI", return_value=client):
            provider = LLMProvider(model="m", api_key="k")
        self.assertEqual(provider.generate("prompt", max_retries=0), "fallback text")

    def test_generate_cancelable_streams(self):
        client = MagicMock()
        client.close = MagicMock()

        def create(**kwargs):
            self.assertTrue(kwargs.get("stream"))
            return iter([_delta_event("ok")])

        client.chat.completions.create.side_effect = create
        with patch("core.llm_provider.OpenAI", return_value=client):
            provider = LLMProvider(model="m", api_key="k")
            text = provider.generate_cancelable("prompt", threading.Event(), max_retries=0)
        self.assertEqual(text, "ok")

    def test_lm_studio_kwargs_disable_thinking(self):
        provider = LLMProvider(
            model="8-bit",
            base_url="http://127.0.0.1:1234/v1",
            api_key="lm-studio",
        )
        kwargs = provider._completion_kwargs("prompt", 0.7, False, None)
        extra = kwargs["extra_body"]
        self.assertEqual(extra["enable_thinking"], False)
        self.assertEqual(extra["chat_template_kwargs"]["enable_thinking"], False)

    def test_non_lm_studio_kwargs_omit_thinking(self):
        provider = LLMProvider(
            model="grok-4.6",
            base_url="http://127.0.0.1:8788/v1",
            api_key="grok-cli",
        )
        kwargs = provider._completion_kwargs("prompt", 0.7, False, None)
        self.assertNotIn("extra_body", kwargs)


if __name__ == "__main__":
    unittest.main()
