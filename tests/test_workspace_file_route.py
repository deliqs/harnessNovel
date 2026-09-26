"""Offline tests for the workbench file routes: dot folders stay out of reach."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui.app import create_app


class WorkspaceFileRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        story = self.root / "book" / "file_system" / "story_design"
        story.mkdir(parents=True)
        (story / "worldview.md").write_text("# Worldview\n", encoding="utf-8")
        hidden = self.root / "book" / ".orchestrator"
        hidden.mkdir()
        (hidden / "x.json").write_text("{}", encoding="utf-8")
        web_home = self.root / "web-home"
        web_home.mkdir()
        with patch("webui.app.WEB_HOME", web_home), patch(
            "webui.app.WEB_SETTINGS_PATH", web_home / "settings.json",
        ):
            self.client = TestClient(create_app(str(self.root)))

    def tearDown(self):
        self._tmp.cleanup()

    def test_dot_folder_paths_are_refused_for_read_and_write(self):
        for path in (".orchestrator/x.json", "file_system/../.orchestrator/x.json"):
            read = self.client.get("/api/workspaces/book/file", params={"path": path})
            self.assertEqual(read.status_code, 400, path)
            write = self.client.put("/api/workspaces/book/file", json={"path": path, "content": "{\"a\": 1}"})
            self.assertEqual(write.status_code, 400, path)
        hidden = self.root / "book" / ".orchestrator" / "x.json"
        self.assertEqual(hidden.read_text(encoding="utf-8"), "{}")

    def test_normal_file_still_reads_and_writes(self):
        path = "file_system/story_design/worldview.md"
        write = self.client.put("/api/workspaces/book/file", json={"path": path, "content": "# Worldview\n\nTides.\n"})
        self.assertEqual(write.status_code, 200)
        read = self.client.get("/api/workspaces/book/file", params={"path": path})
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["content"], "# Worldview\n\nTides.\n")


if __name__ == "__main__":
    unittest.main()
