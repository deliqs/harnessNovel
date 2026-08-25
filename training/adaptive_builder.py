import sys
import os
import re
import json
import hashlib
import math
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.llm_provider import LLMCallCancelled, LLMProvider
from core.prompt_loader import PromptLoader
from core.config import ConfigLoader
from core.text_utils import normalize_text, parse_json_response
from core.workspace import init_workspace
from core.chapter_utils import (
    chapter_draft_write_path,
    remove_legacy_chapter_draft,
    resolve_chapter_draft_path,
)
from core.adaptation import (
    append_adaptation_report,
    format_forbidden_terms,
    load_rewrite_map,
    scan_forbidden_terms,
)
from core.world_knowledge import (
    build_world_knowledge,
    import_world_sources,
    load_world_knowledge_context,
    world_knowledge_status,
)
from training.reference_finder import (
    list_reference_volumes,
    list_reference_story_arcs,
    load_reference_novel_outline,
    load_reference_volume_outline,
)

BATCH_SIZE = 20
STORY_ARC_FILE_RE = re.compile(r'^arc_(\d+)_ch(\d+)_(\d+)\.md$')
STORY_ARC_TARGET_CHAPTERS = 5
STORY_ARC_TARGET_CHARS_MAX = 2000
STAGE_DESIGN_PIPELINE_VERSION = 2
OPERATION_ADJUST_LAST_PHASE = "adjust last phase"
OPERATION_ADD_PHASE = "add phase"
STAGE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:舞台|stage)\s*0*(\d+)\b[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)
STAGE_OUTLINE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:第\s*)?(?:阶段|phase)\s*0*(\d+)\b[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)
# Stage-roadmap normalize: match stage header lines in any form (# / ## / bold / plain, leading zeros ok),
# rewrite to `# Stage N: name`. Body lines like `舞台规则：` do not match because no digit follows 舞台.
_STAGE_HEADER_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|\*\*\s*)?(?:舞台|stage)\s*0*(\d+)[\s:：.．]*([^\n]*?)\s*\**\s*$",
    re.IGNORECASE,
)
# Document title lines such as `# 舞台路线图` often push stages to h2; drop them while normalizing.
_STAGE_TITLE_LINE_RE = re.compile(
    r"^\s*#{1,6}\s+(?:舞台路线图|全书舞台路线图|舞台设计|舞台规划|舞台大纲|舞台总览|stage\s*roadmap)\s*$",
    re.IGNORECASE,
)
_STAGE_CODE_FENCE_RE = re.compile(r"^\s*```.*$", re.IGNORECASE)


def _normalize_stage_roadmap(text):
    """Normalize stage headers to `# Stage N: name`. Also parses `# 舞台N：名称`."""
    if not text:
        return ""
    output_lines = []
    for line in text.splitlines():
        if _STAGE_CODE_FENCE_RE.match(line):
            continue
        header = _STAGE_HEADER_LINE_RE.match(line)
        if header:
            number = int(header.group(1))
            name = header.group(2).strip().strip("*#：:.- ").strip()
            output_lines.append(f"# Stage {number}: {name}" if name else f"# Stage {number}:")
            continue
        if _STAGE_TITLE_LINE_RE.match(line):
            continue
        output_lines.append(line)
    return "\n".join(output_lines).strip()


def _stage_append_starts_at(text, stage_number):
    """True when appended roadmap text starts at stage N (English emit or Chinese alias)."""
    return bool(re.search(
        rf"^#{{1,6}}\s*(?:舞台|stage)\s*{int(stage_number)}\s*[：:]",
        text or "",
        re.IGNORECASE | re.MULTILINE,
    ))


def _is_empty_design_asset(text):
    value = text or ""
    return value.startswith("(not generated") or value.startswith("（未生成")


def _get_llm():
    config = ConfigLoader.get_adaptive_builder_config()
    if not config.get("api_key"):
        config["api_key"] = os.getenv("OPENAI_API_KEY")
    if not config.get("api_key"):
        print("Error: API Key not detected.")
        return None
    return LLMProvider(**config)


def _get_lite_llm():
    """Get the writing-production LLM (flash): story arcs, chapter outlines, drafts, and light helper tasks."""
    config = ConfigLoader.get_adaptive_builder_lite_config()
    if not config:
        config = ConfigLoader.get_adaptive_builder_config()
    if not config.get("api_key"):
        config["api_key"] = os.getenv("OPENAI_API_KEY")
    if not config.get("api_key"):
        print("Error: API Key not detected.")
        return None
    return LLMProvider(**config)


def _read_file(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return content if content else None


def _load_outline_rules(ws):
    """Load outline / volume-outline design rules."""
    rules = _read_file(os.path.join(ws.file_system, "OUTLINE_RULES.md"))
    return rules or "(no outline design rules)"


def _load_world_knowledge_optional(ws, purpose, max_chars=80000, require_ready=False):
    """Load the target-world knowledge base; if missing, fall back to reference-novel + creative-direction only."""
    status = world_knowledge_status(ws)
    if (
        require_ready
        and status["enabled"]
        and status["source_count"] > 0
        and not status["ready"]
    ):
        raise RuntimeError(
            "Target-world sources are uploaded and enabled, but the knowledge base is not fully built. "
            "Return to the Target world step and finish all 7 sections before generating book design."
        )
    world_knowledge = load_world_knowledge_context(ws, max_chars=max_chars)
    if world_knowledge:
        print(f"  -> Loaded target-world knowledge base for {purpose}.")
        return world_knowledge
    print(f"  -> No target-world knowledge base detected; skipping {purpose}.")
    print("     To add knowledge-base enhancement, run novel world-import / novel world-build first.")
    return ""


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")


def run_step(*, llm, folder, prompt_vars, output_path, label=None,
             header=None, save=None, write_guard=False, cancel_event=None):
    """Core generate triple: load -> generate -> normalize -> write, with optional header/save prints.

    label is also the default basis for header/save (header=">>> Generating {label} <<<",
    save="  -> {label} saved: {output_path}"); explicit header/save override that.
    label=None with no header/save is silent. write_guard=True writes and prints save only when result is non-empty.
    """
    if label is not None and header is None:
        header = f">>> Generating {label} <<<"
    if label is not None and save is None:
        save = f"  -> {label} saved: {output_path}"
    if header is not None:
        print(header)
    prompt = PromptLoader.load(folder, **prompt_vars)
    result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event))
    if result or not write_guard:
        _write_file(output_path, result)
    if save is not None and (result or not write_guard):
        print(save)
    return result


def _load_creative_direction(ws, cli_input=None, direction_file=None):
    """Load creative direction: CLI first, then a given file, then the workspace creative_direction.md."""
    if cli_input:
        return cli_input
    if direction_file:
        content = _read_file(direction_file)
        if content:
            return content
    content = _read_file(ws.creative_direction)
    if content:
        lines = []
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('<!--') and stripped.endswith('-->'):
                continue
            lines.append(line)
        cleaned = '\n'.join(lines).strip()
        body = cleaned
        for heading in [
            '# Creative direction', '## Genre and positioning', '## Protagonist concept',
            '## Worldview direction', '## Core conflict', '## Reference traits to keep',
            '## Parts to change', '## Other notes',
            '# 创作方向', '## 题材与定位', '## 主角构想', '## 世界观方向',
            '## 核心冲突', '## 希望保留的参考特质', '## 希望改变的部分', '## 其他补充',
        ]:
            body = body.replace(heading, '')
        if body.strip():
            return cleaned
    return ""


def _gen_rewrite_map(ws, llm, force=False):
    """Build the book rewrite map from reference and the new-book plan, as a later-stage hard constraint."""
    adaptation_dir = os.path.join(ws.file_system, "adaptation")
    output_path = os.path.join(adaptation_dir, "rewrite_map.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Rewrite map already exists: {output_path}")
        return existing

    reference_outline = load_reference_novel_outline(ws.reference_outlines)
    novel_outline = _read_file(os.path.join(ws.file_system, "novel_outline.md")) or ""
    new_worldview = _read_file(os.path.join(ws.file_system, "new_novel_worldview.md")) or ""

    if not reference_outline or not novel_outline:
        print("  Warning: reference outline or new-novel outline is missing; skipping rewrite-map generation.")
        return ""

    return run_step(
        llm=llm,
        folder="rewrite_map_extract",
        label="book rewrite map",
        save=f"  -> Rewrite map saved: {output_path}",
        write_guard=True,
        output_path=output_path,
        prompt_vars=dict(
            reference_outline=reference_outline,
            novel_outline=novel_outline,
            new_novel_worldview=new_worldview or "(not generated: new-novel worldview)",
        ),
    )


def _ensure_rewrite_map(ws, llm):
    """Ensure older workspaces can still fill in a rewrite map in later stages."""
    output_path = os.path.join(ws.file_system, "adaptation", "rewrite_map.md")
    if _read_file(output_path):
        return
    _gen_rewrite_map(ws, llm, force=False)


def _story_design_dir(ws):
    return os.path.join(ws.file_system, "story_design")


def _story_design_path(ws, name):
    return os.path.join(_story_design_dir(ws), name)


def _volume_stage_plan_path(ws, vol_idx):
    return os.path.join(_story_design_dir(ws), "stages", f"vol_{vol_idx:02d}_stage.md")


def _rough_outline_path(ws):
    return _story_design_path(ws, "rough_outline.md")


def _worldview_path(ws):
    return _story_design_path(ws, "worldview.md")


def _stage_outline_path(ws):
    return _story_design_path(ws, "stage_outline.md")


def _rough_outline_with_stages(ws):
    """Read for stages and later steps; on disk the rough outline and phase outline stay separate."""
    rough = (
        _read_file(_rough_outline_path(ws))
        or _read_file(_story_design_path(ws, "core_gameplay.md"))
        or "(not generated: rough outline)"
    )
    stages = _read_file(_stage_outline_path(ws))
    return f"{rough}\n\n---\n\n{stages}" if stages else rough


def _design_versions_dir(ws, scope):
    return os.path.join(_story_design_dir(ws), "versions", scope)


def _backup_design_files(ws, scope, files):
    """Back up current files under versions/<scope>/ so multi-round refine can roll back."""
    import time
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = _design_versions_dir(ws, scope)
    os.makedirs(out_dir, exist_ok=True)
    for rel, path in files.items():
        content = _read_file(path)
        if not content:
            continue
        _write_file(os.path.join(out_dir, f"{rel}_{stamp}.md"), content)



def _load_story_design_assets(ws):
    # Current flow: core gameplay and character arcs live in rough_outline.md; keep the downstream 4 keys.
    rough = _rough_outline_with_stages(ws)
    return {
        "core_gameplay": rough or "(not generated: rough outline / core gameplay)",
        "long_mainline": _read_file(_story_design_path(ws, "long_mainline.md")) or "(not generated: long mainline)",
        "stage_roadmap": _read_file(_story_design_path(ws, "stage_roadmap.md")) or "(not generated: stage roadmap)",
        "character_arcs": rough or _read_file(_story_design_path(ws, "character_arcs.md")) or "(not generated: character arcs)",
    }


def _story_design_state_path(ws):
    return _story_design_path(ws, "design_state.json")


