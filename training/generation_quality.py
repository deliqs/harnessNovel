"""Deterministic generation diagnostics used before replacing prose artifacts."""

import re


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|[\u3400-\u9fff]")
_EN_HEADING_RE = re.compile(r"^\s*Chapter\s+0*(\d+)\s*:\s*(.*?)\s*$", re.I)
_ZH_HEADING_RE = re.compile(r"^\s*第\s*0*(\d+)\s*章\s*[：:]?\s*(.*?)\s*$")
_FORBIDDEN = (
    ("em dash", re.compile(r"—|——")),
    ("not-X-but-Y template", re.compile(
        r"(?:"
        r"(?:^|[\n.!?])[\"“'‘]?\s*not\s+(?!only\b)[^.\n]{0,40}?\bbut\b"
        r"|\b(?:is|are|was|were|be|been|being|'s|'re|'m)\s+not\b"
        r"(?!\s+only\b)[^.\n]{0,50}?\bbut\b"
        r"|\bnot\s+(?:a|an|the|just|merely|simply|exactly|because)\b"
        r"[^.\n]{0,40}?\bbut\b)"
        r"(?!\s+(?:I|he|she|it|they|we|you|there)\b)", re.I | re.M,
    )),
    ("not-only-but-also template", re.compile(
        r"\bnot only\b[^.\n]{0,80}\bbut(?:\s+also)?\b", re.I,
    )),
    ("Chinese contrast template", re.compile(
        r"(?:不是|并非)[^。！？\n]{0,60}(?:而是|却是)",
    )),
)
_COMMON_CAPITALS = {
    "Act", "Arc", "Book", "Chapter", "Character", "Current", "English",
    "Final", "Plot", "Protagonist", "Stage", "Story", "The", "This", "Volume",
}
_ARC_FIELDS = (
    "Plot function:", "Boundary reason:", "Rise and turn:", "Narrative stages:",
    "Protagonist action chain:", "Conflict and emotion curve:",
    "Core payoff or tension:", "Character and relationship change:",
    "Gains and costs:", "Foreshadowing and next bind:",
)


def word_count(text):
    return len(_WORD_RE.findall(text or ""))


def visible_char_count(text):
    return len(re.sub(r"\s+", "", text or ""))


def forbidden_diagnostics(text):
    findings = []
    for label, pattern in _FORBIDDEN:
        matches = list(pattern.finditer(text or ""))
        if matches:
            findings.append({
                "code": "forbidden_style",
                "reason": "%s appears %d time(s)" % (label, len(matches)),
                "examples": [match.group(0)[:100] for match in matches[:3]],
            })
    return findings


def infer_pov(text):
    tokens = [token.lower() for token in _WORD_RE.findall(text or "")]
    first = sum(token in {"i", "me", "my", "mine", "we", "us", "our"} for token in tokens)
    third = sum(token in {"he", "him", "his", "she", "her", "hers", "they", "them", "their"} for token in tokens)
    if max(first, third) < 4 or abs(first - third) < 2:
        return "unclear"
    return "first" if first > third else "third"


def infer_tense(text):
    tokens = [token.lower() for token in _WORD_RE.findall(text or "")]
    past = sum(
        token in {"was", "were", "had", "did", "said", "went", "came", "saw"}
        or (len(token) > 4 and token.endswith("ed"))
        for token in tokens
    )
    present = sum(token in {"is", "are", "has", "have", "does", "says", "goes", "comes", "sees"} for token in tokens)
    if max(past, present) < 4 or abs(past - present) < 2:
        return "unclear"
    return "past" if past > present else "present"


def extract_critical_anchors(text, limit=24):
    """Extract numeric facts and repeated name-like terms, avoiding title-case prose."""
    anchors = []
    for value in re.findall(r"(?<!\w)\d+(?:\.\d+)?(?:%|st|nd|rd|th)?", text or "", re.I):
        if value not in anchors:
            anchors.append(value)
    candidates = re.findall(
        r"\b[A-Z][A-Za-z'’-]{2,}(?:\s+[A-Z][A-Za-z'’-]{2,}){0,1}\b",
        text or "",
    )
    counts = {}
    for value in candidates:
        counts[value.casefold()] = counts.get(value.casefold(), 0) + 1
    for value in candidates:
        if counts[value.casefold()] >= 2 and value not in _COMMON_CAPITALS and value not in anchors:
            anchors.append(value)
    return anchors[:limit]


