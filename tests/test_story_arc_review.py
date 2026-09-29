import unittest

from tests.test_story_arc_ledger import ARC_ONE, ARC_THREE, ARC_TWO, build_arc
from training.story_arc_review import (
    CONTENT_OVERLAP_THRESHOLD,
    content_words,
    opening_event,
    outcome,
    render_outcomes,
    retry_note,
    review_against_siblings,
)


SIBLING_BOUNDARY = (
    "Arc 1 starts at Chapter 1 because the storm drowns the harbor bell and Wren Halloway "
    "climbs the dead lighthouse to relight the lamp, and ends at Chapter 5 when the beam "
    "reveals the wreck."
)
CANDIDATE_COPY = SIBLING_BOUNDARY.replace("Arc 1", "Arc 2").replace(
    "Chapter 1 ", "Chapter 6 ").replace("Chapter 5 ", "Chapter 10 ")
CANDIDATE_PARAPHRASE = (
    "This unit opens at Chapter 6 as a gale silences the harbor bell, so Wren Halloway "
    "scales the abandoned lighthouse and rekindles its lamp; it closes at Chapter 10 once "
    "the light exposes the wreck."
)
CANDIDATE_DISTINCT = (
    "Arc 2 starts at Chapter 6 because Tamsin smuggles Wren into the flooded archive under "
    "the customs house, and ends at Chapter 10 when the tide fills the vault."
)
CANDIDATE_CONTINUATION = (
    "Arc 2 starts at Chapter 6 because divers sent to the wreck the beam uncovered bring up "
    "the keeper's logbook, and ends at Chapter 10 at the salt market."
)


MARA_SIBLING_BOUNDARY = (
    "Starts at Chapter 6 with the immediate aftermath of the First Showing on the beach, when "
    "the tide pool reveals Mara's gift to the fishermen; ends at Chapter 10 when Mara is "
    "enrolled at the village school against her mother's wishes."
)
MARA_SIBLING_BIND = (
    "Mara's first day at the village school; the Showing rumour reaches the harbour master."
)
MARA_CONTINUATION = (
    "Chapter 11 opens on Mara's first day at the village school, enrolled against her "
    "mother's wishes, and ends at Chapter 15 with the harbour master's inspection."
)
MARA_REOPENING = (
    "Starts at Chapter 11 hours after the First Showing on the beach, with the fishermen still "
    "arguing over the tide pool and Mara's gift; ends at Chapter 15 at the school gate."
)
NOUN_END_SIBLING = (
    "It starts at the end of the First Showing on the beach, as Mara and her mother walk home "
    "across the dunes, and it closes at Chapter 10 when the harbour master arrives."
)
NOUN_END_REOPENING = (
    "Chapter 11 opens on the dunes as Mara and her mother walk home from the First Showing on "
    "the beach; the unit closes at Chapter 15 with a locked door."
)
SHORT_SIBLING = (
    "Starts at Chapter 6 in the aftermath of the First Showing at the beach; ends at Chapter 10 "
    "with Mara enrolled at school."
)
SHORT_REOPENING = (
    "Starts at Chapter 11 hours after the First Showing on the beach, with the fishermen still "
    "arguing over the tide pool; ends at Chapter 15 at the school gate."
)
UNCUT_PARAPHRASE = (
    "A gale silences the harbor bell, so Wren Halloway scales the abandoned lighthouse to "
    "rekindle its lamp."
)


def mara_sibling():
    return build_arc(relabel(
        ARC_TWO, 2, boundary=MARA_SIBLING_BOUNDARY, next_bind=MARA_SIBLING_BIND,
    ))


def sibling_with_chapters():
    return build_arc(dict(ARC_ONE, boundary=SIBLING_BOUNDARY))


def relabel(arc, idx, **changes):
    value = dict(arc)
    value.update(changes)
    value["idx"] = idx
    return value


