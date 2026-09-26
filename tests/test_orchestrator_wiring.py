"""Offline tests for wiring the orchestrator chat into the wizard, and for the /history contract.

The static checks read index.html and the wizard scripts. The history tests build a thread
through the real backend (TestClient plus a scripted FunctionModel), then feed GET /history
through the browser reducer under node. Those skip when node is not installed.
"""
import json
import re
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic_ai import RunContext, Tool

from tests.orchestrator_fakes import OrchestratorAppCase, ScriptedModel, Thought, background, run_body, user
from webui.orchestrator.deps import PHASES, JobRef
from webui.orchestrator.tools import world
from webui.orchestrator.tools.results import done, job_started

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"
REDUCER = STATIC / "orchestrator-reducer.js"
ORCHESTRATOR_SCRIPTS = [
    "orchestrator-stream.js", "orchestrator-jobs.js", "orchestrator-reducer.js", "orchestrator-view.js",
    "orchestrator-chat.js", "orchestrator-mount.js",
]
PANEL_RENDERERS = {"renderArcsChat", "renderChaptersChat", "renderDraftChat", "renderDesignChat", "renderWorldChat"}


class WizardWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.wizard = (STATIC / "wizard-v0.js").read_text(encoding="utf-8")
        cls.world = (STATIC / "world-chat.js").read_text(encoding="utf-8")
        cls.mount = (STATIC / "orchestrator-mount.js").read_text(encoding="utf-8")

    def test_index_loads_the_chat_scripts_and_css_before_the_wizard(self):
        wizard = self.index.index('<script src="/assets/wizard-v0.js?v=')
        positions = [re.search(r'<script src="/assets/%s\?v=\d+"></script>' % re.escape(name), self.index).start()
                     for name in ORCHESTRATOR_SCRIPTS]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[-1], wizard)
        self.assertRegex(self.index, r'<link rel="stylesheet" href="/assets/orchestrator-chat\.css\?v=\d+" />')
        self.assertNotIn('type="module"', self.index)
        for legacy in ("/assets/world-chat.js?", "/assets/design-lenses.js?"):
            self.assertGreater(self.index.index(legacy), wizard)

    def mounts(self):
        calls = []
        for source in (self.wizard, self.world):
            for match in re.finditer(r"mountPhaseChat\((\w+), ([^)]*)\);", source):
                start = source.rfind("\nfunction ", 0, match.start())
                name = re.match(r"\nfunction (\w+)", source[start:]).group(1)
                calls.append((name, match.group(2)))
        return calls

    def test_each_panel_renderer_mounts_its_phase(self):
        calls = self.mounts()
        self.assertEqual({name for name, _ in calls}, PANEL_RENDERERS)
        phases = set()
        for _, argument in calls:
            phases.update(re.findall(r'"(\w+)"', argument))
        phases.discard("concept")
        self.assertEqual(phases, set(PHASES))
        self.assertIn(("renderDesignChat", 'scope === "concept" ? "design" : "stage"'), calls)

    def test_reference_step_has_no_chat(self):
        self.assertNotIn("reference", PHASES)
        self.assertNotIn("reference", " ".join(argument for _, argument in self.mounts()))
        reload_block = self.mount[self.mount.index("const RELOAD = {"):self.mount.index("};", self.mount.index("const RELOAD = {"))]
        self.assertEqual(set(re.findall(r"^\s+(\w+): \(\) =>", reload_block, re.M)), set(PHASES))

    def test_switching_workspace_closes_the_other_chats(self):
        select = self.wizard[self.wizard.index("async function selectWorkspace(name) {"):]
        self.assertLess(select.index("closeOtherPhaseChats(wizardState.workspace);"), select.index("renderActiveStep();"))

    def test_legacy_flag_keeps_the_old_chat(self):
        self.assertIn('const LEGACY_KEY = "harnessNovel.legacyChat";', self.mount)
        self.assertIn('root.localStorage.getItem(LEGACY_KEY) === "1"', self.mount)
        self.assertIn("if (legacyChat() || !node || !wizardState.workspace) return null;", self.mount)
        for fragment in ("sendArcsMessage", "sendChaptersMessage", "sendDraftMessage", "sendDesignMessage", "sendWorldMessage"):
            self.assertIn("async function %s(" % fragment, self.wizard + self.world)

    def test_glue_uses_uploads_hooks_and_no_html_strings(self):
        for fragment in ("uploadFile(file)", "[attached upload ${file.upload_id}: ${file.name}]", "getUiState",
                         "onJobStarted", "onJobFinished", "refreshWorkspaceArtifacts()", "composeMessage",
                         "renderMarkdown: markdownPreview", "destroyOtherWorkspaces(workspace || null)"):
            self.assertIn(fragment, self.mount)
        self.assertNotRegex(self.mount, r"innerHTML|outerHTML|insertAdjacentHTML")


