"""Folds a run's AG-UI events into AG-UI messages for the display transcript.

Assistant text and its tool calls become one assistant message, each tool result a tool
message, and streamed reasoning a reasoning message. A run error, a stop or an approval interrupt
becomes an activity message, so a reload can show why a run ended. A RUN_ERROR whose `code` is
STOP_CODE is a stop and becomes a `stopped` activity; any other becomes `run_error`.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from ag_ui.core import (
    ActivityMessage,
    AssistantMessage,
    EventType,
    FunctionCall,
    ReasoningMessage,
    ToolCall,
    ToolMessage,
)


STOP_CODE = "stopped"
"""The RUN_ERROR `code` of a run the author stopped, and the activity type it is recorded as."""


def activity_message(activity_type: str, content: dict[str, Any]) -> dict[str, Any]:
    """An AG-UI activity message for a notice in the transcript, such as a run error."""
    return _dump(ActivityMessage(id=_new_id(), activity_type=activity_type, content=content))


class TranscriptBuilder:
    def __init__(self) -> None:
        self._assistant: Optional[dict[str, Any]] = None
        self._reasoning: Optional[dict[str, str]] = None
        self._ready: list[dict[str, Any]] = []
        self._handlers = {
            EventType.TEXT_MESSAGE_START: self._text_start,
            EventType.TEXT_MESSAGE_CONTENT: self._text_content,
            EventType.TOOL_CALL_START: self._tool_call_start,
            EventType.TOOL_CALL_ARGS: self._tool_call_args,
            EventType.TOOL_CALL_RESULT: self._tool_call_result,
            EventType.REASONING_MESSAGE_START: self._reasoning_start,
            EventType.REASONING_MESSAGE_CONTENT: self._reasoning_content,
            EventType.REASONING_MESSAGE_END: self._reasoning_end,
            EventType.RUN_ERROR: self._run_error,
            EventType.RUN_FINISHED: self._run_finished,
        }

    def feed(self, event: Any) -> None:
        handler = self._handlers.get(event.type)
        if handler is not None:
            handler(event)

    def take(self) -> list[dict[str, Any]]:
        """The messages completed so far, each returned once."""
        ready, self._ready = self._ready, []
        return ready

    def close(self) -> list[dict[str, Any]]:
        """Complete the open messages and return everything not yet taken."""
        self._reasoning_end(None)
        self._flush_assistant()
        return self.take()

    def record_notice(self, activity_type: str, content: dict[str, Any]) -> None:
        """Complete the open messages, then add an activity message after them."""
        self._reasoning_end(None)
        self._flush_assistant()
        self._ready.append(activity_message(activity_type, content))

    def _open_assistant(self, message_id: Optional[str]) -> dict[str, Any]:
        if self._assistant is None:
            self._assistant = {"id": message_id or _new_id(), "content": "", "calls": {}}
        return self._assistant

    def _flush_assistant(self) -> None:
        assistant, self._assistant = self._assistant, None
        if assistant is None or not (assistant["content"] or assistant["calls"]):
            return
        calls = [
            ToolCall(id=call_id, type="function", function=FunctionCall(name=call["name"], arguments=call["arguments"]))
            for call_id, call in assistant["calls"].items()
        ]
        message = AssistantMessage(id=assistant["id"], content=assistant["content"] or None, tool_calls=calls or None)
        self._ready.append(_dump(message))

    def _text_start(self, event: Any) -> None:
        self._flush_assistant()
        self._open_assistant(event.message_id)

    def _text_content(self, event: Any) -> None:
        self._open_assistant(event.message_id)["content"] += event.delta

    def _tool_call_start(self, event: Any) -> None:
        calls = self._open_assistant(event.parent_message_id)["calls"]
        calls[event.tool_call_id] = {"name": event.tool_call_name, "arguments": ""}

    def _tool_call_args(self, event: Any) -> None:
        call = self._open_assistant(None)["calls"].get(event.tool_call_id)
        if call is not None:
            call["arguments"] += event.delta

    def _tool_call_result(self, event: Any) -> None:
        self._flush_assistant()
        message = ToolMessage(id=event.message_id, content=event.content, tool_call_id=event.tool_call_id)
        self._ready.append(_dump(message))

    def _reasoning_start(self, event: Any) -> None:
        self._flush_assistant()
        self._reasoning = {"id": event.message_id, "content": ""}

    def _reasoning_content(self, event: Any) -> None:
        if self._reasoning is not None:
            self._reasoning["content"] += event.delta

    def _reasoning_end(self, event: Any) -> None:
        reasoning, self._reasoning = self._reasoning, None
        if reasoning and reasoning["content"]:
            self._ready.append(_dump(ReasoningMessage(id=reasoning["id"], content=reasoning["content"])))

    def _run_error(self, event: Any) -> None:
        self.record_notice(STOP_CODE if event.code == STOP_CODE else "run_error", {"message": event.message})

    def _run_finished(self, event: Any) -> None:
        outcome = event.model_dump(mode="json", by_alias=True, exclude_none=True).get("outcome") or {}
        if outcome.get("type") == "interrupt":
            self.record_notice("interrupt", {"interrupts": outcome.get("interrupts", [])})


def _new_id() -> str:
    return uuid.uuid4().hex


def _dump(message: Any) -> dict[str, Any]:
    return message.model_dump(mode="json", by_alias=True, exclude_none=True)
