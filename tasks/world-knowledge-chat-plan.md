# Target-world chat plan

Goal: in the "Build the target-world knowledge base" step, create and refine the whole
target world through chat, next to the existing source upload.

## Decisions (Robin, 2026-09-19)

- Chat edits the 7 final section files in `world_knowledge/worlds/_final` directly.
- Upload and chat coexist; chat can start from nothing or refine an upload-built world.
- Always two model steps per message (one code path); co-lead preferred single-call for small worlds.
- Rules live in built-in prompts plus an optional per-workspace custom guide file.
- Empty world: interview first, write sections once there is enough, list what is missing.

## Design

One chat message = two model steps, both in `core/` so CLI/Web share the workflow:

1. **Plan step** (`core/prompts/world_chat_plan`): sees guidelines, custom guide, a compacted
   view of all 7 sections, recent turns, and the message. Returns a `# Reply` (answer,
   follow-up questions, "still missing" list) and a `# Section changes` list: section name +
   change instruction. Writes nothing, so a truncated view is safe.
2. **Section step** (`core/prompts/world_chat_section`), once per changed section: sees the
   full current section text + the instruction, returns the full replacement section.

Section step also receives the custom guide, the user's message and a compacted view of the
other sections, and is told to preserve everything the instruction does not address.

Write contract (one message = one transaction): all section replacements are generated and
validated in memory first; only then are the changed section files written, together with
`chat_edited`. A stop, or a failure on any section, writes nothing. Sections the chat did not
name are never touched (own writer, not `_write_sections_to_final`, which rewrites all 7).

Guide: `file_system/world_knowledge/chat_guide.md`; appended after the built-in rules and
wins on conflict; reset deletes the file.

Readiness: `ready` stays "all 7 final files exist" (a `None` body already counts today). The
first chat write creates all 7 files, so a partial chat world is injected downstream like a
partial upload world is. The enable toggle must show for chat-only worlds (no sources).

Safety rules:
- Unknown section names are dropped; only the 7 `WORLD_SECTIONS` are writable.
- Empty/invalid model output writes nothing and raises (existing `_run_prompt` behaviour).
- A meaningful section is never replaced by an empty/"None" body.
- Manifest gets `chat_edited: true` on first chat write.
- Import vs chat edits (decided: rebuild replaces, with backup + confirm): before a build
  overwrites `_final` while `chat_edited` is set, core copies `_final` to
  `worlds/_final_backup_<timestamp>` and clears the flag; Web asks for confirmation first.
  A non-force rebuild with unchanged sources
  already skips `_final` (chat files are newer), but a new source, `--force` or merge-only
  overwrites it (`core/world_knowledge.py:1008`, `:1168`). Policy lives in core so CLI and
  Web behave the same; the Web confirmation is only its UI.
- Chat and world import/build tasks are mutually exclusive per workspace (task creation at
  `webui/app.py:798` currently bypasses chat guards).

## Units

- [x] **U1 core** — `core/world_chat.py` (apply message, guide load/save/reset, response
      parsing), two prompt templates, small public section read/write helpers and the
      backup-before-overwrite in `core/world_knowledge.py`. Tests: `tests/test_world_chat.py`
      with a fake LLM (interview turn writes nothing; change turn rewrites only named
      sections; empty output preserves files; unknown section ignored; backup on rebuild).
- [x] **U2 web backend** — `webui/world_chat.py` manager following `arc_chat.py`
      (conversation.json under `world_knowledge/`, background job, stop, clear), routes in
      `webui/app.py` (`/world-knowledge/chat|job|conversation|prompts|stop|guide`), runtime
      wiring incl. busy/forget guards, `chat_edited` in the task_runner summary. Tests for
      manager + route contracts, offline.
- [x] **U3 frontend + docs** — chat panel in the world step (new `webui/static/world-chat.js`,
      loaded from `index.html`; `wizard-v0.js` gets the mount hook, toggle/readiness for chat-only worlds and step copy), guide upload/reset,
      rebuild confirmation, `world` step copy no longer says upload-only, README section.
      Packaging test must still pass for the new static file.

Order: U1 → U2 → U3, shared tree, serial (U2/U3 touch files with Robin's uncommitted work).

## Definition of done

- `python -m unittest discover -s tests -v` green (incl. `test_no_cjk`, packaging, web parity).
- `python -m compileall -q core training webui novel_cli.py` and `python novel_cli.py --help` clean.
- Deterministic scenario test with a scripted fake LLM: interview turn (no writes) → write
  turn (2 sections) → refine turn (1 section, others byte-identical) → failing turn (no
  writes) → status ready.
- Build contract tests: unchanged non-force skip keeps chat edits; new source / force /
  merge-only follow the chosen policy.
- Manual smoke with scratch `HARNESS_NOVEL_HOME`: empty workspace → chat interview →
  sections appear in review → world step shows ready.
- No new file over 300 lines; no CJK literals; existing uploads flow unchanged.

## Out of scope

- A `world-chat` CLI command (core function makes it a small follow-up).
- Splitting the already oversized `wizard-v0.js` / `world_knowledge.py`.

## Review

- Engine grok (3 units, rework rounds: U1 x3, U3 x2); co-lead Codex gpt-6-astra ran all touchpoints.
- Write contract as built: all replacements are generated and validated in memory, then
  `chat_edited` is set, then section files are written. There is no rollback for a disk
  failure in the middle of the file writes (flag-first keeps protection from lagging).
- Stop is cooperative between model calls.
- Open reservation (co-lead dissent): chat vs world import/build exclusion is check-then-start,
  so two simultaneous requests (e.g. two tabs) can both pass. Needs a shared reservation to close.
- Inherited, not fixed here (same pattern in all chat managers): workspace-root switch does not
  check running chats or drop conversation caches; a failure in conv.save()/thread start
  strands a registered job.
- Full suite: 183 tests, 5 failures identical to a clean HEAD checkout (packaging: no
  setuptools; prompt_trace_privacy x3; story_arc_context).
- Not verified: live model round-trip; rebuild confirm dialog and guide upload in a browser.