def _load_story_design_state(ws):
    content = _read_file(_story_design_state_path(ws))
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _mark_concept_revision(ws):
    """Record that the rough outline / worldview had a real update."""
    state = _load_story_design_state(ws)
    state["concept_revision"] = int(state.get("concept_revision") or 0) + 1
    state.setdefault("stage_synced_concept_revision", 0)
    state["concept_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json_file(_story_design_state_path(ws), state)
    return state["concept_revision"]


def _mark_stage_design_synced(ws):
    """Record that stage design has absorbed the current book design."""
    state = _load_story_design_state(ws)
    revision = int(state.get("concept_revision") or 0)
    state["stage_synced_concept_revision"] = revision
    state["stage_synced_at"] = datetime.now().isoformat(timespec="seconds")
    state["pending_reference_stage_sync"] = False
    state.pop("reference_stage_increment", None)
    _write_json_file(_story_design_state_path(ws), state)
    return revision


def _arc_usage_state_path(ws):
    return _story_design_path(ws, "arc_usage_state.json")


def _chapter_usage_state_path(ws):
    return _story_design_path(ws, "chapter_usage_state.json")


def _load_chapter_usage_state(ws):
    content = _read_file(_chapter_usage_state_path(ws))
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _reference_chapter_cards(ws):
    cards_dir = os.path.join(ws.reference, "chapter_cards")
    cards = []
    if not os.path.isdir(cards_dir):
        return cards
    for filename in sorted(os.listdir(cards_dir)):
        if not re.match(r"chapter_\d+\.json$", filename):
            continue
        try:
            card = json.loads(_read_file(os.path.join(cards_dir, filename)) or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(card, dict) and int(card.get("chapter") or 0) > 0:
            cards.append(card)
    return cards


def _mark_reference_chapters_used(ws, chapter_numbers):
    state = _load_chapter_usage_state(ws)
    now = datetime.now().isoformat(timespec="seconds")
    for number in chapter_numbers:
        state[str(int(number))] = {"used": True, "used_at": now}
    _write_json_file(_chapter_usage_state_path(ws), state)


def _unused_reference_chapter_context(ws, max_chars=30000):
    state = _load_chapter_usage_state(ws)
    legacy_baseline = 0
    if not state:
        legacy_baseline = int(_load_story_design_state(ws).get("reference_processed_chapters") or 0)
    selected = []
    length = 0
    for card in _reference_chapter_cards(ws):
        number = int(card["chapter"])
        record = state.get(str(number), {})
        used = bool(record.get("used")) if isinstance(record, dict) else bool(record)
        used = used or (not state and number <= legacy_baseline)
        if used:
            continue
        text = json.dumps({
            "chapter": number,
            "title": card.get("title"),
            "chapter_outline": card.get("chapter_outline_600"),
            "chapter_rhythm": card.get("chapter_rhythm"),
            "story_line": card.get("story_line"),
            "highlights": card.get("highlights"),
        }, ensure_ascii=False)
        if selected and length + len(text) > max_chars:
            break
        selected.append((number, text))
        length += len(text)
    return selected


def _load_arc_usage_state(ws):
    """Load segment-level used flags: {arc_rel_path: true}. Return {} if missing."""
    content = _read_file(_arc_usage_state_path(ws))
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_arc_usage_state(ws, state):
    _write_json_file(_arc_usage_state_path(ws), state)


def _all_reference_arc_keys(ws):
    """List relative paths of all story segments in the current reference deconstruction (vs reference_outlines)."""
    keys = []
    base = ws.reference_outlines
    for volume in list_reference_volumes(base):
        for arc in list_reference_story_arcs(base, volume["vol_idx"]):
            try:
                rel = os.path.relpath(arc["path"], base)
            except ValueError:
                rel = arc["path"]
            keys.append(rel)
    return keys


def _init_arc_usage_state(ws):
    """Mark every current reference segment as used=True.

    Called after the first generation (route 1); later extend only consumes used=False new segments.
    """
    keys = _all_reference_arc_keys(ws)
    state = _load_arc_usage_state(ws)
    for key in keys:
        state[key] = True
    _save_arc_usage_state(ws, state)


def _unused_reference_arcs(ws, max_chars=26000):
    """Collect used=False reference-segment content as extend input."""
    state = _load_arc_usage_state(ws)
    keys = _all_reference_arc_keys(ws)
    base = ws.reference_outlines
    unused = []
    length = 0
    for volume in list_reference_volumes(base):
        for arc in list_reference_story_arcs(base, volume["vol_idx"]):
            try:
                rel = os.path.relpath(arc["path"], base)
            except ValueError:
                rel = arc["path"]
            if state.get(rel, False):
                continue
            content = arc.get("content") or _read_file(arc["path"])
            if not content:
                continue
            if length + len(content) > max_chars:
                break
            unused.append({
                "path": rel,
                "start_ch": arc["start_ch"],
                "end_ch": arc["end_ch"],
                "content": content,
            })
            length += len(content)
    return unused


def _mark_arcs_used(ws, rel_paths, stage_numbers=None):
    """Record that a segment was actually mapped by stage design, and keep the stage number."""
    if not rel_paths:
        return
    state = _load_arc_usage_state(ws)
    now = datetime.now().isoformat(timespec="seconds")
    stages = sorted({int(item) for item in (stage_numbers or []) if str(item).isdigit()})
    for path in rel_paths:
        previous = state.get(path, {})
        previous_stages = previous.get("stage_numbers", []) if isinstance(previous, dict) else []
        state[path] = {
            "used": True,
            "used_at": now,
            "stage_numbers": sorted(set(previous_stages + stages)),
        }
    _save_arc_usage_state(ws, state)


def _direction_history_path(ws):
    return _story_design_path(ws, "direction_history.json")


_DIRECTION_MODE_LABELS = {
    "initial": "initial design",
    "rebuild": "redesign",
    "extend": "extend stages",
    "stage_insert": "insert stage",
}


def record_creative_direction(ws, direction, mode):
    """Record one creative-direction input into history for the workbench to reuse."""
    text = (direction or "").strip()
    if not text:
        return
    os.makedirs(_story_design_dir(ws), exist_ok=True)
    history_path = _direction_history_path(ws)
    history = []
    raw = _read_file(history_path)
    if raw:
        try:
            history = json.loads(raw)
        except json.JSONDecodeError:
            history = []
    if not isinstance(history, list):
        history = []
    preview = re.sub(r"\s+", " ", text)[:80]
    history.append({
        "id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": _DIRECTION_MODE_LABELS.get(mode, mode),
        "preview": preview,
        "text": text,
    })
    _write_file(history_path, json.dumps(history[-20:], ensure_ascii=False, indent=2))


def _reference_design_progress(ws):
    """Read current reference-deconstruction progress; if the checkpoint is missing, fall back to split chapter count."""
    state_path = os.path.join(ws.reference, "import_state.json")
    state = _read_file(state_path)
    try:
        state = json.loads(state) if state else {}
    except json.JSONDecodeError:
        state = {}

    processed = int(state.get("processed_chapters") or 0) if isinstance(state, dict) else 0
    total = int(state.get("total_chapters") or 0) if isinstance(state, dict) else 0
    if processed:
        return processed, total

    chapters_dir = os.path.join(ws.reference, "chapters")
    if not os.path.isdir(chapters_dir):
        return 0, total
    chapter_count = sum(
        1
        for _, _, files in os.walk(chapters_dir)
        for filename in files
        if filename.endswith((".md", ".txt")) and not filename.startswith("_")
    )
    return chapter_count, total or chapter_count


def _record_story_design_reference_snapshot(ws, reset_extensions=False):
    """Save the first-edition design's reference progress, used to detect later new reference content."""
    existing = _load_story_design_state(ws)
    if existing and not reset_extensions:
        return existing

    processed, total = _reference_design_progress(ws)
    state = {
        "reference_processed_chapters": processed,
        "reference_total_chapters": total,
        "extension_count": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json_file(_story_design_state_path(ws), state)
    return state


def _reference_story_arc_delta(ws, baseline_chapter, current_chapter, max_chars=26000):
    """Collect only reference story segments added after the design snapshot, so old reference is not stuffed back into context."""
    if current_chapter <= baseline_chapter:
        return ""

    items = []
    chapter_offset = 0
    for volume in list_reference_volumes(ws.reference_outlines):
        meta_path = os.path.join(volume["dir_path"], "meta.json")
        meta = {}
        try:
            meta = json.loads(_read_file(meta_path) or "{}")
        except json.JSONDecodeError:
            pass
        is_virtual_volume = isinstance(meta, dict) and int(meta.get("start_ch") or 0) > 0

        for arc in list_reference_story_arcs(ws.reference_outlines, volume["vol_idx"]):
            if is_virtual_volume:
                start_ch = int(arc["start_ch"])
                end_ch = int(arc["end_ch"])
            else:
                start_ch = chapter_offset + int(arc["start_ch"])
                end_ch = chapter_offset + int(arc["end_ch"])
            if end_ch <= baseline_chapter or start_ch > current_chapter:
                continue
            items.append((start_ch, end_ch, arc["content"]))

        chapter_offset += int(volume.get("chapter_count") or 0)

    parts = []
    length = 0
    for start_ch, end_ch, content in sorted(items, key=lambda item: (item[0], item[1])):
        part = f"[Newly added reference story segment: chapters {start_ch}-{end_ch}]\n{content.strip()}"
        if parts and length + len(part) > max_chars:
            break
        parts.append(part)
        length += len(part)
    return "\n\n---\n\n".join(parts)


def _next_stage_number(stage_roadmap):
    numbers = [int(value) for value in STAGE_HEADING_RE.findall(_normalize_stage_roadmap(stage_roadmap or ""))]
    return max(numbers, default=0) + 1


def _parse_story_design_extension(raw):
    markers = ["LONG_MAINLINE_APPEND", "CHARACTER_ARCS_APPEND", "STAGE_ROADMAP_APPEND"]
    marker_re = re.compile(r"^<<<(" + "|".join(markers) + r")>>>\s*$", re.MULTILINE)
    matches = list(marker_re.finditer(raw or ""))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        sections[match.group(1)] = raw[match.end():end].strip()
    missing = [marker for marker in markers if not sections.get(marker)]
    if missing:
        raise ValueError("Design-extend result is missing section markers: " + ", ".join(missing))
    return sections


def _append_story_design_section(path, title, content):
    existing = _read_file(path) or ""
    suffix = f"# {title}\n\n{content.strip()}"
    _write_file(path, f"{existing}\n\n---\n\n{suffix}" if existing else suffix)


def extend_story_design(ws, use_reference=False, creative_direction=None, direction_file=None):
    """Append long mainline, character arcs, and later stages without rewriting existing design."""
    assets = _load_story_design_assets(ws)
    required_assets = ("core_gameplay", "long_mainline", "stage_roadmap", "character_arcs")
    missing = [name for name in required_assets if _is_empty_design_asset(assets[name])]
    if missing:
        print("Error: finish book design before extending it.")
        return

    state = _load_story_design_state(ws)
    baseline = int(state.get("reference_processed_chapters") or 0)
    current_progress, total_progress = _reference_design_progress(ws)
    if use_reference and current_progress <= baseline:
        print("Error: no newly added reference deconstruction detected. Omit --use-reference to extend from existing stages.")
        return

    reference_delta = ""
    if use_reference:
        reference_delta = _reference_story_arc_delta(ws, baseline, current_progress)
        if not reference_delta:
            print("Warning: no newly added reference story segments found; extending from the new chapter range and existing design only.")
            reference_delta = "(new reference chapters were parsed, but no readable new story segments were found.)"

    direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, direction, "extend")
    llm = _get_llm()
    if not llm:
        return
    world_knowledge = _load_world_knowledge_optional(ws, "book-design extend")
    next_stage = _next_stage_number(assets["stage_roadmap"])

    source_label = (
        f"reference novel newly added chapters {baseline + 1}-{current_progress}"
        if use_reference else "the current new book's existing gameplay, long mainline, character arcs, and stages"
    )
    print(f">>> Extending book design based on {source_label} <<<")
    raw = normalize_text(llm.generate(PromptLoader.load(
        "story_design_extend",
        creative_direction=direction or "(no extra direction)",
        use_reference="yes" if use_reference else "no",
        reference_range=f"chapters {baseline + 1}-{current_progress}" if use_reference else "(not reading newly added reference chapters this time)",
        reference_delta=reference_delta or "(not using newly added deconstruction this time)",
        core_gameplay=assets["core_gameplay"],
        existing_long_mainline=assets["long_mainline"],
        existing_character_arcs=assets["character_arcs"],
        existing_stage_roadmap=assets["stage_roadmap"],
        next_stage_number=next_stage,
        world_knowledge=world_knowledge or "(target world knowledge base not provided)",
    )))

    try:
        sections = _parse_story_design_extension(raw)
    except ValueError as exc:
        print(f"Error: {exc}; no design files were written.")
        return
    sections["STAGE_ROADMAP_APPEND"] = _normalize_stage_roadmap(sections["STAGE_ROADMAP_APPEND"])
    if not _stage_append_starts_at(sections["STAGE_ROADMAP_APPEND"], next_stage):
        print(f"Error: appended stages must start at \"# Stage {next_stage}:\"; no design files were written.")
        return

    _append_story_design_section(
        _story_design_path(ws, "long_mainline.md"),
        f"Long-mainline extend (round {int(state.get('extension_count') or 0) + 1})",
        sections["LONG_MAINLINE_APPEND"],
    )
    _append_story_design_section(
        _story_design_path(ws, "character_arcs.md"),
        f"Character-arc extend (round {int(state.get('extension_count') or 0) + 1})",
        sections["CHARACTER_ARCS_APPEND"],
    )
    stage_path = _story_design_path(ws, "stage_roadmap.md")
    combined_stage_roadmap = _normalize_stage_roadmap(
        f"{assets['stage_roadmap']}\n\n---\n\n{sections['STAGE_ROADMAP_APPEND'].strip()}"
    )
    _write_file(stage_path, combined_stage_roadmap)

    extension_dir = os.path.join(ws.file_system, "adaptation", "story_design_extensions")
    extension_path = os.path.join(extension_dir, f"extension_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    _write_file(extension_path, (
        f"# Book-design extend record\n\n"
        f"Source: {source_label}\n\n"
        f"Use newly added reference: {'yes' if use_reference else 'no'}\n\n"
        f"{raw}"
    ))

    state.update({
        "reference_processed_chapters": current_progress if use_reference else baseline,
        "reference_total_chapters": total_progress,
        "extension_count": int(state.get("extension_count") or 0) + 1,
        "last_extension_used_reference": use_reference,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    _write_json_file(_story_design_state_path(ws), state)
    print(f"  -> Long mainline appended: {_story_design_path(ws, 'long_mainline.md')}")
    print(f"  -> Character arcs appended: {_story_design_path(ws, 'character_arcs.md')}")
    print(f"  -> New stages appended: {stage_path}")
    print(f"  -> Extension record saved: {extension_path}")


def _mechanics_dir(ws):
    return os.path.join(ws.file_system, "mechanics")


def _mechanics_path(ws, name):
    return os.path.join(_mechanics_dir(ws), name)


def _write_json_file(path, data):
    _write_file(path, json.dumps(data, ensure_ascii=False, indent=2))


def _read_json_file(path):
    content = _read_file(path)
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _finalized_chapters_path(ws):
    return os.path.join(ws.file_system, "finalized_chapters.json")


def _draft_chapter_dir(ws, volume):
    return os.path.join(ws.file_system, "chapters", f"vol_{volume:02d}")


def _draft_chapter_path(ws, volume, chapter):
    return resolve_chapter_draft_path(_draft_chapter_dir(ws, volume), chapter)


def _write_draft_chapter(out_dir, chapter_num, content):
    path = chapter_draft_write_path(out_dir, chapter_num)
    _write_file(path, content)
    remove_legacy_chapter_draft(out_dir, chapter_num)
    return path


def _content_hash(content):
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _load_finalization_payload(ws):
    payload = _read_json_file(_finalized_chapters_path(ws))
    if not isinstance(payload, dict):
        payload = {}
    drafts = payload.get("drafts") if isinstance(payload.get("drafts"), dict) else {}
    normalized = {"version": 2, "drafts": {}}
    for volume_key, entries in drafts.items():
        records = {}
        if isinstance(entries, list):  # Compatible with the old format that stored only chapter numbers.
            entries = {str(chapter): {"finalized": True} for chapter in entries}
        if not isinstance(entries, dict):
            continue
        for chapter_key, record in entries.items():
            try:
                chapter = int(chapter_key)
            except (TypeError, ValueError):
                continue
            if chapter < 1:
                continue
            record = record if isinstance(record, dict) else {}
            if record.get("finalized", True):
                records[str(chapter)] = {
                    "finalized": True,
                    "synced_hash": str(record.get("synced_hash") or ""),
                    "synced_at": str(record.get("synced_at") or ""),
                }
        if records:
            normalized["drafts"][str(volume_key)] = records
    return normalized


def chapter_finalization_status(ws):
    payload = _load_finalization_payload(ws)
    result = {"version": 2, "drafts": {}}
    for volume_key, records in payload["drafts"].items():
        try:
            volume = int(str(volume_key).replace("vol_", ""))
        except ValueError:
            continue
        rendered = {}
        for chapter_key, record in records.items():
            chapter = int(chapter_key)
            content = _read_file(_draft_chapter_path(ws, volume, chapter)) or ""
            current_hash = _content_hash(content) if content else ""
            synced_hash = record.get("synced_hash") or ""
            rendered[str(chapter)] = {
                "finalized": True,
                "status": "synced" if current_hash and synced_hash == current_hash else "pending",
                "current_hash": current_hash,
                "synced_hash": synced_hash,
                "synced_at": record.get("synced_at") or "",
            }
        if rendered:
            result["drafts"][f"vol_{volume:02d}"] = rendered
    return result


def _finalized_chapter_numbers(ws, kind, volume):
    if kind not in {"outlines", "drafts"}:
        return set()
    records = chapter_finalization_status(ws)["drafts"].get(f"vol_{volume:02d}", {})
    if kind == "drafts":
        return {int(chapter) for chapter, record in records.items() if record.get("finalized")}
    return {
        int(chapter) for chapter, record in records.items()
        if record.get("status") == "synced"
    }


def _finalized_chapter_boundary(ws, kind, volume, start_chapter, end_chapter):
    finalized = [
        chapter for chapter in _finalized_chapter_numbers(ws, kind, volume)
        if start_chapter <= chapter <= end_chapter
    ]
    return max(finalized) if finalized else start_chapter - 1


def set_chapter_finalized(ws, kind, volume, chapter, finalized):
    if kind != "drafts":
        raise ValueError("Only drafts can be marked final; chapter outlines lock automatically after draft sync.")
    volume, chapter = int(volume), int(chapter)
    if volume < 1 or chapter < 1:
        raise ValueError("Volume and chapter numbers must be positive integers.")
    payload = _load_finalization_payload(ws)
    key = f"vol_{volume:02d}"
    records = payload["drafts"].setdefault(key, {})
    if finalized:
        if not _read_file(_draft_chapter_path(ws, volume, chapter)):
            raise ValueError("Draft does not exist or is empty; cannot mark it as final.")
        previous = records.get(str(chapter), {})
        records[str(chapter)] = {
            "finalized": True,
            "synced_hash": str(previous.get("synced_hash") or ""),
            "synced_at": str(previous.get("synced_at") or ""),
        }
    else:
        records.pop(str(chapter), None)
    if not records:
        payload["drafts"].pop(key, None)
    _write_json_file(_finalized_chapters_path(ws), payload)
    return chapter_finalization_status(ws)


def clear_finalized_chapters(ws, kind, volume, chapters):
    if kind != "drafts":
        return chapter_finalization_status(ws)
    payload = _load_finalization_payload(ws)
    key = f"vol_{int(volume):02d}"
    records = payload["drafts"].get(key, {})
    for chapter in chapters:
        records.pop(str(int(chapter)), None)
    if not records:
        payload["drafts"].pop(key, None)
    _write_json_file(_finalized_chapters_path(ws), payload)
    return chapter_finalization_status(ws)


def _mark_finalized_draft_synced(ws, volume, chapter, expected_hash):
    content = _read_file(_draft_chapter_path(ws, volume, chapter)) or ""
    if not content or _content_hash(content) != expected_hash:
        raise RuntimeError(f"Chapter {chapter} final draft changed during sync; please sync again.")
    payload = _load_finalization_payload(ws)
    key = f"vol_{volume:02d}"
    record = payload["drafts"].get(key, {}).get(str(chapter))
    if not record:
        raise RuntimeError(f"Chapter {chapter} is no longer marked final; stopping sync.")
    record["synced_hash"] = expected_hash
    record["synced_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json_file(_finalized_chapters_path(ws), payload)


def _default_mechanics_disabled(reason):
    return {
        "profile": {
            "mode": "none",
            "enabled": False,
            "visible_panel": False,
            "precision": "none",
            "type": "none",
            "reason": reason,
            "tracked_domains": [],
        },
        "design": reason,
        "rules": {
            "version": 1,
            "mode": "none",
            "event_types": [],
            "display": {
                "panel_enabled": False,
                "panel_name": "",
                "chapter_panel_sections": [],
            },
            "constraints": ["This novel does not enable the mechanics layer; chapter outlines and drafts must not force a system panel."],
        },
        "state": {
            "version": 1,
            "mode": "none",
            "chapter": 0,
            "values": {},
            "inventory": {},
            "skills": {},
            "tasks": {},
            "relationships": {},
            "flags": {},
        },
    }


def _normalize_mechanics_payload(payload):
    if not isinstance(payload, dict):
        payload = _default_mechanics_disabled("LLM did not return valid mechanics JSON; defaulting to off.")

    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    mode = profile.get("mode") or payload.get("mode") or "none"
    if mode not in {"none", "light_state", "explicit_mechanics"}:
        mode = "none"

    enabled = mode != "none"
    visible_panel = bool(profile.get("visible_panel")) if enabled else False
    precision = profile.get("precision") or ("strict" if mode == "explicit_mechanics" else ("loose" if mode == "light_state" else "none"))
    mechanics_type = profile.get("type") or ("state_tracking" if mode == "light_state" else ("system_panel" if mode == "explicit_mechanics" else "none"))
    tracked_domains = profile.get("tracked_domains")
    if not isinstance(tracked_domains, list):
        tracked_domains = []

    normalized = {
        "profile": {
            "mode": mode,
            "enabled": enabled,
            "visible_panel": visible_panel,
            "precision": precision,
            "type": mechanics_type,
            "reason": profile.get("reason") or payload.get("reason") or "",
            "tracked_domains": tracked_domains,
        },
        "design": payload.get("design") if isinstance(payload.get("design"), str) else "",
        "rules": payload.get("rules") if isinstance(payload.get("rules"), dict) else {},
        "state": payload.get("state") if isinstance(payload.get("state"), dict) else {},
    }
    normalized["rules"].setdefault("version", 1)
    normalized["rules"].setdefault("mode", mode)
    normalized["rules"].setdefault("event_types", [])
    normalized["rules"].setdefault("display", {})
    normalized["rules"]["display"].setdefault("panel_enabled", visible_panel)
    normalized["rules"]["display"].setdefault("panel_name", "")
    normalized["rules"]["display"].setdefault("chapter_panel_sections", [])
    normalized["rules"].setdefault("constraints", [])
    normalized["state"].setdefault("version", 1)
    normalized["state"].setdefault("mode", mode)
    normalized["state"].setdefault("chapter", 0)
    for key in ["values", "inventory", "skills", "tasks", "relationships", "flags"]:
        normalized["state"].setdefault(key, {})
    return normalized


def _write_mechanics_payload(ws, payload):
    os.makedirs(_mechanics_dir(ws), exist_ok=True)
    profile = payload["profile"]
    state = payload["state"]
    panel = {
        "version": 1,
        "selection_mode": "auto",
        "decided": True,
        "enabled": bool(profile.get("enabled")),
        "visible_panel": bool(profile.get("visible_panel")),
        "mode": profile.get("mode") or "none",
        "type": profile.get("type") or "none",
        "reason": profile.get("reason") or payload.get("design") or "",
        "rules": payload["rules"],
        "initial_panel": _legacy_state_to_panel(state),
    }
    _write_json_file(_mechanics_path(ws, "system_panel.json"), panel)


def _load_mechanics_context(ws):
    panel = _read_json_file(_mechanics_path(ws, "system_panel.json"))
    if panel:
        return "[System panel definition]\n" + json.dumps(panel, ensure_ascii=False, indent=2)
    profile = _read_json_file(_mechanics_path(ws, "profile.json"))
    if not profile or not profile.get("enabled"):
        return "(mechanics layer is off. Chapter outlines and drafts do not need a system panel.)"

    design = _read_file(_mechanics_path(ws, "design.md")) or ""
    rules = _read_file(_mechanics_path(ws, "rules.json")) or "{}"
    state = _read_file(_mechanics_path(ws, "state.json")) or "{}"
    return (
        "[Mechanics-layer profile]\n"
        + json.dumps(profile, ensure_ascii=False, indent=2)
        + "\n\n[Mechanics-layer design]\n"
        + design
        + "\n\n[Mechanics-layer rules]\n"
        + rules
        + "\n\n[Current mechanics state]\n"
        + state
    )


def _system_panel_chapter_dir(ws, volume):
    return os.path.join(ws.file_system, "system_panels", f"vol_{volume:02d}")


def _system_panel_chapter_path(ws, volume, chapter_num):
    return os.path.join(_system_panel_chapter_dir(ws, volume), f"chapter_{chapter_num:03d}.json")


def system_panel_status(ws):
    panel = _read_json_file(_mechanics_path(ws, "system_panel.json")) or {}
    selection_mode = panel.get("selection_mode") or ("auto" if not panel else ("enabled" if panel.get("enabled") else "disabled"))
    return {
        "selection_mode": selection_mode,
        "decided": bool(panel.get("decided", selection_mode != "auto")),
        "enabled": bool(panel.get("enabled")),
        "reason": str(panel.get("reason") or ""),
    }


def configure_system_panel(ws, selection_mode):
    if selection_mode not in {"auto", "enabled", "disabled"}:
        raise ValueError("Invalid system-panel mode.")
    os.makedirs(_mechanics_dir(ws), exist_ok=True)
    if selection_mode == "auto":
        panel = {
            "version": 1, "selection_mode": "auto", "decided": False,
            "enabled": False, "visible_panel": False, "mode": "pending",
            "reason": "Will be auto-decided from book design and story arcs on first chapter-outline generation.",
            "rules": {}, "initial_panel": {},
        }
    else:
        enabled = selection_mode == "enabled"
        panel = {
            "version": 1, "selection_mode": selection_mode, "decided": True,
            "enabled": enabled, "visible_panel": enabled,
            "mode": "explicit_mechanics" if enabled else "none",
            "type": "system_panel" if enabled else "none",
            "reason": "User enabled the system panel." if enabled else "User explicitly disabled the system panel.",
            "rules": {
                "constraints": ["Record only protagonist state changes that actually happen in the chapter outline; do not grant rewards or extra plot early."]
            } if enabled else {},
            "initial_panel": {},
        }
    _write_json_file(_mechanics_path(ws, "system_panel.json"), panel)
    return system_panel_status(ws)


def _ensure_system_panel_decision(ws, cancel_event=None):
    status = system_panel_status(ws)
    if status["selection_mode"] != "auto" or status["decided"]:
        return status
    assets = _load_story_design_assets(ws)
    llm = _get_lite_llm()
    if not llm:
        raise RuntimeError("No usable model is configured; cannot auto-decide whether a system panel is needed.")
    prompt = PromptLoader.load(
        "mechanics_init",
        mechanics_source="(user chose auto-decide)",
        creative_direction="(no extra direction)",
        core_gameplay=assets["core_gameplay"],
        long_mainline=assets["long_mainline"],
        stage_roadmap=assets["stage_roadmap"],
        character_arcs=assets["character_arcs"],
    )
    raw = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2))
    payload = _normalize_mechanics_payload(parse_json_response(raw))
    _write_mechanics_payload(ws, payload)
    panel_path = _mechanics_path(ws, "system_panel.json")
    panel = _read_json_file(panel_path) or {}
    panel.update(selection_mode="auto", decided=True)
    _write_json_file(panel_path, panel)
    return system_panel_status(ws)


def _previous_system_panel(ws, volume, chapter_num):
    if not system_panel_status(ws)["enabled"]:
        return {"enabled": False, "chapter": max(0, chapter_num - 1), "panel": {}}
    previous = _read_json_file(_system_panel_chapter_path(ws, volume, chapter_num - 1))
    if previous:
        return {
            "chapter": previous.get("chapter", max(0, chapter_num - 1)),
            "panel": (
                previous.get("panel")
                if isinstance(previous.get("panel"), dict)
                else _legacy_state_to_panel(previous.get("protagonist_state") or {})
            ),
            "changes": previous.get("changes") if isinstance(previous.get("changes"), list) else [],
        }
    config = _read_json_file(_mechanics_path(ws, "system_panel.json")) or {}
    return {
        "chapter": max(0, chapter_num - 1),
        "panel": (
            config.get("initial_panel")
            if isinstance(config.get("initial_panel"), dict)
            else _legacy_state_to_panel(config.get("protagonist_initial_state") or {})
        ),
        "changes": [],
    }


def _panel_state_for_prompt(previous):
    """Current panel only. Last chapter's changelog is omitted so the model does not copy it."""
    previous = previous if isinstance(previous, dict) else {}
    payload = {
        "chapter": previous.get("chapter", 0),
        "panel": previous.get("panel") if isinstance(previous.get("panel"), dict) else {},
    }
    if "enabled" in previous:
        payload["enabled"] = previous["enabled"]
    return payload


SYSTEM_PANEL_MAX_PANEL_FIELDS = 40
SYSTEM_PANEL_MAX_CHANGES = 30
SYSTEM_PANEL_MAX_CHARS = 50000


class SystemPanelValidationError(RuntimeError):
    """The model-returned system panel failed JSON or basic-structure validation."""


def _legacy_state_to_panel(state):
    """Migrate a legacy state snapshot into a freely extensible generic panel."""
    state = state if isinstance(state, dict) else {}
    labels = {
        "identity": "Identity",
        "attributes": "Attributes",
        "values": "Core values",
        "resources": "Resources",
        "inventory": "Items",
        "equipment": "Equipment",
        "skills": "Skills",
        "tasks": "Tasks",
        "task_progress": "Task progress",
        "relationships": "Relationships",
        "injuries_and_status": "Current state",
        "flags": "Status flags",
    }
    panel = {}
    for key, value in state.items():
        if key in {"version", "mode", "chapter"}:
            continue
        if value not in ({}, [], "", None):
            panel[labels.get(key, key)] = value
    return panel


def _validate_system_panel_response(payload):
    if not isinstance(payload, dict):
        raise ValueError("Top level must be a JSON object")
    unknown = set(payload) - {"panel", "changes"}
    if unknown:
        raise ValueError(f"Contains disallowed top-level fields: {', '.join(sorted(unknown))}")
    panel = payload.get("panel")
    changes = payload.get("changes")
    if not isinstance(panel, dict):
        raise ValueError("panel must be an object")
    if len(panel) > SYSTEM_PANEL_MAX_PANEL_FIELDS:
        raise ValueError(
            f"panel may contain at most {SYSTEM_PANEL_MAX_PANEL_FIELDS} top-level fields"
        )
    if not isinstance(changes, list):
        raise ValueError("changes must be an array")
    # Changelog is a summary; the panel is the source of truth. Truncate instead of
    # failing generation when the model lists every nested field that moved.
    if len(changes) > SYSTEM_PANEL_MAX_CHANGES:
        changes = changes[:SYSTEM_PANEL_MAX_CHANGES]
    normalized = []
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise ValueError(f"changes[{index}] must be an object")
        field = change.get("field")
        reason = change.get("reason", "")
        if not isinstance(field, str) or not field.strip():
            raise ValueError(f"changes[{index}].field must be a non-empty string")
        if "before" not in change or "after" not in change:
            raise ValueError(f"changes[{index}] must include before and after")
        if not isinstance(reason, str):
            raise ValueError(f"changes[{index}].reason must be a string")
        normalized.append({
            "field": field.strip(),
            "before": change["before"],
            "after": change["after"],
            "reason": reason.strip(),
        })
    result = {"panel": panel, "changes": normalized}
    try:
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"panel contains an illegal JSON value: {exc}") from exc
    while len(serialized) > SYSTEM_PANEL_MAX_CHARS and result["changes"]:
        result["changes"].pop()
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(serialized) > SYSTEM_PANEL_MAX_CHARS:
        raise ValueError("System panel content is too long")
    return result


def _generate_chapter_system_panel(llm, ws, volume, chapter_num, chapter_outline,
                                   cancel_event=None):
    if not system_panel_status(ws)["enabled"]:
        return None
    previous = _previous_system_panel(ws, volume, chapter_num)
    definition = _read_json_file(_mechanics_path(ws, "system_panel.json")) or {
        "enabled": True,
        "visible_panel": False,
        "rules": {"constraints": ["Keep protagonist-state continuity; do not invent numeric changes the chapter outline did not show."]},
    }
    validation_feedback = "(first generation; no validation errors)"
    last_error = ""
    for _attempt in range(3):
        prompt = PromptLoader.load(
            "chapter_system_panel",
            chapter_num=chapter_num,
            system_panel_definition=json.dumps(definition, ensure_ascii=False, indent=2),
            previous_system_panel=json.dumps(
                _panel_state_for_prompt(previous), ensure_ascii=False,
            ),
            chapter_outline=chapter_outline,
            validation_feedback=validation_feedback,
        )
        raw = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2))
        try:
            response = _validate_system_panel_response(parse_json_response(raw))
            panel = {
                "chapter": chapter_num,
                "panel": response["panel"],
                "changes": response["changes"],
            }
            break
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            validation_feedback = (
                f"Previous return failed validation: {last_error}."
                "Fix it and output complete JSON again. Do not explain."
            )
    else:
        inherited = previous.get("panel") if isinstance(previous.get("panel"), dict) else {}
        print(
            f"  Warning: chapter {chapter_num} system panel failed JSON validation "
            f"({last_error}); inheriting the previous panel so generation can continue."
        )
        panel = {
            "chapter": chapter_num,
            "panel": inherited,
            "changes": [],
        }
    path = _system_panel_chapter_path(ws, volume, chapter_num)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_json_file(path, panel)
    return panel


def _update_chapter_system_panel_with_controls(
    llm, ws, volume, chapter_num, chapter_outline, completed, total,
    progress_callback=None, pause_event=None, stop_event=None, cancel_event=None,
):
    while True:
        try:
            if progress_callback:
                progress_callback(
                    "system_panel", completed, total,
                    f"Chapter outline saved; updating chapter {chapter_num} system panel",
                )
            _generate_chapter_system_panel(
                llm, ws, volume, chapter_num, chapter_outline, cancel_event,
            )
            return True
        except LLMCallCancelled:
            if stop_event is not None and stop_event.is_set():
                return False
            if progress_callback:
                progress_callback(
                    "paused", completed, total,
                    f"Chapter {chapter_num} system-panel update paused; continue to update again",
                )
            if pause_event is not None:
                pause_event.wait()
            if cancel_event is not None:
                cancel_event.clear()


def sync_finalized_drafts_for_outlines(
    llm, ws, volume, through_chapter, progress_callback=None,
    pause_event=None, stop_event=None, cancel_event=None,
):
    """Walk finalized drafts in order and reverse-sync matching chapter outlines and end-of-chapter system panels."""
    status = chapter_finalization_status(ws)
    records = status["drafts"].get(f"vol_{volume:02d}", {})
    pending = sorted(
        int(chapter) for chapter, record in records.items()
        if int(chapter) <= through_chapter and record.get("status") != "synced"
    )
    if not pending:
        return []

    outline_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    synced = []
    for index, chapter in enumerate(pending):
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback("paused", index, len(pending), "Final-draft outline sync paused")
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break

        live_status = chapter_finalization_status(ws)
        record = live_status["drafts"].get(f"vol_{volume:02d}", {}).get(str(chapter))
        if not record or record.get("status") == "synced":
            continue
        finalized_draft = _read_file(_draft_chapter_path(ws, volume, chapter))
        if not finalized_draft:
            raise RuntimeError(f"Chapter {chapter} final draft is missing or empty; cannot sync.")
        expected_hash = record.get("current_hash") or _content_hash(finalized_draft)
        outline_path = os.path.join(outline_dir, f"chapter_{chapter:03d}.md")
        current_outline = _read_file(outline_path) or "(this chapter has no existing outline)"
        previous_panel = _previous_system_panel(ws, volume, chapter)
        if progress_callback:
            progress_callback(
                "syncing_finalized_draft", index, len(pending),
                f"Syncing the chapter outline from chapter {chapter} final draft",
            )
        prompt = PromptLoader.load(
            "finalized_draft_outline_sync",
            chapter_num=chapter,
            previous_system_panel=json.dumps(previous_panel, ensure_ascii=False, indent=2),
            current_outline=current_outline,
            finalized_draft=finalized_draft,
        )
        while True:
            try:
                synced_outline = normalize_text(
                    _generate_with_cancel(llm, prompt, cancel_event, temperature=0.2)
                )
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    return synced
                if progress_callback:
                    progress_callback(
                        "paused", index, len(pending),
                        f"Chapter {chapter} draft sync paused; continue to sync again",
                    )
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if not synced_outline:
            raise RuntimeError(f"Chapter {chapter} final draft did not produce a synced chapter outline.")

        old_panel = _read_json_file(_system_panel_chapter_path(ws, volume, chapter)) or {}
        panel_source = (
            "[Final draft (highest-priority facts for this chapter)]\n"
            + finalized_draft
            + "\n\n[Chapter outline synced from the final draft]\n"
            + synced_outline
            + "\n\n[Old system panel for this chapter (fill missing fields only; on conflict the final draft wins)]\n"
            + json.dumps(old_panel, ensure_ascii=False, indent=2)
        )
        if not _update_chapter_system_panel_with_controls(
            llm, ws, volume, chapter, panel_source, index, len(pending),
            progress_callback, pause_event, stop_event, cancel_event,
        ):
            break
        _write_file(outline_path, synced_outline)
        _mark_finalized_draft_synced(ws, volume, chapter, expected_hash)
        synced.append(chapter)
        if progress_callback:
            progress_callback(
                "syncing_finalized_draft", index + 1, len(pending),
                f"Chapter {chapter} outline and system panel synced",
            )
    return synced


def init_mechanics(ws, force=False, creative_direction=None, direction_file=None,
                   mechanics_file=None, disable=False):
    """Initialize the optional mechanics layer: none / light_state / explicit_mechanics."""
    profile_path = _mechanics_path(ws, "system_panel.json")
    if os.path.exists(profile_path) and not force:
        print(f"Mechanics layer already exists: {profile_path}")
        print("Use --force to overwrite.")
        return

    if disable:
        payload = _default_mechanics_disabled("User explicitly disabled the mechanics layer.")
        _write_mechanics_payload(ws, payload)
        print(f"  -> Mechanics layer disabled: {profile_path}")
        return

    direction = _load_creative_direction(ws, creative_direction, direction_file)
    mechanics_source = ""
    if mechanics_file:
        mechanics_source = _read_file(mechanics_file) or ""
        if not mechanics_source:
            print(f"Error: mechanics settings file is missing or empty: {mechanics_file}")
            return
    elif creative_direction:
        mechanics_source = creative_direction

    assets = _load_story_design_assets(ws)
    llm = _get_llm()
    if not llm:
        return

    print(">>> Initialize mechanics layer <<<")
    if mechanics_source:
        print(f"  -> Loaded user mechanics settings ({len(mechanics_source)} chars)")
    else:
        print("  -> No user mechanics settings provided; whether to enable the mechanics layer will be judged from core gameplay.")

    prompt = PromptLoader.load(
        "mechanics_init",
        mechanics_source=mechanics_source or "(user did not provide mechanics settings)",
        creative_direction=direction or "(no extra direction)",
        core_gameplay=assets["core_gameplay"],
        long_mainline=assets["long_mainline"],
        stage_roadmap=assets["stage_roadmap"],
        character_arcs=assets["character_arcs"],
    )
    raw = normalize_text(llm.generate(prompt))
    try:
        payload = parse_json_response(raw)
    except Exception as exc:
        print(f"  Warning: mechanics JSON parse failed; defaulting to off. Reason: {exc}")
        payload = _default_mechanics_disabled("Mechanics-layer init JSON parse failed; defaulting to off.")
        payload["design"] += "\n\n# Raw return\n" + raw

    payload = _normalize_mechanics_payload(payload)
    _write_mechanics_payload(ws, payload)
    print(f"  -> System-panel definition saved: {_mechanics_path(ws, 'system_panel.json')}")
    print(f"  -> Mechanics-layer mode: {payload['profile']['mode']}")


def _gen_core_gameplay(ws, llm, direction, world_knowledge, force=False):
    output_path = _story_design_path(ws, "core_gameplay.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Core gameplay document already exists: {output_path}")
        return existing

    reference_outline = load_reference_novel_outline(ws.reference_outlines)
    return run_step(
        llm=llm,
        folder="core_gameplay_design",
        label="core gameplay document",
        output_path=output_path,
        prompt_vars=dict(
            creative_direction=direction or "(no extra direction)",
            reference_outline=reference_outline or "(reference novel outline not provided)",
            world_knowledge=world_knowledge or "(target world knowledge base not provided)",
            outline_rules=_load_outline_rules(ws),
        ),
    )


