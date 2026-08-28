# Zero CJK in tracked text

Robin asked for zero Chinese in this repo. Explicit compatibility removal: Chinese artifact names, parse aliases, tests, docs, prompts, and author-string CJK all go. Runtime may still see CJK in user files; tracked source must not contain codepoints U+4E00-U+9FFF.

## Done when

```bash
python3 -m compileall -q core training webui novel_cli.py
python3 -m unittest discover -s tests -v
python3 novel_cli.py --help
```

`tests/test_no_cjk.py` scans git-tracked `py/js/html/md/txt/svg/css` plus `prompt.txt` and fails on any CJK codepoint. Tests that need CJK at runtime use ASCII unicode escapes only (`"\\u4e00"`), never literal characters.

## Product consequences (accepted)

- English-only heading and filename parsers. Imported novels that only use Chinese chapter/volume headings are no longer auto-sliced by those headings.
- Chapter-draft `legacy=` Chinese basenames and world-knowledge Chinese filename aliases are removed, not kept as read fallbacks.
- `DEFAULT_FORBIDDEN_TERMS` become English proper nouns (Sun Wukong, Journey to the West, Flower-Fruit Mountain, Ruyi Jingu Bang, Bodhi Patriarch, Tang Sanzang).
- `_COMMON_CJK` frequency table is deleted. Encoding still tries UTF-8, charset_normalizer, then `gb18030` / `big5` ranked by CJK *range count* via unicode escapes, not literal characters.
- Author string becomes ASCII `Fei Niao`. Logo `<title>` becomes `HarnessNovel mark`. Delete `docs/heading-web-zh.svg`.
- PNG/JPG screenshots are out of this pass (binary false positives; visual reshoot is separate).

## Units (serial, shared tree)

`adaptive_builder.py` is a collision magnet. One worker at a time on the shared tree.

### Unit A — engine Python + tests (hard)

Owner: `core/*.py` except `core/prompts/**`, `training/*.py`, `tests/*.py` except `test_no_cjk.py` (that file lands in C).

- Strip every CJK codepoint. English-only regexes. Keep Stage != Phase.
- `chapter_utils.py`: drop Chinese numeral maps and `legacy=`. Resolve/delete English draft names only. `_fix_chapter_numbering` only rewrites `Chapter N` / `Ch. N`.
- `world_knowledge.py`: English keys and files only; no Chinese heading aliases or Chinese filenames.
- `outline_builder.py`: drop the whole-book Chinese token; English volume/chapter/arc regexes only.
- Rewrite tests that used Chinese fixtures to English (or unicode-escape) fixtures. Remove assertions that Chinese aliases still parse.
- Do not split large modules. Do not rewrite `adaptive_builder.py` from scratch. Do not add `test_no_cjk.py` yet (it would fail on web/docs).

Check:

```bash
python3 -m compileall -q core training
python3 -m unittest tests.test_heading_parsers tests.test_chapter_filenames tests.test_world_knowledge_filenames tests.test_webui_headings tests.test_generation_quality tests.test_chapter_style_violations tests.test_adaptive_quality_integration -v
```

### Unit B — web + CLI

Owner: `webui/**` except brand-spec/logo (C), plus `novel_cli.py`.

- Wizard world-knowledge labels: English filenames only (`worldview.md`, `power_system.md`, …).
- `chapterNumberFromPath`: English patterns only.
- `design_chat.py` / `task_runner.py`: paste Unit A's exact Stage / Phase / Arc regexes. English-only error markers.
- `WORKSPACE_NAME_RE`: ASCII word characters only (no CJK range).
- `index.html`: `Fei Niao on the Way`; bump `?v=`.

Check: `python3 -m compileall -q webui novel_cli.py && python3 novel_cli.py --help`

### Unit C — prompts, docs, brand, tasks, AGENTS

Owner: `core/prompts/**`, `README.md`, `setup.py`, `webui/brand-spec.md`, `webui/static/logo.svg`, `docs/heading-web-zh.svg`, `docs/cli_io_mindmap.html` if needed, all `AGENTS.md`, `tasks/*.md`.

- Replace the Seven-Treasure Tree prompt example with ASCII.
- Author / brand copy ASCII. Delete `docs/heading-web-zh.svg`.
- AGENTS.md: English is the only stored form; do not tell workers to keep Chinese aliases.
- Historical task docs: rewrite so they contain no CJK.
- Add `tests/test_no_cjk.py` and run the full suite.

Check:

```bash
python3 -m compileall -q core training webui novel_cli.py
python3 -m unittest discover -s tests -v
python3 novel_cli.py --help
```

## Out of scope

Reshooting `docs/web-ui-*.png`. Translating user novel workspaces under `my-novels/`. Splitting `adaptive_builder.py`. Git commits.
