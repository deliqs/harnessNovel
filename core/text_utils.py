import json
import re


def clean_markdown_symbols(text: str) -> str:
    """Strip Markdown markers (bold, italic, list bullets, etc.) while keeping # headings."""
    if not text:
        return text
    # Remove **bold** and *italic* markers; keep the inner text.
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Remove leading - list markers; keep the content.
    text = re.sub(r'^(\s*)-\s+', r'\1', text, flags=re.MULTILINE)
    # Remove leading > quote markers.
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    return text


def normalize_text(text: str) -> str:
    """Normalize text: drop full-width indent spaces, collapse extra blank lines, rstrip lines."""
    if not text:
        return text

    # Drop ideographic space (U+3000), used only as paragraph indent in Chinese web novels.
    text = text.replace('　', '')

    # Collapse 3+ newlines to 2 (keep paragraph breaks).
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Strip trailing whitespace on each line.
    lines = text.split('\n')
    lines = [line.rstrip() for line in lines]
    text = '\n'.join(lines)

    # Strip leading/trailing whitespace of the whole string.
    text = text.strip()

    return text


def parse_json_response(raw: str) -> dict:
    """Clean and parse LLM JSON, handling control characters and fenced code blocks."""
    import re as _re
    cleaned = raw.strip()
    # Strip markdown code fences.
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    # Drop illegal control characters inside JSON values.
    cleaned = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)
    # Fix trailing commas.
    cleaned = cleaned.replace(",}", "}").replace(",]", "]")
    return json.loads(cleaned)