class ReviewAgainstSiblingsTests(unittest.TestCase):
    def test_relabeled_copy_collides_with_original(self):
        candidate = build_arc(relabel(ARC_ONE, 2, start=6, end=10))
        result = review_against_siblings(candidate, [(1, build_arc(ARC_ONE))])
        self.assertEqual(result["collision"], 1)
        self.assertIn("arc 1", result["reason"])
        self.assertIn("boundary phrase similarity", result["reason"])
        self.assertEqual(result["severity"], "block")
        self.assertEqual(result["scores"][0]["arc"], 1)
        self.assertGreaterEqual(result["scores"][0]["boundary_similarity"], 0.3)

    def test_distinct_arc_does_not_collide(self):
        siblings = [(1, build_arc(ARC_ONE)), (2, build_arc(ARC_TWO))]
        result = review_against_siblings(build_arc(ARC_THREE), siblings)
        self.assertIsNone(result["collision"])
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["severity"], "")
        self.assertEqual([score["arc"] for score in result["scores"]], [1, 2])

    def test_lowest_colliding_sibling_wins(self):
        candidate = build_arc(relabel(ARC_TWO, 4))
        siblings = [(3, build_arc(relabel(ARC_TWO, 3))), (1, build_arc(ARC_ONE)), (2, build_arc(ARC_TWO))]
        result = review_against_siblings(candidate, siblings)
        self.assertEqual(result["collision"], 2)

    def test_copy_with_new_chapter_numbers_collides(self):
        candidate = relabel(ARC_ONE, 2, start=6, end=10, boundary=CANDIDATE_COPY)
        result = review_against_siblings(build_arc(candidate), [(1, sibling_with_chapters())])
        self.assertEqual(result["collision"], 1)
        self.assertIn("arc 1", result["reason"])
        self.assertIn("boundary phrase similarity", result["reason"])
        self.assertEqual(result["severity"], "block")

    def test_paraphrased_opening_collides_via_content_overlap(self):
        candidate = relabel(ARC_THREE, 2, start=6, end=10, boundary=CANDIDATE_PARAPHRASE)
        result = review_against_siblings(build_arc(candidate), [(1, sibling_with_chapters())])
        score = result["scores"][0]
        self.assertLess(score["boundary_similarity"], 0.3)
        self.assertLess(score["text_similarity"], 0.22)
        self.assertGreaterEqual(score["content_overlap"], CONTENT_OVERLAP_THRESHOLD)
        self.assertEqual(result["collision"], 1)
        self.assertIn("shared boundary words", result["reason"])
        self.assertIn("lighthouse", result["reason"])
        self.assertEqual(result["severity"], "warn")

    def test_boundary_without_cut_word_still_compares(self):
        candidate = relabel(ARC_THREE, 2, start=6, end=10, boundary=UNCUT_PARAPHRASE)
        result = review_against_siblings(build_arc(candidate), [(1, build_arc(ARC_ONE))])
        self.assertEqual(result["collision"], 1)
        self.assertEqual(result["severity"], "warn")
        self.assertIn("lighthouse", result["reason"])

    def test_distinct_opening_with_same_chapter_framing_does_not_collide(self):
        for boundary in (CANDIDATE_DISTINCT, CANDIDATE_CONTINUATION):
            with self.subTest(boundary=boundary):
                candidate = relabel(ARC_THREE, 2, start=6, end=10, boundary=boundary)
                result = review_against_siblings(build_arc(candidate), [(1, sibling_with_chapters())])
                self.assertIsNone(result["collision"])
                self.assertLess(result["scores"][0]["content_overlap"], CONTENT_OVERLAP_THRESHOLD)

    def test_opening_on_sibling_next_bind_does_not_collide(self):
        candidate = relabel(ARC_THREE, 3, start=11, end=15, boundary=MARA_CONTINUATION)
        result = review_against_siblings(build_arc(candidate), [(2, mara_sibling())])
        self.assertIsNone(result["collision"], result["reason"])
        self.assertEqual(result["severity"], "")
        self.assertLess(result["scores"][0]["content_overlap"], CONTENT_OVERLAP_THRESHOLD)

    def test_reopening_on_sibling_opening_event_still_collides(self):
        candidate = relabel(ARC_THREE, 3, start=11, end=15, boundary=MARA_REOPENING)
        result = review_against_siblings(build_arc(candidate), [(2, mara_sibling())])
        self.assertEqual(result["collision"], 2)
        self.assertIn("arc 2", result["reason"])
        self.assertEqual(result["severity"], "warn")
        self.assertIn("fishermen", result["reason"])
        self.assertGreaterEqual(result["scores"][0]["content_overlap"], CONTENT_OVERLAP_THRESHOLD)

    def test_content_words_drop_digits_framing_and_stopwords(self):
        self.assertEqual(
            content_words("Arc 2 starts at Chapter 6 because Wren's lamp fails, and ends at Chapter 10."),
            ["wren", "lamp", "fails"],
        )

    def test_opening_event_is_boundary_reason_before_the_end_chapter(self):
        self.assertEqual(opening_event(build_arc(ARC_ONE)), ARC_ONE["boundary"])
        self.assertEqual(opening_event("no labels here"), "")
        self.assertEqual(
            opening_event(mara_sibling()),
            "Starts at Chapter 6 with the immediate aftermath of the First Showing on the beach, "
            "when the tide pool reveals Mara's gift to the fishermen; ends at",
        )

    def test_noun_end_does_not_cut_the_opening(self):
        sibling = build_arc(relabel(ARC_TWO, 2, boundary=NOUN_END_SIBLING))
        self.assertTrue(opening_event(sibling).startswith("It starts at the end of the First Showing"))
        candidate = build_arc(relabel(ARC_THREE, 3, start=11, end=15, boundary=NOUN_END_REOPENING))
        result = review_against_siblings(candidate, [(2, sibling)])
        self.assertEqual(result["collision"], 2)
        self.assertIn("dunes", result["reason"] + opening_event(candidate))

    def test_short_opening_collides_with_fewer_shared_words(self):
        sibling = build_arc(relabel(ARC_TWO, 2, boundary=SHORT_SIBLING))
        candidate = build_arc(relabel(ARC_THREE, 3, start=11, end=15, boundary=SHORT_REOPENING))
        result = review_against_siblings(candidate, [(2, sibling)])
        self.assertEqual(result["collision"], 2)
        self.assertEqual(result["severity"], "warn")
        self.assertIn("3 shared boundary words: first, showing, beach", result["reason"])

    def test_too_short_opening_falls_back_to_whole_field(self):
        text = build_arc(relabel(ARC_TWO, 2, boundary="It opens as the storm ends and Wren boards the ferry."))
        self.assertEqual(opening_event(text), "It opens as the storm ends and Wren boards the ferry.")

    def test_end_chapter_number_cuts_the_opening(self):
        text = build_arc(relabel(ARC_TWO, 2, boundary="Wren boards the ferry; Chapter 10 sees the pier burn."))
        self.assertEqual(opening_event(text), "Wren boards the ferry")


