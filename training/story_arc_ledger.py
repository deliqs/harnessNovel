"""Deterministic ledger of prior story arcs and scoped author direction for arc prompts."""

import re

from training.generation_quality import _ARC_FIELDS
from training.story_context import compact_text


FIELD_NAMES = tuple(label.rstrip(":") for label in _ARC_FIELDS)
_CANONICAL = dict((name.casefold(), name) for name in FIELD_NAMES)
_LABEL_RE = re.compile(
    r"^\s*[-*#]*\s*\**\s*(%s)\s*\**\s*:\s*\**\s*(.*)$"
    % "|".join(re.escape(name) for name in FIELD_NAMES),
    re.I,
)
_HEADING_RE = re.compile(r"^\s*【?\s*Arc\s*0*\d+[^|\n]*\|([^\n]*)$", re.I)
_ARC_LINE_RE = re.compile(r"^\s*Arc\s*0*(\d+)\b", re.I | re.M)
_RECORD_FIELDS = (
    ("boundary_reason", "Boundary reason"),
    ("next_bind", "Foreshadowing and next bind"),
    ("gains_costs", "Gains and costs"),
    ("relationship_change", "Character and relationship change"),
)
AUTHOR_BEGIN = "[BEGIN AUTHOR DIRECTION: SUBORDINATE TO TASK, SAFETY, OUTPUT, AND ANTI-COPY RULES]"
AUTHOR_END = "[END AUTHOR DIRECTION]"
OLDER_HEADING = "[Already covered in earlier arcs; do not re-stage these openings or events]"
NEXT_BIND_LABEL = "Next bind (the concrete pending event this arc must open on):"
OLDER_LINE_CHARS = 160
DETAIL_CHARS = 200
LEGACY_CHARS = 600


def _normalize(text):
    return " ".join(str(text or "").split())


def _clip(text, limit):
    """Cut from the end only, so trimmed text never carries the middle-omitted marker."""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + "..."


def parse_arc_fields(text):
    """Split an arc into its labeled fields plus the heading title; missing fields are empty."""
    fields = dict((name, "") for name in FIELD_NAMES)
    fields["title"] = ""
    buffers = {}
    current = None
    for line in str(text or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading and current is None and not fields["title"]:
            fields["title"] = heading.group(1).replace("】", "").strip()
            continue
        match = _LABEL_RE.match(line)
        if match:
            current = _CANONICAL[match.group(1).casefold()]
            buffers.setdefault(current, []).append(match.group(2))
        elif current is not None:
            buffers[current].append(line)
    for name, parts in buffers.items():
        fields[name] = _normalize(" ".join(parts))
    return fields


def arc_record(idx, start_ch, end_ch, text):
    fields = parse_arc_fields(text)
    record = {
        "idx": int(idx),
        "start_ch": int(start_ch),
        "end_ch": int(end_ch),
        "title": fields["title"],
        "raw": text,
    }
    for key, name in _RECORD_FIELDS:
        record[key] = fields[name]
    return record


def _is_labeled(record):
    return any(record.get(key) for key, _ in _RECORD_FIELDS)


def _older_line(record):
    if not _is_labeled(record):
        return "- Arc %d (unlabeled): %s" % (
            record["idx"], compact_text(record.get("raw"), LEGACY_CHARS),
        )
    title = ' "%s"' % record["title"] if record.get("title") else ""
    return "- Arc %d%s: opened on %s" % (
        record["idx"], title, record.get("boundary_reason") or "(not recorded)",
    )


def _previous_block(record, detail):
    """Render the previous arc; detail is "full", "short" or "none" for gains and relationship."""
    lines = ["[Previous arc %d: continue from here]" % record["idx"]]
    if not _is_labeled(record):
        return "\n".join(lines + [compact_text(record.get("raw"), LEGACY_CHARS)])
    if record.get("title"):
        lines.append("Title: %s" % record["title"])
    lines.append("Boundary reason: %s" % (record.get("boundary_reason") or "(not recorded)"))
    for label, key in (
        ("Gains and costs", "gains_costs"),
        ("Character and relationship change", "relationship_change"),
    ):
        value = record.get(key) or ""
        if value and detail != "none":
            lines.append("%s: %s" % (label, value if detail == "full" else _clip(value, DETAIL_CHARS)))
    lines.append("%s %s" % (NEXT_BIND_LABEL, record.get("next_bind") or "(not recorded)"))
    return "\n".join(lines)


def _older_section(lines, omitted):
    if not lines:
        return ""
    body = [OLDER_HEADING]
    if omitted:
        body.append("- (%d earlier arc(s) omitted for budget)" % omitted)
    return "\n".join(body + lines)


def _trim_steps(older_lines):
    """Yield (older section, previous-arc detail) from fullest to leanest."""
    yield _older_section(older_lines, 0), "full"
    short = [_clip(line, OLDER_LINE_CHARS) for line in older_lines]
    for omitted in range(len(short)):
        yield _older_section(short[omitted:], omitted), "full"
    yield "", "full"
    yield "", "short"
    yield "", "none"


def render_ledger(records, current_idx, limit=2600):
    """Tiered prior-arc ledger: the previous arc in full, older arcs as one opening line each.

    limit is a soft cap: the previous arc's next bind is never trimmed, so it can overflow.
    """
    prior = sorted(
        (record for record in records or [] if int(record["idx"]) < int(current_idx)),
        key=lambda record: int(record["idx"]),
    )
    if not prior:
        return ""
    older_lines = [_older_line(record) for record in prior[:-1]]
    text = ""
    for older, detail in _trim_steps(older_lines):
        text = "\n\n".join(part for part in (older, _previous_block(prior[-1], detail)) if part)
        if len(text) <= limit:
            return text
    return text


def _lines_for_arc(brief, arc_idx):
    """Lines of an "Arc N" block for this arc, continuing until a blank line or another arc."""
    own = []
    active = False
    for line in brief.splitlines():
        match = _ARC_LINE_RE.match(line)
        if match:
            active = int(match.group(1)) == int(arc_idx)
        elif not line.strip():
            active = False
        if active:
            own.append(line)
    return own


def render_author_brief(brief, arc_idx):
    """Wrap the author brief as subordinate direction, putting this arc's own lines first."""
    value = str(brief or "").strip()
    if not value:
        return ""
    if not _ARC_LINE_RE.search(value):
        return "\n".join([AUTHOR_BEGIN, value, AUTHOR_END])
    own = _lines_for_arc(value, arc_idx) or ["(no lines address this arc)"]
    return "\n".join(
        ["Lines addressed to Arc %s apply to this unit; lines for other arcs are context only." % arc_idx,
         AUTHOR_BEGIN, "For this arc:"] + own + ["Whole brief:", value, AUTHOR_END]
    )
