#!/usr/bin/env python3
"""harness-novel unified CLI entry"""

import sys
import os
import argparse
import re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

def cmd_config(args):
      """Initialize the global config file ~/.harnessNovel/.env"""
      import os
      config_dir = os.path.join(os.path.expanduser("~"), ".harnessNovel")
      env_path = os.path.join(config_dir, ".env")
      if os.path.exists(env_path) and not args.force:
          print(f"Config file already exists: {env_path}")
          print("Use --force to overwrite")
          return
      os.makedirs(config_dir, exist_ok=True)
      template = """# Reference novel story-arc extraction (init flow; flash model recommended)
  DATA_BUILDER_MODEL=deepseek-v4-flash
  DATA_BUILDER_BASE_URL=https://api.deepseek.com
  DATA_BUILDER_API_KEY=your-api-key

  # Book design and stage design (pro model recommended)
  ADAPTIVE_BUILDER_MODEL=deepseek-v4-pro
  ADAPTIVE_BUILDER_BASE_URL=https://api.deepseek.com
  ADAPTIVE_BUILDER_API_KEY=your-api-key

  # Story arcs, chapter outlines, drafts, and lightweight tasks (flash model recommended)
  ADAPTIVE_BUILDER_LITE_MODEL=deepseek-v4-flash
  ADAPTIVE_BUILDER_LITE_BASE_URL=https://api.deepseek.com
  ADAPTIVE_BUILDER_LITE_API_KEY=your-api-key
  """
      with open(env_path, "w", encoding="utf-8") as f:
          f.write(template)
      print(f"Config file created: {env_path}")
      print("Edit the file and fill in your API key")

def cmd_list(args):
    from core.workspace import list_novels
    novels = list_novels()
    if novels:
        print("Existing workspaces:")
        for name in novels:
            print(f"  - {name}")
    else:
        print("No workspaces yet.")


def _reference_state_path(ws):
    return os.path.join(ws.reference, "import_state.json")


def _uploaded_source_name(path):
    """Strip the random prefix from a web temp upload and keep the original filename."""
    return re.sub(r"^[0-9a-f]{16}_", "", os.path.basename(path), flags=re.IGNORECASE)


