"""Draft-style hard check: rhetorical contrast templates, not ordinary coordination."""
import unittest

from training.adaptive_builder import _chapter_style_violations

_EN_CONTRAST = "not X but Y contrast template"
_ZH_CONTRAST = "Chinese not-X-but-Y contrast template"
_EN_NOT_ONLY = "not only X but also Y template"
_EM_DASH = "em dash '——'"


def _labels(text):
    return [item["label"] for item in _chapter_style_violations(text)]


class ChapterStyleViolationsTests(unittest.TestCase):
    def test_english_contrast_formula_flags(self):
        samples = (
            "It was not luck, but skill.",
            "This is not a request, but an order.",
            "What she felt was not relief, but dread.",
            "Not love, but possession.",
            "It is not because she was kind, but because she was afraid.",
            "He is not a hero, but a survivor.",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertIn(_EN_CONTRAST, _labels(text))

    def test_ordinary_not_but_coordination_does_not_flag(self):
        samples = (
            "The bra did not dig in, but it did not move either.",
            "He did not stop, but he slowed his pace as he walked past the door.",
            "She could not see him, but she heard him.",
            "I do not know, but I can guess.",
            "There's not much time, but we'll try.",
            "Do not speak unless spoken to.",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertNotIn(_EN_CONTRAST, _labels(text))
                self.assertEqual(_labels(text), [])

    def test_other_hard_patterns_unchanged(self):
        self.assertIn(_ZH_CONTRAST, _labels("这不是请求，而是命令。"))
        self.assertIn(_EN_NOT_ONLY, _labels("She was not only tired, but also angry."))
        self.assertNotIn(_EN_CONTRAST, _labels("She was not only tired, but also angry."))
        self.assertIn(_EM_DASH, _labels("Wait——she froze."))
