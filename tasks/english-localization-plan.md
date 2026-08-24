# Remove the Chinese layer (English-only)

Engine: grok-4.6. Co-lead: yes (cross-cutting; parser/compat risk).
Co-lead plan critique reconciled 2026-08-24 (session `51d890f8-61cb-4f78-b4cf-873d06466c44`). Shape kept (serial shared tree, Chinese parse aliases, preserve dirty hunk, no file splits). Contracts expanded before briefing.

## Problem

This checkout is a partial English fork of harnessNovel:

- `HARNESS_NOVEL_LANG` already defaults to `en`.
- Every prompt folder has both `prompt.txt` (Chinese) and `prompt.en.txt` (English).
- `core/system_prompt.md` is Chinese; `core/system_prompt.en.md` is English.
- `README.md` is Chinese; `README_EN.md` is English and still says "CLI and web UI strings remain Chinese".
- ~78k CJK characters remain across 98 files. Live wizard: `webui/static/wizard-v0.js` + `index.html` (`app.js` unused leftover).
- Some parsers already accept English (`舞台|stage`). Others are Chinese-only and already fail under the English default (see Unit 2a list).

Uncommitted local change that **must be preserved**:
`training/adaptive_builder.py` `gen_novel_name_synopsis` falls back to `core_gameplay.md` when `rough_outline.md` is missing.

## Hard constraints (all units)

- **Stage ≠ Phase.** `Stage` is the volume-instance (`# Stage N:` / 舞台). `Phase` is the book-structure unit (`## Phase N:` / 阶段). Never add `stage` as an alias for 阶段.
- New files **emit English glossary forms only**. Chinese remains in regex/alias lists so old workspaces, Chinese reference deconstruction, and Chinese sample-novel import still parse.
- **Freeze on-disk Chinese world-knowledge filenames.** `WORLD_SECTIONS` keys stay `世界观`, `力量体系`, `关键人物`, `势力描述`, `故事主线`, `关键物品`, `技能体系`. Files stay `世界观.md` etc. `_HEADING_ALIASES` already maps English headings onto those keys — copy that map, do not re-translate. `_render_section_document` may keep writing `# 世界观` as the stored heading; English prompt output is already aliased on read.
- Do not rewrite `training/adaptive_builder.py` or `wizard-v0.js` from scratch. Do not split files. Do not revert the `core_gameplay.md` fallback. Do not rename ASCII workspace paths (`story_arcs/`, `stage_outline.md`, `vol_01_…`). Do not translate `DEFAULT_FORBIDDEN_TERMS`. Do not remove the author WeChat block. Do not keep `HARNESS_NOVEL_LANG=zh`. Do not run git. Do not translate `LICENSE`. Leave `docs/web-ui-*.png` binaries.

## Canonical glossary (workers must not invent synonyms)

| Concept | Emit (English) | Also parse (Chinese / old English) |
|---|---|---|
| Volume-instance | `# Stage N: Name` | `# 舞台N：名称` |
| Book-structure phase | `## Phase N: Name` | `## 阶段N：名称` / `第N阶段` |
| Phase outline doc | `# Phase outline` | `# 阶段粗纲` |
| Worldview / rough outline / long mainline / stage roadmap | Worldview / Rough outline / Long mainline / Stage roadmap | 世界观 / 粗略大纲 / 长线主线 / 舞台路线图 |
| Story arc title | `【Arc{n}: Chapters {a}-{b} \| title】` | `【情节…】` |
| Volume-style sections | `Volume overview`, `Three-act structure`, `Character roster`, `Foreshadowing tracker`, `Core payoff` | 卷纲概览 / 三幕结构 / 人物谱系 / 伏笔追踪 / 核心爽点. Also accept old-English `Volume-outline overview`, `Foreshadowing tracking`, `Core payoffs` as parse aliases only. **Prompts must emit the glossary forms**, including `volume_merge` and `adaptive_volume_outline`. |
| Planned chapters | `Planned chapters` | 预计章节数 |
| Incremental phase-outline op | `adjust last phase` / `add phase` | `调整最后阶段` / `新增阶段` |
| Final-window / use-reference interpolation | `yes` / `no` | `是` / `否` |
| Source-role interpolation | `primary source` / `supplement source` | `主资料` / `补充资料` |
| Virtual volume line | `Volume {n}: name \| Chapters {a}-{b}` | `卷N：… \| 第a-b章` |
| Worldview map heading | `# 6. Maps / stage layers` plus `- Layer N:` | current Chinese map heading. Do **not** AND `map`∧`stage`∧`region`∧`layer`. |
| Empty-asset prefix | `(not generated` | `（未生成` — detector must accept both |
| Fake design-field marker | `Model did not return` / `please retry or fill in manually` | `模型未返回` / `请重试或人工补充` — keep detector in sync with `_normalize_design_field` |
| Chat generic commands | `Generate`, `Regenerate`, `Continue generating`, `Adjust`, `Optimize` | `生成`, `重新生成`, `继续生成`, `调整`, `优化` |
| Regenerating intent | `regenerate`, `rewrite`, `start over`, `full rewrite` | `重新生成`, `完全重写`, `推倒重来`, `全部重写`, `从头生成` |
| Extend-stage intent | `extend`, `continue`, `add stage`, `append stage`, `next stage`, `new stage` | `续写`, `新增`, `继续添加`, `往后加`, `加舞台`, `追加舞台`, `下一个舞台`, `新舞台` |
| System-panel example keys | `Name`, `Identity`, `Race`, `Realm`, `Cultivation`, `Level`, `Talent`, `Jing-Qi-Shen`, `Skills`, `Arts`, `Equipment`, `Resources`, `Special abilities`, `Current state`; nested skill: `name`, `stage`, `note`; `Killing aura` | Chinese keys in old JSON still accepted if the validator is key-agnostic |
| Sample-novel markers | `[Title]` / `[Synopsis]` if newly emitted | `【书名】` / `【简介】` |
| Creative-direction headings (if wizard template is translated) | pin in Unit 3; Unit 2 `_load_creative_direction` must strip both English and the current Chinese list at `adaptive_builder.py:192` | `# 创作方向`, `## 题材与定位`, … |
| World sections (prompt headings; disk keys stay Chinese) | copy `_HEADING_ALIASES` in `core/world_knowledge.py` | 世界观.md etc. frozen |
| Canon index | same alias map | Chinese keys frozen |

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
- Prompt proper nouns such as `七宝妙树` may stay.

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
assert '调整最后阶段' not in inc
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
- `## Phase 2: Bar` and `## 阶段2：Bar` match phase 2.
- `_normalize_stage_roadmap` emits `# Stage N:` and still parses `# 舞台N：`.
- `_remove_stage_outline_section` drops both `# Phase outline` and `# 阶段粗纲`.
- `_design_structure_counts` on a stub whose worldview heading is exactly `# 6. Maps / stage layers` plus `- Layer 1: …`.
- `_is_volume_style_stage` true for the English five-section glossary block + `Planned chapters: 12`.
- `_parse_virtual_volumes("Volume 1: The Lock | Chapters 1-78")` and the Chinese `卷1：… | 第1-78章` form.
- English `# Stage {n}:` passes the extend gate currently at `adaptive_builder.py:707`.
- `gen_novel_name_synopsis` still reads `core_gameplay.md` when `rough_outline.md` is missing (dirty hunk).
- Operation constants equal the strings in `design_stage_outline_incremental/prompt.txt`.
- `ARC_HEADER_RE` matches `【Arc1: Chapters 1-5 | The Hook】` (function lives in `outline_builder.py`).
- `_reference_volume_structure_context` / `_reference_volume_stage_structure` resolve `Volume overview` with Chinese fallback.

