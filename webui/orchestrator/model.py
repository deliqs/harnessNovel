"""The orchestrator's chat model: the ADAPTIVE_BUILDER_LITE slot with the OrcaBonsai profile."""

from __future__ import annotations

import httpx2
import pydantic_ai
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from core.config import ConfigLoader

# The workbench owns its console output; skip pydantic-ai's first-run banner.
pydantic_ai.BANNER_ENABLED = False

ORCA_PROFILE = OpenAIModelProfile(
    # OrcaBonsai streams native reasoning in `reasoning_content`.
    openai_chat_thinking_field="reasoning_content",
    # Earlier-turn reasoning is dropped from requests: it only costs prefill time.
    openai_chat_send_back_thinking_parts=False,
    # OrcaBonsai rejects a system message that is not leading; merge the leading ones.
    openai_chat_supports_multiple_system_messages=False,
    openai_supports_strict_tool_definition=False,
    # Only tool_choice auto/none is accepted.
    openai_supports_tool_choice_required=False,
)

THINK_ON = {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
THINK_OFF = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}

# A cold turn on a long context takes about a minute; summarising can take longer.
REQUEST_TIMEOUT_SECONDS = 900
NO_MODEL = "No usable model is configured. Set the LLM API in the top-right first."

_cache: dict[tuple[str, str, str], OpenAIChatModel] = {}


def model_settings(thinking: bool) -> dict:
    """Per-turn settings that switch the model's reasoning on or off."""
    return THINK_ON if thinking else THINK_OFF


def build_model() -> OpenAIChatModel:
    """The orchestrator model for the current ADAPTIVE_BUILDER_LITE config, reused per config."""
    config = ConfigLoader.get_adaptive_builder_lite_config()
    key = (config.get("model", ""), config.get("base_url", ""), config.get("api_key", ""))
    if not key[0] or not key[1]:
        raise ValueError(NO_MODEL)
    if key not in _cache:
        _cache[key] = _openai_model(*key)
    return _cache[key]


def _openai_model(model_name: str, base_url: str, api_key: str) -> OpenAIChatModel:
    http_client = httpx2.AsyncClient(timeout=httpx2.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10))
    # A local server needs no key, but the OpenAI client refuses an empty one.
    provider = OpenAIProvider(base_url=base_url, api_key=api_key or "local-only", http_client=http_client)
    return OpenAIChatModel(model_name, provider=provider, profile=ORCA_PROFILE)
