import hashlib
import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.prompt_loader import PromptLoader
from core.text_utils import normalize_text


SUPPORTED_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
}

CHAPTER_HEADER_RE = re.compile(
    r'^[ \t　]*(?:(?:\d+\.)?第[一二三四五六七八九十百千零\d]+[章回节]'
    r'(?:\s*[（(]\d+[）)])?\s*.+|(?:Chapter|Ch\.?)\s+\d+\b.*)',
    re.MULTILINE | re.IGNORECASE,
)
CHAPTER_HEADER_FALLBACK = re.compile(
    r'(^[ \t　]*(?:第[一二三四五六七八九十百千零0-9]+[章回节].{0,60}?'
    r'|(?:Chapter|Ch\.?)\s+\d+\b.{0,60}?)\n)',
    re.MULTILINE | re.IGNORECASE,
)
VOLUME_TITLE_RE = re.compile(
    r'^[ \t　]*(?:第[一二三四五六七八九十百千零0-9]+卷\b|(?:Volume|Book|Vol\.?)\s+\d+\b)',
    re.IGNORECASE,
)

WORLD_SECTIONS = [
    ("世界观", "Cosmos structure, era background, heaven-and-earth rules, historical stages, core conflict, and world-running logic."),
    ("力量体系", "Cultivation ranks, realm structure, power sources, promotion methods, combat-power differences, and limits."),
    ("关键人物", "Important characters' identities, stances, relations, abilities, plot roles, and main experiences."),
    ("势力描述", "Sects, churches, peoples, dynasties, alliances: positioning, relations, interests, and conflicts."),
    ("故事主线", "Mainline events, staged conflicts, causal chains, turns, key battles, or key plot advances."),
    ("关键物品", "Artifacts, resources, tools, divine items, pills, and cultivation-art carriers: attributes, ownership, and plot role."),
    ("技能体系", "Spells, divine abilities, cultivation arts, secret arts, formations, forging/refining, and usage rules."),
]

SECTION_LOOKUP = dict(WORLD_SECTIONS)
WORLD_SECTION_NAMES = tuple(name for name, _ in WORLD_SECTIONS)
CANON_INDEX_SECTIONS = (
    "公共人物", "公共势力", "公共地点", "公共事件与历史线",
    "公共物品与法宝", "公共技能与法术", "力量体系关键词",
    "世界观规则关键词", "待补充类别",
)

# English headings from generation; stored under the Chinese canonical key.
_HEADING_ALIASES = {
    "Worldview": "世界观",
    "Power system": "力量体系",
    "Key characters": "关键人物",
    "Factions": "势力描述",
    "Story spine": "故事主线",
    "Key items": "关键物品",
    "Skills and techniques": "技能体系",
    "Shared characters": "公共人物",
    "Shared factions": "公共势力",
    "Shared places": "公共地点",
    "Shared events and history": "公共事件与历史线",
    "Shared items and artifacts": "公共物品与法宝",
    "Shared skills and spells": "公共技能与法术",
    "Power-system keywords": "力量体系关键词",
    "Worldview-rule keywords": "世界观规则关键词",
    "To be filled": "待补充类别",
}
_EMPTY_SECTION_BODIES = {"", "无", "none", "none.", "(none)"}


def _heading_names(section_name):
    names = [section_name]
    for alias, canonical in _HEADING_ALIASES.items():
        if canonical == section_name:
            names.append(alias)
    return names


def _heading_re(section_name):
    alt = "|".join(re.escape(n) for n in _heading_names(section_name))
    return re.compile(rf"(?m)^#\s*(?:{alt})\s*$", re.I)


# ── path helpers ──

def _world_root(ws):
    return os.path.join(ws.file_system, "world_knowledge")


def _imports_dir(ws):
    return os.path.join(_world_root(ws), "imports")


def _cards_dir(ws):
    return os.path.join(_world_root(ws), "cards")


def _partials_dir(ws):
    return os.path.join(_world_root(ws), "partials")


def _worlds_dir(ws):
    return os.path.join(_world_root(ws), "worlds")


def _audits_dir(ws):
    return os.path.join(_world_root(ws), "audits")


def _manifest_path(ws):
    return os.path.join(_world_root(ws), "manifest.json")


# ── file IO ──

def _read_file(path):
    for encoding in ["utf-8", "utf-8-sig", "gb18030"]:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _require_headings(content, headings, label):
    missing = [
        heading for heading in headings
        if not _heading_re(heading).search(content or "")
    ]
    if missing:
        raise RuntimeError(f"{label} is missing required sections: {', '.join(missing)}. This round was not written; please retry.")


def _has_meaningful_file(path):
    return bool(os.path.exists(path) and _read_file(path).strip())


def _load_fresh_checkpoint(path, dependencies, required_headings, force=False):
    """Read a still-valid stage checkpoint; return empty if deps changed or the file is corrupt."""
    if force or not _has_meaningful_file(path):
        return ""
    dependency_paths = [item for item in dependencies if item and os.path.exists(item)]
    if dependency_paths and os.path.getmtime(path) < max(os.path.getmtime(item) for item in dependency_paths):
        return ""
    content = _read_file(path).strip()
    try:
        _require_headings(content, required_headings, "checkpoint file")
    except RuntimeError:
        return ""
    return content


