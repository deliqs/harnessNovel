"""Guard retirement of the obsolete layered ContextManager implementation."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContextManagerRetirementTests(unittest.TestCase):
    def test_obsolete_module_is_not_reintroduced_or_imported_by_production_code(self):
        self.assertFalse((ROOT / "core" / "context_manager.py").exists())
        source_paths = [ROOT / "novel_cli.py"]
        for directory in ("core", "training", "webui"):
            source_paths.extend((ROOT / directory).rglob("*.py"))
        for source_path in source_paths:
            self.assertNotIn("context_manager", source_path.read_text(encoding="utf-8"), source_path)


if __name__ == "__main__":
    unittest.main()
