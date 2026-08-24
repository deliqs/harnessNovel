"""Encoding detection and UTF-8 normalization for imported text."""

from __future__ import annotations

from pathlib import Path

try:
    from charset_normalizer import from_bytes
except ImportError:  # Fall back to built-in Chinese encodings if charset_normalizer is missing.
    from_bytes = None


_BOM_ENCODINGS = (
    (b"\xef\xbb\xbf", "utf-8-sig", "UTF-8 with BOM"),
    (b"\xff\xfe\x00\x00", "utf-32-le", "UTF-32 LE"),
    (b"\x00\x00\xfe\xff", "utf-32-be", "UTF-32 BE"),
    (b"\xff\xfe", "utf-16-le", "UTF-16 LE"),
    (b"\xfe\xff", "utf-16-be", "UTF-16 BE"),
)
_COMMON_CJK = set(
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说"
    "這個們來時國為著與讓從於後還開發體說學會應測試"
)


def _cjk_score(text: str) -> tuple[int, int]:
    """Score Chinese decode candidates so short GBK text is not mistaken for Big5."""
    cjk_count = sum("\u4e00" <= char <= "\u9fff" for char in text)
    common_count = sum(char in _COMMON_CJK for char in text)
    return common_count, cjk_count


def decode_text_bytes(raw: bytes) -> tuple[str, str]:
    """Decode common web-novel encodings and return the text plus the detected encoding name."""
    for marker, encoding, label in _BOM_ENCODINGS:
        if raw.startswith(marker):
            return raw.decode(encoding), label

    try:
        return raw.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass

    candidates: list[tuple[str, str]] = []
    if from_bytes is not None:
        detected = from_bytes(raw).best()
        if detected and detected.encoding:
            candidates.append((str(detected), detected.encoding.upper()))

    # GB18030 covers GBK/GB2312 and is the usual Chinese-novel fallback; score it with detections.
    for encoding, label in (("gb18030", "GB18030/GBK"), ("big5", "Big5")):
        try:
            candidates.append((raw.decode(encoding), label))
        except UnicodeDecodeError:
            continue

    chinese_candidates = [candidate for candidate in candidates if _cjk_score(candidate[0])[1] > 0]
    if chinese_candidates:
        return max(chinese_candidates, key=lambda candidate: _cjk_score(candidate[0]))
    if candidates:
        return candidates[0]

    raise ValueError("Could not detect the reference novel encoding. Convert it to UTF-8 and import again.")


def copy_as_utf8(source: str | Path, destination: str | Path) -> str:
    """Read source text, write it as UTF-8 to the destination, and return the detected encoding."""
    source_path = Path(source)
    destination_path = Path(destination)
    text, encoding = decode_text_bytes(source_path.read_bytes())
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(text, encoding="utf-8")
    return encoding
