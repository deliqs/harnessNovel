import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.workspace import NovelWorkspace
from tests.test_story_arc_ledger import ARC_ONE, ARC_THREE, ARC_TWO, build_arc
from training.adaptive_builder import gen_story_arcs
from training.story_arc_ledger import AUTHOR_BEGIN, AUTHOR_END, render_author_brief
from training.story_arc_review import EXCERPT_BEGIN, EXCERPT_END, render_outcomes
from webui.arc_chat import ArcsChatManager


ARC1 = build_arc(ARC_ONE)
ARC2 = build_arc(ARC_TWO)
ARC3 = build_arc(ARC_THREE)
DUPLICATE = build_arc(dict(ARC_ONE, idx=2, start=6, end=10))
BROKEN_ARC1 = "【Arc1: Chapters 1-5 | Broken】\nPlot function: too short."
BRIEF = "Arc 2: open at the harbour"
TARGET_CHARS = 800
ACTIVE = {"running", "pausing", "paused", "stopping"}


def _plans():
    return [
        {"idx": idx, "start_ch": start, "end_ch": end, "stage_story_plan": "plan",
         "arc_obligations": [], "chapter_beats": []}
        for idx, start, end in ((1, 1, 5), (2, 6, 10), (3, 11, 15))
    ]


class _TempHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HARNESS_NOVEL_HOME")
        os.environ["HARNESS_NOVEL_HOME"] = self.tmp.name
        self.ws = NovelWorkspace("book")
        self.ws.ensure_dirs()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HARNESS_NOVEL_HOME", None)
        else:
            os.environ["HARNESS_NOVEL_HOME"] = self.old_home
        self.tmp.cleanup()


class StoryArcRunTestBase(_TempHomeTest):
    """A 15-chapter stage with three planned arcs; _run fakes the arc writer with fixed responses."""

    def setUp(self):
        super().setUp()
        design = os.path.join(self.ws.file_system, "story_design")
        os.makedirs(design, exist_ok=True)
        with open(os.path.join(design, "long_mainline.md"), "w", encoding="utf-8") as handle:
            handle.write("Wren keeps the harbor lit.\n")
        with open(os.path.join(design, "stage_roadmap.md"), "w", encoding="utf-8") as handle:
            handle.write("# Stage 1: Harbor\nPlanned chapters: 15\n")
        self.arc_dir = os.path.join(self.ws.file_system, "story_arcs", "vol_01")

    def _run(self, responses, **kwargs):
        with patch("training.adaptive_builder._get_critic_llm", return_value=object()), \
                patch("training.adaptive_builder._story_arc_plans_for_volume", return_value=_plans()), \
                patch("training.adaptive_builder._reference_story_arc_average_chars", return_value=TARGET_CHARS), \
                patch("training.adaptive_builder._generate_story_arc", side_effect=responses) as writer:
            result = gen_story_arcs(self.ws, volume=1, **kwargs)
        contexts = [call.kwargs["generation_context"] for call in writer.call_args_list]
        return result, contexts

    def _arc_files(self):
        return sorted(
            name for name in os.listdir(self.arc_dir)
            if name.startswith("arc_") and name.endswith(".md")
        )

    def _statuses(self, result):
        return [(item["arc"], item["status"]) for item in result["outcomes"]]


