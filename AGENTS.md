# harnessNovel repository guide

## Repository map

- `novel_cli.py` is the public CLI and dispatches into the package.
- `core/` contains shared workspace, configuration, LLM, prompt, text, chapter-path, and world-knowledge primitives.
- `training/` contains the production deconstruction and novel-generation pipelines despite its historical name.
- `webui/` contains the FastAPI workbench, background task/chat managers, and dependency-free browser assets.
- `core/prompts/<name>/prompt.txt` contains model prompt templates loaded at runtime.
- `tests/` is the standard-library `unittest` suite. `tasks/` contains historical implementation plans, not current source-of-truth instructions.

Read `README.md` when changing user-visible workflow or CLI behavior. More specific `AGENTS.md` files supplement this guide and take precedence only where a subtree instruction conflicts.

## Compatibility and data contracts

- Support Python 3.9 and later. Match the style and typing level of the module being changed; do not introduce a repository-wide typing or `pathlib` conversion as a side effect.
- Treat CLI commands and flags, Web API payloads, workspace layout, artifact filenames, Markdown headings, JSON fields, and checkpoint files as compatibility surfaces.
- English is the only stored form for our generated story, chapter, and world-knowledge headings and filenames. User-import parsers may match CJK in imported reference novels via unicode escapes, never hanzi literals. Do not keep Chinese aliases for generated files.
- `Stage` identifies the current volume-sized story unit. `Phase` identifies a structural subdivision inside a design. Do not make their parsers or headings interchangeable.
- Route novel workspace paths through `NovelWorkspace`, `get_novels_dir()`, and the existing path helpers. `NOVELS_DIR` is a compatibility snapshot; new runtime code should use `get_novels_dir()`.
- Preserve resume, idempotency, and `--force` behavior. Never silently replace valid user artifacts after an empty model response or incompatible source change.
- Keep the CLI and Web workbench on the same core/training workflows. Do not duplicate generation logic in a route or browser asset.

`core/agents.md` is runtime prose-writing data loaded into model context. It is not a coding-agent instruction file. Do not rename it or create `core/AGENTS.md`; those names collide on case-insensitive filesystems. Workspace-level `AGENTS.md` files may also be novel-writing data consumed by the application.

## Working practices

- Treat Robin as a coworker: communicate concisely, state uncertainty candidly, support pushback with evidence, and do not give time estimates.
- Prefer the smallest simple, maintainable change. Ask before replacing or reimplementing an existing feature or subsystem.
- Work from the repository root so package imports and workspace defaults behave consistently.
- Treat every checkout as shared. Keep diffs focused, preserve unrelated changes, and ask before reverting, deleting, or cleaning unexpected work. Avoid broad reformatting, especially in the large pipeline modules.
- Preserve comments unless they are demonstrably false. Keep comments and names evergreen; avoid labels such as `New`, `Improved`, `Enhanced`, or `V2`.
- Verify drift-prone facts against current primary sources when the answer depends on them, and state remaining uncertainty plainly.
- There is no configured formatter, linter, type checker, or JavaScript package manager. Offline CI is `.github/workflows/ci.yml` (unittest, compileall, and CLI help on Python 3.9 and 3.12). Do not invent extra mandatory checks; follow surrounding formatting.
- Never commit API keys or tokens. Keep configuration-key changes synchronized across `core/config.py`, `.env.example`, the Web configuration UI, and `README.md` where applicable.
- Do not add generated-agent footers or co-author trailers to commits or pull requests.
- Unit tests must remain offline. Mock or fake LLM calls; do not invoke paid/live generation to validate a code change unless explicitly requested.
- Use a temporary `HARNESS_NOVEL_HOME` or an explicit scratch `--workspace-root` for manual checks. Do not test against a real novel workspace. Starting the Web app also persists state under `~/.harnessNovel/web`, so only do so when the change needs a manual Web check.
- Follow `CLAUDE.md` for repository-local delegation policy. Small fixes, docs changes, and lookups stay in the current session; use additional workers only for genuinely cross-cutting work spanning at least five files.

## Setup and verification

Create an editable development environment when needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Run the narrowest relevant test module first, then the full suite for cross-cutting changes:

```bash
python -m unittest tests.test_heading_parsers -v
python -m unittest discover -s tests -v
```

Cheap syntax and CLI checks are:

```bash
python -m compileall -q core training webui novel_cli.py
python novel_cli.py --help
```

Report pre-existing failures separately from regressions introduced by the change. A CLI contract change normally requires matching parser/Web updates, tests, and a `README.md` update.
