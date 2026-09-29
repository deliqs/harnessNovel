# Story-arc pipeline split: ledger, writer, checker, targeted refine

Robin's report (2026-09-29): the arcs step generates near-identical arcs that do not progress the
story, and "rewrite Arc 2" left Arc 2 alone and rewrote Arcs 3 and 4. His proposal: split the
orchestrator's work over smaller, single-purpose subagents.

## Diagnosis

The orchestrator (`webui/orchestrator/`) is a thin router: `arcs_generate` starts one background
job and reads back a 1500-character note when it ends. All the story reasoning happens below it,
in `training/adaptive_builder.py`, as one big prompt per arc. That is where the defects are.

### A. Duplicate arcs (initial generation, `gen_story_arcs`)

Evidence: `projects/Robin/file_system/story_arcs/vol_01/`. Arcs 2, 3 and 4 all open "with the
immediate aftermath of the First Showing at the beach" and all close on school entry or the
Age-8 scene. Arc 4's boundary paragraph is Arc 2's.

1. Every arc gets the same whole-stage input: long mainline, previous stage, the full current
   stage, the reference digest, the whole-stage plan. Prior arcs reach the prompt only through
   `build_story_context`'s "Prior generated arcs" section, budgeted at 2600 chars with the middle
   cut out (`compact_text`). By arc 3 that is the head of arc 1 plus the tail of arc 2.
2. `core/prompts/novel_story_arc/prompt.txt` never says what to do with prior arcs. Rule 2 says
   "pick up the previous stage's ending state"; nothing says "start where arc N-1's next bind
   ends" or "do not re-stage beats already spent".
3. Nothing checks a new arc against its siblings. `diagnose_story_arc` only compares phrase
   similarity against the reference sample.
4. The author's first message is dropped. `ArcsChatManager.start_message` records it in the
   conversation, then calls `gen_story_arcs(ws, volume=volume, ...)` without it. Robin's four-arc
   brief with the act mapping never reached the model.

### B. "Rewrite Arc 2" edited 3 and 4 (`refine_story_arcs_serial`)

1. Refinement is always "route to the earliest affected arc, then regenerate every arc from there
   to the end". A router LLM call picks `start_arc`; there is no way to name one arc. The 12:57
   run needed "Refine ONLY Arc 4 ... Do not change Arcs 1, 2, or 3" to behave.
2. An arc whose output fails `diagnose_story_arc` (heading, length window, missing field) is
   skipped with a `print()` and the loop continues. The note still counts it. Evidence: the 12:06
   run's note reads "started at story-arc unit 4, finished 0/1" and nobody was told why. With
   start 2, a rejected Arc 2 followed by accepted 3 and 4 is exactly Robin's symptom.
3. When arc N is skipped, arc N+1 is regenerated with `previous_story_arc` set to "(this is the
   first story-arc unit of the current volume)", so the cascade also destroys continuity.

### C. What the orchestrator can and cannot see

The job result the orchestrator reads (`job_status` → `result`) is the tail of the last note. It
has no per-arc outcome, so it cannot tell the author an arc was rejected. `arcs_generate` is one
tool for both generation and refinement, with no `arc` or `mode` argument.

### Verdict on "the model is not intelligent enough"

Partly. A 27B model given a 20k-character prompt of the whole stage, and asked to infer which
slice is its own, will re-summarise the stage. The fix is the one Robin proposed, applied one
layer down: narrow inputs per role, explicit state handed between roles, deterministic checks
between them. The orchestrator itself stays a router.

## Design

Four roles inside the arcs pipeline. No new LLM roles except an optional retry; the ledger and
the checker are deterministic so the small model is not asked to judge itself.

1. **Ledger** (deterministic). After each arc is written, extract its state-carrying fields
   ("Gains and costs", "Character and relationship change", "Foreshadowing and next bind", plus the
   heading and "Boundary reason") into a compact record. The writer prompt gets the ledger of all
   prior arcs instead of truncated raw arcs. Lives in a new module, not in `adaptive_builder.py`.
2. **Writer** (existing prompt, narrowed). The prompt gets: the ledger, this arc's own obligations
   and beat slots, the author's brief, and explicit rules: start from arc N-1's "next bind"; do not
   re-stage any beat, event or boundary listed in the ledger; the opening event must differ from
   every prior arc's opening event.
