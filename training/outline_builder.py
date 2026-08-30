import sys
import os
import re
import argparse
import json
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.llm_provider import LLMProvider
from core.prompt_loader import PromptLoader
from core.config import ConfigLoader
from core.text_utils import normalize_text
from core.workspace import NovelWorkspace, init_workspace

# Standalone volume titles of reasonable length, e.g. "Volume 1 Title" / "Volume 2 Title"
_IDEO_SPACE = "\u3000"
_CH_PREFIX = "\u7b2c"
_CN_NUM_CLASS = (
    "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u96f6"
)
_CH_UNITS = "\u7ae0\u56de\u8282"
_VOL_UNIT = "\u5377"
VOLUME_HEADER_RE = re.compile(
    r"^[ \t" + _IDEO_SPACE + r"]*(?:" + _CH_PREFIX + r"[" + _CN_NUM_CLASS
    + r"0-9]+" + _VOL_UNIT + r"\s+\S+"
    r"|(?:Volume|Book|Vol\.?)\s+\d+\b.*)",
    re.MULTILINE | re.IGNORECASE,
)

# Chapter titles, e.g. "Chapter 12: Title", "Ch. 12", plus imported Chinese headings.
CHAPTER_HEADER_RE = re.compile(
    r"^[ \t" + _IDEO_SPACE + r"]*(?:(?:\d+\.)?" + _CH_PREFIX + r"[" + _CN_NUM_CLASS
    + r"\d]+[" + _CH_UNITS + r"]"
    r"(?:\s*[（(]\d+[）)])?\s*.+|(?:Chapter|Ch\.?)\s+\d+\b.*)",
    re.MULTILINE | re.IGNORECASE,
)
CHAPTER_HEADER_FALLBACK = re.compile(
    r"(^[ \t" + _IDEO_SPACE + r"]*(?:" + _CH_PREFIX + r"[" + _CN_NUM_CLASS
    + r"0-9]+[" + _CH_UNITS + _VOL_UNIT + r"].{0,40}?"
    r"|(?:Chapter|Ch\.?)\s+\d+\b.{0,40}?)\n)",
    re.MULTILINE | re.IGNORECASE,
)
VOLUME_TITLE_RE = re.compile(
    r"^[ \t" + _IDEO_SPACE + r"]*(?:" + _CH_PREFIX + r"[" + _CN_NUM_CLASS
    + r"0-9]+" + _VOL_UNIT + r"\b|(?:Volume|Book|Vol\.?)\s+\d+\b)",
    re.IGNORECASE,
)

# Volume directory name format: vol_01_<title>
VOL_DIR_RE = re.compile(r'^vol_(\d+)_(.+)$')
PROMPT_JOIN_MAX_CHARS = 26000
PROMPT_PART_TRUNCATION_MARKER = "\n[... content truncated; later obligations retained ...]\n"


