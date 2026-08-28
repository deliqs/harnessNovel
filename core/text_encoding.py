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
    "\u7684\u4e00\u662f\u5728\u4e0d\u4e86\u6709\u548c\u4eba\u8fd9\u4e2d\u5927\u4e3a"
    "\u4e0a\u4e2a\u56fd\u6211\u4ee5\u8981\u4ed6\u65f6\u6765\u7528\u4eec\u751f\u5230"
    "\u4f5c\u5730\u4e8e\u51fa\u5c31\u5206\u5bf9\u6210\u4f1a\u53ef\u4e3b\u53d1\u5e74"
    "\u52a8\u540c\u5de5\u4e5f\u80fd\u4e0b\u8fc7\u5b50\u8bf4"
    "\u9019\u500b\u5011\u4f86\u6642\u570b\u70ba\u8457\u8207\u8b93\u5f9e\u65bc"
    "\u5f8c\u9084\u958b\u767c\u9ad4\u8aaa\u5b78\u6703\u61c9\u6e2c\u8a66"
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