def _gen_long_mainline(ws, llm, direction, world_knowledge, force=False):
    output_path = _story_design_path(ws, "long_mainline.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Book long mainline already exists: {output_path}")
        return existing

    reference_outline = load_reference_novel_outline(ws.reference_outlines)
    core_gameplay = _read_file(_story_design_path(ws, "core_gameplay.md")) or "(not generated: core gameplay)"

    return run_step(
        llm=llm,
        folder="long_mainline_design",
        label="book long mainline",
        output_path=output_path,
        prompt_vars=dict(
            creative_direction=direction or "(no extra direction)",
            core_gameplay=core_gameplay,
            reference_outline=reference_outline or "(reference novel outline not provided)",
            world_knowledge=world_knowledge or "(target world knowledge base not provided)",
        ),
    )


def _gen_stage_roadmap(ws, llm, direction, world_knowledge, force=False):
    output_path = _story_design_path(ws, "stage_roadmap.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Stage roadmap already exists: {output_path}")
        return existing

    core_gameplay = _read_file(_story_design_path(ws, "core_gameplay.md")) or "(not generated: core gameplay)"
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md")) or "(not generated: long mainline)"

    result = run_step(
        llm=llm,
        folder="stage_roadmap_design",
        label="book stage roadmap",
        save=f"  -> Stage roadmap saved: {output_path}",
        output_path=output_path,
        prompt_vars=dict(
            creative_direction=direction or "(no extra direction)",
            core_gameplay=core_gameplay,
            long_mainline=long_mainline,
            world_knowledge=world_knowledge or "(target world knowledge base not provided)",
        ),
    )
    # Normalize to a stable stage header (`# Stage N: name`) so later steps can find stages.
    result = _normalize_stage_roadmap(result)
    if result:
        _write_file(output_path, result)
    return result


def _gen_character_arcs(ws, llm, direction, world_knowledge, force=False):
    output_path = _story_design_path(ws, "character_arcs.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Character arcs already exist: {output_path}")
        return existing

    core_gameplay = _read_file(_story_design_path(ws, "core_gameplay.md")) or "(not generated: core gameplay)"
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md")) or "(not generated: long mainline)"
    stage_roadmap = _read_file(_story_design_path(ws, "stage_roadmap.md")) or "(not generated: stage roadmap)"

    return run_step(
        llm=llm,
        folder="character_arcs_design",
        label="character arcs",
        output_path=output_path,
        prompt_vars=dict(
            creative_direction=direction or "(no extra direction)",
            core_gameplay=core_gameplay,
            long_mainline=long_mainline,
            stage_roadmap=stage_roadmap,
            world_knowledge=world_knowledge or "(target world knowledge base not provided)",
        ),
    )


def _load_reference_context(ws):
    return load_reference_novel_outline(ws.reference_outlines) or "(reference novel outline not provided)"


