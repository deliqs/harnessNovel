"""Offline tests for the arcs chat route: arc, mode and cascade are validated before a job starts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from webui.app import create_app


URL = "/api/workspaces/book/arcs/1/chat"


class ArcsChatRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        web_home = self.root / "web-home"
        web_home.mkdir()
        with patch("webui.app.WEB_HOME", web_home), patch(
            "webui.app.WEB_SETTINGS_PATH", web_home / "settings.json",
        ):
            app = create_app(str(self.root))
        self.start = MagicMock(return_value={"status": "running"})
        app.state.runtime.arcs_chat.start_message = self.start
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def _post(self, **fields):
        return self.client.post(URL, json=dict(message="Rewrite arc 2", **fields))

    def test_arc_must_be_a_positive_integer(self):
        for arc in ("two", "2", 0, -1, 2.5, True):
            response = self._post(arc=arc)
            self.assertEqual(response.status_code, 400, arc)
            self.assertIn("arc", response.json()["detail"])
        self.start.assert_not_called()

    def test_mode_must_be_revise_or_regenerate(self):
        for mode in ("rewrite", "REVISE", "", 1):
            response = self._post(mode=mode)
            self.assertEqual(response.status_code, 400, mode)
            self.assertIn("mode", response.json()["detail"])
        self.start.assert_not_called()

    def test_cascade_must_be_a_json_boolean(self):
        for cascade in ("false", "true", 0, 1):
            response = self._post(cascade=cascade)
            self.assertEqual(response.status_code, 400, cascade)
            self.assertIn("cascade", response.json()["detail"])
        self.start.assert_not_called()

    def test_valid_options_reach_the_arcs_chat_manager(self):
        response = self._post(arc=2, mode="regenerate", cascade=False)
        self.assertEqual(response.status_code, 200)
        self.start.assert_called_once_with(
            "book", 1, "Rewrite arc 2", arc=2, mode="regenerate", cascade=False,
        )

    def test_absent_options_keep_the_default_cascade(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.start.assert_called_once_with(
            "book", 1, "Rewrite arc 2", arc=None, mode=None, cascade=True,
        )


if __name__ == "__main__":
    unittest.main()
