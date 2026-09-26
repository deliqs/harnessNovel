"""Every orchestrator source file stays at or under 300 lines."""
import unittest
from pathlib import Path

MAX_LINES = 300
WEBUI = Path(__file__).resolve().parents[1] / "webui"


def _limited_files():
    files = [path for path in (WEBUI / "orchestrator").rglob("*") if path.suffix in {".py", ".js"}]
    files += sorted((WEBUI / "static").glob("orchestrator-*.js"))
    return files


class FileSizeLimitTests(unittest.TestCase):
    def test_orchestrator_files_have_at_most_300_lines(self):
        files = _limited_files()
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=str(path.relative_to(WEBUI))):
                lines = len(path.read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(lines, MAX_LINES)


if __name__ == "__main__":
    unittest.main()
