# Prompt template guide

## Runtime contract

- Each prompt lives at `<folder>/prompt.txt`. Its folder name is passed to `PromptLoader.load()` and the files are included as package data by `setup.py`.
- `PromptLoader` renders templates with Python `str.format`. Keep placeholders synchronized with every call site's keyword arguments. Write literal braces, including JSON examples, as `{{` and `}}`.
- Treat requested headings, JSON keys, field names, ordering rules, language rules, and plain-text/code-fence restrictions as machine-readable output contracts. Downstream code often parses them with regexes or JSON validation.
- English is the canonical generated format. Do not remove intentional Chinese aliases from downstream readers when changing a prompt.
- Repeated hard constraints can be deliberate model steering. Remove or consolidate them only when the task includes changing that behavior.

## Changing a prompt

- Inspect the `PromptLoader.load()` call and the parser/validator that consumes the response before editing the template.
- Change the prompt, caller variables, parser, and behavioral tests together when the output schema changes.
- Keep prompt growth deliberate: many pipelines combine several large contexts and apply explicit size caps.
- Do not add alternate prompt filenames without updating the loader and package-data rules.
- `core/system_prompt.md` and `core/agents.md` are separate runtime writing guides, not folder-based prompt templates.

Mechanical prompt changes should be verified with focused parser/caller tests and the full `unittest` suite. Live model calls are not required for normal validation.
