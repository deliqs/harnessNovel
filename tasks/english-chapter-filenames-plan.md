# English chapter draft filenames

Engine: grok-4.6. Co-lead: no (single unit, localized filename bug).

Historical plan. Not current source of truth. Later zero-CJK work dropped Chinese read/delete aliases for our artifacts.

## Problem

`novel write` still wrote Chinese draft filenames (chapter-N forms under `file_system/chapters/` and `file_system/drafts/`).

Chapter outlines already emit English (`chapter_001.md`). Drafts/raw backups did not get the same treatment.

## Canonical emit

| Kind | English (write) |
|---|---|
| Draft | `{n:03d}_chapter_{n}.md` |
| Raw backup | `{n:03d}_chapter_{n}.raw.md` |
| Raw version | `{n:03d}_chapter_{n}_{stamp}.raw.md` |
| Draft version | `{basename}_{stamp}` of the file being backed up |

The original freeze also kept a Chinese chapter-N filename as a read/delete alias. That alias layer is no longer the stored form for generated drafts.

## Behavior (as planned)

- **New writes** always use the English name.
- **Reads / exists / skip / resume** were planned to resolve English first, then a Chinese alias, so old workspaces still worked.
- **Overwrite** writes English; if a Chinese sibling existed, delete it after a successful write so the directory does not contain both.
- **Reset/delete** (draft_chat) removes English and leftover alias names plus both version globs.
- Wizard `chapterNumberFromPath` already parses `001_chapter_1.md` via the leading-digits regex.

## Unit 1 — Path helpers + call sites

Owner files: `core/chapter_utils.py`, `training/adaptive_builder.py`, `webui/draft_chat.py`, `docs/cli_io_mindmap.html`, `tests/test_chapter_filenames.py`.

Do not split `adaptive_builder.py`. Do not rewrite `_fix_chapter_numbering` (reference-novel titles). Do not change outline filenames (`chapter_001.md`). Do not migrate frozen world-knowledge names. Do not run git. Do not translate unrelated CJK.

Tests first; keep going until the done check is green.

## Done check

```bash
python3 -m unittest tests.test_chapter_filenames -v
python3 -m compileall -q core/chapter_utils.py training/adaptive_builder.py webui/draft_chat.py
# no remaining Chinese chapter-N constructions used as write names in adaptive_builder.py or draft_chat.py
```
