import io
import os
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openai import Timeout

from core.config import ConfigLoader
from core.llm_provider import LLMProvider


def _delta_event(text, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish_reason)]
    )


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

    def _generate_captured(self, events, max_tokens=None):
        client = MagicMock()
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return iter(events)

        client.chat.completions.create.side_effect = create
        buffer = io.StringIO()
        with patch("core.llm_provider.OpenAI", return_value=client):
            provider = LLMProvider(model="m", api_key="k", max_tokens=max_tokens)
            with redirect_stdout(buffer):
                text = provider.generate("prompt", max_retries=2)
        return text, buffer.getvalue(), captured, client

    def test_env_max_tokens_is_sent_on_the_request(self):
        client = MagicMock()
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return iter([_delta_event("done", "stop")])

        client.chat.completions.create.side_effect = create
        overrides = {
            "ADAPTIVE_BUILDER_MODEL": "test-model",
            "ADAPTIVE_BUILDER_BASE_URL": "http://example.test/v1",
            "ADAPTIVE_BUILDER_API_KEY": "test-key",
            "ADAPTIVE_BUILDER_MAX_TOKENS": "32768",
        }
        with patch.dict(os.environ, overrides, clear=False), \
             patch("core.config._load_env", return_value={}), \
             patch("core.llm_provider.OpenAI", return_value=client):
            ConfigLoader.reload()
            config = ConfigLoader.get_adaptive_builder_config()
            provider = LLMProvider(
                model="m",
                api_key="k",
                max_tokens=config["max_tokens"],
            )
            text = provider.generate("prompt", max_retries=0)
        ConfigLoader.reload()
        self.assertEqual(config["max_tokens"], 32768)
        self.assertEqual(text, "done")
        self.assertEqual(captured["max_tokens"], 32768)

    def test_unset_or_invalid_max_tokens_sends_none(self):
        for raw in (None, "", "0", "nope"):
            with self.subTest(raw=raw):
                overrides = {
                    "DATA_BUILDER_MODEL": "test-model",
                    "DATA_BUILDER_BASE_URL": "http://example.test/v1",
                    "DATA_BUILDER_API_KEY": "test-key",
                }
                if raw is not None:
                    overrides["DATA_BUILDER_MAX_TOKENS"] = raw
                with patch.dict(os.environ, overrides, clear=False), \
                     patch("core.config._load_env", return_value={}):
                    os.environ.pop("DATA_BUILDER_MAX_TOKENS", None)
                    if raw is not None:
                        os.environ["DATA_BUILDER_MAX_TOKENS"] = raw
                    ConfigLoader.reload()
                    config = ConfigLoader.get_data_builder_config()
                ConfigLoader.reload()
                self.assertIsNone(config["max_tokens"])
                text, output, captured, client = self._generate_captured(
                    [_delta_event("ok", "stop")],
                    max_tokens=config["max_tokens"],
                )
                self.assertEqual(text, "ok")
                self.assertIn("max_tokens", captured)
                self.assertIsNone(captured["max_tokens"])
                self.assertNotIn("truncated", output)
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_length_finish_warns_and_returns_text(self):
        text, output, captured, client = self._generate_captured([
            _delta_event("partial", None),
            _delta_event(None, "length"),
        ])
        self.assertEqual(text, "partial")
        self.assertIsNone(captured["max_tokens"])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertIn("hit the max_tokens limit (server default)", output)
        self.assertIn("truncated", output)
        self.assertIn("<SLOT>_MAX_TOKENS", output)

        text, output, captured, client = self._generate_captured(
            [_delta_event("partial", "length")],
            max_tokens=32768,
        )
        self.assertEqual(text, "partial")
        self.assertEqual(captured["max_tokens"], 32768)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertIn("hit the max_tokens limit (32768)", output)
        self.assertIn("truncated", output)
        self.assertIn("<SLOT>_MAX_TOKENS", output)
        self.assertNotIn("server default", output)

    def test_stop_finish_prints_no_truncation_warning(self):
        text, output, captured, client = self._generate_captured([
            _delta_event("Hello", None),
            _delta_event(" world", "stop"),
        ], max_tokens=128)
        self.assertEqual(text, "Hello world")
        self.assertEqual(captured["max_tokens"], 128)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn("truncated", output)
        self.assertNotIn("<SLOT>_MAX_TOKENS", output)
        self.assertNotIn("max_tokens limit", output)


if __name__ == "__main__":
    unittest.main()
