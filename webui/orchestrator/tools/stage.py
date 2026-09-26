"""Orchestrator tools for the stage step."""

from __future__ import annotations

from typing import Any, Literal

from pydantic_ai import RunContext, Tool

from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.design_ops import (
    design_continue,
    design_lenses_status,
    design_reset,
    design_stop,
    start_design_chat,
)
from webui.orchestrator.tools.results import job_started, refused

INSTRUCTIONS = (
    "This thread belongs to the Stage step: the long mainline and the stage roadmap of each volume, "
    "written to file_system/story_design/long_mainline.md and stage_roadmap.md. "
    "Use design_chat for what the author says about the stages: mode chat drafts or changes them, "
    "extend appends later stages, question answers without changing files, critique reviews them. "
    "With no stages yet, design_chat in mode chat drafts them; for a full rewrite, call design_reset and "
    "then design_chat. Use design_continue after an interrupted run, design_stop to end a running one, "
    "and regenerate_title_synopsis to "
    "refresh file_system/novel_name_synopsis.md after the stages change. "
    "If the author's request is ambiguous, ask them in plain text before calling a job tool. "
    "Pass the ids of `[attached upload <id>: <name>]` tags as attachment_upload_ids; "
    "a `[sync_updated_design]` tag means sync_updated_design=true."
)


def design_chat(
    ctx: RunContext[OrchestratorDeps],
    message: str,
    mode: Literal["chat", "extend", "question", "critique"] = "chat",
    use_new_reference: bool = False,
    sync_updated_design: bool = False,
    attachment_upload_ids: list[str] | None = None,
) -> str:
    """Send one message to the stage-design chat as a background job.

    Args:
        message: The author's request, in their words.
        mode: chat drafts or changes the stages; extend appends later stages; question answers
            without changing files; critique reviews the stages through the critic lenses.
        use_new_reference: Leave False on this step.
        sync_updated_design: True syncs a changed book design into the last or appended stages.
        attachment_upload_ids: Upload ids of files the author attached: the ids from `[attached upload <id>: <name>]` tags in the author's message.
    """
    return start_design_chat(ctx, message, mode, use_new_reference, sync_updated_design, attachment_upload_ids)


_BUSY_STATUSES = ("queued", "running", "pausing", "paused", "stopping")


def _title_synopsis_blocker(runtime: Any, workspace: str) -> str:
    """Why the title and synopsis cannot be regenerated now, as the Stage step's button decides; empty if they can."""
    try:
        design = runtime.store.summary(workspace)["story_design"]
    except FileNotFoundError:
        return "This workspace does not exist."
    if not design.get("stage_assets_exist"):
        return "Generate the stage design first; the title and synopsis are derived from it."
    if runtime.design_chat.job_status(workspace, "stage").get("status") in _BUSY_STATUSES:
        return "A stage-design job is still running. Wait for it or stop it first."
    return ""


def regenerate_title_synopsis(ctx: RunContext[OrchestratorDeps]) -> str:
    """Regenerate the title suggestions and synopsis from the current design, as a background task."""
    blocker = _title_synopsis_blocker(ctx.deps.runtime, ctx.deps.workspace)
    if blocker:
        return refused(blocker)
    try:
        task = ctx.deps.runtime.tasks.create("novel_name_synopsis", ctx.deps.workspace, {"force": True})
    except ValueError as exc:
        return refused(str(exc))
    ref = JobRef(kind="task", workspace=ctx.deps.workspace, task_id=task.id)
    return job_started(ref, "Title and synopsis regeneration started.")


TOOLS: list = [
    design_chat,
    Tool(design_continue, requires_approval=True),
    design_stop,
    Tool(design_reset, requires_approval=True),
    design_lenses_status,
    Tool(regenerate_title_synopsis, requires_approval=True),
]
