# Story-arcs timeouts (and the same class of CLI failures)

Engine: grok-4.6. Co-lead: no (single unit, localised timeout/context fix).

## Problem

`novel story-arcs` times out because each plot-unit LLM call is oversized, and the HTTP timeout treats a slow-but-alive completion as a dead request.

Two independent mechanisms stack:

1. **Unbounded prompt.** `_reference_volume_story_arcs_summary` concatenates *every* story-arc in the matching reference volume into `{reference_story_arcs}` for *every* generated unit (`training/adaptive_builder.py`). README already specifies the intended design: one matching reference arc as a format/rhythm sample, not a full-volume dump. `_plan_story_arcs_from_reference` already exists and is unused. `_simple_story_arc_context` is built once and reused for generate, refine, and serial refine.

2. **Total-request timeout.** `LLMProvider` passes a float `timeout=600` into the OpenAI client and does **not** stream (`core/llm_provider.py`). A float is a wall-clock cap for the whole response body. A 12-minute generation that is still emitting tokens fails at 600s. Timeouts are retried like generic errors (`max_retries=2`) so one hung call can block ~30 minutes. Exhausted `generate()` returns `""`; `gen_story_arcs` still writes that as a completed file.

`_reference_story_arc_average_chars` has no upper bound. Legacy `batch_*.md` fallbacks can be tens of thousands of characters; the prompt then asks for that much output, which will not finish inside the timeout.

Bumping `HARNESS_NOVEL_LLM_TIMEOUT` is not the fix.

## CLI audit (same problems, not a rewrite of every command)

| Command | Unbounded concat of many docs? | Empty result written? | Helped by streaming timeout? |
|---|---|---|---|
| `story-arcs` | **Yes — full matching-volume arcs every call** | Yes | Yes |
| `chapter-outlines` | No (one arc + previous chapter) | Yes | Yes |
| `write` | No (2 previous chapters + one arc + outline) | Yes | Yes |
| `init` / `reference-resume` (`volume_merge`, `novel_extract`) | Yes — all arc summaries / all volume outlines in one merge | Writes fallback, not empty | Yes |
| `world-build` | No — already chunked (`chunk_size=36000`) and `_aggregate_sections` / `_compact_world_document` cap | Raises, does not write | Yes |
| `design-concept` / `stage-design` / `novel-outline` | World knowledge already capped (60k/80k); stage design is one volume outline per stage with resume | Design path raises on empty | Yes |
| `volume-outline` | One reference volume outline + previous volume | Skips write on empty | Yes |
| `mechanics-init`, `novel-name-synopsis`, `stage-insert` | Bounded design inputs | Design path raises | Yes |

Shared LLM timeout/streaming is the all-commands fix. Per-call dump-all is unique to story-arcs (plus the init merge prompts, which get a `max_chars` cap in the same unit). Do not restore `arc_context_extract` as an extra LLM call — it was removed on purpose and would itself time out if fed the same dump.

## Approach

### A. `LLMProvider` — idle timeout, not wall-clock

- Stream `chat.completions.create`. Assemble the text from delta chunks.
- Pass `httpx.Timeout(connect=…, read=self.timeout, write=…, pool=…)` so `HARNESS_NOVEL_LLM_TIMEOUT` is time-between-tokens, not total request time. Keep the 600s default and the 30s floor.
- `generate_cancelable` must stream too (it currently creates a fresh client per attempt).
- Keep retrying timeouts (they can be transient) but log them as timeouts, not generic failures.
- If a provider rejects `stream=True`, fall back to a non-stream call for that attempt only.
- Do not change the empty-string contract of `generate()` for auth/config failures.

### B. Story-arcs — one matching sample, capped output, no empty files

- `gen_story_arcs` uses `_plan_story_arcs_from_reference` when the matching reference volume has arcs; otherwise keep `_plan_story_arcs`.
- Each generate call puts **that unit's matching reference arc(s)** into `{reference_story_arcs}`. Shared `_simple_story_arc_context` may keep a short volume index (id + chapter range, no bodies) so refine still sees structure.
- Cap `_reference_story_arc_average_chars` (suggested 2000). Fallback 1000 stays.
- If generation returns empty, do **not** write the file and do **not** mark the unit complete so resume retries it.
- Same empty-write skip in `gen_serial_chapter_outlines` and `gen_serial_chapters` (the CLI siblings that currently write empty on timeout).

### C. Init merge cap

- `volume_merge` / `novel_extract` concatenations in `training/outline_builder.py` get a `max_chars` cap in the same style as `_unused_reference_arcs` (26000). Truncate with an explicit note rather than sending an unbounded join.

## Constraints

- Smallest reasonable change. Do not rewrite `adaptive_builder.py` or `outline_builder.py`.
- Do not split those files. Do not touch unrelated localization hunks already in the tree.
- New files ≤ 300 lines. `core/llm_provider.py` must stay ≤ 300.
- No `improved` / `new` / `enhanced` in names.
- No git commands.
- Tests first, no live API.

## Tests (must exist before the production change, or in the same unit with the failing test written first)

- `tests/test_llm_provider.py` — mock OpenAI client: streaming assembles content; timeout object has a `read` of `HARNESS_NOVEL_LLM_TIMEOUT`; a stream-not-supported error falls back.
- `tests/test_story_arc_context.py` — tempfile workspace with several long reference arcs: generation context for arc 2 contains arc 2's body and does **not** contain arc 1/3 bodies; average chars is capped; empty generate does not create `arc_*.md`.

## Done when

```
python3 -m unittest tests.test_llm_provider tests.test_story_arc_context -v
```

is green, and `git diff` only contains the timeout/context/empty-write/merge-cap changes plus the new tests.

## Results

- `python3 -m unittest tests.test_llm_provider tests.test_story_arc_context -v` — 9 tests, OK.
- Engine: grok-4.6. Co-lead: no (single unit). Grok worker CLI with `bypassPermissions` was blocked; the same change was implemented in this session.
- Not verified: a live `novel story-arcs` run against Robin's API (no workspace/API in this environment).
