"""System-panel JSON validation: changelog cap must not abort generation."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from core.text_utils import parse_json_response
from core.workspace import NovelWorkspace
from training.adaptive_builder import (
    SYSTEM_PANEL_MAX_CHANGES,
    SYSTEM_PANEL_MAX_CHARS,
    _generate_chapter_system_panel,
    _mechanics_path,
    _panel_state_for_prompt,
    _read_json_file,
    _system_panel_chapter_path,
    _validate_system_panel_response,
    _write_json_file,
)


def _change(index):
    return {
        "field": f"field_{index}",
        "before": index,
        "after": index + 1,
        "reason": f"changed {index}",
    }


class SystemPanelValidationTests(unittest.TestCase):
    def test_accepts_max_changes(self):
        payload = {
            "panel": {"Name": "Esmee"},
            "changes": [_change(i) for i in range(SYSTEM_PANEL_MAX_CHANGES)],
        }
        result = _validate_system_panel_response(payload)
        self.assertEqual(len(result["changes"]), SYSTEM_PANEL_MAX_CHANGES)
        self.assertEqual(result["panel"], {"Name": "Esmee"})

    def test_truncates_extra_changes_instead_of_raising(self):
        payload = {
            "panel": {"Name": "Esmee", "Realm": "onset"},
            "changes": [_change(i) for i in range(SYSTEM_PANEL_MAX_CHANGES + 12)],
        }
        result = _validate_system_panel_response(payload)
        self.assertEqual(len(result["changes"]), SYSTEM_PANEL_MAX_CHANGES)
        self.assertEqual(result["changes"][0]["field"], "field_0")
        self.assertEqual(
            result["changes"][-1]["field"],
            f"field_{SYSTEM_PANEL_MAX_CHANGES - 1}",
        )
        self.assertEqual(result["panel"]["Realm"], "onset")

    def test_drops_trailing_changes_to_fit_size_cap(self):
        bulky = "x" * 2000
        payload = {
            "panel": {"Name": "Esmee"},
            "changes": [
                {
                    "field": f"field_{i}",
                    "before": bulky,
                    "after": bulky,
                    "reason": bulky,
                }
                for i in range(SYSTEM_PANEL_MAX_CHANGES)
            ],
        }
        raw = json.dumps(
            {"panel": payload["panel"], "changes": payload["changes"]},
            ensure_ascii=False,
        )
        self.assertGreater(len(raw), SYSTEM_PANEL_MAX_CHARS)
        result = _validate_system_panel_response(payload)
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertLessEqual(len(serialized), SYSTEM_PANEL_MAX_CHARS)
        self.assertLess(len(result["changes"]), SYSTEM_PANEL_MAX_CHANGES)
        self.assertEqual(result["panel"], {"Name": "Esmee"})

    def test_panel_alone_over_size_cap_still_raises(self):
        payload = {
            "panel": {"blob": "y" * (SYSTEM_PANEL_MAX_CHARS + 1)},
            "changes": [],
        }
        with self.assertRaisesRegex(ValueError, "too long"):
            _validate_system_panel_response(payload)

    def test_rejects_malformed_change_before_truncating(self):
        payload = {
            "panel": {"Name": "Esmee"},
            "changes": [
                {"field": "ok", "before": 1, "after": 2, "reason": "ok"},
                {"field": "", "before": 1, "after": 2, "reason": "bad"},
            ] + [_change(i) for i in range(SYSTEM_PANEL_MAX_CHANGES)],
        }
        with self.assertRaisesRegex(ValueError, r"changes\[2\]\.field"):
            _validate_system_panel_response(payload)


class ParseJsonResponseTests(unittest.TestCase):
    def test_extracts_object_from_preamble_and_trailing_text(self):
        raw = 'Here you go:\n{"panel": {"Name": "Esmee"}, "changes": []}\nThanks.'
        self.assertEqual(
            parse_json_response(raw),
            {"panel": {"Name": "Esmee"}, "changes": []},
        )

    def test_strips_trailing_commas_with_whitespace(self):
        raw = '{"panel": {"Name": "Esmee", }, "changes": [], }'
        self.assertEqual(
            parse_json_response(raw),
            {"panel": {"Name": "Esmee"}, "changes": []},
        )


class SystemPanelGenerateFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self._tmp.name
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()
        os.makedirs(os.path.join(self.ws.file_system, "mechanics"), exist_ok=True)
        self.previous_panel = {"Name": "Esmee", "Realm": "onset"}
        _write_json_file(_mechanics_path(self.ws, "system_panel.json"), {
            "enabled": True,
            "decided": True,
            "selection_mode": "enabled",
        })
        _write_json_file(_system_panel_chapter_path(self.ws, 1, 12), {
            "chapter": 12,
            "panel": self.previous_panel,
            "changes": [
                {"field": "Realm", "before": "a", "after": "onset", "reason": "x"},
            ],
        })

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_prompt_payload_omits_previous_changes(self):
        previous = _panel_state_for_prompt({
            "chapter": 12,
            "panel": self.previous_panel,
            "changes": [{"field": "Realm", "before": "a", "after": "onset", "reason": "x"}],
        })
        self.assertNotIn("changes", previous)
        self.assertEqual(previous["panel"], self.previous_panel)

    def test_inherits_previous_panel_when_json_always_invalid(self):
        llm = MagicMock()
        llm.generate.return_value = '{ "panel": { "Name"\n "Realm": "x" }, "changes": [] }'
        result = _generate_chapter_system_panel(llm, self.ws, 1, 13, "outline text")
        self.assertEqual(result["chapter"], 13)
        self.assertEqual(result["panel"], self.previous_panel)
        self.assertEqual(result["changes"], [])
        self.assertEqual(llm.generate.call_count, 3)
        written = _read_json_file(_system_panel_chapter_path(self.ws, 1, 13))
        self.assertEqual(written["panel"], self.previous_panel)
        self.assertEqual(written["changes"], [])

    def test_writes_valid_model_panel(self):
        llm = MagicMock()
        llm.generate.return_value = json.dumps({
            "panel": {"Name": "Esmee", "Realm": "named"},
            "changes": [{
                "field": "Realm",
                "before": "onset",
                "after": "named",
                "reason": "clinic",
            }],
        })
        result = _generate_chapter_system_panel(llm, self.ws, 1, 13, "outline text")
        self.assertEqual(result["panel"]["Realm"], "named")
        self.assertEqual(result["changes"][0]["field"], "Realm")
        self.assertEqual(llm.generate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