Parser/interpolation sites that must be patched (not only the original four):

- `STAGE_OUTLINE_HEADING_RE`, `_design_structure_counts`, `_remove_stage_outline_section`
- `adaptive_builder.py:707` Chinese-only `# 舞台{n}` gate after `_normalize_stage_roadmap`
- `re.findall(r"舞台\s*0*(\d+)", instruction)` (~2399) also `stage`
- `_parse_virtual_volumes` (`outline_builder.py:580`)
- `context_manager.extract_relevant_volume_outline` Chinese `一、 …（第N章 - 第M章）`
- `use_reference="是"/"否"` (~690), `is_final_window` in outline_builder/reference_analyzer
- `role_label = "主资料"/"补充资料"` (`world_knowledge.py`)
- `assets[name].startswith("（未生成")` (~655)
- `_is_real_design_field` / placeholder `模型未返回`
- `_load_creative_direction` heading strip list
- `generic_instruction` set and regenerate markers (~2409, 4103–4108, 4713)
- `sections.get("卷纲概览")` / `sections.get("三幕结构")` English-first
- `_CHAPTER_FORBIDDEN_STYLE_PATTERNS`: keep as Chinese-leak detectors **and** add English equivalents (`not X but Y`, `not only X but also Y`)
- `TERM_EXTRACT_STOPWORDS`: add English (`chapter outline`, `draft`, `volume outline`, …) and keep Chinese
- Empty placeholders interpolated into prompts (`（未提供目标世界知识库）`, `（无额外补充方向）`, context-manager layer labels)

Remove remaining `HARNESS_NOVEL_LANG` branches; `_normalize_stage_roadmap` always emits `# Stage N: name`.

Do **not** change `WORLD_SECTIONS` keys or `_section_file_name` output. Do **not** “fix” `text_encoding._COMMON_CJK` (GBK vs Big5 scorer).

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
- `story_arc_title` must extract from `【Arc1: Chapters 1-5 | The Hook】` without requiring `情节`.
- Wizard `worldDescriptions` keys stay `世界观.md` etc. Labels/descriptions may be English.
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

1. Heading/keyword alias string literals (`舞台`, `阶段`, `情节`, `卷纲概览`, `是`, `否`, …)
2. `DEFAULT_FORBIDDEN_TERMS` + `TERM_EXTRACT_STOPWORDS`
3. `text_encoding._COMMON_CJK`
4. Tests asserting Chinese aliases
5. Author proper noun `飞鸟`
6. Frozen on-disk names `世界观.md` etc. in `WORLD_SECTIONS` / wizard `worldDescriptions` keys
7. Prompt proper nouns (`七宝妙树`)
8. CSS comments optional

```bash
python3 -m compileall -q core training webui novel_cli.py
python3 -m unittest tests.test_heading_parsers -v
python3 novel_cli.py --help
python3 -c "from core.prompt_loader import PromptLoader; print(PromptLoader.load('adaptive_drafting')[:200])"
```

No live LLM run required. Browser chrome smoke after Unit 3.

## Out of scope

File splits, screenshot reshoots, translating user novel content, keeping a zh mode, migrating `世界观.md` filenames, changing workspace directory layout.
