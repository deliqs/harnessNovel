from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


class PackagingRuntimeAssetsTests(unittest.TestCase):
    def test_setup_includes_runtime_prose_assets(self):
        repository = Path(__file__).resolve().parents[1]
        with patch("setuptools.setup") as setup:
            runpy.run_path(str(repository / "setup.py"), run_name="__setup_test__")

        package_data = setup.call_args.kwargs["package_data"]
        install_requires = setup.call_args.kwargs["install_requires"]
        self.assertIn("prompts/*/prompt.txt", package_data["core"])
        self.assertIn("system_prompt.md", package_data["core"])
        self.assertIn("agents.md", package_data["core"])
        self.assertTrue((repository / "core" / "system_prompt.md").is_file())
        self.assertTrue((repository / "core" / "agents.md").is_file())
        self.assertIn("openai>=1.0.0", install_requires)


if __name__ == "__main__":
    unittest.main()
