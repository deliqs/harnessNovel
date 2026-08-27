"""Resumable three-stage reference-novel deconstruction: chapter cards -> story segments -> book structure."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import ConfigLoader
from core.llm_provider import LLMProvider
from core.prompt_loader import PromptLoader
from core.text_utils import normalize_text, parse_json_response
from training.reference_craft import (
    compact_cards_for_craft,
    load_reference_craft_bible,
    normalize_reference_craft_bible,
    render_reference_craft_bible,
)


ARC_FILE_RE = re.compile(r"^arc_(\d+)_ch(\d+)_(\d+)\.md$")
PIPELINE_VERSION = 2


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chapter_digest(content: str) -> str:
    """Stable chapter-level fingerprint; ignore whitespace from download sites, keep character differences in the prose."""
    canonical = re.sub(r"\s+", "", normalize_text(content or ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ranges(numbers: list[int]) -> list[dict[str, int]]:
    ordered = sorted({number for number in numbers if number > 0})
    if not ordered:
        return []
    result: list[dict[str, int]] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        result.append({"start": start, "end": previous})
        start = previous = number
    result.append({"start": start, "end": previous})
    return result


def _compact_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "").strip()
    return text[:limit]


class ReferenceAnalyzer:
    """Use independently saved single-chapter fact cards as the factual base for story-segment extract."""

    def __init__(
        self,
        txt_path: str | Path,
        output_dir: str | Path,
        *,
        max_chapters: int | None = None,
        card_batch_size: int = 20,
        max_workers: int = 6,
        segment_load_size: int = 8,
        max_chapters_per_segment: int = 12,
        llm: Any | None = None,
        rebuild: bool = False,
    ) -> None:
        self.txt_path = Path(txt_path)
        self.output_dir = Path(output_dir)
        self.max_chapters = max_chapters
        self.card_batch_size = max(1, int(card_batch_size))
        self.max_workers = max(1, int(max_workers))
        self.segment_load_size = max(1, int(segment_load_size))
        self.max_chapters_per_segment = max(2, int(max_chapters_per_segment))
        # This is an input-safety bound, not a forced segment boundary. Natural units may exceed the
        # historical 12-chapter preference; unresolved material is quarantined rather than fake-closed.
        self.max_segment_analysis_window = max(64, self.segment_load_size * 4)
        self.llm = llm
        self.rebuild = rebuild

        self.cards_dir = self.output_dir / "chapter_cards"
        self.cards_index_path = self.output_dir / "chapter_cards_index.json"
        self.state_path = self.output_dir / "analysis_state.json"
        self.outlines_dir = self.output_dir / "outlines"
        self.craft_bible_path = self.outlines_dir / "reference_craft_bible.json"
        self.craft_bible_markdown_path = self.outlines_dir / "reference_craft_bible.md"
        self.state: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        if not self.txt_path.is_file():
            raise FileNotFoundError(f"Reference novel not found: {self.txt_path}")

        volumes, chapters = self._load_chapters()
        total_chapters = len(chapters)
        if not total_chapters:
            raise RuntimeError("No valid chapters were identified; cannot deconstruct the reference novel.")
        target = min(self.max_chapters or total_chapters, total_chapters)
        if target < 1:
            raise ValueError("The deconstruction chapter count must be a positive integer.")

        source_digest = _file_digest(self.txt_path)
        self._prepare_state(source_digest, total_chapters)
        previous_target = int(self.state.get("target_chapters") or 0)
        if self.state.get("resegmented") and not self.rebuild:
            completed_target = previous_target
            if target <= completed_target:
                print("  Reference deconstruction and smart volume split already done; reusing existing split.")
                stored_cards = self._read_stored_cards(completed_target)
                segment_status = self._reconcile_resegmented_segments(completed_target)
                craft_path = self._ensure_craft_bible(stored_cards) if stored_cards else None
                return {
                    "target_chapters": completed_target,
                    "total_chapters": total_chapters,
                    "chapter_card_count": len(stored_cards),
                    "segmented_chapter_count": segment_status["segmented_chapter_count"],
                    "pending_chapter_count": segment_status["pending_chapter_count"],
                    "structure_updated": False,
                    "is_complete": (
                        completed_target >= total_chapters
                        and len(stored_cards) >= completed_target
                        and segment_status["pending_chapter_count"] == 0
                    ),
                    "craft_bible_path": craft_path,
                }
            self._restore_resegmented_working_volume()
        self.previous_target = previous_target
        volume_specs = self._build_volume_specs(volumes, chapters, target)

        print(">>> Starting three-stage reference-novel deconstruction <<<")
        print(f"  Single-chapter fact cards: target chapters 1-{target}/{total_chapters}, concurrency {self.max_workers}")
        cards = self._extract_missing_cards(volume_specs, source_digest)
        self._write_card_index(cards, target, total_chapters)

        print("\n--- Reference craft bible: transferable techniques with source evidence ---")
        craft_path = self._ensure_craft_bible(cards)

        print("\n--- Stage two: rolling story-segment extraction from fact cards ---")
        segment_stats = self._extract_story_segments(volume_specs, cards)

        print("\n--- Stage three: structure from closed segments ---")
        structure_stats = self._build_structures(volume_specs, target, total_chapters)

        self.state["target_chapters"] = target
        self.state["total_chapters"] = total_chapters
        self.state["source_digest"] = source_digest
        self.state["chapter_cards"] = {
            "complete_count": len(cards),
            "completed_ranges": _ranges([int(card["chapter"]) for card in cards]),
        }
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(self.state_path, self.state)

        return {
            "target_chapters": target,
            "total_chapters": total_chapters,
            "chapter_card_count": len(cards),
            "segmented_chapter_count": segment_stats["segmented_chapter_count"],
            "pending_chapter_count": segment_stats["pending_chapter_count"],
            "structure_updated": structure_stats["updated"],
            "is_complete": target == total_chapters and segment_stats["pending_chapter_count"] == 0,
            "craft_bible_path": craft_path,
        }

    def _load_chapters(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Delayed import so outline_builder calling this module does not create an init cycle.
        from training.outline_builder import split_chapters

        return split_chapters(str(self.txt_path))

    def _prepare_state(self, source_digest: str, total_chapters: int) -> None:
        state = _read_json(self.state_path, {})
        has_legacy = self._has_legacy_outline_assets()
        if self.rebuild and (state or has_legacy):
            self._clear_derived_assets()
            state = {}
            has_legacy = False
        if state and state.get("pipeline_version") != PIPELINE_VERSION:
            if not self.rebuild:
                raise RuntimeError("Detected a legacy reference-deconstruction state. To avoid overwriting existing artifacts, use --rebuild-reference.")
            self._clear_derived_assets()
            state = {}
        if not state and has_legacy:
            if not self.rebuild:
                raise RuntimeError("Detected legacy story segments. The three-stage deconstruction will not silently overwrite them; use --rebuild-reference.")
            self._clear_derived_assets()
        if state and state.get("source_digest") and state.get("source_digest") != source_digest:
            if not self.rebuild:
                raise RuntimeError("The reference novel source file has changed. Use --rebuild-reference to rebuild chapter cards and story segments.")
            self._clear_derived_assets()
            state = {}

        if not state:
            state = {
                "pipeline_version": PIPELINE_VERSION,
                "source_digest": source_digest,
                "total_chapters": total_chapters,
                "target_chapters": 0,
                "chapter_cards": {},
                "volumes": {},
                "structure": {},
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        self.state = state
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.outlines_dir.mkdir(parents=True, exist_ok=True)

    def _restore_resegmented_working_volume(self) -> None:
        """After an author update, restore smart-split artifacts into a whole-book working volume that can keep rolling deconstruction.

        Smart split only changes file ownership; chapter ranges in story-segment files stay whole-book numbers, so they can
        be merged back into one working volume losslessly; after new segments finish, the outer flow rechecks and smart-splits again.
        """
        arc_items: dict[tuple[int, int], str] = {}
        for path in self.outlines_dir.glob("vol_*/story_arcs/arc_*_ch*_*.md"):
            match = ARC_FILE_RE.match(path.name)
            if not match:
                continue
            key = (int(match.group(2)), int(match.group(3)))
            arc_items[key] = _read_text(path)
        if not arc_items:
            self.state["resegmented"] = False
            self.state["volumes"] = {}
            return

        backup = self.outlines_dir / f".before_incremental_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        for directory in sorted(self.outlines_dir.glob("vol_*")):
            if directory.is_dir():
                shutil.move(str(directory), str(backup / directory.name))

        working = self.outlines_dir / "vol_01_全书" / "story_arcs"
        working.mkdir(parents=True, exist_ok=True)
        for index, ((start, end), content) in enumerate(sorted(arc_items.items()), start=1):
            _write_text(working / f"arc_{index:03d}_ch{start:03d}_{end:03d}.md", content)
        self.state["resegmented"] = False
        self.state["volumes"] = {}
        self.state.setdefault("incremental_updates", []).append({
            "restored_at": datetime.now().isoformat(timespec="seconds"),
            "previous_target": int(self.state.get("target_chapters") or 0),
            "arc_count": len(arc_items),
        })
        _write_json(self.state_path, self.state)
        print(f"  Restored {len(arc_items)} existing story segments; will recheck the tail boundary with newly added chapters.")

    def _has_legacy_outline_assets(self) -> bool:
        if not self.outlines_dir.is_dir():
            return False
        return any(self.outlines_dir.glob("vol_*/story_arcs/arc_*.md"))

    def _clear_derived_assets(self) -> None:
        shutil.rmtree(self.cards_dir, ignore_errors=True)
        shutil.rmtree(self.outlines_dir, ignore_errors=True)
        self.cards_index_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def _read_stored_cards(self, target: int) -> list[dict[str, Any]]:
        cards = []
        for chapter in range(1, target + 1):
            card = _read_json(self._card_path(chapter), {})
            if isinstance(card, dict) and card.get("chapter") is not None:
                item = {
                    "chapter": chapter,
                    "volume_index": int(card.get("volume_index") or 1),
                    "volume_title": card.get("volume_title") or "",
                    "volume_chapter": int(card.get("volume_chapter") or chapter),
                    "title": card.get("title") or f"Chapter {chapter}",
                    "content": "",
                    "content_digest": card.get("content_digest") or "",
                }
                normalized = self._normalize_card(
                    card,
                    item,
                    str(card.get("source_digest") or self.state.get("source_digest") or ""),
                )
                if not card.get("content_digest"):
                    normalized["content_digest"] = ""
                _write_json(self._card_path(chapter), normalized)
                cards.append(normalized)
        return cards

    def _reconcile_resegmented_segments(self, target: int) -> dict[str, int]:
        """Migrate and recount smart-split global arc ranges before a no-work reuse return."""
        covered: set[int] = set()
        quarantined_count = 0
        for arc_dir in sorted(self.outlines_dir.glob("vol_*/story_arcs")):
            if not arc_dir.is_dir():
                continue
            quarantined_count += self._quarantine_placeholder_arcs(arc_dir)
            items = self._load_arc_items(arc_dir)
            self._write_arc_index(arc_dir, items)
            for item in items:
                start = max(1, int(item["start_chapter"]))
                end = min(target, int(item["end_chapter"]))
                if end >= start:
                    covered.update(range(start, end + 1))

        segmented_count = len(covered)
        pending_count = max(0, target - segmented_count)
        self.state["reuse_reconciliation"] = {
            "segmented_ranges": _ranges(list(covered)),
            "segmented_chapter_count": segmented_count,
            "pending_chapter_count": pending_count,
            "quarantined_segment_count": quarantined_count,
            "analysis_status": "complete" if pending_count == 0 else "incomplete",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(self.state_path, self.state)
        return {
            "segmented_chapter_count": segmented_count,
            "pending_chapter_count": pending_count,
        }

    def _build_volume_specs(
        self,
        volumes: list[dict[str, Any]],
        chapters: list[dict[str, Any]],
        target: int,
    ) -> list[dict[str, Any]]:
        from training.outline_builder import group_chapters_by_volume, _vol_dir_name

        groups = group_chapters_by_volume(chapters, volumes)
        specs = []
        global_chapter = 0
        for index, group in enumerate(groups, start=1):
            all_items = list(group["chapters"])
            start_global = global_chapter + 1
            global_chapter += len(all_items)
            target_count = max(0, min(len(all_items), target - start_global + 1))
            title = str(group["title"])
            directory_name = _vol_dir_name(index - 1, title)
            specs.append({
                "index": index,
                "title": title,
                "directory_name": directory_name,
                "directory": self.outlines_dir / directory_name,
                "global_start": start_global,
                "total_count": len(all_items),
                "target_count": target_count,
                "chapters": all_items[:target_count],
            })
        return specs

    def _card_path(self, chapter: int) -> Path:
        return self.cards_dir / f"chapter_{chapter:04d}.json"

    def _load_card(self, chapter: int, source_digest: str, content_digest: str) -> dict[str, Any] | None:
        card = _read_json(self._card_path(chapter), {})
        if not isinstance(card, dict):
            return None
        saved_chapter_digest = str(card.get("content_digest") or "")
        if saved_chapter_digest:
            if saved_chapter_digest != content_digest:
                return None
        elif card.get("source_digest") != source_digest:
            return None
        return card

    def _extract_missing_cards(self, specs: list[dict[str, Any]], source_digest: str) -> list[dict[str, Any]]:
        planned: list[dict[str, Any]] = []
        existing: dict[int, dict[str, Any]] = {}
        for spec in specs:
            for local, chapter_data in enumerate(spec["chapters"], start=1):
                global_chapter = spec["global_start"] + local - 1
                content_digest = _chapter_digest(chapter_data.get("content", ""))
                item = {
                    "chapter": global_chapter,
                    "volume_index": spec["index"],
                    "volume_title": spec["title"],
                    "volume_chapter": local,
                    "title": chapter_data.get("title", f"Chapter {global_chapter}"),
                    "content": chapter_data.get("content", ""),
                    "content_digest": content_digest,
                }
                card = self._load_card(global_chapter, source_digest, content_digest)
                if card:
                    # Normalize old cached cards into the additive schema while preserving their factual content.
                    card = self._normalize_card(card, item, source_digest)
                    _write_json(self._card_path(global_chapter), card)
                    existing[global_chapter] = card
                    continue
                planned.append(item)

        if planned:
            print(f"  Chapters still to extract: {len(planned)}; reused: {len(existing)}")
        errors: list[str] = []
        for start in range(0, len(planned), self.card_batch_size):
            batch = planned[start : start + self.card_batch_size]
            print(f"  Extracting single-chapter fact cards in parallel: chapters {batch[0]['chapter']}-{batch[-1]['chapter']} ({len(batch)} chapters)...")
            workers = min(self.max_workers, len(batch))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._extract_one_card, item, source_digest): item for item in batch}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        card = future.result()
                    except Exception as exc:  # noqa: BLE001 - keep already-successful checkpoint assets.
                        errors.append(f"Chapter {item['chapter']}: {exc}")
                        continue
                    existing[int(card["chapter"])] = card
                    print(f"    -> Chapter {item['chapter']} fact card saved")
            self._update_card_state(existing)

        cards = [existing[number] for number in sorted(existing)]
        if errors:
            raise RuntimeError("Some single-chapter fact cards failed; you can retry:\n" + "\n".join(errors[:8]))
        return cards

    def _extract_one_card(self, item: dict[str, Any], source_digest: str) -> dict[str, Any]:
        prompt = PromptLoader.load(
            "reference_chapter_card",
            chapter=item["chapter"],
            volume_chapter=item["volume_chapter"],
            title=item["title"],
            chapter_text=item["content"],
        )
        payload = self._generate_json(prompt, f"chapter {item['chapter']} fact card")
        card = self._normalize_card(payload, item, source_digest)
        _write_json(self._card_path(int(card["chapter"])), card)
        return card

    def _generate_json(self, prompt: str, label: str) -> dict[str, Any]:
        if not self.llm:
            raise RuntimeError("No usable model is configured.")
        last_error: Exception | None = None
        current_prompt = prompt
        for attempt in range(3):
            raw = self.llm.generate(current_prompt, temperature=0.2, is_json=True)
            try:
                payload = parse_json_response(raw or "")
                if isinstance(payload, dict):
                    return payload
                raise ValueError("The model did not return a JSON object")
            except Exception as exc:  # noqa: BLE001 - retry with format tolerance for model output.
                last_error = exc
                if attempt < 2:
                    current_prompt = (
                        prompt
                        + "\n\n[Previous output could not be parsed]\n"
                        + f"Error: {exc}\n"
                        + "Return a valid JSON object only. Do not include Markdown or explanation."
                    )
        raise RuntimeError(f"{label} JSON parse failed: {last_error}")

    def _normalize_card(self, payload: dict[str, Any], item: dict[str, Any], source_digest: str) -> dict[str, Any]:
        rhythm = payload.get("chapter_rhythm") or {}
        if not isinstance(rhythm, dict):
            rhythm = {"core_content": str(rhythm)}
        outline = str(payload.get("chapter_outline_600") or payload.get("summary") or "").strip()
        entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
        perspective = payload.get("pov_tense") or payload.get("narrative_perspective") or {}
        if not isinstance(perspective, dict):
            perspective = {"pov": perspective}
        source_location = payload.get("source_location") if isinstance(payload.get("source_location"), dict) else {}
        return {
            "chapter": item["chapter"],
            "volume_index": item["volume_index"],
            "volume_title": item["volume_title"],
            "volume_chapter": item["volume_chapter"],
            "title": _compact_text(payload.get("title") or item["title"], 160),
            "chapter_outline_600": _compact_text(outline, 2000),
            "chapter_rhythm": {
                "core_content": _compact_text(rhythm.get("core_content") or rhythm.get("core") or "", 400),
                "emotion_tone": _compact_text(rhythm.get("emotion_tone", ""), 400),
                "beat_detail": _compact_text(rhythm.get("beat_detail") or rhythm.get("detail") or "", 500),
            },
            "story_line": _compact_text(payload.get("story_line", ""), 500),
            "highlights": self._string_list(payload.get("highlights") or []),
            "entities": {
                key: self._string_list(entities.get(key) or [])
                for key in sorted(set(entities) | {"characters", "locations", "factions", "abilities", "objects"})
            },
            "pov_tense": {
                "pov": _compact_text(perspective.get("pov") or perspective.get("point_of_view"), 100),
                "tense": _compact_text(perspective.get("tense"), 100),
                "evidence": self._normalize_evidence(perspective.get("evidence") or []),
            },
            "scene_observations": self._normalize_scene_observations(
                payload.get("scene_observations") or payload.get("scenes") or [],
            ),
            "craft_observations": self._normalize_craft_observations(
                payload.get("craft_observations") or payload.get("craft") or [],
            ),
            "evidence": self._normalize_evidence(payload.get("evidence") or []),
            "source_location": {
                "chapter": item["chapter"],
                "volume_index": item["volume_index"],
                "volume_chapter": item["volume_chapter"],
                "title": _compact_text(source_location.get("title") or item["title"], 160),
                "analyzed_scope": _compact_text(source_location.get("analyzed_scope") or "full supplied chapter", 100),
            },
            "confidence": self._normalize_confidence(payload.get("confidence")),
            "uncertainty": _compact_text(payload.get("uncertainty"), 400),
            "source_digest": source_digest,
            "content_digest": item.get("content_digest") or _chapter_digest(item.get("content", "")),
        }

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [_compact_text(item, 160) for item in value if str(item).strip()]
        if value:
            return [_compact_text(value, 160)]
        return []

    @staticmethod
    def _normalize_confidence(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            raw_score = value.get("score", value.get("confidence"))
            reason = _compact_text(value.get("reason") or value.get("basis"), 240)
        else:
            raw_score = value
            reason = ""
        if isinstance(raw_score, str):
            score = {"high": 0.85, "medium": 0.6, "low": 0.35, "uncertain": 0.2}.get(
                raw_score.strip().lower(), 0.5,
            )
        else:
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                score = 0.0
                if not reason:
                    reason = "Confidence was not recorded."
        score = max(0.0, min(1.0, score))
        return {
            "level": "high" if score >= 0.75 else "medium" if score >= 0.45 else "low",
            "score": round(score, 2),
            "reason": reason,
        }

    @staticmethod
    def _normalize_evidence(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        evidence = []
        for item in value[:12]:
            if not isinstance(item, dict):
                continue
            signal = _compact_text(item.get("observed_signal") or item.get("evidence") or item.get("signal"), 260)
            if not signal:
                continue
            evidence.append({
                "claim": _compact_text(item.get("claim"), 180),
                "source_span": _compact_text(item.get("source_span") or "whole chapter", 100),
                "observed_signal": signal,
            })
        return evidence

    def _normalize_scene_observations(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        scenes = []
        for item in value[:12]:
            if not isinstance(item, dict):
                continue
            scenes.append({
                "source_span": _compact_text(item.get("source_span") or "whole chapter", 100),
                "setting": _compact_text(item.get("setting"), 160),
                "participants": self._string_list(item.get("participants") or []),
                "scene_function": _compact_text(item.get("scene_function") or item.get("function"), 300),
                "entry_exit": _compact_text(item.get("entry_exit"), 260),
                "rhythm": _compact_text(item.get("rhythm"), 260),
                "evidence": _compact_text(item.get("evidence") or item.get("observed_signal"), 260),
            })
        return scenes

    def _normalize_craft_observations(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        observations = []
        for item in value[:12]:
            if not isinstance(item, dict):
                continue
            technique = _compact_text(item.get("technique") or item.get("observation"), 240)
            evidence = _compact_text(item.get("evidence") or item.get("observed_signal"), 260)
            if not technique or not evidence:
                continue
            observations.append({
                "technique": technique,
                "effect": _compact_text(item.get("effect"), 260),
                "source_span": _compact_text(item.get("source_span") or "whole chapter", 100),
                "evidence": evidence,
                "confidence": self._normalize_confidence(item.get("confidence")),
                "uncertainty": _compact_text(item.get("uncertainty"), 240),
            })
        return observations

    def _update_card_state(self, cards: dict[int, dict[str, Any]]) -> None:
        self.state["chapter_cards"] = {
            "complete_count": len(cards),
            "completed_ranges": _ranges(list(cards)),
        }
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(self.state_path, self.state)

    def _write_card_index(self, cards: list[dict[str, Any]], target: int, total: int) -> None:
        payload = {
            "target_chapters": target,
            "total_chapters": total,
            "card_count": len(cards),
            "cards": [
                {
                    "chapter": card["chapter"],
                    "volume_index": card["volume_index"],
                    "volume_chapter": card["volume_chapter"],
                    "title": card["title"],
                    "path": str(self._card_path(int(card["chapter"])).relative_to(self.output_dir)),
                }
                for card in cards
            ],
        }
        _write_json(self.cards_index_path, payload)

    def _ensure_craft_bible(self, cards: list[dict[str, Any]]) -> str:
        fingerprint_source = "|".join(
            f"{card.get('chapter')}:{card.get('content_digest') or json.dumps(card, ensure_ascii=False, sort_keys=True)}"
            for card in cards
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        existing = load_reference_craft_bible(self.outlines_dir)
        if existing and existing.get("source_fingerprint") == fingerprint and existing.get("techniques"):
            if not self.craft_bible_markdown_path.is_file():
                _write_text(self.craft_bible_markdown_path, render_reference_craft_bible(existing))
            print("  Reusing the existing reference craft bible.")
            return str(self.craft_bible_path.relative_to(self.output_dir))

        chapter_cards_json = compact_cards_for_craft(cards)
        supplied_cards = json.loads(chapter_cards_json)
        supplied_chapters = {
            int(card["chapter"])
            for card in supplied_cards
            if isinstance(card, dict) and card.get("chapter") is not None
        }
        if not supplied_chapters:
            raise ValueError("No bounded chapter-card evidence was available for the reference craft bible.")
        prompt = PromptLoader.load(
            "reference_craft_bible",
            card_count=len(supplied_chapters),
            first_chapter=min(supplied_chapters),
            last_chapter=max(supplied_chapters),
            chapter_cards_json=chapter_cards_json,
        )
        payload = self._generate_json(prompt, "reference craft bible")
        bible = normalize_reference_craft_bible(payload, cards, fingerprint, supplied_chapters)
        _write_json(self.craft_bible_path, bible)
        _write_text(self.craft_bible_markdown_path, render_reference_craft_bible(bible))
        craft_state = self.state.setdefault("craft_bible", {})
        craft_state.update({
            "source_fingerprint": fingerprint,
            "technique_count": len(bible["techniques"]),
            "path": str(self.craft_bible_path.relative_to(self.output_dir)),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(self.state_path, self.state)
        print(f"  Reference craft bible saved: {self.craft_bible_markdown_path}")
        return str(self.craft_bible_path.relative_to(self.output_dir))

    def _extract_story_segments(self, specs: list[dict[str, Any]], cards: list[dict[str, Any]]) -> dict[str, int]:
        cards_by_chapter = {int(card["chapter"]): card for card in cards}
        segmented_global: list[int] = []
        pending_global: list[int] = []
        for spec in specs:
            if not spec["target_count"]:
                continue
            volume_cards = []
            for local in range(1, spec["target_count"] + 1):
                global_chapter = spec["global_start"] + local - 1
                card = cards_by_chapter.get(global_chapter)
                if card:
                    copied = dict(card)
                    copied["chapter"] = local
                    copied["global_chapter"] = global_chapter
                    volume_cards.append(copied)
            if len(volume_cards) != spec["target_count"]:
                pending_global.extend(spec["global_start"] + local - 1 for local in range(1, spec["target_count"] + 1))
                continue
            result = self._extract_volume_segments(spec, volume_cards)
            segmented_global.extend(result["segmented_global"])
            pending_global.extend(result["pending_global"])
        return {
            "segmented_chapter_count": len(set(segmented_global)),
            "pending_chapter_count": len(set(pending_global)),
        }

    def _extract_volume_segments(self, spec: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, list[int]]:
        volume_dir: Path = spec["directory"]
        arc_dir = volume_dir / "story_arcs"
        arc_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_placeholder_arcs(arc_dir)
        existing = self._load_arc_items(arc_dir)
        self._reconsider_previous_tail(spec, cards, arc_dir, existing)
        existing = self._load_arc_items(arc_dir)
        closed_through = self._contiguous_end(existing)
        remaining = [card for card in cards if int(card["chapter"]) > closed_through]
        next_index = max((item["index"] for item in existing), default=0) + 1
        carryover: list[dict[str, Any]] = []
        cursor = 0
        force_final = spec["target_count"] == spec["total_count"]
        pending_reason = ""

        while cursor < len(remaining) or (carryover and force_final):
            if len(carryover) >= self.max_segment_analysis_window:
                pending_reason = (
                    f"No evidence-supported natural boundary was found within the bounded "
                    f"{self.max_segment_analysis_window}-chapter analysis window."
                )
                break
            new_cards = remaining[cursor : cursor + self.segment_load_size]
            room = self.max_segment_analysis_window - len(carryover)
            new_cards = new_cards[:room]
            cursor += len(new_cards)
            if not new_cards and not carryover:
                break
            window = carryover + new_cards
            if not window:
                break
            is_final_window = force_final and cursor >= len(remaining)
            print(
                f"  Volume {spec['index']} rolling segments: chapters {window[0]['chapter']}-{window[-1]['chapter']}"
                f" (new {len(new_cards)}, carryover {len(carryover)})..."
            )
            prompt = PromptLoader.load(
                "reference_segment_extract",
                window_start=window[0]["chapter"],
                window_end=window[-1]["chapter"],
                max_chapters=self.max_chapters_per_segment,
                is_final_window="yes" if is_final_window else "no",
                previous_tail_context="(none; this round uses a normal rolling window.)",
                chapter_cards_json=self._segment_cards_json(window),
            )
            payload = self._generate_json(prompt, f"volume {spec['index']} story segments")
            segments = self._normalize_segments(payload, window)
            if not segments:
                carryover = window
                carryover_reason = _compact_text(payload.get("carryover_reason"), 500)
                if is_final_window:
                    pending_reason = carryover_reason or "The available range ends before an evidence-supported natural boundary."
                    break
                if len(window) >= self.max_segment_analysis_window:
                    pending_reason = carryover_reason or (
                        f"No evidence-supported natural boundary was found within the bounded "
                        f"{self.max_segment_analysis_window}-chapter analysis window."
                    )
                    break
                continue

            consumed = int(segments[-1]["end_chapter"])
            for segment in segments:
                segment["segment_id"] = next_index
                _write_text(arc_dir / f"arc_{next_index:03d}_ch{segment['start_chapter']:03d}_{segment['end_chapter']:03d}.md", self._render_segment(segment))
                next_index += 1
            carryover = [card for card in window if int(card["chapter"]) > consumed]
            existing = self._load_arc_items(arc_dir)
            closed_through = self._contiguous_end(existing)
            self._write_arc_index(arc_dir, existing)
            self._update_volume_state(spec, closed_through, len(cards))
            if not carryover and cursor >= len(remaining):
                break

        existing = self._load_arc_items(arc_dir)
        closed_through = self._contiguous_end(existing)
        pending_cards = [card for card in cards if int(card["chapter"]) > closed_through]
        if pending_cards and not pending_reason:
            pending_reason = "No evidence-supported natural boundary has closed yet."
        self._write_pending_segment(arc_dir, pending_cards, pending_reason)
        self._write_arc_index(arc_dir, existing)
        self._update_volume_state(spec, closed_through, len(cards), pending_reason)
        segmented_global = [spec["global_start"] + local - 1 for local in range(1, min(closed_through, len(cards)) + 1)]
        pending_global = [spec["global_start"] + local - 1 for local in range(closed_through + 1, len(cards) + 1)]
        return {"segmented_global": segmented_global, "pending_global": pending_global}

    def _reconsider_previous_tail(
        self,
        spec: dict[str, Any],
        cards: list[dict[str, Any]],
        arc_dir: Path,
        existing: list[dict[str, Any]],
    ) -> None:
        """Re-judge a natural boundary from the old tail segment plus the first 10 newly added chapters."""
        if not existing or not getattr(self, "previous_target", 0):
            return
        previous_local_end = self.previous_target - int(spec["global_start"]) + 1
        if previous_local_end < 1 or previous_local_end >= len(cards):
            return
        contiguous = [
            item for item in sorted(existing, key=lambda value: value["start_chapter"])
            if item["end_chapter"] <= previous_local_end
        ]
        if not contiguous:
            return
        tail = contiguous[-1]
        window_end = min(len(cards), previous_local_end + 10)
        window = [
            card for card in cards
            if int(tail["start_chapter"]) <= int(card["chapter"]) <= window_end
        ]
        if len(window) <= (tail["end_chapter"] - tail["start_chapter"] + 1):
            return
        print(
            f"  Volume {spec['index']} tail-segment re-eval: old chapters {tail['start_chapter']}-{tail['end_chapter']}"
            f" + previous open tail + first {window_end - previous_local_end} new chapters..."
        )
        prompt = PromptLoader.load(
            "reference_segment_extract",
            window_start=window[0]["chapter"],
            window_end=window[-1]["chapter"],
            max_chapters=self.max_chapters_per_segment,
            is_final_window="no",
            previous_tail_context=tail["content"],
            chapter_cards_json=self._segment_cards_json(window),
        )
        payload = self._generate_json(prompt, f"volume {spec['index']} tail-segment boundary re-eval")
        segments = self._normalize_segments(payload, window)
        if not segments or int(segments[-1]["end_chapter"]) < int(tail["end_chapter"]):
            print("    -> Re-evaluation did not form a reliable new boundary; keeping the original tail segment.")
            return

        next_index = int(tail["index"])
        replacements = []
        with tempfile.TemporaryDirectory(prefix=".tail_resegment_stage_", dir=str(arc_dir)) as staging_name:
            staging_dir = Path(staging_name)
            for segment in segments:
                segment["segment_id"] = next_index
                filename = f"arc_{next_index:03d}_ch{segment['start_chapter']:03d}_{segment['end_chapter']:03d}.md"
                staged_path = staging_dir / filename
                _write_text(staged_path, self._render_segment(segment))
                replacements.append((staged_path, arc_dir / filename))
                next_index += 1
            self._commit_tail_replacements(tail["path"], replacements, arc_dir)
        print(
            f"    -> Tail boundary re-split; current re-eval covers through chapter {segments[-1]['end_chapter']}."
        )

    @staticmethod
    def _commit_tail_replacements(
        old_tail_path: Path,
        replacements: list[tuple[Path, Path]],
        arc_dir: Path,
    ) -> None:
        """Commit fully staged tail files, restoring every prior path if a rename fails."""
        backup_dir = Path(tempfile.mkdtemp(prefix=".tail_resegment_backup_", dir=str(arc_dir)))
        prior_paths = {old_tail_path}
        prior_paths.update(destination for _, destination in replacements if destination.exists())
        moved: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        remove_backup_dir = True
        try:
            for original in sorted(prior_paths, key=lambda path: path.name):
                if not original.exists():
                    continue
                backup = backup_dir / original.name
                original.replace(backup)
                moved.append((original, backup))
            for staged, destination in replacements:
                staged.replace(destination)
                committed.append(destination)
        except Exception as commit_error:
            rollback_errors = []
            for destination in reversed(committed):
                try:
                    destination.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(f"remove {destination.name}: {exc}")
            for original, backup in reversed(moved):
                try:
                    if backup.exists():
                        backup.replace(original)
                except OSError as exc:
                    rollback_errors.append(f"restore {original.name}: {exc}")
            if rollback_errors:
                remove_backup_dir = False
                raise RuntimeError(
                    f"Tail resegmentation commit failed and rollback was incomplete; "
                    f"recoverable backups remain in {backup_dir}: {'; '.join(rollback_errors)}"
                ) from commit_error
            raise
        finally:
            if remove_backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)

    @staticmethod
    def _segment_cards_json(cards: list[dict[str, Any]]) -> str:
        """Keep a long rolling window bounded without dropping its early or late chapter facts."""
        compact_cards = []
        for card in cards:
            rhythm = card.get("chapter_rhythm") if isinstance(card.get("chapter_rhythm"), dict) else {}
            compact_cards.append({
                "chapter": card.get("chapter"),
                "global_chapter": card.get("global_chapter", card.get("chapter")),
                "title": _compact_text(card.get("title"), 120),
                "chapter_outline_600": _compact_text(
                    card.get("chapter_outline_600") or card.get("summary"), 700,
                ),
                "chapter_rhythm": {
                    "core_content": _compact_text(rhythm.get("core_content"), 240),
                    "emotion_tone": _compact_text(rhythm.get("emotion_tone"), 180),
                    "beat_detail": _compact_text(rhythm.get("beat_detail"), 240),
                },
                "story_line": _compact_text(card.get("story_line"), 360),
                "entities": card.get("entities") or {},
                "evidence": (card.get("evidence") or [])[:4],
                "uncertainty": _compact_text(card.get("uncertainty"), 180),
            })
        return json.dumps(compact_cards, ensure_ascii=False, separators=(",", ":"))

    def _load_arc_items(self, arc_dir: Path) -> list[dict[str, Any]]:
        items = []
        for path in sorted(arc_dir.glob("arc_*_ch*_*.md")):
            match = ARC_FILE_RE.match(path.name)
            if not match:
                continue
            items.append({
                "index": int(match.group(1)),
                "start_chapter": int(match.group(2)),
                "end_chapter": int(match.group(3)),
                "path": path,
                "content": _read_text(path),
            })
        return items

    def _quarantine_placeholder_arcs(self, arc_dir: Path) -> int:
        quarantine_dir = arc_dir / "quarantine"
        quarantined_count = 0
        for path in sorted(arc_dir.glob("arc_*_ch*_*.md")):
            content = _read_text(path)
            if content and not self._is_placeholder(content):
                continue
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            destination = quarantine_dir / path.name
            if destination.exists():
                destination = quarantine_dir / f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
            path.replace(destination)
            quarantined_count += 1
            print(f"  Quarantined incomplete reference segment: {destination}")
        return quarantined_count

    @staticmethod
    def _contiguous_end(items: list[dict[str, Any]]) -> int:
        expected = 1
        for item in sorted(items, key=lambda value: (value["start_chapter"], value["end_chapter"])):
            if item["start_chapter"] != expected:
                break
            expected = item["end_chapter"] + 1
        return expected - 1

    def _normalize_segments(self, payload: dict[str, Any], window: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = payload.get("completed_segments") or payload.get("segments") or []
        if not isinstance(candidates, list):
            return []
        expected = int(window[0]["chapter"])
        available = {int(card["chapter"]) for card in window}
        accepted = []
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            try:
                start = int(raw.get("start_chapter"))
                end = int(raw.get("end_chapter"))
            except (TypeError, ValueError):
                continue
            if end < start or start != expected:
                break
            if any(number not in available for number in range(start, end + 1)):
                break
            analysis_status = _compact_text(raw.get("analysis_status") or "complete", 40).lower()
            quality_status = _compact_text(raw.get("quality_status") or "evidence_supported", 60).lower()
            required = [
                raw.get("narrative_function"),
                raw.get("boundary_reason"),
                raw.get("structure"),
            ]
            if analysis_status != "complete" or quality_status not in {
                "evidence_supported", "verified", "complete",
            }:
                break
            if any(not str(value or "").strip() or self._is_placeholder(value) for value in required):
                break
            evidence_chapters = []
            raw_evidence = raw.get("evidence_chapters") or []
            if isinstance(raw_evidence, list):
                for value in raw_evidence:
                    try:
                        chapter = int(value)
                    except (TypeError, ValueError):
                        continue
                    if start <= chapter <= end and chapter not in evidence_chapters:
                        evidence_chapters.append(chapter)
            if not evidence_chapters:
                # Older model/cache responses did not emit this additive field; their validated range is the provenance.
                evidence_chapters = list(range(start, end + 1))
            normalized = {
                "start_chapter": start,
                "end_chapter": end,
                "title": _compact_text(raw.get("title") or f"Chapters {start}-{end} plot", 180),
                "narrative_function": _compact_text(raw.get("narrative_function"), 600),
                "boundary_reason": _compact_text(raw.get("boundary_reason"), 700),
                "structure": _compact_text(raw.get("structure"), 1400),
                "protagonist_action": _compact_text(raw.get("protagonist_action"), 1000),
                "emotion_rhythm": _compact_text(raw.get("emotion_rhythm"), 700),
                "satisfaction_point": _compact_text(raw.get("satisfaction_point"), 700),
                "character_changes": _compact_text(raw.get("character_changes"), 900),
                "gains_costs": _compact_text(raw.get("gains_costs"), 800),
                "foreshadowing": _compact_text(raw.get("foreshadowing"), 900),
                "evidence_chapters": evidence_chapters,
                "confidence": self._normalize_confidence(raw.get("confidence")),
                "uncertainty": _compact_text(raw.get("uncertainty"), 400),
                "analysis_status": "complete",
                "quality_status": "evidence_supported",
            }
            if any(self._is_placeholder(value) for value in normalized.values() if isinstance(value, str)):
                break
            accepted.append(normalized)
            expected = end + 1
        return accepted

    @staticmethod
    def _is_placeholder(value: Any) -> bool:
        text = str(value or "").strip().lower()
        return bool(re.search(
            r"\b(?:tbd|todo|placeholder|to be filled|fill (?:this|later)|unknown for now)\b|待补|待填写|占位",
            text,
        ))

    @staticmethod
    def _write_pending_segment(arc_dir: Path, cards: list[dict[str, Any]], reason: str) -> None:
        path = arc_dir / "_pending_segment.json"
        if not cards:
            path.unlink(missing_ok=True)
            return
        _write_json(path, {
            "analysis_status": "incomplete",
            "quality_status": "quarantined",
            "start_chapter": int(cards[0]["chapter"]),
            "end_chapter": int(cards[-1]["chapter"]),
            "card_count": len(cards),
            "reason": reason or "No evidence-supported natural boundary has closed yet.",
            "resume_from_chapter": int(cards[0]["chapter"]),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    @staticmethod
    def _render_segment(segment: dict[str, Any]) -> str:
        return "\n\n".join([
            f"【Arc{segment['segment_id']}: Chapters {segment['start_chapter']}-{segment['end_chapter']} | {segment['title']}】",
            f"Plot function: {segment['narrative_function']}",
            f"Boundary reason: {segment['boundary_reason']}",
            f"Rise and turn: {segment['structure']}",
            "Narrative stages: summarize advance, pressure, turn, and staged close from single-chapter fact cards.",
            f"Protagonist action chain: {segment['protagonist_action']}",
            f"Conflict and emotion curve: {segment['emotion_rhythm']}",
            f"Core payoff or tension: {segment['satisfaction_point']}",
            f"Character and relationship change: {segment['character_changes']}",
            f"Gains and costs: {segment['gains_costs']}",
            f"Foreshadowing and next bind: {segment['foreshadowing']}",
            f"Evidence chapters: {', '.join(str(value) for value in segment['evidence_chapters'])}",
            f"Analysis status: {segment['analysis_status']} / {segment['quality_status']}",
            f"Confidence: {segment['confidence']['level']} ({segment['confidence']['score']})",
            f"Uncertainty: {segment['uncertainty'] or 'No additional uncertainty stated.'}",
        ])

    @staticmethod
    def _write_arc_index(arc_dir: Path, items: list[dict[str, Any]]) -> None:
        _write_json(arc_dir / "arcs_index.json", [
            {
                "id": item["index"],
                "start_ch": item["start_chapter"],
                "end_ch": item["end_chapter"],
                "file": item["path"].name,
            }
            for item in items
        ])

    def _update_volume_state(
        self,
        spec: dict[str, Any],
        closed_through: int,
        available_through: int,
        pending_reason: str = "",
    ) -> None:
        volumes = self.state.setdefault("volumes", {})
        volumes[str(spec["index"])] = {
            "title": spec["title"],
            "directory": spec["directory_name"],
            "global_start": spec["global_start"],
            "total_chapters": spec["total_count"],
            "available_through": available_through,
            "closed_through": closed_through,
            "pending_start": closed_through + 1 if closed_through < available_through else None,
            "analysis_status": "complete" if closed_through >= available_through else "incomplete",
            "pending_quality": None if closed_through >= available_through else "quarantined",
            "pending_reason": pending_reason if closed_through < available_through else "",
        }
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(self.state_path, self.state)

    def _build_structures(self, specs: list[dict[str, Any]], target: int, total: int) -> dict[str, bool]:
        volume_outlines = []
        segmented_global: list[int] = []
        fingerprint_parts = []
        for spec in specs:
            arc_items = self._load_arc_items(spec["directory"] / "story_arcs")
            closed = self._contiguous_end(arc_items)
            if not arc_items or not closed:
                continue
            arc_texts = [item["content"] for item in arc_items if item["content"]]
            if not arc_texts:
                continue
            digest = hashlib.sha256("\n".join(arc_texts).encode("utf-8")).hexdigest()
            fingerprint_parts.append(f"{spec['index']}:{digest}")
            state = self.state.setdefault("volumes", {}).setdefault(str(spec["index"]), {})
            outline_path = spec["directory"] / "volume_outline.md"
            if state.get("structure_digest") != digest or not outline_path.is_file():
                suffix = "this volume is complete" if closed >= spec["total_count"] else f"currently covers only chapters 1-{closed} of this volume"
                prompt = PromptLoader.load(
                    "volume_merge",
                    volume_title=f"{spec['title']}（{suffix}）",
                    start_chapter=1,
                    end_chapter=closed,
                    total_chapters=closed,
                    total_batches=len(arc_texts),
                    batch_summaries="\n\n---\n\n".join(arc_texts),
                )
                outline = normalize_text(self._generate_text(prompt, f"volume {spec['index']} structure extract"))
                _write_text(outline_path, outline)
                state["structure_digest"] = digest
            volume_outlines.append({"title": spec["title"], "outline": _read_text(outline_path)})
            segmented_global.extend(spec["global_start"] + local - 1 for local in range(1, closed + 1))

        fingerprint = hashlib.sha256("|".join(fingerprint_parts).encode("utf-8")).hexdigest()
        structure = self.state.setdefault("structure", {})
        novel_outline_path = self.outlines_dir / "novel_outline.md"
        changed = structure.get("fingerprint") != fingerprint or not novel_outline_path.is_file()
        if not volume_outlines:
            return {"updated": False}
        if changed:
            all_outlines = "\n\n---\n\n".join(
                f"【{item['title']}】\n{item['outline']}" for item in volume_outlines
            )
            prompt = PromptLoader.load("novel_extract", all_volume_outlines=all_outlines)
            generated = normalize_text(self._generate_text(prompt, "reference-novel whole-book structure extract"))
            coverage = self._coverage_header(target, total, segmented_global)
            _write_text(novel_outline_path, coverage + "\n\n" + generated)
            structure.update({
                "fingerprint": fingerprint,
                "segmented_ranges": _ranges(segmented_global),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
        return {"updated": changed}

    def _generate_text(self, prompt: str, label: str) -> str:
        if not self.llm:
            raise RuntimeError("No usable model is configured.")
        result = self.llm.generate(prompt, temperature=0.2)
        if not result:
            raise RuntimeError(f"{label} did not receive model output.")
        return result

    @staticmethod
    def _coverage_header(target: int, total: int, segmented_global: list[int]) -> str:
        ranges = _ranges(segmented_global)
        range_text = "、".join(
            f"chapter {item['start']}" if item["start"] == item["end"] else f"chapters {item['start']}-{item['end']}"
            for item in ranges
        ) or "none yet"
        state = "final structure" if target == total and ranges and ranges[-1]["end"] >= total else "staged structure"
        return "\n".join([
            "# Deconstruction coverage",
            "",
            f"Structure type: {state}",
            f"Single-chapter fact-card coverage: chapters 1-{target} / {total} in the book",
            f"Closed story-segment coverage: {range_text}",
            "Note: an unclosed tail is not forced into the structure; later deconstruction continues to fill it without changing closed segments.",
        ])


def run_reference_analysis(
    txt_path: str | Path,
    output_dir: str | Path,
    *,
    batch_size: int = 20,
    max_chapters: int | None = None,
    resume: bool = False,
    rebuild: bool = False,
) -> dict[str, Any]:
    """CLI entry: run the three-stage analysis with the reference-deconstruction model."""
    config = ConfigLoader.get_data_builder_config()
    if not config.get("api_key"):
        config["api_key"] = os.getenv("OPENAI_API_KEY")
    if not config.get("api_key"):
        raise RuntimeError("Reference-deconstruction model API Key not detected.")
    analyzer = ReferenceAnalyzer(
        txt_path,
        output_dir,
        max_chapters=max_chapters,
        card_batch_size=batch_size,
        max_workers=min(8, max(2, batch_size)),
        segment_load_size=min(8, max(4, batch_size // 2)),
        max_chapters_per_segment=12,
        llm=LLMProvider(**config),
        rebuild=rebuild,
    )
    return analyzer.run()


def mark_resegmented(output_dir: str | Path) -> None:
    """Record that a legacy virtual split was regrouped; a later full retry must not write back original single-volume dirs."""
    state_path = Path(output_dir) / "analysis_state.json"
    state = _read_json(state_path, {})
    if not isinstance(state, dict) or state.get("pipeline_version") != PIPELINE_VERSION:
        return
    state["resegmented"] = True
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(state_path, state)
