"""Shapes an AG-UI request into one turn against the server-held thread history."""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

from ag_ui.core import RunAgentInput
from pydantic_ai import DeferredToolResults, ToolDenied
from pydantic_ai.ui.ag_ui import AGUIAdapter

from webui.orchestrator.transcript import activity_message


def new_turn_only(run_input: RunAgentInput) -> RunAgentInput:
    """Keep only what the client added since the last assistant message.

    The stored history is authoritative, so a client that resends everything (as
    `@ag-ui/client` HttpAgent does) is trimmed to its new user or tool messages. Client
    system/developer messages are dropped, so the server owns every system prompt, and so are
    client-declared tools: the orchestrator has no frontend tools.
    """
    messages = run_input.messages
    last_assistant = max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)
    tail = [m for m in messages[last_assistant + 1:] if m.role in ("user", "tool")]
    return run_input.model_copy(update={"messages": tail, "tools": []})


def frontend_tool_results(adapter: AGUIAdapter) -> tuple[AGUIAdapter, Optional[DeferredToolResults]]:
    """Move client tool messages into `DeferredToolResults.calls`, next to any resume approvals.

    `AGUIAdapter.load_messages` needs each tool message's call in the same request, but with
    server-side history that call lives in the stored history, not in the trimmed input.
    """
    run_input = adapter.run_input
    tool_messages = [m for m in run_input.messages if m.role == "tool"]
    if not tool_messages:
        return adapter, adapter.deferred_tool_results
    results = adapter.deferred_tool_results or DeferredToolResults()
    results.calls.update({m.tool_call_id: m.content for m in tool_messages})
    others = [m for m in run_input.messages if m.role != "tool"]
    return dataclasses.replace(adapter, run_input=run_input.model_copy(update={"messages": others})), results


DENIED = "The author denied this call."
_DEFAULT_DENIAL = ToolDenied().message


def prepare_turn(adapter: AGUIAdapter) -> tuple[AGUIAdapter, Optional[DeferredToolResults], list[dict[str, Any]]]:
    """The adapter trimmed to this turn's input, its deferred results, and the author's decisions.

    Denials are reworded to say the author denied the call, so the model cannot mistake the
    author's reason for the tool's own answer.
    """
    adapter = dataclasses.replace(adapter, run_input=new_turn_only(adapter.run_input))
    adapter, deferred = frontend_tool_results(adapter)
    decisions = approval_decisions(deferred)
    for decision in decisions:
        if not decision["approved"]:
            reason = decision["reason"]
            deferred.approvals[decision["toolCallId"]] = ToolDenied(f"{DENIED} Their reason: {reason}" if reason else DENIED)
    return adapter, deferred, decisions


def approval_decisions(deferred: Optional[DeferredToolResults]) -> list[dict[str, Any]]:
    """`{toolCallId, approved, reason}` for each approval the author answered in this turn."""
    decisions = []
    for call_id, result in (deferred.approvals.items() if deferred else ()):
        denied = result is False or isinstance(result, ToolDenied)
        reason = result.message if isinstance(result, ToolDenied) and result.message != _DEFAULT_DENIAL else None
        decisions.append({"toolCallId": call_id, "approved": not denied, "reason": reason})
    return decisions


def has_user_message(run_input: RunAgentInput) -> bool:
    return any(m.role == "user" for m in run_input.messages)


def _forwarded_flag(run_input: RunAgentInput, name: str) -> bool:
    props = run_input.forwarded_props
    return isinstance(props, dict) and bool(props.get(name))


def wants_thinking(run_input: RunAgentInput) -> bool:
    """`forwardedProps.thinking` turns the model's reasoning on for this turn; it is off by default."""
    return _forwarded_flag(run_input, "thinking")


def auto_continue(run_input: RunAgentInput) -> Optional[dict[str, Any]]:
    """The job a turn continues after, when the UI sent it by itself because a job finished.

    `forwardedProps.autoContinue` is `{toolCallId, jobKey}`, or `true` from older clients;
    the result holds whichever of the two keys were given. None for a turn the author sent.
    """
    props = run_input.forwarded_props
    value = props.get("autoContinue") if isinstance(props, dict) else None
    if isinstance(value, dict):
        return {key: value[key] for key in ("toolCallId", "jobKey") if value.get(key) is not None}
    return {} if value is True else None


def input_records(run_input: RunAgentInput) -> list[dict[str, Any]]:
    """The transcript entries for the turn's input.

    The author's messages are recorded as user messages. An automatic continuation is recorded
    as an `auto_continue` activity, `{message, toolCallId, jobKey}`, instead, though the model
    still receives it as the prompt.
    """
    messages = user_messages(run_input)
    job = auto_continue(run_input)
    if job is None:
        return messages
    return [activity_message("auto_continue", {"message": m.get("content", ""), **job}) for m in messages]


def user_messages(run_input: RunAgentInput) -> list[dict[str, Any]]:
    """The turn's user messages in AG-UI JSON form, for the display transcript."""
    return [
        m.model_dump(mode="json", by_alias=True, exclude_none=True)
        for m in run_input.messages
        if m.role == "user"
    ]