def _run_prompt(llm, folder, prompt_vars, output_path, required_headings=None):
    """Load prompt -> llm.generate -> normalize_text -> _write_file; return normalized text.

    Pure generate-and-write. No print, no mtime skip, no read-back.
    Print order / skip branches / return semantics (path vs content) stay at the call site.
    """
    prompt = PromptLoader.load(folder, **prompt_vars)
    content = normalize_text(llm.generate(prompt))
    if not content:
        raise RuntimeError(f"{folder} did not return valid content. This round was not written; check the model config or retry.")
    if required_headings:
        _require_headings(content, required_headings, folder)
    _write_file(output_path, content)
    return content


# ── Manifest and source naming ──

def _load_manifest(ws):
    path = _manifest_path(ws)
    if not os.path.exists(path):
        return {"version": 1, "sources": [], "enabled": True}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "sources": [], "enabled": True}
        data.setdefault("version", 1)
        data.setdefault("sources", [])
        # enabled defaults to True (imported sources are treated as enabled); backfill old manifests.
        if "enabled" not in data:
            data["enabled"] = True
        return data
    except Exception:
        return {"version": 1, "sources": [], "enabled": True}


def _save_manifest(ws, manifest):
    _write_file(_manifest_path(ws), json.dumps(manifest, ensure_ascii=False, indent=2))


def _iter_source_files(paths):
    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.exists(path):
            yield path, False
            continue
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    file_path = os.path.join(root, name)
                    ext = os.path.splitext(file_path)[1].lower()
                    if ext in SUPPORTED_TEXT_EXTS:
                        yield file_path, True
        else:
            ext = os.path.splitext(path)[1].lower()
            yield path, ext in SUPPORTED_TEXT_EXTS


def _source_id(path):
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]


def _safe_name(name):
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("_")
    return name or "source.txt"


def _source_name(record):
    file_name = record.get("file_name") or "source"
    stem = os.path.splitext(file_name)[0] or file_name
    return _safe_name(stem)


def _section_file_name(section_name):
    return f"{_safe_name(section_name)}.md"


def _source_world_dir(ws, record):
    return os.path.join(_worlds_dir(ws), _source_name(record))


def _source_section_path(ws, record, section_name):
    return os.path.join(_source_world_dir(ws, record), _section_file_name(section_name))


def _source_partials_dir(ws, record):
    return os.path.join(_partials_dir(ws), _source_name(record))


def _final_partials_dir(ws):
    return os.path.join(_partials_dir(ws), "_final")


def _final_world_dir(ws):
    return os.path.join(_worlds_dir(ws), "_final")


def _final_section_path(ws, section_name):
    return os.path.join(_final_world_dir(ws), _section_file_name(section_name))


def _canon_index_path(ws):
    return os.path.join(_world_root(ws), "canon_index.md")


# ── public entry: import sources ──

def import_world_sources(ws, paths, force=False):
    """Copy one or more target-world source files into the workspace."""
    os.makedirs(_imports_dir(ws), exist_ok=True)
    manifest = _load_manifest(ws)
    by_source = {item.get("source_path"): item for item in manifest.get("sources", [])}

    imported = []
    skipped = []
    missing = []
    unsupported = []

    for source_path, supported in _iter_source_files(paths):
        if not os.path.exists(source_path):
            missing.append(source_path)
            continue
        if not supported:
            unsupported.append(source_path)
            continue

        abs_path = os.path.abspath(source_path)
        existing = by_source.get(abs_path)
        if existing and not force and os.path.exists(existing.get("imported_path", "")):
            skipped.append(abs_path)
            continue

        sid = _source_id(abs_path)
        dest_name = f"{sid}_{_safe_name(os.path.basename(abs_path))}"
        dest_path = os.path.join(_imports_dir(ws), dest_name)
        shutil.copy2(abs_path, dest_path)

        record = {
            "id": sid,
            "source_path": abs_path,
            "imported_path": dest_path,
            "file_name": os.path.basename(abs_path),
            "size": os.path.getsize(dest_path),
            "mtime": os.path.getmtime(dest_path),
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        }

        if existing:
            manifest["sources"] = [
                record if item.get("source_path") == abs_path else item
                for item in manifest["sources"]
            ]
        else:
            manifest["sources"].append(record)
        by_source[abs_path] = record
        imported.append(abs_path)

    _save_manifest(ws, manifest)
    return {
        "imported": imported,
        "skipped": skipped,
        "missing": missing,
        "unsupported": unsupported,
        "manifest": _manifest_path(ws),
    }


# ── text slicing ──

