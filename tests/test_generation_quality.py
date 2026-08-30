import os
import unittest
from unittest import mock

from training.adaptive_builder import _is_numeric_anchor
from training.generation_quality import (
    diagnose_chapter,
    diagnose_chapter_outline,
    diagnose_rewrite,
    extract_critical_anchors,
    forbidden_diagnostics,
    is_core_character_name,
    phrase_similarity,
    split_outline_anchors,
)


def _chapter(number=3, name="Alice", count=650, fact="17"):
    words = []
    for index in range(count):
        if index % 40 == 0:
            words.extend([name, "carried", fact, "silver", "coins", "home."])
        else:
            words.append("walked")
    return "Chapter %d: The Return\n%s" % (number, " ".join(words[:count]))


class GenerationQualityTests(unittest.TestCase):
    def test_chapter_contract_reports_actionable_reasons(self):
        result = diagnose_chapter("Chapter 8:\nToo short.", 7)
        codes = {item["code"] for item in result["errors"]}
        self.assertFalse(result["valid"])
        self.assertIn("chapter_number", codes)
        warning_codes = {item["code"] for item in result["warnings"]}
        self.assertIn("word_count", warning_codes)

    def test_valid_chapter_keeps_repeated_name_and_numeric_anchors(self):
        text = _chapter()
        anchors = extract_critical_anchors(text)
        self.assertIn("Alice", anchors)
        self.assertIn("17", anchors)
        result = diagnose_chapter(text, 3, min_words=100, max_words=2000, required_anchors=anchors)
        self.assertTrue(result["valid"], result)

    def test_sentence_initial_title_case_is_not_automatically_an_anchor(self):
        anchors = extract_critical_anchors(
            "When Alice Entered, rain fell. Later the gate closed. Nothing repeated."
        )
        self.assertNotIn("When Alice", anchors)
        self.assertNotIn("When Alice Entered", anchors)

    def test_possessive_outline_anchors_use_base_names(self):
        outline = (
            "Esmee's ledger sits beside Priya's map. "
            "She counts the remaining hours. "
            "Then the street goes quiet. "
            "They wait for the signal. "
            "Esmee's ledger sits beside Priya's map. "
            "She counts the remaining hours. "
            "Then the street goes quiet. "
            "They wait for the signal."
        )
        anchors = extract_critical_anchors(outline)
        self.assertIn("Esmee", anchors)
        self.assertNotIn("Esmee's", anchors)
        self.assertNotIn("She", anchors)
        self.assertNotIn("Then", anchors)
        self.assertNotIn("They", anchors)

    def test_possessive_name_is_deduped_with_base(self):
        anchors = extract_critical_anchors(
            "Esmee crossed the square. Esmee's coat was wet."
        )
        self.assertIn("Esmee", anchors)
        self.assertEqual(anchors.count("Esmee"), 1)
        self.assertNotIn("Esmee's", anchors)

    def test_ordinary_negative_coordination_is_allowed(self):
        self.assertEqual(
            forbidden_diagnostics("He was not ready, but he went when the bell rang."),
            [],
        )

    def test_rewrite_drops_facts_and_increases_reference_similarity(self):
        original = _chapter(count=120)
        reference = "Chapter 3: Sample\n" + "red fox crosses quiet bridge at dawn " * 30
        candidate = "Chapter 3: The Return\n" + "red fox crosses quiet bridge at dawn " * 20
        result = diagnose_rewrite(
            original, candidate, 3,
            required_anchors=["Alice", "17"], reference_text=reference,
        )
        codes = {item["code"] for item in result["warnings"]}
        self.assertIn("anchor_retention", codes)
        self.assertIn("reference_similarity_regression", codes)
        self.assertGreater(phrase_similarity(candidate, reference), 0.8)

    def test_pov_tense_and_premature_reveal_are_diagnosed(self):
        text = "Chapter 3: Wrong Turn\n" + (
            "He walked home and said the hidden heir was Rowan. " * 80
        )
        result = diagnose_chapter(
            text, 3, min_words=20, max_words=1000,
            expected_pov="first", expected_tense="present",
            premature_reveal_markers=["hidden heir"],
        )
        codes = {item["code"] for item in result["warnings"]}
        self.assertIn("pov", codes)
        self.assertIn("tense", codes)
        self.assertIn("premature_reveal", codes)

    def test_legacy_headings_are_readable_but_rejected_for_new_emission(self):
        heading = "\u7b2c1\u7ae0:\u65e7\u6807\u9898"
        chapter = heading + "\n" + ("content " * 700)
        self.assertFalse(diagnose_chapter(chapter, 1, min_words=100, max_words=2000)["valid"])
        self.assertTrue(diagnose_chapter(chapter, 1, min_words=100, max_words=2000, allow_legacy_heading=True)["valid"])
        outline = (
            "\u3010\u7b2c1\u7ae0\u5927\u7eb2\u3011\n"
            "# \u6545\u4e8b\u7ebf\nline\n"
            "# \u7ae0\u8282\u8282\u594f\nfast\n"
            "# \u7ae0\u8282\u6458\u8981\nsummary"
        )
        self.assertFalse(diagnose_chapter_outline(outline, 1)["valid"])
        self.assertTrue(diagnose_chapter_outline(outline, 1, allow_legacy_heading=True)["valid"])

    def test_chapter_word_bounds_env_override(self):
        text = "Chapter 1: Test\n" + ("word " * 250)
        with mock.patch.dict(os.environ, {"HARNESS_NOVEL_MAX_WORDS": "100"}):
            result = diagnose_chapter(text, 1)
            codes = {item["code"] for item in result["warnings"]}
            self.assertIn("word_count", codes)
        with mock.patch.dict(os.environ, {
                "HARNESS_NOVEL_MIN_WORDS": "100",
                "HARNESS_NOVEL_MAX_WORDS": "500"}):
            result = diagnose_chapter(text, 1)
            codes = {item["code"] for item in result["warnings"]}
            self.assertNotIn("word_count", codes)

    def test_is_numeric_anchor(self):
        self.assertTrue(_is_numeric_anchor("14"))
        self.assertFalse(_is_numeric_anchor("Esmee"))
        self.assertTrue(_is_numeric_anchor("1.5"))
        self.assertTrue(_is_numeric_anchor("3rd"))
        self.assertTrue(_is_numeric_anchor("20%"))

    def test_is_core_character_name(self):
        for name in ("Esmee", "Priya", "Carol", "Halloway"):
            self.assertTrue(is_core_character_name(name), name)
        for token in (
            "Saturday", "Sunday", "Monday", "Situation", "Under",
            "I-cup", "K-cup", "The I-cup", "Then",
        ):
            self.assertFalse(is_core_character_name(token), token)

    def test_split_outline_anchors(self):
        core, numeric, other = split_outline_anchors(
            ["Esmee", "Saturday", "Situation", "I-cup", "14"]
        )
        self.assertEqual(core, ["Esmee"])
        self.assertEqual(numeric, ["14"])
        self.assertEqual(other, ["Saturday", "Situation", "I-cup"])


if __name__ == "__main__":
    unittest.main()
