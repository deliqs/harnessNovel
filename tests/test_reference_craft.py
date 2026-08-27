import json
from pathlib import Path
import tempfile
import unittest

from training.reference_analyzer import ReferenceAnalyzer
from training.reference_craft import (
    load_reference_craft_bible,
    normalize_reference_craft_bible,
    render_reference_craft_bible,
)


class CraftLLM:
    def generate(self, *args, **kwargs):
        return json.dumps({
            "narrative_profile": {
                "pov_tense_tendencies": ["Mostly close third-person past tense."],
                "scene_patterns": ["Scenes enter on an active problem."],
                "rhythm_patterns": ["Pressure rises before a short consequence beat."],
            },
            "techniques": [{
                "name": "Consequence bridge",
                "observation": "A result immediately changes the next objective.",
                "transferable_principle": "End a resolved beat by exposing a causally related next pressure.",
                "when_to_use": "At scene or chapter transitions.",
                "failure_mode": "Unrelated hooks feel arbitrary.",
                "evidence_refs": [
                    {"chapter": 1, "source_span": "ending", "observed_signal": "The outcome creates the next constraint."},
                    {"chapter": 2, "source_span": "ending", "observed_signal": "The solved task reveals a related cost."},
                ],
                "confidence": {"score": 0.85, "reason": "repeats across chapters"},
                "uncertainty": "Only two chapters are available.",
            }],
            "global_uncertainties": ["The sample is short."],
        })


class ReferenceCraftTests(unittest.TestCase):
    @staticmethod
    def _cards():
        return [{
            "chapter": number,
            "title": f"Chapter {number}",
            "content_digest": f"digest-{number}",
            "source_location": {"chapter": number},
            "pov_tense": {"pov": "third-person limited", "tense": "past"},
            "chapter_rhythm": {"core_content": "goal + pressure + consequence"},
            "story_line": "goal + pressure + consequence",
            "craft_observations": [{
                "technique": "consequence bridge",
                "source_span": "ending",
                "evidence": "The result alters the next goal.",
            }],
        } for number in (1, 2)]

    def test_normalizer_requires_real_chapter_provenance(self):
        payload = json.loads(CraftLLM().generate())
        payload["techniques"][0]["evidence_refs"] = [{
            "chapter": 999,
            "source_span": "ending",
            "observed_signal": "Unsupported evidence.",
        }]
        with self.assertRaises(ValueError):
            normalize_reference_craft_bible(payload, self._cards(), "fingerprint")

    def test_analyzer_writes_structured_and_readable_craft_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analyzer = ReferenceAnalyzer(
                root / "source.txt",
                root / "reference",
                llm=CraftLLM(),
            )
            analyzer.state = {}
            relative_path = analyzer._ensure_craft_bible(self._cards())

            self.assertEqual(relative_path, "outlines/reference_craft_bible.json")
            bible = load_reference_craft_bible(root / "reference" / "outlines")
            self.assertIsNotNone(bible)
            self.assertEqual(bible["techniques"][0]["confidence"]["level"], "high")
            self.assertEqual(bible["techniques"][0]["evidence_refs"][1]["chapter"], 2)
            rendered = render_reference_craft_bible(bible)
            self.assertIn("Anti-copy constraints", rendered)
            self.assertIn("Do not reuse or closely paraphrase", rendered)
            self.assertIn("chapter 1 (ending)", rendered)


if __name__ == "__main__":
    unittest.main()
