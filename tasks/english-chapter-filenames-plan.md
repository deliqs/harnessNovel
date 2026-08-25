# English chapter draft filenames

Engine: grok-4.6. Co-lead: no (single unit, localized filename bug).

## Problem

`novel write` still writes Chinese draft filenames:

- `file_system/chapters/vol_01/001_第1章.md`
- `file_system/drafts/vol_01/raw_chapters/001_第1章.raw.md`

Chapter outlines already emit English (`chapter_001.md`). Drafts/raw backups did not get the same treatment.

## Canonical emit

| Kind | English (write) | Chinese alias (read/delete only) |
|---|---|---|
| Draft | `{n:03d}_chapter_{n}.md` | `{n:03d}_第{n}章.md` |
| Raw backup | `{n:03d}_chapter_{n}.raw.md` | `{n:03d}_第{n}章.raw.md` |
| Raw version | `{n:03d}_chapter_{n}_{stamp}.raw.md` | `{n:03d}_第{n}章_{stamp}.raw.md` |
| Draft version | `{basename}_{stamp}` of the file being backed up | same for leftover Chinese files |

## Behavior

- **New writes** always use the English name.
- **Reads / exists / skip / resume** resolve English first, then the Chinese alias, so old workspaces still work.
- **Overwrite** (regenerate / humanize / new write to the canonical path) writes English; if a Chinese sibling exists, delete it after a successful write so the directory does not contain both.
- **Reset/delete** (draft_chat) removes English and Chinese names plus both version globs.
- Wizard `chapterNumberFromPath` already parses `001_chapter_1.md` via the leading-digits regex; keep the Chinese filename regex as an alias.

## Unit 1 — Path helpers + call sites

Owner files: `core/chapter_utils.py`, `training/adaptive_builder.py`, `webui/draft_chat.py`, `docs/cli_io_mindmap.html`, `tests/test_chapter_filenames.py`.

Do not split `adaptive_builder.py`. Do not rewrite `_fix_chapter_numbering` (reference-novel titles). Do not change outline filenames (`chapter_001.md`). Do not migrate frozen world-knowledge names. Do not run git. Do not translate unrelated CJK.

Tests first; keep going until the done check is green.

## Done check

```bash
python3 -m unittest tests.test_chapter_filenames -v
python3 -m compileall -q core/chapter_utils.py training/adaptive_builder.py webui/draft_chat.py
# no remaining f"...第{...}章.md" constructions used as write names in adaptive_builder.py or draft_chat.py
```
