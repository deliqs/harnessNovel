"""Design-chat tools shared by the design (scope `concept`) and stage (scope `stage`) threads.

The scope follows from the thread's phase, so both phase modules can list the same functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_ai import ApprovalRequired, ModelRetry, RunContext

from webui.orchestrator.deps import JobRef, OrchestratorDeps
from webui.orchestrator.tools.results import done, failed, job_started, refused
from webui.orchestrator.tools.tags import author_tags, strip_tag_header

PHASE_SCOPES = {"design": "concept", "stage": "stage"}
READ_ONLY_MODES = ("question", "critique")
"""Chat modes that answer or review without changing design files, so they need no approval."""
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 4 * 1024 * 1024


def scope_of(ctx: RunContext[OrchestratorDeps]) -> str:
    return PHASE_SCOPES[ctx.deps.phase]


def _design_job(ctx: RunContext[OrchestratorDeps], message: str) -> str:
    return job_started(JobRef(kind="design", workspace=ctx.deps.workspace, scope=scope_of(ctx)), message)


def _upload_name(path: Path) -> str:
    """The author's file name; `/api/uploads` stores files as `<random hex>_<name>`."""
    prefix, _, rest = path.name.partition("_")
    return rest if rest and len(prefix) == 16 else path.name


def _attachments(uploads: Any, upload_ids: list[str]) -> list[dict[str, str]]:
    """Read the uploaded files server-side, after checking their sizes; the model only sees their ids."""
    paths = [uploads.resolve(str(upload_id)) for upload_id in upload_ids]
    total = 0
    for path in paths:
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment {_upload_name(path)} is over 2 MB; trim it and upload it again.")
        total += size
    if total > MAX_ATTACHMENTS_TOTAL_BYTES:
        raise ValueError("The attachments are over 4 MB together; attach fewer or smaller files.")
    return [{"name": _upload_name(path), "content": path.read_text(encoding="utf-8")} for path in paths]


def _resolves(uploads: Any, upload_id: str) -> bool:
    try:
        uploads.resolve(upload_id)
    except ValueError:
        return False
    return True


def _upload_ids(ctx: RunContext[OrchestratorDeps], tagged: tuple[str, ...], passed: list[str] | None) -> list[str]:
    """The tagged ids, which are authoritative, plus any the model passed that resolve to an upload."""
    uploads = ctx.deps.runtime.uploads
    extra = [str(upload_id) for upload_id in passed or [] if _resolves(uploads, str(upload_id))]
    return list(dict.fromkeys([*tagged, *extra]))


def _read_attachments(ctx: RunContext[OrchestratorDeps], upload_ids: list[str] | None) -> list[dict[str, str]] | str:
    """The attachments, or a refusal result when they cannot be used."""
    try:
        return _attachments(ctx.deps.runtime.uploads, upload_ids or [])
    except UnicodeDecodeError:
        return refused("Attachments must be UTF-8 text files.")
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)


def _start(ctx: RunContext[OrchestratorDeps], text: str, attachments: list, mode: str, flags: dict[str, bool]) -> str:
    try:
        ctx.deps.runtime.design_chat.start_message(
            ctx.deps.workspace, scope_of(ctx), text, attachments, chat_mode=mode, **flags,
        )
    except ValueError as exc:
        return refused(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected error must not end the turn
        return failed(exc)
    return _design_job(ctx, f"Design chat ({mode}) started.")


def start_design_chat(
    ctx: RunContext[OrchestratorDeps],
    message: str,
    mode: str,
    use_new_reference: bool,
    sync_updated_design: bool,
    attachment_upload_ids: list[str] | None,
) -> str:
    """The `design_chat` tool body for either scope; mirrors POST design/{scope}/chat.

    Modes that change files ask the author for approval first; question and critique do not.
    The tags on the author's latest message are the truth: their uploads and flags always apply.
    """
    tags = author_tags(ctx)
    flags = {
        "use_new_reference": bool(use_new_reference) or tags.use_new_reference,
        "sync_updated_design": bool(sync_updated_design) or tags.sync_updated_design,
    }
    if mode in READ_ONLY_MODES and any(flags.values()):
        return refused("Question and critique modes change no files, so they cannot sync the design.")
    attachments = _read_attachments(ctx, _upload_ids(ctx, tags.upload_ids, attachment_upload_ids))
    if isinstance(attachments, str):
        return attachments
    text = strip_tag_header(message or "").strip()
    if not (text or any(att["content"].strip() for att in attachments) or any(flags.values())):
        raise ModelRetry("design_chat needs a message, a non-empty attachment, or one of the sync flags.")
    if mode not in READ_ONLY_MODES and not ctx.tool_call_approved:
        raise ApprovalRequired()
    return _start(ctx, text, attachments, mode, flags)


def design_continue(ctx: RunContext[OrchestratorDeps]) -> str:
    """Resume generating the stage roadmap from the first unfinished stage, as a background job."""
    try:
        ctx.deps.runtime.design_chat.continue_incomplete(ctx.deps.workspace, scope_of(ctx))
    except ValueError as exc:
        return refused(str(exc))
    return _design_job(ctx, "Stage design continues from the first unfinished stage.")


def design_stop(ctx: RunContext[OrchestratorDeps]) -> str:
    """Stop the running stage-design job; stages already written are kept."""
    try:
        status = ctx.deps.runtime.design_chat.stop(ctx.deps.workspace, scope_of(ctx))
    except ValueError as exc:
        return refused(str(exc))
    return done(f"Stage design {status.get('status', 'stopping')}.")


def design_reset(ctx: RunContext[OrchestratorDeps]) -> str:
    """Delete this step's design files and its conversation so the next message drafts from scratch."""
    try:
        ctx.deps.runtime.design_chat.reset(ctx.deps.workspace, scope_of(ctx))
    except ValueError as exc:
        return refused(str(exc))
    return done("Design reset; its files and conversation were cleared.")


def design_lenses_status(ctx: RunContext[OrchestratorDeps]) -> str:
    """List the critic lenses a critique uses and whether the author uploaded their own. Read-only."""
    status = ctx.deps.runtime.design_chat.lens_status(ctx.deps.workspace)
    source = f"author file {status['path']}" if status.get("exists") else "built-in defaults"
    return done(f"Lenses ({source}): {', '.join(status.get('lenses') or []) or 'none'}.")
