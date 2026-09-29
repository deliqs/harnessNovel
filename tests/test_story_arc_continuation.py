"""Sibling review inside the generation loop, with arcs shaped like the prompt's real output."""

import os
import unittest

from tests.test_story_arc_generation import StoryArcRunTestBase
from tests.test_story_arc_ledger import build_arc


BELL = {
    "idx": 1, "start": 1, "end": 5, "title": "The Drowned Bell",
    "plot": "Wren Halloway learns the harbor answers to a signal nobody admits is dead.",
    "boundary": "Starts at Chapter 1 when the storm drowns the harbor bell and Wren Halloway climbs the "
                "dead lighthouse to relight it; ends at Chapter 5 when the lamp beam exposes an "
                "unreported wreck on the reef.",
    "rise": "Ch 1-2: the lamp will not catch until Wren trades her father's compass for oil. "
            "Ch 3-5: the warden doubts her until the flame holds.",
    "stages": "Storm, climb, bargain, first light over the rocks.",
    "actions": "Wren climbs, bargains with the lamp warden, and burns the compass case for kindling.",
    "curve": "Fear of the height turns into stubborn pride once the beam sweeps the rocks.",
    "payoff": "The beam reveals a hull that no harbor ledger records.",
    "relationship": "The lamp warden stops treating Wren as a child.",
    "gains": "Wren gains the keeper's logbook and loses her father's compass.",
    "next_bind": "The unreported wreck on the reef draws salvage divers from the Guild of Hooks "
                 "at dawn in Chapter 6.",
}
SALVAGE = {
    "idx": 2, "start": 6, "end": 10, "title": "Salvage Rights",
    "plot": "The wreck turns the waterfront into a quarrel over salvage law.",
    "boundary": "Starts at Chapter 6 when salvage divers from the Guild of Hooks reach the "
                "unreported wreck at dawn; ends at Chapter 10 when Wren steals the sealed manifest "
                "from the divers' barge.",
    "rise": "Ch 6-7: Ossian Crane claims the hull for the Guild. Ch 8-10: Wren swims under the "
            "barge at night.",
    "stages": "Arrival, claim, night swim, theft.",
    "actions": "Wren watches the crews, bribes a deckhand with smoked eel, and cuts the pouch loose.",
    "curve": "Envy of the crews sours into dread when Crane mentions her mother.",
    "payoff": "The manifest lists cargo stamped with her mother's seal.",
    "relationship": "Wren and the ferry pilot Tamsin become reluctant allies.",
    "gains": "Wren wins the manifest and spends her last silver on the bribe.",
    "next_bind": "Crane posts a reward for the stolen manifest at the customs house in Chapter 11.",
}
REOPENING = dict(
    SALVAGE,
    boundary="Starts at Chapter 6 as a storm silences the harbor bell while Wren Halloway scales the "
             "dead lighthouse, hoping to relight its lamp; ends at Chapter 10 when the beam finds a hull.",
)
COPY = dict(
    BELL, idx=2, start=6, end=10,
    boundary=BELL["boundary"].replace("Chapter 1 ", "Chapter 6 ").replace("Chapter 5 ", "Chapter 10 "),
)
CUSTOMS = {
    "idx": 3, "start": 11, "end": 15, "title": "The Customs House",
    "plot": "The reward turns every dockhand into a hunter.",
    "boundary": "Starts at Chapter 11 when Crane nails the reward notice to the customs house door; "
                "ends at Chapter 15 when Tamsin hides Wren in the flooded archive below it.",
    "rise": "Ch 11-12: dockhands search the ferries. Ch 13-15: Tamsin leads Wren through the cellar grates.",
    "stages": "Notice, hunt, betrayal scare, descent.",
    "actions": "Wren burns her coat, trades the ferry token for a map, and follows Tamsin underground.",
    "curve": "Hunted panic cools into grim focus inside the archive.",
    "payoff": "The archive holds the other half of her mother's ledger.",
    "relationship": "Tamsin admits she once worked for Crane.",
    "gains": "Wren secures a hiding place and loses her standing on the docks.",
    "next_bind": "Crane's men wait on the customs steps as the full moon rises in Chapter 16.",
}
ARC1, ARC2, ARC3 = build_arc(BELL), build_arc(SALVAGE), build_arc(CUSTOMS)


class GenerationLoopContinuationTests(StoryArcRunTestBase):
    def test_correct_continuation_is_written_without_retry(self):
        result, contexts = self._run([ARC1, ARC2, ARC3])
        self.assertEqual(len(contexts), 3)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "written"), (3, "written")])
        self.assertEqual([item["reason"] for item in result["outcomes"]], ["", "", ""])

    def test_paraphrased_reopening_then_continuation_is_retried(self):
        result, contexts = self._run([ARC1, build_arc(REOPENING), ARC2, ARC3])
        self.assertEqual(len(contexts), 4)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "retried"), (3, "written")])
        self.assertIn("arc 1", result["outcomes"][1]["reason"])
        self.assertIn("arc 1", contexts[2]["review_note"])

    def test_repeated_paraphrased_reopening_is_accepted_with_warning(self):
        result, contexts = self._run([ARC1, build_arc(REOPENING), build_arc(REOPENING), ARC3])
        self.assertEqual(len(contexts), 4)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "written"), (3, "written")])
        reason = result["outcomes"][1]["reason"]
        self.assertTrue(reason.startswith("accepted with warning: "), reason)
        self.assertIn("arc 1", reason)
        with open(os.path.join(self.arc_dir, "arc_002_ch006_010.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), build_arc(REOPENING))
        self.assertTrue(os.path.isfile(os.path.join(self.arc_dir, "arcs_index.json")))
        self.assertIn("- arc 2: written: accepted with warning", result["adjustment_note"])

    def test_repeated_literal_copy_is_rejected_and_halts(self):
        result, contexts = self._run([ARC1, build_arc(COPY), build_arc(COPY), ARC3])
        self.assertEqual(len(contexts), 3)
        self.assertEqual(self._statuses(result), [(1, "written"), (2, "rejected")])
        self.assertEqual(self._arc_files(), ["arc_001_ch001_005.md"])
        self.assertFalse(os.path.exists(os.path.join(self.arc_dir, "arcs_index.json")))


if __name__ == "__main__":
    unittest.main()
