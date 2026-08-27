import unittest

from training.generation_quality import (
    diagnose_chapter,
    diagnose_chapter_outline,
    diagnose_rewrite,
    extract_critical_anchors,
    forbidden_diagnostics,
    phrase_similarity,
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
        self.assertIn("word_count", codes)

    def test_valid_chapter_keeps_repeated_name_and_numeric_anchors(self):
        text = _chapter()
        anchors = extract_critical_anchors(text)
        self.assertIn("Alice", anchors)
        self.assertIn("17", anchors)
        result = diagnose_chapter(text, 3, required_anchors=anchors)
        self.assertTrue(result["valid"], result)

    def test_sentence_initial_title_case_is_not_automatically_an_anchor(self):
        anchors = extract_critical_anchors(
            "When Alice Entered, rain fell. Later the gate closed. Nothing repeated."
        )
        self.assertNotIn("When Alice", anchors)
        self.assertNotIn("When Alice Entered", anchors)

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
        codes = {item["code"] for item in result["errors"]}
        self.assertFalse(result["valid"])
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
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("pov", codes)
        self.assertIn("tense", codes)
        self.assertIn("premature_reveal", codes)

    def test_legacy_headings_are_readable_but_rejected_for_new_emission(self):
        chapter = "第1章：旧标题\n" + "内容 " * 700
        self.assertFalse(diagnose_chapter(chapter, 1)["valid"])
        self.assertTrue(diagnose_chapter(chapter, 1, allow_legacy_heading=True)["valid"])
        outline = "【第1章大纲】\n# 故事线\n线\n# 章节节奏\n快\n# 章节摘要\n摘要"
        self.assertFalse(diagnose_chapter_outline(outline, 1)["valid"])
        self.assertTrue(diagnose_chapter_outline(outline, 1, allow_legacy_heading=True)["valid"])


if __name__ == "__main__":
    unittest.main()