def _reference_volume_structure_context(ws, per_volume_chars=1800, max_chars=36000):
    """Extract only each volume's Volume overview and Three-act structure as structure reference for the new-book phase outline.

    Do not concatenate character, foreshadowing, or setting blocks, so long reference material does not crowd the new-book worldview and
    rough-outline context. Per-volume and total length are both capped.
    """
    volumes = list_reference_volumes(ws.reference_outlines)
    # When there are many volumes, split the budget evenly so later volume outlines are not dropped.
    volume_budget = max(
        450,
        min(per_volume_chars, max_chars // max(1, len(volumes)) - 120),
    )
    overview_budget = max(180, int(volume_budget * 0.35))
    three_acts_budget = max(250, volume_budget - overview_budget)
    parts = []
    used = 0
    for volume in volumes:
        content = load_reference_volume_outline(ws.reference_outlines, volume["vol_idx"]) or ""
        if not content:
            continue
        sections = _parse_markdown_h1_sections(content)

        selected = []
        overview = _lookup_volume_style_section(sections, "Volume overview")
        three_acts = _lookup_volume_style_section(sections, "Three-act structure")
        if overview:
            selected.append(overview[:overview_budget].rstrip())
        if three_acts:
            selected.append(three_acts[:three_acts_budget].rstrip())
        selected_text = "\n\n".join(part for part in selected if part).strip()
        if len(selected_text) > volume_budget:
            selected_text = selected_text[:volume_budget].rstrip()
        if not selected:
            selected_text = "(this volume has no Volume overview or Three-act structure)"

        meta = _read_file(os.path.join(volume["dir_path"], "meta.json"))
        chapter_range = ""
        if meta:
            try:
                meta_data = json.loads(meta)
                start_ch = int(meta_data.get("start_ch") or 0)
                end_ch = int(meta_data.get("end_ch") or 0)
                if start_ch > 0 and end_ch >= start_ch:
                    chapter_range = f" | chapters {start_ch}-{end_ch}"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        part = (
            f"## Reference volume {volume['vol_idx']}: {volume['title']}{chapter_range}\n\n"
            + selected_text
        )
        parts.append(part)
        used += len(part)
    return "\n\n---\n\n".join(parts) or "(no reference-novel volume outlines found)"


def _reference_volume_stage_structure(ws, volume):
    """Read one volume's full Volume overview + Three-act structure for tail-phase incremental sync."""
    content = load_reference_volume_outline(
        ws.reference_outlines, volume["vol_idx"]
    ) or ""
    sections = _parse_markdown_h1_sections(content)
    selected = [
        _lookup_volume_style_section(sections, "Volume overview"),
        _lookup_volume_style_section(sections, "Three-act structure"),
    ]
    return "\n\n".join(item for item in selected if item).strip() or "(matching reference volume is missing Volume overview and Three-act structure)"


def _design_structure_guidance(ws):
    """New-book phase count matches reference volume count one-to-one; without reference volumes use a fallback range."""
    volume_count = len(list_reference_volumes(ws.reference_outlines))
    if volume_count == 0:
        stage_min, stage_max = 5, 7
    else:
        stage_min = stage_max = volume_count
    map_min = max(3, math.ceil(stage_min * 0.75))
    map_max = max(map_min + 2, stage_max)
    return {
        "reference_volume_count": volume_count,
        "stage_range": str(stage_min) if stage_min == stage_max else f"{stage_min}-{stage_max}",
        "stage_min": stage_min,
        "stage_max": stage_max,
        "map_range": f"{map_min}-{map_max}",
        "map_min": map_min,
        "map_max": map_max,
    }


def _design_structure_counts(rough, worldview):
    chinese_number = r"[一二三四五六七八九十百]+"
    arabic_or_chinese = rf"(?:\d+|{chinese_number})"
    phase_token = rf"(?:阶段|phase)"
    stage_patterns = (
        # ## Phase 1 / ## 阶段1 / ### 阶段一 / ## 第八阶段
        rf"(?m)^\s*#{{2,6}}\s*(?:第\s*)?(?:{phase_token}\s*{arabic_or_chinese}|{arabic_or_chinese}\s*{phase_token})\b",
        # 1. Phase 1 / - 第八阶段 (models sometimes skip subheadings)
        rf"(?m)^\s*(?:[-*+]|\d+[.、．])\s*(?:第\s*)?(?:{phase_token}\s*{arabic_or_chinese}|{arabic_or_chinese}\s*{phase_token})\b",
    )
    stage_lines = set()
    for pattern in stage_patterns:
        stage_lines.update(
            match.group(0).strip()
            for match in re.finditer(pattern, rough or "", re.IGNORECASE)
        )
    stage_count = len(stage_lines)
    map_text = ""
    worldview_lines = (worldview or "").splitlines()
    for index, line in enumerate(worldview_lines):
        heading = re.match(r"^(#{1,6})\s*(.+?)\s*$", line.strip())
        if not heading:
            continue
        heading_title = re.sub(r"^\s*6\s*[.、．:]?\s*", "", heading.group(2)).strip()
        title_l = heading_title.lower()
        is_map_heading = (
            (
                "地图" in heading_title
                and any(word in heading_title for word in ("舞台", "区域", "版图"))
                and any(word in heading_title for word in ("层级", "层次", "体系", "结构"))
            )
            or ("map" in title_l and "layer" in title_l)
        )
        if not is_map_heading:
            continue
        heading_level = len(heading.group(1))
        body = []
        for following in worldview_lines[index + 1:]:
            next_heading = re.match(r"^(#{1,6})\s+\S", following.strip())
            if next_heading and len(next_heading.group(1)) <= heading_level:
                break
            body.append(following)
        map_text = "\n".join(body)
        break
    map_count = len(re.findall(r"(?m)^\s*(?:[-*+]|\d+[.、．])\s+\S+", map_text))
    if not map_count:
        map_count = len(re.findall(r"(?m)^##+\s+\S+", map_text))
    if not map_count:
        map_count = len(re.findall(
            rf"(?mi)^\s*(?:层级|地图|舞台|layer)\s*{arabic_or_chinese}\s*[：:]|"
            rf"^\s*第\s*{arabic_or_chinese}\s*(?:层|级|区域|舞台)\s*[：:]",
            map_text,
        ))
    if not map_count and map_text:
        # Models sometimes put several layers in one paragraph, split by semicolons.
        labels = re.findall(
            rf"(?i)(?:层级|地图|舞台|layer)\s*{arabic_or_chinese}\s*[：:]|"
            rf"第\s*{arabic_or_chinese}\s*(?:层|级|区域|舞台)\s*[：:]",
            map_text,
        )
        map_count = len(labels)
    return stage_count, map_count


def _remove_stage_outline_section(rough):
    """Keep phase-outline content out of rough_outline.md; phases live in their own file."""
    lines = (rough or "").splitlines()
    output = []
    skipping = False
    skipped_level = 0
    for line in lines:
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
        if heading and (
            "阶段粗纲" in heading.group(2)
            or re.search(r"phase\s+outline", heading.group(2), re.I)
        ):
            skipping = True
            skipped_level = len(heading.group(1))
            continue
        if skipping:
            if heading and len(heading.group(1)) <= skipped_level:
                skipping = False
            else:
                continue
        output.append(line)
    return "\n".join(output).strip()


def _stage_outline_sections(stage_outline):
    """Split the standalone phase outline into {phase number: full phase text}."""
    headings = list(STAGE_OUTLINE_HEADING_RE.finditer(stage_outline or ""))
    sections = {}
    for index, heading in enumerate(headings):
        number = int(heading.group(1))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(stage_outline)
        sections[number] = (stage_outline or "")[heading.start():end].strip()
    return sections


def _completed_stage_prefix(stage_roadmap, target_count):
    """Keep only the contiguous prefix from stage 1; later stages must be generated serially from that prefix."""
    parts = []
    for number in range(1, target_count + 1):
        content = _extract_stage_from_roadmap(stage_roadmap, number)
        if not content or not _is_volume_style_stage(content):
            break
        parts.append(content)
    return parts


_VOLUME_STYLE_SECTIONS_ZH = ("卷纲概览", "三幕结构", "人物谱系", "伏笔追踪", "核心爽点")
_VOLUME_STYLE_SECTIONS_EN = (
    "Volume overview", "Three-act structure", "Character roster",
    "Foreshadowing tracker", "Core payoff",
)
_VOLUME_STYLE_SECTION_ALIASES = {
    "Volume overview": ("Volume overview", "Volume-outline overview", "卷纲概览"),
    "Three-act structure": ("Three-act structure", "三幕结构"),
    "Character roster": ("Character roster", "人物谱系"),
    "Foreshadowing tracker": ("Foreshadowing tracker", "Foreshadowing tracking", "伏笔追踪"),
    "Core payoff": ("Core payoff", "Core payoffs", "核心爽点"),
}


def _parse_markdown_h1_sections(content):
    sections = {}
    text = content or ""
    matches = list(re.finditer(r"(?m)^#\s+(.+?)\s*$", text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = re.sub(
            r"^\s*(?:[一二三四五六七八九十百]+|\d+|[IVXivx]+)\s*[、.．：:]\s*",
            "",
            match.group(1).strip(),
        )
        sections[title] = text[match.start():end].strip()
    return sections


def _lookup_volume_style_section(sections, english_name):
    aliases = _VOLUME_STYLE_SECTION_ALIASES.get(english_name, (english_name,))
    for alias in aliases:
        value = sections.get(alias) or ""
        if value:
            return value
    wanted = {alias.lower() for alias in aliases}
    for key, value in sections.items():
        if key.strip().lower() in wanted and value:
            return value
    return ""


def _is_volume_style_stage(content):
    text = content or ""
    has_sections = all(
        any(alias in text for alias in aliases)
        for aliases in _VOLUME_STYLE_SECTION_ALIASES.values()
    )
    has_count = bool(re.search(
        r"(?:预计章节数|Planned chapters?)\s*[：:]\s*\d+",
        text,
        re.I,
    ))
    return has_sections and has_count


def _reference_volume_chapter_count(volume, volume_outline):
    """Return the actual chapter scale of a reference volume, preferring volume-boundary metadata."""
    meta = _read_json_file(os.path.join(volume.get("dir_path") or "", "meta.json")) or {}
    try:
        start = int(meta.get("start_ch") or 0)
        end = int(meta.get("end_ch") or 0)
    except (TypeError, ValueError):
        start = end = 0
    if start > 0 and end >= start:
        return end - start + 1

    # Older workspaces without meta.json: infer from Three-act chapter ranges in the volume outline.
    ranges = re.findall(
        r"第\s*(\d+)\s*章?\s*(?:至|到|[-—~])\s*第?\s*(\d+)\s*章",
        volume_outline or "",
    )
    if ranges:
        starts = [min(int(left), int(right)) for left, right in ranges]
        ends = [max(int(left), int(right)) for left, right in ranges]
        return max(ends) - min(starts) + 1

    try:
        fallback = int(volume.get("chapter_count") or 0)
    except (TypeError, ValueError):
        fallback = 0
    if fallback > 0:
        return fallback
    raise RuntimeError(
        f"Cannot derive a chapter count from reference volume {volume.get('vol_idx') or ''} "
        "metadata or Three-act structure. Please complete the reference volume outline first."
    )


def gen_design_concept(
    ws, force=False, creative_direction=None, direction_file=None,
    progress_callback=None,
):
    """Serially generate worldview, a rough_outline without the phase outline, and a separate stage_outline."""
    print(">>> Book design: serially generate worldview, rough outline, and phase outline <<<")
    direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, direction, "initial")

    rough_path = _rough_outline_path(ws)
    worldview_path = _worldview_path(ws)
    stage_outline_path = _stage_outline_path(ws)
    existing_rough = _read_file(rough_path)
    existing_worldview = _read_file(worldview_path)
    existing_stage_outline = _read_file(stage_outline_path)

    def report(phase, completed, detail):
        if progress_callback:
            progress_callback(phase, completed, 3, detail)
    structure_guidance = _design_structure_guidance(ws)
    expected_existing_stages = structure_guidance["reference_volume_count"]
    existing_stage_count, _ = _design_structure_counts(existing_stage_outline, "")
    existing_stage_valid = _is_real_design_field(existing_stage_outline) and (
        expected_existing_stages == 0
        or existing_stage_count == expected_existing_stages
    )
    if _is_real_design_field(existing_stage_outline) and not existing_stage_valid:
        print(
            f"  -> Existing phase outline has {existing_stage_count} phases, "
            f"which does not match the reference novel's {expected_existing_stages} volumes; regenerating the phase outline."
        )
        existing_stage_outline = ""
    if (
        not force
        and _is_real_design_field(existing_rough)
        and _is_real_design_field(existing_worldview)
        and existing_stage_valid
    ):
        print("  -> Worldview, rough outline, and phase outline already exist; skipping (use --force to overwrite, or refine in chat).")
        return {
            "worldview": existing_worldview,
            "rough_outline": existing_rough,
            "stage_outline": existing_stage_outline,
        }

    # Book design is a core creative task; use the user-configured ADAPTIVE_BUILDER (pro) model.
    llm = _get_llm()
    if not llm:
        return {}
    reference_outline = _load_reference_context(ws)

    # Write each step to disk immediately. If a later step fails, reuse the previous one instead of resending long context.
    worldview = existing_worldview if not force else ""
    report("worldview", 0, "Generating the new-novel worldview")
    if not _is_real_design_field(worldview):
        # Target-world sources are the factual bound for worldview generation; the reference novel supplies structure only.
        # Cap at 60k chars so stacking with the reference full outline does not crowd model context.
        world_knowledge = _load_world_knowledge_optional(
            ws, "new-novel worldview", max_chars=60000, require_ready=True,
        )
        prompt = PromptLoader.load(
            "design_worldview",
            creative_direction=direction or "(no extra direction)",
            world_knowledge=world_knowledge or "(target world knowledge base not provided; create an original world.)",
            reference_outline=reference_outline,
        )
        payload = parse_json_response(_call_design_llm(llm, prompt, "new-novel worldview"))
        worldview = _normalize_design_field(payload, "worldview_md", "# Worldview")
        if not _is_real_design_field(worldview):
            raise RuntimeError("Worldview generation failed: the model did not return valid content. Please retry.")
        _write_file(worldview_path, worldview)
        print(f"  -> Worldview saved: {worldview_path}")
    else:
        print("  -> Reusing the generated worldview.")
    report("worldview_complete", 1, "Worldview generated; generating rough outline")

    rough = existing_rough if not force else ""
    if not _is_real_design_field(rough):
        prompt = PromptLoader.load(
            "design_rough_outline",
            creative_direction=direction or "(no extra direction)",
            worldview=worldview,
            reference_outline=reference_outline,
            outline_rules=_load_outline_rules(ws),
        )
        payload = parse_json_response(_call_design_llm(llm, prompt, "new-novel rough outline"))
        rough = _normalize_design_field(payload, "rough_outline_md", "# Rough outline")
        rough = _remove_stage_outline_section(rough)
        if not _is_real_design_field(rough):
            raise RuntimeError("Rough-outline generation failed: the model did not return valid content. Please retry.")
        _write_file(rough_path, rough)
        print(f"  -> Rough outline saved: {rough_path}")
    else:
        print("  -> Reusing the generated rough outline.")
    report("rough_outline_complete", 2, "Rough outline generated; generating phase outline")

    stage_outline = existing_stage_outline if not force else ""
    if not _is_real_design_field(stage_outline):
        base_prompt = PromptLoader.load(
            "design_stage_outline",
            worldview=worldview,
            rough_outline=rough,
            reference_volume_structures=_reference_volume_structure_context(ws),
            **structure_guidance,
        )
        expected_count = structure_guidance["reference_volume_count"]
        actual_count = 0
        for attempt in range(1, 3):
            prompt = base_prompt
            if attempt > 1:
                prompt += (
                    "\n\n[Previous output failed the count check]\n"
                    f"Last time generated {actual_count} phases; this time must generate exactly {expected_count} phases."
                )
            payload = parse_json_response(
                _call_design_llm(llm, prompt, f"new-novel phase outline (attempt {attempt})")
            )
            candidate = _normalize_design_field(payload, "stage_outline_md", "# Phase outline")
            if not _is_real_design_field(candidate):
                continue
            actual_count, _ = _design_structure_counts(candidate, "")
            if expected_count == 0 or actual_count == expected_count:
                stage_outline = candidate
                break
            print(
                f"  -> Phase-count check failed: generated {actual_count},"
                f"expected {expected_count}; retrying automatically."
            )
        if not _is_real_design_field(stage_outline) or (
            expected_count > 0 and actual_count != expected_count
        ):
            raise RuntimeError(
                f"Phase-outline generation failed: expected {expected_count} phases, "
                f"the model produced {actual_count}; nothing was written. Please retry."
            )
        _write_file(stage_outline_path, stage_outline)
        print(f"  -> Phase outline saved: {stage_outline_path}")
    else:
        print("  -> Reusing the generated phase outline.")
    report("stage_outline_complete", 3, "Worldview, rough outline, and phase outline are all generated")

    stage_count, map_count = _design_structure_counts(stage_outline, worldview)
    structure_warning = ""
    expected_stage_count = structure_guidance["reference_volume_count"]
    stage_invalid = (
        stage_count != expected_stage_count
        if expected_stage_count > 0
        else stage_count < structure_guidance["stage_min"]
    )
    if stage_invalid or map_count < structure_guidance["map_min"]:
        structure_warning = (
            "Structure coverage may be insufficient: "
            f"phases {stage_count}/{structure_guidance['stage_min']}, "
            f"maps {map_count}/{structure_guidance['map_min']}."
        )
        print(f"  Warning: {structure_warning}")
    _mark_reference_chapters_used(ws, [card["chapter"] for card in _reference_chapter_cards(ws)])
    _record_story_design_reference_snapshot(ws, reset_extensions=True)
    _mark_concept_revision(ws)
    result = {
        "worldview": worldview,
        "rough_outline": rough,
        "stage_outline": stage_outline,
    }
    if structure_warning:
        result["structure_warning"] = structure_warning
        result["adjustment_note"] = structure_warning
    return result


def gen_stage_design(
    ws, force=False, creative_direction=None, direction_file=None,
    progress_callback=None, cancel_event=None,
):
    """Generate the long mainline first, then generate the stage roadmap serially by phase and matching reference volume outline."""
    print(">>> Stage two: generate the long mainline, then generate the stage roadmap serially <<<")
    rough = _read_file(_rough_outline_path(ws))
    worldview = _read_file(_worldview_path(ws))
    stage_outline = _read_file(_stage_outline_path(ws))
    stage_sections = _stage_outline_sections(stage_outline)
    reference_volumes = list_reference_volumes(ws.reference_outlines)
    total_stages = len(stage_sections)

    def report(phase, completed, detail):
        if progress_callback:
            progress_callback(phase, completed, max(1, total_stages), detail)

    if not rough or not worldview or not stage_outline:
        print("Error: finish book design (worldview, rough outline, and phase outline) before stage design.")
        return {}
    if len(stage_sections) != len(reference_volumes):
        raise RuntimeError(
            f"Phase outline count does not match reference volumes: {len(stage_sections)} phases, "
            f"{len(reference_volumes)} reference volumes. Please regenerate the phase outline first."
        )

    direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, direction, "stage_design")
    long_path = _story_design_path(ws, "long_mainline.md")
    stage_path = _story_design_path(ws, "stage_roadmap.md")
    existing_long = _read_file(long_path)
    existing_stage = _read_file(stage_path)
    design_state = _load_story_design_state(ws)
    if int(design_state.get("stage_pipeline_version") or 0) != STAGE_DESIGN_PIPELINE_VERSION:
        existing_long = ""
        existing_stage = ""
        print("  -> Detected a legacy stage-design artifact; regenerating with the serial flow.")

    # Stage design is a core creative task; use the user-configured Pro model.
    llm = _get_llm()
    if not llm:
        return {}

    report("long_mainline", 0, "Generating the book long mainline")
    long_mainline = existing_long if not force else ""
    long_generated = False
    if not _is_real_design_field(long_mainline):
        prompt = PromptLoader.load(
            "long_mainline_serial",
            worldview=worldview,
            rough_outline=rough,
            stage_outline=stage_outline,
        )
        payload = parse_json_response(
            _call_design_llm(llm, prompt, "book long mainline", cancel_event=cancel_event)
        )
        long_mainline = _normalize_design_field(payload, "long_mainline_md", "# Long mainline")
        if not _is_real_design_field(long_mainline):
            raise RuntimeError("Long-mainline generation failed: the model did not return valid content. Please retry.")
        _write_file(long_path, long_mainline)
        design_state = _load_story_design_state(ws)
        design_state["stage_pipeline_version"] = STAGE_DESIGN_PIPELINE_VERSION
        design_state["stage_pipeline_updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json_file(_story_design_state_path(ws), design_state)
        long_generated = True
        print(f"  -> Long mainline saved: {long_path}")
    else:
        print("  -> Reusing the generated long mainline.")
    report(
        "long_mainline_complete", 0,
        f"Long mainline generated; preparing stage 1/{total_stages}",
    )

    # Force rebuild or a freshly regenerated long mainline starts at stage 1; ordinary retry after restart reuses the contiguous prefix.
    completed_parts = [] if (force or long_generated) else _completed_stage_prefix(
        existing_stage, len(stage_sections)
    )
    if completed_parts:
        print(f"  -> Detected {len(completed_parts)}/{len(stage_sections)} contiguous stages; continuing from the breakpoint.")
        report(
            "stage_resume", len(completed_parts),
            f"Kept {len(completed_parts)}/{total_stages} stages; continuing from the breakpoint",
        )
    if len(completed_parts) == len(stage_sections):
        stage_roadmap = "\n\n".join(completed_parts)
        print("  -> Stage roadmap is already complete; skipping.")
    else:
        # Write back the trusted contiguous prefix; drop old content after a gap that cannot guarantee serial deps.
        if completed_parts:
            _write_file(stage_path, "\n\n".join(completed_parts))
        elif os.path.exists(stage_path):
            _write_file(stage_path, "")

        for number in range(len(completed_parts) + 1, len(stage_sections) + 1):
            report(
                "stage_generating", number - 1,
                f"Generating stage {number}/{total_stages}",
            )
            volume = reference_volumes[number - 1]
            reference_volume_outline = load_reference_volume_outline(
                ws.reference_outlines, volume["vol_idx"]
            ) or "(matching reference volume outline is missing)"
            reference_chapter_count = _reference_volume_chapter_count(
                volume, reference_volume_outline,
            )
            previous_stage = completed_parts[-1] if completed_parts else "(this is the first stage; no previous stage)"
            prompt = PromptLoader.load(
                "stage_roadmap_serial",
                stage_number=number,
                total_stages=len(stage_sections),
                long_mainline=long_mainline,
                current_stage_outline=stage_sections[number],
                reference_volume_number=volume["vol_idx"],
                reference_volume_title=volume["title"],
                reference_chapter_count=reference_chapter_count,
                reference_volume_outline=reference_volume_outline,
                previous_stage=previous_stage,
            )
            payload = parse_json_response(
                _call_design_llm(
                    llm, prompt, f"stage {number}/{len(stage_sections)}",
                    cancel_event=cancel_event,
                )
            )
            stage = _normalize_design_field(payload, "stage_roadmap_md", "")
            stage = _normalize_stage_roadmap(stage)
            numbers = [int(value) for value in STAGE_HEADING_RE.findall(stage)]
            if numbers != [number]:
                raise RuntimeError(
                    f"Stage {number} generation has an invalid number (detected {numbers or 'none'}); "
                    "previous stages were kept. Please retry to continue."
                )
            required_sections = (
                _VOLUME_STYLE_SECTIONS_EN
                if any(name in stage for name in _VOLUME_STYLE_SECTIONS_EN)
                else _VOLUME_STYLE_SECTIONS_ZH
            )
            missing_sections = [name for name in required_sections if name not in stage]
            if not _is_volume_style_stage(stage):
                raise RuntimeError(
                    f"Stage {number} format is incomplete: missing "
                    + (", ".join(missing_sections) if missing_sections else "Planned chapters")
                    + ". Previous stages were kept. Please retry to continue."
                )
            completed_parts.append(stage)
            stage_roadmap = "\n\n".join(completed_parts)
            _write_file(stage_path, stage_roadmap)
            mapped_arc_paths = []
            for arc in list_reference_story_arcs(ws.reference_outlines, volume["vol_idx"]):
                try:
                    mapped_arc_paths.append(os.path.relpath(arc["path"], ws.reference_outlines))
                except ValueError:
                    mapped_arc_paths.append(arc["path"])
            if mapped_arc_paths:
                _mark_arcs_used(ws, mapped_arc_paths, [number])
            print(f"  -> Stage {number} saved ({number}/{len(stage_sections)}): {stage_path}")
            report(
                "stage_complete", number,
                (
                    f"Stage {number}/{total_stages} generated; generating stage {number + 1}/{total_stages}"
                    if number < total_stages else
                    f"All {total_stages} stages generated"
                ),
            )

    stage_roadmap = "\n\n".join(completed_parts) if completed_parts else ""
    if not _is_real_design_field(stage_roadmap):
        raise RuntimeError("Stage-roadmap generation failed: no valid stage content was generated.")
    _mark_stage_design_synced(ws)
    report(
        "name_synopsis", total_stages,
        "Stage roadmap generated; generating title and synopsis",
    )
    name_synopsis = gen_novel_name_synopsis(
        ws, force=True, cancel_event=cancel_event,
    )
    print(f"  -> Stage roadmap saved: {stage_path}")
    report("completed", total_stages, f"All {total_stages} stages generated")
    return {
        "long_mainline": long_mainline,
        "stage_roadmap": stage_roadmap,
        "name_synopsis": name_synopsis,
    }


def refine_design_concept(ws, instruction, compact_summary="", use_new_reference=False):
    """Normal book-design refine: the model reads only the instruction and the current three design files."""
    if use_new_reference:
        return sync_stage_outline_from_new_reference(ws, instruction)
    _ = compact_summary  # Keep the call signature; historical summary no longer enters the model.
    paths = {
        "worldview": _worldview_path(ws),
        "rough_outline": _rough_outline_path(ws),
        "stage_outline": _stage_outline_path(ws),
    }
    current = {key: _read_file(path) for key, path in paths.items()}
    if not all(current.values()):
        raise RuntimeError("Finish worldview, rough outline, and phase outline first.")
    llm = _get_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")
    prompt = PromptLoader.load(
        "design_concept_refine",
        instruction=instruction,
        worldview=current["worldview"],
        rough_outline=current["rough_outline"],
        stage_outline=current["stage_outline"],
    )
    payload = parse_json_response(_call_design_llm(llm, prompt, "concept refine"))
    updated = {
        "worldview": _normalize_design_field(payload, "worldview_md", ""),
        "rough_outline": _remove_stage_outline_section(
            _normalize_design_field(payload, "rough_outline_md", "")
        ),
        "stage_outline": _normalize_design_field(payload, "stage_outline_md", ""),
    }
    if not all(updated.values()):
        raise RuntimeError("Book-design adjustment did not return a complete set of three design files.")
    before_count, _ = _design_structure_counts(current["stage_outline"], "")
    after_count, _ = _design_structure_counts(updated["stage_outline"], "")
    if before_count != after_count:
        raise RuntimeError(
            f"A normal book-design adjustment cannot change the phase count: before {before_count}, after {after_count}."
        )
    _backup_design_files(ws, "concept", paths)
    for key, path in paths.items():
        _write_file(path, updated[key])
    _mark_concept_revision(ws)
    updated["adjustment_note"] = str(payload.get("adjustment_note") or "").strip()
    return updated


def sync_stage_outline_from_new_reference(ws, instruction=""):
    """Use only newly added deconstruction to adjust the last phase or append a new phase; do not touch worldview or rough outline."""
    _ = instruction  # Keep the chat-entry signature; incremental generation uses fixed structure input only.
    new_cards = _unused_reference_chapter_context(ws)
    if not new_cards:
        raise ValueError("No newly added deconstruction chapters unused by the phase outline were detected.")
    stage_path = _stage_outline_path(ws)
    stage_outline = _read_file(stage_path)
    sections = _stage_outline_sections(stage_outline)
    reference_volumes = list_reference_volumes(ws.reference_outlines)
    if not sections:
        raise RuntimeError("The current phase outline is empty. Finish the first book design first.")
    if not reference_volumes:
        raise RuntimeError("Reference-novel volume structure not found.")
    if len(reference_volumes) < len(sections):
        raise RuntimeError(
            f"The reference novel currently has only {len(reference_volumes)} volumes, but the phase outline already has {len(sections)} phases; "
            "cannot safely run tail incremental sync."
        )

    llm = _get_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")
    original_stage_outline = stage_outline
    worldview = _read_file(_worldview_path(ws))
    rough_outline = _read_file(_rough_outline_path(ws))
    if not worldview or not rough_outline:
        raise RuntimeError("Finish the new-novel worldview and rough outline first.")
    old_count = len(sections)
    target_count = len(reference_volumes)
    start_number = old_count if target_count == old_count else old_count + 1
    operation_for_first = (
        OPERATION_ADJUST_LAST_PHASE if target_count == old_count else OPERATION_ADD_PHASE
    )

    for number in range(start_number, target_count + 1):
        sections = _stage_outline_sections(stage_outline)
        operation = operation_for_first if number == start_number else OPERATION_ADD_PHASE
        volume = reference_volumes[number - 1]
        reference_structure = _reference_volume_stage_structure(ws, volume)
        if operation in (OPERATION_ADJUST_LAST_PHASE, "调整最后阶段"):
            stage_context = (
                "[Second-to-last phase]\n"
                + (sections.get(number - 1) or "(this is the first phase; no second-to-last phase)")
                + "\n\n[Current last phase]\n"
                + sections[number]
            )
        else:
            stage_context = "[Current last phase]\n" + sections[number - 1]
        prompt = PromptLoader.load(
            "design_stage_outline_incremental",
            operation=operation,
            stage_number=number,
            worldview=worldview,
            rough_outline=rough_outline,
            stage_context=stage_context,
            reference_volume_structure=reference_structure,
        )
        payload = parse_json_response(
            _call_design_llm(llm, prompt, f"incremental phase-outline sync {number}/{target_count}")
        )
        candidate = _normalize_design_field(payload, "stage_outline_md", "")
        numbers = [int(value) for value in STAGE_OUTLINE_HEADING_RE.findall(candidate)]
        if numbers != [number]:
            raise RuntimeError(
                f"Phase-outline increment has an invalid number: expected phase {number}, detected {numbers or 'none'}."
            )
        if operation in (OPERATION_ADJUST_LAST_PHASE, "调整最后阶段"):
            heading = list(STAGE_OUTLINE_HEADING_RE.finditer(stage_outline))[-1]
            stage_outline = stage_outline[:heading.start()].rstrip() + "\n\n" + candidate.strip()
        else:
            stage_outline = stage_outline.rstrip() + "\n\n" + candidate.strip()

    _backup_design_files(ws, "concept_stage_increment", {"stage_outline": stage_path})
    _write_file(stage_path, stage_outline)
    _mark_reference_chapters_used(ws, [number for number, _ in new_cards])
    revision = _mark_concept_revision(ws)
    state = _load_story_design_state(ws)
    state["pending_reference_stage_sync"] = True
    state["reference_stage_increment"] = {
        "concept_revision": revision,
        "kind": "adjust_last" if target_count == old_count else "append",
        "previous_stage_count": old_count,
        "current_stage_count": target_count,
        "reference_chapters": [number for number, _ in new_cards],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json_file(_story_design_state_path(ws), state)
    operation_note = (
        f"Adjusted the last phase (phase {old_count}) from newly added deconstruction chapters."
        if target_count == old_count else
        f"Reference novel grew from {old_count} to {target_count} volumes; appended phases {old_count + 1}-{target_count}."
    )
    return {
        "stage_outline": stage_outline,
        "adjustment_note": operation_note,
        "used_reference_chapters": [number for number, _ in new_cards],
        "previous_stage_outline": original_stage_outline,
    }


def refine_stage_design(
    ws, instruction, compact_summary="", cancel_event=None,
    progress_callback=None, pause_event=None, stop_event=None,
):
    """Route the earliest affected stage, keep the prefix, then serially regenerate the rest."""
    _ = compact_summary  # Keep the call signature; historical summary no longer enters the model.
    long_path = _story_design_path(ws, "long_mainline.md")
    stage_path = _story_design_path(ws, "stage_roadmap.md")
    long_mainline = _read_file(long_path)
    original_roadmap = _read_file(stage_path)
    stage_outline = _read_file(_stage_outline_path(ws))
    stage_sections = _stage_outline_sections(stage_outline)
    reference_volumes = list_reference_volumes(ws.reference_outlines)
    total_stages = len(stage_sections)
    if not long_mainline or not original_roadmap or not stage_sections:
        raise RuntimeError("Generate the long mainline, phase outline, and stage roadmap first.")
    if total_stages != len(reference_volumes):
        raise RuntimeError(
            f"Phase outline count does not match reference volumes: {total_stages} phases, "
            f"{len(reference_volumes)} reference volumes. Please sync the phase outline first."
        )
    original_parts = [
        _extract_stage_from_roadmap(original_roadmap, number)
        for number in range(1, total_stages + 1)
    ]
    if not all(original_parts):
        raise RuntimeError("The current stage roadmap is incomplete; continue generating the missing stages first.")
    llm = _get_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")

    def report(phase, completed, detail):
        if progress_callback:
            progress_callback(phase, completed, max(1, total_stages), detail)

    def stopped_result(completed_parts):
        return {
            "long_mainline": _read_file(long_path) or long_mainline,
            "stage_roadmap": _read_file(stage_path) or original_roadmap,
            "adjustment_note": "This round of stage adjustment ended; completed content was kept.",
            "stopped": True,
        }

    def controlled_call(prompt, label, completed, paused_message):
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            if pause_event is not None and not pause_event.is_set():
                report("paused", completed, paused_message)
                pause_event.wait()
                if stop_event is not None and stop_event.is_set():
                    return None
                if cancel_event is not None:
                    cancel_event.clear()
            try:
                return _call_design_llm(
                    llm, prompt, label, cancel_event=cancel_event,
                )
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    return None
                report("paused", completed, paused_message)
                if pause_event is not None:
                    pause_event.wait()
                if stop_event is not None and stop_event.is_set():
                    return None
                if cancel_event is not None:
                    cancel_event.clear()

    report("routing", 0, "Determining the earliest affected stage")
    route_prompt = PromptLoader.load(
        "stage_design_refine_route",
        instruction=instruction,
        long_mainline=long_mainline,
        stage_roadmap=original_roadmap,
    )
    route_raw = controlled_call(
        route_prompt, "stage-adjust range routing", 0,
        "Range analysis paused; click continue to analyze again",
    )
    if route_raw is None:
        return stopped_result(original_parts)
    routed = parse_json_response(route_raw)
    if not isinstance(routed, dict):
        routed = {}
    try:
        start_stage = int(routed.get("start_stage") or 1)
    except (TypeError, ValueError):
        start_stage = 1
    explicit_stages = [
        int(value) for value in re.findall(
            r"(?:舞台|stage)\s*0*(\d+)", instruction or "", re.I,
        )
        if 1 <= int(value) <= total_stages
    ]
    if explicit_stages:
        start_stage = min(explicit_stages)
    start_stage = min(total_stages, max(1, start_stage))
    mode = str(routed.get("mode") or "").strip().lower()
    if mode not in {"regenerate", "revise"}:
        mode = (
            "regenerate"
            if re.search(
                r"重新生成|完全重写|推倒重来|全部重写|regenerate|rewrite|start over|full rewrite",
                instruction or "",
                re.I,
            )
            else "revise"
        )
    update_long_mainline = routed.get("update_long_mainline") is True
    reason = str(routed.get("reason") or "Located the earliest affected stage from the user instruction.")

    _backup_design_files(ws, "stage", {
        "long_mainline": long_path,
        "stage_roadmap": stage_path,
    })
    if update_long_mainline:
        report("long_mainline_refine", start_stage - 1, "Adjusting the book long mainline")
        long_prompt = PromptLoader.load(
            "stage_long_mainline_refine",
            instruction=instruction,
            long_mainline=long_mainline,
        )
        long_raw = controlled_call(
            long_prompt, "long-mainline adjust", start_stage - 1,
            "Long-mainline adjust paused; click continue to regenerate",
        )
        if long_raw is None:
            return stopped_result(original_parts)
        long_payload = parse_json_response(long_raw)
        updated_long = _normalize_design_field(long_payload, "long_mainline_md", "")
        if not updated_long:
            raise RuntimeError("Long-mainline adjustment did not return valid content; stages were not rewritten this round.")
        long_mainline = updated_long
        _write_file(long_path, long_mainline)

    completed_parts = original_parts[:start_stage - 1]

    for number in range(start_stage, total_stages + 1):
        if stop_event is not None and stop_event.is_set():
            return stopped_result(completed_parts)
        report(
            "stage_refining", number - 1,
            f"Adjusting stage {number}/{total_stages}",
        )
        volume = reference_volumes[number - 1]
        reference_volume_outline = load_reference_volume_outline(
            ws.reference_outlines, volume["vol_idx"]
        ) or "(matching reference volume outline is missing)"
        reference_chapter_count = _reference_volume_chapter_count(
            volume, reference_volume_outline,
        )
        previous_stage = completed_parts[-1] if completed_parts else "(this is the first stage; no previous stage)"
        current_stage = (
            original_parts[number - 1]
            if mode == "revise" else
            "(full regenerate: do not use the current stage's old content)"
        )
        prompt = PromptLoader.load(
            "stage_roadmap_serial_refine",
            instruction=instruction,
            stage_number=number,
            total_stages=total_stages,
            long_mainline=long_mainline,
            current_stage_outline=stage_sections[number],
            reference_volume_number=volume["vol_idx"],
            reference_volume_title=volume["title"],
            reference_chapter_count=reference_chapter_count,
            reference_volume_outline=reference_volume_outline,
            previous_stage=previous_stage,
            current_stage=current_stage,
        )
        stage_raw = controlled_call(
            prompt, f"serial stage adjust {number}/{total_stages}", number - 1,
            f"Stage {number} adjust paused; click continue to regenerate the current stage",
        )
        if stage_raw is None:
            return stopped_result(completed_parts)
        payload = parse_json_response(stage_raw)
        stage = _normalize_stage_roadmap(
            _normalize_design_field(payload, "stage_roadmap_md", "")
        )
        numbers = [int(value) for value in STAGE_HEADING_RE.findall(stage)]
        if numbers != [number] or not _is_volume_style_stage(stage):
            raise RuntimeError(
                f"Stage {number} adjustment result has incomplete format or number; previously written stages were kept."
            )
        completed_parts.append(stage)
        # Keep unprocessed old stages in the file; replace them one by one only after a new result returns,
        # so pause/stop does not delete still-usable original content early.
        pending_original_parts = original_parts[number:]
        _write_file(
            stage_path,
            "\n\n".join(completed_parts + pending_original_parts),
        )
        report(
            "stage_refine_complete", number,
            f"Stage {number}/{total_stages} adjust complete",
        )

    return {
        "long_mainline": long_mainline,
        "stage_roadmap": "\n\n".join(completed_parts),
        "adjustment_note": (
            f"Per the instruction, serially processed from stage {start_stage} "
            f"{total_stages - start_stage + 1} stages."
            f"Handling: {'full regenerate' if mode == 'regenerate' else 'revise from current content'}."
            f"Route reason: {reason}"
        ),
        "start_stage": start_stage,
        "mode": mode,
    }


def _sync_later_stages_serial(ws, instruction, cancel_event=None):
    """If phase count is unchanged, redo the last stage; if it grew, keep existing stages and append serially."""
    design_state = _load_story_design_state(ws)
    if not design_state.get("pending_reference_stage_sync"):
        raise ValueError("There is no phase-outline change triggered by newly added deconstruction chapters; no incremental stage sync is needed.")
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md"))
    stage_outline = _read_file(_stage_outline_path(ws))
    stage_path = _story_design_path(ws, "stage_roadmap.md")
    stage_roadmap = _read_file(stage_path)
    stage_sections = _stage_outline_sections(stage_outline)
    reference_volumes = list_reference_volumes(ws.reference_outlines)
    if not long_mainline or not stage_roadmap or not stage_sections:
        raise RuntimeError("Finish the long mainline, phase outline, and existing stage design first.")
    if len(stage_sections) != len(reference_volumes):
        raise RuntimeError(
            f"Phase outline count does not match reference volumes: {len(stage_sections)} phases, "
            f"{len(reference_volumes)} reference volumes. Please regenerate the phase outline first."
        )

    completed_parts = _completed_stage_prefix(stage_roadmap, len(stage_sections))
    if not completed_parts:
        raise RuntimeError("The current stage roadmap has no recognizable contiguous stages; cannot run tail incremental sync.")
    # Phase count did not grow, so new reference content supplemented the last volume: redo only the last stage.
    adjust_last = len(completed_parts) == len(stage_sections)
    if adjust_last:
        completed_parts = completed_parts[:-1]
    next_stage = len(completed_parts) + 1

    _backup_design_files(ws, "stage_increment", {"stage_roadmap": stage_path})

    llm = _get_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")
    for number in range(next_stage, len(stage_sections) + 1):
        volume = reference_volumes[number - 1]
        reference_volume_outline = load_reference_volume_outline(
            ws.reference_outlines, volume["vol_idx"]
        ) or "(matching reference volume outline is missing)"
        reference_chapter_count = _reference_volume_chapter_count(
            volume, reference_volume_outline,
        )
        previous_stage = completed_parts[-1] if completed_parts else "(this is the first stage; no previous stage)"
        prompt = PromptLoader.load(
            "stage_roadmap_serial",
            stage_number=number,
            total_stages=len(stage_sections),
            long_mainline=long_mainline,
            current_stage_outline=stage_sections[number],
            reference_volume_number=volume["vol_idx"],
            reference_volume_title=volume["title"],
            reference_chapter_count=reference_chapter_count,
            reference_volume_outline=reference_volume_outline,
            previous_stage=previous_stage,
        )
        payload = parse_json_response(
            _call_design_llm(
                llm, prompt, f"sync appended stage {number}/{len(stage_sections)}",
                cancel_event=cancel_event,
            )
        )
        stage = _normalize_stage_roadmap(
            _normalize_design_field(payload, "stage_roadmap_md", "")
        )
        numbers = [int(value) for value in STAGE_HEADING_RE.findall(stage)]
        if numbers != [number] or not _is_volume_style_stage(stage):
            raise RuntimeError(
                f"Synced new stage {number} has incomplete format or number; previously generated stages were kept."
            )
        completed_parts.append(stage)
        _write_file(stage_path, "\n\n".join(completed_parts))
        mapped_paths = []
        for arc in list_reference_story_arcs(ws.reference_outlines, volume["vol_idx"]):
            try:
                mapped_paths.append(os.path.relpath(arc["path"], ws.reference_outlines))
            except ValueError:
                mapped_paths.append(arc["path"])
        _mark_arcs_used(ws, mapped_paths, [number])

    _mark_stage_design_synced(ws)
    return {
        "long_mainline": long_mainline,
        "stage_roadmap": "\n\n".join(completed_parts),
        "adjustment_note": (
            f"Phase count did not grow; only the last stage (stage {next_stage}) was regenerated."
            if adjust_last else
            f"Kept existing stages and serially filled later stages from stage {next_stage}."
        ),
    }


def extend_stage_design(ws, instruction, sync_updated_design=False, cancel_event=None):
    """Route 3: extend / add stages.

    Read only used=False reference segments as input, output only appended stage content,
    append it programmatically to the end of stage_roadmap, and do not rewrite existing stages.
    """
    print(">>> Extend the stage roadmap <<<")
    if sync_updated_design:
        return _sync_later_stages_serial(ws, instruction, cancel_event=cancel_event)
    rough = _rough_outline_with_stages(ws)
    worldview = _read_file(_worldview_path(ws)) or "(not generated: worldview)"
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md"))
    stage_roadmap = _read_file(_story_design_path(ws, "stage_roadmap.md"))
    if not long_mainline or not stage_roadmap:
        print("Error: finish stage-roadmap design before extending it.")
        return {}

    llm = _get_llm()
    if not llm:
        return {}

    unused_arcs = _unused_reference_arcs(ws)
    if unused_arcs:
        ref_parts = []
        for arc in unused_arcs:
            ref_parts.append(
                f"--- Segment ID: {arc['path']} | reference chapters {arc['start_ch']}-{arc['end_ch']} ---\n{arc['content']}"
            )
        reference_text = "\n\n".join(ref_parts)
        print(f"  -> Found {len(unused_arcs)} unused reference segments.")
    else:
        reference_text = "(no newly added reference segments)"

    next_stage = _next_stage_number(stage_roadmap)
    world_knowledge = _load_world_knowledge_optional(ws, "extend stages")
    prompt = PromptLoader.load(
        "stage_design_extend",
        instruction=instruction,
        rough_outline=rough,
        worldview=worldview,
        long_mainline=long_mainline,
        stage_roadmap=stage_roadmap,
        reference_arcs=reference_text,
        world_knowledge=world_knowledge or "(target world knowledge base not provided)",
        next_stage_number=next_stage,
    )
    payload = parse_json_response(
        _call_design_llm(llm, prompt, "extend stages", cancel_event=cancel_event)
    )
    append_content = _normalize_design_field(payload, "stage_roadmap_append", "")
    if not append_content:
        print("Error: the model did not return appended stage content.")
        return {}
    generated_numbers = [int(item) for item in STAGE_HEADING_RE.findall(append_content)]
    if not generated_numbers or generated_numbers[0] != next_stage or any(
        number < next_stage for number in generated_numbers
    ):
        raise RuntimeError(
            f"Appended stage numbers are invalid: must start at stage {next_stage} and must not include existing stages."
        )
    append_content = _normalize_stage_roadmap(append_content)
    note = str(payload.get("adjustment_note") or "").strip() or "Later stages appended."
    long_mainline_append = ""
    referenced_paths = payload.get("referenced_arc_paths") if isinstance(payload, dict) else []

    # Programmatic append: existing content stays, only append at the end
    stage_path = _story_design_path(ws, "stage_roadmap.md")
    _append_story_design_section(stage_path, f"Extend stages (from stage {next_stage})", append_content)
    print(f"  -> New stages appended: {stage_path}")

    # Mark segments consumed this round as used
    allowed_paths = {arc["path"] for arc in unused_arcs}
    consumed = [str(path) for path in (referenced_paths or []) if str(path) in allowed_paths]
    if consumed:
        _mark_arcs_used(ws, consumed, generated_numbers)
        print(f"  -> Marked {len(consumed)} reference segments as used.")
    if long_mainline_append:
        _append_story_design_section(
            _story_design_path(ws, "long_mainline.md"),
            "Long-mainline supplement based on the current book design",
            long_mainline_append,
        )
    return {
        "stage_roadmap_append": append_content,
        "long_mainline": _read_file(_story_design_path(ws, "long_mainline.md")),
        "stage_roadmap": _read_file(stage_path),
        "adjustment_note": note,
    }


def _refine_design(ws, scope, instruction, compact_summary, prompt_folder, fields, prompt_field_map, output_keys, extra_vars=None):
    rough = _rough_outline_with_stages(ws)
    worldview = _read_file(_worldview_path(ws)) or "(not generated: worldview)"
    llm = _get_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")
    prompt_vars = {
        "creative_direction": _read_file(ws.creative_direction) or "(not provided)",
        "reference_outline": load_reference_novel_outline(ws.reference_outlines) or "(reference novel outline not provided)",
        "compact_summary": compact_summary or "(none)",
        "rough_outline": rough,
        "worldview": worldview,
    }
    for rel, path in fields.items():
        prompt_vars[prompt_field_map[rel]] = _read_file(path) or f"(not generated: {rel})"
    if extra_vars:
        prompt_vars.update(extra_vars)
    prompt = PromptLoader.load(prompt_folder, **prompt_vars)
    prompt_with_instruction = prompt + "\n\n[Instruction this round]\n" + instruction
    payload = parse_json_response(_call_design_llm(llm, prompt_with_instruction, f"{scope} refine"))
    if "stage_outline_md" in output_keys:
        candidate_stage = _normalize_design_field(payload, "stage_outline_md", "")
        expected_stages = _design_structure_guidance(ws)["reference_volume_count"]
        candidate_count, _ = _design_structure_counts(candidate_stage, "")
        if expected_stages > 0 and candidate_count != expected_stages:
            raise RuntimeError(
                f"Phase-outline adjustment failed validation: reference novel has {expected_stages} volumes, "
                f"the model returned {candidate_count} phases; none of the three files were written. Please retry."
            )
    result = {}
    _backup_design_files(ws, scope, fields)
    for out_key, rel in output_keys.items():
        content = _normalize_design_field(payload, out_key, "")
        if rel == "rough_outline" and content:
            content = _remove_stage_outline_section(content)
        if content:
            _write_file(fields[rel], content)
            result[rel] = content
        else:
            result[rel] = _read_file(fields[rel])
    result["adjustment_note"] = str(payload.get("adjustment_note") or "").strip() if isinstance(payload, dict) else ""
    if isinstance(payload, dict) and isinstance(payload.get("referenced_arc_paths"), list):
        result["referenced_arc_paths"] = payload["referenced_arc_paths"]
    return result


def _call_design_llm(llm, prompt, label, cancel_event=None):
    print(f"[LLMProvider] Calling model ({label})...")
    if cancel_event is not None and hasattr(llm, "generate_cancelable"):
        generated = llm.generate_cancelable(
            prompt, cancel_event, temperature=0.3, is_json=True,
        )
    else:
        generated = llm.generate(prompt, temperature=0.3, is_json=True)
    raw = normalize_text(generated)
    if not raw:
        raise RuntimeError(f"{label} did not receive model output.")
    return raw


def _normalize_design_field(payload, key, fallback_title):
    if not isinstance(payload, dict):
        return ""
    text = str(payload.get(key) or "").strip()
    if not text:
        return (
            (fallback_title + "\n\n(Model did not return " + key + ", please retry or fill in manually.)")
            if fallback_title else ""
        )
    return text


def _is_real_design_field(text):
    """True when a design field is real content, not empty or a placeholder."""
    if not text or not str(text).strip():
        return False
    t = str(text).strip()
    if "模型未返回" in t and "请重试或人工补充" in t:
        return False
    if "Model did not return" in t and "please retry or fill in manually" in t:
        return False
    return True


def gen_story_design(ws, force=False, creative_direction=None, direction_file=None):
    """Generate long-form web-novel gameplay, long mainline, stage, and character-arc design assets."""
    llm = _get_llm()
    if not llm:
        return

    direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, direction, "rebuild")
    world_knowledge = _load_world_knowledge_optional(ws, "story gameplay / stage / character-arc design")

    _gen_core_gameplay(ws, llm, direction, world_knowledge, force=force)
    _gen_long_mainline(ws, llm, direction, world_knowledge, force=force)
    _gen_stage_roadmap(ws, llm, direction, world_knowledge, force=force)
    _gen_character_arcs(ws, llm, direction, world_knowledge, force=force)
    if force or not _load_story_design_state(ws):
        _record_story_design_reference_snapshot(ws, reset_extensions=force)


def gen_novel_outline(ws, force=False, creative_direction=None, direction_file=None, preserved_content=None):
    """Generate core gameplay, book long mainline, stage roadmap, and character growth lines."""
    print(">>> Generate core gameplay and book stage design <<<")

    direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, direction, "initial")
    if direction:
        print(f"  -> Creative direction loaded ({len(direction)} chars)")
    else:
        print("  -> No creative direction provided; the LLM will create freely.")
        print("     Provide direction with --direction or a creative_direction.md file.")

    llm = _get_llm()
    if not llm:
        return

    world_knowledge = _load_world_knowledge_optional(ws, "core gameplay and stage design")
    _gen_core_gameplay(ws, llm, direction, world_knowledge, force=force)
    _gen_long_mainline(ws, llm, direction, world_knowledge, force=force)
    _gen_stage_roadmap(ws, llm, direction, world_knowledge, force=force)
    _gen_character_arcs(ws, llm, direction, world_knowledge, force=force)

    # Recommend title and synopsis
    print()
    gen_novel_name_synopsis(ws, force=True)
    if force or not _load_story_design_state(ws):
        _record_story_design_reference_snapshot(ws, reset_extensions=force)

    print(f"\n  -> Review and edit core gameplay, long mainline, stage roadmap, and character arcs before generating story-arc units.")


def import_target_world_sources(ws, paths, force=False):
    """Import target-genre sources into the workspace."""
    result = import_world_sources(ws, paths, force=force)
    for path in result["imported"]:
        print(f"  Imported: {path}")
    for path in result["skipped"]:
        print(f"  Already exists, skipping: {path}")
    for path in result["unsupported"]:
        print(f"  Unsupported file type, skipping: {path}")
    for path in result["missing"]:
        print(f"  File does not exist, skipping: {path}")
    print(f"  -> manifest：{result['manifest']}")
    return result


def build_target_world_knowledge(ws, force=False, chunk_size=36000, chapter_batch_size=20,
                                 max_workers=4, primary_source=None, merge_only=False):
    """Structure imported sources into a target-world knowledge base."""
    llm = _get_lite_llm()
    if not llm:
        return None
    print(">>> Build target-world knowledge base <<<")
    return build_world_knowledge(
        ws,
        llm,
        force=force,
        chunk_size=chunk_size,
        chapter_batch_size=chapter_batch_size,
        max_workers=max_workers,
        primary_source=primary_source,
        merge_only=merge_only,
    )


def _extract_reference_name_synopsis(ws):
    """Extract the reference novel's title and synopsis from sample_novel.txt.

    Prefer marked format: 【书名】XXX / 【简介】XXX (may be multiple lines).
    Without markers, heuristic fallback: first line is the title, continuous text before the chapter heading is the synopsis.
    """
    if not os.path.exists(ws.reference_sample):
        return "(unknown)", "(not provided)"

    with open(ws.reference_sample, "r", encoding="utf-8") as f:
        content = f.read()

    # Prefer marked format
    name_match = re.search(r'^【书名】(.+)', content, re.MULTILINE)
    synopsis_match = re.search(r'^【简介】(.+?)(?=^【|^第[一二三四五六七八九十百千零\d]+[章回节])', content, re.MULTILINE | re.DOTALL)

    if name_match:
        name = name_match.group(1).strip()
        synopsis = synopsis_match.group(1).strip() if synopsis_match else "(not provided)"
        return name, synopsis

    # Fallback: heuristic extract
    lines = content.split('\n')
    name = ""
    synopsis_lines = []
    in_synopsis = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_synopsis and synopsis_lines:
                break
            continue

        if not name:
            name = stripped.strip("《》")
            continue

        if re.match(r'^第[一二三四五六七八九十百千零\d]+[章回节]', stripped):
            break

        in_synopsis = True
        synopsis_lines.append(stripped)

    synopsis = "\n".join(synopsis_lines) if synopsis_lines else "(synopsis not extracted)"
    return name, synopsis


def gen_novel_name_synopsis(ws, force=False, cancel_event=None):
    """Recommend title and synopsis from the rough outline and long mainline only."""
    rough_outline = _read_file(_rough_outline_path(ws))
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md"))
    # novel-outline (legacy combined command) never writes rough_outline.md;
    # core_gameplay.md is the equivalent brief for title/synopsis.
    if not rough_outline:
        rough_outline = _read_file(_story_design_path(ws, "core_gameplay.md"))
    if not rough_outline or not long_mainline:
        raise RuntimeError("Generate the rough outline and long mainline before generating title and synopsis.")

    output_path = os.path.join(ws.file_system, "novel_name_synopsis.md")
    existing = _read_file(output_path)
    if existing and not force:
        print(f"Title and synopsis recommendation already exists: {output_path}")
        print("Use --force to overwrite.")
        return

    llm = _get_llm()
    if not llm:
        return

    return run_step(
        llm=llm,
        folder="novel_name_synopsis",
        label="title and synopsis",
        header=">>> Recommend title and synopsis <<<",
        write_guard=True,
        output_path=output_path,
        prompt_vars=dict(
            rough_outline=rough_outline,
            long_mainline=long_mainline,
        ),
        cancel_event=cancel_event,
    )


def _stage_insert_backup_path(ws):
    return os.path.join(ws.file_system, "adaptation", "stage_roadmap_before_insert.md")


def insert_stage(ws, creative_direction=None, direction_file=None, after_stage=None, before_stage=None):
    """Design a new stage from new inspiration and insert it into the book stage roadmap."""
    stage_direction = _load_creative_direction(ws, creative_direction, direction_file)
    record_creative_direction(ws, stage_direction, "stage_insert")
    if not stage_direction:
        print("Error: provide new-stage inspiration with --direction or --direction-file.")
        return

    llm = _get_llm()
    if not llm:
        return

    stage_roadmap_path = _story_design_path(ws, "stage_roadmap.md")
    stage_roadmap = _read_file(stage_roadmap_path)
    if not stage_roadmap:
        print("Error: stage roadmap not found. Run novel-outline or story-design first.")
        return

    assets = _load_story_design_assets(ws)
    world_knowledge = _load_world_knowledge_optional(ws, "insert new stage")
    if after_stage is not None:
        insert_hint = f"Prefer inserting after stage {after_stage}, then renumber all stages."
    elif before_stage is not None:
        insert_hint = f"Prefer inserting before stage {before_stage}, then renumber all stages."
    else:
        insert_hint = "Judge the best insert position from core gameplay, long mainline, and before/after continuity."

    print(">>> Insert a new stage from inspiration <<<")
    prompt = PromptLoader.load(
        "stage_insert_design",
        stage_direction=stage_direction,
        insert_hint=insert_hint,
        core_gameplay=assets["core_gameplay"],
        long_mainline=assets["long_mainline"],
        stage_roadmap=stage_roadmap,
        character_arcs=assets["character_arcs"],
        world_knowledge=world_knowledge or "(target world knowledge base not provided)",
    )
    result = _normalize_stage_roadmap(normalize_text(llm.generate(prompt)))
    backup_path = _stage_insert_backup_path(ws)
    _write_file(backup_path, stage_roadmap)
    _write_file(stage_roadmap_path, result)
    print(f"  -> Original stage roadmap backed up: {backup_path}")
    print(f"  -> New stage roadmap saved: {stage_roadmap_path}")


def _map_to_reference_volumes_sequential(ws, vol_idx, ref_volumes):
    """Sequential mapping: new-novel volume N uses reference-novel volume N."""
    if not ref_volumes:
        return ""

    idx = min(vol_idx - 1, len(ref_volumes) - 1)
    vol = ref_volumes[idx]
    outline = load_reference_volume_outline(ws.reference_outlines, vol["vol_idx"])
    return f"(reference original volume {vol['vol_idx']})\n{outline}" if outline else "(no matching reference volume outline)"


def _gen_volume_worldview(ws, vol_idx, llm, force, novel_outline, new_novel_worldview):
    """Generate this volume's worldview from the new outline, new-book worldview, and this volume outline."""
    new_wv_dir = os.path.join(ws.file_system, "new_worldviews")
    vol_wv_path = os.path.join(new_wv_dir, f"vol_{vol_idx:02d}_worldview.md")

    existing_wv = _read_file(vol_wv_path)
    if existing_wv and not force:
        print(f"  Volume {vol_idx} worldview already exists; skipping.")
        return existing_wv

    # Read this volume's new outline (from per-volume files)
    vol_outline_dir = os.path.join(ws.file_system, "new_volume_outlines")
    vol_outline_file = os.path.join(vol_outline_dir, f"vol_{vol_idx:02d}_outline.md")
    current_vol_text = _read_file(vol_outline_file) or ""
    if not current_vol_text:
        print(f"  Warning: this volume outline file was not found: {vol_outline_file}")
        return
    # Strip the final-volume marker
    current_vol_text = re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', current_vol_text).strip()

    # Read previous-volume worldview (continuity reference)
    prev_wv = ""
    if vol_idx > 1:
        prev_path = os.path.join(new_wv_dir, f"vol_{vol_idx - 1:02d}_worldview.md")
        prev_wv = _read_file(prev_path) or ""

    # Old worldview (reference when force-overwriting)
    old_wv = existing_wv or ""

    os.makedirs(new_wv_dir, exist_ok=True)
    print(f"  -> Generating volume {vol_idx} worldview...")

    rewrite_map = load_rewrite_map(ws, vol_idx)

    prompt = (
        "You are a professional novel-worldview designer. From the new novel's full worldview and this "
        "volume's outline, generate a detailed worldview for the given volume.\n\n"
        "[New-novel worldview]\n" + new_novel_worldview + "\n\n"
        "[This volume outline]\n" + current_vol_text + "\n\n"
        "[Rewrite map] (how reference elements translate; the new novel's setting wins)\n" + rewrite_map + "\n\n"
        + (f"[Previous volume worldview] (keep worldview evolution consistent)\n{prev_wv}\n\n" if prev_wv else "")
        + (f"[Old worldview for this volume] (upgrade from existing settings)\n{old_wv}\n\n" if old_wv else "")
        + "[Requirements]\n"
        "1. Use the full-book worldview as the base; refine to the factions, people, places, and items this volume actually involves.\n"
        "2. Show worldview evolution in this volume: new factions, character growth, newly unlocked regions, and so on.\n"
        "3. Stay continuous with the previous volume's worldview; do not introduce contradictory settings.\n"
        "4. Every aspect must list concrete names; do not stay generic.\n"
        "5. Do not freeze the reference novel's old-world events, people, timeline, or religious causality as new-worldview facts.\n"
        "6. If this volume outline's 'matching reference novel' notes contain old names, treat them as mapping notes only; do not write them into the new worldview body.\n"
        "7. Output plain text. Do not use Markdown format markers. Use # for headings. Separate paragraphs with blank lines.\n\n"
        "Output in this structure:\n"
        "I. Factions and characters\n"
        "II. Cultivation system\n"
        "III. Special items\n"
        "IV. Geography and scenes\n"
        "V. Races and peoples\n"
        "VI. Core rules and taboos\n"
        "VII. Protagonist golden-finger progress"
    )
    result = normalize_text(llm.generate(prompt))

    _write_file(vol_wv_path, result)
    print(f"  -> Volume {vol_idx} worldview saved: {vol_wv_path}")
    return result


def _gen_volume_stage_plan(ws, vol_idx, llm, force, vol_outline, vol_worldview,
                           novel_outline, new_novel_worldview):
    """Generate the stage / instance plan for the current volume."""
    output_path = _volume_stage_plan_path(ws, vol_idx)
    existing = _read_file(output_path)
    if existing and not force:
        print(f"  Volume {vol_idx} stage plan already exists; skipping.")
        return existing

    assets = _load_story_design_assets(ws)
    rewrite_map = load_rewrite_map(ws, vol_idx)

    return run_step(
        llm=llm,
        folder="volume_stage_plan",
        header=f"  -> Generating volume {vol_idx} stage plan...",
        save=f"  -> Volume {vol_idx} stage plan saved: {output_path}",
        output_path=output_path,
        prompt_vars=dict(
            volume_index=vol_idx,
            core_gameplay=assets["core_gameplay"],
            stage_roadmap=assets["stage_roadmap"],
            character_arcs=assets["character_arcs"],
            novel_outline=novel_outline or "(not generated: new-novel outline)",
            new_novel_worldview=new_novel_worldview or "(not generated: new-novel worldview)",
            volume_outline=vol_outline or "(not generated: this volume outline)",
            volume_worldview=vol_worldview or "(not generated: this volume worldview)",
            rewrite_map=rewrite_map,
        ),
    )


def _gen_single_volume(ws, vol_idx, ref_volumes, force, creative_direction, llm, preserved_content=None):
    """Generate one volume outline, then that volume's worldview. Return True if this is the final volume."""
    vol_dir = os.path.join(ws.file_system, "new_volume_outlines")
    vol_file = os.path.join(vol_dir, f"vol_{vol_idx:02d}_outline.md")
    os.makedirs(vol_dir, exist_ok=True)

    existing_this = _read_file(vol_file)
    if existing_this and not force:
        print(f"  -> Volume {vol_idx} outline already exists; skipping. (use --force to overwrite)")
        vol_outline_clean = re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', existing_this).strip()
        existing_novel_outline = _read_file(os.path.join(ws.file_system, "novel_outline.md")) or ""
        new_novel_worldview = _read_file(os.path.join(ws.file_system, "new_novel_worldview.md")) or "(no new-novel worldview; run novel-outline first)"
        vol_worldview = _gen_volume_worldview(ws, vol_idx, llm, force, existing_novel_outline, new_novel_worldview)
        _gen_volume_stage_plan(
            ws,
            vol_idx,
            llm,
            force,
            vol_outline_clean,
            vol_worldview,
            existing_novel_outline,
            new_novel_worldview,
        )
        if existing_this.rstrip().endswith("[FINISHED]"):
            return True
        return False

    print(f"  -> Generating volume {vol_idx} outline...")

    direction = _load_creative_direction(ws, creative_direction)

    novel_outline = _read_file(os.path.join(ws.file_system, "novel_outline.md")) or ""

    # Read previous volume outline (stored per volume)
    prev_vol_file = os.path.join(vol_dir, f"vol_{vol_idx - 1:02d}_outline.md")
    previous_volumes = _read_file(prev_vol_file) if vol_idx > 1 and os.path.exists(prev_vol_file) else ""
    if not previous_volumes:
        previous_volumes = "(no previous volume; this is volume 1)"

    # Use the new-novel full worldview
    new_novel_worldview = _read_file(os.path.join(ws.file_system, "new_novel_worldview.md")) or "(no new-novel worldview; run novel-outline first)"

    ref_vol_outline = _map_to_reference_volumes_sequential(ws, vol_idx, ref_volumes)
    rewrite_map = load_rewrite_map(ws, vol_idx)

    preserved_section = ""
    if preserved_content:
        preserved_section = f"[Volume-outline content worth keeping from existing finals]\nThe following comes from analysis of finalized chapters; keep its continuity when regenerating the volume outline:\n{preserved_content}"

    prompt = PromptLoader.load(
        "adaptive_volume_outline",
        novel_outline=novel_outline,
        reference_volume_outline=ref_vol_outline or "(no reference volume outline)",
        new_novel_worldview=new_novel_worldview,
        rewrite_map=rewrite_map,
        inspirations="(no inspiration library)",
        volume_index=vol_idx,
        creative_direction=direction or "(no extra direction)",
        previous_volumes=previous_volumes,
        outline_rules=_load_outline_rules(ws),
        preserved_content=preserved_section,
        audit_feedback="",
    )
    result = normalize_text(llm.generate(prompt))

    if not result:
        return False

    is_finished = result.rstrip().endswith("[FINISHED]")
    result_clean = re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', result).strip()

    # Write per-volume files (keep [FINISHED] so reruns can detect it)
    marker = "\n[FINISHED]" if is_finished else "\n[CONTINUE]"
    _write_file(vol_file, result_clean + marker + "\n")

    if is_finished:
        print(f"  -> Volume {vol_idx} outline saved (final volume; generation complete).")
    else:
        print(f"  -> Volume {vol_idx} outline saved; continuing with the next volume.")

    # Step 2: generate this volume's worldview
    vol_worldview = _gen_volume_worldview(ws, vol_idx, llm, force, novel_outline, new_novel_worldview)
    _gen_volume_stage_plan(
        ws,
        vol_idx,
        llm,
        force,
        result_clean,
        vol_worldview,
        novel_outline,
        new_novel_worldview,
    )

    return is_finished


def _write_aggregate_volume_outline(ws):
    """Combine per-volume files into volume_outline.md (compatible with old callers)."""
    vol_dir = os.path.join(ws.file_system, "new_volume_outlines")
    if not os.path.isdir(vol_dir):
        return
    vol_files = sorted(f for f in os.listdir(vol_dir) if re.match(r'^vol_\d+_outline\.md$', f))
    if not vol_files:
        return

    parts = []
    for vf in vol_files:
        content = _read_file(os.path.join(vol_dir, vf))
        if content:
            # Strip final/continue markers (used only for per-volume rerun detection)
            clean = re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', content).strip()
            if clean:
                parts.append(clean)
            parts.append(content.strip())

    output_path = os.path.join(ws.file_system, "volume_outline.md")
    _write_file(output_path, "\n\n---\n\n".join(parts))
    print(f"\n  -> Combined volume outline written: {output_path}")


def gen_volume_outline(ws, volume=None, force=False, creative_direction=None, preserved_content=None):
    """Step 2: generate volume outlines one by one; the LLM decides whether this is the final volume."""
    MAX_VOLUMES = 20

    novel_outline = _read_file(os.path.join(ws.file_system, "novel_outline.md"))
    if not novel_outline:
        print("Error: new-novel outline not found. Run the novel-outline subcommand first.")
        return

    outlines_dir = ws.reference_outlines
    ref_volumes = list_reference_volumes(outlines_dir)
    if not ref_volumes:
        print("Error: reference-novel volume data not found. Run outline_builder.py first.")
        return

    print(f"  -> Reference novel has {len(ref_volumes)} volumes; the new novel volume count will be judged volume by volume by the LLM.")

    llm = _get_llm()
    if not llm:
        return
    _ensure_rewrite_map(ws, llm)

    if volume is not None:
        if volume < 1 or volume > MAX_VOLUMES:
            print(f"Error: volume number {volume} is out of range (1-{MAX_VOLUMES}).")
            return
        print(f">>> Generating volume {volume} outline by imitation <<<")
        _gen_single_volume(ws, volume, ref_volumes, force, creative_direction, llm, preserved_content=preserved_content)
    else:
        # Detect existing volume count from per-volume files (supports resume)
        vol_dir = os.path.join(ws.file_system, "new_volume_outlines")
        start_vol = 1
        if os.path.isdir(vol_dir) and not force:
            vol_files = sorted(f for f in os.listdir(vol_dir) if re.match(r'^vol_\d+_outline\.md$', f))
            if vol_files:
                # Infer the next volume from the last file
                last_match = re.match(r'^vol_(\d+)_outline\.md$', vol_files[-1])
                if last_match:
                    last_vol = int(last_match.group(1))
                    # Check the final-volume marker
                    last_content = _read_file(os.path.join(vol_dir, vol_files[-1]))
                    if last_content and last_content.rstrip().endswith("[FINISHED]"):
                        print(f">>> All volume outlines generated ({last_vol} volumes); nothing left to do. Use --force to overwrite.<<<")
                        return
                    start_vol = last_vol + 1
                    print(f">>> Resume: volumes 1-{last_vol} already exist; continuing from volume {start_vol} <<<")
                else:
                    print(f">>> Generating all volume outlines by imitation (max {MAX_VOLUMES} volumes; LLM decides the final volume) <<<")
            else:
                print(f">>> Generating all volume outlines by imitation (max {MAX_VOLUMES} volumes; LLM decides the final volume) <<<")
        else:
            print(f">>> Generating all volume outlines by imitation (max {MAX_VOLUMES} volumes; LLM decides the final volume) <<<")

        for vol_idx in range(start_vol, MAX_VOLUMES + 1):
            is_finished = _gen_single_volume(ws, vol_idx, ref_volumes, force, creative_direction, llm, preserved_content=preserved_content)
            if is_finished:
                break

    # Combine into volume_outline.md (compatible with old callers)
    _write_aggregate_volume_outline(ws)


def _novel_outlines_dir(ws):
    """Return the new-novel batch-summary directory."""
    return os.path.join(ws.file_system, "outlines")


def _novel_story_arcs_dir(ws):
    """Return the new novel's story-arc unit directory."""
    return os.path.join(ws.file_system, "story_arcs")


def _volume_story_arc_dir(ws, volume):
    return os.path.join(_novel_story_arcs_dir(ws), f"vol_{volume:02d}")


def _story_arc_file_name(arc_idx, start_ch, end_ch):
    return f"arc_{arc_idx:03d}_ch{start_ch:03d}_{end_ch:03d}.md"


def _story_arc_path(ws, volume, arc_idx, start_ch, end_ch):
    return os.path.join(
        _volume_story_arc_dir(ws, volume),
        _story_arc_file_name(arc_idx, start_ch, end_ch),
    )


def _extract_stage_from_roadmap(stage_roadmap, stage_idx):
    stage_roadmap = _normalize_stage_roadmap(stage_roadmap)
    if not stage_roadmap:
        return ""
    headings = list(STAGE_HEADING_RE.finditer(stage_roadmap))
    for index, heading in enumerate(headings):
        if int(heading.group(1)) != int(stage_idx):
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(stage_roadmap)
        return stage_roadmap[heading.start():end].strip()
    return ""


def _infer_stage_chapter_count(stage_text):
    if not stage_text:
        return 0
    range_patterns = [
        r'预计章节数[：:]\s*(\d+)\s*[-—–~至到to]+\s*(\d+)',
        r'Planned chapters?[：:]\s*(\d+)\s*[-—–~to]+\s*(\d+)',
        r'章节数[：:]\s*(\d+)\s*[-—–~至到to]+\s*(\d+)',
        r'预计\s*(\d+)\s*[-—–~至到to]+\s*(\d+)\s*章',
    ]
    for pattern in range_patterns:
        m = re.search(pattern, stage_text)
        if m:
            return max(int(m.group(1)), int(m.group(2)))

    patterns = [
        r'预计章节数[：:]\s*(\d+)',
        r'Planned chapters?[：:]\s*(\d+)',
        r'章节数[：:]\s*(\d+)',
        r'预计\s*(\d+)\s*章',
        r'共\s*(\d+)\s*章',
    ]
    for pattern in patterns:
        m = re.search(pattern, stage_text)
        if m:
            return max(1, int(m.group(1)))

    range_match = re.search(r'第\s*(\d+)\s*[-—~至到]\s*(\d+)\s*章', stage_text)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return max(1, end - start + 1)
    return 0


def _load_stage_context(ws, stage_idx):
    stage_roadmap = _read_file(_story_design_path(ws, "stage_roadmap.md"))
    stage_text = _extract_stage_from_roadmap(stage_roadmap, stage_idx)
    if not stage_text:
        return None
    total_chapters = _infer_stage_chapter_count(stage_text)
    if total_chapters <= 0:
        print(f"Error: stage {stage_idx} is missing Planned chapters; cannot generate story-arc units.")
        print("Add Planned chapters for this stage in stage_roadmap.md, or rerun novel-outline/story-design.")
        return None
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md")) or "(not generated: long mainline)"
    stage_worldview = (
        "[Book long mainline]\n" + long_mainline + "\n\n"
        "[Current stage rules and boundaries]\n" + stage_text
    )
    return stage_text, stage_worldview, total_chapters


def _list_novel_story_arcs(ws, volume):
    arc_dir = _volume_story_arc_dir(ws, volume)
    if not os.path.isdir(arc_dir):
        return []
    items = []
    for fname in sorted(os.listdir(arc_dir)):
        m = STORY_ARC_FILE_RE.match(fname)
        if not m:
            continue
        path = os.path.join(arc_dir, fname)
        content = _read_file(path)
        if not content:
            continue
        items.append({
            "idx": int(m.group(1)),
            "start_ch": int(m.group(2)),
            "end_ch": int(m.group(3)),
            "file": fname,
            "path": path,
            "content": content,
        })
    return items


def _write_story_arc_index(ws, volume, arc_items):
    index_path = os.path.join(_volume_story_arc_dir(ws, volume), "arcs_index.json")
    lines = ["["]
    for idx, item in enumerate(arc_items):
        comma = "," if idx < len(arc_items) - 1 else ""
        lines.append(
            "  {"
            f"\"id\": {item['idx']}, "
            f"\"start_ch\": {item['start_ch']}, "
            f"\"end_ch\": {item['end_ch']}, "
            f"\"file\": \"{item['file']}\""
            f"}}{comma}"
        )
    lines.append("]")
    _write_file(index_path, "\n".join(lines))


def _clear_story_arc_files(ws, volume):
    arc_dir = _volume_story_arc_dir(ws, volume)
    if not os.path.isdir(arc_dir):
        return
    for fname in os.listdir(arc_dir):
        if STORY_ARC_FILE_RE.match(fname) or fname == "arcs_index.json":
            os.remove(os.path.join(arc_dir, fname))


def _target_story_arc_count(total_chapters):
    return max(1, (total_chapters + STORY_ARC_TARGET_CHAPTERS - 1) // STORY_ARC_TARGET_CHAPTERS)


def _select_reference_arc_groups(reference_arcs, target_count):
    groups = []
    for idx in range(target_count):
        if idx < len(reference_arcs):
            groups.append([reference_arcs[idx]])
        else:
            groups.append([])
    return groups


def _allocate_story_arc_lengths(total_chapters, target_count):
    target_count = max(1, target_count)
    base = total_chapters // target_count
    remainder = total_chapters % target_count
    return [
        max(1, base + (1 if idx < remainder else 0))
        for idx in range(target_count)
    ]


def _reference_story_arc_average_chars(ws, stage_number=None):
    """Return mean character count of matching reference-volume story segments; if no stage is given, count all."""
    lengths = []
    volumes = list_reference_volumes(ws.reference_outlines)
    if stage_number is not None:
        mapped = _reference_volume_for_stage(ws, stage_number)
        volumes = [mapped] if mapped else []
    for volume in volumes:
        for arc in list_reference_story_arcs(ws.reference_outlines, volume["vol_idx"]):
            content = arc.get("content", "")
            # "Character count" is visible chars after stripping whitespace, so Markdown layout does not inflate it.
            char_count = len(re.sub(r"\s+", "", content))
            if char_count:
                lengths.append(char_count)
    if not lengths:
        return 1000
    return max(300, min(STORY_ARC_TARGET_CHARS_MAX, round(sum(lengths) / len(lengths))))


def _reference_volume_for_stage(ws, stage_number):
    """Phase N maps one-to-one to reference volume N in order."""
    volumes = list_reference_volumes(ws.reference_outlines)
    if 1 <= int(stage_number) <= len(volumes):
        return volumes[int(stage_number) - 1]
    return None


def _reference_volume_story_arcs_summary(ws, stage_number):
    """Matching-volume arc index (ids and chapter ranges only, no bodies)."""
    volume = _reference_volume_for_stage(ws, stage_number)
    if not volume:
        return "(no reference-volume story segments found for the current stage)"
    arcs = list_reference_story_arcs(ws.reference_outlines, volume["vol_idx"])
    if not arcs:
        return f"(reference volume {volume['vol_idx']} has no usable story segments)"
    return "\n".join(
        f"[Reference story-arc {arc['idx']}: chapters {arc['start_ch']}-{arc['end_ch']}]"
        for arc in arcs
    )


def _story_arc_plans_for_volume(ws, volume, total_chapters):
    mapped = _reference_volume_for_stage(ws, volume)
    reference_arcs = (
        list_reference_story_arcs(ws.reference_outlines, mapped["vol_idx"])
        if mapped else []
    )
    if reference_arcs:
        return _plan_story_arcs_from_reference(reference_arcs, total_chapters)
    return _plan_story_arcs(total_chapters)


def _story_arc_prompt_context(generation_context, plan):
    ctx = dict(generation_context)
    sample = str(plan.get("reference_story_arc") or "").strip()
    if sample:
        ctx["reference_story_arcs"] = sample
    return ctx


def _simple_story_arc_context(ws, stage_number):
    """The only four content sources story-arc generation is allowed to use."""
    long_mainline = _read_file(_story_design_path(ws, "long_mainline.md")) or "(not generated: long mainline)"
    stage_roadmap = _read_file(_story_design_path(ws, "stage_roadmap.md")) or ""
    current_stage = _extract_stage_from_roadmap(stage_roadmap, stage_number) or "(current stage not found)"
    previous_stage = (
        _extract_stage_from_roadmap(stage_roadmap, stage_number - 1)
        if int(stage_number) > 1 else ""
    )
    return {
        "long_mainline": long_mainline,
        "previous_stage": previous_stage or "(this is the first stage; no previous stage)",
        "current_stage": current_stage,
        "reference_story_arcs": _reference_volume_story_arcs_summary(ws, stage_number),
    }


def _visible_char_count(text):
    return len(re.sub(r"\s+", "", text or ""))


def _generate_with_cancel(llm, prompt, cancel_event=None, temperature=0.7):
    if cancel_event is not None and hasattr(llm, "generate_cancelable"):
        return llm.generate_cancelable(prompt, cancel_event, temperature=temperature)
    return llm.generate(prompt, temperature=temperature)


def _compact_story_arc_result(llm, result, arc_idx, start_ch, end_ch, target_char_count,
                              cancel_event=None):
    """Compress obviously overlong story-arc results without changing structure, so verbosity does not keep growing."""
    max_chars = round(target_char_count * 1.25)
    if _visible_char_count(result) <= max_chars:
        return result
    prompt = PromptLoader.load(
        "story_arc_compact",
        arc_index=arc_idx,
        start_chapter=start_ch,
        end_chapter=end_ch,
        target_char_count=target_char_count,
        max_char_count=max_chars,
        original_story_arc=result,
    )
    compacted = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2))
    return compacted or result