3. **Checker** (deterministic). Before writing, compare the candidate against each sibling arc with
   `phrase_similarity` and an opening-event check on "Boundary reason". Over threshold: one retry
   with a "distinct from arc K, which already covers ..." note. Still over: write it, but record
   the warning. Every rejection or skip is recorded per arc.
4. **Targeted refine.** `refine_story_arcs_serial` takes an explicit `arc` and a `cascade` flag.
   The router runs only when no arc is named. A skipped arc stops the cascade instead of feeding a
   placeholder to the next one. Per-arc outcomes go into the result and the note.

The orchestrator gets the vocabulary to use this: `arcs_refine(message, arc, mode, cascade)` next to
`arcs_generate`, and `job_status`'s result shows the per-arc outcome list.

## Units

- [ ] **U1 Ledger + writer context** (`training/story_arc_ledger.py`, new; `training/story_context.py`;
  `core/prompts/novel_story_arc/prompt.txt`; `core/prompts/story_arc_serial_refine/prompt.txt`;
  `gen_story_arcs`/`_story_arc_prompt_context` in `adaptive_builder.py`; `ArcsChatManager.start_message`).
  - The ledger is extracted deterministically from the fields the arc format already has.
  - `build_story_context` renders the ledger in place of the raw "Prior generated arcs" text.
  - The author's message reaches `gen_story_arcs` as `creative_direction` and the prompt.
  - Prompt rules for continuing from the previous arc and not re-staging spent beats.
  - Done: `python -m unittest tests.test_story_arc_context tests.test_story_arc_ledger -v`, plus a
    test that the ledger for Robin-shaped arcs names each arc's opening event once.
- [ ] **U2 Checker + per-arc outcomes** (`training/story_arc_review.py`, new; `training/generation_quality.py`;
  `gen_story_arcs` and `refine_story_arcs_serial`).
  - Sibling similarity and opening-event checks, one retry, then accept-with-warning.
  - Every arc's outcome (`written`, `retried`, `rejected: <reason>`, `skipped`) is in the result
    dict as `outcomes` and rendered into `adjustment_note`.
  - A rejected arc ends a cascade rather than continuing with a placeholder previous arc.
  - Done: `python -m unittest tests.test_story_arc_review tests.test_adaptive_quality_integration -v`
    with a fake LLM that returns a duplicate on the first call.
- [ ] **U3 Targeted refine** (`refine_story_arcs_serial`, `_serial_refinement_targets`,
  `ArcsChatManager.start_message`, `webui/app.py` arcs route, `README.md`).
  - `arc` and `cascade` arguments; router only when `arc` is None; `mode` may be passed explicitly.
  - Done: `python -m unittest tests.test_orchestrator_tools_arcs tests.test_task_runner_commands -v`
    and a new test that "arc=2, cascade=False" touches only arc 2 and leaves 3 and 4 byte-identical.
- [ ] **U4 Orchestrator tools** (`webui/orchestrator/tools/arcs.py`, `tools/shared.py` job result,
  `tests/test_orchestrator_tools_arcs.py`).
  - `arcs_refine(message, arc=None, mode=None, cascade=False)`; `arcs_generate` is generation only.
  - INSTRUCTIONS tell the model to use `arcs_refine` with `arc` when the author names one, and to
    read the per-arc outcomes back to the author.
  - Done: `python -m unittest tests.test_orchestrator_tools_arcs tests.test_orchestrator_wiring -v`.

U1 and U2 touch `gen_story_arcs`; U2 and U3 touch `refine_story_arcs_serial`. Run U1 first, then
U2 and U3 in sequence in the shared tree, U4 last. Chapters and drafts have the same
silent-skip shape (`adaptive_builder.py:4863`, `:5070`); they are out of scope here and get their own plan.

## Definition of done

- `.venv/bin/python -m unittest discover -s tests` passes except the 5 failures already at HEAD
  (test_packaging_runtime_assets, 3 in test_prompt_trace_privacy, test_story_arc_context average_chars).
- `uvx ruff check --select C901 --config 'lint.mccabe.max-complexity=12'` clean on new and changed files;
  every new file 300 lines or fewer.
- A live check on Robin's workspace copy (scratch `HARNESS_NOVEL_HOME`): regenerate volume 1 and
  confirm the four arcs open on four different events; then `arcs_refine(arc=2)` and confirm 3 and 4
  are untouched.

## Review

(filled in after the work)

## Reconciled units (after co-lead critique, 2026-09-29)

