"""Offline tests for repeated compaction of one orchestrator thread, through the real OpenAI model."""
import os
import unittest

from pydantic_ai.messages import SystemPromptPart

from tests.orchestrator_fakes import FakeOrca, OrchestratorAppCase, run_body, user

SUMMARY_PREFIX = "Summary of previous conversation:"
TARGET_ENV = "HARNESS_NOVEL_ORCHESTRATOR_COMPACT_TARGET"
TURNS = 14
# Tokens above the first request's size (instructions, tool schemas, one turn) before compacting.
# A turn adds about 200 and a compacted thread keeps about 550, so this fits several turns and
# leaves room after a compaction, whatever the instructions and tool schemas weigh.
TARGET_MARGIN = 900


class RepeatedCompactionTests(OrchestratorAppCase):
    def summaries_in_context(self):
        messages = self.runtime.orchestrator.history.load_context("book", "world")
        return [
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, SystemPromptPart) and part.content.startswith(SUMMARY_PREFIX)
        ]

    def test_each_compaction_keeps_only_the_latest_summary_and_settles_under_target(self):
        orca = FakeOrca(summary="SUMMARY " + "fact " * 150, reply_words=80)
        model = orca.model()
        self.runtime.orchestrator.model_factory = lambda: model
        os.environ[TARGET_ENV] = "1000000"
        self.post_turn(run_body([user("Question 0 %s" % ("detail " * 30))]))
        os.environ[TARGET_ENV] = str(orca.prompt_tokens[0] + TARGET_MARGIN)

        compacted_turns = []
        for turn in range(1, TURNS):
            before = len(orca.plain)
            self.post_turn(run_body([user("Question %d %s" % (turn, "detail " * 30))]))
            if len(orca.plain) > before:
                compacted_turns.append(turn)

        self.assertGreaterEqual(len(compacted_turns), 3)
        back_to_back = [turn for turn in compacted_turns if turn - 1 in compacted_turns]
        self.assertEqual(back_to_back, [], "a compaction did not bring the thread back under target")
        summaries = self.summaries_in_context()
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0].endswith("#%d" % len(orca.plain)))


if __name__ == "__main__":
    unittest.main()