def _missing_anchors(text, anchors):
    folded = (text or "").casefold()
    return [anchor for anchor in anchors if str(anchor).casefold() not in folded]


def phrase_similarity(text, reference_text, phrase_words=6):
    """Return shared phrase coverage, ignoring punctuation and case."""
    left = [token.casefold() for token in _WORD_RE.findall(text or "")]
    right = [token.casefold() for token in _WORD_RE.findall(reference_text or "")]
    if len(left) < phrase_words or len(right) < phrase_words:
        return 0.0
    reference = {
        tuple(right[index:index + phrase_words])
        for index in range(len(right) - phrase_words + 1)
    }
    phrases = [
        tuple(left[index:index + phrase_words])
        for index in range(len(left) - phrase_words + 1)
    ]
    return sum(phrase in reference for phrase in phrases) / max(1, len(phrases))


def diagnose_chapter(text, expected_number, min_words=600, max_words=1500,
                     required_anchors=None, outline_text="", canon_text="",
                     expected_pov=None, expected_tense=None,
                     premature_reveal_markers=None, reference_text="",
                     allow_legacy_heading=False):
    """Validate a generated chapter and return actionable error/warning records."""
    errors = []
    warnings = []
    lines = (text or "").splitlines()
    heading = lines[0].strip() if lines else ""
    match = _EN_HEADING_RE.match(heading) or _ZH_HEADING_RE.match(heading)
    if not match:
        errors.append({"code": "heading", "reason": "First line must be 'Chapter %d: <title>'" % expected_number})
    else:
        if int(match.group(1)) != int(expected_number):
            errors.append({"code": "chapter_number", "reason": "Expected chapter %d, found chapter %s" % (expected_number, match.group(1))})
        if not match.group(2).strip(" .:：-_"):
            errors.append({"code": "chapter_title", "reason": "Chapter title is empty"})
        if _ZH_HEADING_RE.match(heading):
            item = {"code": "legacy_heading", "reason": "Legacy Chinese heading is readable, but new generation must emit English"}
            (warnings if allow_legacy_heading else errors).append(item)
    count = word_count("\n".join(lines[1:]))
    if count < min_words:
        errors.append({"code": "word_count", "reason": "Chapter has %d words; minimum is %d" % (count, min_words)})
    if count > max_words:
        errors.append({"code": "word_count", "reason": "Chapter has %d words; maximum is %d" % (count, max_words)})
    errors.extend(forbidden_diagnostics(text))

    anchors = list(required_anchors or [])
    if not anchors:
        anchors = extract_critical_anchors(outline_text + "\n" + canon_text, limit=16)
    missing = _missing_anchors(text, anchors)
    if missing:
        errors.append({"code": "anchor_retention", "reason": "Missing required canon/outline anchors: " + ", ".join(missing)})
    reveals = [marker for marker in (premature_reveal_markers or []) if str(marker).casefold() in (text or "").casefold()]
    if reveals:
        errors.append({"code": "premature_reveal", "reason": "Contains reveal markers scheduled for later: " + ", ".join(reveals)})

    pov = infer_pov(text)
    tense = infer_tense(text)
    if expected_pov and pov not in {"unclear", expected_pov}:
        errors.append({"code": "pov", "reason": "Expected %s-person POV; detected %s-person" % (expected_pov, pov)})
    if expected_tense and tense not in {"unclear", expected_tense}:
        errors.append({"code": "tense", "reason": "Expected %s tense; detected %s tense" % (expected_tense, tense)})
    similarity = phrase_similarity(text, reference_text)
    if reference_text and similarity > 0.22:
        errors.append({"code": "reference_similarity", "reason": "Shared six-word phrase coverage %.1f%% exceeds 22%%" % (similarity * 100)})
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {"word_count": count, "pov": pov, "tense": tense, "reference_phrase_similarity": round(similarity, 4)},
    }


