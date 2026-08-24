"""Workspace reads and background CLI tasks for the web workbench."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


WORKSPACE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w .\-\u4e00-\u9fff]{0,79}$", re.UNICODE)
STAGE_RE = re.compile(r"^#{1,6}\s*(?:舞台|stage)\s*0*(\d+)", re.IGNORECASE | re.MULTILINE)
PHASE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:第\s*)?(?:阶段|phase)\s*0*(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)
REFERENCE_ARC_PATH_RE = re.compile(
    r"^reference/outlines/([^/]+)/story_arcs/arc_\d+_ch(\d+)_(\d+)\.md$",
    re.IGNORECASE,
)
REFERENCE_VOLUME_DIR_RE = re.compile(r"^vol_(\d+)_")
EDITABLE_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv"}
TASK_LABELS = {
    "workspace_init": "Create workspace",
    "init": "Initialize and deconstruct the reference novel",
    "reference_resume": "Continue deconstructing the reference novel",
    "world_import": "Import and build the target-world knowledge base",
    "world_build": "Build the target-world knowledge base",
    "novel_outline": "Design core gameplay and book stages",
    "design_concept": "Generate worldview, rough outline, and phase outline",
    "stage_design": "Generate long mainline and stage roadmap",
    "story_design": "Rebuild design assets",
    "story_design_extend": "Extend the mainline, character arcs, and later stages",
    "stage_insert": "Insert a new stage",
    "mechanics_init": "Initialize the system panel",
    "story_arcs": "Generate story arcs",
    "chapter_outlines": "Generate chapter outlines",
    "write": "Generate draft",
    "novel_name_synopsis": "Suggest title and synopsis",
    "volume_outline": "Generate legacy volume outline",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def valid_workspace_name(name: str) -> bool:
    return bool(name and WORKSPACE_NAME_RE.fullmatch(name))


def require_workspace_name(name: str) -> str:
    name = (name or "").strip()
    if not valid_workspace_name(name):
        raise ValueError("Workspace name may contain Chinese, letters, digits, spaces, underscores, dots, and hyphens (max 80 characters).")
    return name


def _positive_int(value: Any, field_name: str, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer.") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be a positive integer.")
    return parsed


def story_arc_title(content: str) -> str:
    """Extract the story-arc name from common heading forms."""
    heading = next((line.strip() for line in (content or "").splitlines() if line.strip()), "")
    if not heading or not re.search(r"(?:情节|Arc)", heading, re.IGNORECASE):
        return ""
    heading = re.sub(r"^#{1,6}\s*", "", heading).strip()
    for pattern in (
        r"(?:[｜|]|\s+[—–-]\s+)\s*([^】\]\n]+?)\s*[】\]]?\s*$",
        r"第\s*\d+\s*[-—–至到]\s*\d+\s*章\s*[）)]?\s*[：:]\s*([^】\]\n]+?)\s*[】\]]?\s*$",
    ):
        matched = re.search(pattern, heading)
        if matched:
            return matched.group(1).strip()
    return ""


class WorkspaceStore:
    """Only allow workspaces under the configured root so web file APIs cannot escape it."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def set_root(self, root: str | Path) -> None:
        path = Path(root).expanduser().resolve()
        if path.exists() and not path.is_dir():
            raise ValueError("Workspace root points at a file and cannot be used.")
        path.mkdir(parents=True, exist_ok=True)
        self.root = path

    def workspace_path(self, name: str) -> Path:
        safe_name = require_workspace_name(name)
        path = (self.root / safe_name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Invalid workspace path.") from exc
        return path

    def list_workspaces(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        items = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.name.startswith(".") or not valid_workspace_name(path.name):
                continue
            summary = self.summary(path.name)
            items.append({
                "name": path.name,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "next_action": summary["next_action"],
                "progress": summary["progress"],
            })
        return sorted(items, key=lambda item: item["updated_at"], reverse=True)

    def delete_workspace(self, name: str) -> dict[str, Any]:
        """Delete a workspace that is a direct child of the workspace root."""
        safe_name = require_workspace_name(name)
        path = self.workspace_path(safe_name)
        if not path.is_dir():
            raise FileNotFoundError(safe_name)
        # workspace_path already uses resolve + relative_to to block path escape; also require
        # the target to be a direct child of the root so later path-rule changes cannot widen deletes.
        if path.parent != self.root or path == self.root:
            raise ValueError("Invalid workspace delete path.")
        shutil.rmtree(path)
        return {"deleted": True, "workspace": safe_name}

    def _volume_details(self, base: Path, fs: Path) -> list[dict[str, Any]]:
        """List story arcs and chapter data for each stage/volume."""
        details = []
        stage_roadmap = _read_file(fs / "story_design" / "stage_roadmap.md") if False else ""
        # Scan story_arcs and chapter_outlines directories
        for kind, dirname in [("story_arcs", "story_arcs"), ("chapter_outlines", "chapter_outlines")]:
            pass
        # Collect all volume numbers
        vol_nums = set()
        for dirname in ["story_arcs", "chapter_outlines", "chapters"]:
            dir_path = fs / dirname
            if dir_path.is_dir():
                for child in dir_path.iterdir():
                    import re as _re
                    m = _re.match(r"vol_(\d+)", child.name)
                    if m:
                        vol_nums.add(int(m.group(1)))
        for vol in sorted(vol_nums):
            vol_idx = vol
            arc_dir = fs / "story_arcs" / f"vol_{vol_idx:02d}"
            arcs = []
            if arc_dir.is_dir():
                for fname in sorted(arc_dir.iterdir()):
                    import re as _re
                    m = _re.match(r"arc_(\d+)_ch(\d+)_(\d+)\.md", fname.name)
                    if m:
                        try:
                            heading = fname.read_text(encoding="utf-8")[:1000]
                        except (OSError, UnicodeDecodeError):
                            heading = ""
                        arcs.append({
                            "idx": int(m.group(1)),
                            "start_ch": int(m.group(2)),
                            "end_ch": int(m.group(3)),
                            "title": story_arc_title(heading),
                        })
            ch_dir = fs / "chapter_outlines" / f"vol_{vol_idx:02d}"
            ch_count = 0
            if ch_dir.is_dir():
                ch_count = sum(1 for f in ch_dir.iterdir() if f.name.startswith("chapter_") and f.suffix == ".md")
            details.append({"volume": vol_idx, "arcs": arcs, "chapter_outline_count": ch_count})
        return details

    def summary(self, name: str) -> dict[str, Any]:
        base = self.workspace_path(name)
        if not base.is_dir():
            raise FileNotFoundError(name)
        fs = base / "file_system"
        reference = base / "reference"
        sample = reference / "sample_novel.txt"
        ref_chapters = self._count_files(reference / "chapters", {".txt", ".md"})
        ref_arcs = self._count_files(reference / "outlines", {".md"}, "story_arcs")
        reference_state = self._load_json(reference / "import_state.json") or {}
        analysis_state = self._load_json(reference / "analysis_state.json") or {}
        card_state = analysis_state.get("chapter_cards") if isinstance(analysis_state, dict) else {}
        card_state = card_state if isinstance(card_state, dict) else {}
        card_count = int(card_state.get("complete_count") or 0)
        arc_progress = self._reference_story_arc_end(reference / "outlines")
        processed_chapters = max(int(reference_state.get("processed_chapters") or 0), arc_progress, card_count)
        total_chapters = reference_state.get("total_chapters")
        total_chapters = int(total_chapters) if isinstance(total_chapters, int) or str(total_chapters).isdigit() else None
        # import_state is the new-flow breakpoint; older workspaces without it are treated as fully deconstructed.
        if not reference_state and sample.exists() and ref_chapters > 0:
            total_chapters = ref_chapters
            processed_chapters = max(processed_chapters, ref_chapters)
        # Tolerance: historical volume meta.json end_ch may keep a stale value (after a source change or partial deconstruction),
        # so arc_progress can exceed actual deconstruction. Capped at the source chapter count.
        if total_chapters:
            processed_chapters = min(processed_chapters, total_chapters)
            arc_progress = min(arc_progress, total_chapters)
        reference_analysis_complete = (
            bool(reference_state.get("is_complete"))
            if reference_state
            else bool(sample.exists() and ref_chapters > 0)
        )
        # Reference deconstruction only produces the book outline, volume outlines, and story arcs.
        # Legacy reference worldview no longer affects deconstruction completion.
        reference_complete = reference_analysis_complete
        manifest = self._load_json(fs / "world_knowledge" / "manifest.json") or {}
        source_records = manifest.get("sources", []) if isinstance(manifest.get("sources", []), list) else []
        world_sources = len(source_records)
        world_final_dir = fs / "world_knowledge" / "worlds" / "_final"
        world_sections = sum(
            1 for path in world_final_dir.glob("*.md")
            if path.is_file() and path.read_text(encoding="utf-8", errors="ignore").strip()
        ) if world_final_dir.is_dir() else 0
        world_enabled = bool(manifest.get("enabled", True)) if isinstance(manifest, dict) else True
        design_dir = fs / "story_design"

        def _has(path):
            return bool(path.exists() and path.stat().st_size > 0)

        # Step 1: book design (worldview + rough outline + standalone phase outline).
        concept_defs = [
            ("worldview", "Worldview", design_dir / "worldview.md", None),
            ("rough_outline", "Rough outline", design_dir / "rough_outline.md", design_dir / "core_gameplay.md"),
            ("stage_outline", "Phase outline", design_dir / "stage_outline.md", None),
        ]
        concept_assets = [
            {"key": key, "label": label, "done": bool(_has(primary) or (alt is not None and _has(alt)))}
            for key, label, primary, alt in concept_defs
        ]
        concept_ready = all(item["done"] for item in concept_assets)
        # Step 2: stage design (long mainline + stage roadmap).
        stage_defs = [
            ("long_mainline", "Long mainline", design_dir / "long_mainline.md"),
            ("stage_roadmap", "Stage roadmap", design_dir / "stage_roadmap.md"),
        ]
        stage_assets = [
            {"key": key, "label": label, "done": _has(path)} for key, label, path in stage_defs
        ]
        stage_count = self._stage_count(design_dir / "stage_roadmap.md")
        stage_outline_path = design_dir / "stage_outline.md"
        stage_outline_text = (
            stage_outline_path.read_text(encoding="utf-8") if _has(stage_outline_path) else ""
        )
        stage_outline_count = len(PHASE_HEADING_RE.findall(stage_outline_text))
        stage_assets_exist = all(item["done"] for item in stage_assets)
        stage_ready = stage_assets_exist and (
            not stage_outline_count or stage_count == stage_outline_count
        )
        # Keep ready_count/total_count/assets for older clients (both steps combined).
        design_assets = concept_assets + stage_assets
        name_synopsis_path = fs / "novel_name_synopsis.md"
        design_assets.append({"key": "name_synopsis", "label": "Title and synopsis", "done": _has(name_synopsis_path)})
        design_ready = (3 if concept_ready else 0) + (2 if stage_ready else 0)
        design_state = self._load_json(design_dir / "design_state.json") or {}
        stage_ready = stage_ready and int(design_state.get("stage_pipeline_version") or 0) == 2
        concept_revision = int(design_state.get("concept_revision") or 0) if isinstance(design_state, dict) else 0
        stage_synced_revision = int(design_state.get("stage_synced_concept_revision") or 0) if isinstance(design_state, dict) else 0
        stage_sync_pending = bool(
            stage_assets_exist
            and isinstance(design_state, dict)
            and design_state.get("pending_reference_stage_sync")
        )
        chapter_usage = self._load_json(design_dir / "chapter_usage_state.json") or {}
        if not isinstance(chapter_usage, dict):
            chapter_usage = {}
        unused_reference_chapters = 0
        legacy_usage_baseline = int(design_state.get("reference_processed_chapters") or 0) if not chapter_usage else 0
        cards_dir = reference / "chapter_cards"
        if cards_dir.is_dir():
            for card_path in cards_dir.glob("chapter_*.json"):
                matched = re.search(r"chapter_(\d+)", card_path.name)
                if not matched:
                    continue
                record = chapter_usage.get(str(int(matched.group(1))), {})
                used = bool(record.get("used")) if isinstance(record, dict) else bool(record)
                used = used or (not chapter_usage and int(matched.group(1)) <= legacy_usage_baseline)
                if not used:
                    unused_reference_chapters += 1
        raw_design_baseline = design_state.get("reference_processed_chapters") if isinstance(design_state, dict) else None
        design_baseline = int(raw_design_baseline) if isinstance(raw_design_baseline, int) or str(raw_design_baseline).isdigit() else None
        new_reference_chapters = max(0, processed_chapters - design_baseline) if design_baseline is not None else 0
        direction_history = self._load_json(design_dir / "direction_history.json") or []
        if not isinstance(direction_history, list):
            direction_history = []
        mechanics = self._load_json(fs / "mechanics" / "system_panel.json") or self._load_json(fs / "mechanics" / "profile.json") or {}
        from types import SimpleNamespace
        from training.adaptive_builder import chapter_finalization_status
        finalized_chapters = chapter_finalization_status(
            SimpleNamespace(file_system=str(fs))
        )
        mechanics_mode = (
            mechanics.get("mode")
            or ((mechanics.get("profile") or {}).get("mode") if isinstance(mechanics, dict) else None)
            or "Not initialized"
        )
        story_arcs = self._count_files(fs / "story_arcs", {".md"})
        chapter_outlines = self._count_files(fs / "chapter_outlines", {".md"})
        chapters = self._count_files(fs / "chapters", {".md", ".txt"})

        steps = [
            ("Reference deconstruction", sample.exists() and ref_chapters > 0),
            ("Core design", concept_ready and stage_ready),
            ("Story arcs", story_arcs > 0),
            ("Chapter outlines", chapter_outlines > 0),
            ("Draft", chapters > 0),
        ]
        completed = sum(done for _, done in steps)
        if not sample.exists():
            next_action = "Upload a reference novel and initialize"
        elif not (concept_ready and stage_ready):
            next_action = "Generate core gameplay and the stage roadmap"
        elif story_arcs == 0:
            next_action = "Choose a stage and generate story arcs"
        elif chapter_outlines == 0:
            next_action = "Generate chapter outlines"
        elif chapters == 0:
            next_action = "Generate draft"
        else:
            next_action = "Continue with the next stage, or edit existing assets"

        return {
            "name": name,
            "root": str(base),
            "reference": {
                "has_sample": sample.exists(),
                "chapter_count": ref_chapters,
                "story_arc_count": ref_arcs,
                "chapter_card_count": card_count,
                "segmented_chapter_count": arc_progress,
                "source_name": (reference_state.get("source_name") or sample.name) if sample.exists() else "",
                "source_encoding": reference_state.get("source_encoding") or "",
                "processed_chapter_count": processed_chapters,
                "total_chapter_count": total_chapters,
                "analysis_complete": reference_analysis_complete,
                "is_complete": reference_complete,
            },
            "world_knowledge": {
                "enabled": world_enabled,
                "source_count": world_sources,
                "final_section_count": world_sections,
                "ready": world_sections == 7,
                "sources": [
                    {
                        "id": str(source.get("id") or ""),
                        "file_name": re.sub(
                            r"^[0-9a-f]{16}_", "", str(source.get("file_name") or "Unnamed source"),
                            flags=re.IGNORECASE,
                        ),
                        "size": source.get("size") if isinstance(source.get("size"), int) else 0,
                    }
                    for source in source_records
                    if isinstance(source, dict)
                ],
            },
            "story_design": {
                "ready_count": design_ready,
                "total_count": 5,
                "concept_ready": concept_ready,
                "stage_ready": stage_ready,
                "stage_assets_exist": stage_assets_exist,
                "concept_assets": concept_assets,
                "stage_assets": stage_assets,
                "assets": design_assets,
                "stage_count": stage_count,
                "has_name_synopsis": name_synopsis_path.exists(),
                "reference_baseline_chapters": design_baseline,
                "new_reference_chapter_count": new_reference_chapters,
                "unused_reference_chapter_count": unused_reference_chapters,
                "concept_revision": concept_revision,
                "stage_synced_concept_revision": stage_synced_revision,
                "stage_sync_pending": stage_sync_pending,
                "extension_count": int(design_state.get("extension_count") or 0) if isinstance(design_state, dict) else 0,
                "direction_history": direction_history[-20:] if direction_history else [],
            },
            "mechanics": {"mode": mechanics_mode},
            "finalized_chapters": finalized_chapters,
            "volumes": self._volume_details(base, fs),
            "writing": {
                "story_arc_count": story_arcs,
                "chapter_outline_count": chapter_outlines,
                "chapter_count": chapters,
            },
            "steps": [{"name": step, "done": bool(done)} for step, done in steps],
            "progress": {"completed": completed, "total": len(steps)},
            "next_action": next_action,
        }

    @staticmethod
    def _reference_story_arc_end(outlines_dir: Path) -> int:
        """Return actual coverage, supporting local numbering of natural volumes and global ranges of virtual volumes."""
        if not outlines_dir.is_dir():
            return 0
        local_coverage = 0
        global_endpoints: list[int] = []
        pattern = re.compile(r"^arc_\d+_ch\d+_(\d+)\.md$", re.IGNORECASE)
        for volume_dir in sorted(path for path in outlines_dir.iterdir() if path.is_dir()):
            arc_dir = volume_dir / "story_arcs"
            if not arc_dir.is_dir():
                continue
            volume_end = 0
            for path in arc_dir.glob("arc_*_ch*_*.md"):
                matched = pattern.match(path.name)
                if matched:
                    volume_end = max(volume_end, int(matched.group(1)))
            if not volume_end:
                continue
            local_coverage += volume_end
            meta = WorkspaceStore._load_json(volume_dir / "meta.json") or {}
            meta_end = meta.get("end_ch") if isinstance(meta, dict) else None
            if isinstance(meta_end, int) and meta_end > 0:
                global_endpoints.append(meta_end)
        return max(global_endpoints) if global_endpoints else local_coverage

    def tree(self, name: str, max_entries: int = 800) -> list[dict[str, Any]]:
        base = self.workspace_path(name)
        if not base.is_dir():
            raise FileNotFoundError(name)
        entries: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(base, followlinks=False):
            root_path = Path(root)
            relative_root = root_path.relative_to(base).as_posix()
            hidden_bulk_dirs = {"chapter_cards", "chapters"} if relative_root == "reference" else set()
            dirs[:] = sorted(
                directory
                for directory in dirs
                if not directory.startswith(".")
                and directory != "__pycache__"
                and directory not in hidden_bulk_dirs
            )
            for directory in dirs:
                path = root_path / directory
                entries.append({"path": str(path.relative_to(base)), "type": "directory"})
                if len(entries) >= max_entries:
                    return entries
            for filename in sorted(files):
                if filename.startswith(".") or filename == ".DS_Store":
                    continue
                path = root_path / filename
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                entries.append({"path": str(path.relative_to(base)), "type": "file", "size": size})
                if len(entries) >= max_entries:
                    return entries
        return entries

    def read_file(self, name: str, relative_path: str) -> dict[str, Any]:
        path = self._safe_file_path(name, relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        if path.suffix.lower() not in EDITABLE_EXTENSIONS:
            raise ValueError("This file type cannot be previewed in the workbench.")
        size = path.stat().st_size
        if size > 1_500_000:
            raise ValueError("File is over 1.5MB; the workbench will not open it. Use a local editor.")
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"path": str(path.relative_to(self.workspace_path(name))), "content": content, "size": size}

    def write_file(self, name: str, relative_path: str, content: str) -> None:
        path = self._safe_file_path(name, relative_path)
        if relative_path.replace("\\", "/").startswith("reference/"):
            raise ValueError("Reference deconstruction assets are read-only and cannot be edited in the workbench.")
        if path.suffix.lower() not in EDITABLE_EXTENSIONS:
            raise ValueError("This file type cannot be edited in the workbench.")
        if len(content.encode("utf-8")) > 1_500_000:
            raise ValueError("Saved content exceeds 1.5MB.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def reference_arc_chapters(self, name: str, relative_path: str) -> dict[str, Any]:
        """Return deconstruction fact cards for chapters covered by a reference story arc, for read-only browsing."""
        normalized = relative_path.replace("\\", "/")
        matched = REFERENCE_ARC_PATH_RE.fullmatch(normalized)
        if not matched:
            raise ValueError("Only chapters for a reference story arc can be viewed.")
        arc_path = self._safe_file_path(name, normalized)
        if not arc_path.is_file():
            raise FileNotFoundError(relative_path)

        volume_dir_name, start_text, end_text = matched.groups()
        start, end = int(start_text), int(end_text)
        base = self.workspace_path(name)
        sample = base / "reference" / "sample_novel.txt"
        card_dir = base / "reference" / "chapter_cards"
        volume_dir = base / "reference" / "outlines" / volume_dir_name
        meta = self._load_json(volume_dir / "meta.json") or {}
        volume_match = REFERENCE_VOLUME_DIR_RE.match(volume_dir_name)
        volume_index = int(volume_match.group(1)) if volume_match else 0

        source_chapters: list[dict[str, Any]] | None = None

        def _source() -> list[dict[str, Any]]:
            nonlocal source_chapters
            if source_chapters is None and sample.is_file():
                from training.outline_builder import split_chapters

                _, source_chapters = split_chapters(str(sample))
            return source_chapters or []

        # Resolve book chapter numbers covered by this story arc: volume metadata first, then fact cards, then source splits.
        chapter_numbers: list[int] = []
        if isinstance(meta, dict) and meta.get("start_ch") is not None:
            # After intelligent volume split, story-arc filenames keep book-level chapter ranges.
            chapter_numbers = list(range(start, end + 1))
        elif card_dir.is_dir() and volume_index:
            for card_path in sorted(card_dir.glob("chapter_*.json")):
                card = self._load_json(card_path) or {}
                if not isinstance(card, dict):
                    continue
                if int(card.get("volume_index") or 0) != volume_index:
                    continue
                local = int(card.get("volume_chapter") or 0)
                if start <= local <= end:
                    chapter_numbers.append(int(card.get("chapter") or 0))
        if not chapter_numbers and volume_index:
            local_chapter = 0
            for index, chapter in enumerate(_source(), start=1):
                if int(chapter.get("volume_idx") or 0) != volume_index - 1:
                    continue
                local_chapter += 1
                if start <= local_chapter <= end:
                    chapter_numbers.append(index)

        chapters: list[dict[str, Any]] = []
        for chapter_number in chapter_numbers:
            if chapter_number < 1:
                continue
            card = self._load_json(card_dir / f"chapter_{chapter_number:04d}.json") if card_dir.is_dir() else None
            if isinstance(card, dict) and (card.get("summary") or card.get("event_chain") or card.get("title")):
                chapters.append(self._chapter_card_payload(chapter_number, card))
                continue
            # Fall back to source text when a chapter has no fact card yet, so it can still be browsed.
            source = _source()
            if 1 <= chapter_number <= len(source):
                chapter = source[chapter_number - 1]
                chapters.append({
                    "number": chapter_number,
                    "title": str(chapter.get("title") or f"Chapter {chapter_number}"),
                    "summary": str(chapter.get("content") or ""),
                    "source": "raw",
                })
        if not chapters:
            raise ValueError("No chapter fact cards were found for this story arc.")
        return {
            "path": normalized,
            "start_chapter": start,
            "end_chapter": end,
            "chapters": chapters,
        }

    @staticmethod
    def _chapter_card_payload(number: int, card: dict[str, Any]) -> dict[str, Any]:
        rhythm = card.get("chapter_rhythm") if isinstance(card.get("chapter_rhythm"), dict) else {}
        outline = str(card.get("chapter_outline_600") or card.get("summary") or "")
        return {
            "number": number,
            "title": str(card.get("title") or f"Chapter {number}"),
            "summary": outline,
            "chapter_outline_600": outline,
            "chapter_rhythm": {
                "core_content": str(rhythm.get("core_content") or ""),
                "emotion_tone": str(rhythm.get("emotion_tone") or ""),
                "beat_detail": str(rhythm.get("beat_detail") or ""),
            },
            "story_line": str(card.get("story_line") or ""),
            "highlights": card.get("highlights") if isinstance(card.get("highlights"), list) else [],
            "entities": card.get("entities") if isinstance(card.get("entities"), dict) else {},
            "source": "card",
        }

    def _safe_file_path(self, name: str, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("Invalid file path.")
        base = self.workspace_path(name)
        path = (base / relative_path).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValueError("File path is outside the current workspace.") from exc
        return path

    @staticmethod
    def _count_files(path: Path, suffixes: set[str], required_parent: str | None = None) -> int:
        if not path.is_dir():
            return 0
        total = 0
        for item in path.rglob("*"):
            if not item.is_file() or item.suffix.lower() not in suffixes:
                continue
            if required_parent and required_parent not in item.parts:
                continue
            total += 1
        return total

    @staticmethod
    def _load_json(path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _stage_count(path: Path) -> int:
        if not path.is_file():
            return 0
        content = path.read_text(encoding="utf-8", errors="replace")
        indices = {int(value) for value in STAGE_RE.findall(content)}
        return len(indices)


@dataclass
class TaskRecord:
    id: str
    type: str
    label: str
    workspace: str
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    message: str = "Waiting to run"
    log_path: str = ""

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("log_path", None)
        return data


class UploadStore:
    """Store temporary browser uploads; tasks receive files only by upload id."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, Path] = {}
        self._lock = threading.Lock()

    def register(self, path: Path) -> str:
        upload_id = uuid.uuid4().hex
        with self._lock:
            self._items[upload_id] = path.resolve()
        return upload_id

    def resolve(self, upload_id: str) -> Path:
        with self._lock:
            path = self._items.get(upload_id)
        if not path or not path.is_file():
            raise ValueError("The uploaded file is no longer available. Upload it again.")
        return path


class TaskManager:
    """Reuse the CLI in a subprocess, log output, and serialize write tasks per workspace."""

    def __init__(self, store: WorkspaceStore, task_dir: Path, uploads: UploadStore):
        self.store = store
        self.task_dir = task_dir
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.uploads = uploads
        self._tasks: dict[str, TaskRecord] = {}
        self._active_workspaces: set[str] = set()
        self._deleting_workspaces: set[str] = set()
        self._lock = threading.RLock()
        self._load_records()

    def _record_path(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def _persist_record(self, task: TaskRecord) -> None:
        """Persist task metadata; log text stays in a separate .log file."""
        path = self._record_path(task.id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_records(self) -> None:
        """Restore historical tasks and close tasks that did not finish before the service restarted."""
        for path in self.task_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                task_id = str(data.get("id") or "")
                if not re.fullmatch(r"[0-9a-f]{12}", task_id):
                    continue
                record = TaskRecord(
                    id=task_id,
                    type=str(data["type"]),
                    label=str(data["label"]),
                    workspace=require_workspace_name(str(data["workspace"])),
                    status=str(data.get("status") or "failed"),
                    created_at=str(data.get("created_at") or now_iso()),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                    exit_code=data.get("exit_code"),
                    message=str(data.get("message") or ""),
                    # Do not trust arbitrary paths in metadata; logs are always restored from the task directory.
                    log_path=str(self.task_dir / f"{task_id}.log"),
                )
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            if record.status in {"queued", "running"}:
                record.status = "failed"
                record.finished_at = now_iso()
                record.message = "Service restarted; the task was interrupted"
                self._append_log(record, "\nService restarted; unfinished tasks were marked interrupted.\n")
                self._persist_record(record)
            self._tasks[record.id] = record

    def create(self, task_type: str, workspace: str, args: dict[str, Any] | None = None) -> TaskRecord:
        if task_type not in TASK_LABELS:
            raise ValueError("Unsupported workbench task.")
        workspace = require_workspace_name(workspace)
        args = args or {}
        command = self._build_command(task_type, workspace, args)
        with self._lock:
            if workspace in self._deleting_workspaces:
                raise ValueError("This workspace is being deleted, so a new task cannot start.")
            if workspace in self._active_workspaces:
                raise ValueError("This workspace already has a running task. Wait for it to finish.")
            # Make init tasks visible in the UI immediately; the CLI then fills in the standard directories.
            if task_type in {"workspace_init", "init"}:
                self.store.workspace_path(workspace).mkdir(parents=True, exist_ok=True)
            task_id = uuid.uuid4().hex[:12]
            record = TaskRecord(
                id=task_id,
                type=task_type,
                label=TASK_LABELS[task_type],
                workspace=workspace,
                log_path=str(self.task_dir / f"{task_id}.log"),
            )
            self._tasks[task_id] = record
            self._persist_record(record)
            self._active_workspaces.add(workspace)
            thread = threading.Thread(target=self._run, args=(record, command), daemon=True)
            thread.start()
            return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, workspace: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            tasks = list(self._tasks.values())
        if workspace:
            tasks = [task for task in tasks if task.workspace == workspace]
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return [self._public(task) for task in tasks]

    def _public(self, task: TaskRecord) -> dict[str, Any]:
        data = task.public()
        prompt_path = self.task_dir / f"{task.id}.prompts.jsonl"
        if prompt_path.is_file():
            with prompt_path.open("rb") as handle:
                data["prompt_count"] = sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(65536), b""))
        else:
            data["prompt_count"] = 0
        return data

    def logs(self, task_id: str, offset: int = 0) -> dict[str, Any]:
        task = self.get(task_id)
        if not task:
            raise KeyError(task_id)
        offset = max(0, offset)
        path = Path(task.log_path)
        content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return {
            "task": self._public(task),
            "content": content[offset:],
            "next_offset": len(content),
        }

    def prompts(self, task_id: str) -> dict[str, Any]:
        """Return prompts actually sent to the model while the task ran."""
        task = self.get(task_id)
        if not task:
            raise KeyError(task_id)
        path = self.task_dir / f"{task_id}.prompts.jsonl"
        items = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("prompt"):
                    items.append(item)
        return {"task": self._public(task), "items": items[-50:]}

    def delete(self, task_id: str) -> dict[str, Any]:
        """Delete metadata, logs, and prompt records for a finished task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.status in {"queued", "running"}:
                raise ValueError("A running task cannot be deleted.")
            self._tasks.pop(task_id, None)
        removed = []
        for path in (
            self._record_path(task_id),
            Path(task.log_path),
            self.task_dir / f"{task_id}.prompts.jsonl",
        ):
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        return {"deleted": True, "task_id": task_id, "removed": removed}

    def clear_prompts(self, workspace: str | None = None) -> dict[str, Any]:
        """Clear prompts for finished tasks; task metadata and run logs are kept."""
        if workspace:
            workspace = require_workspace_name(workspace)
        with self._lock:
            tasks = [
                task for task in self._tasks.values()
                if (not workspace or task.workspace == workspace)
            ]
        removed = 0
        skipped = 0
        for task in tasks:
            if task.status in {"queued", "running"}:
                skipped += 1
                continue
            path = self.task_dir / f"{task.id}.prompts.jsonl"
            if path.is_file():
                path.unlink()
                removed += 1
        return {
            "cleared": True,
            "workspace": workspace,
            "removed_task_count": removed,
            "skipped_active_count": skipped,
        }

    def delete_workspace_records(self, workspace: str) -> dict[str, Any]:
        """Delete finished task records, logs, and prompts for a workspace."""
        workspace = require_workspace_name(workspace)
        with self._lock:
            tasks = [task for task in self._tasks.values() if task.workspace == workspace]
            if any(task.status in {"queued", "running"} for task in tasks):
                raise ValueError("This workspace still has a running task. Stop it before deleting.")
            task_ids = [task.id for task in tasks]
        for task_id in task_ids:
            self.delete(task_id)
        return {"workspace": workspace, "removed_task_count": len(task_ids)}

    def begin_workspace_delete(self, workspace: str) -> None:
        """Atomically block new CLI tasks from entering a workspace that is being deleted."""
        workspace = require_workspace_name(workspace)
        with self._lock:
            if workspace in self._active_workspaces or any(
                task.workspace == workspace and task.status in {"queued", "running"}
                for task in self._tasks.values()
            ):
                raise ValueError("This workspace still has a background task. Wait for it or stop it before deleting.")
            self._deleting_workspaces.add(workspace)

    def end_workspace_delete(self, workspace: str) -> None:
        with self._lock:
            self._deleting_workspaces.discard(workspace)

    def _run(self, task: TaskRecord, command: list[str]) -> None:
        with self._lock:
            task.status = "running"
            task.started_at = now_iso()
            task.message = "Running"
            self._persist_record(task)
        self._append_log(task, f"Start: {task.label}\n")
        reported_warning = False

        env = os.environ.copy()
        env["HARNESS_NOVEL_HOME"] = str(self.store.root)
        env["PYTHONUNBUFFERED"] = "1"
        env["HARNESS_NOVEL_PROMPT_TRACE_FILE"] = str(
            self.task_dir / f"{task.id}.prompts.jsonl"
        )
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=os.getcwd(),
                bufsize=1,
            )
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                if (
                    line.lstrip().startswith(("Error:", "错误：", "Traceback"))
                    or "[LLMProvider] Call failed" in line
                    or "[LLMProvider] 调用失败" in line
                    or "[LLMProvider] api_key is not configured" in line
                    or "[LLMProvider] 未配置 api_key" in line
                ):
                    reported_warning = True
                self._append_log(task, line)
            process.stdout.close()
            exit_code = process.wait()
            with self._lock:
                task.exit_code = exit_code
                if exit_code != 0:
                    task.status = "failed"
                    task.message = f"Run failed (exit code {exit_code})"
                elif reported_warning:
                    task.status = "succeeded_with_warnings"
                    task.message = "Command finished; check the log for notes"
                else:
                    task.status = "succeeded"
                    task.message = "Run completed"
        except Exception as exc:  # noqa: BLE001 - logs must record background run exceptions
            self._append_log(task, f"\nWorkbench failed to start the task: {exc}\n")
            with self._lock:
                task.status = "failed"
                task.message = "The workbench could not start this task"
        finally:
            with self._lock:
                task.finished_at = now_iso()
                self._active_workspaces.discard(task.workspace)
                self._persist_record(task)

    @staticmethod
    def _append_log(task: TaskRecord, content: str) -> None:
        path = Path(task.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)

    def _build_command(self, task_type: str, workspace: str, args: dict[str, Any]) -> list[str]:
        command = [sys.executable, "-m", "novel_cli"]
        force = bool(args.get("force"))

        if task_type == "workspace_init":
            return [*command, "init", workspace]

        if task_type == "init":
            reference = self.uploads.resolve(str(args.get("reference_upload_id", "")))
            if reference.suffix.lower() != ".txt":
                raise ValueError("Reference novels currently support .txt files only.")
            command += ["init", workspace, "--txt", str(reference)]
            if args.get("defer_reference_analysis"):
                command.append("--no-analyze")
            batch_size = _positive_int(args.get("batch_size"), "chapter batch size", 20)
            if batch_size != 20:
                command += ["--batch-size", str(batch_size)]
            max_chapters = _positive_int(args.get("max_chapters"), "deconstruction chapter count", None)
            if max_chapters:
                command += ["--max-chapters", str(max_chapters)]
            return command

        if task_type == "reference_resume":
            command += ["reference-resume", workspace]
            upload_id = str(args.get("reference_upload_id") or "")
            if upload_id:
                reference = self.uploads.resolve(upload_id)
                if reference.suffix.lower() != ".txt":
                    raise ValueError("Later novel chapters currently support .txt files only.")
                command += ["--txt", str(reference)]
            batch_size = _positive_int(args.get("batch_size"), "chapter batch size", 20)
            if batch_size != 20:
                command += ["--batch-size", str(batch_size)]
            max_chapters = _positive_int(args.get("max_chapters"), "deconstruction chapter count", None)
            if max_chapters:
                command += ["--max-chapters", str(max_chapters)]
            if args.get("rebuild_reference"):
                command.append("--rebuild-reference")
            return command

        if task_type == "world_import":
            ids = args.get("upload_ids") or []
            if not isinstance(ids, list) or not ids:
                raise ValueError("Upload at least one target-genre source.")
            paths = [str(self.uploads.resolve(str(upload_id))) for upload_id in ids]
            command += ["world-import", workspace, *paths, "--build"]
            if force:
                command.append("--force")
            return command

        if task_type == "world_build":
            command += ["world-build", workspace]
            if force:
                command.append("--force")
            if args.get("merge_only"):
                command.append("--merge-only")
            primary = str(args.get("primary") or "").strip()
            if primary:
                command += ["--primary", primary]
            chunk_size = _positive_int(args.get("chunk_size"), "source chunk size", None)
            if chunk_size:
                command += ["--chunk-size", str(chunk_size)]
            chapter_batch_size = _positive_int(args.get("chapter_batch_size"), "chapters per source batch", None)
            if chapter_batch_size:
                command += ["--chapter-batch-size", str(chapter_batch_size)]
            return command

        if task_type in {"novel_outline", "story_design", "story_design_extend", "design_concept", "stage_design"}:
            command += [task_type.replace("_", "-"), workspace]
            if force and task_type != "story_design_extend":
                command.append("--force")
            if task_type == "story_design_extend" and args.get("use_reference"):
                command.append("--use-reference")
            direction_upload = str(args.get("direction_upload_id") or "")
            direction = str(args.get("direction") or "").strip()
            if direction_upload:
                command += ["--direction-file", str(self.uploads.resolve(direction_upload))]
            elif direction:
                command += ["--direction", direction]
            return command

        if task_type == "stage_insert":
            direction_upload = str(args.get("direction_upload_id") or "")
            direction = str(args.get("direction") or "").strip()
            if not direction_upload and not direction:
                raise ValueError("Enter inspiration for the new stage.")
            command += ["stage-insert", workspace]
            if direction_upload:
                command += ["--direction-file", str(self.uploads.resolve(direction_upload))]
            else:
                command += ["--direction", direction]
            after_stage = _positive_int(args.get("after_stage"), "insert position", None)
            before_stage = _positive_int(args.get("before_stage"), "insert position", None)
            if after_stage and before_stage:
                raise ValueError("Choose insertion before or after a stage, not both.")
            if after_stage:
                command += ["--after-stage", str(after_stage)]
            if before_stage:
                command += ["--before-stage", str(before_stage)]
            return command

        if task_type == "mechanics_init":
            command += ["mechanics-init", workspace]
            if force:
                command.append("--force")
            if args.get("disable"):
                command.append("--none")
                return command
            mechanics_upload = str(args.get("mechanics_upload_id") or "")
            if mechanics_upload:
                command += ["--file", str(self.uploads.resolve(mechanics_upload))]
            else:
                direction = str(args.get("direction") or "").strip()
                if direction:
                    command += ["--direction", direction]
            return command

        if task_type in {"story_arcs", "chapter_outlines", "volume_outline"}:
            command += [task_type.replace("_", "-"), workspace]
            volume = _positive_int(args.get("volume"), "stage/volume number", 1)
            command += ["--volume", str(volume)]
            if force:
                command.append("--force")
            return command

        if task_type == "write":
            command += ["write", workspace]
            volume = _positive_int(args.get("volume"), "stage/volume number", 1)
            start = _positive_int(args.get("start"), "start chapter", 1)
            max_chapters = _positive_int(args.get("max"), "chapter count to generate", None)
            command += ["--volume", str(volume), "--start", str(start)]
            if max_chapters:
                command += ["--max", str(max_chapters)]
            if args.get("no_humanize"):
                command.append("--no-humanize")
            if args.get("humanize_existing"):
                command.append("--humanize-existing")
            return command

        if task_type == "novel_name_synopsis":
            command += ["novel-name-synopsis", workspace]
            if force:
                command.append("--force")
            return command

        raise ValueError("Unsupported workbench task.")