Cut by function, not by concern, because `gen_story_arcs` (C901 29), `refine_story_arcs_serial` (30)
and `ArcsChatManager.start_message` (17) are all over the complexity limit at HEAD and two workers
splitting helpers out of the same function would fight.

- **UA** pure modules, no `adaptive_builder.py` edits: `training/story_arc_ledger.py` (field parse,
  tiered ledger render, author-brief render), `training/story_arc_review.py` (sibling opening-event
  review, retry note, outcomes render), `enrich_arc_plans` "part k of m" labels. Runs parallel with U4.
- **U4** orchestrator: `arcs_refine(message, arc, mode, cascade)` tool, `arcs_generate` generation-only,
  INSTRUCTIONS, `_job_result` returns the error of a failed job.
- **UB** `gen_story_arcs` + `novel_story_arc` prompt: author brief from the first user turn, ledger
  placeholder, continuity rule rendered per arc (rule 2 for arc 1 only), sibling check with one retry,
  rejection stops the run, outcomes in result and note.
- **UC** `refine_story_arcs_serial`, `_serial_refinement_targets`, `story_arc_serial_refine` and
  `story_arc_refine_route` prompts, `start_message`: `arc`/`mode`/`cascade`, router only when no
  arc, rejection stops the cascade, one retry on validation failure with the reason fed back, outcomes.

Contract: training-layer `cascade` defaults to today's behaviour (True); only `arcs_refine` defaults
to False. Outcomes are `[{"arc": int, "status": "written|kept|retried|rejected|skipped|no_output",
"reason": str}]`, rendered last in the note. The ledger is arc-only; `build_story_context` is shared
with chapters and drafts and is not changed. Live check on a real workspace needs Robin's OK.

Complexity gate: new functions ≤12; touched functions end ≤12 where extracting the per-arc body is a
natural part of the change, otherwise no higher than baseline and reported.

## Review (2026-09-29, after implementation)

Engine: claude (Opus 5.5 workers, `lead-colead` co-lead engaged for all three touchpoints).

What landed (13 files changed, 8 new; 528 tests, only the 5 pre-existing failures):

- `training/story_arc_ledger.py`: field parser, tiered prior-arc ledger (previous arc in full with its
  next bind, older arcs as one "opened on" line each), author brief with this arc's lines pinned first.
- `training/story_arc_review.py`: sibling review on the OPENING half of "Boundary reason" (4-gram phrase
  check = block, content-word overlap = warn, whole-text phrase = block), retry note with delimited
  sibling excerpt, per-arc outcomes.
- `enrich_arc_plans`: obligations shared by several arcs are labelled "(part k of m)".
- `gen_story_arcs`: author brief from the first user turn, ledger + per-arc continuity rule in the
  prompt (rule 2 "pick up the previous stage" for arc 1 only), one retry with the reason fed back,
  warn-only second failure written with a warning, block/validation second failure rejected and the run
  halted, `outcomes` in the result and rendered last in the note. Complexity 29 → 11.
- `refine_story_arcs_serial(..., arc, mode, cascade=True, author_brief)`: named arc skips the router,
  `cascade=False` touches one arc, rejection stops the cascade (no placeholder previous), outcomes.
  Complexity 30 → ≤12 per helper. Router rule 3 no longer starts at an arc mentioned as comparison.
- `ArcsChatManager.start_message(..., arc, mode, cascade)`; Web route validates them (400s);
  orchestrator gains `arcs_refine(message, arc, mode, cascade=False)`, `arcs_generate` is generation
  only, `job_status` returns a failed job's error, INSTRUCTIONS tell the model to report rejected arcs.

Co-lead findings that changed the plan: cut units by function not concern; obligations repeated
identically across arcs; rule 2 contradicted the new continuity rule; `cascade` default must stay
True in the training layer; the checker's first two versions flagged correct continuations (caught by
running the real function on prompt-shaped fixtures, twice, after per-unit review had passed).

Not verified: any live run. A 27B model following the new rules, the router following rule 3, and the
orchestrator actually passing `arc=2` all need a run on a scratch copy of a real workspace. The checker
thresholds (`CONTENT_OVERLAP_THRESHOLD=0.4`, ≥4 shared words) were set on synthetic fixtures only.

Follow-ups, not done here: chapters and drafts have the same silent-skip shape
(`gen_chapter_outlines_for_arc`, `gen_serial_chapters`); `ArcsChatManager.run_message` has no callers
and uses the old batch refine; `webui/arc_chat.py` (412) and `training/story_context.py` (342) were over
300 lines before this work.