def _split_text(text, chunk_size):
    text = text.strip()
    if not text:
        return []

    paragraphs = re.split(r"\n{2,}", text)
    chunks = []
    current = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > chunk_size:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(paragraph), chunk_size):
                chunks.append(paragraph[i:i + chunk_size])
            continue
        projected = current_len + len(paragraph) + 2
        if current and projected > chunk_size:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len = projected

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _find_chapters(text):
    matches = list(CHAPTER_HEADER_RE.finditer(text))
    if not matches:
        parts = CHAPTER_HEADER_FALLBACK.split(text)
        chapters = []
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            content = normalize_text(f"{title}\n{body}")
            if VOLUME_TITLE_RE.match(title) or len(content) < 80:
                continue
            chapters.append({"title": title, "content": content})
        return chapters

    chapters = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = normalize_text(text[start:end])
        first_newline = content.find("\n")
        title = content[:first_newline].strip() if first_newline != -1 else content[:60]
        if VOLUME_TITLE_RE.match(title) or len(content) < 80:
            continue
        chapters.append({"title": title, "content": content})
    return chapters


def _split_source_slices(text, chapter_batch_size, fallback_chunk_size):
    chapters = _find_chapters(text)
    if len(chapters) >= 2:
        slices = []
        batch = []
        batch_chars = 0
        batch_start = 1

        def flush_batch(end_index):
            nonlocal batch, batch_chars
            if not batch:
                return
            slices.append({
                "label": f"Chapters {batch_start}-{end_index}",
                "kind": "chapter_batch",
                "text": "\n\n".join(ch["content"] for ch in batch),
                "chapter_count": len(batch),
            })
            batch = []
            batch_chars = 0

        for chapter_index, chapter in enumerate(chapters, start=1):
            chapter_chars = len(chapter["content"])
            over_count = len(batch) >= chapter_batch_size
            over_chars = bool(batch) and batch_chars + chapter_chars + 2 > fallback_chunk_size
            if over_count or over_chars:
                flush_batch(chapter_index - 1)
                batch_start = chapter_index
            batch.append(chapter)
            batch_chars += chapter_chars + (2 if len(batch) > 1 else 0)
        flush_batch(len(chapters))
        return slices

    chunks = _split_text(text, fallback_chunk_size)
    return [
        {
            "label": f"character slice {i + 1}",
            "kind": "text_chunk",
            "text": chunk,
            "chapter_count": 0,
        }
        for i, chunk in enumerate(chunks)
    ]


def _format_knowledge_items(items):
    return "\n\n---\n\n".join(
        f"【{title}】\n{text.strip()}"
        for title, text in items
        if text and text.strip()
    )


# ── source records and roles ──

def _source_records(ws):
    manifest = _load_manifest(ws)
    records = []
    for item in manifest.get("sources", []):
        imported_path = item.get("imported_path")
        if imported_path and os.path.exists(imported_path):
            records.append(item)
    if records:
        return records

    imports_dir = _imports_dir(ws)
    if not os.path.isdir(imports_dir):
        return []
    fallback = []
    for name in sorted(os.listdir(imports_dir)):
        path = os.path.join(imports_dir, name)
        if os.path.isfile(path) and os.path.splitext(path)[1].lower() in SUPPORTED_TEXT_EXTS:
            fallback.append({
                "id": _source_id(path),
                "source_path": path,
                "imported_path": path,
                "file_name": name,
                "size": os.path.getsize(path),
                "mtime": os.path.getmtime(path),
            })
    return fallback


def _normalize_source_match(value):
    return os.path.abspath(os.path.expanduser(value)).lower() if value else ""


