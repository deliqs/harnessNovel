"""Tracked text files must not contain CJK Unified Ideographs."""
import subprocess
import unittest
from pathlib import Path

_CJK_MIN = 0x4E00
_CJK_MAX = 0x9FFF
_SCAN_SUFFIXES = (".py", ".js", ".html", ".md", ".txt", ".svg", ".css")
_SKIP_SUFFIXES = (".png", ".jpg", ".jpeg")


def _repository_root():
    return Path(__file__).resolve().parents[1]


def _tracked_paths():
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=str(_repository_root()),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.splitlines()


def _should_scan(relative_path):
    path = Path(relative_path)
    suffix = path.suffix.lower()
    if suffix in _SKIP_SUFFIXES:
        return False
    if path.name == "prompt.txt":
        return True
    return suffix in _SCAN_SUFFIXES


def _cjk_line_hits(relative_path, text):
    hits = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for char in line:
            if _CJK_MIN <= ord(char) <= _CJK_MAX:
                hits.append("%s:%d" % (relative_path, line_no))
                break
    return hits


class TestNoCjk(unittest.TestCase):
    def test_tracked_text_has_no_cjk_ideographs(self):
        root = _repository_root()
        hits = []
        for relative_path in _tracked_paths():
            if not _should_scan(relative_path):
                continue
            path = root / relative_path
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            hits.extend(_cjk_line_hits(relative_path, text))
        self.assertEqual(
            hits,
            [],
            "CJK Unified Ideographs (U+4E00-U+9FFF) found:\n%s" % "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
