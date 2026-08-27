# Generation pipeline guide

## Responsibilities

- `adaptive_builder.py` orchestrates current book design, story arcs, chapter outlines, drafts, mechanics, and target-world generation.
- `reference_analyzer.py` owns the current resumable chapter-card, story-segment, and reference-structure analysis.
- `outline_builder.py` contains shared parsing and compatibility behavior and routes current deconstruction into `ReferenceAnalyzer`.
- `reference_finder.py` resolves persisted reference artifacts.

These modules are production code used by both the CLI and Web workbench. Keep changes local in the large modules and extract a helper only when it clarifies a reused contract.

## Pipeline invariants

- Preserve checkpoint/resume and idempotency behavior. Reuse complete artifacts, keep atomic checkpoint writes where already used, and require an explicit rebuild/force path for incompatible existing state.
- Do not write or overwrite an artifact when a model returns empty content. Preserve the existing write guards and actionable retry behavior.
- Propagate `progress_callback`, pause/stop events, and cancellation events through long-running loops and model calls. Do not convert cancellation into an ordinary successful result.
- Use `PromptLoader`, `LLMProvider`, `ConfigLoader`, `NovelWorkspace`, chapter/path helpers, and `reference_finder` instead of creating parallel implementations.
- Keep model roles distinct: reference analysis, high-level design, and lightweight production use the existing configuration accessors. Do not silently route one role to another.
- Preserve current English output while accepting supported Chinese/legacy headings and filenames. In particular, keep Stage and Phase parsing distinct.
- Treat generated model text as untrusted. Normalize and validate it before persisting structured output, and preserve existing fallback behavior where validation fails.

## Verification

Add regression coverage for both the current emitted form and any retained legacy input form when changing parsers, filenames, or persisted schemas. Common focused suites are:

```bash
python -m unittest tests.test_heading_parsers tests.test_story_arc_context -v
python -m unittest tests.test_system_panel tests.test_chapter_filenames -v
```

Then run `python -m unittest discover -s tests -v`. Tests must use fake/mocked LLMs and temporary workspaces, never live model calls or user data.
