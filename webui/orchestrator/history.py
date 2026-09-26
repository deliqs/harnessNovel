"""Server-side orchestrator history, one thread per (workspace, phase).

Each thread lives in `<workspace>/.orchestrator/<phase>/`, a dot directory the workspace tree
and the artifact tools skip, as two files:

- `model_context.json`: the compacted pydantic-ai messages the model sees next turn, without
  reasoning parts and with only the latest compaction summary.
- `transcript.jsonl`: an append-only display transcript of AG-UI-style messages. It is never
  compacted, so a reload shows the whole thread.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ThinkingPart,
)

# How the harness marks a compaction summary. A private name; the pin in setup.py keeps it stable.
from pydantic_ai_harness.compaction._summarizing_compaction import _SUMMARY_PREFIX as SUMMARY_PREFIX

from training.artifact_provenance import atomic_write_text
from webui.orchestrator.deps import PHASES
from webui.orchestrator.transcript import activity_message

THREADS_DIR = ".orchestrator"
CONTEXT_FILE = "model_context.json"
TRANSCRIPT_FILE = "transcript.jsonl"


class HistoryStore:
    """Reads and writes orchestrator threads inside the workspace root's containment."""

    def __init__(self, store):
        self.store = store

    def thread_dir(self, workspace: str, phase: str) -> Path:
        if phase not in PHASES:
            raise ValueError("Unknown orchestrator phase.")
        return self.store.workspace_path(workspace) / THREADS_DIR / phase

    def _writable_dir(self, workspace: str, phase: str) -> Optional[Path]:
        """The thread directory, or None once the workspace is gone, so a write never recreates it."""
        if not self.store.workspace_path(workspace).is_dir():
            return None
        return self.thread_dir(workspace, phase)

    def load_context(self, workspace: str, phase: str) -> list[ModelMessage]:
        """The saved model context; an unreadable one is moved aside and the thread starts fresh."""
        path = self.thread_dir(workspace, phase) / CONTEXT_FILE
        if not path.is_file():
            return []
        try:
            return ModelMessagesTypeAdapter.validate_json(path.read_bytes())
        except ValueError:
            self._quarantine(workspace, phase, path)
            return []

    def _quarantine(self, workspace: str, phase: str, path: Path) -> None:
        moved = path.with_name(f"model_context.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            path.replace(moved)
        except FileNotFoundError:
            return  # A concurrent reader already moved it aside and wrote the notice.
        notice = {
            "message": "The saved chat context could not be read, so the model starts without it.",
            "file": moved.name,
        }
        self.append_transcript(workspace, phase, [activity_message("context_reset", notice)])

    def save_context(self, workspace: str, phase: str, messages: list[ModelMessage]) -> None:
        directory = self._writable_dir(workspace, phase)
        if directory is None:
            return
        data = ModelMessagesTypeAdapter.dump_json(latest_summary_only(without_thinking(messages)), indent=2)
        atomic_write_text(directory / CONTEXT_FILE, data.decode("utf-8"))

    def append_transcript(self, workspace: str, phase: str, messages: Iterable[dict[str, Any]]) -> None:
        lines = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
        directory = self._writable_dir(workspace, phase)
        if not lines or directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        # One write per batch keeps each line whole; a crash can lose a batch but not split one.
        with (directory / TRANSCRIPT_FILE).open("a", encoding="utf-8") as handle:
            handle.write(lines)

    def read_transcript(self, workspace: str, phase: str) -> list[dict[str, Any]]:
        path = self.thread_dir(workspace, phase) / TRANSCRIPT_FILE
        if not path.is_file():
            return []
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return messages

    def clear(self, workspace: str, phase: str) -> None:
        directory = self.thread_dir(workspace, phase)
        for name in (CONTEXT_FILE, TRANSCRIPT_FILE):
            (directory / name).unlink(missing_ok=True)


def settled(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Drop a trailing response whose tool calls never got results.

    A run that failed or stopped mid-tool leaves such a response, and the next user prompt
    would then be refused for having unprocessed tool calls. Approval requests are saved from
    a completed run's result instead, so they are never passed through here.
    """
    messages = list(messages)
    while messages and isinstance(messages[-1], ModelResponse) and messages[-1].tool_calls:
        messages.pop()
    return messages


def without_thinking(messages: list[ModelMessage]) -> list[ModelMessage]:
    """`messages` without reasoning parts, which only cost prefill time; empty responses go too."""
    kept: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            parts = [part for part in message.parts if not isinstance(part, ThinkingPart)]
            if not parts:
                continue
            message = dataclasses.replace(message, parts=parts)
        kept.append(message)
    return kept


def latest_summary_only(messages: list[ModelMessage]) -> list[ModelMessage]:
    """`messages` with only the latest compaction summary; requests left empty go too.

    Compaction puts its new summary ahead of everything it keeps, so the first summary part in
    the history is the latest and any later one is stale.
    """
    summaries = [part for message in messages for part in message.parts if _is_summary(part)]
    stale = {id(part) for part in summaries[1:]}
    kept: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest) and any(id(part) in stale for part in message.parts):
            parts = [part for part in message.parts if id(part) not in stale]
            if not parts:
                continue
            message = dataclasses.replace(message, parts=parts)
        kept.append(message)
    return kept


def _is_summary(part) -> bool:
    return isinstance(part, SystemPromptPart) and part.content.startswith(SUMMARY_PREFIX)