def _format_reference_arc_group(group):
    if not group:
        return ""
    parts = []
    for arc in group:
        source_label = "reference story-arc unit" if arc.get("source_type") == "story_arc" else "legacy reference batch"
        parts.append(
            f"[{source_label}{arc['idx']}: chapters {arc['start_ch']}-{arc['end_ch']}]\n"
            f"{arc.get('content', '')}"
        )
    return "\n\n---\n\n".join(parts)


def _plan_story_arcs(total_chapters):
    """Plan story-arc chapter ranges from total chapter count, without depending on the reference novel."""
    target_count = _target_story_arc_count(total_chapters)
    lengths = _allocate_story_arc_lengths(total_chapters, target_count)
    plans = []
    start_ch = 1
    for idx, length in enumerate(lengths, 1):
        end_ch = min(total_chapters, start_ch + length - 1)
        plans.append({"idx": idx, "start_ch": start_ch, "end_ch": end_ch})
        start_ch = end_ch + 1
    if plans and plans[-1]["end_ch"] < total_chapters:
        plans[-1]["end_ch"] = total_chapters
    return plans


def story_arc_resume_status(ws, volume):
    """From the stage plan and on-disk files, decide whether story-arc generation can resume from a breakpoint."""
    context = _load_volume_outline_context(ws, volume)
    if not context:
        return {"can_resume": False, "completed": 0, "total": 0}
    _, _, total_chapters = context
    plans = _story_arc_plans_for_volume(ws, volume, total_chapters)
    completed_files = {
        (item["idx"], item["start_ch"], item["end_ch"])
        for item in _list_novel_story_arcs(ws, volume)
    }
    completed = sum((plan["idx"], plan["start_ch"], plan["end_ch"]) in completed_files for plan in plans)
    first_missing = next(
        (
            plan["idx"] for plan in plans
            if (plan["idx"], plan["start_ch"], plan["end_ch"]) not in completed_files
        ),
        None,
    )
    return {
        "can_resume": 0 < completed < len(plans),
        "completed": completed,
        "total": len(plans),
        "next_arc": first_missing,
    }


