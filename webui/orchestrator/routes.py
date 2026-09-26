"""HTTP routes of the per-phase orchestrator chat. `{phase}` is one of `deps.PHASES`; others are 404.

- `POST /api/workspaces/{name}/orchestrator/{phase}` runs one AG-UI turn and streams its events.
  A new user message while approvals are pending denies them and continues with the message.
  A denial reaches the model as "The author denied this call." plus " Their reason: <reason>".
  `forwardedProps.thinking: true` turns reasoning on for the turn. `forwardedProps.autoContinue`
  (`{toolCallId, jobKey}`, or `true`) marks a turn the UI sent after a job ended: it never sets
  approvals aside and is recorded as an `auto_continue` activity, not a user message.
  409 `detail` strings:
  - `runs.BUSY`: the thread already has an active run;
  - `pending.NOTHING_PENDING`: a `resume` answers no pending approval;
  - `runs.AWAITING_APPROVAL`: an auto-continue while an approval is pending;
  - `"DUPLICATE_AUTO_CONTINUE"`: an auto-continue for a `jobKey` the transcript already has.
- `POST .../{phase}/stop` cancels the active run (404 without one). The stream then ends with
  RUN_ERROR `{message: "Stopped by the author", code: "stopped"}`, and the partial turn is kept.
- `GET  .../{phase}/stream` replays the active or last run from its first event; 404 without one.
- `DELETE .../{phase}/history` clears the thread; 409 (`runs.BUSY`) while a run is active.
- `GET  .../{phase}/history` returns the thread for a page load:

      {
        "messages": [...],            # the display transcript: AG-UI user, assistant, tool,
                                      # reasoning and activity messages, oldest first
        "running": bool,              # whether a run is active
        "run_offset": int | null,     # while running: render messages[:run_offset], then replay
                                      # GET /stream, which yields everything after it; else null
        "pending_interrupts": [...],  # approvals the thread waits on, shaped like the interrupts
                                      # of RUN_FINISHED: {id: "int-<toolCallId>", reason,
                                      # toolCallId, message, responseSchema}; [] while running
      }

  Activity messages carry `activityType` and `content`:
  - `run_error` `{message}`: a RUN_ERROR without code "stopped";
  - `stopped` `{message}`: the author stopped the run (the RUN_ERROR with code "stopped");
  - `interrupt` `{interrupts}`: the run ended waiting for approvals;
  - `approval_decision` `{toolCallId, approved, reason}`: the author's answer, reason or null;
  - `auto_denied` `{message, toolCallIds}`: approvals set aside by a new message;
  - `auto_continue` `{message, toolCallId?, jobKey?}`: the UI's turn after a job ended;
  - `context_reset` `{message, file}`: an unreadable model context was moved aside.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_ai.ui.ag_ui import AGUIAdapter

from webui.orchestrator.compaction import compaction_capability
from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.model import model_settings
from webui.orchestrator.registry import UnknownPhase, build_agent, require_phase
from webui.orchestrator.runs import BUSY, OrchestratorRuns, RunConflict, ThreadRun, Turn
from webui.orchestrator.turn_input import prepare_turn, wants_thinking
from webui.task_runner import require_workspace_name

THREAD = "/api/workspaces/{name}/orchestrator/{phase}"
# How long a stop waits for the run to wind down before answering.
STOP_WAIT_SECONDS = 5

router = APIRouter()


def _runs(request: Request) -> OrchestratorRuns:
    return request.app.state.runtime.orchestrator


def _thread(request: Request, name: str, phase: str) -> tuple[str, str]:
    """The validated (workspace, phase) of a thread route, or the HTTP error to return."""
    try:
        require_phase(phase)
    except UnknownPhase:
        raise HTTPException(status_code=404, detail="Unknown orchestrator phase.") from None
    try:
        workspace = require_workspace_name(name)
        path = request.app.state.runtime.store.workspace_path(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Workspace does not exist.")
    return workspace, phase


def _stream_response(run: ThreadRun) -> StreamingResponse:
    return StreamingResponse(run.buffer.replay(), media_type=run.content_type)


@router.post(THREAD)
async def run_orchestrator(name: str, phase: str, request: Request) -> StreamingResponse:
    workspace, phase = _thread(request, name, phase)
    runs = _runs(request)
    if runs.is_running(workspace, phase):
        raise HTTPException(status_code=409, detail=BUSY)
    try:
        model = runs.model_factory()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    agent = build_agent(phase, model)
    try:
        adapter = await AGUIAdapter.from_request(request, agent=agent, manage_system_prompt="client")
        adapter, deferred_results, decisions = prepare_turn(adapter)
    except (ValidationError, UserError) as exc:
        raise HTTPException(status_code=422, detail="Invalid AG-UI run input.") from exc
    deps = OrchestratorDeps(
        runtime=request.app.state.runtime, workspace=workspace, phase=phase, ui_state=dict(adapter.state or {})
    )
    turn = Turn(
        adapter=adapter,
        deps=deps,
        deferred_results=deferred_results,
        model_settings=model_settings(wants_thinking(adapter.run_input)),
        capabilities=[compaction_capability(model)],
        decisions=decisions,
    )
    try:
        run = runs.start(turn)
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _stream_response(run)


@router.post(THREAD + "/stop")
async def stop_orchestrator(name: str, phase: str, request: Request) -> dict[str, bool]:
    workspace, phase = _thread(request, name, phase)
    try:
        run = _runs(request).stop(workspace, phase)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await asyncio.wait({run.task}, timeout=STOP_WAIT_SECONDS)
    return {"running": run.active}


@router.get(THREAD + "/stream")
async def replay_orchestrator(name: str, phase: str, request: Request) -> StreamingResponse:
    workspace, phase = _thread(request, name, phase)
    run = _runs(request).get(workspace, phase)
    if run is None:
        raise HTTPException(status_code=404, detail="This chat has no run to replay.")
    return _stream_response(run)


@router.get(THREAD + "/history")
def orchestrator_history(name: str, phase: str, request: Request) -> dict[str, Any]:
    workspace, phase = _thread(request, name, phase)
    return _runs(request).thread_view(workspace, phase)


@router.delete(THREAD + "/history")
def clear_orchestrator_history(name: str, phase: str, request: Request) -> dict[str, bool]:
    workspace, phase = _thread(request, name, phase)
    try:
        _runs(request).clear(workspace, phase)
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"cleared": True}
