# Remove the Chinese layer (English-only)

Engine: grok-4.6. Co-lead: yes (cross-cutting; parser/compat risk).
Co-lead plan critique reconciled 2026-08-24 (session `51d890f8-61cb-4f78-b4cf-873d06466c44`). Shape kept (serial shared tree, Chinese parse aliases, preserve dirty hunk, no file splits). Contracts expanded before briefing.

Historical plan. Not current source of truth. Later zero-CJK work dropped Chinese aliases for our generated artifacts; user-import parsers may still match CJK via unicode escapes.

## Problem

This checkout is a partial English fork of harnessNovel:

- `HARNESS_NOVEL_LANG` already defaults to `en`.
- Every prompt folder has both `prompt.txt` (Chinese) and `prompt.en.txt` (English).
- `core/system_prompt.md` is Chinese; `core/system_prompt.en.md` is English.
- `README.md` is Chinese; `README_EN.md` is English and still says "CLI and web UI strings remain Chinese".
- ~78k CJK characters remain across 98 files. Live wizard: `webui/static/wizard-v0.js` + `index.html` (`app.js` unused leftover).
- Some parsers already accept English (`stage` alongside the Chinese Stage token). Others are Chinese-only and already fail under the English default (see Unit 2a list).

Uncommitted local change that **must be preserved**:
`training/adaptive_builder.py` `gen_novel_name_synopsis` falls back to `core_gameplay.md` when `rough_outline.md` is missing.

## Hard constraints (all units)

- **Stage ≠ Phase.** `Stage` is the volume-instance (`# Stage N:`). `Phase` is the book-structure unit (`## Phase N:`). Never add `stage` as an alias for Phase.
- New files **emit English glossary forms only**. The original freeze kept Chinese in regex/alias lists so old workspaces, Chinese reference deconstruction, and Chinese sample-novel import still parsed.
- **World-knowledge filenames.** The original freeze kept Chinese on-disk keys (`WORLD_SECTIONS`) and mapped English headings onto those keys via `_HEADING_ALIASES`. Later work made English the stored form.
- Do not rewrite `training/adaptive_builder.py` or `wizard-v0.js` from scratch. Do not split files. Do not revert the `core_gameplay.md` fallback. Do not rename ASCII workspace paths (`story_arcs/`, `stage_outline.md`, `vol_01_…`). Do not translate `DEFAULT_FORBIDDEN_TERMS`. Do not remove the author WeChat block. Do not keep `HARNESS_NOVEL_LANG=zh`. Do not run git. Do not translate `LICENSE`. Leave `docs/web-ui-*.png` binaries.

## Canonical glossary (workers must not invent synonyms)

