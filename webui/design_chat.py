"""Book design / stage design conversation management.

Each workspace + step (concept / stage) keeps its own conversation. Every
input regenerates from the latest artifacts; history is display-only and is
not limited or compressed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.workspace import init_workspace
from core.prompt_trace import capture_prompts


# Route 3 keywords: any match takes the extend/append path (scope=stage only).
_EXTEND_KEYWORDS = (
    "extend", "continue", "add stage", "append stage", "next stage", "new stage",
    "续写", "新增", "继续添加", "往后加", "加舞台", "追加舞台", "下一个舞台", "新舞台",
)
PHASE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:第\s*)?(?:阶段|phase)\s*0*(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)
STAGE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:舞台|stage)\s*0*(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

_SCOPE_FILES = {
    "concept": ("worldview.md", "rough_outline.md", "stage_outline.md"),
    "stage": ("long_mainline.md", "stage_roadmap.md"),
}


def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _is_real_content(text: str) -> bool:
    """Return whether file text is real design rather than empty or a model placeholder."""
    if not text or not text.strip():
        return False
    t = text.strip()
    if ("模型未返回" in t and "请重试或人工补充" in t) or (
        "Model did not return" in t and "please retry or fill in manually" in t
    ):
        return False
    return True


def _design_files_exist(ws, scope: str) -> bool:
    base = os.path.join(ws.file_system, "story_design")
    if not all(_is_real_content(_read_text(os.path.join(base, name))) for name in _SCOPE_FILES.get(scope, ())):
        return False
    if scope == "stage":
        state_path = os.path.join(base, "design_state.json")
        try:
            design_state = json.loads(_read_text(state_path) or "{}")
        except json.JSONDecodeError:
            design_state = {}
        if int(design_state.get("stage_pipeline_version") or 0) != 2:
            return False
        stage_outline = _read_text(os.path.join(base, "stage_outline.md"))
        stage_roadmap = _read_text(os.path.join(base, "stage_roadmap.md"))
        expected = sorted({
            int(value) for value in PHASE_HEADING_RE.findall(stage_outline)
        })
        generated = [
            int(value) for value in STAGE_HEADING_RE.findall(stage_roadmap)
        ]
        # Stopping mid-way is a valid breakpoint and must not be treated as a complete stage design that enters refine.
        if not expected or generated != list(range(1, len(expected) + 1)):
            return False
    return True


def _design_dir(ws) -> str:
    return os.path.join(ws.file_system, "story_design")


def _clear_system_panel_artifacts(ws) -> None:
    """Clear system-panel definitions and chapter snapshots derived from book/stage design."""
    try:
        os.remove(os.path.join(ws.file_system, "mechanics", "system_panel.json"))
    except FileNotFoundError:
        pass
    snapshots_dir = os.path.join(ws.file_system, "system_panels")
    if os.path.isdir(snapshots_dir):
        shutil.rmtree(snapshots_dir)


def _stage_resume_status(ws) -> dict[str, Any]:
    """Decide from consecutive on-disk stages whether a resume breakpoint exists."""
    base = _design_dir(ws)
    stage_outline = _read_text(os.path.join(base, "stage_outline.md"))
    stage_roadmap = _read_text(os.path.join(base, "stage_roadmap.md"))
    expected = len({
        int(value) for value in PHASE_HEADING_RE.findall(stage_outline)
    })
    generated = [
        int(value) for value in STAGE_HEADING_RE.findall(stage_roadmap)
    ]
    completed = 0
    for number in generated:
        if number != completed + 1:
            break
        completed += 1
    return {
        "can_resume": expected > 0 and completed < expected,
        "completed": completed,
        "total": expected,
        "next_stage": completed + 1 if expected > completed else None,
    }


class DesignConversation:
    def __init__(self, scope: str, path: Path):
        self.scope = scope
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
        if not isinstance(data, dict):
            return
        self.turns = [t for t in data.get("turns", []) if isinstance(t, dict)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"scope": self.scope, "turns": self.turns,
                 "updated_at": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def history(self) -> dict[str, Any]:
        return {"scope": self.scope, "turns": self.turns}

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


# Generated artifacts -> review path + display label
_RESULT_ARTIFACTS = {
    "concept": {
        "worldview": ("file_system/story_design/worldview.md", "Worldview"),
        "rough_outline": ("file_system/story_design/rough_outline.md", "Rough outline"),
        "stage_outline": ("file_system/story_design/stage_outline.md", "Phase outline"),
    },
    "stage": {
        "long_mainline": ("file_system/story_design/long_mainline.md", "Long mainline"),
        "stage_roadmap": ("file_system/story_design/stage_roadmap.md", "Stage roadmap"),
        "name_synopsis": ("file_system/novel_name_synopsis.md", "Title and synopsis"),
    },
}


def _extract_artifacts(scope: str, result: dict) -> list:
    """Extract artifacts saved by this generate/refine result (path + label)."""
    mapping = _RESULT_ARTIFACTS.get(scope, {})
    artifacts = []
    for key, (path, label) in mapping.items():
        if isinstance(result, dict) and result.get(key):
            artifacts.append({"path": path, "label": label})
    return artifacts


def _conversation_path(root: Path, workspace: str, scope: str) -> Path:
    return root / workspace / "file_system" / "story_design" / "conversation" / f"{scope}.json"


def _is_extend_intent(message: str) -> bool:
    return any(kw in message for kw in _EXTEND_KEYWORDS)


class DesignChatManager:
    def __init__(self, workspace_root: Path):
        self.root = Path(workspace_root)
        self._cache: dict[tuple[str, str], DesignConversation] = {}
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    def get(self, workspace: str, scope: str) -> DesignConversation:
        key = (workspace, scope)
        if key not in self._cache:
            self._cache[key] = DesignConversation(scope, _conversation_path(self.root, workspace, scope))
        return self._cache[key]

    def run_message(
        self, workspace: str, scope: str, message: str, attachments=None,
        use_new_reference=False, sync_updated_design=False,
        progress_callback=None, pause_event=None, stop_event=None,
        cancel_event=None,
    ) -> dict[str, Any]:
        """Unified chat entry: the first message generates the first draft; later messages refine it.

        attachments is [{name, content}, ...]; content is merged into the model
        instruction, while conversation history only stores a short display string.
        """
        ws = init_workspace(workspace)
        conv = self.get(workspace, scope)
        attachments = attachments or []

        combined_parts = [message.strip()]
        for att in attachments:
            name = str(att.get("name") or "attachment")
            content = str(att.get("content") or "").strip()
            if content:
                combined_parts.append(f"\n\n[Reference file: {name}]\n{content}")
        combined_for_llm = "\n".join(combined_parts).strip()
        if scope == "concept" and use_new_reference and not combined_for_llm:
            combined_for_llm = "Read the newly deconstructed chapters and only sync the last phase of the phase outline or append a new phase."
        if scope == "stage" and sync_updated_design and not combined_for_llm:
            combined_for_llm = "Sync the latest phase-outline changes and only adjust the last stage or append a new stage."

        if attachments:
            names = ", ".join(str(a.get("name") or "attachment") for a in attachments)
            display_text = f"{message.strip()}\n(attachments: {names})" if message.strip() else f"(attachments: {names})"
        else:
            display_text = message.strip()
        if not display_text and scope == "stage" and sync_updated_design:
            display_text = "Sync later stages"
        elif not display_text and scope == "concept" and use_new_reference:
            display_text = "Sync new deconstruction into the phase outline"
        if not combined_for_llm:
            raise ValueError("Enter inspiration or upload a file before sending.")

        conv.append_user(display_text)
        from training.adaptive_builder import (
            gen_design_concept, gen_stage_design, refine_design_concept, refine_stage_design,
            extend_stage_design, sync_stage_outline_from_new_reference,
        )
        from core.llm_provider import LLMCallCancelled

        progress_state = {"completed": 0, "total": 1}

        def report(phase: str, completed: int, total: int, detail: str) -> None:
            progress_state.update(completed=int(completed), total=max(1, int(total)))
            if progress_callback:
                progress_callback(phase, completed, total, detail)

        def stopped_stage_result() -> dict[str, Any]:
            base = _design_dir(ws)
            return {
                "long_mainline": _read_text(os.path.join(base, "long_mainline.md")),
                "stage_roadmap": _read_text(os.path.join(base, "stage_roadmap.md")),
                "adjustment_note": "This stage-design round has ended. Written long mainline and stages were kept.",
                "stopped": True,
            }

        def run_stage_operation(operation):
            """Cancel the current request and stop at the breakpoint; resume re-runs the step that has not been written yet."""
            while True:
                if stop_event is not None and stop_event.is_set():
                    return stopped_stage_result()
                if pause_event is not None and not pause_event.is_set():
                    report(
                        "paused", progress_state["completed"], progress_state["total"],
                        "Stage design is paused; click Resume to continue from the current breakpoint",
                    )
                    pause_event.wait()
                    if stop_event is not None and stop_event.is_set():
                        return stopped_stage_result()
                    if cancel_event is not None:
                        cancel_event.clear()
                try:
                    result = operation()
                except LLMCallCancelled:
                    if stop_event is not None and stop_event.is_set():
                        return stopped_stage_result()
                    report(
                        "paused", progress_state["completed"], progress_state["total"],
                        "The current model request is paused; click Resume to regenerate the current stage",
                    )
                    if pause_event is not None:
                        pause_event.wait()
                    if stop_event is not None and stop_event.is_set():
                        return stopped_stage_result()
                    if cancel_event is not None:
                        cancel_event.clear()
                    continue
                if stop_event is not None and stop_event.is_set():
                    stopped = stopped_stage_result()
                    # If the request already returned and artifacts were written, keep the completed write.
                    if isinstance(result, dict):
                        stopped.update({k: v for k, v in result.items() if v})
                        stopped["stopped"] = True
                        stopped["adjustment_note"] = "This stage-design round has ended. Completed content was kept."
                    return stopped
                return result

        is_initial = not _design_files_exist(ws, scope)
        is_concept_stage_sync = scope == "concept" and not is_initial and bool(use_new_reference)
        is_sync_extend = scope == "stage" and not is_initial and bool(sync_updated_design)
        is_extend = scope == "stage" and not is_initial and _is_extend_intent(combined_for_llm)
        if is_initial:
            if scope == "concept":
                result = gen_design_concept(
                    ws, creative_direction=combined_for_llm,
                    progress_callback=progress_callback,
                )
            else:
                result = run_stage_operation(
                    lambda: gen_stage_design(
                        ws, creative_direction=combined_for_llm,
                        progress_callback=report, cancel_event=cancel_event,
                    )
                )
            mode = "initial"
        elif is_concept_stage_sync:
            if progress_callback:
                progress_callback("generating", 0, 1, "Syncing new deconstruction into the phase outline")
            result = sync_stage_outline_from_new_reference(
                ws, instruction=combined_for_llm,
            )
            if progress_callback:
                progress_callback("completed", 1, 1, "Phase outline synced")
            mode = "concept_stage_sync"
        elif is_sync_extend or is_extend:
            if progress_callback:
                progress_callback("generating", 0, 1, "Updating the stage roadmap")
            result = run_stage_operation(
                lambda: extend_stage_design(
                    ws, instruction=combined_for_llm,
                    sync_updated_design=is_sync_extend,
                    cancel_event=cancel_event,
                )
            )
            if progress_callback:
                progress_callback("completed", 1, 1, "Stage roadmap updated")
            mode = "sync_extend" if is_sync_extend else "extend"
        else:
            if progress_callback and scope == "concept":
                progress_callback("generating", 0, 1, "Adjusting the design from the instruction")
            if scope == "concept":
                result = refine_design_concept(
                    ws, instruction=combined_for_llm, use_new_reference=False,
                )
            else:
                result = run_stage_operation(
                    lambda: refine_stage_design(
                        ws, instruction=combined_for_llm,
                        cancel_event=cancel_event,
                        progress_callback=report,
                        pause_event=pause_event,
                        stop_event=stop_event,
                    )
                )
            if progress_callback and scope == "concept":
                progress_callback("completed", 1, 1, "Design updated")
            mode = "refine"
        if not result:
            raise RuntimeError("No usable model is configured. Set the LLM API in the top-right first.")

        note = str(result.get("adjustment_note") or "").strip()
        if not note:
            note = ("First draft generated. You can keep sending adjustment requests." if mode == "initial"
                    else "Synced the last phase of the phase outline from the newly deconstructed chapters." if mode == "concept_stage_sync"
                    else "Synced the updated book design and appended later stages." if mode == "sync_extend"
                    else "Later stages appended." if mode == "extend" else "Updated from the instruction.")
        artifacts = _extract_artifacts(scope, result)
        conv.append_assistant(note, artifacts)
        conv.save()

        return {"mode": mode, "result": result, "conversation": conv.history()}

    def start_message(
        self, workspace: str, scope: str, message: str, attachments=None,
        use_new_reference=False, sync_updated_design=False,
    ) -> dict[str, Any]:
        """Run design chat in the background so the UI can poll three-step book-design progress."""
        if scope not in _SCOPE_FILES:
            raise ValueError("Design step must be concept or stage.")
        key = (workspace, scope)
        ws = init_workspace(workspace)
        is_initial = not _design_files_exist(ws, scope)
        total = 3 if scope == "concept" and is_initial else 1
        with self._jobs_lock:
            current = self._jobs.get(key)
            if current and current.get("status") in {"queued", "running", "pausing", "paused", "stopping"}:
                raise ValueError("This design step already has a generation task running.")
            pause_event = threading.Event()
            pause_event.set()
            stop_event = threading.Event()
            cancel_event = threading.Event()
            job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "phase": "queued",
                "completed": 0,
                "total": total,
                "progress_kind": "design_concept" if scope == "concept" else "stage_design",
                "message": "Task created, starting",
                "pause_event": pause_event,
                "stop_event": stop_event,
                "cancel_event": cancel_event,
                "prompt_history": [],
                "prompt_count": 0,
                "error": "",
            }
            self._jobs[key] = job

        def update(phase: str, completed: int, callback_total: int, detail: str) -> None:
            with self._jobs_lock:
                active = self._jobs.get(key)
                if not active or active.get("id") != job["id"]:
                    return
                active.update(
                    phase=phase,
                    completed=int(completed),
                    total=max(1, int(callback_total)),
                    message=detail,
                )

        def worker() -> None:
            def trace_prompt(event: dict) -> None:
                with self._jobs_lock:
                    active = self._jobs.get(key)
                    if not active or active.get("id") != job["id"]:
                        return
                    history = active.setdefault("prompt_history", [])
                    history.append(event)
                    del history[:-50]
                    active.update(
                        prompt_count=len(history),
                        current_prompt_id=event.get("id"),
                        prompt_model=event.get("model", ""),
                        prompt_created_at=event.get("created_at", ""),
                    )
            trace_context = capture_prompts(trace_prompt)
            trace_context.__enter__()
            try:
                response = self.run_message(
                    workspace, scope, message, attachments,
                    use_new_reference=use_new_reference,
                    sync_updated_design=sync_updated_design,
                    progress_callback=update,
                    pause_event=pause_event,
                    stop_event=stop_event,
                    cancel_event=cancel_event,
                )
                with self._jobs_lock:
                    active = self._jobs.get(key)
                    if active and active.get("id") == job["id"]:
                        stopped = bool((response.get("result") or {}).get("stopped"))
                        active.update(
                            status="stopped" if stopped else "completed",
                            phase="stopped" if stopped else "completed",
                            completed=(
                                active.get("completed", 0) if stopped
                                else active.get("total", total)
                            ),
                            message=(
                                "This stage-design round has ended" if stopped else
                                "Book design generated" if scope == "concept" else "Stage design generated"
                            ),
                            result={"mode": response.get("mode")},
                        )
            except Exception as exc:
                with self._jobs_lock:
                    active = self._jobs.get(key)
                    if active and active.get("id") == job["id"]:
                        active.update(
                            status="failed", phase="failed",
                            message="Generation failed", error=str(exc),
                        )
            finally:
                trace_context.__exit__(None, None, None)

        threading.Thread(
            target=worker, name=f"design-chat-{scope}", daemon=True,
        ).start()
        return self.job_status(workspace, scope)

    def job_status(self, workspace: str, scope: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace, scope))
            if not job:
                status = {
                    "status": "idle", "phase": "idle", "completed": 0,
                    "total": 0, "message": "", "error": "",
                }
            else:
                status = {
                key: value for key, value in job.items()
                if key not in {"pause_event", "stop_event", "cancel_event", "prompt_history"}
                }
        if scope == "stage" and status.get("status") in {"idle", "stopped", "failed"}:
            status.update(_stage_resume_status(init_workspace(workspace)))
        return status

    def prompts(self, workspace: str, scope: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get((workspace, scope))
            if not job:
                return {"items": []}
            return {
                "job_id": job.get("id"),
                "items": [dict(item) for item in job.get("prompt_history", [])],
            }

    def continue_incomplete(self, workspace: str, scope: str) -> dict[str, Any]:
        if scope != "stage":
            raise ValueError("Only stage design supports resume from a breakpoint.")
        resume = _stage_resume_status(init_workspace(workspace))
        if not resume.get("can_resume"):
            raise ValueError("There is no unfinished stage to continue.")
        return self.start_message(
            workspace, scope, "Continue generating unfinished stage design",
        )

    def pause(self, workspace: str, scope: str) -> dict[str, Any]:
        if scope != "stage":
            raise ValueError("Only stage design supports pause.")
        key = (workspace, scope)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"running", "pausing"}:
                raise ValueError("There is no pausable stage-design task.")
            job["pause_event"].clear()
            job["cancel_event"].set()
            job.update(status="pausing", phase="pausing", message="Pausing the current model request")
        return self.job_status(workspace, scope)

    def resume(self, workspace: str, scope: str) -> dict[str, Any]:
        if scope != "stage":
            raise ValueError("Only stage design supports resume.")
        key = (workspace, scope)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"paused", "pausing"}:
                raise ValueError("There is no paused stage-design task.")
            job["cancel_event"].clear()
            job["pause_event"].set()
            job.update(status="running", phase="generating", message="Resumed stage design")
        return self.job_status(workspace, scope)

    def stop(self, workspace: str, scope: str) -> dict[str, Any]:
        if scope != "stage":
            raise ValueError("Only stage design supports stop.")
        key = (workspace, scope)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if not job or job["status"] not in {"running", "pausing", "paused"}:
                raise ValueError("There is no stage-design task to stop.")
            job["stop_event"].set()
            job["cancel_event"].set()
            job["pause_event"].set()
            job.update(status="stopping", phase="stopping", message="Ending this stage-design round")
        return self.job_status(workspace, scope)

    def reset(self, workspace: str, scope: str) -> dict[str, Any]:
        """Clear the conversation and delete this step's design files so the next message generates a first draft again."""
        key = (workspace, scope)
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job and job.get("status") in {"queued", "running", "pausing", "paused", "stopping"}:
                raise ValueError("The current design task is still running. Stop it before resetting.")
            if job:
                job["prompt_history"] = []
                job["prompt_count"] = 0
                for field in ("current_prompt_id", "prompt_model", "prompt_created_at"):
                    job.pop(field, None)
        ws = init_workspace(workspace)
        for name in _SCOPE_FILES.get(scope, ()):
            try:
                os.remove(os.path.join(_design_dir(ws), name))
            except FileNotFoundError:
                pass
        if scope == "stage":
            # Title and synopsis are derived from the rough outline plus long mainline; after a stage-design reset the old copy is stale.
            try:
                os.remove(os.path.join(ws.file_system, "novel_name_synopsis.md"))
            except FileNotFoundError:
                pass
        if scope in {"concept", "stage"}:
            # The system panel is derived from the current book design and stage design. After an upstream reset, old definitions and all
            # chapter state is no longer trusted; the next chapter-outline run should decide and initialize again.
            _clear_system_panel_artifacts(ws)
        state_files = (
            ("chapter_usage_state.json", "design_state.json")
            if scope == "concept" else ("arc_usage_state.json",)
        )
        for name in state_files:
            try:
                os.remove(os.path.join(_design_dir(ws), name))
            except FileNotFoundError:
                pass
        conv = self.get(workspace, scope)
        conv.clear()
        return {"reset": True, "conversation": conv.history()}

    def clear(self, workspace: str, scope: str) -> dict[str, Any]:
        conv = self.get(workspace, scope)
        conv.clear()
        return {"cleared": True, "conversation": conv.history()}
