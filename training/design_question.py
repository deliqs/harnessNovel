"""Route design-chat messages and answer questions from current design files."""
from __future__ import annotations

from core.prompt_loader import PromptLoader
from core.text_utils import parse_json_response
from training.adaptive_builder import (
    _call_design_llm,
    _get_llm,
    _read_file,
    _rough_outline_path,
    _stage_outline_path,
    _story_design_path,
    _worldview_path,
)

_NO_MODEL = "No usable model is configured. Set the LLM API in the top-right first."
_RECENT_TURN_CAP = 6
_ROUTE_MODES = ("question", "critique", "change")

_CONCEPT_SECTIONS = (
    ("Worldview", _worldview_path),
    ("Rough outline", _rough_outline_path),
    ("Phase outline", _stage_outline_path),
)


def _long_mainline_path(ws):
    return _story_design_path(ws, "long_mainline.md")


def _stage_roadmap_path(ws):
    return _story_design_path(ws, "stage_roadmap.md")


def _sections_for(scope):
    sections = list(_CONCEPT_SECTIONS)
    if scope == "stage":
        sections.extend((
            ("Long mainline", _long_mainline_path),
            ("Stage roadmap", _stage_roadmap_path),
        ))
    return sections


def design_context(ws, scope):
    """Return labelled text for design files that exist for this scope."""
    parts = []
    for label, path_fn in _sections_for(scope):
        text = _read_file(path_fn(ws))
        if text:
            parts.append("[%s]\n%s" % (label, text))
    return "\n\n".join(parts)


def _require_llm():
    llm = _get_llm()
    if not llm:
        raise RuntimeError(_NO_MODEL)
    return llm


def _parse_point_numbers(value):
    numbers = []
    seen = set()
    if not isinstance(value, list):
        return numbers
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            continue
        if item in seen:
            continue
        seen.add(item)
        numbers.append(item)
    return numbers


def _parse_route_request(raw):
    try:
        payload = parse_json_response(raw)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in _ROUTE_MODES:
        mode = "change"
    points = _parse_point_numbers(payload.get("points")) if mode == "change" else []
    return {"mode": mode, "points": points}


def route_design_request(ws, scope, message):
    """Classify a design-chat message as question, critique, or change."""
    llm = _require_llm()
    prompt = PromptLoader.load(
        "design_chat_route",
        scope=scope,
        message=message,
    )
    raw = _call_design_llm(llm, prompt, "design chat route")
    return _parse_route_request(raw)


def route_design_message(ws, scope, message):
    """Classify a design-chat message as question, critique, or change."""
    return route_design_request(ws, scope, message)["mode"]


def _format_recent_turns(recent_turns):
    turns = list(recent_turns or [])[-_RECENT_TURN_CAP:]
    lines = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "user").strip() or "user"
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append("%s: %s" % (role, content))
    return "\n\n".join(lines) if lines else "(none)"


def _parse_answer(raw):
    try:
        payload = parse_json_response(raw)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("answer_md") or "").strip()


def answer_design_question(ws, scope, message, recent_turns):
    """Answer a design question from current files. Does not write files."""
    llm = _require_llm()
    prompt = PromptLoader.load(
        "design_chat_answer",
        scope=scope,
        message=message,
        design_files=design_context(ws, scope),
        recent_turns=_format_recent_turns(recent_turns),
    )
    raw = _call_design_llm(llm, prompt, "design chat answer")
    answer = _parse_answer(raw)
    if not answer:
        raise RuntimeError("The model did not return an answer.")
    return answer
