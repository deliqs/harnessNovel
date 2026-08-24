"""Record the actual prompt of each model call for the Web workbench live view."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Callable


_TRACE_CALLBACK: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "harness_novel_prompt_trace_callback", default=None,
)
_FILE_LOCK = threading.Lock()


def record_prompt(prompt: str, model: str = "", label: str = "") -> dict:
    event = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": str(model or ""),
        "label": str(label or "model call"),
        "prompt": str(prompt or ""),
    }
    trace_file = os.getenv("HARNESS_NOVEL_PROMPT_TRACE_FILE", "").strip()
    if trace_file:
        try:
            path = Path(trace_file)
            with _FILE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
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
    """Capture model prompts on the current background-task thread without affecting other tasks."""
    token = _TRACE_CALLBACK.set(callback)
    try:
        yield
    finally:
        _TRACE_CALLBACK.reset(token)
