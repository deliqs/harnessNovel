"""Node test that a streaming reasoning row is updated in place.

No DOM library is available, so this builds the smallest element fake the view's
attach/render path touches. Tests skip when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "webui" / "static"

# Enough of a document for orchestrator-view.js: create, append, query, and move nodes.
DOM = r"""
function match(node, sel) {
  if (sel.charAt(0) === ".") return node.className.split(/\s+/).indexOf(sel.slice(1)) !== -1;
  return node.tagName === sel;
}
function query(node, sel) {
  for (let i = 0; i < node.children.length; i += 1) {
    const kid = node.children[i];
    if (match(kid, sel)) return kid;
    const found = query(kid, sel);
    if (found) return found;
  }
  return null;
}
function element(tag) {
  const node = { tagName: tag, className: "", textContent: "", children: [], style: {}, dataset: {},
    parent: null, open: false, type: "", value: "", checked: false, disabled: false, title: "",
    scrollHeight: 1000, scrollTop: 0, clientHeight: 100 };
  node.classList = { toggle(name, on) {
    const parts = node.className.split(/\s+/).filter(Boolean).filter((part) => part !== name);
    if (on) parts.push(name);
    node.className = parts.join(" ");
  } };
  node.setAttribute = () => {};
  node.addEventListener = () => {};
  node.querySelector = (sel) => query(node, sel);
  node.remove = () => {
    if (!node.parent) return;
    node.parent.children = node.parent.children.filter((kid) => kid !== node);
    node.parent = null;
  };
  node.insertBefore = (kid, before) => {
    if (kid.parent) kid.remove();
    kid.parent = node;
    const index = before ? node.children.indexOf(before) : node.children.length;
    node.children.splice(index < 0 ? node.children.length : index, 0, kid);
  };
  node.append = (...kids) => kids.forEach((kid) => node.insertBefore(kid, null));
  node.replaceChildren = (...kids) => { node.children.splice(0).forEach((kid) => { kid.parent = null; }); node.append(...kids); };
  Object.defineProperty(node, "lastElementChild", { get() { return node.children[node.children.length - 1] || null; } });
  return node;
}
var document = { createElement: element, activeElement: null };
"""


class NodeCase(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def run_view(self, body):
        script = """
const fs = require("fs");
const vm = require("vm");
const win = {};
const context = vm.createContext({ window: win, console, setTimeout });
vm.runInContext(%(dom)s + "\\n" + fs.readFileSync(%(reducer)s, "utf8") + "\\n" + fs.readFileSync(%(view)s, "utf8"), context, { filename: "orchestrator-view.js" });
const S = win.OrchestratorChat.state;
const document = context.document;
const view = win.OrchestratorChat.createView({ onSend() {}, onStop() {}, onClear() {}, onThink() {}, onAnswer() {}, onDraft() {} });
const host = document.createElement("div");
view.attach(host);
const ui = (running) => ({ running, status: "", statusError: false, thinking: true, draft: "" });
const feed = (state, list) => list.reduce(S.reduce, state);
const row = () => host.querySelector(".orch-list").children[0];
const out = (value) => process.stdout.write(JSON.stringify(value));
%(body)s
""" % {
            "dom": json.dumps(DOM),
            "reducer": json.dumps(str(STATIC / "orchestrator-reducer.js")),
            "view": json.dumps(str(STATIC / "orchestrator-view.js")),
            "body": body,
        }
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class ReasoningViewTests(NodeCase):
    def test_streaming_reasoning_keeps_its_node_and_shows_the_count(self):
        result = self.run_view("""
          let state = feed(S.initialState(), [
            { type: "RUN_STARTED" },
            { type: "REASONING_MESSAGE_START", messageId: "r1" },
            { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "Hello" },
          ]);
          view.render(state, new Map(), ui(true));
          const first = row();
          const details = first.querySelector("details");
          details.open = true;
          host.querySelector(".orch-list").scrollTop = 0;
          state = S.reduce(state, { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: " x".repeat(3135) });
          view.render(state, new Map(), ui(true));
          const streaming = {
            same: row() === first, details: row().querySelector("details") === details, open: details.open,
            label: row().querySelector(".orch-label").textContent,
            tail: row().querySelector(".orch-reasoning-tail").textContent,
            scroll: host.querySelector(".orch-list").scrollTop,
          };
          state = S.reduce(state, { type: "TEXT_MESSAGE_START", messageId: "t1" });
          view.render(state, new Map(), ui(true));
          const ended = { same: row() === first, label: row().querySelector(".orch-label").textContent,
            tail: row().querySelector(".orch-reasoning-tail").textContent, open: details.open };
          const restored = S.fromHistory({ running: false, messages: [{ id: "r9", role: "reasoning", content: "Saved." }] });
          view.render(restored, new Map(), ui(false));
          const history = row().querySelector(".orch-label").textContent;
          const tool = { kind: "tool", id: "c1", name: "list_artifacts", args: "{}", result: null, pending: true, data: null, job: null };
          view.render({ items: [tool], running: false, openReasoningId: null }, new Map(), ui(false));
          const toolNode = row();
          view.render({ items: [Object.assign({}, tool, { args: "{\\"a\\":1}" })], running: false, openReasoningId: null }, new Map(), ui(false));
          out({ streaming, ended, history, toolRebuilt: row() !== toolNode });
        """)
        streaming, ended = result["streaming"], result["ended"]
        self.assertTrue(streaming["same"])
        self.assertTrue(streaming["details"])
        self.assertTrue(streaming["open"])
        self.assertEqual(streaming["label"], "Reasoning… 6,275 characters")
        self.assertNotIn("\n", streaming["tail"])
        self.assertTrue(streaming["tail"].endswith("x"))
        self.assertEqual(streaming["scroll"], 0)
        self.assertTrue(ended["same"])
        self.assertTrue(ended["open"])
        self.assertEqual((ended["label"], ended["tail"]), ("Reasoning", ""))
        self.assertEqual(result["history"], "Reasoning")
        self.assertTrue(result["toolRebuilt"])


if __name__ == "__main__":
    unittest.main()