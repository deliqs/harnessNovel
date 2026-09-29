"""Deterministic sibling review for candidate story arcs and per-arc outcome reporting."""

import re

from training.generation_quality import phrase_similarity
from training.story_arc_ledger import _clip, parse_arc_fields


BOUNDARY_PHRASE_WORDS = 4
BOUNDARY_PHRASE_THRESHOLD = 0.3
CONTENT_MIN_SHARED = 4
CONTENT_MIN_WORDS = 3
CONTENT_OVERLAP_THRESHOLD = 0.4
TEXT_PHRASE_THRESHOLD = 0.22
OUTCOME_STATUSES = ("written", "kept", "retried", "rejected", "skipped", "no_output")
_QUOTE_CHARS = 125
_BIND_CHARS = 125
EXCERPT_BEGIN = "[BEGIN UNTRUSTED WORKSPACE DATA: SIBLING ARC EXCERPT]"
EXCERPT_END = "[END UNTRUSTED WORKSPACE DATA: SIBLING ARC EXCERPT]"
_CONTENT_WORD_RE = re.compile(r"[a-z]+(?:['\u2019][a-z]+)*")
# Boundary reasons explain why a unit starts and ends at given chapters, so the
# framing words carry no event content and are ignored with ordinary stopwords.
_STOPWORDS = frozenset((
    "a an the and or but nor of to in on at by for with from into onto as is are was were "
    "be been being it its this that these those he she they him her them his their i we you "
    "when while once after before so then than which who whom where why how what not no "
    "over under up out off through until there here has have had can could will would "
    "chapter chapters ch starts start ends end opens closes arc because unit"
).split())


_CLOSE_RE = re.compile(r"\b(?:ends|closes|closing)\b", re.I)
_CHAPTER_RANGE_RE = re.compile(r"Chapters?\s+0*\d+\s*[-\u2013\u2014]\s*0*(\d+)", re.I)


def _end_chapter(text):
    match = _CHAPTER_RANGE_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _cut_at_close(boundary, end_ch):
    """Keep the part of a boundary reason before it describes how the unit closes.

    The end chapter number is the primary cue and a closing verb the fallback; an opening
    left with fewer than CONTENT_MIN_WORDS content words falls back to the whole field.
    """
    match = None
    if end_ch is not None:
        match = re.search(r"\bChapter\s+0*%d\b" % end_ch, boundary, re.I)
    match = match or _CLOSE_RE.search(boundary)
    opening = boundary[:match.start()].rstrip(" ,;:-") if match else boundary
    return opening if len(content_words(opening)) >= CONTENT_MIN_WORDS else boundary


def opening_event(text):
    """The opening part of the Boundary reason, cut before the close or the end chapter."""
    boundary = " ".join(parse_arc_fields(text)["Boundary reason"].split())
    return _cut_at_close(boundary, _end_chapter(text))


def content_words(text):
    """Distinct casefolded content words in order, without digits, possessives, or framing words."""
    words = []
    for token in _CONTENT_WORD_RE.findall(str(text or "").casefold()):
        word = re.sub(r"['\u2019]s$", "", token)
        if word not in _STOPWORDS and word not in words:
            words.append(word)
    return words


def _shared_content(candidate_opening, sibling_opening):
    """Return shared content words and their share of the shorter opening's words."""
    candidate = content_words(candidate_opening)
    sibling = content_words(sibling_opening)
    shared = [word for word in candidate if word in sibling]
    smaller = min(len(candidate), len(sibling))
    return shared, len(shared) / smaller if smaller else 0.0, smaller


def _required_shared(smaller):
    """Shared words needed: CONTENT_MIN_SHARED, eased for openings of only a few content words."""
    if smaller < CONTENT_MIN_WORDS:
        return None
    return max(CONTENT_MIN_WORDS, min(CONTENT_MIN_SHARED, smaller - 1))


