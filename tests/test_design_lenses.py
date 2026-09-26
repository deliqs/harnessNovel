import os
import tempfile
import unittest
from pathlib import Path

from core.workspace import init_workspace
from training.design_lenses import (
    lens_file_status,
    load_lenses,
    reset_lens_file,
    save_lens_file,
)


_BUILTIN_NAMES = [
    "Depth",
    "Engagement",
    "Intensity and pacing",
    "Internal consistency",
    "Character stakes",
]


class DesignLensesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        self.root = Path(self._tmp.name)
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def _ws(self):
        return init_workspace("book")

    def test_builtin_lenses(self):
        lenses = load_lenses(self._ws())
        self.assertEqual([name for name, _focus in lenses], _BUILTIN_NAMES)
        for _name, focus in lenses:
            self.assertTrue(focus.strip())

    def test_lens_file_add_replace_status_and_reset(self):
        ws = self._ws()
        status = lens_file_status(ws)
        self.assertFalse(status["exists"])
        self.assertEqual(status["path"], "file_system/story_design/critic_lenses.md")
        self.assertEqual(status["lenses"], _BUILTIN_NAMES)

        for empty in ("", " \n", "no headings here"):
            with self.assertRaises(ValueError):
                save_lens_file(ws, empty)

        save_lens_file(
            ws,
            "## DEPTH\nFocus on motif density.\n\n## Voice\nTrack distinctive narration.\n",
        )
        lenses = load_lenses(ws)
        names = [name for name, _focus in lenses]
        by_name = {name: focus for name, focus in lenses}
        self.assertEqual(names[0], "Depth")
        self.assertIn("motif density", by_name["Depth"])
        self.assertEqual(names[-1], "Voice")
        self.assertIn("distinctive narration", by_name["Voice"])
        self.assertEqual(len(names), len(_BUILTIN_NAMES) + 1)

        status = lens_file_status(ws)
        self.assertTrue(status["exists"])
        self.assertIn("Voice", status["lenses"])
        self.assertIn("Depth", status["lenses"])

        reset_lens_file(ws)
        self.assertFalse(lens_file_status(ws)["exists"])
        self.assertEqual([name for name, _focus in load_lenses(ws)], _BUILTIN_NAMES)


if __name__ == "__main__":
    unittest.main()
