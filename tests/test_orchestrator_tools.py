"""Offline tests for the shared orchestrator tools and the frozen contracts."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from webui.orchestrator import compaction
from webui.orchestrator.deps import JOB_URLS, PHASES, JobRef, OrchestratorDeps
from webui.orchestrator.registry import PHASE_MODULES, build_agent, instructions_for
from webui.orchestrator.tools.shared import (
    MAX_RESULT_CHARS,
    RESULT_CHARS,
    job_status,
    list_artifacts,
    read_artifact,
)
from webui.task_runner import TaskRecord, WorkspaceStore


class SharedToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        story = self.root / "book" / "file_system" / "story_design"
        story.mkdir(parents=True)
        (story / "worldview.md").write_text("# Worldview\n\n" + "tide " * 1000, encoding="utf-8")
        (self.root / "secret.md").write_text("outside", encoding="utf-8")
        self.runtime = SimpleNamespace(store=WorkspaceStore(self.root), tasks=MagicMock())
        self.ctx = self._ctx({})

    def tearDown(self):
        self._tmp.cleanup()

    def _ctx(self, ui_state, phase="world"):
        deps = OrchestratorDeps(runtime=self.runtime, workspace="book", phase=phase, ui_state=ui_state)
        return SimpleNamespace(deps=deps)

    def test_read_artifact_rejects_paths_outside_the_workspace(self):
        for path in ("../secret.md", "file_system/../../secret.md", str(self.root / "secret.md"), ""):
            with self.assertRaises(ModelRetry):
                read_artifact(self.ctx, path)

    def test_list_artifacts_rejects_prefixes_outside_the_workspace(self):
        for prefix in ("../", "file_system/../..", "/etc"):
            with self.assertRaises(ModelRetry):
                list_artifacts(self.ctx, prefix)

    def test_thread_files_stay_hidden_from_the_artifact_tools(self):
        thread_dir = self.root / "book" / ".orchestrator" / "world"
        thread_dir.mkdir(parents=True)
        (thread_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

        self.assertNotIn(".orchestrator", list_artifacts(self.ctx))
        for path in (".orchestrator/world/transcript.jsonl", "file_system/../.orchestrator/world/transcript.jsonl"):
            with self.assertRaises(ModelRetry):
                read_artifact(self.ctx, path)
        with self.assertRaises(ModelRetry):
            list_artifacts(self.ctx, ".orchestrator")

    def test_list_artifacts_walks_the_folder_and_caps_the_count(self):
        drafts = self.root / "book" / "file_system" / "drafts"
        drafts.mkdir(parents=True)
        for index in range(250):
            (drafts / ("ch%03d.md" % index)).write_text("x", encoding="utf-8")

        listing = list_artifacts(self.ctx, "file_system/drafts")
        with patch("webui.orchestrator.tools.shared.MAX_WALKED_FILES", 10):
            walked = list_artifacts(self.ctx, "file_system/drafts")

        lines = listing.splitlines()
        self.assertEqual(lines[0], "file_system/drafts/ch000.md (1 bytes)")
        self.assertLessEqual(len(listing), MAX_RESULT_CHARS)
        self.assertRegex(lines[-1], r"^\[\d+ more files not listed; list a narrower folder\]$")
        self.assertEqual(len(lines) - 1 + int(lines[-1][1:].split()[0]), 250)
        self.assertNotIn("worldview.md", listing)
        self.assertEqual(walked.splitlines()[-1], "[More files not listed; list a narrower folder]")

    def test_read_artifact_pages_through_a_long_file(self):
        path = "file_system/story_design/worldview.md"
        text = "# Worldview\n\n" + "tide " * 1000

        first = read_artifact(self.ctx, path, max_chars=50000)
        second = read_artifact(self.ctx, path, offset=MAX_RESULT_CHARS)
        tail = read_artifact(self.ctx, path, max_chars=100, from_end=True)
        before_tail = read_artifact(self.ctx, path, max_chars=100, offset=100, from_end=True)

        self.assertEqual(first, "[characters 0-2000 of %d]\n%s" % (len(text), text[:2000]))
        self.assertEqual(second, "[characters 2000-4000 of %d]\n%s" % (len(text), text[2000:4000]))
        self.assertEqual(tail.splitlines()[1:], text[-100:].splitlines())
        self.assertTrue(tail.startswith("[characters %d-%d of" % (len(text) - 100, len(text))))
        self.assertTrue(before_tail.endswith(text[-200:-100]))

    def test_read_artifact_returns_a_short_file_whole(self):
        (self.root / "book" / "file_system" / "note.md").write_text("Short note.", encoding="utf-8")
        self.assertEqual(read_artifact(self.ctx, "file_system/note.md"), "Short note.")
        self.assertEqual(read_artifact(self.ctx, "file_system/note.md", from_end=True), "Short note.")

    def test_read_artifact_missing_file_asks_the_model_to_retry(self):
        with self.assertRaises(ModelRetry):
            read_artifact(self.ctx, "file_system/nope.md")

    def test_list_artifacts_filters_by_folder(self):
        self.assertIn("file_system/story_design/worldview.md", list_artifacts(self.ctx, "file_system/"))
        self.assertIn("No folder", list_artifacts(self.ctx, "file_system/story"))

    def test_job_status_defaults_volume_and_arc_from_ui_state(self):
        self.runtime.draft_chat = MagicMock()
        self.runtime.draft_chat.job_status.return_value = {"status": "running", "items": [1, 2], "message": "ch 3"}

        result = job_status(self._ctx({"volume": 2, "arc": 4}), "drafts")

        self.runtime.draft_chat.job_status.assert_called_once_with("book", 2, 4)
        self.assertIn('"status": "running"', result)
        self.assertNotIn("items", result)

    def test_job_status_defaults_the_design_scope_from_the_phase(self):
        self.runtime.design_chat = MagicMock()
        self.runtime.design_chat.job_status.return_value = {"status": "idle"}

        job_status(self._ctx({}, phase="design"), "design")
        job_status(self._ctx({}, phase="stage"), "design")
        job_status(self._ctx({}, phase="stage"), "design", scope="concept")

        calls = [call.args for call in self.runtime.design_chat.job_status.call_args_list]
        self.assertEqual(calls, [("book", "concept"), ("book", "stage"), ("book", "concept")])

    def test_job_status_gives_the_finished_jobs_latest_note_tail_first(self):
        self.runtime.design_chat = MagicMock()
        self.runtime.design_chat.job_status.return_value = {"status": "completed", "result": {"mode": "critique"}}
        critique = "Critique: " + "x" * 3000 + " END"
        self.runtime.design_chat.get.return_value.turns = [
            {"role": "user", "content": "Critique it"},
            {"role": "assistant", "content": "Old answer"},
            {"role": "user", "content": "Again"},
            {"role": "assistant", "content": critique},
        ]

        result = json.loads(job_status(self._ctx({}, phase="design"), "design"))["result"]

        self.runtime.design_chat.get.assert_called_once_with("book", "concept")
        self.assertTrue(result.endswith(" END"))
        self.assertTrue(result.startswith("[%d earlier characters left out]" % (len(critique) - RESULT_CHARS)))
        self.assertEqual(len(result.split("\n", 1)[1]), RESULT_CHARS)

    def test_job_status_leaves_the_result_out_while_the_job_runs(self):
        self.runtime.arcs_chat = MagicMock()
        self.runtime.arcs_chat.job_status.return_value = {"status": "running", "message": "arc 2 of 5"}

        report = json.loads(job_status(self._ctx({"volume": 1}), "arcs"))

        self.assertNotIn("result", report)
        self.runtime.arcs_chat.get.assert_not_called()

    def test_job_status_notes_a_live_job_and_refuses_a_repeat(self):
        self.runtime.arcs_chat = MagicMock()
        self.runtime.arcs_chat.job_status.return_value = {"status": "running", "message": "arc 2 of 5"}
        ctx = self._ctx({"volume": 1})
        note = "Still running. End your turn now; an automatic message will tell you when it finishes."

        first = json.loads(job_status(ctx, "arcs"))
        repeat = json.loads(job_status(ctx, "arcs", volume=1))
        other = json.loads(job_status(ctx, "arcs", volume=2))

        self.assertEqual(first["note"], note)
        self.assertNotIn("result", first)
        self.assertEqual(repeat, {"status": "refused", "message": note})
        self.assertNotIn("arc 2 of 5", json.dumps(repeat))
        self.assertEqual(other["status"], "running")
        self.assertEqual(other["note"], note)

    def test_job_status_never_refuses_a_finished_job(self):
        self.runtime.arcs_chat = MagicMock()
        self.runtime.arcs_chat.job_status.return_value = {"status": "completed"}
        self.runtime.arcs_chat.get.return_value.turns = [{"role": "assistant", "content": "Arcs ready."}]
        ctx = self._ctx({"volume": 1})

        first = json.loads(job_status(ctx, "arcs"))
        second = json.loads(job_status(ctx, "arcs"))

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["result"], "Arcs ready.")
        self.assertNotIn("note", first)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["result"], "Arcs ready.")

        live = self._ctx({"volume": 3})
        self.runtime.arcs_chat.job_status.return_value = {"status": "running"}
        job_status(live, "arcs")
        self.runtime.arcs_chat.job_status.return_value = {"status": "completed"}
        done = json.loads(job_status(live, "arcs"))
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"], "Arcs ready.")

    def test_job_status_allows_the_same_live_job_on_fresh_deps(self):
        self.runtime.world_chat = MagicMock()
        self.runtime.world_chat.job_status.return_value = {"status": "paused", "message": "waiting"}
        note = "Still running. End your turn now; an automatic message will tell you when it finishes."
        job_status(self._ctx({}), "world")

        report = json.loads(job_status(self._ctx({}), "world"))

        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["note"], note)
        self.assertNotEqual(report["status"], "refused")

    def test_job_status_gives_a_finished_tasks_message(self):
        self.runtime.tasks.get.return_value = TaskRecord(
            id="t1", type="world_build", label="Build", workspace="book", status="failed", message="Run failed (exit code 2)",
        )
        report = json.loads(job_status(self.ctx, "task", task_id="t1"))
        self.assertEqual(report["result"], "Run failed (exit code 2)")

    def test_job_status_asks_for_missing_arguments(self):
        with self.assertRaises(ModelRetry):
            job_status(self.ctx, "chapters", volume=1)
        with self.assertRaises(ModelRetry):
            job_status(self.ctx, "design")

    def test_job_status_only_reports_tasks_of_this_workspace(self):
        self.runtime.tasks.get.return_value = TaskRecord(id="t1", type="write", label="Write", workspace="other")
        with self.assertRaises(ModelRetry):
            job_status(self.ctx, "task", task_id="t1")
        self.runtime.tasks.get.return_value = TaskRecord(id="t1", type="write", label="Write", workspace="book")
        self.assertIn('"id": "t1"', job_status(self.ctx, "task", task_id="t1"))


class ContractTests(unittest.TestCase):
    def test_job_ref_to_dict_keeps_only_set_locators(self):
        ref = JobRef(kind="chapters", workspace="my book", volume=1, arc=2)
        self.assertEqual(ref.to_dict(), {"kind": "chapters", "workspace": "my book", "volume": 1, "arc": 2})
        self.assertEqual(ref.url(), "/api/workspaces/my%20book/chapters/1/2/job")
        self.assertEqual(JobRef(kind="task", workspace="book", task_id="abc").url(), "/api/tasks/abc")
        self.assertEqual(JobRef(kind="design", workspace="book", scope="stage").url(),
                         "/api/workspaces/book/design/stage/job")
        self.assertEqual(set(JOB_URLS), {"world", "design", "arcs", "chapters", "drafts", "task"})

    def test_job_ref_refuses_missing_locators(self):
        for fields in ({"kind": "arcs"}, {"kind": "chapters", "volume": 1}, {"kind": "task"},
                       {"kind": "design"}, {"kind": "design", "scope": "book"}, {"kind": "export"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                JobRef(workspace="book", **fields)
        self.assertEqual(JobRef(kind="world", workspace="book").to_dict(), {"kind": "world", "workspace": "book"})

    def test_every_phase_has_a_tool_module_and_builds_an_agent(self):
        self.assertEqual(tuple(PHASE_MODULES), PHASES)
        for phase, module in PHASE_MODULES.items():
            self.assertIsInstance(module.INSTRUCTIONS, str)
            self.assertIsInstance(module.TOOLS, list)
            agent = build_agent(phase, TestModel())
            self.assertTrue({"list_artifacts", "read_artifact", "job_status"} <= set(agent._function_toolset.tools))

    def test_instructions_send_the_model_to_job_status_after_a_job(self):
        for phase in PHASE_MODULES:
            text = instructions_for(phase)
            self.assertIn("call job_status", text)
            self.assertIn("Never poll or wait on a job you started", text)

    def test_compaction_target_reads_the_environment(self):
        old = os.environ.get(compaction.TARGET_ENV)
        try:
            os.environ[compaction.TARGET_ENV] = "123"
            self.assertEqual(compaction.target_tokens(), 123)
            os.environ[compaction.TARGET_ENV] = "junk"
            self.assertEqual(compaction.target_tokens(), compaction.DEFAULT_TARGET_TOKENS)
            os.environ.pop(compaction.TARGET_ENV)
            self.assertEqual(compaction.target_tokens(), 10000)
        finally:
            if old is None:
                os.environ.pop(compaction.TARGET_ENV, None)
            else:
                os.environ[compaction.TARGET_ENV] = old


if __name__ == "__main__":
    unittest.main()
