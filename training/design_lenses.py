"""Built-in design-critic lenses and the optional per-workspace lens file."""
from __future__ import annotations

import os
import re

from training.adaptive_builder import _read_file, _story_design_path, _write_file

_LENS_RELATIVE = "file_system/story_design/critic_lenses.md"
_HEADING_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_BUILTIN_LENSES = (
    ("Depth", "Look for thin worldbuilding, unexplained systems, and missing consequences. Flag claims that are stated but never shown working in the plot."),
    ("Engagement", "Look for hooks, curiosity, and reasons a reader would keep going. Flag stretches that explain or summarize without pulling the reader forward."),
    ("Intensity and pacing", "Look at the rise and fall of pressure, scene-to-scene energy, and whether the outline stalls or rushes. Flag plateaus, stacked peaks, and missing recovery."),
    ("Internal consistency", "Look for contradictions among worldview, outline, and character logic. Flag rules, timelines, and motives that cannot all be true at once."),
    ("Character stakes", "Look at what each major character stands to lose and why they cannot walk away. Flag goals without cost and conflicts that never become personal."),
)


def _lens_path(ws):
    return _story_design_path(ws, "critic_lenses.md")


def _parse_lens_file(text):
    matches = list(_HEADING_RE.finditer(text or ""))
    lenses = []
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        if not name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lenses.append((name, (text[match.end():end] or "").strip()))
    return lenses


def load_lenses(ws):
    """Return the effective (name, focus) list for this workspace."""
    lenses = list(_BUILTIN_LENSES)
    for name, focus in _parse_lens_file(_read_file(_lens_path(ws)) or ""):
        replaced = False
        for index, (existing, _focus) in enumerate(lenses):
            if existing.lower() == name.lower():
                lenses[index] = (existing, focus)
                replaced = True
                break
        if not replaced:
            lenses.append((name, focus))
    return lenses


def save_lens_file(ws, text):
    body = (text or "").strip()
    if not body or not _parse_lens_file(body):
        raise ValueError("The critic-lenses file needs at least one ## heading.")
    _write_file(_lens_path(ws), body)


def reset_lens_file(ws):
    path = _lens_path(ws)
    if os.path.isfile(path):
        os.remove(path)


def lens_file_status(ws):
    path = _lens_path(ws)
    return {
        "exists": os.path.isfile(path),
        "path": _LENS_RELATIVE,
        "lenses": [name for name, _focus in load_lenses(ws)],
    }
