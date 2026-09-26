"""Tool calls a thread left waiting for the author, and how a new turn resolves them."""

from __future__ import annotations

from typing import Any, Optional

from pydantic_ai import DeferredToolResults, ToolDenied
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

# The adapter's own builder, so a reload shows exactly the interrupt a live run sent. It is a
# private module; the pydantic-ai pin in setup.py keeps it stable.
from pydantic_ai.ui.ag_ui._interrupt import approval_to_interrupt

AUTO_DENIED = "The author sent a new message instead of approving."
NOTHING_PENDING = "There is no pending approval to answer."


class NothingPending(LookupError):
    """A resume answered a tool call the thread is not waiting on."""


def pending_calls(messages: list[ModelMessage]) -> list[ToolCallPart]:
    """The last response's tool calls that have no result yet."""
    answered: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return [call for call in message.tool_calls if call.tool_call_id not in answered]
        answered.update(
            part.tool_call_id for part in message.parts if isinstance(part, (ToolReturnPart, RetryPromptPart))
        )
    return []


def resolve_pending(
    messages: list[ModelMessage], deferred: Optional[DeferredToolResults], new_prompt: bool
) -> tuple[Optional[DeferredToolResults], list[str]]:
    """The deferred results for this turn, and the ids of calls denied because the author moved on.

    Answers for calls the thread is not waiting on raise `NothingPending`. A new message while
    calls are still waiting denies them, so the run continues with the new prompt.
    """
    waiting = [call.tool_call_id for call in pending_calls(messages)]
    answered = set(deferred.approvals) | set(deferred.calls) if deferred else set()
    if answered - set(waiting):
        raise NothingPending(NOTHING_PENDING)
    unanswered = [call_id for call_id in waiting if call_id not in answered]
    if not new_prompt or not unanswered:
        return deferred, []
    deferred = deferred or DeferredToolResults()
    for call_id in unanswered:
        deferred.approvals[call_id] = ToolDenied(AUTO_DENIED)
    return deferred, unanswered


def pending_interrupts(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """The waiting calls as AG-UI interrupt objects, as a run's RUN_FINISHED outcome carries them."""
    return [
        approval_to_interrupt(call, {}).model_dump(mode="json", by_alias=True, exclude_none=True)
        for call in pending_calls(messages)
    ]