| Concept | Emit (English) | Historical parse (Chinese / old English) |
|---|---|---|
| Volume-instance | `# Stage N: Name` | Chinese Stage heading |
| Book-structure phase | `## Phase N: Name` | Chinese Phase heading / Nth-phase form |
| Phase outline doc | `# Phase outline` | Chinese phase-outline heading |
| Worldview / rough outline / long mainline / stage roadmap | Worldview / Rough outline / Long mainline / Stage roadmap | Chinese document titles for those four |
| Story arc title | `【Arc{n}: Chapters {a}-{b} \| title】` | Chinese plot-unit title marker |
| Volume-style sections | `Volume overview`, `Three-act structure`, `Character roster`, `Foreshadowing tracker`, `Core payoff` | Chinese volume-style section titles. Also accept old-English `Volume-outline overview`, `Foreshadowing tracking`, `Core payoffs` as parse aliases only. **Prompts must emit the glossary forms**, including `volume_merge` and `adaptive_volume_outline`. |
| Planned chapters | `Planned chapters` | Chinese planned-chapter count label |
| Incremental phase-outline op | `adjust last phase` / `add phase` | Chinese adjust-last-phase / add-phase ops |
| Final-window / use-reference interpolation | `yes` / `no` | Chinese yes/no tokens |
| Source-role interpolation | `primary source` / `supplement source` | Chinese primary/supplement labels |
| Virtual volume line | `Volume {n}: name \| Chapters {a}-{b}` | Chinese volume N / chapters a-b line |
| Worldview map heading | `# 6. Maps / stage layers` plus `- Layer N:` | then-current Chinese map heading. Do **not** AND `map`∧`stage`∧`region`∧`layer`. |
| Empty-asset prefix | `(not generated` | Chinese not-generated prefix — detector must accept both |
| Fake design-field marker | `Model did not return` / `please retry or fill in manually` | Chinese model-did-not-return / please-retry markers — keep detector in sync with `_normalize_design_field` |
| Chat generic commands | `Generate`, `Regenerate`, `Continue generating`, `Adjust`, `Optimize` | Chinese generate / regenerate / continue / adjust / optimize commands |
| Regenerating intent | `regenerate`, `rewrite`, `start over`, `full rewrite` | Chinese regenerate / full-rewrite / start-over intents |
| Extend-stage intent | `extend`, `continue`, `add stage`, `append stage`, `next stage`, `new stage` | Chinese continue-writing / add / append-stage intents |
| System-panel example keys | `Name`, `Identity`, `Race`, `Realm`, `Cultivation`, `Level`, `Talent`, `Jing-Qi-Shen`, `Skills`, `Arts`, `Equipment`, `Resources`, `Special abilities`, `Current state`; nested skill: `name`, `stage`, `note`; `Killing aura` | Chinese keys in old JSON still accepted if the validator is key-agnostic |
| Sample-novel markers | `[Title]` / `[Synopsis]` if newly emitted | Chinese title / synopsis markers |
| Creative-direction headings (if wizard template is translated) | pin in Unit 3; Unit 2 `_load_creative_direction` must strip both English and the then-current Chinese list at `adaptive_builder.py:192` | Chinese creative-direction / genre-positioning headings |
| World sections (prompt headings) | copy `_HEADING_ALIASES` in `core/world_knowledge.py` | original freeze kept Chinese world-section filenames |
| Canon index | same alias map | original freeze kept Chinese keys |

## Units

Shared tree, **serial**. Do not worktree: the dirty `core_gameplay.md` fallback exists only in this working tree; Unit 2a must see Unit 1’s `{operation}` strings; Unit 3 must paste Unit 2’s Phase/Stage regexes.

### Unit 1 — Collapse the prompt layer

Owner files: `core/prompts/**`, `core/prompt_loader.py`, `core/system_prompt.md`, `core/system_prompt.en.md`, `setup.py` **only** the `package_data` tuple.

- Replace each `prompt.txt` with that folder’s `prompt.en.txt`, then delete `prompt.en.txt`.
- Delete `_EN_OUTPUT_CONTRACT`. All 59 English templates already contain `Write the entire response in English`. Do not append a second language block.
- `PromptLoader.load` reads only `prompts/<name>/prompt.txt`. Drop `_LANG` / `HARNESS_NOVEL_LANG`.
- `system_prompt.en.md` overwrites `system_prompt.md`; delete the `.en` file.
- `setup.py` package_data must become exactly:
  `"core": ["prompts/*/prompt.txt"], "webui": ["static/*"]`
  Do not touch `description` / `long_description` (Unit 3).
- Translate leftover CJK in templates and **align schema tokens to the glossary**, including:
  - `chapter_system_panel`: English example keys as in the glossary.
  - `design_stage_outline_incremental`: `{operation}` values `adjust last phase` / `add phase`.
  - `reference_segment_extract`: `yes` / `no` (may mention Chinese as parse hint only).
  - `world_knowledge_*`: `primary source` / `supplement source`.
  - `volume_merge` and `adaptive_volume_outline`: `Volume overview`, `Foreshadowing tracker`, `Core payoff` — not `Volume-outline overview` / `Foreshadowing tracking`.
- Prompt proper nouns such as Seven-Treasure Tree may stay (ASCII).

Done check:

