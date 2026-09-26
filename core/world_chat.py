import os
import re

from core.prompt_loader import PromptLoader
from core.text_utils import normalize_text
from core.world_knowledge import (
    SECTION_LOOKUP,
    WORLD_SECTION_NAMES,
    WORLD_SECTIONS,
    _aggregate_sections,
    _final_section_path,
    _HEADING_ALIASES,
    _EMPTY_SECTION_BODIES,
    _heading_re,
    _read_file,
    _require_headings,
    _world_root,
    _write_file,
    read_final_section,
    set_chat_edited,
    write_selected_final_sections,
)


PLAN_TURN_LIMIT = 12
SECTION_BUDGET = 40000
_LEVEL2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")


def _guide_path(ws):
    return os.path.join(_world_root(ws), "chat_guide.md")


def load_chat_guide(ws):
    path = _guide_path(ws)
    if not os.path.isfile(path):
        return ""
    return _read_file(path)


def save_chat_guide(ws, text):
    body = (text or "").strip()
    if not body:
        raise ValueError("The chat-guide file is empty.")
    _write_file(_guide_path(ws), body)


def reset_chat_guide(ws):
    path = _guide_path(ws)
    if os.path.isfile(path):
        os.remove(path)


def chat_guide_status(ws):
    path = _guide_path(ws)
    return {
        "exists": os.path.isfile(path),
        "path": "file_system/world_knowledge/chat_guide.md",
    }


def _guide_text(ws):
    text = load_chat_guide(ws).strip()
    return text or "(none)"


def _ensure_running(should_stop):
    if should_stop and should_stop():
        raise RuntimeError("World chat stopped. This round was not written.")


def _emit_progress(progress, phase, completed, total, detail):
    if progress:
        progress(phase, completed, total, detail)


def _generate(llm, folder, prompt_vars):
    prompt = PromptLoader.load(folder, **prompt_vars)
    content = normalize_text(llm.generate(prompt))
    if not content:
        raise RuntimeError(
            f"{folder} did not return valid content. This round was not written; "
            "check the model config or retry."
        )
    return content


def _canonical_section_name(raw):
    key = (raw or "").strip().lower()
    for name in WORLD_SECTION_NAMES:
        if name.lower() == key:
            return name
    for alias, name in _HEADING_ALIASES.items():
        if alias.lower() == key:
            return name
    return None


def _parse_section_changes(block):
    body = (block or "").strip()
    if body.lower() in ("", "none", "none.", "(none)"):
        return []
    matches = list(_LEVEL2_RE.finditer(block or ""))
    changes = []
    seen = set()
    for index, match in enumerate(matches):
        name = _canonical_section_name(match.group(1))
        if not name or name in seen:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        instruction = (block[match.end():end] or "").strip()
        if not instruction:
            continue
        seen.add(name)
        changes.append((name, instruction))
    return changes


def _parse_plan_output(text):
    text = normalize_text(text or "")
    if not text:
        raise RuntimeError(
            "world_chat_plan did not return valid content. This round was not written; "
            "check the model config or retry."
        )
    _require_headings(text, ("Reply", "Section changes"), "world_chat_plan")
    reply_match = _heading_re("Reply").search(text)
    changes_match = _heading_re("Section changes").search(text)
    if reply_match.start() < changes_match.start():
        reply = text[reply_match.end():changes_match.start()].strip()
        changes_body = text[changes_match.end():].strip()
    else:
        reply = text[reply_match.end():].strip()
        changes_body = text[changes_match.end():reply_match.start()].strip()
    return reply, _parse_section_changes(changes_body)


def _format_recent_turns(turns):
    items = list(turns or [])[-PLAN_TURN_LIMIT:]
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip() or "user"
        content = str(item.get("content") or "").strip()
        lines.append(f"{role}:\n{content}")
    return "\n\n".join(lines) if lines else "(none)"


def _compact_sections(ws, skip_name=None, max_chars=SECTION_BUDGET):
    paths = [
        (name, _final_section_path(ws, name))
        for name, _ in WORLD_SECTIONS
        if name != skip_name
    ]
    return _aggregate_sections(paths, max_chars=max_chars)