def _record_matches_selector(record, selector):
    if not selector:
        return False

    selector_raw = selector.strip()
    selector_lower = selector_raw.lower()
    selector_abs = _normalize_source_match(selector_raw)
    candidates = [
        record.get("id", ""),
        record.get("file_name", ""),
        os.path.splitext(record.get("file_name", ""))[0],
        record.get("source_path", ""),
        record.get("imported_path", ""),
        os.path.basename(record.get("source_path", "")),
        os.path.basename(record.get("imported_path", "")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_lower = str(candidate).lower()
        if selector_lower == candidate_lower:
            return True
        if selector_abs and selector_abs == _normalize_source_match(str(candidate)):
            return True
    return False


def _select_primary_record(records, primary_source=None):
    if primary_source:
        matches = [record for record in records if _record_matches_selector(record, primary_source)]
        if len(matches) == 1:
            return matches[0], "user-specified"
        if len(matches) > 1:
            print(
                "  Warning: --primary matched multiple sources; using the first: "
                + ", ".join(record.get("file_name", "") for record in matches)
            )
            return matches[0], "user-specified"
        print(f"  Warning: --primary source not found: {primary_source}")
        print(
            "  Available sources: "
            + ", ".join(record.get("file_name", record.get("id", "")) for record in records)
        )
        print("  Falling back to the largest file as the primary source.")
    return max(records, key=lambda item: item.get("size", 0)), "largest file"


def _assign_source_roles(records, primary_source=None):
    if not records:
        return []
    primary, primary_reason = _select_primary_record(records, primary_source=primary_source)
    primary_id = primary.get("id")
    assigned = []
    for item in records:
        copied = dict(item)
        copied["role"] = "primary" if item.get("id") == primary_id else "supplement"
        copied["role_label"] = "primary source" if copied["role"] == "primary" else "supplement source"
        copied["role_reason"] = primary_reason if copied["role"] == "primary" else ""
        assigned.append(copied)
    return sorted(
        assigned,
        key=lambda item: (0 if item["role"] == "primary" else 1, item.get("file_name", "")),
    )


# ── section document handling ──

def _render_section_document(section_name, content):
    content = (content or "").strip()
    if content.startswith("#"):
        _, _, rest = content.partition("\n")
        body = rest.strip()
    else:
        body = content
    if body.lower() in _EMPTY_SECTION_BODIES:
        body = ""
    if not body:
        return f"# {section_name}\n\nNone"
    return f"# {section_name}\n\n{body}"


def _split_sections_from_document(content):
    sections = {}
    content = normalize_text(content or "")
    for idx, (section_name, _) in enumerate(WORLD_SECTIONS):
        match = _heading_re(section_name).search(content)
        if not match:
            sections[section_name] = f"# {section_name}\n\nNone"
            continue

        next_start = len(content)
        for next_section, _ in WORLD_SECTIONS[idx + 1:]:
            next_match = _heading_re(next_section).search(content[match.end():])
            if next_match:
                next_start = match.end() + next_match.start()
                break
        block = content[match.start():next_start].strip()
        sections[section_name] = _render_section_document(section_name, block)
    return sections


def _compact_world_document(content, max_chars=45000):
    """Compress stage summaries evenly by section so serial merges do not keep growing context."""
    content = normalize_text(content or "")
    if len(content) <= max_chars:
        return content
    sections = _split_sections_from_document(content)
    section_budget = max(1200, max_chars // len(WORLD_SECTIONS) - 80)
    parts = []
    for section_name, _ in WORLD_SECTIONS:
        block = sections.get(section_name, f"# {section_name}\n\nNone")
        if len(block) > section_budget:
            block = block[:section_budget].rstrip() + "\n\n(this section stage summary was too long and was truncated evenly.)"
        parts.append(block)
    return "\n\n---\n\n".join(parts)


def _final_section_paths(ws):
    return [
        (section_name, _final_section_path(ws, section_name))
        for section_name, _ in WORLD_SECTIONS
        if _has_meaningful_file(_final_section_path(ws, section_name))
    ]


def _aggregate_sections(section_paths, max_chars=None):
    parts = []
    section_budget = None
    if max_chars and section_paths:
        section_budget = max(1200, max_chars // len(section_paths) - 80)

    for section_name, path in section_paths:
        content = _read_file(path).strip() if os.path.exists(path) else ""
        if section_budget and len(content) > section_budget:
            content = (
                content[:section_budget]
                + f"\n\n({section_name} was too long; the above is a truncated front summary.)"
            )
        parts.append(_render_section_document(section_name, content))
    result = "\n\n---\n\n".join(parts)
    if max_chars and len(result) > max_chars:
        return result[:max_chars] + "\n\n(target world knowledge base was too long; the above is a truncated sectioned summary.)"
    return result


def _source_slices(record, chunk_size, chapter_batch_size):
    source_text = _read_file(record["imported_path"]).strip()
    return _split_source_slices(
        source_text,
        chapter_batch_size=chapter_batch_size,
        fallback_chunk_size=chunk_size,
    )


# ── Cards and baseline index ──

def _resolved_workers(max_workers, task_count):
    try:
        requested = int(max_workers or 4)
    except (TypeError, ValueError):
        requested = 4
    return max(1, min(requested, max(1, task_count)))


def _build_record_cards(ws, record, slices, llm, force=False, canon_index="",
                        dependency_mtime=None, max_workers=4):
    source_mtime = record.get("mtime") or os.path.getmtime(record["imported_path"])
    source_card_paths = []
    pending = []
    for idx, source_slice in enumerate(slices, start=1):
        card_path = os.path.join(_cards_dir(ws), f"{record['id']}_part_{idx:03d}.md")
        source_card_paths.append(card_path)
        card_is_fresh = (
            _has_meaningful_file(card_path)
            and os.path.getmtime(card_path) >= source_mtime
            and (not dependency_mtime or os.path.getmtime(card_path) >= dependency_mtime)
        )
        if card_is_fresh and not force:
            continue

        pending.append((idx, source_slice, card_path))

    reused_count = len(source_card_paths) - len(pending)
    if reused_count:
        print(
            f"  Resuming {record['role_label']} '{record['file_name']}': "
            f"reusing {reused_count}/{len(source_card_paths)} completed source cards"
        )
    if not pending:
        return source_card_paths

    workers = _resolved_workers(max_workers, len(pending))
    print(
        f"  Structuring {record['role_label']} '{record['file_name']}' in parallel: "
        f"{len(pending)} chapter batches, concurrency {workers}"
    )

    def extract_card(job):
        idx, source_slice, card_path = job
        print(
            f"  Starting structure of {record['role_label']}: {record['file_name']} "
            f"{source_slice['label']}（{idx}/{len(slices)}）"
        )
        _run_prompt(
            llm,
            "world_knowledge_extract",
            dict(
                source_name=record["file_name"],
                source_role=record["role_label"],
                canon_index=canon_index or "(none. This source is the primary source, or the primary-source baseline index is not generated yet.)",
                slice_label=source_slice["label"],
                slice_kind=source_slice["kind"],
                chunk_index=idx,
                chunk_count=len(slices),
                source_text=source_slice["text"],
            ),
            card_path,
            required_headings=WORLD_SECTION_NAMES,
        )
        return idx, source_slice["label"]

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="world-card") as executor:
        futures = [executor.submit(extract_card, job) for job in pending]
        try:
            for future in as_completed(futures):
                idx, label = future.result()
                completed += 1
                print(f"  Finished source card {completed}/{len(pending)}: {label} (original batch {idx})")
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return source_card_paths


def _build_canon_index(ws, primary_record, primary_card_paths, llm, force=False):
    existing_cards = [path for path in primary_card_paths if _has_meaningful_file(path)]
    if not existing_cards:
        return ""

    output_path = _canon_index_path(ws)
    newest_card_mtime = max(os.path.getmtime(path) for path in existing_cards)
    if (
        _has_meaningful_file(output_path)
        and not force
        and os.path.getmtime(output_path) >= newest_card_mtime
    ):
        existing_index = _read_file(output_path).strip()
        try:
            _require_headings(existing_index, CANON_INDEX_SECTIONS, "primary-source baseline index")
            print("  -> Reusing the completed primary-source baseline index.")
            return existing_index
        except RuntimeError:
            pass

    print(f"  Generating primary-source baseline index serially: {primary_record['file_name']}")
    partial_dir = os.path.join(_partials_dir(ws), "_canon_index")
    os.makedirs(partial_dir, exist_ok=True)
    previous_index = "(none; this is the first batch of primary-source cards.)"
    previous_checkpoint = None
    total_steps = (len(existing_cards) + 1) // 2
    canon_index = ""
    for step_index, start in enumerate(range(0, len(existing_cards), 2), start=1):
        selected_paths = existing_cards[start:start + 2]
        card_items = [
            (os.path.basename(path), _read_file(path))
            for path in selected_paths
        ]
        checkpoint_path = os.path.join(partial_dir, f"partial_{step_index:03d}.md")
        canon_index = _load_fresh_checkpoint(
            checkpoint_path,
            [*selected_paths, previous_checkpoint],
            CANON_INDEX_SECTIONS,
            force=force,
        )
        if canon_index:
            print(f"  Resuming primary-source baseline index {step_index}/{total_steps}")
        else:
            print(f"  Summarizing primary-source baseline index {step_index}/{total_steps}")
            canon_index = _run_prompt(
                llm,
                "world_canon_index",
                dict(
                    source_name=primary_record["file_name"],
                    previous_index=previous_index,
                    knowledge_cards=_format_knowledge_items(card_items),
                ),
                checkpoint_path,
                required_headings=CANON_INDEX_SECTIONS,
            )
        previous_index = canon_index
        previous_checkpoint = checkpoint_path
    _write_file(output_path, canon_index)
    print(f"  -> Primary-source baseline index saved: {output_path}")
    return canon_index


# ── per-source all-section summary ──

def _write_sections_to_source(ws, record, section_documents):
    section_paths = {}
    for section_name, _ in WORLD_SECTIONS:
        output_path = _source_section_path(ws, record, section_name)
        _write_file(output_path, section_documents.get(section_name, f"# {section_name}\n\nNone"))
        section_paths[section_name] = output_path
    return section_paths


def _write_sections_to_final(ws, section_documents):
    section_paths = {}
    for section_name, _ in WORLD_SECTIONS:
        output_path = _final_section_path(ws, section_name)
        _write_file(output_path, section_documents.get(section_name, f"# {section_name}\n\nNone"))
        section_paths[section_name] = output_path
    return section_paths


def _build_source_all_sections(ws, record, card_paths, llm, force=False):
    existing_cards = [path for path in card_paths if _has_meaningful_file(path)]
    if not existing_cards:
        return None

    source_dir = _source_world_dir(ws, record)
    source_partials_dir = os.path.join(_source_partials_dir(ws, record), "_all_sections")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(source_partials_dir, exist_ok=True)

    newest_card_mtime = max(os.path.getmtime(path) for path in existing_cards)
    section_paths = {
        section_name: _source_section_path(ws, record, section_name)
        for section_name, _ in WORLD_SECTIONS
    }
    if (
        all(_has_meaningful_file(path) for path in section_paths.values())
        and not force
        and min(os.path.getmtime(path) for path in section_paths.values()) >= newest_card_mtime
    ):
        return section_paths

    previous_summary = "(none; this is the first all-section summary for this source.)"
    previous_checkpoint = None
    total_steps = (len(existing_cards) + 1) // 2
    current_summary = ""

    for step_index, start in enumerate(range(0, len(existing_cards), 2), start=1):
        selected_paths = existing_cards[start:start + 2]
        card_items = [
            (os.path.basename(path), _read_file(path))
            for path in selected_paths
        ]
        checkpoint_path = os.path.join(source_partials_dir, f"partial_{step_index:03d}.md")
        current_summary = _load_fresh_checkpoint(
            checkpoint_path,
            [*selected_paths, previous_checkpoint],
            WORLD_SECTION_NAMES,
            force=force,
        )
        if current_summary:
            print(
                f"  Resuming all-section summary for {record['role_label']} '{record['file_name']}' "
                f"{step_index}/{total_steps}"
            )
        else:
            print(
                f"  Summarizing all sections for {record['role_label']} '{record['file_name']}' "
                f"{step_index}/{total_steps}"
            )
            current_summary = _run_prompt(
                llm,
                "world_knowledge_merge_all_sections",
                dict(
                    merge_task=(
                        f"Source-level all-section serial summary: for {record['role_label']} '{record['file_name']}' "
                        "build a complete 7-section source knowledge base. This round only reads 2 new source cards "
                        "and the previous stage summary; dedupe, fill, and correct inside this source. "
                        "Do not rank this source against other sources."
                    ),
                    previous_summary=_compact_world_document(previous_summary),
                    knowledge_cards=_format_knowledge_items(card_items),
                ),
                checkpoint_path,
                required_headings=WORLD_SECTION_NAMES,
            )
        previous_summary = current_summary
        previous_checkpoint = checkpoint_path

    return _write_sections_to_source(ws, record, _split_sections_from_document(current_summary))


def _build_single_source_sections(ws, record, card_paths, llm, force=False, max_workers=None):
    existing_cards = [path for path in card_paths if _has_meaningful_file(path)]
    if not existing_cards:
        return None

    section_paths = _build_source_all_sections(ws, record, existing_cards, llm, force=force) or {}

    index_path = os.path.join(_source_world_dir(ws, record), "README.md")
    index_lines = [
        f"# {record['file_name']} source knowledge base",
        "",
        f"- Source role: {record.get('role_label', 'source')}",
        f"- Source path: {record.get('source_path') or record.get('imported_path')}",
        "",
        "## Sections",
    ]
    for section_name, _ in WORLD_SECTIONS:
        if section_name in section_paths:
            index_lines.append(f"- [{section_name}]({_section_file_name(section_name)})")
    _write_file(index_path, "\n".join(index_lines))
    return {
        "record": record,
        "sections": section_paths,
        "index": index_path,
    }


def _load_existing_source_sections(ws, records):
    source_items = []
    for record in records:
        section_paths = {
            section_name: _source_section_path(ws, record, section_name)
            for section_name, _ in WORLD_SECTIONS
            if _has_meaningful_file(_source_section_path(ws, record, section_name))
        }
        if not section_paths:
            print(f"  Warning: source sectioned knowledge base not found, skipping: {record['file_name']}")
            continue
        source_items.append({
            "record": record,
            "sections": section_paths,
            "index": os.path.join(_source_world_dir(ws, record), "README.md"),
        })
    return source_items


# ── final merge and audit ──

def _integrate_final_sections(ws, source_items, llm, force=False, max_workers=None):
    if not source_items:
        return None

    primary_item = next(
        (item for item in source_items if item["record"].get("role") == "primary"),
        source_items[0],
    )
    supplement_items = [
        item for item in source_items
        if item["record"].get("id") != primary_item["record"].get("id")
    ]

    source_section_paths = [
        path
        for item in source_items
        for path in item.get("sections", {}).values()
        if _has_meaningful_file(path)
    ]
    if not source_section_paths:
        print("Error: failed to generate the final sectioned world knowledge base.")
        return None

    final_paths = {
        section_name: _final_section_path(ws, section_name)
        for section_name, _ in WORLD_SECTIONS
    }
    final_all_partials_dir = os.path.join(_final_partials_dir(ws), "_all_sections")
    has_final_trace = (
        not supplement_items
        or (
            os.path.isdir(final_all_partials_dir)
            and any(name.startswith("integrated_") for name in os.listdir(final_all_partials_dir))
        )
    )
    newest_source_mtime = max(os.path.getmtime(path) for path in source_section_paths)
    if (
        all(_has_meaningful_file(path) for path in final_paths.values())
        and not force
        and min(os.path.getmtime(path) for path in final_paths.values()) >= newest_source_mtime
        and has_final_trace
    ):
        print(f"Target-world sectioned knowledge base already exists: {_final_world_dir(ws)}")
        print("Use --force to rebuild the summary.")
        return _final_world_dir(ws)

    primary_paths = [
        (section_name, primary_item["sections"][section_name])
        for section_name, _ in WORLD_SECTIONS
        if section_name in primary_item.get("sections", {}) and _has_meaningful_file(primary_item["sections"][section_name])
    ]
    current_summary = _aggregate_sections(primary_paths)
    if not supplement_items:
        _write_sections_to_final(ws, _split_sections_from_document(current_summary))
    else:
        os.makedirs(final_all_partials_dir, exist_ok=True)
        previous_checkpoint = None
        for idx, item in enumerate(supplement_items, start=1):
            record = item["record"]
            supplement_paths = [
                (section_name, item["sections"][section_name])
                for section_name, _ in WORLD_SECTIONS
                if section_name in item.get("sections", {}) and _has_meaningful_file(item["sections"][section_name])
            ]
            if not supplement_paths:
                continue
            checkpoint_path = os.path.join(
                final_all_partials_dir, f"integrated_{idx:03d}_{record['id']}.md"
            )
            current_checkpoint = _load_fresh_checkpoint(
                checkpoint_path,
                [*(path for _, path in supplement_paths), previous_checkpoint, *(path for _, path in primary_paths)],
                WORLD_SECTION_NAMES,
                force=force,
            )
            if current_checkpoint:
                print(
                    f"  Resuming final knowledge base: {record['file_name']} "
                    f"{idx}/{len(supplement_items)}"
                )
                current_summary = current_checkpoint
            else:
                print(
                    f"  Merging final knowledge-base all sections: {record['file_name']} "
                    f"{idx}/{len(supplement_items)}"
                )
                current_summary = _run_prompt(
                    llm,
                    "world_knowledge_merge_all_sections",
                    dict(
                        merge_task=(
                            "Final all-section serial merge: the existing stage summary is the primary-source "
                            "7-section knowledge base plus already merged supplement sources. "
                            f"This round only merges the 7-section knowledge base of supplement source '{record['file_name']}'. "
                            "Story spine, core event order, core causality, and basic identity relations follow the primary source. "
                            "Power system, skills and techniques, key items, character power/background, and worldview details "
                            "may be corrected and completed from the supplement source as shared knowledge. "
                            "Exclude the supplement source's original protagonist line, original golden finger, and original mainline tasks."
                        ),
                        previous_summary=_compact_world_document(current_summary),
                        knowledge_cards=_aggregate_sections(supplement_paths, max_chars=40000),
                    ),
                    checkpoint_path,
                    required_headings=WORLD_SECTION_NAMES,
                )
            previous_checkpoint = checkpoint_path
        _write_sections_to_final(ws, _split_sections_from_document(current_summary))

    legacy_path = os.path.join(_world_root(ws), "world_knowledge.md")
    if os.path.exists(legacy_path):
        os.remove(legacy_path)
        print(f"  -> Removed legacy aggregated knowledge base: {legacy_path}")
    print(f"  -> Final sectioned knowledge-base directory: {_final_world_dir(ws)}")
    return _final_world_dir(ws)


def _build_supplement_usage_audit(ws, source_items, llm, force=False):
    primary_item = next(
        (item for item in source_items if item["record"].get("role") == "primary"),
        source_items[0] if source_items else None,
    )
    supplement_items = [
        item for item in source_items
        if primary_item and item["record"].get("id") != primary_item["record"].get("id")
    ]
    if not primary_item or not supplement_items:
        return None

    final_paths = _final_section_paths(ws)
    supplement_paths = [
        (f"{item['record'].get('file_name', 'supplement source')} / {section_name}", path)
        for item in supplement_items
        for section_name, path in item.get("sections", {}).items()
        if _has_meaningful_file(path)
    ]
    if not final_paths or not supplement_paths:
        return None

    output_path = os.path.join(_audits_dir(ws), "supplement_usage_audit.md")
    source_paths = [path for _, path in final_paths + supplement_paths]
    canon_path = _canon_index_path(ws)
    if os.path.exists(canon_path):
        source_paths.append(canon_path)

    newest_mtime = max(os.path.getmtime(path) for path in source_paths)
    if os.path.exists(output_path) and not force and os.path.getmtime(output_path) >= newest_mtime:
        return output_path

    print("  Auditing supplement-source usage...")
    _run_prompt(
        llm,
        "world_supplement_usage_audit",
        dict(
            primary_name=primary_item["record"].get("file_name", "primary source"),
            supplement_names=", ".join(item["record"].get("file_name", "supplement source") for item in supplement_items),
            canon_index=_read_file(canon_path) if os.path.exists(canon_path) else "(no primary-source baseline index)",
            final_knowledge=_aggregate_sections(final_paths, max_chars=40000),
            supplement_knowledge=_aggregate_sections(supplement_paths, max_chars=40000),
        ),
        output_path,
    )
    print(f"  -> Supplement-source usage audit saved: {output_path}")
    return output_path


# ── public entry: build knowledge base ──

def build_world_knowledge(ws, llm, force=False, chunk_size=36000, merge_chunk_size=50000,
                          chapter_batch_size=20, max_workers=4, primary_source=None,
                          merge_only=False):
    """Build structured target-world knowledge from imported source files."""
    _ = merge_chunk_size  # Keep the old argument for compatibility; merge is serial, 2 cards per round.
    records = _assign_source_roles(_source_records(ws), primary_source=primary_source)
    if not records:
        print("Error: target-world sources not found. Run world-import first.")
        return None

    os.makedirs(_cards_dir(ws), exist_ok=True)
    os.makedirs(_partials_dir(ws), exist_ok=True)
    os.makedirs(_worlds_dir(ws), exist_ok=True)

    primary = next((record for record in records if record.get("role") == "primary"), None)
    if primary:
        reason = primary.get("role_reason") or "largest file"
        print(f"  Primary source: {primary['file_name']} ({reason}, {primary.get('size', 0)} bytes)")
    supplements = [record for record in records if record.get("role") != "primary"]
    if supplements:
        print("  Supplement sources: " + ", ".join(record["file_name"] for record in supplements))

    if merge_only:
        print("  -> merge-only: skip cards/canon/source worlds; rebuild _final from existing worlds/<source>/*.md.")
        source_items = _load_existing_source_sections(ws, records)
        if not source_items:
            print("Error: no source-level sectioned knowledge base available to merge. Run a full world-build first.")
            return None
        final_dir = _integrate_final_sections(ws, source_items, llm, force=True, max_workers=max_workers)
        if final_dir:
            _build_supplement_usage_audit(ws, source_items, llm, force=True)
        return final_dir

    card_paths_by_source = {}
    source_slices_by_id = {}
    for record in records:
        slices = _source_slices(record, chunk_size=chunk_size, chapter_batch_size=chapter_batch_size)
        if slices:
            source_slices_by_id[record["id"]] = slices

    if primary and primary.get("id") in source_slices_by_id:
        card_paths_by_source[primary["id"]] = _build_record_cards(
            ws,
            primary,
            source_slices_by_id[primary["id"]],
            llm,
            force=force,
            canon_index="",
            max_workers=max_workers,
        )

    canon_index = _build_canon_index(
        ws,
        primary,
        card_paths_by_source.get(primary["id"], []),
        llm,
        force=force,
    ) if primary else ""
    canon_index_mtime = (
        os.path.getmtime(_canon_index_path(ws))
        if canon_index and os.path.exists(_canon_index_path(ws))
        else None
    )

    for record in records:
        if primary and record.get("id") == primary.get("id"):
            continue
        slices = source_slices_by_id.get(record["id"], [])
        if not slices:
            continue
        card_paths_by_source[record["id"]] = _build_record_cards(
            ws,
            record,
            slices,
            llm,
            force=force,
            canon_index=canon_index,
            dependency_mtime=canon_index_mtime,
            max_workers=max_workers,
        )

    existing_cards = [
        path
        for paths in card_paths_by_source.values()
        for path in paths
        if _has_meaningful_file(path)
    ]
    if not existing_cards:
        print("Error: source slices are empty; cannot generate the world knowledge base.")
        return None

    source_items = []
    for record in records:
        source_world = _build_single_source_sections(
            ws,
            record,
            card_paths_by_source.get(record["id"], []),
            llm,
            force=force,
            max_workers=max_workers,
        )
        if source_world:
            source_items.append(source_world)

    if not source_items:
        print("Error: failed to generate any source-level sectioned world knowledge base.")
        return None

    final_dir = _integrate_final_sections(ws, source_items, llm, force=force, max_workers=max_workers)
    if final_dir:
        _build_supplement_usage_audit(ws, source_items, llm, force=force)
    return final_dir


# ── public entry: load context ──

def load_world_knowledge_context(ws, max_chars=80000):
    manifest = _load_manifest(ws)
    # Users can disable the knowledge base in the Web UI; downstream design then skips it.
    if manifest.get("enabled", True) is False:
        return ""
    section_paths = _final_section_paths(ws)
    if section_paths:
        return _aggregate_sections(section_paths, max_chars=max_chars)

    legacy_path = os.path.join(_world_root(ws), "world_knowledge.md")
    if not os.path.exists(legacy_path):
        return ""

    content = _read_file(legacy_path).strip()
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "\n\n(target world knowledge base was too long; the above is a truncated front summary.)"


def world_knowledge_status(ws):
    """Return whether the knowledge base has all 7 sections ready to inject into later generation."""
    manifest = _load_manifest(ws)
    source_count = len(_source_records(ws))
    final_section_count = len(_final_section_paths(ws))
    return {
        "enabled": bool(manifest.get("enabled", True)),
        "source_count": source_count,
        "final_section_count": final_section_count,
        "ready": final_section_count == len(WORLD_SECTIONS),
    }


def set_world_knowledge_enabled(ws, enabled: bool) -> bool:
    """Toggle knowledge-base enabled state; persist it in the manifest. Return the final state."""
    manifest = _load_manifest(ws)
    manifest["enabled"] = bool(enabled)
    _save_manifest(ws, manifest)
    return bool(enabled)


def is_world_knowledge_enabled(ws) -> bool:
    """Read the knowledge-base enabled state."""
    return bool(_load_manifest(ws).get("enabled", True))