```bash
test $(find core/prompts -name 'prompt.en.txt' | wc -l | tr -d ' ') = 0
test $(find core/prompts -name 'prompt.txt' | wc -l | tr -d ' ') = 59
test ! -f core/system_prompt.en.md
python3 -c "
from core.prompt_loader import PromptLoader
import inspect, core.prompt_loader as pl
src = inspect.getsource(pl)
assert 'HARNESS_NOVEL_LANG' not in src
assert '_EN_OUTPUT_CONTRACT' not in src
t = PromptLoader.load('design_stage_outline', worldview='w', rough_outline='r', reference_volume_structures='v', reference_volume_count=1, stage_range='1')
assert 'Phase outline' in t
inc = open('core/prompts/design_stage_outline_incremental/prompt.txt', encoding='utf-8').read()
assert 'adjust last phase' in inc and 'add phase' in inc
vm = open('core/prompts/volume_merge/prompt.txt', encoding='utf-8').read()
assert 'Volume overview' in vm and 'Foreshadowing tracker' in vm
assert 'Volume-outline overview' not in vm
"
```

### Unit 2 — Engine Python (core + training)

Owner files: `core/*.py` except `prompt_loader.py`, `training/*.py`.

**Tests first. Do not start comment/print translation until `python3 -m unittest tests.test_heading_parsers -v` is green.**

#### 2a — emit/parse contracts

Add `tests/test_heading_parsers.py` covering (must fail on today’s tree, then pass):

- `# Stage 1: Foo` matches stage regex; does **not** match phase regex.
- `## Phase 2: Bar` matches phase 2 (original freeze also asserted the Chinese Phase heading).
- `_normalize_stage_roadmap` emits `# Stage N:` (original freeze also parsed the Chinese Stage heading).
- `_remove_stage_outline_section` drops `# Phase outline` (original freeze also dropped the Chinese phase-outline heading).
- `_design_structure_counts` on a stub whose worldview heading is exactly `# 6. Maps / stage layers` plus `- Layer 1: …`.
- `_is_volume_style_stage` true for the English five-section glossary block + `Planned chapters: 12`.
- `_parse_virtual_volumes("Volume 1: The Lock | Chapters 1-78")` (original freeze also parsed the Chinese volume/chapters line).
- English `# Stage {n}:` passes the extend gate currently at `adaptive_builder.py:707`.
- `gen_novel_name_synopsis` still reads `core_gameplay.md` when `rough_outline.md` is missing (dirty hunk).
- Operation constants equal the strings in `design_stage_outline_incremental/prompt.txt`.
- `ARC_HEADER_RE` matches `【Arc1: Chapters 1-5 | The Hook】` (function lives in `outline_builder.py`).
- `_reference_volume_structure_context` / `_reference_volume_stage_structure` resolve `Volume overview` (original freeze had a Chinese fallback).

Parser/interpolation sites that must be patched (not only the original four):

- `STAGE_OUTLINE_HEADING_RE`, `_design_structure_counts`, `_remove_stage_outline_section`
- `adaptive_builder.py:707` Chinese-only Stage heading gate after `_normalize_stage_roadmap`
- `re.findall` on the Chinese Stage token (~2399) also `stage`
- `_parse_virtual_volumes` (`outline_builder.py:580`)
- `context_manager.extract_relevant_volume_outline` Chinese numbered volume-outline slices
- `use_reference` yes/no tokens (~690), `is_final_window` in outline_builder/reference_analyzer
- `role_label` primary/supplement (`world_knowledge.py`)
- `assets[name]` empty-asset prefix (~655)
- `_is_real_design_field` / model-did-not-return placeholder
- `_load_creative_direction` heading strip list
- `generic_instruction` set and regenerate markers (~2409, 4103–4108, 4713)
- `sections.get` volume-overview / three-act-structure keys, English-first
- `_CHAPTER_FORBIDDEN_STYLE_PATTERNS`: keep as Chinese-leak detectors **and** add English equivalents (`not X but Y`, `not only X but also Y`)
- `TERM_EXTRACT_STOPWORDS`: add English (`chapter outline`, `draft`, `volume outline`, …) and keep Chinese
- Empty placeholders interpolated into prompts (missing world-knowledge / extra-direction stubs, context-manager layer labels)