def _load_reference_state(ws):
    import json

    path = _reference_state_path(ws)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_reference_state(ws, state):
    import json

    with open(_reference_state_path(ws), "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _saved_reference_story_arc_end(ws):
    """Return total chapters covered by saved story arcs, for resumable reference deconstruction."""
    import json
    import re

    outlines_dir = os.path.join(ws.reference, "outlines")
    if not os.path.isdir(outlines_dir):
        return 0

    pattern = re.compile(r"^arc_\d+_ch\d+_(\d+)\.md$", re.IGNORECASE)
    local_coverage = 0
    global_endpoints = []
    for dirname in sorted(os.listdir(outlines_dir)):
        vol_dir = os.path.join(outlines_dir, dirname)
        arc_dir = os.path.join(vol_dir, "story_arcs")
        if not os.path.isdir(arc_dir):
            continue
        volume_end = 0
        for filename in os.listdir(arc_dir):
            matched = pattern.match(filename)
            if matched:
                volume_end = max(volume_end, int(matched.group(1)))
        if not volume_end:
            continue
        local_coverage += volume_end
        meta_path = os.path.join(vol_dir, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta_end = int(json.load(handle).get("end_ch") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            meta_end = 0
        if meta_end:
            global_endpoints.append(meta_end)
    return max(global_endpoints) if global_endpoints else local_coverage


def _reference_card_complete_count(ws):
    """Read completed chapter-card count from analysis_state as a reliable lower bound on deconstruction progress.

    Chapter fact cards are the factual base of deconstruction: a chapter without a card cannot have been deconstructed. More reliable than story-arc volume meta.json,
    which can keep stale values after a source change or partial deconstruction.
    """
    import json

    path = os.path.join(ws.reference, "analysis_state.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return 0
    cards = data.get("chapter_cards") if isinstance(data, dict) else None
    if not isinstance(cards, dict):
        return 0
    try:
        return int(cards.get("complete_count") or 0)
    except (TypeError, ValueError):
        return 0


def _canonical_chapter_digest(chapter):
    """Build the same chapter digest as the deconstructor, to find the shared prefix of a new full-book snapshot."""
    import hashlib
    import re
    from core.text_utils import normalize_text

    content = normalize_text(str(chapter.get("content") or ""))
    canonical = re.sub(r"\s+", "", content)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replace_reference_with_latest_snapshot(ws, incoming_path):
    """Replace the source file with the author's latest full-book snapshot and reuse fact cards for the shared prefix."""
    import hashlib
    import json
    import tempfile
    from core.text_encoding import decode_text_bytes
    from training.outline_builder import split_chapters

    with open(incoming_path, "rb") as handle:
        new_text, source_encoding = decode_text_bytes(handle.read())
    _, old_chapters = split_chapters(ws.reference_sample)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write(new_text)
        temporary_path = handle.name
    try:
        _, new_chapters = split_chapters(temporary_path)
    finally:
        os.unlink(temporary_path)
    if not new_chapters:
        raise ValueError("No valid chapters were found in the uploaded file.")

    reusable_limit = min(_reference_card_complete_count(ws), len(old_chapters))
    common_prefix = 0
    for old_chapter, new_chapter in zip(old_chapters, new_chapters):
        if _canonical_chapter_digest(old_chapter) != _canonical_chapter_digest(new_chapter):
            break
        common_prefix += 1
    if common_prefix < reusable_limit:
        raise ValueError(
            f"The new novel diverges from deconstructed content starting at chapter {common_prefix + 1},"
            f"so the first {reusable_limit} chapters cannot be reused safely. If older chapters were edited, rebuild the deconstruction."
        )
    if len(new_chapters) < reusable_limit:
        raise ValueError(
            f"The new novel only has {len(new_chapters)} chapters, fewer than the {reusable_limit} already deconstructed."
        )

    with open(ws.reference_sample, "w", encoding="utf-8") as handle:
        handle.write(new_text)
    source_digest = hashlib.sha256(new_text.encode("utf-8")).hexdigest()

    cards_dir = os.path.join(ws.reference, "chapter_cards")
    for number in range(1, reusable_limit + 1):
        path = os.path.join(cards_dir, f"chapter_{number:04d}.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                card = json.load(handle)
            card["source_digest"] = source_digest
            card["content_digest"] = _canonical_chapter_digest(new_chapters[number - 1])
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(card, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except (OSError, ValueError):
            continue

    analysis_path = os.path.join(ws.reference, "analysis_state.json")
    if os.path.isfile(analysis_path):
        try:
            with open(analysis_path, "r", encoding="utf-8") as handle:
                analysis = json.load(handle)
            analysis["source_digest"] = source_digest
            analysis["total_chapters"] = len(new_chapters)
            analysis["latest_snapshot"] = {
                "common_prefix_chapters": common_prefix,
                "reused_chapter_cards": reusable_limit,
                "new_chapters": max(0, len(new_chapters) - reusable_limit),
                "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
            with open(analysis_path, "w", encoding="utf-8") as handle:
                json.dump(analysis, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except (OSError, ValueError):
            pass

    return {
        "encoding": source_encoding,
        "old_chapters": len(old_chapters),
        "total_chapters": len(new_chapters),
        "common_prefix": common_prefix,
        "reused_cards": reusable_limit,
        "new_chapters": max(0, len(new_chapters) - reusable_limit),
    }


def _run_reference_pipeline(ws, batch_size, max_chapters=None, resume=False, source_name=None, source_encoding=None,
                            rebuild_reference=False):
    """Run reference deconstruction; resume only processes chapters after the saved coverage."""
    import re
    from datetime import datetime
    from training.outline_builder import run_outline_build, split_chapters, split_chapters_to_files

    _, all_chapters = split_chapters(ws.reference_sample)
    total_chapters = len(all_chapters)
    if not total_chapters:
        print("Error: no valid chapters were found, so deconstruction cannot run. Check the novel's chapter-heading format.")
        return False

    target_chapters = min(max_chapters or total_chapters, total_chapters)
    previous_state = _load_reference_state(ws)
    state_progress = int(previous_state.get("processed_chapters") or 0)
    arc_progress = _saved_reference_story_arc_end(ws)
    # Tolerance: a stale volume meta.json end_ch can inflate arc_progress (after a source change or partial deconstruction).
    # Chapter fact cards are the true deconstruction base, and processed chapters cannot exceed the source chapter count; cap here so resume is not blocked and messages stay accurate.
    arc_progress = min(arc_progress, total_chapters, _reference_card_complete_count(ws) or total_chapters)
    previous_chapters = max(state_progress, arc_progress)
    if rebuild_reference:
        print("  Rebuild requested; existing reference deconstruction assets will be cleared.")
        previous_chapters = 0
    if resume and target_chapters < previous_chapters:
        print(f"Already deconstructed through chapter {previous_chapters}; the target chapter count cannot be smaller.")
        return False

    if target_chapters < total_chapters:
        print(f"  Deconstruction range: first {target_chapters}/{total_chapters} chapters (you can continue later)")
    else:
        print(f"  Deconstruction range: whole book ({total_chapters} chapters)")

    if resume and target_chapters == previous_chapters and not rebuild_reference:
        print(f"  Retrying from the saved result at chapter {previous_chapters} to finish remaining derived steps.")

    # Write in-progress state first. Even if a model call or process stops, the web UI still knows the full range and can resume from saved arcs.
    _save_reference_state(ws, {
        "source_name": source_name or previous_state.get("source_name") or os.path.basename(ws.reference_sample),
        "source_encoding": source_encoding or previous_state.get("source_encoding") or "UTF-8",
        "total_chapters": total_chapters,
        "processed_chapters": previous_chapters,
        "is_complete": False,
        "status": "in_progress",
        "batch_size": batch_size,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })

    print()
    split_chapters_to_files(ws, max_chapters=target_chapters, refresh=resume)

    print()
    analysis_result = run_outline_build(
        txt_path=ws.reference_sample,
        output_dir=ws.reference,
        batch_size=batch_size,
        max_chapters=target_chapters,
        resume=resume,
        rebuild_reference=rebuild_reference,
    )
    if not analysis_result:
        raise RuntimeError("Reference deconstruction did not produce a valid result.")

    outlines_dir = os.path.join(ws.reference, "outlines")
    is_partial = target_chapters < total_chapters
    if not is_partial and os.path.isdir(outlines_dir):
        vol_dirs = [
            name for name in sorted(os.listdir(outlines_dir))
            if re.match(r"^vol_\d+_.+$", name) and os.path.isdir(os.path.join(outlines_dir, name))
        ]
        if len(vol_dirs) <= 1:
            print("\nOnly one volume was detected; running intelligent volume split...")
            from training.outline_builder import resegment
            from training.reference_analyzer import mark_resegmented
            resegment(outlines_dir)
            resulting_dirs = [
                name for name in os.listdir(outlines_dir)
                if re.match(r"^vol_\d+_.+$", name) and os.path.isdir(os.path.join(outlines_dir, name))
            ]
            if resulting_dirs and not any("全书" in name for name in resulting_dirs):
                mark_resegmented(ws.reference)
            else:
                print("  Intelligent volume split did not finish; keeping the current deconstruction state for the next retry.")
        else:
            print(f"\nDetected {len(vol_dirs)} volumes; skipping intelligent volume split.")
    elif is_partial:
        print("\nThis is a partial deconstruction; keeping existing story arcs. Run intelligent volume split after the whole book is deconstructed.")

    _save_reference_state(ws, {
        "source_name": source_name or previous_state.get("source_name") or os.path.basename(ws.reference_sample),
        "source_encoding": source_encoding or previous_state.get("source_encoding") or "UTF-8",
        "total_chapters": total_chapters,
        "processed_chapters": target_chapters,
        "is_complete": target_chapters >= total_chapters,
        "status": "complete",
        "batch_size": batch_size,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"\nWorkspace directory: {ws.root}")
    return True


def cmd_init(args):
    """Create a workspace. novel init <name> --txt <path>"""
    from core.workspace import init_workspace
    from core.text_encoding import copy_as_utf8

    ws = init_workspace(args.workspace)

    txt_path = args.txt

    if not txt_path:
        print(f"Workspace '{args.workspace}' created: {ws.root}")
        print("Tip: use --txt to add a reference novel, for example: novel init <name> --txt novel.txt")
        return

    if not os.path.exists(txt_path):
        print(f"Error: file does not exist: {txt_path}")
        return
    if args.max_chapters is not None and args.max_chapters < 1:
        print("Error: --max-chapters must be a positive integer.")
        return

    dest = ws.reference_sample
    try:
        source_encoding = copy_as_utf8(txt_path, dest)
    except ValueError as exc:
        print(f"Error: {exc}")
        return
    name = os.path.splitext(os.path.basename(txt_path))[0]
    print(f"Workspace '{args.workspace}' created")
    print(f"  Reference novel: {name}")
    print(f"  File location: {dest}")
    if source_encoding == "UTF-8":
        print("  File encoding: UTF-8")
    else:
        print(f"  File encoding: detected {source_encoding}, converted to UTF-8")
    if args.no_analyze:
        print("  Reference novel imported. In the reference step, choose whole-book or first-N deconstruction, then start.")
        return
    _run_reference_pipeline(
        ws,
        batch_size=args.batch_size,
        max_chapters=args.max_chapters,
        source_name=_uploaded_source_name(txt_path),
        source_encoding=source_encoding,
        rebuild_reference=args.rebuild_reference,
    )


def cmd_reference_resume(args):
    """Resume deconstruction; an uploaded file is treated as the author's latest full-book snapshot."""
    from core.workspace import init_workspace

    ws = init_workspace(args.workspace)
    if not os.path.isfile(ws.reference_sample):
        print("Error: this workspace has not imported a reference novel. Run novel init <workspace> --txt <novel.txt> first.")
        return
    if args.max_chapters is not None and args.max_chapters < 1:
        print("Error: --max-chapters must be a positive integer.")
        return
    snapshot_source_name = None
    snapshot_source_encoding = None
    if args.txt:
        if not os.path.isfile(args.txt):
            print(f"Error: the new novel file does not exist: {args.txt}")
            return
        try:
            snapshot = _replace_reference_with_latest_snapshot(ws, args.txt)
        except ValueError as exc:
            print(f"Error: {exc}")
            return
        print(
            f"  Recognized a new full-book novel ({snapshot['encoding']}): {snapshot['total_chapters']} chapters;"
            f" reusing deconstruction for the first {snapshot['reused_cards']} chapters, with {snapshot['new_chapters']} new chapters to deconstruct."
        )
        snapshot_source_name = _uploaded_source_name(args.txt)
        snapshot_source_encoding = snapshot["encoding"]
    _run_reference_pipeline(
        ws,
        batch_size=args.batch_size,
        max_chapters=args.max_chapters,
        resume=True,
        source_name=snapshot_source_name,
        source_encoding=snapshot_source_encoding,
        rebuild_reference=args.rebuild_reference,
    )


def _ws(name):
    from core.workspace import init_workspace
    return init_workspace(name)


def _resolve_volume_arg(args):
    """Resolve the new-flow volume number. --stage is kept only as a compatibility alias."""
    volume = getattr(args, "volume", None)
    stage = getattr(args, "stage", None)
    if volume is not None and stage is not None and volume != stage:
        print("Error: in the current flow a stage is the same as a volume; different --volume and --stage values cannot be set together.")
        print("Use --volume N; --stage is kept only for older commands.")
        return None
    return volume if volume is not None else (stage if stage is not None else 1)


# ── Imitation flow ──────────────────────────────────────────────

def cmd_novel_outline(args):
    from training.adaptive_builder import gen_novel_outline
    ws = _ws(args.workspace)
    gen_novel_outline(ws, force=args.force, creative_direction=args.direction,
                      direction_file=args.direction_file)


def cmd_world_import(args):
    from training.adaptive_builder import (
        build_target_world_knowledge,
        import_target_world_sources,
    )
    ws = _ws(args.workspace)
    import_target_world_sources(ws, args.paths, force=args.force)
    if getattr(args, "build", False):
        result = build_target_world_knowledge(
            ws,
            force=False,
            chunk_size=args.chunk_size,
            chapter_batch_size=args.chapter_batch_size,
            max_workers=args.max_workers,
            primary_source=args.primary,
        )
        if not result:
            raise RuntimeError("Target-world sources were imported, but knowledge-base build failed. Check the task log and retry.")


def cmd_world_build(args):
    from training.adaptive_builder import build_target_world_knowledge
    ws = _ws(args.workspace)
    result = build_target_world_knowledge(
        ws,
        force=args.force,
        chunk_size=args.chunk_size,
        chapter_batch_size=args.chapter_batch_size,
        max_workers=args.max_workers,
        primary_source=args.primary,
        merge_only=args.merge_only,
    )
    if not result:
        raise RuntimeError("Target-world knowledge-base build failed. Check the task log and retry.")


def cmd_novel_name_synopsis(args):
    from training.adaptive_builder import gen_novel_name_synopsis
    ws = _ws(args.workspace)
    gen_novel_name_synopsis(ws, force=args.force)


def cmd_story_design(args):
    from training.adaptive_builder import gen_story_design
    ws = _ws(args.workspace)
    gen_story_design(ws, force=args.force, creative_direction=args.direction,
                     direction_file=args.direction_file)


def cmd_story_design_extend(args):
    from training.adaptive_builder import extend_story_design
    ws = _ws(args.workspace)
    extend_story_design(
        ws,
        use_reference=args.use_reference,
        creative_direction=args.direction,
        direction_file=args.direction_file,
    )


def cmd_design_concept(args):
    from training.adaptive_builder import gen_design_concept
    ws = _ws(args.workspace)
    gen_design_concept(ws, force=args.force, creative_direction=args.direction,
                       direction_file=args.direction_file)


def cmd_stage_design(args):
    from training.adaptive_builder import gen_stage_design
    ws = _ws(args.workspace)
    gen_stage_design(ws, force=args.force, creative_direction=args.direction,
                     direction_file=args.direction_file)


def cmd_stage_insert(args):
    from training.adaptive_builder import insert_stage
    ws = _ws(args.workspace)
    if args.after_stage is not None and args.before_stage is not None:
        print("Error: --after-stage and --before-stage cannot be used together.")
        return
    insert_stage(
        ws,
        creative_direction=args.direction,
        direction_file=args.direction_file,
        after_stage=args.after_stage,
        before_stage=args.before_stage,
    )


def cmd_mechanics_init(args):
    from training.adaptive_builder import init_mechanics
    ws = _ws(args.workspace)
    init_mechanics(
        ws,
        force=args.force,
        creative_direction=args.direction,
        direction_file=args.direction_file,
        mechanics_file=args.file,
        disable=args.none,
    )


def cmd_volume_outline(args):
    from training.adaptive_builder import gen_volume_outline
    ws = _ws(args.workspace)
    gen_volume_outline(ws, volume=args.volume, force=args.force,
                       creative_direction=args.direction)


def cmd_story_arcs(args):
    from training.adaptive_builder import gen_story_arcs
    volume = _resolve_volume_arg(args)
    if volume is None:
        return
    ws = _ws(args.workspace)
    gen_story_arcs(ws, volume=volume, force=args.force)


def cmd_chapter_outlines(args):
    from training.adaptive_builder import gen_serial_chapter_outlines
    volume = _resolve_volume_arg(args)
    if volume is None:
        return
    ws = _ws(args.workspace)
    gen_serial_chapter_outlines(ws, volume=volume, force=args.force)


def cmd_write(args):
    from training.adaptive_builder import gen_serial_chapters
    volume = _resolve_volume_arg(args)
    if volume is None:
        return
    ws = _ws(args.workspace)
    gen_serial_chapters(ws, volume=volume, start_chapter=args.start,
                        max_chapters=args.max,
                        humanize=not args.no_humanize,
                        humanize_existing=args.humanize_existing)


def cmd_web(args):
    """Start the local visual workbench."""
    try:
        import uvicorn
        from webui.app import create_app
    except ImportError as exc:
        print("Error: web workbench dependencies are not installed. Run pip install --upgrade harnessNovel again.")
        print(f"Details: {exc}")
        return

    app = create_app(workspace_root=args.workspace_root)
    print(f">>> HarnessNovel web workbench started: http://{args.host}:{args.port} <<<")
    print("Press Ctrl+C to stop the server.")
    uvicorn.run(app, host=args.host, port=args.port)


# ── Main entry ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="novel",
        description="harness-novel unified CLI",
    )
    sub = parser.add_subparsers(dest="command", help="subcommands")

    # config
    p = sub.add_parser("config", help="Initialize the global config file")
    p.add_argument("--force", action="store_true", help="Overwrite existing config")

    # list
    sub.add_parser("list", help="List all workspaces")

    # init
    p = sub.add_parser("init", help="Create a workspace")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--txt", help="Reference novel file path")
    p.add_argument("--batch-size", type=int, default=20, help="Chapters per read window for story-arc detection (default 20)")
    p.add_argument("--max-chapters", type=int, default=None, help="Deconstruct only the first N chapters (default: whole book)")
    p.add_argument("--no-analyze", action="store_true", help="Import the reference novel only; do not start deconstruction yet")
    p.add_argument("--rebuild-reference", action="store_true", help="Clear existing reference deconstruction assets and deconstruct again")

    # reference-resume
    p = sub.add_parser("reference-resume", help="Resume deconstruction of an imported reference novel without re-uploading")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--txt", help="Full novel TXT re-downloaded after the author updated it")
    p.add_argument("--batch-size", type=int, default=20, help="Chapters per read window for story-arc detection (default 20)")
    p.add_argument("--max-chapters", type=int, default=None, help="Extend deconstruction through the first N chapters (default: whole book)")
    p.add_argument("--rebuild-reference", action="store_true", help="Clear existing reference deconstruction assets and deconstruct again")

    # novel-outline
    p = sub.add_parser("novel-outline", help="Generate core gameplay, long mainline, stage roadmap, and character arcs")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--direction", help="Creative direction (string)")
    p.add_argument("--direction-file", help="Creative-direction file path")

    # world-import
    p = sub.add_parser("world-import", help="Import target-genre sources")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("paths", nargs="+", help="Source file or directory paths; multiple allowed")
    p.add_argument("--force", action="store_true", help="Overwrite already imported files from the same source")
    p.add_argument("--build", action="store_true", help="Build the target-world knowledge base immediately after import")
    p.add_argument("--chunk-size", type=int, default=36000, help="Source chunk size in characters when building (default 36000)")
    p.add_argument("--chapter-batch-size", type=int, default=20, help="Max chapters per batch when building (default 20, also capped by chunk size)")
    p.add_argument("--max-workers", type=int, default=4, help="Parallel extraction workers for chapter batches (default 4)")
    p.add_argument("--primary", default=None, help="Set the primary source; if omitted, the largest file is used")

    # world-build
    p = sub.add_parser("world-build", help="Structure target-genre sources")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true", help="Force restructuring and aggregation")
    p.add_argument("--chunk-size", type=int, default=36000, help="Source chunk size in characters (default 36000)")
    p.add_argument("--chapter-batch-size", type=int, default=20, help="Chapters per batch for chapter-like sources (default 20)")
    p.add_argument("--max-workers", type=int, default=4, help="Parallel extraction workers for chapter batches (default 4)")
    p.add_argument("--primary", default=None, help="Primary source as file name, path, or source ID; if omitted, the largest file is used")
    p.add_argument("--merge-only", action="store_true", help="Rebuild worlds/_final and audits from existing worlds/<source>/*.md only; skip cards/canon/source worlds")

    # novel-name-synopsis
    p = sub.add_parser("novel-name-synopsis", help="Suggest title and synopsis")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true")

    # story-design
    p = sub.add_parser("story-design", help="Generate core gameplay, long mainline, stage roadmap, and character arcs")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--direction", help="Creative direction (string)")
    p.add_argument("--direction-file", help="Creative-direction file path")

    # design-concept (step 1: book design)
    p = sub.add_parser("design-concept", help="Generate rough outline and worldview (book-design step 1)")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--direction", help="Creative direction (string)")
    p.add_argument("--direction-file", help="Creative-direction file path")

    # stage-design (step 2: stage design)
    p = sub.add_parser("stage-design", help="Generate long mainline and stage roadmap from the rough outline and worldview")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--direction", help="Creative direction (string)")
    p.add_argument("--direction-file", help="Creative-direction file path")

    # story-design-extend
    p = sub.add_parser("story-design-extend", help="Keep existing design and append the mainline, character arcs, and later stages")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--use-reference", action="store_true", help="Read reference deconstruction added since the last book design")
    p.add_argument("--direction", help="Optional extend direction (string)")
    p.add_argument("--direction-file", help="Optional extend-direction file path")

    # stage-insert
    p = sub.add_parser("stage-insert", help="Design a new stage from inspiration and insert it into the stage roadmap")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--direction", help="New-stage inspiration (string)")
    p.add_argument("--direction-file", help="New-stage inspiration file path")
    p.add_argument("--after-stage", type=int, default=None, help="Prefer inserting after the given stage")
    p.add_argument("--before-stage", type=int, default=None, help="Prefer inserting before the given stage")

    # mechanics-init
    p = sub.add_parser("mechanics-init", help="Initialize the optional mechanics layer (system/panel/numbers/light state tracking)")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--force", action="store_true", help="Overwrite an existing mechanics layer")
    p.add_argument("--direction", help="Mechanics direction (string)")
    p.add_argument("--direction-file", help="Mechanics-direction file path")
    p.add_argument("--file", help="Mechanics settings file path; takes priority over --direction")
    p.add_argument("--none", action="store_true", help="Explicitly disable the mechanics layer")

    # volume-outline
    p = sub.add_parser("volume-outline", help="Imitate and generate a volume outline")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--volume", type=int, default=None, help="Volume number")
    p.add_argument("--force", action="store_true")
    p.add_argument("--direction", help="Creative direction")

    # story-arcs
    p = sub.add_parser("story-arcs", help="Generate story arcs")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--volume", type=int, default=None, help="Volume number (default 1; in the current flow one volume is one stage)")
    p.add_argument("--stage", type=int, default=None, help="Compatibility alias equal to --volume; not a stage inside a volume")
    p.add_argument("--force", action="store_true", help="Force regeneration")

    # chapter-outlines
    p = sub.add_parser("chapter-outlines", help="Generate chapter outlines serially from story arcs")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--volume", type=int, default=None, help="Volume number (default 1; in the current flow one volume is one stage)")
    p.add_argument("--stage", type=int, default=None, help="Compatibility alias equal to --volume; not a stage inside a volume")
    p.add_argument("--force", action="store_true", help="Force regeneration")

    # write
    p = sub.add_parser("write", help="Generate draft serially")
    p.add_argument("workspace", help="Workspace name")
    p.add_argument("--volume", type=int, default=None, help="Volume number (default 1; in the current flow one volume is one stage)")
    p.add_argument("--stage", type=int, default=None, help="Compatibility alias equal to --volume; not a stage inside a volume")
    p.add_argument("--start", type=int, default=1, help="Starting chapter number")
    p.add_argument("--max", type=int, default=None, help="Maximum number of chapters")
    p.add_argument("--no-humanize", action="store_true", help="Disable automatic humanization after draft generation")
    p.add_argument("--humanize-existing", action="store_true", help="Humanize existing chapter files; by default only newly generated chapters are humanized")

    # web
    p = sub.add_parser("web", help="Start the local visual workbench")
    p.add_argument("--host", default="127.0.0.1", help="Listen address (default 127.0.0.1, local only)")
    p.add_argument("--port", type=int, default=8765, help="Listen port (default 8765)")
    p.add_argument("--workspace-root", help="Workspace root; if omitted, ~/Documents/my-novels is preferred")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "list": cmd_list,
        "init": cmd_init,
        "reference-resume": cmd_reference_resume,
        "world-import": cmd_world_import,
        "world-build": cmd_world_build,
        "novel-outline": cmd_novel_outline,
        "novel-name-synopsis": cmd_novel_name_synopsis,
        "story-design": cmd_story_design,
        "design-concept": cmd_design_concept,
        "stage-design": cmd_stage_design,
        "story-design-extend": cmd_story_design_extend,
        "stage-insert": cmd_stage_insert,
        "mechanics-init": cmd_mechanics_init,
        "volume-outline": cmd_volume_outline,
        "story-arcs": cmd_story_arcs,
        "chapter-outlines": cmd_chapter_outlines,
        "write": cmd_write,
        "web": cmd_web,
        "config": cmd_config
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
