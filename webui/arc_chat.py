"""Story-arc conversation management.

Each workspace + volume keeps its own conversation. The first message generates
all arcs for the volume; later messages refine the current arcs. History is
display-only and is not limited.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.workspace import init_workspace
from core.prompt_trace import capture_prompts


def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _arc_files_exist(ws, volume: int) -> bool:
    """Return whether the volume already has story-arc files."""
    arc_dir = os.path.join(ws.file_system, "story_arcs", f"vol_{volume:02d}")
    if not os.path.isdir(arc_dir):
        return False
    for fname in os.listdir(arc_dir):
        if fname.startswith("arc_") and fname.endswith(".md"):
            return True
    return False


def _conversation_path(root: Path, workspace: str, volume: int) -> Path:
    return root / workspace / "file_system" / "story_arcs" / f"vol_{volume:02d}" / "conversation.json"


class ArcsConversation:
    def __init__(self, volume: int, path: Path):
        self.volume = volume
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
                {"volume": self.volume, "turns": self.turns,
                 "updated_at": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def history(self) -> dict[str, Any]:
        return {"volume": self.volume, "turns": self.turns}

    def append_user(self, content: str) -> None:
        self.turns.append({"role": "user", "content": content, "at": datetime.now().isoformat(timespec="seconds")})

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


class ArcsChatManager:
    def __init__(self, workspace_root: Path):
        self.root = Path(workspace_root)
        self._cache: dict[tuple[str, int], ArcsConversation] = {}
        self._jobs: dict[tuple[str, int], dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    def get(self, workspace: str, volume: int) -> ArcsConversation:
        key = (workspace, volume)
        if key not in self._cache:
            self._cache[key] = ArcsConversation(volume, _conversation_path(self.root, workspace, volume))
        return self._cache[key]

    def history(self, workspace: str, volume: int) -> dict[str, Any]:
        """Return the conversation plus whether the current stage has real arc files."""
        ws = init_workspace(workspace)
        history = self.get(workspace, volume).history()
        history["has_arcs"] = _arc_files_exist(ws, volume)
        return history

    def run_message(self, workspace: str, volume: int, message: str) -> dict[str, Any]:
        """Unified chat entry: the first message generates the volume's arcs; later messages refine them."""
        ws = init_workspace(workspace)
        conv = self.get(workspace, volume)
        display_text = message.strip()
        if not display_text:
            raise ValueError("Enter content before sending.")

        conv.append_user(display_text)
        from training.adaptive_builder import gen_story_arcs, refine_story_arcs

        is_initial = not _arc_files_exist(ws, volume)
        if is_initial:
            # Generate all story arcs for the volume from the message
            # gen_story_arcs runs serial generation internally
            result = gen_story_arcs(ws, volume=volume)
            mode = "initial"
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result["error"]))
            if not isinstance(result, dict) or not result.get("artifacts"):
                raise RuntimeError(
                    f"Generation failed: no story arcs were produced. Confirm stage {volume} is designed (the matching stage exists in stage_roadmap.md and includes Planned chapters) and that an LLM API is configured."
                )
        else:
            result = refine_story_arcs(ws, volume, instruction=display_text)
            mode = "refine"
        if not result:
            raise RuntimeError("No usable model is configured. Set the LLM API in the top-right first.")

        note = str(result.get("adjustment_note") or "").strip()
        if not note:
            note = f"Generated story arcs for volume {volume}." if mode == "initial" else "Adjusted from the instruction."

        # Extract artifact links
        artifacts = result.get("artifacts") or []
        if not artifacts:
            # Scan the volume's arc files from disk
            arc_dir = os.path.join(ws.file_system, "story_arcs", f"vol_{volume:02d}")
            if os.path.isdir(arc_dir):
                for fname in sorted(os.listdir(arc_dir)):
                    if fname.startswith("arc_") and fname.endswith(".md"):
                        import re
                        m = re.match(r'arc_(\d+)_ch(\d+)_(\d+)\.md$', fname)
                        if m:
                            arc_idx = int(m.group(1))
                            s_ch = int(m.group(2))
                            e_ch = int(m.group(3))
                            artifacts.append({
                                "path": f"file_system/story_arcs/vol_{volume:02d}/{fname}",
                                "label": f"Arc {arc_idx} (chapters {s_ch}-{e_ch})",
                            })
        conv.append_assistant(note, artifacts)
        conv.save()
        return {"mode": mode, "result": result, "conversation": conv.history()}

    def start_message(self, workspace: str, volume: int, message: str, resume_incomplete: bool = False) -> dict[str, Any]:
        """Run chat generation in the background so the UI can read progress and pause."""
        key = (workspace, volume)
        display_text = message.strip()
        if not display_text:
            raise ValueError("Enter content before sending.")
        with self._jobs_lock:
            current = self._jobs.get(key)
            if current and current["status"] in {"running", "pausing", "paused", "stopping"}:
                raise ValueError("This stage already has a generation task running.")
            pause_event = threading.Event()
            pause_event.set()
            stop_event = threading.Event()
            cancel_event = threading.Event()
            job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "phase": "queued",
                "completed": 0,
                "total": 0,
                "progress_kind": "story_arcs",
                "message": "Task created, starting",
                "pause_event": pause_event,
                "stop_event": stop_event,
                "cancel_event": cancel_event,
                "prompt_history": [],
                "prompt_count": 0,
                "error": "",
            }
            self._jobs[key] = job
        conv = self.get(workspace, volume)
        if not resume_incomplete:
            conv.append_user(display_text)
            conv.save()

        def update(phase: str, completed: int, total: int, detail: str) -> None:
            with self._jobs_lock:
                active = self._jobs.get(key)
                if not active or active["id"] != job["id"]:
                    return
                active.update(
                    phase=phase,
                    completed=completed,
                    total=total,
                    message=detail,
                    status="paused" if phase == "paused" else active["status"],
                )

        def worker() -> None:
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
            trace_context = capture_prompts(trace_prompt)
            trace_context.__enter__()
            try:
                ws = init_workspace(workspace)
                conv = self.get(workspace, volume)
                from training.adaptive_builder import gen_story_arcs, refine_story_arcs_serial
                is_initial = resume_incomplete or not _arc_files_exist(ws, volume)
                if is_initial:
                    result = gen_story_arcs(
                        ws, volume=volume, progress_callback=update,
                        pause_event=pause_event, stop_event=stop_event,
                        cancel_event=cancel_event,
                    )
                    mode = "resume" if resume_incomplete else "initial"
                else:
                    with self._jobs_lock:
                        active = self._jobs.get(key)
                        if active and active["id"] == job["id"]:
                            active["progress_kind"] = "serial_refine"
                    result = refine_story_arcs_serial(
                        ws, volume, instruction=display_text,
                        progress_callback=update,
                        pause_event=pause_event,
                        stop_event=stop_event,
                        cancel_event=cancel_event,
                    )
                    mode = "refine"
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(str(result["error"]))
                if not result:
                    raise RuntimeError("No usable model is configured. Set the LLM API in the top-right first.")
                artifacts = result.get("artifacts") or []
                note = str(result.get("adjustment_note") or "").strip()
                if not note:
                    note = f"Generated story arcs for volume {volume}." if mode == "initial" else "Adjusted from the instruction."
                conv.append_assistant(note, artifacts)
                conv.save()
                with self._jobs_lock:
                    active = self._jobs.get(key)
                    if active and active["id"] == job["id"]:
                        stopped = bool(result.get("stopped"))
                        active.update(
                            status="stopped" if stopped else "completed",
                            phase="stopped" if stopped else "completed",
                            message=note,
                            result={"mode": mode},
                        )
            except Exception as exc:
                with self._jobs_lock:
                    active = self._jobs.get(key)
                    if active and active["id"] == job["id"]:
                        active.update(status="failed", phase="failed", message="Generation failed", error=str(exc))
            finally:
                trace_context.__exit__(None, None, None)

        threading.Thread(target=worker, name=f"arcs-chat-{volume}", daemon=True).start()
        return self.job_status(workspace, volume)

    def job_status(self, workspace: str, volume: int) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace, volume))
            if not job:
                from training.adaptive_builder import story_arc_resume_status
                ws = init_workspace(workspace)
                resume = story_arc_resume_status(ws, volume)
                return {
                    "status": "idle",
                    "phase": "idle",
                    "message": "",
                    **resume,
                }
            return {
                k: v for k, v in job.items()
                if k not in {"pause_event", "stop_event", "cancel_event", "prompt_history"}
            }

    def prompts(self, workspace: str, volume: int) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace, volume))
            return {
                "job_id": job.get("id") if job else None,
                "items": [dict(item) for item in (job or {}).get("prompt_history", [])],
            }

    def continue_incomplete(self, workspace: str, volume: int) -> dict[str, Any]:
        from training.adaptive_builder import story_arc_resume_status
        ws = init_workspace(workspace)
        resume = story_arc_resume_status(ws, volume)
        if not resume.get("can_resume"):
            raise ValueError("This stage has no unfinished story arcs to continue.")
        return self.start_message(
            workspace, volume, "Continue generating unfinished story arcs",
            resume_incomplete=True,
        )

    def pause(self, workspace: str, volume: int) -> dict[str, Any]:
        key = (workspace, volume)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"running", "pausing"}:
                raise ValueError("There is no pausable generation task.")
            job["pause_event"].clear()
            job["cancel_event"].set()
            job.update(status="pausing", message="Pausing the current model request")
        return self.job_status(workspace, volume)

    def resume(self, workspace: str, volume: int) -> dict[str, Any]:
        key = (workspace, volume)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"paused", "pausing"}:
                raise ValueError("There is no paused generation task.")
            job["cancel_event"].clear()
            job["pause_event"].set()
            job.update(status="running", phase="generating", message="Resumed generation")
        return self.job_status(workspace, volume)

    def stop(self, workspace: str, volume: int) -> dict[str, Any]:
        key = (workspace, volume)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"running", "pausing", "paused"}:
                raise ValueError("There is no generation task to stop.")
            job["stop_event"].set()
            job["cancel_event"].set()
            job["pause_event"].set()
            job.update(status="stopping", phase="stopping", message="Ending this generation round")
        return self.job_status(workspace, volume)

    def reset(self, workspace: str, volume: int) -> dict[str, Any]:
        """Clear only this stage's conversation and story arcs; other stages are unchanged."""
        import re
        key = (workspace, volume)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job and job.get("status") in {"running", "pausing", "paused", "stopping"}:
                raise ValueError("This stage is still generating. Stop the task before resetting.")
            if job:
                job["prompt_history"] = []
                job["prompt_count"] = 0
                for field in ("current_prompt_id", "prompt_model", "prompt_created_at"):
                    job.pop(field, None)
        ws = init_workspace(workspace)
        arc_dir = os.path.join(ws.file_system, "story_arcs", f"vol_{volume:02d}")
        if os.path.isdir(arc_dir):
            for fname in list(os.listdir(arc_dir)):
                if re.match(r'^arc_\d+_ch\d+_\d+\.md$', fname) or fname == "arcs_index.json":
                    try:
                        os.remove(os.path.join(arc_dir, fname))
                    except FileNotFoundError:
                        pass
        conv = self.get(workspace, volume)
        conv.clear()
        return {"reset": True, "conversation": conv.history()}

    def clear(self, workspace: str, volume: int) -> dict[str, Any]:
        conv = self.get(workspace, volume)
        conv.clear()
        return {"cleared": True, "conversation": conv.history()}
