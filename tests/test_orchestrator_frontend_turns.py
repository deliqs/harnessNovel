"""Node tests for automatic post-job turns in the orchestrator chat.

The reducer runs as a node module. The controller runs in a vm with a fake view and fake I/O,
so the continuation rules (dedupe, 409s, approvals, two jobs) are tested without a browser.
Tests skip when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "webui" / "static"
SCRIPTS = ["orchestrator-stream.js", "orchestrator-jobs.js", "orchestrator-reducer.js", "orchestrator-view.js", "orchestrator-chat.js"]
WORLD_JOB = "/api/workspaces/w/world-knowledge/job"
TASK_JOB = "/api/tasks/t1"

PRELUDE = """
const S = require(%(reducer)s);
const out = (value) => process.stdout.write(JSON.stringify(value));
""" % {"reducer": json.dumps(str(STATIC / "orchestrator-reducer.js"))}

# Loads the browser scripts into a vm with a fake view and fake stream I/O. `postRun` refuses
# the first `refuseAuto` automatic turns with a 409 whose detail is `refuseDetail`; every other
# run finishes at once. Every polled job has completed, started at "t1".
CONTROLLER = """
const fs = require("fs");
const vm = require("vm");
const storage = new Map();
const win = {
  localStorage: { getItem: (k) => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)) },
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  confirm: () => true,
};
const context = vm.createContext({ window: win, console, setTimeout, clearTimeout, AbortController, TextDecoder });
%(paths)s.forEach((path) => vm.runInContext(fs.readFileSync(path, "utf8"), context, { filename: path }));
const chat = win.OrchestratorChat;
let last = null;
let handlers = null;
let refuseAuto = 1;
let refuseDetail = "This chat is still answering.";
chat.createView = (given) => {
  handlers = given;
  return { attach() {}, render(state, jobs, ui) { last = { state, ui }; }, clearInput() {}, detach() {} };
};
const posts = [];
chat.stream = Object.assign({}, chat.stream, {
  loadHistory: async () => HISTORY,
  replayRun: async () => false,
  pollJob: async (url) => ({ status: "completed", message: "ok " + url, started_at: "t1" }),
  postRun: async (options, onEvent) => {
    posts.push({ auto: options.autoContinue || null, text: options.messages.length ? options.messages[0].content : null, resume: Boolean(options.resume) });
    if (options.autoContinue && refuseAuto > 0) {
      refuseAuto -= 1;
      const error = new Error(refuseDetail);
      error.status = 409;
      throw error;
    }
    onEvent({ type: "RUN_STARTED" });
    onEvent({ type: "RUN_FINISHED", outcome: { type: "success" } });
  },
});
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
const snapshot = () => ({ posts: posts.slice(), kinds: last.state.items.map((item) => item.activityType || item.kind), draft: last.ui.draft, status: last.ui.status });
"""


class NodeCase(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def run_js(self, body):
        result = subprocess.run([self.node, "-e", body], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_controller(self, history, body):
        paths = json.dumps([str(STATIC / name) for name in SCRIPTS])
        script = PRELUDE + "const HISTORY = %s;\n" % json.dumps(history) + CONTROLLER % {"paths": paths}
        return self.run_js(script + "(async () => {\n%s\n})().catch((error) => { console.error(error); process.exit(1); });" % body)


def started(call_id, url, kind="world"):
    result = json.dumps({"status": "started", "message": "Job started.", "job": {"kind": kind, "workspace": "w", "url": url}})
    return [
        {"id": "a-" + call_id, "role": "assistant", "toolCalls": [{"id": call_id, "type": "function", "function": {"name": "start", "arguments": "{}"}}]},
        {"id": "t-" + call_id, "role": "tool", "toolCallId": call_id, "content": result},
    ]


def auto_continue(call_id, job_key="x@y"):
    content = {"message": "[auto] done", "toolCallId": call_id, "jobKey": job_key}
    return [{"id": "auto-" + call_id, "role": "activity", "activityType": "auto_continue", "content": content}]


def history(*messages, pending=()):
    return {"messages": [m for group in messages for m in group], "running": False, "run_offset": None, "pending_interrupts": list(pending)}


class AutoTurnStateTests(NodeCase):
    def test_auto_turn_is_an_activity_line_live_and_restored(self):
        result = self.run_js(PRELUDE + """
          const live = S.reduce(S.initialState(), { type: "local.user", id: "u1", text: "[auto] done", auto: { toolCallId: "c1", jobKey: "k1" } });
          const restored = S.fromHistory({ running: false, messages: [
            { id: "u2", role: "user", content: "[auto] The job finished." },
            { id: "x1", role: "activity", activityType: "auto_continue", content: { message: "[auto] done", toolCallId: "c2", jobKey: "k2" } },
          ] });
          out({ live: live.items, restored: restored.items, text: S.AUTO_TEXT });
        """)
        self.assertEqual(result["text"], "Job finished — asked the orchestrator to review")
        items = result["live"] + result["restored"]
        for item in items:
            self.assertEqual((item["kind"], item["activityType"], item["text"], item["auto"]), ("activity", "auto_continue", result["text"], True))
        self.assertEqual([(item["toolCallId"], item["jobKey"]) for item in items], [("c1", "k1"), (None, None), ("c2", "k2")])

    def test_reload_settles_only_jobs_with_a_stored_continuation(self):
        both = history(started("c1", WORLD_JOB), auto_continue("c1"), started("c2", TASK_JOB, "task"))
        legacy = history(started("c1", WORLD_JOB), [{"id": "u1", "role": "user", "content": "[auto] done"}])
        jobs = self.run_js(PRELUDE + "out([%s, %s].map((h) => S.trackableJobs(S.fromHistory(h)).map((item) => item.id)));"
                           % (json.dumps(both), json.dumps(legacy)))
        self.assertEqual(jobs, [["c2"], ["c1"]])

    def test_continuation_text_caps_the_job_message(self):
        text = self.run_js(PRELUDE + 'out(S.continuationText({ kind: "design" }, { status: "failed", error: "x".repeat(2000) }));')
        detail = text.split(": ", 1)[1].split(". Review")[0]
        self.assertEqual(len(detail), 300)
        self.assertTrue(detail.endswith("…"))

    def test_auto_turn_sends_its_job_ids(self):
        body = self.run_js("""
          const IO = require(%s);
          let sent = null;
          global.fetch = async (url, init) => {
            sent = JSON.parse(init.body);
            return { ok: true, body: { getReader: () => ({ read: async () => ({ done: true }) }) } };
          };
          (async () => {
            await IO.postRun({ workspace: "w", phase: "world", messages: [], autoContinue: { toolCallId: "c1", jobKey: "k1" }, thinking: true }, () => {});
            const auto = sent.forwardedProps;
            await IO.postRun({ workspace: "w", phase: "world", messages: [] }, () => {});
            process.stdout.write(JSON.stringify({ auto, plain: sent.forwardedProps }));
          })();
        """ % json.dumps(str(STATIC / "orchestrator-stream.js")))
        self.assertEqual(body["auto"], {"thinking": True, "autoContinue": {"toolCallId": "c1", "jobKey": "k1"}})
        self.assertEqual(body["plain"], {"thinking": False})


class AutoTurnControllerTests(NodeCase):
    def test_busy_refusal_is_requeued_not_put_in_the_composer(self):
        result = self.run_controller(history(started("c1", WORLD_JOB)), """
          const controller = chat.mount({}, { workspace: "w", phase: "world" });
          await settle();
          const refused = snapshot();
          controller.send("Hello");
          await settle();
          out({ refused, after: snapshot() });
        """)
        refused, after = result["refused"], result["after"]
        self.assertEqual([post["auto"] for post in refused["posts"]], [{"toolCallId": "c1", "jobKey": WORLD_JOB + "@t1"}])
        self.assertNotIn("auto_continue", refused["kinds"])
        self.assertEqual((refused["draft"], refused["status"]), ("", ""))
        retried = after["posts"][2]
        self.assertEqual([(post["auto"], post["text"]) for post in after["posts"][1:]],
                         [(None, "Hello"), (refused["posts"][0]["auto"], refused["posts"][0]["text"])])
        self.assertTrue(retried["text"].startswith('[auto] The job "world" finished with status completed'))
        self.assertEqual(after["kinds"][-2:], ["user", "auto_continue"])

    def test_duplicate_refusal_drops_the_turn(self):
        result = self.run_controller(history(started("c1", WORLD_JOB)), """
          refuseDetail = "DUPLICATE_AUTO_CONTINUE";
          const controller = chat.mount({}, { workspace: "w", phase: "world" });
          await settle();
          controller.send("Hello");
          await settle();
          out(snapshot());
        """)
        self.assertEqual([(bool(post["auto"]), post["text"]) for post in result["posts"]][1:], [(False, "Hello")])
        self.assertEqual((result["status"], result["kinds"]), ("", ["tool", "user"]))

    def test_two_jobs_from_one_thread_each_get_a_continuation(self):
        result = self.run_controller(history(started("c1", WORLD_JOB), started("c2", TASK_JOB, "task")), """
          refuseAuto = 0;
          chat.mount({}, { workspace: "w", phase: "world" });
          await settle();
          out(snapshot());
        """)
        self.assertEqual([post["auto"]["toolCallId"] for post in result["posts"]], ["c1", "c2"])
        self.assertEqual([post["auto"]["jobKey"] for post in result["posts"]], [WORLD_JOB + "@t1", TASK_JOB + "@t1"])

    def test_a_job_whose_key_was_already_continued_is_not_continued_again(self):
        result = self.run_controller(history(started("c1", WORLD_JOB), auto_continue("other", WORLD_JOB + "@t1")), """
          refuseAuto = 0;
          chat.mount({}, { workspace: "w", phase: "world" });
          await settle();
          out(snapshot());
        """)
        self.assertEqual(result["posts"], [])

    def test_auto_turn_waits_for_open_approvals(self):
        waiting = [{"id": "a2", "role": "assistant", "toolCalls": [{"id": "c2", "type": "function", "function": {"name": "world_rebuild", "arguments": "{}"}}]}]
        pending = [{"id": "int-c2", "toolCallId": "c2", "message": "Approve world_rebuild?"}]
        result = self.run_controller(history(started("c1", WORLD_JOB), waiting, pending=pending), """
          refuseAuto = 0;
          chat.mount({}, { workspace: "w", phase: "world" });
          await settle();
          const before = snapshot();
          handlers.onAnswer("int-c2", true, "");
          await settle();
          out({ before, after: snapshot() });
        """)
        self.assertEqual(result["before"]["posts"], [])
        self.assertIn("approval", result["before"]["kinds"])
        self.assertEqual([(post["resume"], bool(post["auto"])) for post in result["after"]["posts"]], [(True, False), (False, True)])



class ToolDoneTests(NodeCase):
    READ_ONLY = ["list_artifacts", "read_artifact", "job_status", "chapters_overview", "writing_guide_status", "arc_status_summary"]

    def test_only_done_results_of_changing_tools_refresh_the_page(self):
        calls = [("set_system_panel_mode", {"status": "done", "message": "Mode set."}),
                 ("world_set_enabled", {"status": "done", "message": "Enabled."}),
                 ("set_finalized_chapters", {"status": "refused", "message": "No."}),
                 ("chapters_generate", {"status": "started", "message": "Go.", "job": {"kind": "chapters", "url": "/j"}}),
                 ("design_reset", "The author denied this call.")]
        calls += [(name, {"status": "done", "message": "Read."}) for name in self.READ_ONLY]
        events = []
        for index, (name, result) in enumerate(calls):
            content = result if isinstance(result, str) else json.dumps(result)
            events += [{"type": "TOOL_CALL_START", "toolCallId": "c%d" % index, "toolCallName": name},
                       {"type": "TOOL_CALL_RESULT", "toolCallId": "c%d" % index, "content": content}]
        result = self.run_controller(history(), """
          const done = [];
          chat.stream.postRun = async (options, onEvent) => {
            onEvent({ type: "RUN_STARTED" });
            if (!options.autoContinue) %s.forEach(onEvent);
            onEvent({ type: "RUN_FINISHED", outcome: { type: "success" } });
          };
          const controller = chat.mount({}, { workspace: "w", phase: "chapters", onToolDone: (item) => done.push(item.name) });
          await settle();
          controller.send("Change the settings.");
          await settle();
          out(done);
        """ % json.dumps(events))
        self.assertEqual(result, ["set_system_panel_mode", "world_set_enabled"])


if __name__ == "__main__":
    unittest.main()
