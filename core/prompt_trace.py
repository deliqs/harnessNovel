"""Privacy-preserving prompt-call diagnostics for the local workbench.

Prompt text often contains unpublished source material and user instructions. The
default ``metadata`` mode records only a model, timestamp, and character count.
Set ``HARNESS_NOVEL_PROMPT_TRACE_MODE=full`` only for local debugging when the
trace destination is private. ``off`` disables prompt diagnostics completely.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.config import ConfigLoader, write_private_text


_TRACE_CALLBACK: ContextVar[Optional[Callable[[dict], None]]] = ContextVar(
    "harness_novel_prompt_trace_callback", default=None,
)
_FILE_LOCK = threading.Lock()
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?im)(\b(?:api[_ -]?key|authorization|access[_ -]?token|token|secret|password)\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def redact_sensitive_text(value: str) -> str:
    """Redact common credential forms before diagnostics ever retain them."""
    text = str(value or "")
    text = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", text)
    text = _CREDENTIAL_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    return _OPENAI_KEY_RE.sub("sk-[REDACTED]", text)


def _trace_mode() -> str:
    explicit = os.getenv("HARNESS_NOVEL_PROMPT_TRACE_MODE")
    legacy = os.getenv("HARNESS_NOVEL_PROMPT_TRACE")
    value = explicit if explicit is not None else legacy
    if value is None:
        value = ConfigLoader.get_prompt_trace_mode()
    value = str(value or "").strip().lower()
    if value in {"0", "false", "none", "off", "disabled"}:
        return "off"
    if value in {"full", "content", "debug"}:
        return "full"
    return "metadata"


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(os.getenv(name, default)), maximum))
    except (TypeError, ValueError):
        return default


def _append_trace(path: Path, event: dict) -> None:
    """Retain the newest bounded events using an atomic, user-private rewrite."""
    max_events = _positive_int("HARNESS_NOVEL_PROMPT_TRACE_MAX_EVENTS", 50, 500)
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing = []
    lines = existing[-(max_events - 1):] if max_events > 1 else []
    lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    write_private_text(path, "\n".join(lines) + "\n")


def record_prompt(prompt: str, model: str = "", label: str = "") -> dict:
    """Record safe prompt-call diagnostics and optionally full debug content."""
    mode = _trace_mode()
    if mode == "off":
        return {}

    original_prompt = str(prompt or "")
    event = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": str(model or ""),
        "label": str(label or "model call"),
        "prompt_chars": len(original_prompt),
        "trace_mode": mode,
        "content_recorded": mode == "full",
    }
    if mode == "full":
        max_chars = _positive_int("HARNESS_NOVEL_PROMPT_TRACE_MAX_CHARS", 16000, 200000)
        safe_prompt = redact_sensitive_text(original_prompt)
        event["prompt"] = safe_prompt[:max_chars]
        event["prompt_truncated"] = len(safe_prompt) > max_chars

    trace_file = os.getenv("HARNESS_NOVEL_PROMPT_TRACE_FILE", "").strip()
    if trace_file:
        try:
            path = Path(trace_file)
            with _FILE_LOCK:
                _append_trace(path, event)
        except OSError:
            pass
    callback = _TRACE_CALLBACK.get()
    if callback:
        try:
            callback(dict(event))
        except Exception:
            # Prompt display is observational; it must not interrupt draft generation.
            pass
    return event


@contextmanager
def capture_prompts(callback: Callable[[dict], None]):
    """Capture safe prompt diagnostics on one background-task thread."""
    token = _TRACE_CALLBACK.set(callback)
    try:
        yield
    finally:
        _TRACE_CALLBACK.reset(token)
