"""Fan a design review out to parallel lenses and save numbered critique points."""
from __future__ import annotations

import contextvars
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.prompt_loader import PromptLoader
from core.text_utils import parse_json_response
from training.adaptive_builder import (
    _call_design_llm,
    _read_json_file,
    _story_design_path,
    _write_json_file,
)
from training.design_lenses import load_lenses
from training.design_question import _require_llm, design_context

_MAX_FINDINGS = 5
_MAX_POINTS = 12


def _points_path(ws, scope):
    return _story_design_path(ws, "critique_points_%s.json" % scope)


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _stopped_payload(failed=None):
    return {"stopped": True, "points": [], "failed_lenses": list(failed or []), "answer_md": ""}


def critic_worker_count():
    try:
        return max(1, min(int(str(os.getenv("HARNESS_NOVEL_CRITIC_WORKERS") or "").strip()), 8))
    except (TypeError, ValueError):
        return 2


def _parse_findings(raw):
    items = parse_json_response(raw).get("findings")
    if not isinstance(items, list):
        raise ValueError("critic findings missing")
    findings = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        findings.append({
            "title": title,
            "detail": str(item.get("detail") or "").strip(),
            "evidence": str(item.get("evidence") or "").strip(),
        })
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


def _lens_names(value):
    if not isinstance(value, list):
        return []
    return [name for name in (str(item or "").strip() for item in value) if name]


def _parse_merged_points(raw):
    try:
        payload = parse_json_response(raw)
    except ValueError:
        payload = {}
    items = payload.get("points") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("The model did not return critique points.")
    points = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        points.append({
            "title": title,
            "detail": str(item.get("detail") or "").strip(),
            "lenses": _lens_names(item.get("lenses")),
        })
        if len(points) >= _MAX_POINTS:
            break
    if not points:
        raise RuntimeError("The model did not return critique points.")
    return points


def _format_findings(grouped):
    parts = []
    for name, findings in grouped:
        if not findings:
            continue
        lines = ["[%s]" % name]
        for item in findings:
            lines.append("- %s: %s" % (item["title"], item["detail"]))
            if item.get("evidence"):
                lines.append("  Evidence: %s" % item["evidence"])
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "(none)"


def _format_answer(points, failed_lenses):
    lines = []
    for point in points:
        line = "%d. **%s** — %s" % (point["number"], point["title"], point["detail"])
        names = ", ".join(point.get("lenses") or [])
        if names:
            line += " *(%s)*" % names
        lines.append(line)
    if failed_lenses:
        lines.append("")
        lines.append("Skipped lenses: %s." % ", ".join(failed_lenses))
    lines.append("")
    lines.append('You can say e.g. "apply 2 and 4" to apply those points.')
    return "\n".join(lines)


def _run_one_critic(llm, name, focus, message, design_files):
    prompt = PromptLoader.load(
        "design_critic", lens_name=name, lens_focus=focus,
        message=message, design_files=design_files,
    )
    return _parse_findings(_call_design_llm(llm, prompt, "design critic (%s)" % name))


def _collect_critic_results(llm, lenses, message, design_files, progress_callback, stop_event, total):
    grouped, failed, completed = [], [], 0
    workers = min(critic_worker_count(), max(1, len(lenses)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="design-critic") as executor:
        futures = {}
        for name, focus in lenses:
            if _stopped(stop_event):
                break
            future = executor.submit(
                contextvars.copy_context().run,
                _run_one_critic, llm, name, focus, message, design_files,
            )
            futures[future] = name
        for future in as_completed(futures):
            name = futures[future]
            if _stopped(stop_event):
                for pending in futures:
                    pending.cancel()
                return grouped, failed, True
            try:
                findings = future.result()
            except Exception:
                failed.append(name)
                findings = None
            if findings is not None:
                grouped.append((name, findings))
            completed += 1
            if progress_callback:
                progress_callback("critics", completed, total, "%s reviewed" % name)
    return grouped, failed, _stopped(stop_event)


def run_critique(ws, scope, message, progress_callback=None, stop_event=None):
    """Review design files through parallel lenses. Does not write design files."""
    if _stopped(stop_event):
        return _stopped_payload()
    design_files = design_context(ws, scope)
    if not design_files:
        raise RuntimeError("No design files are available to critique.")
    llm = _require_llm()
    lenses = load_lenses(ws)
    total = len(lenses) + 1
    grouped, failed, stopped = _collect_critic_results(
        llm, lenses, message, design_files, progress_callback, stop_event, total,
    )
    if stopped:
        return _stopped_payload(failed)
    if not grouped:
        raise RuntimeError("Every design critic failed.")
    prompt = PromptLoader.load(
        "design_critique_merge", message=message, findings=_format_findings(grouped),
    )
    numbered = []
    merged = _parse_merged_points(_call_design_llm(llm, prompt, "design critique merge"))
    for index, point in enumerate(merged, start=1):
        numbered.append({
            "number": index,
            "title": point["title"],
            "detail": point["detail"],
            "lenses": list(point.get("lenses") or []),
        })
    _write_json_file(_points_path(ws, scope), {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scope": scope,
        "message": message,
        "points": numbered,
    })
    if progress_callback:
        progress_callback("critics", total, total, "points merged")
    return {
        "points": numbered,
        "failed_lenses": failed,
        "answer_md": _format_answer(numbered, failed),
    }


def load_points(ws, scope):
    data = _read_json_file(_points_path(ws, scope))
    return data if isinstance(data, dict) else None


def clear_points(ws, scope):
    path = _points_path(ws, scope)
    if os.path.isfile(path):
        os.remove(path)


def _point_lookup(ws, scope):
    by_number = {}
    for item in (load_points(ws, scope) or {}).get("points") or []:
        number = item.get("number") if isinstance(item, dict) else None
        if isinstance(number, int) and not isinstance(number, bool):
            by_number[number] = item
    return by_number


def points_instruction(ws, scope, numbers):
    by_number = _point_lookup(ws, scope)
    lines, missing, seen = [], [], set()
    for number in numbers or []:
        if number in seen or not isinstance(number, int) or isinstance(number, bool):
            continue
        seen.add(number)
        item = by_number.get(number)
        if item:
            lines.append("%d. %s: %s" % (number, item.get("title") or "", item.get("detail") or ""))
        else:
            missing.append(number)
    text = ""
    if lines:
        text = "[Critique points to apply]\n" + "\n".join(lines)
    return {"text": text, "missing": missing}
