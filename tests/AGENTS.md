# Test suite guide

- Use the standard library's `unittest` and `unittest.mock`; pytest is not configured as a repository dependency.
- Keep tests deterministic, fast, and offline. Patch provider construction or inject a `FakeLLM`; never require API keys or network access.
- Isolate filesystem behavior with `tempfile.TemporaryDirectory` and a temporary `HARNESS_NOVEL_HOME`. Restore every mutated environment variable in `tearDown` or `finally`, including when an assertion fails.
- Do not read, mutate, or delete real workspaces or global HarnessNovel configuration.
- Prefer behavioral assertions. Source-inspection assertions are appropriate only for an explicit cross-file wiring contract and should not replace exercising observable behavior.
- For format migrations, test the canonical English write/emit path. User-import parsers may match CJK via unicode escapes; cover those with escaped fixtures. Do not keep Chinese aliases for generated files. Keep Stage and Phase cases separate.
- Cover empty model output, malformed structured output, cancellation, and resume behavior when changing those paths.

Run a focused module during iteration, for example:

```bash
python -m unittest tests.test_llm_provider -v
```

Before handoff for a cross-cutting change, run:

```bash
python -m unittest discover -s tests -v
```

If the baseline already fails, confirm the focused tests for the change and report the unrelated failure separately; do not weaken an assertion just to make discovery green.
