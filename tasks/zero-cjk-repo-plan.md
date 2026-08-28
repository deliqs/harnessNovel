# Zero CJK in tracked text

Robin asked for zero Chinese in this repo. Co-lead plan critique reconciled: **repo source has no hanzi literals; the product still slices Chinese reference novels.**

Tracked `py/js/html/md/txt/svg/css` must not contain U+4E00-U+9FFF. Runtime may still see CJK in user files. Where a parser must match user Chinese, use ASCII unicode escapes (`"\\u7b2c"`), never a literal character.

## Two surfaces (do not mix)

| Surface | Action |
|---|---|
| **Our** artifacts (world-knowledge files, chapter-draft names, `# Stage` / `# Phase`, volume-style design headings, wizard labels, chat commands, empty-asset prefixes) | English only. Delete Chinese aliases. No read fallback. |
| **User** reference novels (chapter/volume split, Chinese numerals, GBK/Big5 ranking, Journey-to-the-West leak terms, style/word-count CJK ranges, `_safe_name` for imported source names) | Keep behavior. Unicode-escape every hanzi in source. Add tests with escaped fixtures. |

CJK punctuation already used by the English glossary (`【Arc1: Chapters 1-5 | title】`) may stay. Han-glyph **paths** in SVG stay. PNG/JPG out of scanner.

## Done when

```bash
python3 -m compileall -q core training webui novel_cli.py
python3 -m unittest discover -s tests -v
python3 novel_cli.py --help
```

`tests/test_no_cjk.py` uses `git ls-files` and reports `path:line`. Range U+4E00-U+9FFF. Lands in Unit C.

This task **is** the AGENTS.md compatibility-removal exception. Do not keep Chinese read aliases for our artifacts.

## Units (serial, shared tree)

### Unit A — engine Python + tests (hard)

Owner: `core/*.py` except `core/prompts/**`; `training/*.py`; engine tests listed below. Not `tests/test_webui_headings.py`. Not wizard JS.

- Our-artifact aliases: delete (world-knowledge Chinese files/headings, `legacy=` draft names, Stage/Phase/Arc dual parse of Chinese design headings, whole-book directory token, chat-adjacent training command Chinese).
- User-import matchers: escape, do not delete (`CHAPTER_HEADER_RE`, `VOLUME_HEADER_RE`, `_CN_MAP` / `_CN_DIGITS` / `_cn_to_int`, `_COMMON_CJK`, `DEFAULT_FORBIDDEN_TERMS` Chinese tokens). `_int_to_cn` volume labels become ASCII `(part N)`; `_fix_chapter_numbering` may still rewrite imported Chinese titles via escaped patterns.
- `_safe_name` CJK ranges stay as `\\u4e00-\\u9fff` (user source names).
- Tests: English fixtures for our artifacts. Escaped fixtures proving `split_chapters` still splits Chinese `Chapter`-equivalent headings **and** English `Chapter N`. GBK vs Big5 decode test with byte fixtures. Style-violation Chinese patterns stay as escapes.
- Do not split or rewrite `adaptive_builder.py` from scratch. Do not add `test_no_cjk.py`. Do not edit `webui/` or `novel_cli.py`.

Check:

```bash
python3 -m compileall -q core training
python3 -m unittest tests.test_heading_parsers tests.test_chapter_filenames tests.test_world_knowledge_filenames tests.test_generation_quality tests.test_chapter_style_violations tests.test_adaptive_quality_integration tests.test_reference_analyzer -v
```

`test_world_knowledge_filenames` must stop asserting wizard JS Chinese keys (B owns that). Drop Chinese-alias behavior assertions for our artifacts.

### Unit B — web + CLI

Owner: `webui/design_chat.py`, `webui/task_runner.py`, `webui/static/wizard-v0.js`, `webui/static/index.html`, `webui/app.py` only if a user-facing string still has hanzi, `novel_cli.py`, `tests/test_webui_headings.py`.

- Paste Unit A's exact Stage/Phase regexes (English). Run `tests.test_webui_headings`.
- Wizard: English world-knowledge filenames only.
- Chat commands: English only (drop Chinese command aliases).
- `index.html`: WeChat caption `Fei Niao on the Way`; bump **both** `?v=` query params.
- `WORKSPACE_NAME_RE`: remove explicit hanzi range if present as literals; do **not** add `re.ASCII` (Unicode `\w` may still match CJK user names). Update error text if it mentions Chinese.
- Do not ASCII-only `_safe_filename` for uploads.

Check:

```bash
python3 -m compileall -q webui novel_cli.py
python3 novel_cli.py --help
python3 -m unittest tests.test_webui_headings tests.test_world_knowledge_filenames -v
```

### Unit C — prompts, docs, brand, tasks, AGENTS, scanner

Owner: `core/prompts/**`, `README.md`, `setup.py`, `webui/brand-spec.md`, `webui/static/logo.svg` title only, `docs/heading-web-zh.svg` (delete), all `AGENTS.md`, `tasks/*.md`.

- Seven-Treasure Tree ASCII example.
- Author `Fei Niao` in setup/README. Logo title `HarnessNovel mark`. Brand-spec without hanzi.
- AGENTS.md: English stored forms; user-import parsers may match CJK via escapes. Do not tell workers to keep our-artifact Chinese aliases.
- Rewrite historical tasks docs with no hanzi.
- Add `tests/test_no_cjk.py`. If it fails on engine files, that is Unit A, not a C rewrite of adaptive_builder.

Check: full suite + scanner.

## Out of scope

Reshooting `docs/web-ui-*.png` (README may still depict an old Chinese workbench). Han-glyph SVG paths. WeChat image. Translating `my-novels/`. Splitting `adaptive_builder.py`. Git commits. Redesigning the mark.
