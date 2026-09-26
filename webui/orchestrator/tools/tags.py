"""Tags the UI puts in front of the author's message, read server-side so tools never rely on the model.

The UI prefixes the message with one header line of tags, e.g.
`[use_new_reference] [attached upload <32-hex id>: notes.md]`, then a newline and the author's text.
Only the latest author prompt counts; `[auto]` prompts are the client's job-finished continuations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelRequest, UserPromptPart

UPLOAD_TAG_RE = re.compile(r"\[attached upload ([0-9a-f]{32}): ")
AUTO_PREFIX = "[auto]"


@dataclass(frozen=True)
class AuthorTags:
    upload_ids: tuple[str, ...] = ()
    use_new_reference: bool = False
    sync_updated_design: bool = False
    humanize_off: bool = False


def _header(text: str) -> str:
    """The tag line: the first line, when the message has a newline after it and it starts with a tag."""
    first, newline, _ = text.partition("\n")
    return first if newline and first.lstrip().startswith("[") else ""


def parse_tags(text: str) -> AuthorTags:
    """The tags in the header line of one author message."""
    header = _header(text or "")
    return AuthorTags(
        upload_ids=tuple(dict.fromkeys(UPLOAD_TAG_RE.findall(header))),
        use_new_reference="[use_new_reference]" in header,
        sync_updated_design="[sync_updated_design]" in header,
        humanize_off="[humanize: off]" in header,
    )


def strip_tag_header(text: str) -> str:
    """`text` without its tag line, for when the model copies the author's message whole."""
    if parse_tags(text) == AuthorTags():
        return text
    return text[len(_header(text)) + 1:]


def _prompt_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    return "\n".join(item for item in part.content if isinstance(item, str))


def latest_author_prompt(messages: list[Any]) -> str:
    """The text of the newest user prompt that is not an `[auto]` continuation, or an empty string."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if not isinstance(part, UserPromptPart):
                continue
            text = _prompt_text(part)
            if not text.lstrip().startswith(AUTO_PREFIX):
                return text
    return ""


def author_tags(ctx: Any) -> AuthorTags:
    """The tags of the latest author prompt in the run's history (`RunContext.messages`)."""
    return parse_tags(latest_author_prompt(getattr(ctx, "messages", None) or []))