def diagnose_rewrite(original, candidate, expected_number, required_anchors=None,
                     outline_text="", canon_text="", premature_reveal_markers=None,
                     reference_text=""):
    original_words = max(1, word_count("\n".join((original or "").splitlines()[1:])))
    anchors = list(required_anchors or extract_critical_anchors(original))
    result = diagnose_chapter(
        candidate, expected_number,
        min_words=max(1, round(original_words * 0.90)),
        max_words=max(2, round(original_words * 1.12)),
        required_anchors=anchors,
        outline_text=outline_text, canon_text=canon_text,
        expected_pov=infer_pov(original), expected_tense=infer_tense(original),
        premature_reveal_markers=premature_reveal_markers,
        reference_text="",
    )
    original_heading = (original or "").splitlines()[0].strip() if (original or "").splitlines() else ""
    candidate_heading = (candidate or "").splitlines()[0].strip() if (candidate or "").splitlines() else ""
    if original_heading != candidate_heading:
        result["errors"].append({
            "code": "chapter_title_changed",
            "reason": "Editor must preserve the original chapter heading exactly",
        })
        result["valid"] = False
    original_similarity = phrase_similarity(original, reference_text)
    candidate_similarity = phrase_similarity(candidate, reference_text)
    if reference_text and candidate_similarity > max(0.12, original_similarity + 0.06):
        result["errors"].append({
            "code": "reference_similarity_regression",
            "reason": "Reference phrase coverage rose from %.1f%% to %.1f%%" % (original_similarity * 100, candidate_similarity * 100),
        })
        result["valid"] = False
    result["metrics"].update({
        "original_reference_phrase_similarity": round(original_similarity, 4),
        "candidate_reference_phrase_similarity": round(candidate_similarity, 4),
        "required_anchors": anchors,
    })
    return result


def diagnose_story_arc(text, arc_index, start_chapter, end_chapter,
                       target_chars=1000, required_anchors=None, reference_text=""):
    errors = []
    heading = re.compile(
        r"^\s*【Arc\s*0*%d\s*:\s*Chapters\s+0*%d\s*[-–—]\s*0*%d\s*\|\s*\S.+】" %
        (arc_index, start_chapter, end_chapter), re.I,
    )
    if not heading.search(text or ""):
        errors.append({"code": "arc_heading", "reason": "Arc heading must identify arc %d and chapters %d-%d" % (arc_index, start_chapter, end_chapter)})
    chars = visible_char_count(text)
    lower, upper = max(120, round(target_chars * 0.35)), max(300, round(target_chars * 1.6))
    if chars < lower or chars > upper:
        errors.append({"code": "arc_length", "reason": "Arc has %d visible characters; expected %d-%d" % (chars, lower, upper)})
    missing_fields = [field for field in _ARC_FIELDS if field.casefold() not in (text or "").casefold()]
    if missing_fields:
        errors.append({"code": "arc_fields", "reason": "Missing arc fields: " + ", ".join(missing_fields)})
    missing = _missing_anchors(text, required_anchors or [])
    if missing:
        errors.append({"code": "arc_obligations", "reason": "Missing planned obligation anchors: " + ", ".join(missing)})
    similarity = phrase_similarity(text, reference_text)
    if reference_text and similarity > 0.22:
        errors.append({"code": "reference_similarity", "reason": "Arc is too phrase-similar to the reference sample (%.1f%%)" % (similarity * 100)})
    return {"valid": not errors, "errors": errors, "warnings": [], "metrics": {"visible_chars": chars, "reference_phrase_similarity": round(similarity, 4)}}


def diagnose_chapter_outline(text, expected_number, required_anchors=None,
                             allow_legacy_heading=False):
    """Validate the compact outline contract before replacing an existing outline."""
    errors = []
    heading = re.compile(
        r"^\s*【(?:Chapter\s+0*%d\s+outline|第\s*0*%d\s*章(?:大纲)?)】" %
        (expected_number, expected_number), re.I,
    )
    if not heading.search(text or ""):
        errors.append({"code": "outline_heading", "reason": "Outline heading must identify chapter %d" % expected_number})
    elif re.match(r"^\s*【第", text or "") and not allow_legacy_heading:
        errors.append({"code": "legacy_heading", "reason": "New outlines must emit an English chapter heading"})
    sections = (
        ("Story line", ("# Story line", "# 故事线")),
        ("Chapter rhythm", ("# Chapter rhythm", "# 章节节奏")),
        ("Chapter summary", ("# Chapter summary", "# 章节摘要", "# 本章概述")),
    )
    for label, aliases in sections:
        if not any(alias.casefold() in (text or "").casefold() for alias in aliases):
            errors.append({"code": "outline_section", "reason": "Missing %s section" % label})
    missing = _missing_anchors(text, required_anchors or [])
    if missing:
        errors.append({"code": "outline_anchors", "reason": "Missing required arc anchors: " + ", ".join(missing)})
    return {"valid": not errors, "errors": errors, "warnings": [], "metrics": {"word_count": word_count(text)}}