class RetryNoteTests(unittest.TestCase):
    def test_note_quotes_sibling_opening_and_stays_short(self):
        note = retry_note(1, build_arc(ARC_ONE))
        self.assertIn("arc 1's opening event", note)
        self.assertIn(ARC_ONE["boundary"].rstrip("."), note)
        self.assertIn("open on the pending event in arc 1's next bind", note)
        self.assertIn(ARC_ONE["next_bind"].rstrip("."), note)
        self.assertIn("must not repeat arc 1's opening or its beats", note)
        self.assertNotIn("do not reuse", note)
        self.assertLessEqual(len(note), 600)
        long_arc = build_arc(relabel(ARC_ONE, 12, boundary="x " * 300, next_bind="y " * 300))
        self.assertLessEqual(len(retry_note(12, long_arc)), 600)

    def test_note_keeps_sibling_text_inside_excerpt_block(self):
        instruction, excerpt = retry_note(1, build_arc(ARC_ONE)).split("\n", 1)
        self.assertNotIn("Wren", instruction)
        self.assertTrue(excerpt.startswith("[BEGIN UNTRUSTED WORKSPACE DATA: SIBLING ARC EXCERPT]"))
        self.assertTrue(excerpt.endswith("[END UNTRUSTED WORKSPACE DATA: SIBLING ARC EXCERPT]"))


class OutcomeTests(unittest.TestCase):
    def test_outcome_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            outcome(1, "done")

    def test_outcome_shape(self):
        self.assertEqual(
            outcome("2", "retried", "re-stages arc 1"),
            {"arc": 2, "status": "retried", "reason": "re-stages arc 1"},
        )

    def test_render_outcomes_shows_reason_only_when_present(self):
        rendered = render_outcomes([
            outcome(1, "written"),
            outcome(2, "rejected", "re-stages arc 1's opening event"),
        ])
        self.assertEqual(
            rendered,
            "Per-arc outcomes:\n- arc 1: written\n- arc 2: rejected: re-stages arc 1's opening event",
        )
        self.assertEqual(render_outcomes([]), "")


if __name__ == "__main__":
    unittest.main()
