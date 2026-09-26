"""Orchestrator tools for the design step."""

from __future__ import annotations

from typing import Literal

from pydantic_ai import RunContext, Tool

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.tools.design_ops import (
    design_lenses_status,
    design_reset,
    start_design_chat,
)

INSTRUCTIONS = (
    "This thread belongs to the Design step: the worldview, rough outline and phase outline of the book, "
    "written to file_system/story_design/worldview.md, rough_outline.md and stage_outline.md. "
    "Use design_chat for everything the author says about the design: mode chat drafts or changes it, "
    "question answers without changing files, critique reviews it through the critic lenses. "
    "With no design yet, design_chat in mode chat drafts it; for a full rewrite, call design_reset and then "
    "design_chat. "
    "If the author's request is ambiguous, ask them in plain text before calling a job tool. "
    "Pass the ids of `[attached upload <id>: <name>]` tags as attachment_upload_ids; "
    "a `[use_new_reference]` tag means use_new_reference=true."
)


def design_chat(
    ctx: RunContext[OrchestratorDeps],
    message: str,
    mode: Literal["chat", "question", "critique"] = "chat",
    use_new_reference: bool = False,
    sync_updated_design: bool = False,
    attachment_upload_ids: list[str] | None = None,
) -> str:
    """Send one message to the book-design chat as a background job.

    Args:
        message: The author's request, in their words.
        mode: chat drafts or changes the design; question answers without changing files;
            critique reviews the design through the critic lenses.
        use_new_reference: True syncs newly deconstructed reference chapters into the phase outline.
        sync_updated_design: Leave False on this step.
        attachment_upload_ids: Upload ids of files the author attached: the ids from `[attached upload <id>: <name>]` tags in the author's message.
    """
    return start_design_chat(ctx, message, mode, use_new_reference, sync_updated_design, attachment_upload_ids)


TOOLS: list = [
    design_chat,
    Tool(design_reset, requires_approval=True),
    design_lenses_status,
]
