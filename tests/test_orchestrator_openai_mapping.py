"""The requests OrcaBonsai receives after compaction, through the real OpenAI message mapping.

OrcaBonsai rejects a system message that is not leading, so the compaction summary must end up
inside the single leading system message. An in-process transport stands in for the server.
"""
import os

from tests.orchestrator_fakes import FakeOrca, OrchestratorAppCase, run_body, user
from webui.orchestrator.registry import TOOL_RULE

SUMMARY = "SUMMARY-OF-EARLIER-TURNS"


class OpenAIMappingTests(OrchestratorAppCase):
    def test_summary_lands_in_the_single_leading_system_message(self):
        os.environ["HARNESS_NOVEL_ORCHESTRATOR_COMPACT_TARGET"] = "60"
        orca = FakeOrca(summary=SUMMARY)
        model = orca.model()
        self.runtime.orchestrator.model_factory = lambda: model

        for turn in range(4):
            events = self.post_turn(run_body([user("Question %d" % turn)]))
            self.assertEqual(events[-1]["type"], "RUN_FINISHED", events[-1])

        self.assertTrue(orca.plain, "compaction never asked for a summary")
        messages = orca.streamed[-1]["messages"]
        roles = [message["role"] for message in messages]
        self.assertEqual(roles[0], "system")
        self.assertNotIn("system", roles[1:])
        self.assertNotIn("developer", roles)
        self.assertIn(SUMMARY, messages[0]["content"])
        self.assertIn(TOOL_RULE, messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "Question 3"})
        self.assertLess(len(messages), 8)