Remove remaining `HARNESS_NOVEL_LANG` branches; `_normalize_stage_roadmap` always emits `# Stage N: name`.

Do **not** change `WORLD_SECTIONS` keys or `_section_file_name` output in this unit. Do **not** “fix” `text_encoding._COMMON_CJK` (GBK vs Big5 scorer).

Preserve the `core_gameplay.md` fallback.

#### 2b — remaining Python copy (same worker, after 2a tests are green)

Translate comments, docstrings, prints, exceptions, progress callbacks in owner files. Match surrounding style; no drive-by cleanups.

Done check:

```bash
python3 -m compileall -q core training
python3 -m unittest tests.test_heading_parsers -v
# no HARNESS_NOVEL_LANG left in *.py
# tests include at least one Chinese-alias case per heading family
```

`story_arc_title` is **Unit 3** (`webui/task_runner.py`). Do not claim it in this unit’s tests.

### Unit 3 — Surfaces (CLI, web, docs)

Owner files: `novel_cli.py`, `webui/**`, `.env.example`, `README.md`, `README_EN.md`, `docs/cli_io_mindmap.html`, `setup.py` (name/description/long_description only — `package_data` already done).

- Translate CLI help **including every subparser**.
- Translate FastAPI errors, task labels, chat notes, progress, wizard copy, `index.html` (`lang="en"`).
- `_EXTEND_KEYWORDS` English primary + Chinese aliases (glossary).
- Paste Unit 2’s exact Phase/Stage regexes into `webui/design_chat.py` (`_design_files_exist`, `_stage_resume_status`) and `webui/task_runner.py` summary — do not re-invent.
- `story_arc_title` must extract from `【Arc1: Chapters 1-5 | The Hook】` without requiring the Chinese plot-unit token.
- Wizard `worldDescriptions` keys: original freeze kept Chinese world-section filenames; labels/descriptions may be English.
- `WORKSPACE_NAME_RE` may still allow CJK in workspace names. Error text English.
- README: current English README, no switcher, no `README_EN.md`, no `HARNESS_NOVEL_LANG` sentence, no “UI remains Chinese”.
- Bump `wizard-v0.js` / css `?v=` in `index.html`.
- Translate `docs/cli_io_mindmap.html` (`lang="en"`).
- Add tests: `story_arc_title` English arc title; design_chat/task_runner Phase heading counts.

Done check:

```bash
python3 -m compileall -q novel_cli.py webui
python3 novel_cli.py --help
python3 novel_cli.py design-concept --help
python3 novel_cli.py stage-design --help
python3 novel_cli.py write --help
test ! -f README_EN.md
```

Lead will also smoke the wizard chrome in the browser (no LLM): `novel web`, confirm English `WIZARD_STEPS`, settings, toasts, `lang="en"`.

## Verification (lead, after all units)

Allowlist CJK (everything else in `*.py`, `wizard-v0.js`, `index.html`, `README.md`, `.env.example`, `docs/cli_io_mindmap.html` is a failure):

1. Heading/keyword alias string literals (Stage/Phase/plot-unit/volume-section/yes/no tokens — later escaped or deleted)
2. `DEFAULT_FORBIDDEN_TERMS` + `TERM_EXTRACT_STOPWORDS`
3. `text_encoding._COMMON_CJK`
4. Tests asserting Chinese aliases
5. Author proper noun (later ASCII `Fei Niao`)
6. Frozen on-disk names in `WORLD_SECTIONS` / wizard `worldDescriptions` keys (later English)
7. Prompt proper nouns (later ASCII Seven-Treasure Tree)
8. CSS comments optional

```bash
python3 -m compileall -q core training webui novel_cli.py
python3 -m unittest tests.test_heading_parsers -v
python3 novel_cli.py --help
python3 -c "from core.prompt_loader import PromptLoader; print(PromptLoader.load('adaptive_drafting')[:200])"
```

No live LLM run required. Browser chrome smoke after Unit 3.

## Out of scope

File splits, screenshot reshoots, translating user novel content, keeping a zh mode, migrating world-knowledge filenames, changing workspace directory layout.
