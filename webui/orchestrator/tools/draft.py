"""Orchestrator tools for the draft step."""

from __future__ import annotations

import os
from typing import Optional

from pydantic_ai import ModelRetry, RunContext, Tool

from core.chapter_utils import resolve_chapter_draft_path
from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, refused
from webui.orchestrator.tools.scoped import (
    arc_refusal,
    attempt,
    existing_paths,
    find_arc,
    missing_arc,
    progress_line,
    require_message,
    selected,
    start_job,
)
from webui.orchestrator.tools.tags import author_tags, strip_tag_header

INSTRUCTIONS = (
    "This thread belongs to the Draft step: the chapter drafts of the selected arc. "
    "Generating writes one draft per chapter of the arc to `file_system/chapters/vol_NN/NNN_chapter_N.md`, "
    "keeping the pre-humanize text under `file_system/drafts/vol_NN/raw_chapters/`; "
    "a later message revises the existing drafts. Drafts are humanized unless the author asks otherwise; "
    "a `[humanize: off]` tag on the author's message means pass humanize=false to draft_generate. "
    "Marking a draft final (set_finalized_chapters) locks it and its outline against later runs. "
    "Tools use the volume and arc selected in the UI unless you pass `volume` and `arc`. "
    "If it is unclear which volume, arc or chapters the author means, ask them in plain text."
)


def draft_generate(
    ctx: RunContext[OrchestratorDeps],
    message: str,
    volume: Optional[int] = None,
    arc: Optional[int] = None,
    humanize: bool = True,
) -> str:
    """Write the chapter drafts of one arc, or revise the drafts it already has, as a background job.

    Args:
        message: The author's request, e.g. what to write or what to change.
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
        humanize: Run the humanizing pass over the drafts. Leave on unless the author asks otherwise;
            a `[humanize: off]` tag on the author's message means false.
    """
    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    text = require_message(strip_tag_header(message or ""))
    humanize = humanize is not False and not author_tags(ctx).humanize_off
    ws = ctx.deps.workspace
    refusal = arc_refusal(ws, v, a)
    if refusal:
        return refusal
    return start_job(
        JobRef("drafts", ws, volume=v, arc=a),
        lambda: ctx.deps.runtime.draft_chat.start_message(ws, v, a, text, humanize=humanize),
        f"Draft generation started for volume {v}, arc {a}.",
    )


def draft_continue(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Continue an interrupted draft run of one arc, writing only the missing chapters.

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
        JobRef("drafts", ws, volume=v, arc=a),
        lambda: ctx.deps.runtime.draft_chat.continue_incomplete(ws, v, a),
        f"Continuing the unfinished drafts of volume {v}, arc {a}.",
    )


def draft_reset(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Delete the drafts, pre-humanize snapshots and final markers of one arc and clear its conversation.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    return attempt(
        lambda: ctx.deps.runtime.draft_chat.reset(ctx.deps.workspace, v, a),
        lambda result: (
            f"Reset volume {v}, arc {a} (chapters {result.get('start_chapter')}-{result.get('end_chapter')}): "
            f"deleted {result.get('deleted', 0)} files and cleared the conversation."
        ),
    )


def draft_overview(
    ctx: RunContext[OrchestratorDeps], volume: Optional[int] = None, arc: Optional[int] = None,
) -> str:
    """Show an arc's chapter range, which drafts exist (paths), which are final, and whether a run can be continued.

    Args:
        volume: Volume number. Defaults to the volume selected in the UI.
        arc: Arc number within the volume. Defaults to the arc selected in the UI.
    """
    from training.adaptive_builder import chapter_draft_resume_status, chapter_finalization_status

    v, a = selected(ctx, "volume", volume), selected(ctx, "arc", arc)
    ws = init_workspace(ctx.deps.workspace)
    target = find_arc(ws, v, a)
    if not target:
        return missing_arc(ws, v, a)
    job = ctx.deps.runtime.draft_chat.job_status(ctx.deps.workspace, v, a)
    chapters = range(target["start_ch"], target["end_ch"] + 1)
    draft_dir = os.path.join(ws.file_system, "chapters", f"vol_{v:02d}")
    paths = existing_paths(ws, [resolve_chapter_draft_path(draft_dir, ch) for ch in chapters])
    records = chapter_finalization_status(ws)["drafts"].get(f"vol_{v:02d}", {})
    final = [str(ch) for ch in chapters if str(ch) in records]
    lines = [
        f"Volume {v}, arc {a} covers chapters {target['start_ch']}-{target['end_ch']}.",
        f"{len(paths)} drafts on disk" + (":" if paths else "."),
        *(f"- {path}" for path in paths),
        f"Final: {', '.join(final) or 'none'}.",
        progress_line(job, chapter_draft_resume_status(ws, v, a), "chapter drafts"),
    ]
    return done("\n".join(lines))


def writing_guide_status(ctx: RunContext[OrchestratorDeps]) -> str:
    """Show which writing guide the drafts follow: the author's uploaded guide or the project default."""
    status = ctx.deps.runtime.draft_chat.writing_guide_status(ctx.deps.workspace)
    if status.get("custom"):
        return done(f"{status.get('name')}: file_system/writing/system_prompt.md (uploaded by the author).")
    return done(f"{status.get('name')}; no custom guide at file_system/writing/system_prompt.md.")


def set_finalized_chapters(
    ctx: RunContext[OrchestratorDeps], chapters: list[int], finalized: bool, volume: Optional[int] = None,
) -> str:
    """Mark chapter drafts as final (locking them and their outlines) or unmark them.

    Args:
        chapters: Chapter numbers, e.g. [3, 4]. Each needs a non-empty draft to be marked final.
        finalized: True marks the chapters final; False unmarks them.
        volume: Volume number. Defaults to the volume selected in the UI.
    """
    from training.adaptive_builder import set_chapter_finalized

    v = selected(ctx, "volume", volume)
    if not chapters:
        raise ModelRetry("`chapters` is empty. Pass the chapter numbers to mark, e.g. [3, 4].")
    ws = init_workspace(ctx.deps.workspace)
    status: dict = {}
    for chapter in chapters:
        try:
            status = set_chapter_finalized(ws, "drafts", v, int(chapter), bool(finalized))
        except ValueError as exc:
            return refused(f"Volume {v}, chapter {chapter}: {exc} Chapters before it in the list were applied.")
    final = ", ".join(sorted(status.get("drafts", {}).get(f"vol_{v:02d}", {}), key=int)) or "none"
    verb = "marked final" if finalized else "unmarked"
    return done(f"Volume {v}: chapters {', '.join(map(str, chapters))} {verb}. Final drafts now: {final}.")


TOOLS: list = [
    Tool(draft_generate, requires_approval=True),
    Tool(draft_continue, requires_approval=True),
    Tool(draft_reset, requires_approval=True),
    draft_overview,
    writing_guide_status,
    Tool(set_finalized_chapters, requires_approval=True),
]