def _plan_story_arcs_from_reference(reference_arcs, total_chapters):
    target_count = _target_story_arc_count(total_chapters)
    groups = _select_reference_arc_groups(reference_arcs, target_count)
    lengths = _allocate_story_arc_lengths(total_chapters, len(groups))

    plans = []
    start_ch = 1
    for idx, (group, length) in enumerate(zip(groups, lengths), 1):
        end_ch = min(total_chapters, start_ch + length - 1)
        plans.append({
            "idx": idx,
            "start_ch": start_ch,
            "end_ch": end_ch,
            "reference_story_arc": _format_reference_arc_group(group),
            "reference_range": "；".join(
                f"chapters {arc['start_ch']}-{arc['end_ch']}" for arc in group
            ) or "none",
        })
        start_ch = end_ch + 1

    if plans and plans[-1]["end_ch"] < total_chapters:
        plans[-1]["end_ch"] = total_chapters
    return plans


def _find_story_arc_for_chapter(ws, volume, ch_num):
    for arc in _list_novel_story_arcs(ws, volume):
        if arc["start_ch"] <= ch_num <= arc["end_ch"]:
            return arc["content"]
    return ""


def _find_legacy_batch_for_chapter(ws, volume, ch_num, total_chapters):
    batch_dir = os.path.join(ws.file_system, "outlines", f"vol_{volume:02d}")
    if not os.path.isdir(batch_dir):
        return ""
    batch_idx = (ch_num - 1) // BATCH_SIZE + 1
    bs = (batch_idx - 1) * BATCH_SIZE + 1
    be = min(batch_idx * BATCH_SIZE, total_chapters)
    return _read_file(os.path.join(batch_dir, f"batch_{bs:03d}_{be:03d}.md")) or ""


def _adapted_reference_batch_path(ws, volume, start_ch, end_ch):
    return os.path.join(
        ws.file_system,
        "adaptation",
        "adapted_reference_batches",
        f"vol_{volume:02d}",
        f"batch_{start_ch:03d}_{end_ch:03d}.md",
    )


def _adapt_reference_batch(ws, llm, volume, batch_idx, start_ch, end_ch,
                           vol_outline, vol_worldview, reference_batch,
                           rewrite_map, forbidden_terms, force=False):
    """Rewrite the reference batch into a target-world rhythm draft first, to cut old-setting contamination."""
    if not reference_batch:
        return "(no reference-batch data)"

    out_path = _adapted_reference_batch_path(ws, volume, start_ch, end_ch)
    existing = _read_file(out_path)
    if existing and not force:
        return existing

    forbidden_terms_text = format_forbidden_terms(forbidden_terms)
    audit_feedback = ""
    result = ""
    violations = []

    for attempt in range(2):
        prompt = PromptLoader.load(
            "adapt_reference_batch",
            volume_outline=vol_outline,
            volume_worldview=vol_worldview,
            rewrite_map=rewrite_map,
            forbidden_terms=forbidden_terms_text,
            batch_index=batch_idx,
            start_chapter=start_ch,
            end_chapter=end_ch,
            reference_batch=reference_batch,
            audit_feedback=audit_feedback,
        )
        result = normalize_text(llm.generate(prompt))
        violations = scan_forbidden_terms(result, forbidden_terms)
        if not violations:
            _write_file(out_path, result)
            return result

        audit_feedback = (
            f"[Previous adapted-draft violations]\n"
            f"These forbidden leftover reference elements still appear: {', '.join(violations)}.\n"
            "Please re-adapt. Do not keep these old-world elements; if there is no natural counterpart, substitute by function, delete, or delay."
        )
        print(f"  Adapted reference batch still has leftovers: {', '.join(violations)}; rewriting...")

    _write_file(out_path, result)
    append_adaptation_report(
        ws,
        f"Volume {volume} batch {batch_idx} reference-batch adapt leftovers",
        f"File: {out_path}\nViolations: {', '.join(violations)}",
    )
    return result


def _batch_audit_path(ws, volume, batch_idx, start_ch, end_ch, attempt):
    return os.path.join(
        ws.file_system,
        "adaptation",
        "batch_reasonability_audits",
        f"vol_{volume:02d}",
        f"batch_{start_ch:03d}_{end_ch:03d}_attempt_{attempt}.json",
    )


def _audit_batch_summary_reasonability(ws, llm, volume, batch_idx, start_ch, end_ch,
                                       vol_outline, vol_worldview, previous_batch,
                                       reference_batch, adapted_reference_batch,
                                       rewrite_map, batch_summary, attempt):
    """Use the pro model to audit whether a batch summary fits the new-book outline/worldview, not a simple banned-word scan."""
    novel_outline = _read_file(os.path.join(ws.file_system, "novel_outline.md")) or "(new-novel outline not found)"
    new_novel_worldview = _read_file(os.path.join(ws.file_system, "new_novel_worldview.md")) or "(new-novel worldview not found)"

    prompt = PromptLoader.load(
        "batch_reasonability_audit",
        novel_outline=novel_outline,
        new_novel_worldview=new_novel_worldview,
        volume_outline=vol_outline,
        volume_worldview=vol_worldview,
        rewrite_map=rewrite_map,
        batch_index=batch_idx,
        start_chapter=start_ch,
        end_chapter=end_ch,
        previous_batch=previous_batch,
        adapted_reference_batch=adapted_reference_batch or "(no adapted reference-batch draft)",
        reference_batch=reference_batch or "(no reference-batch data)",
        batch_summary=batch_summary,
    )
    raw = normalize_text(llm.generate(prompt))
    audit_path = _batch_audit_path(ws, volume, batch_idx, start_ch, end_ch, attempt)
    _write_file(audit_path, raw)

    try:
        audit = parse_json_response(raw)
    except Exception as e:
        append_adaptation_report(
            ws,
            f"Volume {volume} batch {batch_idx} reasonability-audit parse failed",
            f"File: {audit_path}\nError: {e}",
        )
        return {
            "pass": True,
            "score": 0,
            "violations": [],
            "rewrite_instruction": "",
        }

    audit.setdefault("pass", True)
    audit.setdefault("score", 0)
    audit.setdefault("violations", [])
    audit.setdefault("rewrite_instruction", "")
    return audit


def _generate_batch_summary_with_audit(ws, llm, volume, batch_idx, start_ch, end_ch,
                                       vol_outline, vol_worldview, previous_batch,
                                       reference_batch, adapted_reference_batch,
                                       rewrite_map, forbidden_terms):
    forbidden_terms_text = (
        "The official batch-summary stage does not judge by a static forbidden-word list."
        "Follow the new-novel full outline, this volume outline, this volume worldview, and the rewrite map. "
        "The reference batch supplies only rhythm and plot function; do not write old-world causality as current new-novel fact."
        "After generation a pro model will run a plot-reasonability audit."
    )
    previous_result = ""
    audit_feedback = ""
    result = ""
    audit = {"pass": True, "violations": [], "rewrite_instruction": ""}

    for attempt in range(2):
        prompt = PromptLoader.load(
            "novel_batch_summary",
            volume_outline=vol_outline,
            volume_worldview=vol_worldview,
            rewrite_map=rewrite_map,
            forbidden_terms=forbidden_terms_text,
            batch_index=batch_idx,
            start_chapter=start_ch,
            end_chapter=end_ch,
            previous_batch=previous_batch,
            adapted_reference_batch=adapted_reference_batch or "(no adapted reference-batch draft)",
            reference_batch=reference_batch or "(no reference-batch data)",
            audit_feedback=audit_feedback,
            previous_result=previous_result,
        )
        result = normalize_text(llm.generate(prompt))
        audit = _audit_batch_summary_reasonability(
            ws=ws,
            llm=llm,
            volume=volume,
            batch_idx=batch_idx,
            start_ch=start_ch,
            end_ch=end_ch,
            vol_outline=vol_outline,
            vol_worldview=vol_worldview,
            previous_batch=previous_batch,
            reference_batch=reference_batch,
            adapted_reference_batch=adapted_reference_batch,
            rewrite_map=rewrite_map,
            batch_summary=result,
            attempt=attempt + 1,
        )
        if audit.get("pass"):
            return result

        violations = audit.get("violations") or []
        issue_text = "；".join(
            f"{item.get('type', 'unknown')}: {item.get('reason', item.get('text', ''))}"
            if isinstance(item, dict) else str(item)
            for item in violations
        )
        print(f"  New batch-summary plot-reasonability audit failed; rewriting: {issue_text or 'no specific reason given'}")
        previous_result = f"[Previous generation result]\n{result}"
        rewrite_instruction = audit.get("rewrite_instruction") or "Fix worldview conflicts, leftover old causality, or unreasonable phase progress from the audit."
        audit_feedback = (
            f"[Previous batch-summary plot-reasonability audit failed]\n"
            f"Audit issue: {issue_text or 'no specific reason given'}\n"
            f"Rewrite instruction: {rewrite_instruction}\n"
            "Keep reference rhythm and plot function, but events, people, causality, and phase progress must fit the current new-novel outline and worldview."
        )

    if not audit.get("pass"):
        append_adaptation_report(
            ws,
            f"Volume {volume} batch {batch_idx} batch-summary reasonability audit failed",
            f"Audit result: {audit}\nThe last result is still returned for manual inspection.",
        )
    return result



def _generate_story_arc(ws, llm, volume, arc_idx, start_ch, end_ch,
                        generation_context, target_char_count, cancel_event=None):
    """Generate the current unit from long mainline, neighboring stages, and matching reference-volume story segments only."""
    prompt = PromptLoader.load(
        "novel_story_arc",
        **generation_context,
        arc_index=arc_idx,
        start_chapter=start_ch,
        end_chapter=end_ch,
        target_char_count=target_char_count,
        target_field_chars=max(30, round(target_char_count / 10)),
    )
    result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event))
    return _compact_story_arc_result(
        llm, result, arc_idx, start_ch, end_ch, target_char_count, cancel_event,
    )


def _load_volume_outline_context(ws, volume):
    """Load current stage / legacy volume-outline context and infer total chapter count."""
    stage_context = _load_stage_context(ws, volume)
    if stage_context:
        return stage_context

    vol_outline_file = os.path.join(ws.file_system, "new_volume_outlines", f"vol_{volume:02d}_outline.md")
    vol_outline = _read_file(vol_outline_file)
    if not vol_outline:
        print(f"Error: stage {volume} not found, and no legacy volume-outline file for volume {volume}: {vol_outline_file}")
        print("For the current flow, run novel-outline to generate stage_roadmap.md and make sure the stage exists.")
        return None

    vol_wv_file = os.path.join(ws.file_system, "new_worldviews", f"vol_{volume:02d}_worldview.md")
    vol_worldview = _read_file(vol_wv_file)
    if not vol_worldview:
        print(f"Error: worldview file for volume {volume} not found: {vol_wv_file}")
        print("Run the volume-outline command first to generate volume outlines and worldviews.")
        return None

    chapter_nums = re.findall(r'第(\d+)章', vol_outline)
    if not chapter_nums:
        print("Error: cannot infer total chapter count from the volume outline.")
        return None

    return vol_outline, vol_worldview, max(int(c) for c in chapter_nums)


def gen_story_arcs(ws, volume=1, force=False, progress_callback=None, pause_event=None,
                   stop_event=None, cancel_event=None):
    """Generate new-book story-arc units from the current stage design.

    Return a result dict:
    - success: {"artifacts": [...], "adjustment_note": "..."}
    - failure: {"error": "...", "artifacts": []}
    """
    context = _load_volume_outline_context(ws, volume)
    if not context:
        return {"error": f"Context for stage {volume} not found. Generate stage_roadmap.md in the stage-design step and make sure the stage exists with Planned chapters.", "artifacts": []}
    _, _, total_chapters = context
    if progress_callback:
        progress_callback("preparing", 0, 0, "Reading long mainline, stage, and matching reference story segments")

    llm = _get_lite_llm()
    if not llm:
        return {"error": "No usable model is configured. Configure the LLM API in the top-right first.", "artifacts": []}

    generation_context = _simple_story_arc_context(ws, volume)
    print(f"  -> Read simplified story-arc input for stage {volume} (compressed context is no longer generated).")

    arc_plans = _story_arc_plans_for_volume(ws, volume, total_chapters)
    target_char_count = _reference_story_arc_average_chars(ws, volume)
    total_arcs = len(arc_plans)
    story_arc_dir = _volume_story_arc_dir(ws, volume)
    if force:
        _clear_story_arc_files(ws, volume)
    os.makedirs(story_arc_dir, exist_ok=True)

    print(
        f">>> Generating volume {volume} story-arc units serially "
        f"({total_chapters} chapters, {len(arc_plans)} planned story-arc units, "
        f"about {target_char_count} chars each) <<<"
    )

    generated_items = []
    for plan in arc_plans:
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback(
                    "paused", len(generated_items), total_arcs,
                    "Paused; click continue to generate from the next story-arc unit",
                )
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break
        arc_idx = plan["idx"]
        start_ch = plan["start_ch"]
        end_ch = plan["end_ch"]
        if progress_callback:
            progress_callback(
                "generating", len(generated_items), total_arcs,
                f"Generating story-arc unit {arc_idx} (chapters {start_ch}-{end_ch})",
            )
        arc_file = _story_arc_path(ws, volume, arc_idx, start_ch, end_ch)
        arc_name = _story_arc_file_name(arc_idx, start_ch, end_ch)
        existing = _read_file(arc_file)
        if existing and not force:
            print(f"  Story-arc unit {arc_idx} (chapters {start_ch}-{end_ch}) already exists; skipping.")
            generated_items.append({
                "idx": arc_idx,
                "start_ch": start_ch,
                "end_ch": end_ch,
                "file": arc_name,
                "path": arc_file,
                "content": existing,
            })
            if progress_callback:
                progress_callback(
                    "generating", len(generated_items), total_arcs,
                    f"story-arc unit {arc_idx} already exists; continuing with the next unit",
                )
            continue

        print(f"  Generating story-arc unit {arc_idx} (chapters {start_ch}-{end_ch})...")
        while True:
            try:
                result = _generate_story_arc(
                    ws=ws,
                    llm=llm,
                    volume=volume,
                    arc_idx=arc_idx,
                    start_ch=start_ch,
                    end_ch=end_ch,
                    generation_context=_story_arc_prompt_context(generation_context, plan),
                    target_char_count=target_char_count,
                    cancel_event=cancel_event,
                )
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    result = None
                    break
                if progress_callback:
                    progress_callback(
                        "paused", len(generated_items), total_arcs,
                        "Model request paused; click continue to regenerate the current story-arc",
                    )
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if result is None:
            break
        if not str(result).strip():
            print(
                f"  Warning: story-arc unit {arc_idx} got no model output; not written. You can retry."
            )
            continue
        _write_file(arc_file, result)
        generated_items.append({
            "idx": arc_idx,
            "start_ch": start_ch,
            "end_ch": end_ch,
            "file": arc_name,
            "path": arc_file,
            "content": result,
        })
        if progress_callback:
            progress_callback(
                "generating", len(generated_items), total_arcs,
                f"story-arc unit {arc_idx} complete",
            )
        print(f"  -> Story-arc unit {arc_idx} saved: {arc_file}")

    _write_story_arc_index(ws, volume, generated_items)
    stopped = stop_event is not None and stop_event.is_set()
    if progress_callback:
        progress_callback(
            "stopped" if stopped else "completed",
            len(generated_items), total_arcs,
            "This round of generation ended; completed content was kept" if stopped else "All story-arc units complete",
        )
    print(f"\n>>> Volume {volume} story-arc units generated, {len(generated_items)} total.<<<")

    artifacts = [
        {
            "path": f"file_system/story_arcs/vol_{volume:02d}/{item['file']}",
            "label": f"story-arc unit {item['idx']} (chapters {item['start_ch']}-{item['end_ch']})",
        }
        for item in generated_items
    ]
    return {
        "artifacts": artifacts,
        "adjustment_note": (
            f"This round of generation ended; kept {len(generated_items)} story-arc units."
            if stopped else f"Generated volume {volume} story-arc units, {len(generated_items)} total."
        ),
        "stopped": stopped,
    }


def refine_story_arcs(ws, volume, instruction, cancel_event=None):
    """Adjust all story-arc units of a given stage/volume from a user instruction."""
    print(f">>> Adjusting volume {volume} story-arc units <<<")
    arcs = _list_novel_story_arcs(ws, volume)
    if not arcs:
        print("Error: this volume has no story-arc units yet. Generate them in the chat box first.")
        return {}

    llm = _get_lite_llm()
    if not llm:
        return {}

    generation_context = _simple_story_arc_context(ws, volume)

    current_text = "\n\n===\n\n".join(arc["content"] for arc in arcs)
    prompt = PromptLoader.load(
        "story_arcs_refine",
        **generation_context,
        current_arcs=current_text,
        instruction=instruction,
    )
    raw = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.3))

    # Split on the separator and write back one by one
    segments = [seg.strip() for seg in raw.split("===") if seg.strip()]
    # Back up old files
    import time as _time
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(_volume_story_arc_dir(ws, volume), "versions")
    os.makedirs(backup_dir, exist_ok=True)
    for arc in arcs:
        backup_path = os.path.join(backup_dir, f"{arc['file']}_{stamp}")
        os.rename(arc["path"], backup_path) if os.path.exists(arc["path"]) else None

    written = []
    for idx, seg in enumerate(segments):
        if idx < len(arcs):
            arc = arcs[idx]
        else:
            # Newly added units
            arc = {"file": f"arc_{idx + 1}_ch{'?'}_{'?'}.md"}
        # Extract the chapter range from the first line
        first_line = seg.split("\n")[0] if "\n" in seg else seg.split("\n")[0]
        range_match = re.search(r'第(\d+)-(\d+)章', first_line)
        if range_match:
            start_ch = int(range_match.group(1))
            end_ch = int(range_match.group(2))
        elif idx < len(arcs):
            start_ch = arcs[idx]["start_ch"]
            end_ch = arcs[idx]["end_ch"]
        else:
            continue
        arc_idx_num = idx + 1
        arc_path = _story_arc_path(ws, volume, arc_idx_num, start_ch, end_ch)
        arc_name = _story_arc_file_name(arc_idx_num, start_ch, end_ch)
        _write_file(arc_path, seg)
        written.append({"label": f"story-arc unit {arc_idx_num} (chapters {start_ch}-{end_ch})",
                        "path": f"file_system/story_arcs/vol_{volume:02d}/{arc_name}"})
        print(f"  -> Story-arc unit {arc_idx_num} updated: {arc_path}")

    # Update the index
    updated_items = []
    for idx, seg in enumerate(segments):
        if idx < len(arcs):
            updated_items.append({"idx": idx + 1, "start_ch": arcs[idx]["start_ch"], "end_ch": arcs[idx]["end_ch"], "file": arcs[idx]["file"]})
    if updated_items:
        _write_story_arc_index(ws, volume, updated_items)

    return {"adjustment_note": f"Adjusted {len(written)} story-arc units per the instruction.", "artifacts": written}


