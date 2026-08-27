import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from training.artifact_provenance import sidecar_path, write_artifact, write_provenance
from training.quality_report import build_quality_report, format_quality_report
from webui.task_runner import WorkspaceStore


class QualityReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.workspace = self.home / "sample"
        self.workspace.mkdir()
        self.file_system = self.workspace / "file_system"
        self._write_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_fixture(self):
        current = self.file_system / "story_design" / "current.md"
        write_artifact(str(current), "current artifact", "story_design")

        stale = self.file_system / "chapters" / "chapter_001.md"
        write_artifact(str(stale), "saved draft", "chapter")
        stale.write_text("changed draft", encoding="utf-8")

        malformed = self.file_system / "story_design" / "broken.md"
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("kept artifact", encoding="utf-8")
        Path(sidecar_path(str(malformed))).write_text("{not json", encoding="utf-8")

        diagnostics = self.file_system / "quality_diagnostics" / "vol_01"
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / "chapter_001_draft.json").write_text(json.dumps({
            "valid": True,
            "decision": "accepted",
            "operation": "draft_generation",
            "errors": [],
            "warnings": [{"code": "legacy_heading"}],
        }), encoding="utf-8")
        (diagnostics / "chapter_001_humanize.json").write_text(json.dumps({
            "valid": False,
            "decision": "kept_original",
            "operation": "humanize",
            "errors": [{"code": "anchor_retention"}],
            "warnings": [],
        }), encoding="utf-8")
        (diagnostics / "broken.json").write_text("{not json", encoding="utf-8")

    def test_report_summarizes_sidecars_diagnostics_and_hash_drift(self):
        report = build_quality_report(self.workspace)

        provenance = report["provenance"]
        self.assertEqual(provenance["total"], 3)
        self.assertEqual(provenance["current"], 1)
        self.assertEqual(provenance["stale"], 1)
        self.assertEqual(provenance["legacy_or_invalid"], 1)
        self.assertEqual(provenance["content_hash_drift"], 1)
        self.assertEqual(provenance["by_kind"], {"chapter": 1, "story_design": 1})

        diagnostics = report["quality_diagnostics"]
        self.assertEqual(diagnostics["total"], 3)
        self.assertEqual(diagnostics["valid"], 1)
        self.assertEqual(diagnostics["invalid"], 1)
        self.assertEqual(diagnostics["malformed_or_unreadable"], 1)
        self.assertEqual(diagnostics["error_count"], 1)
        self.assertEqual(diagnostics["warning_count"], 1)
        self.assertEqual(diagnostics["decision_counts"], {"accepted": 1, "kept_original": 1})
        self.assertEqual(diagnostics["errors_by_code"], {"anchor_retention": 1})
        self.assertEqual(diagnostics["warnings_by_code"], {"legacy_heading": 1})
        self.assertEqual(diagnostics["by_stage"]["vol_01"], {"invalid": 1, "total": 2, "valid": 1})
        self.assertEqual(diagnostics["by_operation"], {"draft_generation": 1, "humanize": 1})
        self.assertIn("signals, not literary certification", format_quality_report(report))

    def test_path_input_stays_scoped_to_that_workspace(self):
        report = build_quality_report(Path(self.workspace))

        self.assertEqual(report["provenance"]["total"], 3)
        self.assertEqual(report["quality_diagnostics"]["total"], 3)

    def test_symlink_artifact_is_invalid_without_reading_its_target(self):
        outside = self.home / "outside.md"
        outside.write_text("outside workspace", encoding="utf-8")
        linked = self.file_system / "story_design" / "linked.md"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest("Symbolic links are unavailable: %s" % exc)
        write_provenance(str(linked), "story_design", "outside workspace")

        report = build_quality_report(self.workspace)

        self.assertEqual(report["provenance"]["legacy_or_invalid"], 2)
        linked_example = next(
            item for item in report["provenance"]["examples"]
            if item["artifact_path"].endswith("linked.md")
        )
        self.assertEqual(linked_example["reason"], "Artifact is a symbolic link")

    def test_symlink_workspace_or_file_system_is_not_traversed(self):
        root_alias = self.home / "workspace-alias"
        redirected_workspace = self.home / "redirected"
        redirected_workspace.mkdir()
        try:
            root_alias.symlink_to(self.workspace, target_is_directory=True)
            (redirected_workspace / "file_system").symlink_to(
                self.file_system, target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest("Symbolic links are unavailable: %s" % exc)

        root_report = build_quality_report(root_alias)
        file_system_report = build_quality_report(redirected_workspace)

        self.assertEqual(root_report["provenance"]["total"], 0)
        self.assertEqual(root_report["quality_diagnostics"]["total"], 0)
        self.assertEqual(file_system_report["provenance"]["total"], 0)
        self.assertEqual(file_system_report["quality_diagnostics"]["total"], 0)

    def test_workspace_summary_reuses_the_report_shape(self):
        summary = WorkspaceStore(self.home).summary("sample")

        self.assertEqual(summary["quality_report"], build_quality_report(self.workspace))

    def test_cli_json_output_is_read_only_and_machine_readable(self):
        environment = os.environ.copy()
        environment["HARNESS_NOVEL_HOME"] = str(self.home)
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "novel_cli.py"),
            "quality-report",
            "sample",
            "--json",
        ]
        result = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload, build_quality_report(self.workspace))


if __name__ == "__main__":
    unittest.main()
