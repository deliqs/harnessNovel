"""Node tests for how the orchestrator reducer shows tool calls live and after a reload.

Covers plain-text denials, recorded approval decisions, cards waiting for approval, stopped
runs and the job card. Tests skip when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

REDUCER = Path(__file__).resolve().parents[1] / "webui" / "static" / "orchestrator-reducer.js"

PRELUDE = """
const S = require(%s);
const out = (value) => process.stdout.write(JSON.stringify(value));
const feed = (state, list) => list.reduce(S.reduce, state);
const interrupt = (id) => ({ type: "RUN_FINISHED", outcome: { type: "interrupt", interrupts: [{ id: "int-" + id, toolCallId: id, message: "Approve?" }] } });
""" % json.dumps(str(REDUCER))


def call(call_id, name="world_rebuild"):
    return {"id": "a-" + call_id, "role": "assistant", "toolCalls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def interrupt_activity(call_id):
    content = {"interrupts": [{"id": "int-" + call_id, "toolCallId": call_id, "message": "Approve %s?" % call_id}]}
    return {"id": "i-" + call_id, "role": "activity", "activityType": "interrupt", "content": content}


def decision(call_id, approved, reason=""):
    content = {"toolCallId": call_id, "approved": approved, "reason": reason}
    return {"id": "d-" + call_id, "role": "activity", "activityType": "approval_decision", "content": content}


class NodeCase(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def run_js(self, body):
        result = subprocess.run([self.node, "-e", PRELUDE + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def restore(self, messages, pending=()):
        history = {"messages": messages, "running": False, "run_offset": None, "pending_interrupts": list(pending)}
        return self.run_js("out(S.fromHistory(%s).items);" % json.dumps(history))


class ApprovalDisplayTests(NodeCase):
    def test_recorded_decisions_restore_outcome_and_reason(self):
        items = self.restore([
            call("c1"), interrupt_activity("c1"), decision("c1", False, "Keep the coast."),
            {"id": "t1", "role": "tool", "toolCallId": "c1", "content": "The author denied this call: Keep the coast."},
            call("c2"), interrupt_activity("c2"), decision("c2", True),
            {"id": "t2", "role": "tool", "toolCallId": "c2", "content": json.dumps({"status": "done", "message": "Rebuilt."})},
        ])
        denied_tool, denied, approved_tool, approved = items
        self.assertEqual((denied["status"], denied["reason"], denied["sent"]), ("denied", "Keep the coast.", True))
        self.assertEqual((denied_tool["denied"], denied_tool["awaiting"]), (True, False))
        self.assertEqual((approved["status"], approved["reason"]), ("approved", ""))
        self.assertEqual((approved_tool["denied"], approved_tool["data"]["status"]), (False, "done"))

    def test_decision_without_an_interrupt_record_still_shows(self):
        items = self.restore([call("c1"), decision("c1", False, "No.")])
        self.assertEqual([(item["kind"], item.get("status"), item.get("reason")) for item in items],
                         [("tool", None, None), ("approval", "denied", "No.")])

    def test_card_awaits_approval_until_answered(self):
        result = self.run_js("""
          const live = feed(S.initialState(), [{ type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "world_rebuild" }, interrupt("c1")]);
          const answered = S.reduce(live, { type: "local.answer", id: "int-c1", approved: true });
          const restored = S.fromHistory({ running: false, messages: [%s, %s], pending_interrupts: [{ id: "int-c1", toolCallId: "c1", message: "Approve?" }] });
          out([live, answered, restored].map((state) => state.items[0].awaiting));
        """ % (json.dumps(call("c1")), json.dumps(interrupt_activity("c1"))))
        self.assertEqual(result, [True, False, True])

    def test_plain_text_after_an_approval_is_a_denial(self):
        result = self.run_js("""
          let state = feed(S.initialState(), [{ type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "world_rebuild" }, interrupt("c1")]);
          state = S.reduce(state, { type: "local.answer", id: "int-c1", approved: false, reason: "Not now." });
          state = S.reduce(state, { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: "Not now." });
          const auto = feed(S.initialState(), [
            { type: "TOOL_CALL_START", toolCallId: "c2", toolCallName: "world_rebuild" }, interrupt("c2"),
            { type: "TOOL_CALL_RESULT", toolCallId: "c2", content: "The author sent a new message instead of approving." },
          ]);
          out({ state: state.items, auto: auto.items, resume: S.pendingResume(auto) });
        """)
        tool, approval = result["state"]
        self.assertEqual((tool["denied"], tool["data"], approval["status"], approval["reason"]), (True, None, "denied", "Not now."))
        tool, approval = result["auto"]
        self.assertEqual((tool["denied"], approval["status"], approval["sent"]), (True, "denied", True))
        self.assertEqual(result["resume"], [])

    def test_plain_text_without_an_approval_and_json_results(self):
        items = self.run_js("""
          out(feed(S.initialState(), [
            { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "list_artifacts" },
            { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: "file_system/notes.md (4 bytes)" },
            { type: "TOOL_CALL_START", toolCallId: "c2", toolCallName: "world_chat" }, interrupt("c2"),
            { type: "local.answer", id: "int-c2", approved: true },
            { type: "TOOL_CALL_RESULT", toolCallId: "c2", content: JSON.stringify({ status: "refused", message: "A job is running." }) },
          ]).items);
        """)
        listing, refused, approval = items
        self.assertEqual((listing["denied"], listing["data"], listing["result"]), (False, None, "file_system/notes.md (4 bytes)"))
        self.assertEqual((refused["denied"], refused["data"]["status"], approval["status"]), (False, "refused", "approved"))


class StopAndJobCardTests(NodeCase):
    def test_stop_is_the_same_neutral_notice_live_and_restored(self):
        result = self.run_js("""
          const live = feed(S.initialState(), [{ type: "RUN_STARTED" }, { type: "RUN_ERROR", message: "Stopped by the author", code: "stopped" }]);
          const failed = feed(S.initialState(), [{ type: "RUN_ERROR", message: "Stopped by the author" }]).items[0].activityType;
          const restored = S.fromHistory({ running: false, messages: [
            { id: "s1", role: "activity", activityType: "stopped", content: { message: "Stopped by the author" } },
          ] });
          out({ failed, shown: [live.items[0], restored.items[0]].map((item) => [item.kind, item.activityType, item.text]) });
        """)
        self.assertEqual(result["shown"], [["activity", "stopped", "Stopped by the author"]] * 2)
        self.assertEqual(result["failed"], "run_error")

    def test_job_card_handles_manager_jobs_and_cli_tasks(self):
        result = self.run_js("""
          const item = { job: { kind: "task" }, data: { message: "Title and synopsis regeneration started." } };
          out({
            running: S.jobCardModel({ job: { kind: "arcs" }, data: null }, { record: { status: "running", completed: 2, total: 4, message: "Generating" } }),
            task: S.jobCardModel(item, { record: { status: "succeeded", message: "Finished" }, terminal: true }),
            starting: S.jobCardModel(item, null),
            terminal: ["completed", "failed", "stopped", "succeeded", "succeeded_with_warnings"].map(S.isTerminalJobStatus),
            active: ["idle", "queued", "running", "paused", "stopping"].map(S.isTerminalJobStatus),
            key: S.jobKey({ url: "/api/tasks/t1" }, { id: "t1", created_at: "c", started_at: "s" }, "c9"),
          });
        """)
        self.assertEqual(result["running"], {"label": "Generating", "meta": "2 / 4", "percent": 50, "finished": False})
        self.assertEqual(result["task"], {"label": "Finished", "meta": "succeeded", "percent": 3, "finished": True})
        self.assertEqual(result["starting"]["label"], "Title and synopsis regeneration started.")
        self.assertEqual((result["starting"]["meta"], result["starting"]["finished"]), ("starting", False))
        self.assertEqual(result["terminal"], [True] * 5)
        self.assertEqual(result["active"], [False] * 5)
        self.assertEqual(result["key"], "/api/tasks/t1@s")


if __name__ == "__main__":
    unittest.main()
