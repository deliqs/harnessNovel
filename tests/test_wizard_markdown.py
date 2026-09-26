"""Node tests for the wizard's inline markdown: underscore italics only at word boundaries.

The helpers are cut out of webui/static/wizard-v0.js and evaluated under node. Tests skip when
node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WIZARD = Path(__file__).resolve().parents[1] / "webui" / "static" / "wizard-v0.js"


def inline_helpers():
    source = WIZARD.read_text(encoding="utf-8")
    return source[source.index("function escapeHtml(value) {"):source.index("function tableCells(line) {")]


class InlineMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")

    def render(self, *texts):
        script = inline_helpers() + "\nprocess.stdout.write(JSON.stringify(%s.map(renderInlineMarkdown)));" % json.dumps(list(texts))
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_snake_case_paths_are_unchanged(self):
        paths = ["file_system/story_design/chapter_usage_state.json", "see `a_b` and file_system/world_knowledge/x_y.md"]
        self.assertEqual(self.render(*paths), [paths[0], "see <code>a_b</code> and file_system/world_knowledge/x_y.md"])

    def test_underscore_italics_at_word_boundaries(self):
        self.assertEqual(self.render("_word_", "a _b c_ d", "(_quoted_)"),
                         ["<em>word</em>", "a <em>b c</em> d", "(<em>quoted</em>)"])

    def test_text_is_still_escaped_first(self):
        self.assertEqual(self.render('_<b>&"x"</b>_', "a_<script>_b"),
                         ["<em>&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;</em>", "a_&lt;script&gt;_b"])


if __name__ == "__main__":
    unittest.main()
