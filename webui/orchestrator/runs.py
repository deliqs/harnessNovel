"""Orchestrator turns, decoupled from the HTTP connection that started them.

Each turn runs as a background asyncio task that appends encoded AG-UI events to an in-memory
buffer. The SSE response streams from that buffer, so a client that disconnects can reconnect
and replay the current or last run; a disconnect never cancels a turn, only `stop` does. Only
one run per (workspace, phase) thread is active at a time. While any run is active, the model
gate holds back in-process job threads, so the run's requests reach the serial model first.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from ag_ui.core import EventType, RunErrorEvent
from pydantic_ai import DeferredToolResults, UsageLimits, capture_run_messages
from pydantic_ai.ui.ag_ui import AGUIAdapter

from core.model_gate import model_gate
from core.prompt_trace import redact_sensitive_text
from webui.orchestrator.buffer import ThreadRun
from webui.orchestrator.checkpoint import ContextCheckpoint
from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.history import HistoryStore, settled
from webui.orchestrator.model import build_model
from webui.orchestrator.pending import AUTO_DENIED, NothingPending, pending_calls, pending_interrupts, resolve_pending
from webui.orchestrator.transcript import STOP_CODE, TranscriptBuilder, activity_message
from webui.orchestrator.turn_input import auto_continue, has_user_message, input_records

REQUEST_LIMIT = 15
BUSY = "This chat is still answering. Wait for it to finish first."
STOPPED = "Stopped by the author"
AWAITING_APPROVAL = "The chat is waiting for an approval. Answer it before continuing."
DUPLICATE_AUTO_CONTINUE = "DUPLICATE_AUTO_CONTINUE"

logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    """The thread cannot take this run: one is active, or the resume answers nothing pending."""


@dataclass
class Turn:
    """Everything one run needs, prepared by the route from the AG-UI request."""

    adapter: AGUIAdapter
    deps: OrchestratorDeps
    deferred_results: Optional[DeferredToolResults]
    model_settings: dict
    capabilities: list
    # The author's approval answers in this turn, as `turn_input.approval_decisions` gives them.
    decisions: list = field(default_factory=list)


@dataclass
class _Progress:
    """What a run has produced so far, for persisting it however the run ends."""

    live: list
    builder: TranscriptBuilder = field(default_factory=TranscriptBuilder)
    completed: Optional[list] = None


class OrchestratorRuns:
    """The active or last run of every thread. `model_factory` is swappable for tests.

    `_lock` guards only the in-memory maps. Thread files are read and written outside it, while
    the thread is in `_claimed` so no other start or clear touches them at the same time.
    """

    def __init__(self, store: Any, model_factory: Callable[[], Any] = build_model) -> None:
        self.history = HistoryStore(store)
        self.model_factory = model_factory
        self._runs: dict[tuple[str, str], ThreadRun] = {}
        self._claimed: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def get(self, workspace: str, phase: str) -> Optional[ThreadRun]:
        with self._lock:
            return self._runs.get((workspace, phase))

    def _busy(self, key: tuple[str, str]) -> bool:
        """Whether a run is active or starting on the thread, or its files are being cleared. Hold `_lock`."""
        run = self._runs.get(key)
        return key in self._claimed or (run is not None and run.active)

    def is_running(self, workspace: str, phase: str) -> bool:
        with self._lock:
            return self._busy((workspace, phase))

    def any_running(self) -> bool:
        with self._lock:
            return any(self._busy(key) for key in {*self._runs, *self._claimed})

    def workspace_running(self, workspace: str) -> bool:
        with self._lock:
            return any(self._busy(key) for key in {*self._runs, *self._claimed} if key[0] == workspace)

    def _claim(self, key: tuple[str, str]) -> None:
        with self._lock:
            if self._busy(key):
                raise RunConflict(BUSY)
            self._claimed.add(key)

    def _release(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._claimed.discard(key)

    def forget_workspace(self, workspace: str) -> None:
        """Drop the finished runs of a deleted workspace, so a same-named one starts clean."""
        with self._lock:
            for key in [key for key, run in self._runs.items() if key[0] == workspace and not run.active]:
                del self._runs[key]

    def forget_all(self) -> None:
        """Drop every finished run, after the workspace root changed."""
        with self._lock:
            self._runs = {key: run for key, run in self._runs.items() if run.active}

    def thread_view(self, workspace: str, phase: str) -> dict[str, Any]:
        """The GET /history payload; see routes.py for the contract."""
        run = self.get(workspace, phase)
        running = run is not None and run.active
        interrupts = [] if running else pending_interrupts(self.history.load_context(workspace, phase))
        return {
            "messages": self.history.read_transcript(workspace, phase),
            "running": running,
            "run_offset": run.transcript_offset if running else None,
            "pending_interrupts": interrupts,
        }

    def clear(self, workspace: str, phase: str) -> None:
        """Delete the thread's history and forget its last run; refused while a run is active."""
        key = (workspace, phase)
        self._claim(key)
        try:
            with self._lock:
                self._runs.pop(key, None)
            self.history.clear(workspace, phase)
        finally:
            self._release(key)

    def start(self, turn: Turn) -> ThreadRun:
        key = (turn.deps.workspace, turn.deps.phase)
        self._claim(key)
        try:
            history, offset = self._open(turn)
            run = ThreadRun(turn.adapter.build_event_stream().content_type, offset)
            with self._lock:
                self._runs[key] = run
        finally:
            self._release(key)
        run.task = asyncio.create_task(self._execute(run, turn, history))
        return run

    def stop(self, workspace: str, phase: str) -> ThreadRun:
        """Cancel the active run; its partial turn is kept. Raises LookupError when none is active."""
        run = self.get(workspace, phase)
        if run is None or not run.active or run.task is None:
            raise LookupError("This chat has no active run to stop.")
        run.task.cancel()
        return run

    def _open(self, turn: Turn) -> tuple[list, int]:
        """Load the thread, settle waiting tool calls, and write the turn's input to the transcript."""
        workspace, phase = turn.deps.workspace, turn.deps.phase
        run_input = turn.adapter.run_input
        history = self.history.load_context(workspace, phase)
        job = auto_continue(run_input)
        if job is not None:
            self._check_auto_continue(workspace, phase, history, job)
        try:
            turn.deferred_results, denied = resolve_pending(history, turn.deferred_results, has_user_message(run_input))
        except NothingPending as exc:
            raise RunConflict(str(exc)) from exc
        notices = [activity_message("approval_decision", decision) for decision in turn.decisions]
        if denied:
            notices.append(activity_message("auto_denied", {"message": AUTO_DENIED, "toolCallIds": denied}))
        self._record(turn, notices + input_records(run_input))
        return history, len(self.history.read_transcript(workspace, phase))

    def _check_auto_continue(self, workspace: str, phase: str, history: list, job: dict) -> None:
        if pending_calls(history):
            # Only the author may set an approval aside, never an automatic continuation.
            raise RunConflict(AWAITING_APPROVAL)
        job_key = job.get("jobKey")
        if job_key is None:
            return
        for message in self.history.read_transcript(workspace, phase):
            if message.get("activityType") == "auto_continue" and message.get("content", {}).get("jobKey") == job_key:
                raise RunConflict(DUPLICATE_AUTO_CONTINUE)

    async def _execute(self, run: ThreadRun, turn: Turn, history: list) -> None:
        # Background job threads wait at the model gate until this run ends, so its requests go first.
        with model_gate.priority(), capture_run_messages() as live:
            progress = _Progress(live=live)
            try:
                await self._stream(run, turn, history, progress)
            except asyncio.CancelledError:
                self._end(run, turn, progress, RunErrorEvent(message=STOPPED, code=STOP_CODE))
                raise
            except Exception as exc:
                # Agent errors arrive as RUN_ERROR events; this covers failures around the stream.
                self._end(run, turn, progress, RunErrorEvent(message=redact_sensitive_text(str(exc))))
            finally:
                self._persist(turn, progress)
                run.buffer.finish()

    async def _stream(self, run: ThreadRun, turn: Turn, history: list, progress: _Progress) -> None:
        def on_complete(result: Any) -> None:
            progress.completed = result.all_messages()

        def on_cancel(cancelled: Any) -> None:
            progress.completed = cancelled.all_messages()

        events = turn.adapter.run_stream(
            message_history=history,
            deferred_tool_results=turn.deferred_results,
            deps=turn.deps,
            model_settings=turn.model_settings,
            usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
            capabilities=[*turn.capabilities, ContextCheckpoint(lambda messages: self._save(turn, messages))],
            on_complete=on_complete,
            on_cancel=on_cancel,
        )
        async for chunk in turn.adapter.encode_stream(self._tap(events, turn, progress)):
            run.buffer.append(chunk)

    async def _tap(self, events: AsyncIterator[Any], turn: Turn, progress: _Progress) -> AsyncIterator[Any]:
        """Pass events through, redacting errors and writing each completed message to the transcript."""
        async for event in events:
            if event.type == EventType.RUN_ERROR:
                event = event.model_copy(update={"message": redact_sensitive_text(event.message)})
            progress.builder.feed(event)
            self._record(turn, progress.builder.take())
            yield event

    def _end(self, run: ThreadRun, turn: Turn, progress: _Progress, event: RunErrorEvent) -> None:
        """Close a run that ended outside the agent stream with a terminal RUN_ERROR.

        The transcript records the same event the client sees, so live and reload agree.
        """
        progress.builder.feed(event)
        run.buffer.append(turn.adapter.build_event_stream().encode_event(event))

    def _record(self, turn: Turn, messages: list[dict]) -> None:
        self.history.append_transcript(turn.deps.workspace, turn.deps.phase, messages)

    def _save(self, turn: Turn, messages: list) -> None:
        if not messages:
            return
        try:
            self.history.save_context(turn.deps.workspace, turn.deps.phase, messages)
        except Exception:
            logger.exception("Could not save the orchestrator context %s/%s", turn.deps.workspace, turn.deps.phase)

    def _persist(self, turn: Turn, progress: _Progress) -> None:
        """Save the transcript tail and the model context, whatever way the run ended."""
        try:
            self._record(turn, progress.builder.close())
        except Exception:
            logger.exception("Could not save the orchestrator transcript %s/%s", turn.deps.workspace, turn.deps.phase)
        # A completed or cancelled run reports its messages; otherwise keep what the run got to.
        completed = progress.completed
        self._save(turn, completed if completed is not None else settled(progress.live))
