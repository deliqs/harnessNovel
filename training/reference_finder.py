import os
import re
import glob
import json


DEFAULT_OUTLINES_DIR = os.path.join(os.path.dirname(__file__), 'data', 'outlines')

# Volume directory name format: vol_01_<title>
VOL_DIR_RE = re.compile(r'^vol_(\d+)_(.+)$')

# Batch filename format: batch_001_030.md
BATCH_FILE_RE = re.compile(r'^batch_(\d+)_(\d+)\.md$')
ARC_FILE_RE = re.compile(r'^arc_(\d+)_ch(\d+)_(\d+)\.md$')


def _load_volume_meta(dir_path):
    """Load virtual-volume metadata. Return None if missing."""
    meta_path = os.path.join(dir_path, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def list_reference_volumes(outlines_dir=None):
    """Scan the reference-outline directory and return [{vol_idx, title, dir_path}]."""
    if outlines_dir is None:
        outlines_dir = DEFAULT_OUTLINES_DIR

    if not os.path.isdir(outlines_dir):
        return []

    volumes = []
    for name in sorted(os.listdir(outlines_dir)):
        m = VOL_DIR_RE.match(name)
        if not m:
            continue
        vol_idx = int(m.group(1))
        title = m.group(2).replace('_', ' ')
        dir_path = os.path.join(outlines_dir, name)

        # Virtual volume: chapter count from meta.json
        meta = _load_volume_meta(dir_path)
        if meta:
            chapter_count = meta["end_ch"] - meta["start_ch"] + 1
        else:
            chapter_count = 0
            # Current format: infer chapter count from story-arc unit filenames
            for arc in _story_arc_files(dir_path):
                chapter_count = max(chapter_count, arc["end_ch"])
            # Legacy: infer chapter count from batch filenames
            batch_files = glob.glob(os.path.join(dir_path, "batch_*.md"))
            for bf in batch_files:
                bm = BATCH_FILE_RE.match(os.path.basename(bf))
                if bm:
                    chapter_count = max(chapter_count, int(bm.group(2)))

        volumes.append({
            "vol_idx": vol_idx,
            "title": title,
            "chapter_count": chapter_count,
            "dir_path": dir_path,
        })

    return sorted(volumes, key=lambda v: v["vol_idx"])


def load_reference_novel_outline(outlines_dir=None):
    """Load the reference novel's full outline."""
    if outlines_dir is None:
        outlines_dir = DEFAULT_OUTLINES_DIR

    path = os.path.join(outlines_dir, "novel_outline.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_reference_volume_outline(outlines_dir=None, vol_idx=1):
    """Load the volume outline of a given reference-novel volume."""
    if outlines_dir is None:
        outlines_dir = DEFAULT_OUTLINES_DIR

    volumes = list_reference_volumes(outlines_dir)
    for vol in volumes:
        if vol["vol_idx"] == vol_idx:
            path = os.path.join(vol["dir_path"], "volume_outline.md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
    return ""


def _read_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _story_arc_dir(vol_dir):
    return os.path.join(vol_dir, "story_arcs")


def _story_arc_files(vol_dir):
    arc_dir = _story_arc_dir(vol_dir)
    if not os.path.isdir(arc_dir):
        return []
    items = []
    for fname in sorted(os.listdir(arc_dir)):
        m = ARC_FILE_RE.match(fname)
        if not m:
            continue
        items.append({
            "idx": int(m.group(1)),
            "start_ch": int(m.group(2)),
            "end_ch": int(m.group(3)),
            "path": os.path.join(arc_dir, fname),
        })
    return items


def list_reference_story_arcs(outlines_dir, vol_idx):
    """Load story-arc units of a given reference-novel volume.

    Current workspaces prefer story_arcs/arc_*.md; older workspaces fall back to batch_*.md
    and return those old batches as coarse story-arc units.
    """
    volumes = list_reference_volumes(outlines_dir)
    vol_info = None
    for vol in volumes:
        if vol["vol_idx"] == vol_idx:
            vol_info = vol
            break

    if vol_info is None:
        return []

    items = []
    for arc in _story_arc_files(vol_info["dir_path"]):
        content = _read_file(arc["path"])
        if not content:
            continue
        copied = dict(arc)
        copied["content"] = content
        copied["source_type"] = "story_arc"
        items.append(copied)
    if items:
        return items

    for bf in sorted(glob.glob(os.path.join(vol_info["dir_path"], "batch_*.md"))):
        bm = BATCH_FILE_RE.match(os.path.basename(bf))
        if not bm:
            continue
        content = _read_file(bf)
        if not content:
            continue
        items.append({
            "idx": len(items) + 1,
            "start_ch": int(bm.group(1)),
            "end_ch": int(bm.group(2)),
            "path": bf,
            "content": content,
            "source_type": "legacy_batch",
        })
    return items


def find_reference_batch(outlines_dir, vol_idx, prod_start, prod_end,
                         prod_vol_total, ref_vol_total=None):
    """Map by proportion, prefer matching reference story-arc units, fall back to reference batch files.

    Args:
        outlines_dir: reference outline directory
        vol_idx: reference volume number
        prod_start, prod_end: production-novel segment chapter range (1-indexed)
        prod_vol_total: total chapters in this production volume
        ref_vol_total: total chapters in this reference volume (fetched automatically if None)

    Returns:
        concatenated reference story-arc / batch content
    """
    volumes = list_reference_volumes(outlines_dir)
    vol_info = None
    for vol in volumes:
        if vol["vol_idx"] == vol_idx:
            vol_info = vol
            break

    if vol_info is None:
        return ""

    if ref_vol_total is None:
        ref_vol_total = vol_info["chapter_count"]

    if ref_vol_total == 0 or prod_vol_total == 0:
        return ""

    # Map by proportion onto the reference chapter range (local numbers)
    frac_start = (prod_start - 1) / prod_vol_total
    frac_end = prod_end / prod_vol_total

    ref_start = max(1, int(frac_start * ref_vol_total) + 1)
    ref_end = min(ref_vol_total, int(frac_end * ref_vol_total))

    # Also load a small window on each side so the cut is not too sharp
    window = max(1, ref_vol_total // 20)  # 5% window
    ref_start = max(1, ref_start - window)
    ref_end = min(ref_vol_total, ref_end + window)

    # Virtual volume: convert local numbers to global (batch files use global numbers)
    meta = _load_volume_meta(vol_info["dir_path"])
    if meta:
        offset = meta["start_ch"] - 1
        ref_start += offset
        ref_end += offset

    # Current format: find story-arc units covering this range
    arc_contents = []
    for arc in _story_arc_files(vol_info["dir_path"]):
        if arc["end_ch"] >= ref_start and arc["start_ch"] <= ref_end:
            content = _read_file(arc["path"])
            if content:
                arc_contents.append(content)
    if arc_contents:
        return "\n\n---\n\n".join(arc_contents)

    # Legacy: find batch files covering this range
    batch_contents = []
    for bf in sorted(glob.glob(os.path.join(vol_info["dir_path"], "batch_*.md"))):
        bm = BATCH_FILE_RE.match(os.path.basename(bf))
        if not bm:
            continue
        batch_start = int(bm.group(1))
        batch_end = int(bm.group(2))
        # Check for overlap
        if batch_end >= ref_start and batch_start <= ref_end:
            content = _read_file(bf)
            if content:
                batch_contents.append(content)

    return "\n\n---\n\n".join(batch_contents)


def find_reference_chapter_outlines(outlines_dir, vol_idx, start_ch, end_ch):
    """Find chapter outlines in a given reference volume and chapter range, then concatenate them.

    Chapter-outline path format: outlines/vol_XX_<title>/chapter_outlines/chapter_NNN.md

    Args:
        outlines_dir: reference outline directory
        vol_idx: reference volume number
        start_ch, end_ch: chapter range (1-indexed)

    Returns:
        concatenated chapter-outline text
    """
    volumes = list_reference_volumes(outlines_dir)
    vol_info = None
    for vol in volumes:
        if vol["vol_idx"] == vol_idx:
            vol_info = vol
            break

    if vol_info is None:
        return ""

    ch_dir = os.path.join(vol_info["dir_path"], "chapter_outlines")
    if not os.path.isdir(ch_dir):
        return ""

    outlines = []
    for ch_num in range(start_ch, end_ch + 1):
        ch_file = os.path.join(ch_dir, f"chapter_{ch_num:03d}.md")
        content = _read_file(ch_file)
        if content:
            outlines.append(f"[Reference chapter {ch_num} outline]\n{content}")

    return "\n\n".join(outlines)
