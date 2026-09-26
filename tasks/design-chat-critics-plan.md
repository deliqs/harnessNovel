# Design chat: parallel critics fan-out

## Why
A review-type question ("what is missing, what should improve?") is answered today by one model call that carries every design file. Robin wants several focused critics to read the files in parallel, and a merge step that only sees their findings, so the merging context stays clean and the result is a list of points he can iterate over.

## Decisions (Robin, 2026-09-19)
- Trigger: the existing routing call gets a third outcome, `critique`. `question` and `change` keep their behaviour.
- Lenses: a built-in set plus an optional per-workspace lens file that adds or replaces lenses.
- Iterating: the merged critique is a numbered list saved to a file. "Apply 2 and 4" injects the full text of those points into ONE rewrite instruction.
- Concurrency: two critic calls at a time by default, configurable through an env setting.

## Design
- Router (`design_chat_route`) returns `{"mode": "question|critique|change", "points": [2, 4]}`. It still sees only the message. `points` is only meaningful for `change`.
- `training/design_critique.py`
  - Built-in lenses: depth, engagement, intensity and pacing, internal consistency, character stakes.
  - Lens file: `file_system/story_design/critic_lenses.md`. Each `## Name` heading is one lens, the text under it is its focus. A name that matches a built-in lens replaces it; other names are added. load / save / reset / status helpers.
  - `run_critique(ws, scope, message, progress_callback, stop_event)`: one `design_critic` call per lens through a thread pool capped by `HARNESS_NOVEL_CRITIC_WORKERS` (default 2, max 8). Each pool task runs inside a copied `contextvars` context so prompt tracing keeps working. A failed critic is skipped and named in the result; all critics failing raises.
  - Merge: one `design_critique_merge` call that receives the findings only, never the design files. It dedupes and returns ordered points `{title, detail, lenses}`.
  - Points are numbered from 1 and saved to `file_system/story_design/critique_points.json` (replaced by each critique). The chat answer is the numbered list as markdown.
  - `points_instruction(ws, numbers)`: returns the text block for the saved points with those numbers; unknown numbers are reported, not invented.
- `webui/design_chat.py`: `critique` path next to the `question` path (no file writes, no backups, no revision mark, no creative-direction record). Progress total is lenses + 1. On `change` with `points`, the block from `points_instruction` is appended to the model instruction; the chat history keeps the user's short message. Job completion message "Critique ready".
- Web: lens-file routes under `/api/workspaces/{name}/design/lenses` (GET status, POST save from upload, DELETE reset), and a lens bar in the design chat panel that mirrors the world-chat guide bar. Toast "Critique ready." for mode `critique`.

## Units
- [x] U5 core: `training/design_critique.py`, prompts `design_critic` and `design_critique_merge`, router prompt + `route_design_message` returning mode and points, tests.
- [x] U6 web: `design_chat.py` critique path and point injection, `app.py` lens routes, frontend lens bar and toast, README, tests. Depends on U5.

## Definition of done
- Offline tests for both units pass, plus `tests.test_design_chat_question`, `tests.test_design_chat_failure`, `tests.test_no_cjk`, `tests.test_webui_headings`.
- Full suite shows only the 5 known pre-existing failures.
- A critique never writes or backs up a design file (asserted byte-for-byte in tests).
- The merge prompt never contains design-file text (asserted in tests).

## Review
- Engine: Grok (grok-4.6), two serial units in the shared tree, no co-lead.
- U5 review sent back once: points made per scope (`critique_points_<scope>.json`), lens code split into `training/design_lenses.py`, lens tests split out, answer markdown simplified for the chat renderer.
- U6: the inline brief was refused twice by Grok's API (403 permission-denied on the first call); passing the brief as a file path worked unchanged.
- Verified by the lead: unit tests for both units pass; `node --check` passes on `wizard-v0.js` and `design-lenses.js`; full suite 216 tests with only the 5 known pre-existing failures (packaging, 3 prompt-trace privacy, story-arc average chars).
- Not verified: a live model round-trip, the lens bar and critique rendering in a browser, behaviour of a local model under two parallel calls.
- Known cosmetic gap: during a critique the progress line still says "design items" / "stages" next to the lens count.
- Stop is cooperative: pending critics are cancelled, a critic call already in flight finishes first.
