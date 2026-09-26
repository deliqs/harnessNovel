"""Tool result shapes the orchestrator UI understands.

A tool result reaches the UI as the `content` of a TOOL_CALL_RESULT event, and the transcript
keeps it as a tool message. Every orchestrator tool returns a JSON object with a `status`:

- `started`: a long job began in the background. `job` is the `JobRef` wire shape
  (`kind`, `workspace` and its locators) plus `url`, the existing status route to poll; see
  `deps.JOB_URLS` for the terminal statuses per kind. `message` says what started.
- `done`: the tool finished; `message` summarises the result.
- `refused`: a guard, validation or unexpected error stopped the tool; `message` says why.

Three results are plain text, not JSON, because pydantic-ai writes them rather than a tool:
a denied approval ("The author denied this call." plus " Their reason: <reason>" when given;
see `turn_input.prepare_turn`), an auto-denial when the
author sends a new message while an approval is pending (`pending.AUTO_DENIED`), and the retry
prompt of a tool that raised `ModelRetry` over a bad argument, which the model answers by
calling the tool again.
"""

from __future__ import annotations

import json

from core.prompt_trace import redact_sensitive_text
from webui.orchestrator.deps import JobRef
from webui.orchestrator.tools.shared import capped


def job_started(ref: JobRef, message: str) -> str:
    """A long job started in the background; the UI polls `job.url` and continues the chat when it ends."""
    return json.dumps({"status": "started", "message": capped(message, 500), "job": {**ref.to_dict(), "url": ref.url()}})


def done(message: str) -> str:
    return json.dumps({"status": "done", "message": capped(message)})


def refused(message: str) -> str:
    """An expected domain refusal (guard, validation) as a result the model can read and relay."""
    return json.dumps({"status": "refused", "message": capped(message, 500)})


def failed(exc: Exception) -> str:
    """An unexpected error as a redacted refusal, so it ends the tool call rather than the turn."""
    return refused(redact_sensitive_text(f"{type(exc).__name__}: {exc}"))
