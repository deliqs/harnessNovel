import os
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.config import write_private_text
from novel_cli import cmd_config


class ConfigPermissionTests(unittest.TestCase):
    def test_private_write_does_not_change_existing_parent_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            if os.name == "posix":
                os.chmod(directory, 0o755)
            path = os.path.join(directory, ".env")
            write_private_text(path, "API_KEY=secret\n")

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "API_KEY=secret\n")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o755)

    def test_cli_config_is_private(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                cmd_config(SimpleNamespace(force=False))
            path = os.path.join(home, ".harnessNovel", ".env")
            self.assertTrue(os.path.isfile(path))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
