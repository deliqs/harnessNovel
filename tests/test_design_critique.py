import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import prompt_trace
from core.prompt_trace import capture_prompts
from core.workspace import init_workspace
from training.design_critique import (
    critic_worker_count,
    load_points,
    points_instruction,
    run_critique,
)
from training.design_lenses import load_lenses


_MARKER = "UNIQUE-TIDAL-LOCK-MARKER"
_MERGE_POINTS = [
    {
        "title": "Name the opposition",
        "detail": "No rival is named.",
        "lenses": ["Depth", "Character stakes"],
    },
    {
        "title": "Hook the opening",
        "detail": "The first phase explains weather.",
        "lenses": ["Engagement"],
    },
]


class DesignCritiqueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HARNESS_NOVEL_HOME")
        self._old_workers = os.environ.get("HARNESS_NOVEL_CRITIC_WORKERS")
        self.root = Path(self._tmp.name)
        os.environ["HARNESS_NOVEL_HOME"] = str(self.root)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self._old_home
        if self._old_workers is None:
            os.environ.pop("HARNESS_NOVEL_CRITIC_WORKERS", None)
        else:
            os.environ["HARNESS_NOVEL_CRITIC_WORKERS"] = self._old_workers
        self._tmp.cleanup()

    def _design_dir(self):
        path = self.root / "book" / "file_system" / "story_design"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_concept(self):
        base = self._design_dir()
        files = {
            "worldview.md": "# Worldview\n\n%s\n" % _MARKER,
            "rough_outline.md": "# Rough outline\n\nA tide-born hero.\n",
            "stage_outline.md": "# Phase outline\n\n## Phase 1: Arrival\n",
        }
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")
        return {name: (base / name).read_bytes() for name in files}

    def _ws(self):
        return init_workspace("book")

    def _fake_llm(self, fail_lenses=None, invalid_lenses=None, on_call=None):
        fail_lenses = set(fail_lenses or [])
        invalid_lenses = set(invalid_lenses or [])

        def fake(llm, prompt, label, cancel_event=None):
            if on_call:
                on_call(prompt, label)
            if "merge" in label:
                return json.dumps({"points": _MERGE_POINTS})
            for name in fail_lenses:
                if "(%s)" % name in label:
                    raise RuntimeError("critic failed")
            for name in invalid_lenses:
                if "(%s)" % name in label:
                    return "not-json {"
            return json.dumps({
                "findings": [{
                    "title": "Thin factions",
                    "detail": "No named opposition.",
                    "evidence": "opening worldview paragraph",
                }],
            })

        return fake

    def _run(self, fake, **kwargs):
        ws = self._ws()
        with patch("training.design_critique._require_llm", return_value=object()), patch(
            "training.design_critique._call_design_llm", side_effect=fake,
        ) as call:
            result = run_critique(ws, "concept", "What is weak across the design?", **kwargs)
        return result, call, ws

    def test_worker_count_env_parsing(self):
        cases = [
            (None, 2),
            ("", 2),
            ("abc", 2),
            ("2.5", 2),
            ("1", 1),
            ("2", 2),
            ("8", 8),
            ("9", 8),
            ("0", 1),
            ("-3", 1),
            (" 3 ", 3),
        ]
        for raw, expected in cases:
            if raw is None:
                os.environ.pop("HARNESS_NOVEL_CRITIC_WORKERS", None)
            else:
                os.environ["HARNESS_NOVEL_CRITIC_WORKERS"] = raw
            self.assertEqual(critic_worker_count(), expected, raw)

    def test_critique_saves_numbered_points_and_markdown(self):
        self._write_concept()
        progress = []
        result, _call, ws = self._run(
            self._fake_llm(),
            progress_callback=lambda phase, completed, total, detail: progress.append(
                (phase, completed, total, detail),
            ),
        )
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(result["points"][0]["number"], 1)
        self.assertEqual(result["points"][1]["number"], 2)
        self.assertEqual(result["failed_lenses"], [])
        markdown = result["answer_md"]
        self.assertIn(
            "1. **Name the opposition** — No rival is named. *(Depth, Character stakes)*",
            markdown,
        )
        self.assertNotIn("Lenses:", markdown)
        self.assertIn('\n\nYou can say e.g. "apply 2 and 4"', markdown)

        saved = load_points(ws, "concept")
        self.assertEqual(saved["scope"], "concept")
        self.assertEqual(saved["message"], "What is weak across the design?")
        self.assertIn("created_at", saved)
        self.assertEqual(saved["points"][0]["number"], 1)
        self.assertEqual(saved["points"][0]["title"], "Name the opposition")
        self.assertTrue((self._design_dir() / "critique_points_concept.json").is_file())

        totals = {item[2] for item in progress}
        self.assertEqual(totals, {len(load_lenses(ws)) + 1})
        self.assertTrue(all(item[0] == "critics" for item in progress))
        reviewed = {item[3] for item in progress if str(item[3]).endswith("reviewed")}
        self.assertIn("Depth reviewed", reviewed)

    def test_design_files_unchanged_after_critique(self):
        before = self._write_concept()
        self._run(self._fake_llm())
        base = self._design_dir()
        for name, data in before.items():
            self.assertEqual((base / name).read_bytes(), data)

    def test_merge_prompt_omits_design_file_text(self):
        self._write_concept()
        prompts = {"critic": [], "merge": []}

        def on_call(prompt, label):
            if "merge" in label:
                prompts["merge"].append(prompt)
            else:
                prompts["critic"].append(prompt)

        self._run(self._fake_llm(on_call=on_call))
        self.assertTrue(prompts["critic"])
        self.assertEqual(len(prompts["merge"]), 1)
        for prompt in prompts["critic"]:
            self.assertIn(_MARKER, prompt)
        self.assertNotIn(_MARKER, prompts["merge"][0])
        self.assertNotIn("A tide-born hero.", prompts["merge"][0])
        self.assertNotIn("## Phase 1: Arrival", prompts["merge"][0])

    def test_one_failing_critic_is_skipped_and_named(self):
        self._write_concept()
        result, _call, _ws = self._run(self._fake_llm(fail_lenses=["Engagement"]))
        self.assertEqual(result["failed_lenses"], ["Engagement"])
        self.assertIn("\n\nSkipped lenses: Engagement.\n\n", result["answer_md"])
        self.assertEqual(len(result["points"]), 2)

    def test_all_failing_critics_raise(self):
        self._write_concept()
        with patch("training.design_critique._require_llm", return_value=object()), patch(
            "training.design_critique._call_design_llm", return_value="not-json {",
        ):
            with self.assertRaises(RuntimeError):
                run_critique(self._ws(), "concept", "Review the design.")

    def test_stop_event_returns_without_saving(self):
        self._write_concept()
        stop_event = threading.Event()
        stop_event.set()
        result, call, ws = self._run(self._fake_llm(), stop_event=stop_event)
        self.assertTrue(result.get("stopped"))
        call.assert_not_called()
        self.assertIsNone(load_points(ws, "concept"))
        self.assertFalse((self._design_dir() / "critique_points_concept.json").is_file())

    def test_no_more_than_capped_critics_run_at_once(self):
        self._write_concept()
        os.environ["HARNESS_NOVEL_CRITIC_WORKERS"] = "2"
        lock = threading.Lock()
        current = {"n": 0, "max": 0}

        def fake(llm, prompt, label, cancel_event=None):
            if "merge" in label:
                return json.dumps({"points": _MERGE_POINTS})
            with lock:
                current["n"] += 1
                current["max"] = max(current["max"], current["n"])
            time.sleep(0.08)
            with lock:
                current["n"] -= 1
            return json.dumps({
                "findings": [{"title": "Thin factions", "detail": "x", "evidence": "y"}],
            })

        self._run(fake)
        self.assertLessEqual(current["max"], 2)
        self.assertGreaterEqual(current["max"], 2)

    def test_trace_contextvar_is_visible_inside_critic_calls(self):
        self._write_concept()
        seen = []
        marker = lambda event: None

        def fake(llm, prompt, label, cancel_event=None):
            seen.append(prompt_trace._TRACE_CALLBACK.get())
            if "merge" in label:
                return json.dumps({"points": _MERGE_POINTS})
            return json.dumps({
                "findings": [{"title": "Thin factions", "detail": "x", "evidence": "y"}],
            })

        with capture_prompts(marker):
            self._run(fake)
        self.assertTrue(seen)
        self.assertTrue(all(callback is marker for callback in seen))

    def test_points_instruction_text_and_missing(self):
        ws = self._ws()
        empty = points_instruction(ws, "concept", [1, 2])
        self.assertEqual(empty["text"], "")
        self.assertEqual(empty["missing"], [1, 2])

        self._write_concept()
        self._run(self._fake_llm())
        result = points_instruction(ws, "concept", [2, 99, 1, 1])
        self.assertIn("[Critique points to apply]", result["text"])
        self.assertIn("2. Hook the opening: The first phase explains weather.", result["text"])
        self.assertIn("1. Name the opposition: No rival is named.", result["text"])
        self.assertLess(
            result["text"].index("2. Hook the opening"),
            result["text"].index("1. Name the opposition"),
        )
        self.assertEqual(result["missing"], [99])
        none_matched = points_instruction(ws, "concept", [8])
        self.assertEqual(none_matched["text"], "")
        self.assertEqual(none_matched["missing"], [8])

    def test_concept_points_are_missing_under_stage(self):
        self._write_concept()
        self._run(self._fake_llm())
        ws = self._ws()
        concept = points_instruction(ws, "concept", [1, 2])
        self.assertTrue(concept["text"])
        self.assertEqual(concept["missing"], [])
        stage = points_instruction(ws, "stage", [1, 2])
        self.assertEqual(stage["text"], "")
        self.assertEqual(stage["missing"], [1, 2])
        self.assertIsNone(load_points(ws, "stage"))
        self.assertIsNotNone(load_points(ws, "concept"))

    def test_no_design_files_raises(self):
        with patch("training.design_critique._require_llm", return_value=object()):
            with self.assertRaises(RuntimeError):
                run_critique(self._ws(), "concept", "What is weak?")


if __name__ == "__main__":
    unittest.main()
