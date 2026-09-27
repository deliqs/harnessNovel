"""Read-only tools every phase gets: browse workspace artifacts and check job status.

Results are capped summaries plus paths, so a tool result never floods the small context.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

from pydantic_ai import ModelRetry, RunContext

from webui.orchestrator.deps import JOB_LOCATORS, OrchestratorDeps

MAX_RESULT_CHARS = 2000
# Bounds the walk of a huge folder; the listing itself is cut to MAX_RESULT_CHARS.
MAX_WALKED_FILES = 1000
BAD_PATH = "Paths must stay inside the workspace and not be hidden, e.g. `file_system/story_design`."
# The design scope a phase's thread means when a design job_status call leaves it out.
PHASE_DESIGN_SCOPE = {"design": "concept", "stage": "stage"}
# A finished job's result note is cut to its last RESULT_CHARS; other status fields to FIELD_CHARS.
RESULT_CHARS = 1500
FIELD_CHARS = 300
_LIVE_STATUSES = {"queued", "running", "pausing", "paused", "stopping"}
STILL_RUNNING = "Still running. End your turn now; an automatic message will tell you when it finishes."

_JOB_STATUS = {
    "world": lambda runtime, ws, args: runtime.world_chat.job_status(ws),
    "design": lambda runtime, ws, args: runtime.design_chat.job_status(ws, args["scope"]),
    "arcs": lambda runtime, ws, args: runtime.arcs_chat.job_status(ws, args["volume"]),
    "chapters": lambda runtime, ws, args: runtime.chapters_chat.job_status(ws, args["volume"], args["arc"]),
    "drafts": lambda runtime, ws, args: runtime.draft_chat.job_status(ws, args["volume"], args["arc"]),
    "task": lambda runtime, ws, args: _task_status(runtime, ws, args["task_id"]),
}
_CONVERSATIONS = {
    "world": lambda runtime, ws, args: runtime.world_chat.get(ws),
    "design": lambda runtime, ws, args: runtime.design_chat.get(ws, args["scope"]),
    "arcs": lambda runtime, ws, args: runtime.arcs_chat.get(ws, args["volume"]),
    "chapters": lambda runtime, ws, args: runtime.chapters_chat.get(ws, args["volume"], args["arc"]),
    "drafts": lambda runtime, ws, args: runtime.draft_chat.get(ws, args["volume"], args["arc"]),
}


def capped(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """`text` cut to `limit` characters, saying how much was left out."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated: {len(text) - limit} more characters]"


def _relative_parts(path: str) -> list[str]:
    """`path` as workspace-relative parts; absolute, parent and hidden segments ask for a retry."""
    text = (path or "").replace("\\", "/").strip()
    parts = [part for part in PurePosixPath(text).parts if part != "."]
    if text.startswith("/") or any(part.startswith(".") for part in parts):
        raise ModelRetry(BAD_PATH)
    return parts


def list_artifacts(ctx: RunContext[OrchestratorDeps], prefix: str = "") -> str:
    """List the files in this workspace, optionally only those under one folder.

    Args:
        prefix: Workspace-relative folder such as `file_system/story_design`. Empty lists every file.
    """
    parts = _relative_parts(prefix)
    base = ctx.deps.runtime.store.workspace_path(ctx.deps.workspace)
    folder = base.joinpath(*parts).resolve()
    if folder != base and base not in folder.parents:
        raise ModelRetry(BAD_PATH)
    label = "/".join(parts) or "the workspace"
    if not folder.is_dir():
        return f"No folder {label}."
    lines, complete = _walk_files(base, folder)
    return _fit_lines(lines, complete) if lines else f"No files under {label}."


def _walk_files(base: Path, folder: Path) -> tuple[list[str], bool]:
    """`path (size bytes)` lines for the visible files under `folder`, and whether all were walked."""
    lines: list[str] = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name != "__pycache__")
        for name in sorted(name for name in files if not name.startswith(".")):
            if len(lines) >= MAX_WALKED_FILES:
                return lines, False
            lines.append(_file_line(base, Path(root) / name))
    return lines, True


def _fit_lines(lines: list[str], complete: bool) -> str:
    """Whole lines within the result cap, then a note of how many files were left out."""
    budget = MAX_RESULT_CHARS - 80
    kept: list[str] = []
    for line in lines:
        budget -= len(line) + 1
        if budget < 0:
            break
        kept.append(line)
    omitted = len(lines) - len(kept)
    if omitted or not complete:
        # A walk cut short cannot say how many files it did not reach.
        left = f"{omitted} more files" if complete else "More files"
        kept.append(f"[{left} not listed; list a narrower folder]")
    return "\n".join(kept)


