"""Target-world conversation management.

Each workspace keeps one conversation. History is display-only and is not
limited. A message is a handful of model calls, so stop is enough; there is
no pause/resume.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.prompt_trace import capture_prompts
from core.world_chat import (
    apply_world_chat_message,
    chat_guide_status,
    reset_chat_guide,
    save_chat_guide,
)
from core.workspace import init_workspace
from training.adaptive_builder import _get_lite_llm as _get_llm


NO_USABLE_MODEL = "No usable model is configured. Set the LLM API in the top-right first."
_EVENT_KEYS = ("pause_event", "stop_event", "cancel_event", "prompt_history")
_ACTIVE = {"running", "pausing", "paused", "stopping"}


def _conversation_path(root: Path, workspace: str) -> Path:
    return root / workspace / "file_system" / "world_knowledge" / "conversation.json"


class WorldConversation:
    def __init__(self, path: Path):
        self.path = path
        self.turns: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self.turns = [t for t in data.get("turns", []) if isinstance(t, dict)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"turns": self.turns,
                 "updated_at": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def history(self) -> dict[str, Any]:
        return {"turns": self.turns}

    def append_user(self, content: str) -> None:
        self.turns.append({
            "role": "user", "content": content,
            "at": datetime.now().isoformat(timespec="seconds"),
        })

    def append_assistant(self, note: str, artifacts=None) -> None:
        self.turns.append({
            "role": "assistant",
            "content": note,
            "at": datetime.now().isoformat(timespec="seconds"),
            "artifacts": artifacts or [],
        })

    def clear(self) -> None:
        self.turns = []
        self.save()


class WorldChatManager:
    def __init__(self, workspace_root: Path):
        self.root = Path(workspace_root)
        self._cache: dict[tuple[str], WorldConversation] = {}
        self._jobs: dict[tuple[str], dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    def get(self, workspace: str) -> WorldConversation:
        key = (workspace,)
        if key not in self._cache:
            self._cache[key] = WorldConversation(_conversation_path(self.root, workspace))
        return self._cache[key]

    def history(self, workspace: str) -> dict[str, Any]:
        history = self.get(workspace).history()
        history["guide"] = chat_guide_status(init_workspace(workspace))
        return history

    def _begin_job(self, key: tuple[str]) -> dict[str, Any]:
        with self._jobs_lock:
            current = self._jobs.get(key)
            if current and current["status"] in _ACTIVE:
                raise ValueError("This workspace already has a world-chat task running.")
            pause_event = threading.Event()
            pause_event.set()
            job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "phase": "queued",
                "completed": 0,
                "total": 0,
                "progress_kind": "world_chat",
                "message": "Task created, starting",
                "pause_event": pause_event,
                "stop_event": threading.Event(),
                "cancel_event": threading.Event(),
                "prompt_history": [],
                "prompt_count": 0,
                "error": "",
            }
            self._jobs[key] = job
            return job

    def _touch_job(self, key: tuple[str], job: dict[str, Any], **fields: Any) -> None:
        with self._jobs_lock:
            active = self._jobs.get(key)
            if active and active["id"] == job["id"]:
                active.update(fields)

    def start_message(self, workspace: str, message: str) -> dict[str, Any]:
        display_text = message.strip()
        if not display_text:
            raise ValueError("Enter content before sending.")
        key = (workspace,)
        job = self._begin_job(key)
        conv = self.get(workspace)
        prior_turns = list(conv.turns)
        conv.append_user(display_text)
        conv.save()
        threading.Thread(
            target=self._run_message,
            args=(key, job, workspace, display_text, prior_turns),
            name="world-chat",
            daemon=True,
        ).start()
        return self.job_status(workspace)

    def _run_message(self, key, job, workspace, message, prior_turns) -> None:
        def trace_prompt(event: dict) -> None:
            with self._jobs_lock:
                active = self._jobs.get(key)
                if not active or active["id"] != job["id"]:
                    return
                history = active.setdefault("prompt_history", [])
                history.append(event)
                del history[:-50]
                active.update(
                    prompt_count=len(history), current_prompt_id=event.get("id"),
                    prompt_model=event.get("model", ""),
                    prompt_created_at=event.get("created_at", ""),
                )

        def update(phase: str, completed: int, total: int, detail: str) -> None:
            self._touch_job(key, job, phase=phase, completed=completed, total=total, message=detail)

        stop_event = job["stop_event"]
        trace_context = capture_prompts(trace_prompt)
        trace_context.__enter__()
        try:
            self._apply_message(key, job, workspace, message, prior_turns, stop_event, update)
        except Exception as exc:
            self._fail_job(key, job, stop_event, exc)
        finally:
            trace_context.__exit__(None, None, None)

    def _apply_message(self, key, job, workspace, message, prior_turns, stop_event, update) -> None:
        llm = _get_llm()
        if not llm:
            raise RuntimeError(NO_USABLE_MODEL)
        conv = self.get(workspace)
        result = apply_world_chat_message(
            init_workspace(workspace), llm, message, prior_turns,
            should_stop=stop_event.is_set, progress=update,
        )
        conv.append_assistant(str(result.get("reply") or ""), result.get("artifacts") or [])
        conv.save()
        self._touch_job(
            key, job,
            status="completed", phase="completed",
            message=str(result.get("reply") or ""),
            result={"changed_sections": result.get("changed_sections") or []},
        )

    def _fail_job(self, key, job, stop_event, exc: Exception) -> None:
        stopped = stop_event.is_set()
        self._touch_job(
            key, job,
            status="stopped" if stopped else "failed",
            phase="stopped" if stopped else "failed",
            message=str(exc) if stopped else "Generation failed",
            error=str(exc),
        )

    def job_status(self, workspace: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace,))
            if not job:
                return {"status": "idle", "phase": "idle", "message": ""}
            return {k: v for k, v in job.items() if k not in _EVENT_KEYS}

    def prompts(self, workspace: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace,))
            return {
                "job_id": job.get("id") if job else None,
                "items": [dict(item) for item in (job or {}).get("prompt_history", [])],
            }

    def stop(self, workspace: str) -> dict[str, Any]:
        key = (workspace,)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"running", "pausing", "paused"}:
                raise ValueError("There is no generation task to stop.")
            job["stop_event"].set()
            job["cancel_event"].set()
            job["pause_event"].set()
            job.update(status="stopping", phase="stopping", message="Ending this generation round")
        return self.job_status(workspace)

    def clear(self, workspace: str) -> dict[str, Any]:
        key = (workspace,)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job and job.get("status") in _ACTIVE:
                raise ValueError("This workspace still has a world-chat task. Stop the chat first.")
        conv = self.get(workspace)
        conv.clear()
        return {"cleared": True, "conversation": self.history(workspace)}

    def guide_status(self, workspace: str) -> dict[str, Any]:
        return chat_guide_status(init_workspace(workspace))

    def save_guide(self, workspace: str, content: str) -> dict[str, Any]:
        save_chat_guide(init_workspace(workspace), content)
        return self.guide_status(workspace)

    def reset_guide(self, workspace: str) -> dict[str, Any]:
        reset_chat_guide(init_workspace(workspace))
        return self.guide_status(workspace)
