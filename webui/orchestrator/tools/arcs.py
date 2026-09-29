"""Orchestrator tools for the arcs step."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic_ai import RunContext, Tool

from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, refused
from webui.orchestrator.tools.scoped import (
    arc_refusal,
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
    "(e.g. `arc_001_ch001_010.md`, covering chapters 1-10). "
    "Use arcs_generate only for a volume with no arcs yet; use arcs_refine to change arcs that exist. "
    "When the author names an arc (\"rewrite arc 2\"), call arcs_refine with that `arc` and `cascade=False`, "
    "unless they ask for the later arcs to follow the change. "
    "Tools use the volume selected in the UI unless you pass `volume`. "
    "Use arcs_overview to see which arcs exist before refining, continuing or resetting. "
    "When an arcs job ends, read the `result` from job_status: it lists each arc's outcome "
    "(written, kept, or rejected with a reason). Report every rejected arc to the author; "
    "do not say the change succeeded for an arc that was rejected. "
    "If it is unclear which volume the author means, or what they want changed, ask them in plain text."
)


def arcs_generate(ctx: RunContext[OrchestratorDeps], message: str, volume: Optional[int] = None) -> str:
    """Generate the story arcs of a volume that has no arcs yet, as a background job.

    Args:
        message: The author's request, e.g. what the arcs should do.
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    v = selected(ctx, "volume", volume)
    text = require_message(message)
    ws = ctx.deps.workspace
    if _has_arcs(ws, v):
        return _generate_refusal(ws, v)
    return start_job(
        JobRef("arcs", ws, volume=v),
        lambda: ctx.deps.runtime.arcs_chat.start_message(ws, v, text),
        f"Story-arc generation started for volume {v}.",
    )


def arcs_refine(
    ctx: RunContext[OrchestratorDeps],
    message: str,
    arc: Optional[int] = None,
    mode: Optional[Literal["revise", "regenerate"]] = None,
    cascade: bool = False,
    volume: Optional[int] = None,
) -> str:
    """Change the existing story arcs of a volume, as a background job.

    Args:
        message: The author's request: what to change.
        arc: The arc to change. Pass it whenever the author names an arc, e.g. 2 for "rewrite arc 2".
            Leave it unset only when the change is not tied to one arc; the step then picks the arcs itself.
        mode: "revise" edits the arc in place; "regenerate" writes it again from scratch.
            Leave it unset to let the step decide.
        cascade: False (the default) rewrites only `arc`. True also rewrites every later arc
            so they stay consistent with the change; use it only when the author asks for that.
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    v = selected(ctx, "volume", volume)
    text = require_message(message)
    ws = ctx.deps.workspace
    if not _has_arcs(ws, v):
        return refused(f"Volume {v} has no arcs yet. Use arcs_generate to create them.")
    refusal = arc_refusal(ws, v, arc) if arc is not None else None
    if refusal:
        return refusal
    return start_job(
        JobRef("arcs", ws, volume=v),
        lambda: ctx.deps.runtime.arcs_chat.start_message(ws, v, text, arc=arc, mode=mode, cascade=cascade),
        f"Story-arc refinement started for volume {v}" + (f", arc {arc}." if arc is not None else "."),
    )


def _has_arcs(workspace: str, volume: int) -> bool:
    from training.adaptive_builder import _list_novel_story_arcs

    return bool(_list_novel_story_arcs(init_workspace(workspace), volume))


def _generate_refusal(workspace: str, volume: int) -> str:
    """Why arcs_generate will not run on a volume with arcs, naming the tool that fits instead."""
    from training.adaptive_builder import story_arc_resume_status

    resume = story_arc_resume_status(init_workspace(workspace), volume)
    if resume.get("can_resume"):
        return refused(
            f"Volume {volume} has an unfinished arcs run ({resume.get('completed', 0)} of "
            f"{resume.get('total', 0)} planned arcs). Use arcs_continue to generate the missing arcs."
        )
    return refused(
        f"Volume {volume} already has arcs. Use arcs_refine to change them, "
        "or arcs_reset first to generate the volume from scratch."
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
    Tool(arcs_refine, requires_approval=True),
    Tool(arcs_continue, requires_approval=True),
    Tool(arcs_reset, requires_approval=True),
    arcs_overview,
]
