"""Helpers for the tools scoped to a volume and arc: the arcs, chapters and draft steps.

Volume and arc default to the UI selection in `ctx.deps.ui_state`. Manager calls that hit a
guard raise `ValueError`; these helpers turn that, and any other error, into a redacted `refused`
result the model can relay instead of ending the turn.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from pydantic_ai import ModelRetry, RunContext

from core.prompt_trace import redact_sensitive_text
from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, job_started, refused


def selected(ctx: RunContext[OrchestratorDeps], name: str, value: Optional[int]) -> int:
    """`value`, or the UI's selected `name` (`volume` or `arc`); asks the model when neither is set."""
    if value is None:
        value = ctx.deps.ui_state.get(name)
    if value in (None, ""):
        raise ModelRetry(f"No {name} is selected in the UI. Pass `{name}`, or ask the author which {name}.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ModelRetry(f"`{name}` must be a whole number, not {value!r}.") from None
    if number < 1:
        raise ModelRetry(f"`{name}` must be 1 or higher.")
    return number


def require_message(message: str) -> str:
    text = (message or "").strip()
    if not text:
        raise ModelRetry("`message` is empty. Pass the author's request as the message.")
    return text


def start_job(ref: JobRef, start: Callable[[], Any], message: str) -> str:
    """Run a manager call that starts a background job; a guard refusal becomes `refused`."""
    try:
        start()
    except Exception as exc:
        return _refusal(exc)
    return job_started(ref, message)


def attempt(action: Callable[[], Any], describe: Callable[[Any], str]) -> str:
    """Run a manager call that finishes now; its result is described in one short message."""
    try:
        result = action()
    except Exception as exc:
        return _refusal(exc)
    return done(describe(result))


def _refusal(exc: Exception) -> str:
    """A guard's `ValueError` as its own message; any other error with its type, both redacted."""
    text = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
    return refused(redact_sensitive_text(text))


BUSY_STATUSES = {"running", "pausing", "paused", "stopping"}


def manager_busy(manager: Any, workspace: str) -> bool:
    """Whether a chat manager has an unfinished job in `workspace`, the check the web runtime uses."""
    lock = getattr(manager, "_jobs_lock", None) or getattr(manager, "_lock", None)
    if lock is None:
        return False
    with lock:
        return any(
            isinstance(key, tuple) and key and key[0] == workspace and job.get("status") in BUSY_STATUSES
            for key, job in getattr(manager, "_jobs", {}).items()
        )


def progress_line(job: dict[str, Any], resume: dict[str, Any], unit: str) -> str:
    """One line on the job state and how far the step is, from `job_status` and a resume status."""
    line = f"Job: {job.get('status') or 'idle'}"
    if job.get("message"):
        line += f" ({job['message']})"
    line += f". {resume.get('completed', 0)} of {resume.get('total', 0)} {unit} done"
    if resume.get("can_resume"):
        line += "; an incomplete run can be continued"
    return line + "."


def find_arc(ws: Any, volume: int, arc: int) -> Optional[dict[str, Any]]:
    """The arc record the chat managers use (`idx`, `start_ch`, `end_ch`, `file`), or None."""
    from training.adaptive_builder import _list_novel_story_arcs
    return next((item for item in _list_novel_story_arcs(ws, volume) if item["idx"] == arc), None)


def arc_refusal(workspace: str, volume: int, arc: int) -> Optional[str]:
    """A `refused` result when the arc does not exist, so no job starts only to fail; else None."""
    ws = init_workspace(workspace)
    return None if find_arc(ws, volume, arc) else missing_arc(ws, volume, arc)


def missing_arc(ws: Any, volume: int, arc: int) -> str:
    from training.adaptive_builder import _list_novel_story_arcs
    known = ", ".join(str(item["idx"]) for item in _list_novel_story_arcs(ws, volume)) or "none"
    return refused(f"Volume {volume} has no arc {arc}. Arcs on disk: {known}.")


def workspace_path(ws: Any, path: str) -> str:
    """`path` relative to the workspace, with forward slashes, as `list_artifacts` shows it."""
    return os.path.relpath(path, ws.root).replace(os.sep, "/")


def existing_paths(ws: Any, paths: list[str]) -> list[str]:
    return [workspace_path(ws, path) for path in paths if os.path.isfile(path)]