def _collision_reason(idx, score, shared, smaller):
    """Return (reason, severity): "block" when a phrase check fired, "warn" for content only."""
    literal = (
        score["boundary_similarity"] >= BOUNDARY_PHRASE_THRESHOLD
        or score["text_similarity"] >= TEXT_PHRASE_THRESHOLD
    )
    severity = "block" if literal else "warn"
    if score["boundary_similarity"] >= BOUNDARY_PHRASE_THRESHOLD:
        return "re-stages arc %d's opening event (boundary phrase similarity %d%%)" % (
            idx, round(score["boundary_similarity"] * 100),
        ), severity
    required = _required_shared(smaller)
    if required and len(shared) >= required and score["content_overlap"] >= CONTENT_OVERLAP_THRESHOLD:
        return "re-stages arc %d's opening event (%d shared boundary words: %s)" % (
            idx, len(shared), ", ".join(shared),
        ), severity
    if score["text_similarity"] >= TEXT_PHRASE_THRESHOLD:
        return "repeats arc %d's text (phrase similarity %d%%)" % (
            idx, round(score["text_similarity"] * 100),
        ), severity
    return "", ""


def review_against_siblings(candidate, siblings):
    """Compare a candidate arc's opening with (idx, text) siblings; the lowest colliding idx wins."""
    boundary = opening_event(candidate)
    result = {"collision": None, "severity": "", "reason": "", "scores": []}
    for idx, text in sorted(siblings or [], key=lambda item: int(item[0])):
        sibling_boundary = opening_event(text)
        shared, overlap, smaller = _shared_content(boundary, sibling_boundary)
        score = {
            "arc": int(idx),
            "boundary_similarity": round(phrase_similarity(
                boundary, sibling_boundary, phrase_words=BOUNDARY_PHRASE_WORDS,
            ), 4),
            "content_overlap": round(overlap, 4),
            "text_similarity": round(phrase_similarity(candidate, text), 4),
        }
        result["scores"].append(score)
        reason, severity = _collision_reason(int(idx), score, shared, smaller)
        if reason and result["collision"] is None:
            result["collision"] = int(idx)
            result["severity"] = severity
            result["reason"] = reason
    return result


def retry_note(collision_idx, sibling_text):
    """Writer-facing note after a sibling collision, at most about 600 characters.

    Sibling text is quoted only inside the delimited excerpt block, never in the instruction.
    """
    fields = parse_arc_fields(sibling_text)
    opening = _clip(" ".join(fields["Boundary reason"].split()).rstrip("."), _QUOTE_CHARS) or "(not recorded)"
    next_bind = _clip(" ".join(fields["Foreshadowing and next bind"].split()).rstrip("."), _BIND_CHARS)
    target = (
        "open on the pending event in arc %d's next bind (quoted below)" % collision_idx
        if next_bind else "open on a different concrete event that follows arc %d" % collision_idx
    )
    lines = [
        "Your draft re-staged arc %d's opening event (quoted below). This arc must instead %s, "
        "and must not repeat arc %d's opening or its beats." % (collision_idx, target, collision_idx),
        EXCERPT_BEGIN,
        "Arc %d opening event: %s" % (collision_idx, opening),
    ]
    if next_bind:
        lines.append("Arc %d next bind: %s" % (collision_idx, next_bind))
    lines.append(EXCERPT_END)
    return "\n".join(lines)


def outcome(arc, status, reason=""):
    if status not in OUTCOME_STATUSES:
        raise ValueError("Unknown arc outcome status %r; expected one of %s" % (
            status, ", ".join(OUTCOME_STATUSES),
        ))
    return {"arc": int(arc), "status": status, "reason": reason}


def render_outcomes(outcomes):
    if not outcomes:
        return ""
    lines = ["Per-arc outcomes:"]
    for item in outcomes:
        line = "- arc %d: %s" % (item["arc"], item["status"])
        if item.get("reason"):
            line += ": %s" % item["reason"]
        lines.append(line)
    return "\n".join(lines)
