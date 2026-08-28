# Web workbench guide

## Architecture

- `app.py` composes the FastAPI application and maps domain failures to HTTP responses.
- `task_runner.py` owns workspace validation and containment, uploads, CLI subprocess tasks, logs, and persisted task state.
- `design_chat.py`, `arc_chat.py`, `chapter_chat.py`, and `draft_chat.py` run interactive generation in background threads and expose pause/resume/stop state.
- `static/` is plain HTML, CSS, and JavaScript with no npm or bundling step. Treat `static/index.html` as the source of truth for active assets; it currently loads `wizard-v0.css` and `wizard-v0.js`, not the legacy `styles.css` and `app.js` pair.
- `brand-spec.md` is the visual and interaction reference. Use it instead of duplicating design tokens here.

## Web contracts

- Keep backend request/response shapes, route paths, task states, DOM IDs, and JavaScript calls synchronized.
- English is the only stored form for generated artifacts. User-import parsers may match CJK via unicode escapes. Do not keep Chinese aliases for generated files.
- Reuse the generation functions in `training/`; do not fork CLI behavior in the Web layer.
- Preserve workspace-name validation, resolved-path containment, direct-child deletion checks, per-workspace activity guards, locks, and event-driven pause/stop semantics.
- Never serialize thread locks or event objects into API responses. Return stable JSON data and convert expected domain errors to consistent HTTP details.
- Treat workspace names, file content, model output, and error text as untrusted. Prefer `textContent`; when building HTML strings, pass interpolated content through the existing escaping helper.
- Maintain keyboard access, labels, focus behavior, responsive layout, and reduced-motion behavior when changing the UI.
- Do not assume a frontend build step. `static/index.html` owns asset references and version queries; static files are packaged directly by `setup.py` and served with local no-store headers from `app.py`.

## Verification

Run relevant backend contract tests first:

```bash
python -m unittest tests.test_webui_headings tests.test_chapter_filenames -v
```

Run the full `unittest` suite after API, task, or cross-layer changes. For a necessary manual smoke test, use a scratch workspace root and remember that the app persists Web settings and task data under `~/.harnessNovel/web`; do not point it at real user workspaces.