def _file_line(base: Path, path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return f"{path.relative_to(base).as_posix()} ({size} bytes)"


def read_artifact(
    ctx: RunContext[OrchestratorDeps],
    path: str,
    max_chars: int = MAX_RESULT_CHARS,
    offset: int = 0,
    from_end: bool = False,
) -> str:
    """Read part of one workspace text file. Never use this to fetch whole chapters.

    A file longer than `max_chars` comes back one window at a time, headed by the character
    range it covers. Page on with `offset`, or read the end with `from_end`: conversation files
    keep their newest turns there. A job's latest result is quicker to get from `job_status`.

    Args:
        path: Workspace-relative file path, as `list_artifacts` shows it.
        max_chars: How many characters to return, at most 2000.
        offset: How many characters to skip, counted from the start, or from the end with `from_end`.
        from_end: Read the end of the file instead of the start.
    """
    relative = "/".join(_relative_parts(path))
    try:
        data = ctx.deps.runtime.store.read_file(ctx.deps.workspace, relative)
    except FileNotFoundError:
        raise ModelRetry(f"No such file: {path}. Use list_artifacts to find paths.") from None
    except ValueError as exc:
        raise ModelRetry(f"{exc} Use a workspace-relative path from list_artifacts.") from None
    content = data["content"]
    limit = max(1, min(int(max_chars), MAX_RESULT_CHARS))
    start, end = _window(len(content), limit, max(0, int(offset)), from_end)
    if start == 0 and end == len(content):
        return content
    return f"[characters {start}-{end} of {len(content)}]\n{content[start:end]}"


def _window(total: int, limit: int, offset: int, from_end: bool) -> tuple[int, int]:
    """The `[start, end)` character range of a read."""
    if from_end:
        end = max(0, total - offset)
        return max(0, end - limit), end
    start = min(offset, total)
    return start, min(total, start + limit)


def job_status(
    ctx: RunContext[OrchestratorDeps],
    kind: Literal["world", "design", "arcs", "chapters", "drafts", "task"],
    volume: Optional[int] = None,
    arc: Optional[int] = None,
    scope: Optional[Literal["concept", "stage"]] = None,
    task_id: Optional[str] = None,
) -> str:
    """Report the status of a generation job or CLI task in this workspace.

    Once the job has ended, `result` holds its outcome: the job's latest note (its answer,
    critique or summary, cut to the last 1500 characters) or, for a CLI task, its message.
    While it is still running, end your turn; a second check of that same running job is refused.

    Args:
        kind: Which job: world, design, arcs, chapters, drafts, or a CLI task.
        volume: Volume number for arcs, chapters and drafts. Defaults to the selected volume.
        arc: Arc number for chapters and drafts. Defaults to the selected arc.
        scope: Design scope, `concept` or `stage`, for design jobs. Defaults from this step.
        task_id: The task id, for kind `task`.
    """
    state = ctx.deps.ui_state
    args = {
        "volume": volume if volume is not None else state.get("volume"),
        "arc": arc if arc is not None else state.get("arc"),
        "scope": scope or PHASE_DESIGN_SCOPE.get(ctx.deps.phase),
        "task_id": task_id,
    }
    missing = [name for name in JOB_LOCATORS[kind] if args[name] in (None, "")]
    if missing:
        raise ModelRetry(f"job_status for {kind} needs {', '.join(missing)}.")
    status = _scalars(_JOB_STATUS[kind](ctx.deps.runtime, ctx.deps.workspace, args))
    if status.get("status") in _LIVE_STATUSES:
        return _running_status(ctx.deps, kind, args, status)
    status["result"] = _job_result(ctx.deps.runtime, ctx.deps.workspace, kind, args, status)
    return json.dumps(status, ensure_ascii=False)


def _job_key(kind: str, args: dict[str, Any]) -> tuple[Any, ...]:
    """The identity of one job: its kind and the locators this call resolved."""
    return (kind, *(args[name] for name in JOB_LOCATORS[kind]))


def _running_status(deps: OrchestratorDeps, kind: str, args: dict[str, Any], status: dict[str, Any]) -> str:
    """A live job's status the first time this run asks; a repeat says to end the turn."""
    key = _job_key(kind, args)
    if key in deps.seen_live_jobs:
        # results.py imports `capped` from this module, so this import stays local.
        from webui.orchestrator.tools.results import refused

        return refused(STILL_RUNNING)
    deps.seen_live_jobs.add(key)
    status["note"] = STILL_RUNNING
    return json.dumps(status, ensure_ascii=False)


def _job_result(runtime: Any, workspace: str, kind: str, args: dict[str, Any], status: dict[str, Any]) -> str:
    """A finished job's outcome: a task's message, or the manager's latest assistant note."""
    if kind == "task":
        return str(status.get("message") or "")
    turns = getattr(_CONVERSATIONS[kind](runtime, workspace, args), "turns", None)
    if not isinstance(turns, list):
        return ""
    notes = [turn.get("content") for turn in turns if isinstance(turn, dict) and turn.get("role") == "assistant"]
    return tail_capped(str(notes[-1] or "")) if notes else ""


def tail_capped(text: str, limit: int = RESULT_CHARS) -> str:
    """The last `limit` characters of `text`, saying how much was left out before them."""
    if len(text) <= limit:
        return text
    return f"[{len(text) - limit} earlier characters left out]\n{text[-limit:]}"


def _task_status(runtime: Any, workspace: str, task_id: str) -> dict[str, Any]:
    task = runtime.tasks.get(str(task_id))
    if task is None or task.workspace != workspace:
        raise ModelRetry(f"No task {task_id} in this workspace.")
    return task.public()


def _scalars(status: dict[str, Any]) -> dict[str, Any]:
    """The flat fields of a job status, long text cut; lists and nested records are too large."""
    return {
        key: capped(value, FIELD_CHARS) if isinstance(value, str) else value
        for key, value in status.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }


SHARED_TOOLS = [list_artifacts, read_artifact, job_status]
