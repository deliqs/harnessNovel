"""Orchestrator tools for the world step."""

from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext, Tool

from core.workspace import init_workspace
from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, failed, job_started, refused

INSTRUCTIONS = (
    "This thread belongs to the World step: the target-world knowledge base the story draws on. "
    "It lives in file_system/world_knowledge/, with the finished sections under "
    "file_system/world_knowledge/worlds/_final/. "
    "Use world_chat to change or ask about the knowledge base, world_rebuild to rebuild it from the "
    "uploaded sources, and world_status_summary to check what exists before acting. "
    "Uploading sources or a chat guide happens in the page, not through you. "
    "If the author's request is ambiguous, ask them in plain text before calling a job tool."
)


def world_chat(ctx: RunContext[OrchestratorDeps], message: str) -> str:
    """Send one message to the world-knowledge chat, which edits or answers about the knowledge base.

    Starts a background job; the result says it started. Do not call it again until the job ends.

    Args:
        message: The author's request, in their words.
    """
    text = (message or "").strip()
    if not text:
        raise ModelRetry("world_chat needs a non-empty message.")
    runtime, workspace = ctx.deps.runtime, ctx.deps.workspace
    try:
        runtime.ensure_world_chat_can_start(workspace)
        runtime.world_chat.start_message(workspace, text)
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    return job_started(JobRef(kind="world", workspace=workspace), "World chat started.")


def world_stop(ctx: RunContext[OrchestratorDeps]) -> str:
    """Stop the running world-knowledge chat job."""
    try:
        status = ctx.deps.runtime.world_chat.stop(ctx.deps.workspace)
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    return done(f"World chat {status.get('status', 'stopping')}.")


def world_status_summary(ctx: RunContext[OrchestratorDeps]) -> str:
    """Report whether the world knowledge base is enabled, built, and whether a chat guide exists. Read-only."""
    try:
        return done(_status_text(ctx))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)


def _status_text(ctx: RunContext[OrchestratorDeps]) -> str:
    from core.world_knowledge import WORLD_SECTIONS, world_knowledge_status

    status = world_knowledge_status(init_workspace(ctx.deps.workspace))
    guide = ctx.deps.runtime.world_chat.guide_status(ctx.deps.workspace)
    lines = [
        f"Enabled: {'yes' if status['enabled'] else 'no'}.",
        f"Sources: {status['source_count']}.",
        (
            f"Final sections: {status['final_section_count']} of {len(WORLD_SECTIONS)}"
            f" ({'ready' if status['ready'] else 'not ready'})."
        ),
        f"Chat guide: {guide['path'] if guide.get('exists') else 'none'}.",
    ]
    return " ".join(lines)


def world_set_enabled(ctx: RunContext[OrchestratorDeps], enabled: bool) -> str:
    """Turn injecting the world knowledge base into later generation on or off.

    Args:
        enabled: True to use the knowledge base in later steps, False to ignore it.
    """
    from core.world_knowledge import set_world_knowledge_enabled

    try:
        final = set_world_knowledge_enabled(init_workspace(ctx.deps.workspace), enabled)
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    return done(f"World knowledge {'enabled' if final else 'disabled'}.")


def world_rebuild(ctx: RunContext[OrchestratorDeps], force: bool = False) -> str:
    """Rebuild the world knowledge base from the uploaded sources as a background task.

    Args:
        force: True to rebuild from scratch; False resumes from existing checkpoints.
    """
    runtime, workspace = ctx.deps.runtime, ctx.deps.workspace
    try:
        runtime.ensure_world_task_can_start(workspace)
        task = runtime.tasks.create("world_build", workspace, {"force": bool(force)})
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    ref = JobRef(kind="task", workspace=workspace, task_id=task.id)
    return job_started(ref, "World knowledge rebuild started.")


def world_guide_reset(ctx: RunContext[OrchestratorDeps]) -> str:
    """Delete the author's world-chat guide so the chat falls back to its default behaviour."""
    try:
        ctx.deps.runtime.world_chat.reset_guide(ctx.deps.workspace)
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    return done("World chat guide cleared.")


TOOLS: list = [
    Tool(world_chat, requires_approval=True),
    world_stop,
    world_status_summary,
    world_set_enabled,
    Tool(world_rebuild, requires_approval=True),
    Tool(world_guide_reset, requires_approval=True),
]