def _normalize_refinement_mode(value, instruction):
    """Normalize router output to regenerate/revise, with aliases for older model text."""
    normalized = str(value or "").strip().lower()
    if normalized in {
        "regenerate", "rewrite", "start over", "full rewrite",
        "重新生成", "完全重写", "推倒重来", "全部重写", "从头生成",
    }:
        return "regenerate"
    if normalized in {"revise", "adjust", "optimize", "修改", "调整", "优化"}:
        return "revise"
    compact = re.sub(r"\s+", "", str(instruction or "")).lower()
    regenerate_markers = (
        "重新生成", "完全重写", "推倒重来", "从头生成", "重新写一版", "重写一版", "全部重写",
        "regenerate", "rewrite", "startover", "fullrewrite",
    )
    return "regenerate" if any(marker in compact for marker in regenerate_markers) else "revise"


def _route_story_arc_refinement(llm, arcs, instruction, cancel_event=None):
    current_arcs = "\n\n===\n\n".join(arc["content"] for arc in arcs)
    prompt = PromptLoader.load(
        "story_arc_refine_route",
        current_arcs=current_arcs,
        instruction=instruction,
    )
    raw = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2))
    routed = parse_json_response(raw)
    if not isinstance(routed, dict):
        routed = {}
    existing_ids = {arc["idx"] for arc in arcs}
    try:
        start_arc = int(routed.get("start_arc"))
    except (TypeError, ValueError, AttributeError):
        start_arc = min(existing_ids)
    if start_arc not in existing_ids:
        start_arc = min(existing_ids)
    mode = _normalize_refinement_mode(routed.get("mode"), instruction)
    return start_arc, mode, str(routed.get("reason") or "Located the earliest affected story-arc from the user instruction.")


def _serial_refinement_targets(ws, volume, arcs, start_arc):
    """Merge generated story-arcs with this volume's full plan so missing units can still be generated after adjust."""
    context = _load_volume_outline_context(ws, volume)
    if not context:
        return []
    _, _, total_chapters = context
    existing = {arc["idx"]: arc for arc in arcs}
    targets = []
    for plan in _story_arc_plans_for_volume(ws, volume, total_chapters):
        if plan["idx"] < start_arc:
            continue
        saved = existing.get(plan["idx"])
        targets.append({
            **plan,
            "file": saved["file"] if saved else _story_arc_file_name(
                plan["idx"], plan["start_ch"], plan["end_ch"],
            ),
            "path": saved["path"] if saved else _story_arc_path(
                ws, volume, plan["idx"], plan["start_ch"], plan["end_ch"],
            ),
            "content": saved["content"] if saved else "(this story-arc unit has not been generated; create it from the latest prior state and the user instruction.)",
            "existed": bool(saved),
        })
    return targets


def refine_story_arcs_serial(ws, volume, instruction, progress_callback=None,
                             pause_event=None, stop_event=None, cancel_event=None):
    """Route the adjust start, then serially regenerate following story-arcs from that unit."""
    arcs = _list_novel_story_arcs(ws, volume)
    if not arcs:
        return {"error": "This volume has no story-arc units yet.", "artifacts": []}
    llm = _get_lite_llm()
    if not llm:
        return {"error": "No usable model is configured.", "artifacts": []}

    if progress_callback:
        progress_callback("routing", 0, len(arcs), "Analyzing the earliest story-arc unit affected by the user instruction")
    while True:
        try:
            start_arc, refinement_mode, route_reason = _route_story_arc_refinement(
                llm, arcs, instruction, cancel_event,
            )
            break
        except LLMCallCancelled:
            if stop_event is not None and stop_event.is_set():
                return {"artifacts": [], "adjustment_note": "This round of adjust ended; existing content was left unchanged.", "stopped": True}
            if progress_callback:
                progress_callback("paused", 0, len(arcs), "Range analysis paused; click continue to analyze again")
            if pause_event is not None:
                pause_event.wait()
            if cancel_event is not None:
                cancel_event.clear()

    targets = _serial_refinement_targets(ws, volume, arcs, start_arc)
    if not targets:
        return {"error": "Cannot read the full story-arc plan for the current volume.", "artifacts": []}
    target_char_count = _reference_story_arc_average_chars(ws, volume)
    generation_context = _simple_story_arc_context(ws, volume)
    generated_by_idx = {arc["idx"]: arc["content"] for arc in arcs if arc["idx"] < start_arc}
    written = []
    import shutil
    import time as _time
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(_volume_story_arc_dir(ws, volume), "versions")
    os.makedirs(backup_dir, exist_ok=True)

    for target in targets:
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback("paused", len(written), len(targets), "Serial adjustment paused")
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break

        previous = generated_by_idx.get(target["idx"] - 1) or "(this is the first story-arc unit of the current volume)"
        action_label = (
            "revise from original content"
            if target["existed"] and refinement_mode == "revise"
            else "full regenerate"
            if target["existed"]
            else "continue generating"
        )
        if progress_callback:
            progress_callback(
                "refining", len(written), len(targets),
                f"Route: start at story-arc unit {start_arc}; {action_label} story-arc unit {target['idx']}",
            )
        prompt = PromptLoader.load(
            "story_arc_serial_refine",
            **_story_arc_prompt_context(generation_context, target),
            instruction=instruction,
            previous_story_arc=previous,
            current_story_arc=(
                target["content"]
                if refinement_mode == "revise"
                else "(this round is a full regenerate; do not use the old version of this unit, and do not invent or restore old text.)"
            ),
            arc_index=target["idx"],
            start_chapter=target["start_ch"],
            end_chapter=target["end_ch"],
            target_char_count=target_char_count,
        )
        while True:
            try:
                result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.3))
                result = _compact_story_arc_result(
                    llm, result, target["idx"], target["start_ch"], target["end_ch"],
                    target_char_count, cancel_event,
                )
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    result = None
                    break
                if progress_callback:
                    progress_callback(
                        "paused", len(written), len(targets),
                        f"story-arc unit {target['idx']} adjustment paused; continue to regenerate this unit",
                    )
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if result is None:
            break
        if not str(result).strip():
            print(
                f"  Warning: story-arc unit {target['idx']} got no model output; not written. You can retry."
            )
            continue

        backup_path = os.path.join(backup_dir, f"{target['file']}_{stamp}")
        if target["existed"] and not os.path.exists(backup_path):
            shutil.copy2(target["path"], backup_path)
        _write_file(target["path"], result)
        generated_by_idx[target["idx"]] = result
        written.append({
            "label": f"story-arc unit {target['idx']} (chapters {target['start_ch']}-{target['end_ch']})",
            "path": f"file_system/story_arcs/vol_{volume:02d}/{target['file']}",
        })
        if progress_callback:
            progress_callback(
                "refining", len(written), len(targets),
                f"story-arc unit {target['idx']} {action_label} complete",
            )

    stopped = stop_event is not None and stop_event.is_set()
    current_items = _list_novel_story_arcs(ws, volume)
    if current_items:
        _write_story_arc_index(ws, volume, current_items)
    return {
        "adjustment_note": (
            f"This round ended; started at story-arc unit {start_arc}, finished {len(written)}/{len(targets)}."
            if stopped
            else (
                f"Per the instruction, serially processed {len(written)} story-arc units starting at unit {start_arc}, "
                f"and filled later units that had not been generated. Handling: "
                f"{'full regenerate' if refinement_mode == 'regenerate' else 'revise from current content'}."
                f"Route reason: {route_reason}"
            )
        ),
        "artifacts": written,
        "stopped": stopped,
        "start_arc": start_arc,
        "mode": refinement_mode,
        "total_adjusted": len(targets),
    }


STORY_LINE_LIMIT = 100  # Chapter-outline "story line" char cap: stop serial chapter generation from growing longer each chapter.


def _truncate_plus_chain(content, limit):
    """Trim "A+B+C" content to the limit, preferring cuts at + boundaries so as many full nodes as possible remain."""
    content = (content or "").strip()
    if len(content) <= limit:
        return content
    parts = [part.strip() for part in content.split("+") if part.strip()]
    if len(parts) <= 1:
        return content[:limit].strip()
    kept = ""
    for part in parts:
        candidate = part if not kept else kept + "+" + part
        if len(candidate) <= limit:
            kept = candidate
        else:
            break
    return kept or content[:limit].strip()


def _cap_story_line_in_outline(text, limit=STORY_LINE_LIMIT):
    """Trim the "# Story line" section of a chapter outline to the limit; keep the rest (chapter rhythm, synopsis, etc.) as-is.

    Applied before writing the chapter outline so serial generation does not keep lengthening the story line from prior outlines.
    If there is no "# Story line" section, return as-is. Idempotent.
    """
    if not text:
        return text
    lines = text.splitlines()
    header_idx = None
    same_line = ""
    for idx, line in enumerate(lines):
        match = re.match(r"^\s*#{0,6}\s*(?:故事线|Story line|Storyline)\s*[:：]?\s*(.*)$", line, re.I)
        if match:
            header_idx = idx
            same_line = (match.group(1) or "").strip()
            break
    if header_idx is None:
        return text
    body = []
    if same_line:
        body.append(same_line)
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        # The story-line section runs until the next heading (# / 【).
        if re.match(r"^\s*(?:#{1,6}\s+\S|【)", lines[j]):
            end = j
            break
        if lines[j].strip():
            body.append(lines[j].strip())
    content = " ".join(body)
    if len(content) <= limit:
        return text
    capped = _truncate_plus_chain(content, limit)
    header_clean = re.match(
        r"\s*#{0,6}\s*(?:故事线|Story line|Storyline)",
        lines[header_idx],
        re.I,
    ).group(0)
    new_lines = lines[:header_idx] + [header_clean, capped] + lines[end:]
    return "\n".join(new_lines)


def gen_serial_chapter_outlines(ws, volume=1, force=False):
    """From generated new-book story-arc units, serially generate this volume's per-chapter outlines."""
    context = _load_volume_outline_context(ws, volume)
    if not context:
        return
    _, _, total_chapters = context

    ch_out_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    story_arcs = _list_novel_story_arcs(ws, volume)
    if not story_arcs:
        print("Error: no story-arc units found; cannot generate chapter outlines. Run story-arcs first.")
        return

    llm = _get_lite_llm()
    if not llm:
        return
    _ensure_system_panel_decision(ws)
    sync_finalized_drafts_for_outlines(
        llm, ws, volume, total_chapters,
    )
    print(f">>> Generating volume {volume} chapter outlines serially <<<")
    os.makedirs(ch_out_dir, exist_ok=True)

    for arc in story_arcs:
        arc_start = arc["start_ch"]
        arc_end = arc["end_ch"]
        arc_content = arc["content"]
        if not arc_content:
            print(f"  Warning: story-arc unit {arc['file']} is empty; skipping.")
            continue

        print(f"\n  --- Story-arc unit {arc['idx']}: chapters {arc_start}-{arc_end} ---")

        for ch_num in range(arc_start, arc_end + 1):
            out_file = os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md")
            if os.path.exists(out_file) and not force:
                print(f"  Chapter {ch_num} outline already exists; skipping.")
                continue

            # Read only the immediately previous chapter outline so earlier ones are not carried again.
            previous_text = _read_file(
                os.path.join(ch_out_dir, f"chapter_{ch_num - 1:03d}.md")
            ) if ch_num > 1 else ""
            previous_text = re.sub(
                r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', previous_text or "",
            ).strip() or "(this is chapter 1; no previous chapter outline)"

            print(f"  Generating chapter {ch_num} outline...")
            prompt = PromptLoader.load(
                "serial_chapter_outline",
                previous_system_panel=json.dumps(
                    _previous_system_panel(ws, volume, ch_num), ensure_ascii=False, indent=2,
                ),
                story_arc=arc_content,
                previous_chapter_outline=previous_text,
                chapter_num=ch_num,
            )
            result = normalize_text(llm.generate(prompt))
            if not str(result).strip():
                print(f"  Warning: chapter {ch_num} outline got no model output; not written. You can retry.")
                continue
            _generate_chapter_system_panel(llm, ws, volume, ch_num, result)
            result = _cap_story_line_in_outline(result)
            _write_file(out_file, result)
            print(f"  -> Chapter {ch_num} outline saved: {out_file}")

    print(f"\n>>> Volume {volume}: all {total_chapters} chapter outlines generated.<<<")


def chapter_outline_resume_status(ws, volume, arc_idx):
    """Return the chapter-outline breakpoint of a given story-arc unit, for resume after restart or refresh."""
    target_arc = next(
        (arc for arc in _list_novel_story_arcs(ws, volume) if arc["idx"] == arc_idx),
        None,
    )
    if not target_arc:
        return {"can_resume": False, "completed": 0, "total": 0, "next_chapter": None}
    ch_out_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    chapter_nums = list(range(target_arc["start_ch"], target_arc["end_ch"] + 1))
    panel_status = system_panel_status(ws)
    panel_required = panel_status["enabled"] or not panel_status["decided"]
    has_any_outline = any(
        _read_file(os.path.join(ch_out_dir, f"chapter_{ch:03d}.md"))
        for ch in chapter_nums
    )
    existing = [
        ch for ch in chapter_nums
        if (
            _read_file(os.path.join(ch_out_dir, f"chapter_{ch:03d}.md"))
            and (
                ch in _finalized_chapter_numbers(ws, "outlines", volume)
                or
                not panel_required
                or _read_json_file(_system_panel_chapter_path(ws, volume, ch))
            )
        )
    ]
    missing = [ch for ch in chapter_nums if ch not in existing]
    return {
        "can_resume": bool(has_any_outline and missing),
        "completed": len(existing),
        "total": len(chapter_nums),
        "next_chapter": missing[0] if missing else None,
    }


def gen_chapter_outlines_for_arc(ws, volume, arc_idx, progress_callback=None,
                                 pause_event=None, stop_event=None, cancel_event=None):
    """Generate per-chapter outlines for one story-arc unit of a given stage/volume."""
    context = _load_volume_outline_context(ws, volume)
    if not context:
        return {}
    _, _, total_chapters = context

    story_arcs = _list_novel_story_arcs(ws, volume)
    target_arc = None
    for arc in story_arcs:
        if arc["idx"] == arc_idx:
            target_arc = arc
            break
    if not target_arc:
        print(f"Error: story-arc unit {arc_idx} of volume {volume} not found.")
        return {}

    llm = _get_lite_llm()
    if not llm:
        return {}

    while True:
        try:
            if progress_callback:
                progress_callback("system_panel_setup", 0, 0, "Checking whether a system panel is needed")
            _ensure_system_panel_decision(ws, cancel_event)
            break
        except LLMCallCancelled:
            if stop_event is not None and stop_event.is_set():
                return {"adjustment_note": "This round of generation ended.", "artifacts": [], "stopped": True}
            if progress_callback:
                progress_callback("paused", 0, 0, "System-panel decision paused; click continue to decide again")
            if pause_event is not None:
                pause_event.wait()
            if cancel_event is not None:
                cancel_event.clear()

    ch_out_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    os.makedirs(ch_out_dir, exist_ok=True)
    sync_finalized_drafts_for_outlines(
        llm, ws, volume, target_arc["end_ch"], progress_callback,
        pause_event, stop_event, cancel_event,
    )
    if stop_event is not None and stop_event.is_set():
        return {"adjustment_note": "This round of generation ended.", "artifacts": [], "stopped": True}
    arc_start = target_arc["start_ch"]
    arc_end = target_arc["end_ch"]
    arc_content = target_arc["content"]
    print(f">>> Generating chapter outlines for volume {volume} story-arc unit {arc_idx} (chapters {arc_start}-{arc_end}) <<<")

    written = []
    finalized = _finalized_chapter_numbers(ws, "outlines", volume)
    for ch_num in range(arc_start, arc_end + 1):
        out_file = os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md")
        existing = _read_file(out_file)
        if existing:
            if ch_num in finalized:
                written.append({
                    "label": f"chapter {ch_num} outline",
                    "path": f"file_system/chapter_outlines/vol_{volume:02d}/chapter_{ch_num:03d}.md",
                })
                continue
            panel_required = system_panel_status(ws)["enabled"]
            panel_exists = _read_json_file(_system_panel_chapter_path(ws, volume, ch_num))
            if not panel_required or panel_exists:
                written.append({
                    "label": f"chapter {ch_num} outline",
                    "path": f"file_system/chapter_outlines/vol_{volume:02d}/chapter_{ch_num:03d}.md",
                })
                continue
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback("paused", len(written), arc_end - arc_start + 1, "Chapter-outline generation paused")
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break
        previous_text = _read_file(
            os.path.join(ch_out_dir, f"chapter_{ch_num - 1:03d}.md")
        ) if ch_num > 1 else ""
        previous_text = re.sub(
            r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', previous_text or "",
        ).strip() or "(this is chapter 1; no previous chapter outline)"

        print(f"  Generating chapter {ch_num} outline...")
        prompt = PromptLoader.load(
            "serial_chapter_outline",
            previous_system_panel=json.dumps(
                _previous_system_panel(ws, volume, ch_num), ensure_ascii=False, indent=2,
            ),
            story_arc=arc_content,
            previous_chapter_outline=previous_text,
            chapter_num=ch_num,
        )
        if progress_callback:
            progress_callback(
                "generating", len(written), arc_end - arc_start + 1,
                f"Generating chapter {ch_num} outline",
            )
        while True:
            try:
                result = normalize_text(
                    _generate_with_cancel(llm, prompt, cancel_event, temperature=0.7)
                )
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    result = None
                    break
                if progress_callback:
                    progress_callback(
                        "paused", len(written), arc_end - arc_start + 1,
                        f"Chapter {ch_num} generation paused; continue to regenerate this chapter",
                    )
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if result is None:
            break
        if not _update_chapter_system_panel_with_controls(
            llm, ws, volume, ch_num, result, len(written),
            arc_end - arc_start + 1, progress_callback, pause_event,
            stop_event, cancel_event,
        ):
            break
        result = _cap_story_line_in_outline(result)
        _write_file(out_file, result)
        written.append({
            "label": f"chapter {ch_num} outline",
            "path": f"file_system/chapter_outlines/vol_{volume:02d}/chapter_{ch_num:03d}.md",
        })
        if progress_callback:
            progress_callback(
                "generating", len(written), arc_end - arc_start + 1,
                f"chapter {ch_num} outline written",
            )
        print(f"  -> Chapter {ch_num} outline saved: {out_file}")

    stopped = stop_event is not None and stop_event.is_set()
    return {
        "adjustment_note": (
            f"This round of generation ended; kept {len(written)}/{arc_end - arc_start + 1} chapters."
            if stopped else f"Generated per-chapter outlines for chapters {arc_start}-{arc_end}."
        ),
        "artifacts": written,
        "stopped": stopped,
    }


def _route_chapter_outline_refinement(llm, outlines, instruction, start_ch, end_ch,
                                      cancel_event=None):
    current_text = "\n\n===\n\n".join(
        f"[Chapter {chapter_num}]\n{content}"
        for chapter_num, content in outlines
    )
    prompt = PromptLoader.load(
        "chapter_outline_refine_route",
        start_chapter=start_ch,
        end_chapter=end_ch,
        current_outlines=current_text or "(no chapter outlines yet)",
        instruction=instruction,
    )
    raw = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2))
    routed = parse_json_response(raw)
    if not isinstance(routed, dict):
        routed = {}
    try:
        requested_chapter = int(routed.get("start_chapter"))
    except (TypeError, ValueError, AttributeError):
        requested_chapter = start_ch
    routed_chapter = min(end_ch, max(start_ch, requested_chapter))
    reason = str(routed.get("reason") or "Located the earliest affected chapter from the user instruction.")
    if requested_chapter < start_ch:
        reason = f"Chapters through {start_ch - 1} are locked by final-draft sync; the editable range starts at chapter {start_ch}."
    mode = _normalize_refinement_mode(routed.get("mode"), instruction)
    return routed_chapter, mode, reason


def refine_chapter_outlines_serial(ws, volume, arc_idx, instruction, progress_callback=None,
                                   pause_event=None, stop_event=None, cancel_event=None):
    """Route the earliest affected chapter, then serially regenerate through the story-arc unit's last chapter."""
    target_arc = next(
        (arc for arc in _list_novel_story_arcs(ws, volume) if arc["idx"] == arc_idx),
        None,
    )
    if not target_arc:
        return {"error": f"Story-arc unit {arc_idx} of volume {volume} not found.", "artifacts": []}
    llm = _get_lite_llm()
    if not llm:
        return {"error": "No usable model is configured.", "artifacts": []}
    while True:
        try:
            if progress_callback:
                progress_callback("system_panel_setup", 0, 0, "Checking whether a system panel is needed")
            _ensure_system_panel_decision(ws, cancel_event)
            break
        except LLMCallCancelled:
            if stop_event is not None and stop_event.is_set():
                return {"adjustment_note": "This round of adjust ended.", "artifacts": [], "stopped": True}
            if progress_callback:
                progress_callback("paused", 0, 0, "System-panel decision paused; click continue to decide again")
            if pause_event is not None:
                pause_event.wait()
            if cancel_event is not None:
                cancel_event.clear()
    ch_out_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    os.makedirs(ch_out_dir, exist_ok=True)
    start_ch, end_ch = target_arc["start_ch"], target_arc["end_ch"]
    sync_finalized_drafts_for_outlines(
        llm, ws, volume, end_ch, progress_callback,
        pause_event, stop_event, cancel_event,
    )
    if stop_event is not None and stop_event.is_set():
        return {"adjustment_note": "This round of adjust ended.", "artifacts": [], "stopped": True}
    finalized_boundary = _finalized_chapter_boundary(
        ws, "outlines", volume, start_ch, end_ch,
    )
    editable_start = max(start_ch, finalized_boundary + 1)
    if editable_start > end_ch:
        return {
            "adjustment_note": (
                f"Chapters {start_ch}-{end_ch} are all locked by final-draft sync; "
                "this round did not change chapter outlines."
            ),
            "artifacts": [],
            "stopped": False,
            "start_chapter": None,
            "total_adjusted": 0,
        }
    outlines = []
    for ch_num in range(editable_start, end_ch + 1):
        content = _read_file(os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md"))
        if content:
            outlines.append((ch_num, content))
    if not outlines:
        return {"error": "This story-arc unit has no chapter outlines yet. Generate them first.", "artifacts": []}

    if progress_callback:
        progress_callback(
            "routing", 0, len(outlines),
            f"Choosing the adjustment start in the editable range of chapters {editable_start}-{end_ch}",
        )
    generic_instruction = instruction.strip().lower() in {
        "生成", "重新生成", "继续生成", "调整", "优化",
        "generate", "regenerate", "continue generating", "adjust", "optimize",
    }
    if generic_instruction:
        routed_chapter = editable_start
        refinement_mode = _normalize_refinement_mode(None, instruction)
        route_reason = (
            f"Chapters {start_ch}-{editable_start - 1} are locked by final-draft sync; "
            f"the user did not name a specific change; processing the editable range from chapter {editable_start}."
            if editable_start > start_ch else
            f"the user did not name a specific change; processing from the first editable chapter {editable_start}."
        )
    else:
        while True:
            try:
                routed_chapter, refinement_mode, route_reason = _route_chapter_outline_refinement(
                    llm, outlines, instruction, editable_start, end_ch, cancel_event,
                )
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    return {"adjustment_note": "This round of adjust ended; existing chapter outlines were left unchanged.", "artifacts": [], "stopped": True}
                if progress_callback:
                    progress_callback("paused", 0, len(outlines), "Range analysis paused; continue to analyze again")
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()

    finalized = _finalized_chapter_numbers(ws, "outlines", volume)
    finalized_boundary = _finalized_chapter_boundary(
        ws, "outlines", volume, start_ch, end_ch,
    )
    targets = [
        chapter for chapter in range(max(routed_chapter, finalized_boundary + 1), end_ch + 1)
        if chapter not in finalized
    ]
    written = []
    import shutil
    import time as _time
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(ch_out_dir, "versions")
    os.makedirs(backup_dir, exist_ok=True)
    for ch_num in targets:
        if ch_num in _finalized_chapter_numbers(ws, "outlines", volume):
            continue
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback("paused", len(written), len(targets), "Chapter-outline adjustment paused")
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break
        previous = _read_file(os.path.join(ch_out_dir, f"chapter_{ch_num - 1:03d}.md"))
        current_path = os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md")
        current = _read_file(current_path)
        if progress_callback:
            progress_callback(
                "refining", len(written), len(targets),
                f"Route: start at chapter {routed_chapter}; processing chapter {ch_num}",
            )
        prompt = PromptLoader.load(
            "chapter_outline_serial_refine",
            story_arc=target_arc["content"],
            instruction=instruction,
            previous_outline=previous or "(this is the first chapter of this story-arc unit)",
            previous_system_panel=json.dumps(
                _previous_system_panel(ws, volume, ch_num), ensure_ascii=False, indent=2,
            ),
            current_outline=(
                (current or "(this chapter has not been generated yet)")
                if refinement_mode == "revise"
                else "(this round is a full regenerate; do not use the old chapter outline, and do not invent or restore old text.)"
            ),
            chapter_num=ch_num,
        )
        while True:
            try:
                result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.3))
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    result = None
                    break
                if progress_callback:
                    progress_callback(
                        "paused", len(written), len(targets),
                        f"Chapter {ch_num} adjust paused; continue to regenerate this chapter",
                    )
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if result is None:
            break
        if current:
            backup_path = os.path.join(backup_dir, f"chapter_{ch_num:03d}.md_{stamp}")
            if not os.path.exists(backup_path):
                shutil.copy2(current_path, backup_path)
        if not _update_chapter_system_panel_with_controls(
            llm, ws, volume, ch_num, result, len(written), len(targets),
            progress_callback, pause_event, stop_event, cancel_event,
        ):
            break
        result = _cap_story_line_in_outline(result)
        _write_file(current_path, result)
        written.append({
            "label": f"chapter {ch_num} outline",
            "path": f"file_system/chapter_outlines/vol_{volume:02d}/chapter_{ch_num:03d}.md",
        })
        if progress_callback:
            progress_callback("refining", len(written), len(targets), f"Chapter {ch_num} outline written")

    stopped = stop_event is not None and stop_event.is_set()
    return {
        "adjustment_note": (
            f"This round of adjust ended; started at chapter {routed_chapter}, finished {len(written)}/{len(targets)}."
            if stopped else
            (
                f"Per the instruction, serially processed {len(written)} chapters starting at chapter {routed_chapter}."
                f"Handling: {'full regenerate' if refinement_mode == 'regenerate' else 'revise from current content'}."
                f"Route reason: {route_reason}"
            )
        ),
        "artifacts": written,
        "stopped": stopped,
        "start_chapter": routed_chapter,
        "mode": refinement_mode,
        "total_adjusted": len(targets),
    }