class NodeHistoryCase(OrchestratorAppCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")
        super().setUp()

    def reduce_history(self, body="out(S.fromHistory(history));"):
        script = "const S = require(%s); const history = %s; const out = (v) => process.stdout.write(JSON.stringify(v));\n%s" % (
            json.dumps(str(REDUCER)), json.dumps(self.history()), body)
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


def erase_notes(path: str) -> str:
    """Erase a notes file."""
    return done("Erased %s." % path)


def start_world_job(ctx: RunContext) -> str:
    """Start a world job."""
    return job_started(JobRef(kind="world", workspace=ctx.deps.workspace), "World chat started.")


TEST_TOOLS = [Tool(erase_notes, requires_approval=True), Tool(start_world_job)]


class HistoryContractTests(NodeHistoryCase):
    def test_backend_transcript_loads_into_the_reducer(self):
        self.use_model(ScriptedModel([
            Thought("Let me look.", "Looking."),
            ("erase_notes", {"path": "notes.md"}), "Kept them.",
            ("start_world_job", {}), "Started.",
            "Reviewed.",
            RuntimeError("boom"),
        ]))
        with patch.object(world, "TOOLS", TEST_TOOLS):
            self.post_turn(run_body([user("Hi")], thinking=True))
            interrupt = self.post_turn(run_body([user("Erase my notes.")]))[-1]["outcome"]["interrupts"][0]
            deny = {"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": False, "reason": "No"}}
            self.post_turn(run_body(resume=[deny]))
            job_call = next(e for e in self.post_turn(run_body([user("Build the world.")])) if e["type"] == "TOOL_CALL_START")
            auto = run_body([user('[auto] The job "world" finished with status completed: ok.')])
            auto["forwardedProps"]["autoContinue"] = {"toolCallId": job_call["toolCallId"], "jobKey": "world@t1"}
            self.post_turn(auto)
            self.post_turn(run_body([user("Again")]))

        state = self.reduce_history()
        items = state["items"]
        self.assertEqual(
            [(item["kind"], item.get("activityType")) for item in items],
            [("user", None), ("reasoning", None), ("assistant", None), ("user", None), ("tool", None),
             ("approval", None), ("assistant", None), ("user", None), ("tool", None), ("assistant", None),
             ("activity", "auto_continue"), ("assistant", None), ("user", None), ("activity", "run_error")],
        )
        self.assertEqual(items[1]["text"], "Let me look.")
        erase, approval, job_tool = items[4], items[5], items[8]
        self.assertEqual((erase["name"], json.loads(erase["args"])), ("erase_notes", {"path": "notes.md"}))
        self.assertIn("No", erase["result"])
        self.assertTrue(erase["denied"])
        self.assertEqual((approval["toolCallId"], approval["status"], approval["sent"]), (erase["id"], "denied", True))
        self.assertIn("erase_notes", approval["message"])
        self.assertEqual(job_tool["job"]["url"], "/api/workspaces/book/world-knowledge/job")
        # A reload settles the job exactly when the stored auto_continue activity names its call.
        stored = next(m for m in self.history()["messages"] if m.get("activityType") == "auto_continue")["content"]
        self.assertEqual(job_tool.get("jobSettled", False), stored.get("toolCallId") == job_tool["id"])
        self.assertEqual(items[-1]["text"], "boom")
        self.assertFalse(state["running"])

    def test_pending_approval_resumes_with_the_reducers_answer(self):
        self.use_model(ScriptedModel([("erase_notes", {"path": "notes.md"}), "Erased."]))
        with patch.object(world, "TOOLS", TEST_TOOLS):
            self.post_turn(run_body([user("Erase my notes.")]))
            result = self.reduce_history("""
              let state = S.fromHistory(history);
              const approval = state.items.find((item) => item.kind === "approval");
              state = S.reduce(state, { type: "local.answer", id: approval.id, approved: true });
              out({ status: approval.status, resume: S.pendingResume(state) });
            """)
            self.assertEqual(result["status"], "pending")
            events = self.post_turn(run_body(resume=result["resume"]))

        self.assertEqual(events[-1]["outcome"]["type"], "success")
        tool, approval = self.reduce_history()["items"][1:3]
        self.assertEqual((tool["data"]["message"], tool["denied"]), ("Erased notes.md.", False))
        self.assertEqual((approval["status"], approval["sent"]), ("approved", True))

    def test_running_history_stops_at_the_run_offset(self):
        gate = threading.Event()
        self.use_model(ScriptedModel(["First.", "Second."], gate=gate, gate_from=2))
        self.post_turn(run_body([user("One")]))
        second = background(lambda: self.client.post(self.url(), json=run_body([user("Two")])))
        self.wait_until(lambda: self.runtime.orchestrator.is_running("book", "world"))

        state = self.reduce_history()
        gate.set()
        second["thread"].join(timeout=5)

        self.assertEqual([(item["kind"], item.get("text")) for item in state["items"]],
                         [("user", "One"), ("assistant", "First."), ("user", "Two")])
        self.assertTrue(state["running"])


if __name__ == "__main__":
    unittest.main()
