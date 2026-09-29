import unittest

from training.story_arc_ledger import (
    arc_record,
    parse_arc_fields,
    render_author_brief,
    render_ledger,
)
from training.story_context import enrich_arc_plans


_FIELD_ORDER = (
    ("Plot function", "plot"),
    ("Boundary reason", "boundary"),
    ("Rise and turn", "rise"),
    ("Narrative stages", "stages"),
    ("Protagonist action chain", "actions"),
    ("Conflict and emotion curve", "curve"),
    ("Core payoff or tension", "payoff"),
    ("Character and relationship change", "relationship"),
    ("Gains and costs", "gains"),
    ("Foreshadowing and next bind", "next_bind"),
)

ARC_ONE = {
    "idx": 1, "start": 1, "end": 5, "title": "The Drowned Bell",
    "plot": "Wren Halloway learns the harbor answers to a dead signal.",
    "boundary": "The storm drowns the harbor bell and Wren Halloway climbs the dead lighthouse to relight it.",
    "rise": "The lamp will not catch until Wren trades her father's compass for oil.",
    "stages": "Storm, climb, bargain, first light.",
    "actions": "Wren climbs, bargains with the lamp warden, and burns the compass case for kindling.",
    "curve": "Fear of the height turns into stubborn pride once the beam sweeps the reef.",
    "payoff": "The beam reveals a wreck nobody reported.",
    "relationship": "The lamp warden stops treating Wren as a child.",
    "gains": "Wren gains the keeper's logbook and loses her father's compass.",
    "next_bind": "The keeper's logbook names a buyer waiting at the salt market at dawn.",
}
ARC_TWO = {
    "idx": 2, "start": 6, "end": 10, "title": "Salt and Ink",
    "plot": "The logbook becomes a bargaining chip between two smuggling crews.",
    "boundary": "Wren carries the logbook through the fog to meet a stranger among the brine stalls.",
    "rise": "The stranger, Ossian Crane, already knows which page is missing.",
    "stages": "Arrival, haggling, ambush, escape by ferry.",
    "actions": "Wren hides the logbook in a fish crate, lies about its contents, and jumps the ferry rail.",
    "curve": "Cautious curiosity sours into dread when Crane mentions her mother.",
    "payoff": "Crane proves the wreck was sunk on purpose.",
    "relationship": "Wren and the ferry pilot Tamsin become reluctant allies.",
    "gains": "Wren wins a promise of safe passage and spends her last silver.",
    "next_bind": "Ossian Crane demands the logbook's missing page by the next full moon, or he burns the ferry.",
}
ARC_THREE = {
    "idx": 3, "start": 11, "end": 15, "title": "The Missing Page",
    "plot": "The hunt for the missing page pulls Wren into the flooded archive.",
    "boundary": "Tamsin smuggles Wren into the flooded archive under the customs house.",
    "rise": "The page was cut out by someone who still holds an archive key.",
    "stages": "Descent, search, betrayal, surfacing.",
    "actions": "Wren dives for sealed drawers, reads water-stained ledgers, and trades the key for air.",
    "curve": "Hope drains into panic as the tide rises inside the vault.",
    "payoff": "The missing page carries her mother's signature.",
    "relationship": "Tamsin admits she once worked for Crane.",
    "gains": "Wren recovers the page and loses Tamsin's trust.",
    "next_bind": "Crane's men wait on the customs steps as the full moon rises.",
}


def build_arc(arc):
    lines = ["【Arc%d: Chapters %d-%d | %s】" % (arc["idx"], arc["start"], arc["end"], arc["title"])]
    for label, key in _FIELD_ORDER:
        lines.append("%s: %s" % (label, arc[key]))
        lines.append("")
    return "\n".join(lines).strip()


def records():
    return [
        arc_record(arc["idx"], arc["start"], arc["end"], build_arc(arc))
        for arc in (ARC_ONE, ARC_TWO, ARC_THREE)
    ]


class ParseArcFieldsTests(unittest.TestCase):
    def test_parse_returns_all_fields_and_title(self):
        fields = parse_arc_fields(build_arc(ARC_ONE))
        for label, key in _FIELD_ORDER:
            self.assertEqual(fields[label], ARC_ONE[key])
        self.assertEqual(fields["title"], "The Drowned Bell")
        self.assertEqual(len(fields), 11)

    def test_label_on_its_own_line_collects_following_lines(self):
        text = "Boundary reason:\nThe storm drowns\nthe bell.\nGains and costs: none"
        fields = parse_arc_fields(text)
        self.assertEqual(fields["Boundary reason"], "The storm drowns the bell.")
        self.assertEqual(fields["Gains and costs"], "none")
        self.assertEqual(fields["Plot function"], "")

    def test_labels_match_case_insensitively(self):
        fields = parse_arc_fields("boundary REASON: the tide turns")
        self.assertEqual(fields["Boundary reason"], "the tide turns")