def _is_empty_body(body):
    return (body or "").strip().lower() in _EMPTY_SECTION_BODIES


def _document_body(content):
    text = (content or "").strip()
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("#") and not lines[0].strip().startswith("##"):
        return "\n".join(lines[1:]).strip()
    return text


def _wrap_section(section_name, body):
    body = (body or "").strip()
    if _is_empty_body(body):
        return f"# {section_name}\n\nNone"
    return f"# {section_name}\n\n{body}"


def _extra_top_level_heading(block):
    for line in (block or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            return stripped
    return None


def _extract_section_body(section_name, text):
    text = normalize_text(text or "")
    if not text:
        raise RuntimeError(
            "world_chat_section did not return valid content. This round was not written; "
            "check the model config or retry."
        )
    lines = text.splitlines()
    first = lines[0].strip()
    if not _heading_re(section_name).match(first):
        raise RuntimeError(
            f"world_chat_section must start with # {section_name}. This round was not written."
        )
    rest = "\n".join(lines[1:])
    extra = _extra_top_level_heading(rest)
    if extra:
        raise RuntimeError(
            f"world_chat_section output included another top-level heading ({extra}). "
            "This round was not written."
        )
    return rest.strip()


def _collect_replacements(ws, llm, message, changes, guide, should_stop, progress):
    replacements = {}
    total = len(changes)
    for index, (section_name, instruction) in enumerate(changes, start=1):
        _ensure_running(should_stop)
        _emit_progress(
            progress, "writing", index - 1, total,
            f"Rewriting {section_name}",
        )
        current = read_final_section(ws, section_name)
        text = _generate(llm, "world_chat_section", dict(
            custom_guide=guide,
            section_name=section_name,
            section_focus=SECTION_LOOKUP[section_name],
            current_section=current,
            other_sections=_compact_sections(ws, skip_name=section_name),
            user_message=message or "",
            change_instruction=instruction,
        ))
        body = _extract_section_body(section_name, text)
        if _is_empty_body(body) and not _is_empty_body(_document_body(current)):
            raise RuntimeError(
                f"World chat refused to replace a meaningful {section_name} section "
                "with an empty body. This round was not written."
            )
        replacements[section_name] = _wrap_section(section_name, body)
    return replacements


def _commit_section_writes(ws, replacements):
    set_chat_edited(ws, True)
    written = write_selected_final_sections(ws, replacements)
    artifacts = []
    for section_name in replacements:
        path = written.get(section_name) or _final_section_path(ws, section_name)
        rel = os.path.relpath(path, ws.root).replace("\\", "/")
        artifacts.append({"path": rel, "label": section_name})
    return artifacts


def apply_world_chat_message(ws, llm, message, turns, should_stop=None, progress=None):
    """Apply one user message to the target-world knowledge base.

    Always runs a plan step, then one section step per named change. Writes
    nothing unless every replacement validates.
    """
    _ensure_running(should_stop)
    _emit_progress(progress, "planning", 0, 1, "Planning world-chat reply")
    guide = _guide_text(ws)
    plan_text = _generate(llm, "world_chat_plan", dict(
        custom_guide=guide,
        current_sections=_compact_sections(ws),
        recent_turns=_format_recent_turns(turns),
        user_message=message or "",
    ))
    reply, changes = _parse_plan_output(plan_text)
    _ensure_running(should_stop)
    if not changes:
        _emit_progress(progress, "completed", 1, 1, "No section writes")
        return {"reply": reply, "changed_sections": [], "artifacts": []}
    replacements = _collect_replacements(
        ws, llm, message, changes, guide, should_stop, progress,
    )
    _ensure_running(should_stop)
    artifacts = _commit_section_writes(ws, replacements)
    _emit_progress(progress, "completed", 1, 1, "World sections written")
    return {
        "reply": reply,
        "changed_sections": list(replacements),
        "artifacts": artifacts,
    }
