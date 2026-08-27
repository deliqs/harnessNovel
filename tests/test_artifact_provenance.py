import json
import os
import tempfile
import unittest

from training.artifact_provenance import (
    dependency_status,
    mark_stale,
    read_provenance,
    sidecar_path,
    write_artifact,
)


class ArtifactProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "chapter.md")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_records_dependencies_and_detects_manual_content_change(self):
        write_artifact(self.path, "trusted", "chapter", {"outline": "one"})
        self.assertEqual(dependency_status(self.path, {"outline": "one"})["status"], "current")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("manual edit")
        status = dependency_status(self.path, {"outline": "one"})
        self.assertEqual(status["status"], "stale")
        self.assertTrue(status["content_changed"])
        self.assertIn("artifact_content", status["changed"])

    def test_legacy_and_malformed_sidecars_remain_readable(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("legacy")
        self.assertEqual(dependency_status(self.path, {})["status"], "legacy")
        with open(sidecar_path(self.path), "w", encoding="utf-8") as handle:
            handle.write("{malformed")
        self.assertIsNone(read_provenance(self.path))
        self.assertEqual(dependency_status(self.path, {})["status"], "legacy")

    def test_mark_stale_keeps_artifact_and_exposes_reason(self):
        write_artifact(self.path, "trusted", "chapter", {})
        mark_stale(self.path, "outline changed", "outline")
        self.assertTrue(os.path.isfile(self.path))
        payload = read_provenance(self.path)
        self.assertEqual(payload["status"], "stale")
        self.assertEqual(payload["changed_dependency"], "outline")


if __name__ == "__main__":
    unittest.main()