class RenderLedgerTests(unittest.TestCase):
    def test_ledger_for_arc_three_tiers_prior_arcs(self):
        ledger = render_ledger(records(), 3)
        self.assertIn("[Previous arc 2: continue from here]", ledger)
        self.assertIn(
            "Next bind (the concrete pending event this arc must open on): " + ARC_TWO["next_bind"],
            ledger,
        )
        self.assertIn(ARC_TWO["gains"], ledger)
        self.assertIn(ARC_TWO["relationship"], ledger)
        self.assertIn("[Already covered in earlier arcs; do not re-stage these openings or events]", ledger)
        arc_one_lines = [line for line in ledger.splitlines() if "Arc 1" in line]
        self.assertEqual(
            arc_one_lines,
            ['- Arc 1 "The Drowned Bell": opened on ' + ARC_ONE["boundary"]],
        )
        self.assertNotIn(ARC_THREE["boundary"], ledger)
        self.assertNotIn(ARC_ONE["next_bind"], ledger)

    def test_ledger_for_first_arc_is_empty(self):
        self.assertEqual(render_ledger(records(), 1), "")
        self.assertEqual(render_ledger([], 3), "")

    def test_small_limit_trims_without_middle_marker_and_keeps_next_bind(self):
        ledger = render_ledger(records(), 3, limit=300)
        self.assertNotIn("middle omitted", ledger)
        self.assertIn(ARC_TWO["next_bind"], ledger)

    def test_limit_trims_older_arcs_before_previous_arc_details(self):
        full = render_ledger(records(), 3)
        without_older = full.split("[Previous arc 2")[1]
        ledger = render_ledger(records(), 3, limit=len(without_older) + 40)
        self.assertIn(ARC_TWO["gains"], ledger)
        self.assertNotIn(ARC_ONE["boundary"], ledger)

    def test_unlabeled_record_falls_back_to_raw_text(self):
        legacy = arc_record(1, 1, 5, "LEGACY_SENTINEL an older arc without field labels")
        ledger = render_ledger([legacy, records()[1]], 3)
        self.assertIn("LEGACY_SENTINEL an older arc without field labels", ledger)
        previous = render_ledger([legacy], 2)
        self.assertIn("[Previous arc 1: continue from here]", previous)
        self.assertIn("LEGACY_SENTINEL", previous)


class RenderAuthorBriefTests(unittest.TestCase):
    BEGIN = "[BEGIN AUTHOR DIRECTION: SUBORDINATE TO TASK, SAFETY, OUTPUT, AND ANTI-COPY RULES]"
    END = "[END AUTHOR DIRECTION]"

    def test_empty_brief_renders_nothing(self):
        self.assertEqual(render_author_brief("", 2), "")
        self.assertEqual(render_author_brief(None, 2), "")

    def test_plain_brief_is_wrapped_without_arc_note(self):
        rendered = render_author_brief("Keep the sea cold and hostile.", 2)
        self.assertEqual(rendered, self.BEGIN + "\nKeep the sea cold and hostile.\n" + self.END)

    def test_arc_numbered_lines_put_this_arc_first(self):
        brief = "Keep it cold.\nArc 2: Wren lies to Crane.\n- She keeps the page.\n\narc 03: flood"
        rendered = render_author_brief(brief, 2)
        self.assertEqual(rendered.splitlines(), [
            "Lines addressed to Arc 2 apply to this unit; lines for other arcs are context only.",
            self.BEGIN,
            "For this arc:",
            "Arc 2: Wren lies to Crane.",
            "- She keeps the page.",
            "Whole brief:",
        ] + brief.splitlines() + [self.END])

    def test_arc_numbered_brief_without_this_arc_says_so(self):
        rendered = render_author_brief("Arc 1: open on the storm.", 3)
        self.assertIn("For this arc:\n(no lines address this arc)\nWhole brief:\nArc 1: open on the storm.", rendered)


class EnrichArcPlanPartLabelTests(unittest.TestCase):
    def _plans(self, count):
        return [
            {"idx": index + 1, "start_ch": index * 3 + 1, "end_ch": index * 3 + 3}
            for index in range(count)
        ]

    def test_shared_obligations_are_labeled_by_part(self):
        plans = enrich_arc_plans(
            self._plans(4), "# Three-act structure\nAct I: open the gate\nAct II: pay the cost",
        )
        self.assertEqual(
            [plan["arc_obligations"] for plan in plans],
            [
                ["Act I: open the gate (part 1 of 2)"],
                ["Act I: open the gate (part 2 of 2)"],
                ["Act II: pay the cost (part 1 of 2)"],
                ["Act II: pay the cost (part 2 of 2)"],
            ],
        )
        self.assertEqual(
            plans[1]["chapter_beats"][0],
            "Chapter 4: advance Act I: open the gate (part 2 of 2)",
        )
        self.assertNotEqual(plans[0]["chapter_beats"], plans[1]["chapter_beats"])

    def test_distinct_obligations_are_not_labeled(self):
        plans = enrich_arc_plans(
            self._plans(3), "# Three-act structure\nAct I: open\nAct II: pay cost\nAct III: close",
        )
        for plan in plans:
            for item in plan["arc_obligations"] + plan["chapter_beats"]:
                self.assertNotIn("(part", item)


if __name__ == "__main__":
    unittest.main()