def _join_prompt_parts(parts, sep="\n\n---\n\n", max_chars=PROMPT_JOIN_MAX_CHARS):
    kept = [str(part) for part in parts if part]
    joined = sep.join(kept)
    if len(joined) <= max_chars:
        return joined
    if max_chars <= 0 or not kept:
        return ""

    separator_chars = len(sep) * (len(kept) - 1)
    if separator_chars >= max_chars:
        # Pathological many-part input: retain explicit early/middle/late representatives.
        representatives = [kept[0]]
        if len(kept) > 2:
            representatives.append(kept[len(kept) // 2])
        if len(kept) > 1:
            representatives.append(kept[-1])
        if len(representatives) - 1 >= max_chars:
            return PROMPT_PART_TRUNCATION_MARKER[:max_chars]
        return _join_prompt_parts(representatives, sep="\n", max_chars=max_chars)

    available = max_chars - separator_chars
    budgets = [0] * len(kept)
    remaining = set(range(len(kept)))
    while remaining:
        share = available // len(remaining)
        completed = {index for index in remaining if len(kept[index]) <= share}
        if not completed:
            for index in sorted(remaining):
                budgets[index] = share
            for index in sorted(remaining)[: available - share * len(remaining)]:
                budgets[index] += 1
            break
        for index in completed:
            budgets[index] = len(kept[index])
            available -= budgets[index]
        remaining -= completed

    def truncate_part(part, budget):
        if len(part) <= budget:
            return part
        if budget <= len(PROMPT_PART_TRUNCATION_MARKER):
            return PROMPT_PART_TRUNCATION_MARKER[:budget]
        content_budget = budget - len(PROMPT_PART_TRUNCATION_MARKER)
        head = (content_budget + 1) // 2
        tail = content_budget - head
        return part[:head] + PROMPT_PART_TRUNCATION_MARKER + (part[-tail:] if tail else "")

    return sep.join(truncate_part(part, budget) for part, budget in zip(kept, budgets))


ARC_FILE_RE = re.compile(r'^arc_(\d+)_ch(\d+)_(\d+)\.md$')
ARC_HEADER_RE = re.compile(
    r'^【Arc\s*\d+[：:]\s*Chapters?\s*'
    r'(\d+)\s*[-–—]\s*(\d+)\s*(?:[｜|：:]\s*(.*?))?】',
    re.MULTILINE | re.IGNORECASE,
)


def _read_and_clean(txt_path):
    with open(txt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    skip_markers = {"[file content begin]", "[file content end]"}
    return "".join(line for line in lines if line.strip() not in skip_markers)


def _find_volumes(text):
    volumes = []
    for m in VOLUME_HEADER_RE.finditer(text):
        line_start = text.rfind('\n', 0, m.start()) + 1
        line_end = text.find('\n', m.start())
        if line_end == -1:
            line_end = len(text)
        line_text = text[line_start:line_end].strip()
        if len(line_text) > 40 or m.start() != line_start:
            continue
        volumes.append({"title": line_text, "start": m.start()})
    return volumes


def _find_chapters(text):
    matches = list(CHAPTER_HEADER_RE.finditer(text))
    if not matches:
        parts = CHAPTER_HEADER_FALLBACK.split(text)
        chapters = []
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            content = f"{title}\n{body}"
            # Skip volume titles (still filter volume titles whose intro exceeds 50 chars)
            if VOLUME_TITLE_RE.match(title):
                continue
            # Skip entries that are too short
            if len(content) < 50:
                continue
            chapters.append({
                "title": title,
                "content": content,
                "pos": text.find(title),
                "volume_idx": -1,
            })
        return chapters

    chapters = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = normalize_text(text[start:end])
        first_newline = content.find('\n')
        title = content[:first_newline].strip() if first_newline != -1 else content[:50]
        # Skip volume titles
        if VOLUME_TITLE_RE.match(title):
            continue
        # Skip entries that are too short
        if len(content) < 50:
            continue
        chapters.append({
            "title": title,
            "content": content,
            "pos": start,
            "volume_idx": -1,
        })
    return chapters


def _assign_volumes_by_position(chapters, volumes):
    if not volumes:
        for ch in chapters:
            ch["volume_idx"] = 0
        return

    vol_starts = sorted(v["start"] for v in volumes)
    for ch in chapters:
        pos = ch["pos"]
        assigned = 0
        for vi, vs in enumerate(vol_starts):
            if vs <= pos:
                assigned = vi
            else:
                break
        ch["volume_idx"] = assigned

    for ch in chapters:
        ch.pop("pos", None)


def split_chapters(txt_path, max_chapters=None):
    text = _read_and_clean(txt_path)
    volumes = _find_volumes(text)
    chapters = _find_chapters(text)
    _assign_volumes_by_position(chapters, volumes)
    if max_chapters is not None:
        if max_chapters < 1:
            raise ValueError("Chapter limit must be a positive integer.")
        chapters = chapters[:max_chapters]
    return volumes, chapters


def group_chapters_by_volume(chapters, volumes):
    if not volumes:
        return [{"title": "Whole book", "chapters": chapters}]

    num_volumes = len(volumes)
    groups = []
    for vi in range(num_volumes):
        vol_chapters = [ch for ch in chapters if ch["volume_idx"] == vi]
        if vol_chapters:
            groups.append({"title": volumes[vi]["title"], "chapters": vol_chapters})

    unassigned = [ch for ch in chapters if ch["volume_idx"] < 0 or ch["volume_idx"] >= num_volumes]
    if unassigned:
        if groups:
            groups[-1]["chapters"].extend(unassigned)
        else:
            groups.append({"title": "Whole book", "chapters": unassigned})

    return groups


def split_chapters_to_files(ws, output_dir_name="chapters", max_chapters=None, refresh=False):
    """Split the reference novel into per-chapter files under reference/{output_dir_name}/."""
    from core.chapter_utils import _fix_chapter_numbering, MAX_CHAPTERS_PER_VOLUME

    base_dir = os.path.join(ws.reference, output_dir_name)
    meta_path = os.path.join(base_dir, "_volumes.json")
    if os.path.exists(meta_path) and not refresh:
        print("Chapter split already exists; skipping.")
        return
    if refresh and os.path.isdir(base_dir):
        shutil.rmtree(base_dir)

    sample_path = ws.reference_sample
    if not os.path.exists(sample_path):
        print(f"Error: reference novel file not found: {sample_path}")
        return

    volumes, chapters = split_chapters(sample_path, max_chapters=max_chapters)
    groups = group_chapters_by_volume(chapters, volumes)
    scope = f" (first {max_chapters} chapters only)" if max_chapters is not None else ""
    print(f"Parsed {len(volumes)} volumes, {len(chapters)} chapters{scope}")

    _fix_chapter_numbering(groups)

    # Split oversized volumes
    split_groups = []
    for g in groups:
        vol_chapters = g["chapters"]
        if len(vol_chapters) <= MAX_CHAPTERS_PER_VOLUME:
            split_groups.append(g)
            continue
        num_parts = (len(vol_chapters) + MAX_CHAPTERS_PER_VOLUME - 1) // MAX_CHAPTERS_PER_VOLUME
        part_labels = ["(part 1)", "(part 2)", "(part 3)"] if num_parts <= 3 else \
                      [f"(part {i + 1})" for i in range(num_parts)]
        for pi in range(num_parts):
            start = pi * MAX_CHAPTERS_PER_VOLUME
            end = start + MAX_CHAPTERS_PER_VOLUME
            label = part_labels[pi] if pi < len(part_labels) else f"(part {pi + 1})"
            split_groups.append({
                "title": g["title"] + label,
                "chapters": vol_chapters[start:end],
            })
        print(f"  {g['title']} ({len(vol_chapters)} chapters) -> split into {num_parts} sub-volumes")

    base_dir = os.path.join(ws.reference, output_dir_name)
    vol_meta = []
    saved = 0
    vol_offset = 0
    for vi, g in enumerate(split_groups):
        vol_chapters = g["chapters"]
        vol_dir_name = _vol_dir_name(vi, g["title"])
        vol_dir = os.path.join(base_dir, vol_dir_name)
        os.makedirs(vol_dir, exist_ok=True)

        vol_meta.append({"title": g["title"], "dir": vol_dir_name})

        for ci, ch in enumerate(vol_chapters):
            global_ch = ci + 1 + vol_offset
            safe_title = re.sub(r'[\\/:*?"<>|\s]', '_', ch["title"])[:50]
            fname = f"{global_ch:03d}_{safe_title}.md"
            fpath = os.path.join(vol_dir, fname)
            lines = ch["content"].split("\n", 1)
            corrected_content = ch["content"]
            if len(lines) >= 2 and lines[0].strip() != ch["title"]:
                corrected_content = f"{ch['title']}\n{lines[1]}" if len(lines) > 1 else ch["title"]
            _write_file(fpath, corrected_content)
            saved += 1

        vol_offset += len(vol_chapters)
        print(f"  {g['title']}: {len(vol_chapters)} chapters -> {vol_dir_name}/")

    with open(os.path.join(base_dir, "_volumes.json"), "w", encoding="utf-8") as f:
        json.dump(vol_meta, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {saved} chapter files to {base_dir}")


def load_chapter_text(ws, volume, chapter_num, total_chapters):
    """Load matching reference-novel chapter prose from persisted chapter files.

    Locate reference chapters by in-volume proportion mapping. Return empty string if not found.
    """
    base_dir = ws.reference_chapters
    meta_path = os.path.join(base_dir, "_volumes.json")

    if not os.path.exists(meta_path):
        return ""

    with open(meta_path, "r", encoding="utf-8") as f:
        vol_meta = json.load(f)

    vol_idx = volume - 1
    if vol_idx >= len(vol_meta):
        return ""

    vol_dir = os.path.join(base_dir, vol_meta[vol_idx]["dir"])
    if not os.path.isdir(vol_dir):
        return ""

    chapter_files = sorted(
        f for f in os.listdir(vol_dir) if f.endswith(".md") and not f.startswith("_")
    )
    ref_vol_total = len(chapter_files)

    if ref_vol_total == 0 or total_chapters <= 0:
        return ""

    ref_local_idx = int((chapter_num - 1) / total_chapters * ref_vol_total)
    ref_local_idx = min(ref_local_idx, ref_vol_total - 1)

    content = _read_file(os.path.join(vol_dir, chapter_files[ref_local_idx]))
    return content if content else ""


def _vol_dir_name(vol_idx, title):
    """Build a volume directory name such as vol_01_<title>."""
    safe = re.sub(r'[\\/:*?"<>|\s]', '_', title)[:30]
    return f"vol_{vol_idx + 1:02d}_{safe}"


def _is_whole_book_dir(name):
    return bool(VOL_DIR_RE.match(name or "")) and "whole_book" in (name or "").lower()


def _whole_book_dir_name():
    return _vol_dir_name(0, "whole_book")


def _find_whole_book_dir(outlines_dir):
    if not os.path.isdir(outlines_dir):
        return None
    for name in os.listdir(outlines_dir):
        if not _is_whole_book_dir(name):
            continue
        path = os.path.join(outlines_dir, name)
        if os.path.isdir(path):
            return path
    return None


def _batch_file_name(batch_start, batch_end):
    return f"batch_{batch_start + 1:03d}_{batch_end:03d}.md"


def _arc_dir(vol_dir):
    return os.path.join(vol_dir, "story_arcs")


def _arc_file_name(arc_idx, start_ch, end_ch):
    return f"arc_{arc_idx:03d}_ch{start_ch:03d}_{end_ch:03d}.md"


def _read_file(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return content if content else None


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")


def _format_chapters_for_arc_window(chapters, start_idx, end_idx):
    parts = []
    for i in range(start_idx, end_idx):
        ch_num = i + 1
        parts.append(f"=== Chapter {ch_num} ===\n{chapters[i]['content']}")
    return "\n\n".join(parts)


def _parse_story_arc_result(result):
    """Parse story_arc_extract output into completed arcs and carryover."""
    if not result:
        return [], ""

    carryover = ""
    carry_match = re.search(
        r'^#\s*Open carryover\s*$',
        result,
        re.MULTILINE | re.IGNORECASE,
    )
    arc_part = result
    if carry_match:
        arc_part = result[:carry_match.start()]
        carryover = result[carry_match.end():].strip()
        if carryover.lower() in {"none", "none.", "(none)"}:
            carryover = ""

    matches = list(ARC_HEADER_RE.finditer(arc_part))
    arcs = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(arc_part)
        text = arc_part[start:end].strip()
        if not text:
            continue
        title = (match.group(3) or "").strip()
        arcs.append({
            "start_ch": int(match.group(1)),
            "end_ch": int(match.group(2)),
            "title": title,
            "content": text,
        })
    return arcs, carryover


def _story_arc_files(vol_dir):
    arc_path = _arc_dir(vol_dir)
    if not os.path.isdir(arc_path):
        return []
    items = []
    for fname in sorted(os.listdir(arc_path)):
        m = ARC_FILE_RE.match(fname)
        if not m:
            continue
        items.append({
            "idx": int(m.group(1)),
            "start_ch": int(m.group(2)),
            "end_ch": int(m.group(3)),
            "file": fname,
            "path": os.path.join(arc_path, fname),
        })
    return items


def _load_story_arc_texts(vol_dir):
    texts = []
    for item in _story_arc_files(vol_dir):
        content = _read_file(item["path"])
        if content:
            copied = dict(item)
            copied["content"] = content
            texts.append(copied)
    return texts


def _write_story_arc_index(vol_dir, arc_items):
    index_path = os.path.join(_arc_dir(vol_dir), "arcs_index.json")
    payload = []
    for item in arc_items:
        payload.append({
            "id": item["idx"],
            "start_ch": item["start_ch"],
            "end_ch": item["end_ch"],
            "file": item["file"],
        })
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _extract_story_arcs_for_volume(vol_idx, volume_title, chapters, llm, outlines_dir, batch_size=20):
    """Extract natural story-arc units by read window, continuing from the last chapter of existing segments."""
    vol_dir = os.path.join(outlines_dir, _vol_dir_name(vol_idx, volume_title))
    arc_path = _arc_dir(vol_dir)
    existing_arcs = _load_story_arc_texts(vol_dir)
    total = len(chapters)
    existing_end = max((item["end_ch"] for item in existing_arcs), default=0)
    if existing_end >= total:
        print(f"    -> {len(existing_arcs)} story-arc units already exist, covering through chapter {existing_end}; skipping extraction.")
        return existing_arcs

    if existing_arcs:
        print(f"    -> {len(existing_arcs)} story-arc units already exist; continuing chapters {existing_end + 1}-{total}...")
    carryover = ""
    arc_items = list(existing_arcs)
    arc_idx = max((item["idx"] for item in existing_arcs), default=0) + 1
    last_result = ""
    os.makedirs(arc_path, exist_ok=True)

    for start_idx in range(existing_end, total, batch_size):
        end_idx = min(start_idx + batch_size, total)
        start_ch = start_idx + 1
        end_ch = end_idx
        is_final_window = "yes" if end_idx >= total else "no"
        print(f"    -> Identifying story arcs (chapters {start_ch}-{end_ch}, read window {batch_size} chapters)...")

        prompt = PromptLoader.load(
            "story_arc_extract",
            previous_carryover=carryover or "none",
            start_chapter=start_ch,
            end_chapter=end_ch,
            is_final_window=is_final_window,
            chapters_text=_format_chapters_for_arc_window(chapters, start_idx, end_idx),
        )
        result = normalize_text(llm.generate(prompt))
        last_result = result
        arcs, carryover = _parse_story_arc_result(result)

        for arc in arcs:
            # Guard against the model occasionally outputting reversed or out-of-range bounds; do not rewrite the text, only constrain filename and index.
            arc_start = max(start_ch, min(arc["start_ch"], arc["end_ch"]))
            arc_end = min(total, max(arc["start_ch"], arc["end_ch"]))
            if arc_end < arc_start:
                arc_end = arc_start
            fname = _arc_file_name(arc_idx, arc_start, arc_end)
            fpath = os.path.join(arc_path, fname)
            _write_file(fpath, arc["content"])
            arc_items.append({
                "idx": arc_idx,
                "start_ch": arc_start,
                "end_ch": arc_end,
                "file": fname,
                "path": fpath,
                "content": arc["content"],
            })
            arc_idx += 1

        _write_story_arc_index(vol_dir, arc_items)

    if len(arc_items) == len(existing_arcs) and last_result:
        fname = _arc_file_name(arc_idx, existing_end + 1, total)
        fpath = os.path.join(arc_path, fname)
        fallback = (
            f"【Arc{arc_idx}: Chapters {existing_end + 1}-{total} | format-fallback arc】\n"
            "Plot function: the model did not follow the standard format; "
            "the raw analysis is kept for later inspection.\n\n"
            + last_result
        )
        _write_file(fpath, fallback)
        arc_items.append({
            "idx": arc_idx,
            "start_ch": existing_end + 1,
            "end_ch": total,
            "file": fname,
            "path": fpath,
            "content": fallback,
        })
        _write_story_arc_index(vol_dir, arc_items)
        carryover = ""

    carryover_path = os.path.join(arc_path, "_carryover.md")
    if carryover:
        _write_file(carryover_path, carryover)
        print(f"    -> Open carryover still remains: {carryover_path}")
    elif os.path.exists(carryover_path):
        os.remove(carryover_path)

    print(f"    -> Story-arc units saved: {len(arc_items)}")
    return arc_items


def _generate_volume_outline_from_arcs(vol_dir, volume_title, total_chapters, llm,
                                       start_chapter=1, end_chapter=None, force=False):
    """Read story-arc units and merge them into a volume outline."""
    vol_outline_path = os.path.join(vol_dir, "volume_outline.md")
    existing_outline = _read_file(vol_outline_path)
    if existing_outline and not force:
        print("    -> Volume outline already exists; skipping merge.")
        return existing_outline

    arc_items = _load_story_arc_texts(vol_dir)
    if not arc_items:
        return ""

    end_chapter = end_chapter or total_chapters
    arc_summaries = [item["content"] for item in arc_items]
    if len(arc_summaries) == 1:
        outline = arc_summaries[0]
    else:
        print(f"    -> Merging {len(arc_summaries)} story-arc units into a volume outline...")
        all_subs = _join_prompt_parts(arc_summaries)
        merge_prompt = PromptLoader.load(
            "volume_merge",
            volume_title=volume_title,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            total_chapters=total_chapters,
            total_batches=len(arc_summaries),
            batch_summaries=all_subs,
        )
        outline = normalize_text(llm.generate(merge_prompt))

    _write_file(vol_outline_path, outline)
    print(f"    -> Volume outline saved: {vol_outline_path}")
    return outline


def extract_volume_outline(vol_idx, volume_title, chapters, llm, outlines_dir, batch_size=20, force=False):
    """Extract one volume outline: first pull story-arc units, then merge them."""
    vol_dir = os.path.join(outlines_dir, _vol_dir_name(vol_idx, volume_title))
    total = len(chapters)
    print(f"    [{volume_title}] {total} chapters")
    _extract_story_arcs_for_volume(vol_idx, volume_title, chapters, llm, outlines_dir, batch_size)
    return _generate_volume_outline_from_arcs(vol_dir, volume_title, total, llm, force=force)


def extract_novel_outline(volume_outlines, llm, outlines_dir, force=False):
    """Combine every volume outline into a full outline."""
    novel_outline_path = os.path.join(outlines_dir, "novel_outline.md")
    existing = _read_file(novel_outline_path)
    if existing and not force:
        print("  -> Full outline already exists; skipping.")
        return existing

    print(f"  -> Combining {len(volume_outlines)} volume outlines into a full outline...")
    all_outlines = _join_prompt_parts(
        f"【{vo['title']}】\n{vo['outline']}"
        for vo in volume_outlines
    )
    prompt = PromptLoader.load("novel_extract", all_volume_outlines=all_outlines)
    novel_outline = normalize_text(llm.generate(prompt))
    _write_file(novel_outline_path, novel_outline)
    print(f"  -> Full outline saved: {novel_outline_path}")
    return novel_outline


def _volume_dirs(outlines_dir):
    items = []
    if not os.path.isdir(outlines_dir):
        return items
    for name in sorted(os.listdir(outlines_dir)):
        m = VOL_DIR_RE.match(name)
        if not m:
            continue
        vol_path = os.path.join(outlines_dir, name)
        if os.path.isdir(vol_path):
            items.append((name, vol_path))
    return items


def _parse_virtual_volumes(llm_result):
    """Parse LLM virtual-volume output into [(vol_idx, title, start_ch, end_ch), ...]."""
    volumes = []
    for line in llm_result.strip().split('\n'):
        line = line.strip()
        m = re.match(
            r"(?:Volume|Vol\.?|" + "\u5377" + r")\s*(\d+)\s*[：:]\s*(.+?)\s*\|\s*"
            r"(?:Chapters?\s*|" + "\u7b2c" + r")?(\d+)\s*[-–—]\s*(\d+)\s*"
            + "\u7ae0" + r"?",
            line,
            re.IGNORECASE,
        )
        if m:
            vol_idx = int(m.group(1))
            title = m.group(2).strip()
            start_ch = int(m.group(3))
            end_ch = int(m.group(4))
            volumes.append((vol_idx, title, start_ch, end_ch))
    return volumes


def _extract_segment_endpoints(batch_dir):
    """Extract every segment end-chapter from story-arc units or old batch summaries."""
    endpoints = set()
    for item in _story_arc_files(batch_dir):
        endpoints.add(item["end_ch"])
    for bf in sorted(os.listdir(batch_dir)):
        if not re.match(r'^batch_\d+_\d+\.md$', bf):
            continue
        content = _read_file(os.path.join(batch_dir, bf))
        if not content:
            continue
        for m in re.finditer(
            r"[【]?Segment\s*\d+[：:]\s*Chapters?\s*(\d+)\s*[-–—]\s*(\d+)",
            content,
            re.I,
        ):
            endpoints.add(int(m.group(2)))
    return sorted(endpoints)


def _snap_to_segments(virtual_volumes, segment_endpoints, total_chapters):
    """Snap virtual-volume chapter bounds to the nearest segment endpoint so segments are not split."""
    if not segment_endpoints:
        return virtual_volumes

    snapped = []
    for i, (vi, title, start_ch, end_ch) in enumerate(virtual_volumes):
        # Volume 1 still starts at chapter 1
        s = 1 if i == 0 else snapped[-1][3] + 1
        # Snap the end chapter to the nearest segment endpoint (not far past the original)
        candidates = [ep for ep in segment_endpoints if ep >= s]
        if candidates:
            # Prefer the nearest endpoint that is not far past the original
            nearest = min(candidates, key=lambda x: abs(x - end_ch))
            e = nearest
        else:
            e = end_ch
        snapped.append((vi, title, s, e))
    return snapped


def _assign_batches_to_volumes(src_dir, virtual_volumes):
    """Assign batch files to the virtual volume with the largest overlap so one batch is not in multiple volumes.

    Returns:
        dict: {vol_dir: [batch_file_name, ...]}
    """
    # Collect all batch-file info
    batches = []
    for bf in sorted(os.listdir(src_dir)):
        m = re.match(r'^batch_(\d+)_(\d+)\.md$', bf)
        if not m:
            continue
        batches.append((bf, int(m.group(1)), int(m.group(2))))

    assignment = {i: [] for i in range(len(virtual_volumes))}

    for bf, b_start, b_end in batches:
        best_vol = -1
        best_overlap = 0
        for i, (vi, title, start_ch, end_ch) in enumerate(virtual_volumes):
            overlap_start = max(b_start, start_ch)
            overlap_end = min(b_end, end_ch)
            overlap = max(0, overlap_end - overlap_start + 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_vol = i
        if best_vol >= 0:
            assignment[best_vol].append(bf)

    return assignment


def _assign_story_arcs_to_volumes(src_dir, virtual_volumes):
    """Assign story-arc files to the virtual volume with the largest overlap."""
    arcs = _story_arc_files(src_dir)
    assignment = {i: [] for i in range(len(virtual_volumes))}

    for arc in arcs:
        best_vol = -1
        best_overlap = 0
        for i, (vi, title, start_ch, end_ch) in enumerate(virtual_volumes):
            overlap_start = max(arc["start_ch"], start_ch)
            overlap_end = min(arc["end_ch"], end_ch)
            overlap = max(0, overlap_end - overlap_start + 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_vol = i
        if best_vol >= 0:
            assignment[best_vol].append(arc)

    return assignment


def _copy_chapter_outlines_for_volume(src_dir, dst_dir, start_ch, end_ch):
    """Copy chapter outlines in [start_ch, end_ch] from src_dir/chapter_outlines/ to dst_dir/chapter_outlines/."""
    src_ch_dir = os.path.join(src_dir, "chapter_outlines")
    dst_ch_dir = os.path.join(dst_dir, "chapter_outlines")
    if not os.path.isdir(src_ch_dir):
        return
    os.makedirs(dst_ch_dir, exist_ok=True)
    for ch_num in range(start_ch, end_ch + 1):
        src_file = os.path.join(src_ch_dir, f"chapter_{ch_num:03d}.md")
        if os.path.exists(src_file):
            shutil.copy2(src_file, os.path.join(dst_ch_dir, f"chapter_{ch_num:03d}.md"))


def _generate_virtual_volume_outline(vol_dir, start_ch, end_ch, llm):
    """Read story-arc units or old batch summaries covered by a virtual volume, then call the LLM for a volume outline."""
    vol_outline_path = os.path.join(vol_dir, "volume_outline.md")
    existing = _read_file(vol_outline_path)
    if existing:
        return existing

    arc_items = _load_story_arc_texts(vol_dir)
    if arc_items:
        return _generate_volume_outline_from_arcs(
            vol_dir,
            "virtual volume",
            end_ch - start_ch + 1,
            llm,
            start_chapter=start_ch,
            end_chapter=end_ch,
        )

    batch_summaries = []
    for bf in sorted(os.listdir(vol_dir)):
        m = re.match(r'^batch_\d+_\d+\.md$', bf)
        if not m:
            continue
        content = _read_file(os.path.join(vol_dir, bf))
        if content:
            batch_summaries.append(content)

    if not batch_summaries:
        return ""

    if len(batch_summaries) == 1:
        outline = batch_summaries[0]
    else:
        all_subs = _join_prompt_parts(batch_summaries)
        total = end_ch - start_ch + 1
        merge_prompt = PromptLoader.load(
            "volume_merge",
            volume_title="virtual volume",
            start_chapter=start_ch,
            end_chapter=end_ch,
            total_chapters=total,
            total_batches=len(batch_summaries),
            batch_summaries=all_subs,
        )
        outline = normalize_text(llm.generate(merge_prompt))

    _write_file(vol_outline_path, outline)
    return outline


def _extract_segment_ranges(batch_dir):
    """Extract story-segment chapter ranges from story-arc units or old batch summaries."""
    segments = []
    for item in _story_arc_files(batch_dir):
        segments.append((item["start_ch"], item["end_ch"]))
    for bf in sorted(os.listdir(batch_dir)):
        if not re.match(r'^batch_\d+_\d+\.md$', bf):
            continue
        content = _read_file(os.path.join(batch_dir, bf))
        if not content:
            continue
        for m in re.finditer(
            r"[【]?Segment\s*\d+[：:]\s*Chapters?\s*(\d+)\s*[-–—]\s*(\d+)",
            content,
            re.I,
        ):
            segments.append((int(m.group(1)), int(m.group(2))))
    return segments


def _load_existing_volumes(outlines_dir, groups, chapters):
    """If complete story-arc units / old batch summaries and volume outlines exist, return {volume_outlines, groups}.

    Two cases:
    1. Current: arc files and volume_outline.md under vol_XX_<title>/story_arcs/
    2. Legacy: batch files and volume_outline.md under vol_XX_<title>/
    """
    if not os.path.isdir(outlines_dir):
        return None

    # Scan existing volume directories
    vol_dirs = _volume_dirs(outlines_dir)

    if not vol_dirs:
        return None

    # Check each volume directory for complete batch files and a volume outline
    volume_outlines = []
    vol_groups = []
    all_complete = True

    for name, vol_path in vol_dirs:
        m = VOL_DIR_RE.match(name)
        vol_idx = int(m.group(1))
        title = m.group(2).replace('_', ' ')

        # Check the volume outline
        vol_outline = _read_file(os.path.join(vol_path, "volume_outline.md"))
        if not vol_outline:
            all_complete = False
            break

        # Check current-format story-arc units or legacy batch files
        arc_files = _story_arc_files(vol_path)
        batch_files = [f for f in os.listdir(vol_path) if re.match(r'^batch_\d+_\d+\.md$', f)]
        if not arc_files and not batch_files:
            all_complete = False
            break

        volume_outlines.append({"title": title, "outline": vol_outline})

        # Virtual vs natural volume depends on whether meta.json exists
        meta = None
        meta_path = os.path.join(vol_path, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        if meta:
            # Virtual volume: chapter range from meta.json
            start_ch = meta["start_ch"]
            end_ch = meta["end_ch"]
            vol_chapters = [chapters[i] for i in range(len(chapters))
                            if start_ch <= (i + 1) <= end_ch]
        else:
            # Natural volume: group by volume index
            vol_chapters = [ch for ch in chapters if ch.get("volume_idx", -1) == vol_idx - 1]

        vol_groups.append({"title": title, "chapters": vol_chapters})

    if not all_complete:
        return None

    return {"volume_outlines": volume_outlines, "groups": vol_groups}


def _run_legacy_outline_build(txt_path=None, output_dir=None, batch_size=20, skip_chapter_outlines=False,
                              max_chapters=None, resume=False):
    if txt_path is None:
        txt_path = os.path.join(DATA_DIR, "sample_novel.txt")
    if output_dir is None:
        output_dir = DATA_DIR

    if not os.path.exists(txt_path):
        print(f"Error: novel file not found: {txt_path}")
        return

    outlines_dir = os.path.join(output_dir, "outlines")

    print(">>> Starting reference-novel outline extraction <<<")
    print(f"Reading file: {txt_path}")
    print(f"Output directory: {outlines_dir}")

    # 1. Split chapters and identify volumes
    volumes, chapters = split_chapters(txt_path, max_chapters=max_chapters)
    scope = f"first {max_chapters} chapters only, " if max_chapters is not None else ""
    print(f"Parsed {len(volumes)} volumes, {len(chapters)} chapters, {scope}reading {batch_size} chapters per window to identify story arcs.")

    # 2. Group by volume
    groups = group_chapters_by_volume(chapters, volumes)
    for g in groups:
        n = len(g['chapters'])
        windows = (n + batch_size - 1) // batch_size
        print(f"  {g['title']}: {n} chapters -> {windows} read windows")

    # 3. Skip phase one if complete story-arc units and volume outlines already exist
    existing_volumes = None if resume else _load_existing_volumes(outlines_dir, groups, chapters)

    # 4. Initialize the LLM
    builder_config = ConfigLoader.get_data_builder_config()
    if not builder_config.get("api_key"):
        builder_config["api_key"] = os.getenv("OPENAI_API_KEY")
    if not builder_config.get("api_key"):
        print("Error: API Key not detected.")
        return
    llm = LLMProvider(**builder_config)

    if existing_volumes:
        # Complete data already exists; skip phase one and virtual volume split
        print("\n--- Phase one skipped (existing story-arc units / old batch summaries and volume outlines detected) ---")
        volume_outlines = existing_volumes["volume_outlines"]
        groups = existing_volumes["groups"]
    else:
        # 4. Extract story-arc units and volume outlines per volume (incremental save)
        print("\n--- Phase one: extract story-arc units and volume outlines per volume ---")
        volume_outlines = []
        for vi, g in enumerate(groups):
            print(f"\n  Processing: {g['title']}")
            outline = extract_volume_outline(
                vi, g["title"], g["chapters"], llm, outlines_dir, batch_size, force=resume,
            )
            volume_outlines.append({"title": g["title"], "outline": outline})

    # Combined volume-outline file
    volume_outline_path = os.path.join(outlines_dir, "volume_outline.md")
    with open(volume_outline_path, "w", encoding="utf-8") as f:
        f.write("# Reference-novel volume outlines\n\n")
        for vo in volume_outlines:
            f.write(f"## {vo['title']}\n\n{vo['outline']}\n\n---\n\n")
    print(f"\nCombined volume outline saved to: {volume_outline_path}")

    # Combine into a full outline
    extract_novel_outline(volume_outlines, llm, outlines_dir, force=resume)


def run_outline_build(txt_path=None, output_dir=None, batch_size=20, skip_chapter_outlines=False,
                      max_chapters=None, resume=False, rebuild_reference=False):
    """Run a resumable three-stage reference-novel deconstruction.

    Keep the original function name and story-segment output dir so later stage design and narrative-pattern
    extraction still read from ``reference/outlines/vol_xx/story_arcs``.
    """
    if txt_path is None:
        txt_path = os.path.join(DATA_DIR, "sample_novel.txt")
    if output_dir is None:
        output_dir = DATA_DIR

    from training.reference_analyzer import run_reference_analysis

    return run_reference_analysis(
        txt_path=txt_path,
        output_dir=output_dir,
        batch_size=batch_size,
        max_chapters=max_chapters,
        resume=resume,
        rebuild=rebuild_reference,
    )

def resegment(outlines_dir):
    """Re-run virtual volume split from existing story-arc units or old batch summaries.

    Two cases:
    1. vol_01_whole_book/ exists: re-split from that directory.
    2. Virtual volume dirs (with meta.json) already exist: gather all volume story-arc units / batch summaries into vol_01_whole_book/, dedupe, then re-split.
    """
    all_batch_dir = _find_whole_book_dir(outlines_dir)

    if not all_batch_dir or not os.path.isdir(all_batch_dir):
        # No whole-book directory; find virtual-volume dirs and gather story-arc units / batch summaries
        vol_dirs = _volume_dirs(outlines_dir)

        if not vol_dirs:
            print("Error: no volume directories found; cannot re-segment volumes.")
            return

        whole_book_name = _whole_book_dir_name()
        print(f"  -> {whole_book_name} not found; gathering story-arc units from existing volume directories...")
        all_batch_dir = os.path.join(outlines_dir, whole_book_name)
        os.makedirs(all_batch_dir, exist_ok=True)
        os.makedirs(_arc_dir(all_batch_dir), exist_ok=True)

        # Collect all story-arc and old batch files and dedupe by filename
        seen = set()
        seen_batches = set()
        for name, vol_path in vol_dirs:
            for arc in _story_arc_files(vol_path):
                if arc["file"] in seen:
                    continue
                shutil.copy2(arc["path"], os.path.join(_arc_dir(all_batch_dir), arc["file"]))
                seen.add(arc["file"])
            for bf in sorted(os.listdir(vol_path)):
                if re.match(r'^batch_\d+_\d+\.md$', bf) and bf not in seen_batches:
                    shutil.copy2(os.path.join(vol_path, bf), os.path.join(all_batch_dir, bf))
                    seen_batches.add(bf)

        # Delete old virtual-volume directories
        for name, vol_path in vol_dirs:
            shutil.rmtree(vol_path, ignore_errors=True)
            print(f"  -> Deleted old volume directory: {name}")

        print(f"  -> Gathered {len(seen)} story-arc units and {len(seen_batches)} old batch summaries into {whole_book_name}/")

    # Unified handling below: prefer story arcs, older workspaces fall back to batch summaries
    arc_items = _load_story_arc_texts(all_batch_dir)
    segment_summaries = []
    if arc_items:
        segment_summaries = [item["content"] for item in arc_items]
    else:
        for bf in sorted(os.listdir(all_batch_dir)):
            if re.match(r'^batch_\d+_\d+\.md$', bf):
                content = _read_file(os.path.join(all_batch_dir, bf))
                if content:
                    segment_summaries.append(content)

    if not segment_summaries:
        print("Error: no story-arc units or batch summaries found.")
        return

    batch_summaries = []
    for bf in sorted(os.listdir(all_batch_dir)):
        if re.match(r'^batch_\d+_\d+\.md$', bf):
            content = _read_file(os.path.join(all_batch_dir, bf))
            if content:
                batch_summaries.append(content)

    # Initialize the LLM
    builder_config = ConfigLoader.get_data_builder_config()
    if not builder_config.get("api_key"):
        builder_config["api_key"] = os.getenv("OPENAI_API_KEY")
    if not builder_config.get("api_key"):
        print("Error: API Key not detected.")
        return
    llm = LLMProvider(**builder_config)

    # Infer total chapter count
    total_ch = 0
    if arc_items:
        total_ch = max(item["end_ch"] for item in arc_items)
    else:
        for bf in sorted(os.listdir(all_batch_dir)):
            m = re.match(r'^batch_\d+_(\d+)\.md$', bf)
            if m:
                total_ch = max(total_ch, int(m.group(1)))

    print(">>> Virtual volume split (re-segment) <<<")
    print(f"  Story-arc units/summaries: {len(segment_summaries)} files, about {total_ch} chapters")

    all_batches_text = _join_prompt_parts(segment_summaries)
    print("  -> Calling the LLM to analyze story-arc units and identify volume boundaries...")
    seg_prompt = PromptLoader.load("virtual_volume_segment", batch_summaries=all_batches_text)
    seg_result = normalize_text(llm.generate(seg_prompt))

    virtual_volumes = _parse_virtual_volumes(seg_result)
    if not virtual_volumes:
        print("  Warning: LLM did not output a valid volume split; leaving as-is.")
        return

    # Align bounds to story-segment endpoints
    segment_endpoints = _extract_segment_endpoints(all_batch_dir)
    virtual_volumes = _snap_to_segments(virtual_volumes, segment_endpoints, total_ch)

    # Cover every chapter: volume 1 starts at 1, last volume ends at total_ch
    virtual_volumes = _ensure_full_coverage(virtual_volumes, total_ch)

    print(f"  -> Identified {len(virtual_volumes)} volumes (aligned to segment boundaries):")
    for vi, title, sc, ec in virtual_volumes:
        print(f"     Volume {vi}: {title} (chapters {sc}-{ec}, {ec - sc + 1} chapters)")
    covered = sum(ec - sc + 1 for _, _, sc, ec in virtual_volumes)
    print(f"  -> Coverage: {covered}/{total_ch} chapters")

    # Assign story-arc units / legacy batch files
    arc_assignment = _assign_story_arcs_to_volumes(all_batch_dir, virtual_volumes)
    batch_assignment = _assign_batches_to_volumes(all_batch_dir, virtual_volumes)

    # Create dirs, copy files, and generate a volume outline for each virtual volume
    new_volume_outlines = []
    for i, (vi, vol_title, start_ch, end_ch) in enumerate(virtual_volumes):
        vol_dir_name = _vol_dir_name(vi - 1, vol_title)
        vol_dir = os.path.join(outlines_dir, vol_dir_name)

        print(f"  -> Organizing volume {vi} ({vol_title}, chapters {start_ch}-{end_ch})...")
        os.makedirs(vol_dir, exist_ok=True)
        os.makedirs(_arc_dir(vol_dir), exist_ok=True)
        for arc in arc_assignment.get(i, []):
            shutil.copy2(arc["path"], os.path.join(_arc_dir(vol_dir), arc["file"]))
        for bf in batch_assignment.get(i, []):
            shutil.copy2(os.path.join(all_batch_dir, bf), os.path.join(vol_dir, bf))

        meta = {"start_ch": start_ch, "end_ch": end_ch}
        with open(os.path.join(vol_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        outline = _generate_virtual_volume_outline(vol_dir, start_ch, end_ch, llm)
        new_volume_outlines.append({"title": vol_title, "outline": outline})
        print("     Volume outline generated")

    # Delete the original whole-book pseudo-volume directory
    shutil.rmtree(all_batch_dir, ignore_errors=True)

    # Rewrite the combined volume-outline file
    volume_outline_path = os.path.join(outlines_dir, "volume_outline.md")
    with open(volume_outline_path, "w", encoding="utf-8") as f:
        f.write("# Reference-novel volume outlines\n\n")
        for vo in new_volume_outlines:
            f.write(f"## {vo['title']}\n\n{vo['outline']}\n\n---\n\n")

    # Rebuild the outline
    print("\n  -> Rebuilding the combined outline...")
    extract_novel_outline(new_volume_outlines, llm, outlines_dir, force=True)

    print("\n>>> Virtual volume split complete <<<")
    print(f"  Combined volume outline: {volume_outline_path}")

def _ensure_full_coverage(virtual_volumes, total_ch):
    """Ensure virtual volumes cover the full chapter range: first starts at 1, last ends at total_ch, no gaps."""
    if not virtual_volumes:
        return virtual_volumes

    result = []
    for i, (vi, title, start_ch, end_ch) in enumerate(virtual_volumes):
        s = 1 if i == 0 else (result[-1][3] + 1)
        # Last volume must extend to total_ch
        if i == len(virtual_volumes) - 1:
            e = max(end_ch, total_ch)
        else:
            e = end_ch
        result.append((vi, title, s, e))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract outlines and volume outlines from a reference novel")
    parser.add_argument("--novel", type=str, required=True, help="Workspace name")
    parser.add_argument("--batch-size", type=int, default=20, help="Chapters per read window for identifying story-arc units (default 20)")
    parser.add_argument("--max-chapters", type=int, default=None, help="Deconstruct only the first N chapters (default: whole book)")
    parser.add_argument("--txt-path", type=str, default=None, help="Novel file path (default: workspace reference/sample_novel.txt)")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory (default: workspace reference/)")
    args = parser.parse_args()

    ws = init_workspace(args.novel)
    run_outline_build(
        txt_path=args.txt_path or ws.reference_sample,
        output_dir=args.output_dir or ws.reference,
        batch_size=args.batch_size,
        max_chapters=args.max_chapters,
    )
