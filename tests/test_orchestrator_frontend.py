"""Offline tests for the phase orchestrator chat frontend (webui/static/orchestrator-*.js).

The pure reducer and SSE frame parser run under node, fed with AG-UI event sequences captured
from the spike (tests/fixtures/orchestrator_sse). Tests skip when node is not installed.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "orchestrator_sse"
SCRIPTS = ["orchestrator-stream.js", "orchestrator-jobs.js", "orchestrator-reducer.js", "orchestrator-view.js", "orchestrator-chat.js"]

PRELUDE = """
const fs = require("fs");
const S = require(%(reducer)s);
const IO = require(%(stream)s);
const events = (name) => IO.parseFrames(fs.readFileSync(%(fixtures)s + "/" + name + ".sse", "utf8")).events;
const feed = (state, list) => list.reduce(S.reduce, state);
const out = (value) => process.stdout.write(JSON.stringify(value));
""" % {
    "reducer": json.dumps(str(STATIC / "orchestrator-reducer.js")),
    "stream": json.dumps(str(STATIC / "orchestrator-stream.js")),
    "fixtures": json.dumps(str(FIXTURES)),
}


class NodeCase(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def run_js(self, body):
        result = subprocess.run([self.node, "-e", PRELUDE + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class ReducerSpikeSequenceTests(NodeCase):
    def test_tool_call_card_gets_its_result_and_empty_text_is_skipped(self):
        state = self.run_js('out(feed(S.initialState(), events("toolcall")));')
        kinds = [item["kind"] for item in state["items"]]
        self.assertEqual(kinds, ["tool", "assistant"])
        tool, text = state["items"]
        self.assertEqual(tool["name"], "get_word_count")
        self.assertEqual(json.loads(tool["args"]), {"path": "chapters/ch02.md"})
        self.assertEqual(tool["result"], "chapters/ch02.md has 16 words.")
        self.assertFalse(tool["pending"])
        self.assertEqual(text["text"], "chapters/ch02.md contains 16 words.")
        self.assertFalse(state["running"])

    def test_interrupt_produces_a_pending_approval(self):
        state = self.run_js('out(feed(S.initialState(), events("approval")));')
        tool, approval = state["items"]
        self.assertTrue(tool["pending"])
        self.assertEqual(approval["kind"], "approval")
        self.assertEqual(approval["id"], "int-call_ba38f681cdc19bf4bf8c703a")
        self.assertEqual(approval["toolCallId"], "call_ba38f681cdc19bf4bf8c703a")
        self.assertEqual(approval["status"], "pending")
        self.assertIn("delete_draft", approval["message"])

    def test_approve_builds_the_spike_resume_and_the_resume_run_fills_the_card(self):
        result = self.run_js("""
          let state = feed(S.initialState(), events("approval"));
          const before = S.pendingResume(state);
          state = S.reduce(state, { type: "local.answer", id: "int-call_ba38f681cdc19bf4bf8c703a", approved: true });
          const resume = S.pendingResume(state);
          state = S.reduce(state, { type: "local.resumeSent", ids: resume.map((entry) => entry.interruptId) });
          const after = S.pendingResume(state);
          state = feed(state, events("resume"));
          out({ before, resume, after, state });
        """)
        self.assertEqual(result["before"], [])
        resume = {"interruptId": "int-call_ba38f681cdc19bf4bf8c703a", "status": "resolved", "payload": {"approved": True}}
        self.assertEqual(result["resume"], [resume])
        self.assertEqual(result["after"], [])
        items = result["state"]["items"]
        self.assertEqual([item["kind"] for item in items], ["tool", "approval", "assistant"])
        self.assertEqual(items[0]["result"], "Deleted drafts/old-ch03.md.")
        self.assertEqual(items[1]["status"], "approved")
        self.assertEqual(items[2]["text"], "Deleted drafts/old-ch03.md.")

    def test_deny_with_reason_and_encrypted_value_is_ignored(self):
        result = self.run_js("""
          let state = feed(S.initialState(), events("deny"));
          state = S.reduce(state, { type: "local.answer", id: "int-call_216e7bb97001e75a22dcda66", approved: false, reason: "Keep the outline." });
          const resume = S.pendingResume(state);
          state = feed(state, events("deny-resume"));
          out({ resume, state });
        """)
        self.assertEqual(result["resume"][0]["payload"], {"approved": False, "reason": "Keep the outline."})
        items = result["state"]["items"]
        self.assertEqual([item["kind"] for item in items], ["tool", "approval", "assistant"])
        self.assertEqual(items[0]["result"], "Keep the outline.")
        self.assertEqual(items[1]["status"], "denied")
        self.assertNotIn("61016985-eb5d-4d98-8d39-ee8c48483e8a", [item["id"] for item in items])

    def test_reasoning_is_collected(self):
        state = self.run_js("""
          out(feed(S.initialState(), [
            { type: "RUN_STARTED" },
            { type: "REASONING_START", messageId: "r1" },
            { type: "REASONING_MESSAGE_START", messageId: "r1", role: "reasoning" },
            { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "We need " },
            { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "a count." },
            { type: "REASONING_MESSAGE_END", messageId: "r1" },
            { type: "REASONING_END", messageId: "r1" },
            { type: "THINKING_START" },
            { type: "THINKING_TEXT_MESSAGE_START" },
            { type: "THINKING_TEXT_MESSAGE_CONTENT", delta: "Legacy thinking." },
            { type: "THINKING_TEXT_MESSAGE_END" },
            { type: "TEXT_MESSAGE_START", messageId: "t1", role: "assistant" },
            { type: "TEXT_MESSAGE_CONTENT", messageId: "t1", delta: "Done." },
            { type: "TEXT_MESSAGE_END", messageId: "t1" },
            { type: "RUN_FINISHED", outcome: { type: "success" } },
          ]));
        """)
        items = state["items"]
        self.assertEqual([item["kind"] for item in items], ["reasoning", "reasoning", "assistant"])
        self.assertEqual(items[0]["text"], "We need a count.")
        self.assertEqual(items[1]["text"], "Legacy thinking.")

    def test_run_error_becomes_an_error_notice(self):
        state = self.run_js('out(feed(S.initialState(), [{ type: "RUN_STARTED" }, { type: "RUN_ERROR", message: "Model request failed" }]));')
        self.assertFalse(state["running"])
        self.assertEqual(state["items"][0]["activityType"], "run_error")
        self.assertEqual(state["items"][0]["text"], "Model request failed")

    def test_frames_split_across_chunks_parse_once(self):
        result = self.run_js("""
          const text = fs.readFileSync(%s + "/toolcall.sse", "utf8");
          let buffer = ""; const seen = [];
          for (let i = 0; i < text.length; i += 37) {
            const parsed = IO.parseFrames(buffer + text.slice(i, i + 37));
            buffer = parsed.rest; seen.push(...parsed.events.map((event) => event.type));
          }
          out({ seen, whole: events("toolcall").map((event) => event.type) });
        """ % json.dumps(str(FIXTURES)))
        self.assertEqual(result["seen"], result["whole"])
        self.assertEqual(len(result["seen"]), 22)


class ReducerHistoryTests(NodeCase):
    HISTORY = {
        "messages": [
            {"id": "u1", "role": "user", "content": "Delete the draft."},
            {"id": "a1", "role": "assistant", "toolCalls": [
                {"id": "c1", "type": "function", "function": {"name": "delete_draft", "arguments": "{\"path\": \"x.md\"}"}},
            ]},
            {"id": "act1", "role": "activity", "activityType": "interrupt", "content": {"interrupts": [
                {"id": "int-c1", "reason": "tool_call", "toolCallId": "c1", "message": "Approve delete_draft?"},
            ]}},
            {"id": "act2", "role": "activity", "activityType": "run_error", "content": {"message": "Boom"}},
            {"id": "u2", "role": "user", "content": "Live turn"},
        ],
        "running": False,
        "pending_interrupts": [{"id": "int-c1", "reason": "tool_call", "toolCallId": "c1", "message": "Approve delete_draft?"}],
    }

    def load(self, history):
        return self.run_js("out(S.reduce(S.initialState(), { type: \"local.history\", history: %s }));" % json.dumps(history))

    def test_history_renders_messages_and_open_approvals(self):
        state = self.load(self.HISTORY)
        kinds = [item["kind"] for item in state["items"]]
        self.assertEqual(kinds, ["user", "tool", "approval", "activity", "user"])
        self.assertEqual(state["items"][1]["name"], "delete_draft")
        self.assertEqual(state["items"][2]["status"], "pending")
        self.assertEqual(state["items"][3]["text"], "Boom")

    def test_running_history_stops_at_run_offset(self):
        state = self.load(dict(self.HISTORY, running=True, run_offset=1, pending_interrupts=[]))
        self.assertEqual([item["id"] for item in state["items"]], ["u1"])
        self.assertTrue(state["running"])

    def test_answered_interrupt_is_not_reopened(self):
        state = self.load(dict(self.HISTORY, pending_interrupts=[]))
        self.assertEqual(state["items"][2]["status"], "resolved")
        self.assertTrue(state["items"][2]["sent"])


class JobContinuationTests(NodeCase):
    def started(self, call_id, url="/api/workspaces/w/design/concept/job"):
        result = json.dumps({"status": "started", "message": "Design job started.", "job": {"kind": "design", "workspace": "w", "scope": "concept", "url": url}})
        return [
            {"type": "TOOL_CALL_START", "toolCallId": call_id, "toolCallName": "run_design"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": call_id, "content": result},
        ]

    def test_started_result_becomes_a_trackable_job(self):
        jobs = self.run_js("out(S.trackableJobs(feed(S.initialState(), %s)));" % json.dumps(self.started("c1")))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job"]["url"], "/api/workspaces/w/design/concept/job")

    def test_one_continuation_per_job_and_queued_while_running(self):
        result = self.run_js("""
          const job = { kind: "design", url: "/api/x/job" };
          const record = { status: "completed", message: "Design updated.", started_at: "2026-09-26T10:00:00" };
          const key = S.jobKey(job, record, "c1");
          const text = S.continuationText(job, record);
          const first = S.claimContinuation(S.initialContinuations([]), key, text, false);
          const again = S.claimContinuation(first.cont, key, text, false);
          const queued = S.claimContinuation(S.initialContinuations([]), key, text, true);
          const drained = S.takeQueued(queued.cont);
          const restored = S.claimContinuation(S.initialContinuations([key]), key, text, false);
          const otherRun = S.jobKey(job, { status: "completed", started_at: "2026-09-26T11:00:00" }, "c2");
          out({ key, text, first: first.post, again: again.post, queued: queued.post, drained: drained.post,
                left: drained.cont.queue, restored: restored.post, otherRun,
                terminal: ["completed", "failed", "stopped", "succeeded", "error"].map(S.isTerminalJobStatus),
                active: ["running", "paused", "queued", "idle"].map(S.isTerminalJobStatus) });
        """)
        self.assertEqual(result["key"], "/api/x/job@2026-09-26T10:00:00")
        self.assertEqual(
            result["text"],
            '[auto] The job "design" finished with status completed: Design updated. Review the result and tell me what\'s next.',
        )
        self.assertEqual(result["first"], result["text"])
        self.assertIsNone(result["again"])
        self.assertIsNone(result["queued"])
        self.assertEqual(result["drained"], result["text"])
        self.assertEqual(result["left"], [])
        self.assertIsNone(result["restored"])
        self.assertNotEqual(result["otherRun"], result["key"])
        self.assertEqual(result["terminal"], [True] * 5)
        self.assertEqual(result["active"], [False] * 4)

    def test_restored_jobs_that_were_continued_or_superseded_are_settled(self):
        history = {"running": False, "messages": [
            {"id": "a1", "role": "assistant", "toolCalls": [{"id": "c1", "type": "function", "function": {"name": "run_design", "arguments": "{}"}}]},
            {"id": "t1", "role": "tool", "toolCallId": "c1", "content": self.started("c1")[1]["content"]},
            {"id": "u1", "role": "user", "content": "[auto] The job \"design\" finished with status completed: ok. Review the result and tell me what's next."},
            {"id": "a2", "role": "assistant", "toolCalls": [{"id": "c2", "type": "function", "function": {"name": "run_design", "arguments": "{}"}}]},
            {"id": "t2", "role": "tool", "toolCallId": "c2", "content": self.started("c2")[1]["content"]},
        ]}
        jobs = self.run_js("out(S.trackableJobs(S.fromHistory(%s)));" % json.dumps(history))
        self.assertEqual([job["id"] for job in jobs], ["c2"])


class BrowserGlobalTests(NodeCase):
    def test_scripts_load_as_classic_scripts_and_define_one_global(self):
        paths = json.dumps([str(STATIC / name) for name in SCRIPTS])
        result = self.run_js("""
          const vm = require("vm");
          const win = {};
          const context = vm.createContext({ window: win, console });
          %s.forEach((path) => vm.runInContext(fs.readFileSync(path, "utf8"), context, { filename: path }));
          const chat = win.OrchestratorChat;
          out({ keys: Object.keys(win), mount: typeof chat.mount, reduce: typeof chat.state.reduce,
                post: typeof chat.stream.postRun, view: typeof chat.createView });
        """ % paths)
        self.assertEqual(result["keys"], ["OrchestratorChat"])
        types = (result["mount"], result["reduce"], result["post"], result["view"])
        self.assertEqual(types, ("function",) * 4)


class OrchestratorStaticTests(unittest.TestCase):
    def setUp(self):
        self.sources = {name: (STATIC / name).read_text(encoding="utf-8") for name in SCRIPTS + ["orchestrator-chat.css"]}

    def test_each_file_has_at_most_300_lines(self):
        for name, text in self.sources.items():
            with self.subTest(name=name):
                self.assertLessEqual(len(text.splitlines()), 300)

    def test_no_unescaped_html_interpolation(self):
        pattern = re.compile(r"(innerHTML|outerHTML)\s*\+?=|insertAdjacentHTML\s*\(")
        for name in SCRIPTS:
            for line in self.sources[name].splitlines():
                if not pattern.search(line):
                    continue
                with self.subTest(name=name, line=line.strip()):
                    for expression in re.findall(r"\$\{([^}]*)\}", line):
                        self.assertIn("escapeHtml(", expression)

    def test_no_es_modules(self):
        for name in SCRIPTS:
            with self.subTest(name=name):
                self.assertNotRegex(self.sources[name], r"(?m)^\s*(import|export)\s")

    def test_chat_defines_window_global_and_uses_contract_routes(self):
        self.assertIn("root.OrchestratorChat = root.OrchestratorChat || {}", self.sources["orchestrator-chat.js"])
        self.assertIn("})(window);", self.sources["orchestrator-chat.js"])
        stream = self.sources["orchestrator-stream.js"]
        for fragment in ("/orchestrator/", "/stream", "/history", "/stop", "forwardedProps", "resume"):
            self.assertIn(fragment, stream)
        self.assertNotIn("document.", stream)

    def test_css_reuses_tokens_and_respects_reduced_motion(self):
        css = self.sources["orchestrator-chat.css"]
        self.assertIn("var(--primary)", css)
        self.assertIn("prefers-reduced-motion", css)


if __name__ == "__main__":
    unittest.main()