def refine_chapter_outlines(ws, volume, arc_idx, instruction):
    """Adjust the chapter outlines of a given story-arc unit from a user instruction."""
    llm = _get_lite_llm()
    if not llm:
        return {}

    story_arcs = _list_novel_story_arcs(ws, volume)
    target_arc = None
    for arc in story_arcs:
        if arc["idx"] == arc_idx:
            target_arc = arc
            break
    if not target_arc:
        print(f"Error: story-arc unit {arc_idx} of volume {volume} not found.")
        return {}

    arc_start = target_arc["start_ch"]
    arc_end = target_arc["end_ch"]
    ch_out_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")

    # Read current chapter outlines
    outlines = []
    for ch_num in range(arc_start, arc_end + 1):
        out_file = os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md")
        content = _read_file(out_file)
        if content:
            outlines.append(content)
    if not outlines:
        print("Error: this story-arc unit has no chapter outlines yet. Generate them in the chat box first.")
        return {}

    current_text = "\n\n===\n\n".join(outlines)
    prompt = PromptLoader.load(
        "chapter_outlines_refine",
        story_arc=target_arc["content"],
        current_outlines=current_text,
        instruction=instruction,
    )
    raw = normalize_text(llm.generate(prompt, temperature=0.3))

    # Split on === and write back one by one
    segments = [seg.strip() for seg in raw.split("===") if seg.strip()]
    import time as _time
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(ch_out_dir, "versions")
    os.makedirs(backup_dir, exist_ok=True)

    written = []
    ch_num = arc_start
    for idx, seg in enumerate(segments):
        out_file = os.path.join(ch_out_dir, f"chapter_{ch_num:03d}.md")
        if os.path.exists(out_file):
            import shutil
            shutil.copy2(out_file, os.path.join(backup_dir, f"chapter_{ch_num:03d}.md_{stamp}"))
        seg = _cap_story_line_in_outline(seg)
        _write_file(out_file, seg)
        written.append({
            "label": f"chapter {ch_num} outline",
            "path": f"file_system/chapter_outlines/vol_{volume:02d}/chapter_{ch_num:03d}.md",
        })
        ch_num += 1
    print(f"  -> Adjusted {len(written)} chapter outlines.")
    return {"adjustment_note": f"Adjusted {len(written)} chapter outlines per the instruction.", "artifacts": written}


def _raw_chapter_dir(ws, volume):
    return os.path.join(ws.file_system, "drafts", f"vol_{volume:02d}", "raw_chapters")


def _raw_chapter_backup_path(ws, volume, chapter_num):
    return resolve_chapter_draft_path(_raw_chapter_dir(ws, volume), chapter_num, raw=True)


def _backup_raw_chapter(ws, volume, chapter_num, content):
    """Save the draft from before the last polish, and archive distinct older snapshots under versions."""
    raw_dir = _raw_chapter_dir(ws, volume)
    existing_path = resolve_chapter_draft_path(raw_dir, chapter_num, raw=True)
    write_path = chapter_draft_write_path(raw_dir, chapter_num, raw=True)
    previous = _read_file(existing_path)
    current = str(content or "").strip()
    if previous is not None and previous != current and os.path.isfile(existing_path):
        import shutil
        versions_dir = os.path.join(raw_dir, "versions")
        os.makedirs(versions_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = os.path.basename(existing_path)
        if stem.endswith(".raw.md"):
            version_name = stem[:-len(".raw.md")] + f"_{stamp}.raw.md"
        else:
            version_name = f"{stem}_{stamp}"
        shutil.copy2(existing_path, os.path.join(versions_dir, version_name))
    _write_file(write_path, content)
    remove_legacy_chapter_draft(raw_dir, chapter_num, raw=True)
    return write_path


_PARAGRAPH_PAIR_CLOSERS = {
    "“": "”", "‘": "’", "「": "」", "『": "』",
    "《": "》", "〈": "〉", "（": "）", "(": ")",
    "【": "】", "[": "]", "〔": "〕",
}
_PARAGRAPH_SENTENCE_ENDS = frozenset("。！？!?")


def _chapter_sentence_units(paragraph):
    """Split on complete sentences; punctuation inside paired marks is not a boundary; dialogue stays whole."""
    text = str(paragraph or "")
    if not text:
        return []
    units = []
    buffer = []
    closers = []
    for index, char in enumerate(text):
        buffer.append(char)
        expected = _PARAGRAPH_PAIR_CLOSERS.get(char)
        if expected:
            closers.append(expected)
        elif closers and char == closers[-1]:
            closers.pop()

        boundary = not closers and char in _PARAGRAPH_SENTENCE_ENDS
        if not closers and char == "…":
            boundary = index + 1 >= len(text) or text[index + 1] != "…"
        if (
            not closers
            and char in _PARAGRAPH_PAIR_CLOSERS.values()
            and index > 0
            and text[index - 1] in _PARAGRAPH_SENTENCE_ENDS.union({"…"})
        ):
            boundary = True
        if boundary:
            unit = "".join(buffer).strip()
            if unit:
                units.append(unit)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        units.append(tail)
    return units


def _format_chapter_paragraphs(text, target_length=140, max_length=200):
    """Auto-split only overlong prose paragraphs; do not change words; do not cut inside dialogue or paired marks."""
    source = str(text or "").strip()
    if not source:
        return source
    formatted = []
    for paragraph in re.split(r"\n\s*\n", source):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        rendered_lines = []
        # Keep author-written single newlines, but still handle lone overlong prose such as the line after a heading.
        for line in paragraph.splitlines():
            line = line.strip()
            if len(line) <= max_length:
                rendered_lines.append(line)
                continue
            units = _chapter_sentence_units(line)
            if len(units) < 2:
                rendered_lines.append(line)
                continue

            groups = []
            current = ""
            for unit in units:
                if current and (
                    len(current) >= target_length
                    or len(current) + len(unit) > max_length
                ):
                    groups.append(current)
                    current = unit
                else:
                    current += unit
            if current:
                groups.append(current)
            # Avoid a too-short orphan at the end; merge back only if the merge still fits the max length.
            if len(groups) > 1 and len(groups[-1]) < 60 and len(groups[-2]) + len(groups[-1]) <= max_length:
                tail = groups.pop()
                groups[-1] += tail
            rendered_lines.append("\n\n".join(groups))
        formatted.append("\n".join(rendered_lines))
    return "\n\n".join(formatted)


_CHAPTER_FORBIDDEN_STYLE_PATTERNS = (
    ("em dash '——'", re.compile(r"——")),
    (
        "Chinese not-X-but-Y contrast template",
        re.compile(r"(?:不是|并非)[^。！？\n]{0,60}?(?:而是|却是)"),
    ),
    (
        "Chinese not-only-X-but-also-Y template",
        re.compile(r"(?:不仅|不只是)[^。！？\n]{0,60}?(?:而且|更(?:是|加)?)"),
    ),
    (
        "not X but Y contrast template",
        re.compile(r"\bnot\b(?!\s+only\b)[^.\n]{0,60}?\bbut\b", re.I),
    ),
    (
        "not only X but also Y template",
        re.compile(r"\bnot only\b[^.\n]{0,60}?\bbut(?:\s+also)?\b", re.I),
    ),
)


def _chapter_style_violations(text):
    """Return typical AI templates that the draft must rewrite; detect only, do not mechanically rewrite meaning."""
    violations = []
    for label, pattern in _CHAPTER_FORBIDDEN_STYLE_PATTERNS:
        matches = list(pattern.finditer(text or ""))
        if not matches:
            continue
        examples = []
        for match in matches[:3]:
            start = max(0, match.start() - 18)
            end = min(len(text or ""), match.end() + 18)
            examples.append(re.sub(r"\s+", " ", (text or "")[start:end]).strip())
        violations.append({
            "label": label,
            "count": len(matches),
            "examples": examples,
        })
    return violations


def _repair_chapter_style(llm, chapter_text, violations, cancel_event=None, max_attempts=2):
    """Do a targeted rewrite of clearly matched forbidden templates until the hard check passes or it clearly fails."""
    current = chapter_text
    current_violations = violations
    for attempt in range(1, max_attempts + 1):
        issue_text = "\n".join(
            f"- {item['label']}: {item['count']} hits; examples: "
            + "｜".join(item["examples"])
            for item in current_violations
        )
        prompt = PromptLoader.load(
            "chapter_style_repair",
            chapter_text=current,
            violations=issue_text,
        )
        candidate = normalize_text(
            _generate_with_cancel(llm, prompt, cancel_event, temperature=0.2)
        )
        if candidate:
            current = candidate
        current_violations = _chapter_style_violations(current)
        if not current_violations:
            return current
        print(
            f"  -> Draft-style hard check still has violations after rewrite {attempt}:"
            f" {sum(item['count'] for item in current_violations)} hits."
        )
    labels = "、".join(item["label"] for item in current_violations)
    raise RuntimeError(f"Draft style hard check failed ({labels}); write stopped. Please retry this chapter.")


def _load_system_prompt_guide(ws, project_root):
    custom = _read_file(os.path.join(ws.file_system, "writing", "system_prompt.md"))
    if custom:
        return custom
    return _read_file(os.path.join(project_root, "core", "system_prompt.md"))


def _humanize_chapter_text(
    llm,
    ws,
    volume,
    chapter_num,
    chapter_text,
    cancel_event=None,
):
    _backup_raw_chapter(ws, volume, chapter_num, chapter_text)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    writing_guide = (
        _load_system_prompt_guide(ws, project_root)
        or "(no extra prose guide; keep the author voice already in the draft to refine.)"
    )
    prompt = PromptLoader.load(
        "humanize_chapter",
        chapter_text=chapter_text,
        writing_guide=writing_guide,
    )
    result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event))
    result = result or chapter_text
    violations = _chapter_style_violations(result)
    if violations:
        print(
            "  -> AI polish still hits the style hard check; starting a targeted rewrite: "
            + ", ".join(f"{item['label']} {item['count']} hits" for item in violations)
        )
        result = _repair_chapter_style(
            llm, result, violations, cancel_event=cancel_event,
        )
    return result


def gen_serial_chapters(
    ws,
    volume=1,
    start_chapter=1,
    max_chapters=None,
    humanize=True,
    humanize_existing=False,
    end_chapter=None,
    regenerate_existing=False,
    writing_instruction="",
    refinement_mode="regenerate",
    progress_callback=None,
    pause_event=None,
    stop_event=None,
    cancel_event=None,
):
    """Generate drafts serially: story-arc unit, chapter outline, prior prose, chapter panel, and writing guide for the next chapter."""
    # Project root
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    context = _load_volume_outline_context(ws, volume)
    if not context:
        return
    # Load writing-style guide (workspace first)
    custom_style_path = os.path.join(ws.file_system, "writing", "system_prompt.md")
    style_guide = _load_system_prompt_guide(ws, _root) or ""
    agents_md = _read_file(os.path.join(_root, "core", "agents.md")) or ""
    writing_rules = f"{style_guide}\n\n{agents_md}" if style_guide or agents_md else "(no writing-style guide)"
    hard_style_rules = (
        "=== Hard style constraints for this draft (highest priority) ===\n"
        "1. Do not use not-X-but-Y contrast templates.\n"
        "2. Do not use not-only-X-but-also-Y templates.\n"
        "3. Do not use em dashes. For a pause use a comma, a period, or split the sentence.\n"
        "4. If the reference novel, chapter outline, prior prose, or writing-guide examples use those patterns, treat them as counterexamples; do not copy them.\n"
    )
    writing_rules = f"{writing_rules}\n\n{hard_style_rules}"
    print(
        "  -> Loaded writing guide: "
        f"{'workspace prose guide' if _read_file(custom_style_path) else 'core/system_prompt.md'} {len(style_guide)} chars; "
        f"core/agents.md {len(agents_md)} chars."
    )
    if not style_guide and not agents_md:
        print("     Warning: no writing guide loaded; draft generation will lack style constraints.")

    # Scan chapter outlines
    outlines_dir = os.path.join(ws.file_system, "chapter_outlines", f"vol_{volume:02d}")
    if not os.path.isdir(outlines_dir):
        print(f"Error: chapter-outline directory not found: {outlines_dir}. Run chapter-outlines first.")
        return

    outline_files = sorted(f for f in os.listdir(outlines_dir) if re.match(r'^chapter_\d+\.md$', f))
    if not outline_files:
        print(f"Error: chapter-outline directory is empty. Run chapter-outlines first.")
        return

    # Infer total chapter count
    total_chapters = 0
    for f in outline_files:
        m = re.match(r'^chapter_(\d+)\.md$', f)
        if m:
            total_chapters = max(total_chapters, int(m.group(1)))

    print(f">>> Generating drafts serially: volume {volume}, {total_chapters} chapters <<<")

    llm = _get_lite_llm()
    if not llm:
        return

    def humanize_with_controls(ch_num, text, completed, total):
        while True:
            try:
                return _humanize_chapter_text(
                    llm, ws, volume, ch_num, text, cancel_event=cancel_event,
                )
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    return None
                if progress_callback:
                    progress_callback("paused", completed, total, f"Chapter {ch_num} polish paused; continue to polish this chapter again")
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
    out_dir = os.path.join(ws.file_system, "chapters", f"vol_{volume:02d}")
    os.makedirs(out_dir, exist_ok=True)

    # Decide which chapters to process
    tasks = []
    finalized = _finalized_chapter_numbers(ws, "drafts", volume)
    range_end = min(total_chapters, end_chapter or total_chapters)
    effective_start = start_chapter
    if regenerate_existing or humanize_existing:
        effective_start = max(
            effective_start,
            _finalized_chapter_boundary(
                ws, "drafts", volume, start_chapter, range_end,
            ) + 1,
        )
    for ch_num in range(effective_start, range_end + 1):
        out_file = resolve_chapter_draft_path(out_dir, ch_num)
        if os.path.exists(out_file):
            if ch_num in finalized:
                print(f"  Chapter {ch_num} draft is marked final; skipping.")
                continue
            if humanize and humanize_existing:
                tasks.append(("humanize_existing", ch_num))
            elif regenerate_existing:
                tasks.append(("generate", ch_num))
            else:
                print(f"  Chapter {ch_num} draft already exists; skipping.")
            if max_chapters and len(tasks) >= max_chapters:
                break
            continue
        tasks.append(("generate", ch_num))
        if max_chapters and len(tasks) >= max_chapters:
            break

    if not tasks:
        print("[Orchestrator] No chapters left to generate (all already exist).")
        if humanize and not humanize_existing:
            print("  To humanize existing drafts, use --humanize-existing.")
        return {
            "adjustment_note": "The selected range has no drafts to process; chapters marked final stay unchanged.",
            "artifacts": [],
            "stopped": False,
        }

    generate_count = sum(1 for mode, _ in tasks if mode == "generate")
    existing_count = len(tasks) - generate_count
    range_text = f"chapters {tasks[0][1]}-{tasks[-1][1]}"
    if existing_count:
        print(f"  Pending: {len(tasks)} chapters ({range_text}; generate {generate_count}, humanize existing {existing_count})")
    else:
        print(f"  To generate: {generate_count} chapters ({range_text})")

    processed_chapters = []
    for idx, (task_mode, ch_num) in enumerate(tasks):
        if ch_num in _finalized_chapter_numbers(ws, "drafts", volume):
            if progress_callback:
                progress_callback("generating", idx + 1, len(tasks), f"Chapter {ch_num} marked final; skipped")
            continue
        if stop_event is not None and stop_event.is_set():
            break
        if pause_event is not None:
            if not pause_event.is_set() and progress_callback:
                progress_callback("paused", idx, len(tasks), "Draft generation paused")
            pause_event.wait()
        if stop_event is not None and stop_event.is_set():
            break
        existing_path = resolve_chapter_draft_path(out_dir, ch_num)
        if progress_callback:
            progress_callback("generating", idx, len(tasks), f"Processing chapter {ch_num} draft")

        if task_mode == "generate":
            print(f"\n--- Writing chapter {ch_num} ({idx + 1}/{len(tasks)}) ---")
        else:
            print(f"\n--- Humanizing chapter {ch_num} ({idx + 1}/{len(tasks)}) ---")

        if task_mode == "humanize_existing":
            existing_text = _read_file(existing_path)
            if not existing_text:
                print(f"  Warning: chapter {ch_num} draft is empty; skipping.")
                continue
            result = humanize_with_controls(ch_num, existing_text, idx, len(tasks))
            if result is None:
                break
            result = _format_chapter_paragraphs(result)
            out_file = _write_draft_chapter(out_dir, ch_num, result)
            processed_chapters.append(ch_num)
            if progress_callback:
                progress_callback("generating", idx + 1, len(tasks), f"Chapter {ch_num} draft polished and written")
            print(f"  -> Chapter {ch_num} draft humanized and saved: {out_file}")
            print(f"     Raw backup: {_raw_chapter_backup_path(ws, volume, ch_num)}")
            continue

        # Read this chapter outline
        chapter_outline = _read_file(os.path.join(outlines_dir, f"chapter_{ch_num:03d}.md"))
        if not chapter_outline:
            print(f"  Warning: chapter {ch_num} outline file does not exist; skipping.")
            continue
        chapter_outline = re.sub(r'\n?\[(?:FINISHED|CONTINUE)\]\s*$', '', chapter_outline).strip()

        current_draft_section = ""
        if regenerate_existing and refinement_mode == "revise":
            current_draft = _read_file(existing_path)
            if current_draft:
                current_draft_section = (
                    "=== Current chapter original draft (revise from this) ===\n"
                    f"{current_draft.strip()}\n\n"
                )

        # Read the previous 2 chapter drafts (not truncated)
        prev_texts = []
        for i in range(max(1, ch_num - 2), ch_num):
            prev_file = resolve_chapter_draft_path(out_dir, i)
            content = _read_file(prev_file)
            if content:
                prev_texts.append(content.strip())
        history_section = "\n\n".join(prev_texts) if prev_texts else "(no prior prose; this is chapter 1)"

        # Read the current-flow story-arc unit for this chapter.
        story_arc_summary = _find_story_arc_for_chapter(ws, volume, ch_num)

        # Chapter-start panel is the state after the previous chapter; this chapter panel is the end-state planned by the outline.
        # The draft must write the change between them; do not treat this chapter panel as the opening state.
        panel_section = ""
        if system_panel_status(ws)["enabled"]:
            previous_panel = _previous_system_panel(ws, volume, ch_num)
            current_panel = _read_json_file(
                _system_panel_chapter_path(ws, volume, ch_num)
            )
            if not current_panel:
                raise RuntimeError(
                    f"Chapter {ch_num} has the system panel enabled, but this chapter's panel is missing. "
                    "Please regenerate or sync this chapter outline and system panel first."
                )
            panel_section = (
                "=== System-panel state change ===\n"
                "[Chapter-start state (after previous chapter)]\n"
                f"{json.dumps(previous_panel, ensure_ascii=False, indent=2)}\n\n"
                "[Chapter-end target (after this chapter)]\n"
                f"{json.dumps(current_panel, ensure_ascii=False, indent=2)}\n\n"
            )

        context = (
            f"=== Writing guide ===\n{writing_rules}\n\n"
            f"=== Current story-arc unit ===\n{story_arc_summary or '(story-arc unit not found; follow the chapter outline strictly)'}\n\n"
            f"=== Prior prose (continuity only; must not override this chapter outline) ===\n{history_section}\n\n"
            + panel_section
            + current_draft_section
            + f"=== Current chapter outline (chapter {ch_num}, the only plot blueprint) ===\n{chapter_outline}\n\n"
            + (f"=== User adjustment this round ===\n{writing_instruction}\n\n" if writing_instruction else "")
            + "=== Final execution reminder ===\n"
            + "Output only this chapter's title and prose; follow the chapter outline strictly; if the system panel is enabled, "
              "the prose must naturally show the change from chapter start to chapter end, and must not apply the end state early; "
              "do not copy prior prose; silently check every hard forbidden rule before output."
        )

        prompt = PromptLoader.load(
            "adaptive_drafting",
            context=context,
            start_chapter=ch_num,
            end_chapter=ch_num,
            chapter_count=1,
        )
        while True:
            try:
                result = normalize_text(_generate_with_cancel(llm, prompt, cancel_event))
                break
            except LLMCallCancelled:
                if stop_event is not None and stop_event.is_set():
                    result = None
                    break
                if progress_callback:
                    progress_callback("paused", idx, len(tasks), f"Chapter {ch_num} generation paused; continue to regenerate this chapter")
                if pause_event is not None:
                    pause_event.wait()
                if cancel_event is not None:
                    cancel_event.clear()
        if result is None:
            break
        if not str(result).strip():
            print(f"  Warning: chapter {ch_num} draft got no model output; not written. You can retry.")
            continue
        if humanize:
            print(f"  Humanizing chapter {ch_num} draft...")
            result = humanize_with_controls(ch_num, result, idx, len(tasks))
            if result is None:
                break
            if not str(result).strip():
                print(f"  Warning: chapter {ch_num} humanize got no model output; not written. You can retry.")
                continue
        result = _format_chapter_paragraphs(result)
        if regenerate_existing and os.path.isfile(existing_path):
            import shutil
            backup_dir = os.path.join(out_dir, "versions")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"{os.path.basename(existing_path)}_{stamp}")
            if not os.path.exists(backup_path):
                shutil.copy2(existing_path, backup_path)
        out_file = _write_draft_chapter(out_dir, ch_num, result)
        processed_chapters.append(ch_num)
        if progress_callback:
            progress_callback("generating", idx + 1, len(tasks), f"Chapter {ch_num} draft written")
        if humanize:
            print(f"  -> Chapter {ch_num} draft saved: {out_file}")
            print(f"     Raw backup: {_raw_chapter_backup_path(ws, volume, ch_num)}")
        else:
            print(f"  -> Chapter {ch_num} draft saved: {out_file}")

    completed = 0
    artifacts = []
    for ch_num in processed_chapters:
        path = resolve_chapter_draft_path(out_dir, ch_num)
        if _read_file(path):
            completed += 1
            artifacts.append({
                "label": f"chapter {ch_num} draft",
                "path": f"file_system/chapters/vol_{volume:02d}/{os.path.basename(path)}",
            })
    stopped = stop_event is not None and stop_event.is_set()
    print(f"\n  -> Volume {volume} drafts processed ({completed} chapters).")
    return {
        "adjustment_note": (
            f"This round of draft generation ended; finished {completed}/{len(tasks)} chapters."
            if stopped else (
                f"Finished {completed} chapter drafts"
                + (
                    f"({'full regenerate' if refinement_mode == 'regenerate' else 'revise from current content'})"
                    if regenerate_existing else ""
                )
                + "。"
            )
        ),
        "artifacts": artifacts,
        "stopped": stopped,
    }


def chapter_draft_resume_status(ws, volume, arc_idx):
    arc = next((item for item in _list_novel_story_arcs(ws, volume) if item["idx"] == arc_idx), None)
    if not arc:
        return {"can_resume": False, "completed": 0, "total": 0, "next_chapter": None}
    out_dir = os.path.join(ws.file_system, "chapters", f"vol_{volume:02d}")
    chapters = list(range(arc["start_ch"], arc["end_ch"] + 1))
    existing = [ch for ch in chapters if _read_file(resolve_chapter_draft_path(out_dir, ch))]
    missing = [ch for ch in chapters if ch not in existing]
    return {
        "can_resume": bool(existing and missing), "completed": len(existing), "total": len(chapters),
        "next_chapter": missing[0] if missing else None,
    }


def route_chapter_draft_refinement(ws, volume, arc_idx, instruction, cancel_event=None):
    arc = next((item for item in _list_novel_story_arcs(ws, volume) if item["idx"] == arc_idx), None)
    if not arc:
        raise ValueError("Story-arc units not found.")
    out_dir = os.path.join(ws.file_system, "chapters", f"vol_{volume:02d}")
    current = []
    for ch in range(arc["start_ch"], arc["end_ch"] + 1):
        text = _read_file(resolve_chapter_draft_path(out_dir, ch))
        if text:
            current.append(f"[Chapter {ch} draft]\n{text}")
    llm = _get_lite_llm()
    if not llm:
        raise RuntimeError("No usable model is configured.")
    prompt = PromptLoader.load(
        "chapter_draft_refine_route",
        start_chapter=arc["start_ch"], end_chapter=arc["end_ch"],
        current_chapters="\n\n===\n\n".join(current),
        instruction=instruction,
    )
    routed = parse_json_response(normalize_text(_generate_with_cancel(llm, prompt, cancel_event, temperature=0.2)))
    if not isinstance(routed, dict):
        routed = {}
    try:
        start = int(routed.get("start_chapter"))
    except (TypeError, ValueError):
        start = arc["start_ch"]
    start = min(arc["end_ch"], max(arc["start_ch"], start))
    mode = _normalize_refinement_mode(routed.get("mode"), instruction)
    return start, mode, str(routed.get("reason") or "Located the earliest affected chapter from the user request.")
