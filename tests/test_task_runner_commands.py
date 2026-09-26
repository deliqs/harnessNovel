"""CLI subprocess commands used by the Web workbench."""
import sys
import unittest
from pathlib import Path

from webui.task_runner import TaskManager


ROOT = Path(__file__).resolve().parents[1]


class TaskRunnerCommandTests(unittest.TestCase):
    def test_workspace_init_uses_the_checkout_cli_file(self):
        manager = object.__new__(TaskManager)

        command = manager._build_command("workspace_init", "demo", {})

        self.assertEqual(
            command,
            [sys.executable, str(ROOT / "novel_cli.py"), "init", "demo"],
        )


if __name__ == "__main__":
    unittest.main()