class GenStoryArcsReviewTests(StoryArcRunTestBase):
    def test_sibling_collision_is_retried_with_review_note(self):
        result, contexts = self._run([ARC1, DUPLICATE, ARC2, ARC3])
        self.assertEqual(len(contexts), 4)
        self.assertEqual(contexts[1]["review_note"], "")
        note = contexts[2]["review_note"]
        instruction, excerpt = note.split(EXCERPT_BEGIN, 1)
        self.assertIn("arc 1", instruction)
        self.assertNotIn(ARC_ONE["boundary"].rstrip("."), instruction)
        self.assertIn(ARC_ONE["boundary"].rstrip("."), excerpt.split(EXCERPT_END, 1)[0])
        self.assertEqual(note.count(EXCERPT_BEGIN), 1)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "retried"), (3, "written")])
        self.assertIn("arc 1", result["outcomes"][1]["reason"])
        self.assertEqual(self._arc_files(), [
            "arc_001_ch001_005.md", "arc_002_ch006_010.md", "arc_003_ch011_015.md",
        ])
        self.assertTrue(os.path.isfile(os.path.join(self.arc_dir, "arcs_index.json")))
        self.assertEqual(len(result["artifacts"]), 3)
        self.assertTrue(result["adjustment_note"].endswith(render_outcomes(result["outcomes"])))

    def test_repeated_collision_rejects_and_stops_the_run(self):
        result, contexts = self._run([ARC1, DUPLICATE, DUPLICATE])
        self.assertEqual(len(contexts), 3)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "rejected")])
        self.assertIn("arc 1", result["outcomes"][1]["reason"])
        self.assertEqual(self._arc_files(), ["arc_001_ch001_005.md"])
        self.assertFalse(os.path.exists(os.path.join(self.arc_dir, "arcs_index.json")))
        note = result["adjustment_note"]
        self.assertTrue(note.endswith(render_outcomes(result["outcomes"])))
        self.assertIn("unit 2", note.splitlines()[0])
        self.assertIn("arc 1", note.splitlines()[0])
        self.assertIn("- arc 2: rejected", note)
        self.assertFalse(result["stopped"])

    def test_validation_failure_is_retried_with_reasons(self):
        result, contexts = self._run([BROKEN_ARC1, ARC1, ARC2, ARC3])
        self.assertEqual(len(contexts), 4)
        self.assertIn("Missing arc fields", contexts[1]["review_note"])
        self.assertEqual(self._statuses(result), [(1, "retried"), (2, "written"), (3, "written")])
        self.assertIn("Missing arc fields", result["outcomes"][0]["reason"])

    def test_empty_output_stops_the_run(self):
        result, contexts = self._run([ARC1, ""])
        self.assertEqual(len(contexts), 2)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "no_output")])
        self.assertEqual(self._arc_files(), ["arc_001_ch001_005.md"])

    def test_existing_arc_is_kept_and_counts_as_sibling(self):
        os.makedirs(self.arc_dir, exist_ok=True)
        with open(os.path.join(self.arc_dir, "arc_001_ch001_005.md"), "w", encoding="utf-8") as handle:
            handle.write(ARC1)
        result, contexts = self._run([DUPLICATE, ARC2, ARC3])
        self.assertEqual(self._statuses(result), [(1, "kept"), (2, "retried"), (3, "written")])
        self.assertIn(ARC_ONE["next_bind"], contexts[0]["prior_arc_ledger"])

    def test_forced_halt_keeps_existing_units_byte_identical(self):
        os.makedirs(self.arc_dir, exist_ok=True)
        existing = {
            "arc_002_ch006_010.md": b"trusted arc two\r\nkept as is\n",
            "arc_003_ch011_015.md": b"trusted arc three\n\n",
        }
        for name, data in existing.items():
            with open(os.path.join(self.arc_dir, name), "wb") as handle:
                handle.write(data)
        progress = []
        result, contexts = self._run(
            [ARC1, DUPLICATE, DUPLICATE], force=True,
            progress_callback=lambda *args: progress.append(args),
        )
        self.assertEqual(len(contexts), 3)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "rejected")])
        for name, data in existing.items():
            with open(os.path.join(self.arc_dir, name), "rb") as handle:
                self.assertEqual(handle.read(), data)
            self.assertFalse(os.path.exists(os.path.join(self.arc_dir, name + ".provenance.json")))
        self.assertFalse(os.path.exists(os.path.join(self.arc_dir, "arcs_index.json")))
        self.assertEqual(progress[-1][0], "completed")
        self.assertIn("stopped at unit 2", progress[-1][3])

    def test_prompt_context_carries_ledger_and_continuity_rule(self):
        _, contexts = self._run([ARC1, ARC2, ARC3])
        first, second = contexts[0], contexts[1]
        self.assertIn("previous stage", first["continuity_rule"])
        self.assertEqual(first["prior_arc_ledger"], "(no prior arcs in this stage)")
        self.assertEqual(first["author_brief"], "(none)")
        self.assertIn(ARC_ONE["next_bind"], second["prior_arc_ledger"])
        self.assertIn("next bind", second["continuity_rule"])
        self.assertNotIn(ARC_ONE["next_bind"], first["prior_arc_ledger"])

    def test_author_brief_reaches_arc_prompt_context(self):
        _, contexts = self._run([ARC1, ARC2, ARC3], author_brief=BRIEF)
        brief = contexts[1]["author_brief"]
        self.assertEqual(brief, render_author_brief(BRIEF, 2))
        self.assertLess(brief.index(AUTHOR_BEGIN), brief.index(BRIEF))
        self.assertLess(brief.index(BRIEF), brief.index(AUTHOR_END))


class StartMessageAuthorBriefTests(_TempHomeTest):
    def _wait(self, manager):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = manager.job_status("book", 1)["status"]
            if status not in ACTIVE:
                return status
            time.sleep(0.01)
        self.fail("story-arc chat job did not finish")

    def test_initial_and_resume_runs_pass_the_first_user_turn(self):
        manager = ArcsChatManager(Path(self.tmp.name))
        captured = []

        def fake_gen_story_arcs(ws, **kwargs):
            captured.append(kwargs)
            return {"artifacts": [], "adjustment_note": "ok"}

        with patch("training.adaptive_builder.gen_story_arcs", side_effect=fake_gen_story_arcs):
            manager.start_message("book", 1, "  %s  " % BRIEF)
            self.assertEqual(self._wait(manager), "completed")
            manager.start_message("book", 1, "Try again")
            self.assertEqual(self._wait(manager), "completed")
            manager.start_message(
                "book", 1, "Continue generating unfinished story arcs", resume_incomplete=True,
            )
            self.assertEqual(self._wait(manager), "completed")
        self.assertEqual([call["author_brief"] for call in captured], [BRIEF, BRIEF, BRIEF])


if __name__ == "__main__":
    unittest.main()
