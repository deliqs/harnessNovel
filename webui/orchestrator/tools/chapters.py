"""Orchestrator tools for the chapters step."""

from __future__ import annotations

import json
import os
from typing import Literal, Optional

from pydantic_ai import RunContext, Tool

from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, refused
from webui.orchestrator.tools.scoped import (
    arc_refusal,
    attempt,
    existing_paths,
    find_arc,
    manager_busy,
    missing_arc,
    progress_line,
    require_message,
    selected,
    start_job,
)

INSTRUCTIONS = (
    "This thread belongs to the Chapters step: the chapter outlines of the selected arc. "
    "Generating writes one outline per chapter of the arc to `file_system/chapter_outlines/vol_NN/chapter_NNN.md`, "
    "plus a system-panel snapshot per chapter under `file_system/system_panels/` when the system panel is on; "
    "a later message refines the existing outlines. "
    "Tools use the volume and arc selected in the UI unless you pass `volume` and `arc`. "
    "Use chapters_overview to see which outlines exist before continuing or resetting. "
    "If it is unclear which volume, arc or chapters the author means, ask them in plain text."
)


def chapters_generate(
    ctx: RunContext[OrchestratorDeps], message: str, volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Generate the chapter outlines of one arc, or refine the outlines it already has, as a background job.

    Args:
        message: The author's request, e.g. what the chapters should cover or what to change.
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    text = require_message(message)
    ws = ctx.deps.workspace
    refusal = arc_refusal(ws, v, a)
    if refusal:
        return refusal
    return start_job(
        JobRef("chapters", ws, volume=v, arc=a),
        lambda: ctx.deps.runtime.chapters_chat.start_message(ws, v, a, text),
        f"Chapter-outline generation started for volume {v}, arc {a}.",
    )


def chapters_continue(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Continue an interrupted chapter-outline run of one arc, generating only the missing outlines.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    ws = ctx.deps.workspace
    refusal = arc_refusal(ws, v, a)
    if refusal:
        return refusal
    return start_job(
        JobRef("chapters", ws, volume=v, arc=a),
        lambda: ctx.deps.runtime.chapters_chat.continue_incomplete(ws, v, a),
        f"Continuing the unfinished chapter outlines of volume {v}, arc {a}.",
    )


def chapters_reset(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Delete the chapter outlines and system-panel snapshots of one arc and clear its conversation.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    return attempt(
        lambda: ctx.deps.runtime.chapters_chat.reset(ctx.deps.workspace, v, a),
        lambda result: f"Reset volume {v}, arc {a}: its chapter outlines and conversation were deleted.",
    )


def chapters_overview(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Show an arc's chapter range, which chapter outlines exist (paths), and whether a run can be continued.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    from training.adaptive_builder import chapter_outline_resume_status

    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    ws = init_workspace(ctx.deps.workspace)
    target = find_arc(ws, v, a)
    if not target:
        return missing_arc(ws, v, a)
    job = ctx.deps.runtime.chapters_chat.job_status(ctx.deps.workspace, v, a)
    outline_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{v:02d}")
    chapters = range(target["start_ch"], target["end_ch"] + 1)
    paths = existing_paths(ws, [os.path.join(outline_dir, f"chapter_{ch:03d}.md") for ch in chapters])
    lines = [
        f"Volume {v}, arc {a} covers chapters {target['start_ch']}-{target['end_ch']}.",
        f"{len(paths)} chapter outlines on disk" + (":" if paths else "."),
        *(f"- {path}" for path in paths),
        progress_line(job, chapter_outline_resume_status(ws, v, a), "chapter outlines"),
    ]
    return done("\n".join(lines))


def system_panel_status(ctx: RunContext[OrchestratorDeps]) -> str:
    """Show the book's system-panel setting: its mode (auto, enabled, disabled), whether it is decided, and why."""
    from training.adaptive_builder import system_panel_status as panel_status

    status = panel_status(init_workspace(ctx.deps.workspace))
    return done("System panel: " + json.dumps(status, ensure_ascii=False))


def set_system_panel_mode(
    ctx: RunContext[OrchestratorDeps], mode: Literal["auto", "enabled", "disabled"],
) -> str:
    """Set whether chapter outlines track a system panel (the protagonist's stat/skill panel).

    Args:
        mode: `auto` lets the next outline run decide from the book design, `enabled` turns it on,
            `disabled` turns it off.
    """
    from training.adaptive_builder import configure_system_panel

    if manager_busy(ctx.deps.runtime.chapters_chat, ctx.deps.workspace):
        return refused("A chapter-outline job is running in this workspace. Stop it before changing the system panel.")
    return attempt(
        lambda: configure_system_panel(init_workspace(ctx.deps.workspace), str(mode or "auto")),
        lambda status: "System panel set: " + json.dumps(status, ensure_ascii=False),
    )


TOOLS: list = [
    Tool(chapters_generate, requires_approval=True),
    Tool(chapters_continue, requires_approval=True),
    Tool(chapters_reset, requires_approval=True),
    chapters_overview,
    system_panel_status,
    set_system_panel_mode,
]
