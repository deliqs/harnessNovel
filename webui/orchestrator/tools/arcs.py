"""Orchestrator tools for the arcs step."""

from __future__ import annotations

from typing import Optional

from pydantic_ai import RunContext, Tool

from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done
from webui.orchestrator.tools.scoped import (
    attempt,
    progress_line,
    require_message,
    selected,
    start_job,
    workspace_path,
)

INSTRUCTIONS = (
    "This thread belongs to the Arcs step: the story arcs of the selected volume. "
    "Generating writes one file per arc under `file_system/story_arcs/vol_NN/` "
    "(e.g. `arc_001_ch001_010.md`, covering chapters 1-10); a later message refines the existing arcs. "
    "Tools use the volume selected in the UI unless you pass `volume`. "
    "Use arcs_overview to see which arcs exist before continuing or resetting. "
    "If it is unclear which volume the author means, or what they want changed, ask them in plain text."
)


def arcs_generate(ctx: RunContext[OrchestratorDeps], message: str, volume: Optional[int] = None) -> str:
    """Generate the story arcs of a volume, or refine the arcs it already has, as a background job.

    Args:
        message: The author's request, e.g. what the arcs should do or what to change.
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    v = selected(ctx, "volume", volume)
    text = require_message(message)
    ws = ctx.deps.workspace
    return start_job(
        JobRef("arcs", ws, volume=v),
        lambda: ctx.deps.runtime.arcs_chat.start_message(ws, v, text),
        f"Story-arc generation started for volume {v}.",
    )


def arcs_continue(ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None) -> str:
    """Continue an interrupted story-arc run of a volume, generating only the missing arcs.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    v = selected(ctx, "volume", volume)
    ws = ctx.deps.workspace
    return start_job(
        JobRef("arcs", ws, volume=v),
        lambda: ctx.deps.runtime.arcs_chat.continue_incomplete(ws, v),
        f"Continuing the unfinished story arcs of volume {v}.",
    )


def arcs_reset(ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None) -> str:
    """Delete every story arc of a volume and clear its arcs conversation. Other volumes are unchanged.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    v = selected(ctx, "volume", volume)
    return attempt(
        lambda: ctx.deps.runtime.arcs_chat.reset(ctx.deps.workspace, v),
        lambda result: f"Reset volume {v}: its story arcs and arcs conversation were deleted.",
    )


def arcs_overview(ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None) -> str:
    """Show which story arcs a volume has (paths and chapter ranges) and whether a run can be continued.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    from training.adaptive_builder import _list_novel_story_arcs, story_arc_resume_status

    v = selected(ctx, "volume", volume)
    ws = init_workspace(ctx.deps.workspace)
    arcs = _list_novel_story_arcs(ws, v)
    job = ctx.deps.runtime.arcs_chat.job_status(ctx.deps.workspace, v)
    lines = [f"Volume {v} has {len(arcs)} arcs." if arcs else f"Volume {v} has no arcs yet."]
    lines += [
        f"- arc {arc['idx']}, chapters {arc['start_ch']}-{arc['end_ch']}: "
        + workspace_path(ws, f"{ws.file_system}/story_arcs/vol_{v:02d}/{arc['file']}")
        for arc in arcs
    ]
    lines.append(progress_line(job, story_arc_resume_status(ws, v), "planned arcs"))
    return done("\n".join(lines))


TOOLS: list = [
    Tool(arcs_generate, requires_approval=True),
    Tool(arcs_continue, requires_approval=True),
    Tool(arcs_reset, requires_approval=True),
    arcs_overview,
]
