import os
import threading

import httpx
from openai import OpenAI
from core.text_utils import normalize_text
from core.prompt_trace import record_prompt

# HTTP status codes that are not worth retrying (auth / billing / deterministic errors).
_NO_RETRY_CODES = {401, 402, 403}
_CONNECT_TIMEOUT = 30.0


class LLMCallCancelled(RuntimeError):
    """The model request was cancelled by the user."""


def _is_timeout_error(exc):
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _is_stream_unsupported(exc):
    text = str(exc).lower()
    if "stream" not in text:
        return False
    return any(
        token in text
        for token in ("not support", "unsupported", "unknown", "unexpected", "invalid")
    )


def _collect_stream(stream):
    parts = []
    try:
        for event in stream:
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class LLMProvider:
    """Thin wrapper around an OpenAI-compatible API.

    Handles real API calls and retries only. On missing api_key or failed calls,
    returns an empty string and prints a warning. Never silently returns fake data.
    """

    def __init__(self, model="mock-model", base_url=None, api_key=None, max_tokens=None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.max_tokens = max_tokens
        try:
            self.timeout = max(
                30.0,
                float(os.getenv("HARNESS_NOVEL_LLM_TIMEOUT", "600")),
            )
        except (TypeError, ValueError):
            self.timeout = 600.0
        self.client = self._create_client() if self.api_key else None

    def _request_timeout(self):
        connect = min(_CONNECT_TIMEOUT, self.timeout)
        return httpx.Timeout(
            connect=connect, read=self.timeout, write=connect, pool=connect,
        )

    def _create_client(self):
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self._request_timeout(),
            # This wrapper owns retry count so pause/stop can take effect during SDK retries.
            max_retries=0,
        )

    def _run_completion(self, client, kwargs):
        """Stream by default so the timeout is idle time between tokens."""
        try:
            stream = client.chat.completions.create(stream=True, **kwargs)
            return _collect_stream(stream)
        except Exception as exc:
            if not _is_stream_unsupported(exc):
                raise
        response = client.chat.completions.create(stream=False, **kwargs)
        return response.choices[0].message.content

    def _completion_kwargs(self, prompt, temperature, is_json, max_tokens):
        kwargs = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if is_json:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def generate(self, prompt, temperature=0.7, is_json=False, max_retries=2, max_tokens=None):
        """Call the LLM and return generated content.

        On success, return normalized text. If api_key is missing or the API call
        fails (retries exhausted / 401/402/403), return an empty string and print a warning.
        """
        record_prompt(prompt, self.model)
        if not self.client:
            print("[LLMProvider] api_key is not configured; cannot call the model, returning empty content.")
            return ""

        print(f"[LLMProvider] Calling model {self.model} ...")
        kwargs = self._completion_kwargs(prompt, temperature, is_json, max_tokens)

        for attempt in range(max_retries + 1):
            try:
                return normalize_text(self._run_completion(self.client, kwargs))
            except Exception as e:
                status_code = getattr(e, 'status_code', None)
                if status_code in _NO_RETRY_CODES:
                    print(f"[LLMProvider] API error ({status_code}), not retryable.")
                    break
                kind = "timed out" if _is_timeout_error(e) else "failed"
                if attempt < max_retries:
                    print(f"[LLMProvider] API call {kind} (attempt {attempt+1}), retrying... error: {e}")
                else:
                    print(f"[LLMProvider] API call {kind}, retried {max_retries} times. error: {e}")

        print("[LLMProvider] Call failed, returning empty content (check API Key / balance / network).")
        return ""

    def generate_cancelable(
        self,
        prompt,
        cancel_event,
        temperature=0.7,
        is_json=False,
        max_tokens=None,
        max_retries=2,
    ):
        """Run a cancelable, retryable request; cancelled calls do not return partial content."""
        record_prompt(prompt, self.model)
        if not self.api_key:
            return ""
        for attempt in range(max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCallCancelled("Model request cancelled")
            done = threading.Event()
            outcome = {}
            client = self._create_client()

            def request():
                try:
                    kwargs = self._completion_kwargs(
                        prompt, temperature, is_json, max_tokens,
                    )
                    outcome["result"] = normalize_text(
                        self._run_completion(client, kwargs)
                    )
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            threading.Thread(target=request, name="llm-cancelable-call", daemon=True).start()
            while not done.wait(0.1):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        client.close()
                    except Exception:
                        pass
                    raise LLMCallCancelled("Model request cancelled")

            try:
                client.close()
            except Exception:
                pass
            if "error" not in outcome:
                return outcome.get("result", "")

            error = outcome["error"]
            status_code = getattr(error, "status_code", None)
            if status_code in _NO_RETRY_CODES or attempt >= max_retries:
                raise error

            wait_seconds = min(4.0, 1.5 * (attempt + 1))
            kind = "timed out" if _is_timeout_error(error) else "failed"
            print(
                f"[LLMProvider] Cancelable request {kind} (attempt {attempt + 1}), "
                f"retrying in {wait_seconds:g}s... error: {error}"
            )
            if cancel_event is not None:
                if cancel_event.wait(wait_seconds):
                    raise LLMCallCancelled("Model request cancelled")
            else:
                threading.Event().wait(wait_seconds)

        # Loop is kept for the type checker; success returns or the last exception is raised.
        return ""
