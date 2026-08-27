"""Read-only workspace quality and provenance reporting.

This module only inspects existing JSON sidecars and diagnostics. It never calls a
model, rewrites an artifact, or treats deterministic diagnostics as a literary
quality certification.
"""

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from training.artifact_provenance import SIDECAR_SUFFIX


REPORT_VERSION = 1
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EXAMPLE_LIMIT = 20


def build_quality_report(workspace):
    """Return a stable, read-only report for a workspace path or NovelWorkspace."""
    if hasattr(workspace, "root") and hasattr(workspace, "file_system"):
        root = Path(workspace.root)
    else:
        root = Path(workspace)
    root = Path(os.path.abspath(str(root.expanduser())))
    file_system = root / "file_system"
    return {
        "version": REPORT_VERSION,
        "provenance": _provenance_summary(root, reject_symlink_root=True),
        "quality_diagnostics": _diagnostics_summary(
            file_system / "quality_diagnostics",
            reject_symlink_root=True,
            workspace_root=root,
            file_system_root=file_system,
        ),
    }


def format_quality_report(report):
    """Render the JSON report for CLI use without exposing artifact content."""
    provenance = report.get("provenance") or {}
    diagnostics = report.get("quality_diagnostics") or {}
    lines = [
        "Quality report (read-only; diagnostics are signals, not literary certification)",
        "",
        "Provenance sidecars:",
        "  total: {total}; current: {current}; stale: {stale}; legacy or invalid: {legacy_or_invalid}; content-hash drift: {content_hash_drift}".format(
            total=provenance.get("total", 0),
            current=provenance.get("current", 0),
            stale=provenance.get("stale", 0),
            legacy_or_invalid=provenance.get("legacy_or_invalid", 0),
            content_hash_drift=provenance.get("content_hash_drift", 0),
        ),
        "Quality diagnostics:",
        "  total: {total}; valid: {valid}; invalid: {invalid}; malformed or unreadable: {malformed_or_unreadable}; errors: {error_count}; warnings: {warning_count}".format(
            total=diagnostics.get("total", 0),
            valid=diagnostics.get("valid", 0),
            invalid=diagnostics.get("invalid", 0),
            malformed_or_unreadable=diagnostics.get("malformed_or_unreadable", 0),
            error_count=diagnostics.get("error_count", 0),
            warning_count=diagnostics.get("warning_count", 0),
        ),
    ]
    decisions = diagnostics.get("decision_counts") or {}
    if decisions:
        lines.append("  decisions: " + _render_counts(decisions))
    errors = diagnostics.get("errors_by_code") or {}
    if errors:
        lines.append("  error codes: " + _render_counts(errors))
    warnings = diagnostics.get("warnings_by_code") or {}
    if warnings:
        lines.append("  warning codes: " + _render_counts(warnings))
    return "\n".join(lines)


def _provenance_summary(root, reject_symlink_root=False):
    summary = {
        "total": 0,
        "current": 0,
        "stale": 0,
        "legacy_or_invalid": 0,
        "content_hash_drift": 0,
        "by_kind": {},
        "examples": [],
        "omitted_example_count": 0,
    }
    if not root.is_dir() or (reject_symlink_root and root.is_symlink()):
        return summary
    examples = []
    kinds = Counter()
    for sidecar in _iter_files(root, suffix=SIDECAR_SUFFIX):
        summary["total"] += 1
        artifact = Path(str(sidecar)[:-len(SIDECAR_SUFFIX)])
        item = {
            "path": _relative_path(sidecar, root),
            "artifact_path": _relative_path(artifact, root),
        }
        payload = _read_json(sidecar)
        category, reason, drift, kind = _provenance_status(payload, artifact)
        item["status"] = category
        if reason:
            item["reason"] = reason
        if drift:
            item["content_hash_drift"] = True
            summary["content_hash_drift"] += 1
        if kind:
            item["kind"] = kind
            kinds[kind] += 1
        summary[category] += 1
        examples.append(item)
    summary["by_kind"] = dict(sorted(kinds.items()))
    _set_examples(summary, examples)
    return summary


def _provenance_status(payload, artifact):
    if not isinstance(payload, dict):
        return "legacy_or_invalid", "Sidecar is malformed or unreadable", False, ""
    kind = str(payload.get("kind") or "")
    expected_hash = payload.get("content_hash")
    status = str(payload.get("status") or "")
    if payload.get("version") != 1 or not kind or not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(expected_hash):
        return "legacy_or_invalid", "Sidecar does not match the current provenance schema", False, kind
    if kind == "legacy":
        return "legacy_or_invalid", "Sidecar identifies a legacy artifact", False, kind
    if artifact.is_symlink():
        return "legacy_or_invalid", "Artifact is a symbolic link", False, kind
    if not artifact.is_file():
        return "legacy_or_invalid", "Artifact file is missing", False, kind
    try:
        drift = _file_hash(artifact) != expected_hash
    except OSError:
        return "legacy_or_invalid", "Artifact file could not be read", False, kind
    if status == "stale" or drift:
        reason = "Content hash differs from the sidecar" if drift else "Sidecar is marked stale"
        return "stale", reason, drift, kind
    if status == "current":
        return "current", "", False, kind
    return "legacy_or_invalid", "Sidecar has an unknown status", drift, kind


def _diagnostics_summary(
    directory, reject_symlink_root=False, workspace_root=None, file_system_root=None,
):
    summary = {
        "total": 0,
        "valid": 0,
        "invalid": 0,
        "malformed_or_unreadable": 0,
        "error_count": 0,
        "warning_count": 0,
        "decision_counts": {},
        "errors_by_code": {},
        "warnings_by_code": {},
        "by_stage": {},
        "by_operation": {},
        "examples": [],
        "omitted_example_count": 0,
    }
    if not directory.is_dir() or (
        reject_symlink_root
        and (
            directory.is_symlink()
            or (workspace_root is not None and workspace_root.is_symlink())
            or (file_system_root is not None and file_system_root.is_symlink())
        )
    ):
        return summary
    decisions = Counter()
    errors_by_code = Counter()
    warnings_by_code = Counter()
    stages = defaultdict(Counter)
    operations = Counter()
    examples = []
    for path in _iter_files(directory, suffix=".json"):
        summary["total"] += 1
        payload = _read_json(path)
        if not _is_diagnostic(payload):
            summary["malformed_or_unreadable"] += 1
            examples.append({
                "path": _relative_path(path, directory),
                "status": "malformed_or_unreadable",
            })
            continue
        valid = bool(payload["valid"])
        summary["valid" if valid else "invalid"] += 1
        decision = _text_or_default(payload.get("decision"), "unspecified")
        operation = _text_or_default(payload.get("operation"), _operation_from_path(path))
        stage = _text_or_default(payload.get("stage"), _stage_from_path(path, directory))
        decisions[decision] += 1
        operations[operation] += 1
        stages[stage]["total"] += 1
        stages[stage]["valid" if valid else "invalid"] += 1
        errors = _findings(payload.get("errors"))
        warnings = _findings(payload.get("warnings"))
        summary["error_count"] += len(errors)
        summary["warning_count"] += len(warnings)
        for code in errors:
            errors_by_code[code] += 1
        for code in warnings:
            warnings_by_code[code] += 1
        examples.append({
            "path": _relative_path(path, directory),
            "status": "valid" if valid else "invalid",
            "decision": decision,
            "stage": stage,
            "operation": operation,
            "error_count": len(errors),
            "warning_count": len(warnings),
        })
    summary["decision_counts"] = dict(sorted(decisions.items()))
    summary["errors_by_code"] = dict(sorted(errors_by_code.items()))
    summary["warnings_by_code"] = dict(sorted(warnings_by_code.items()))
    summary["by_stage"] = {
        name: dict(sorted(counts.items())) for name, counts in sorted(stages.items())
    }
    summary["by_operation"] = dict(sorted(operations.items()))
    _set_examples(summary, examples)
    return summary


def _is_diagnostic(payload):
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("valid"), bool)
        and isinstance(payload.get("errors"), list)
        and isinstance(payload.get("warnings"), list)
    )


def _findings(value):
    findings = []
    for item in value:
        if isinstance(item, dict):
            findings.append(_text_or_default(item.get("code"), "unspecified"))
        else:
            findings.append("unspecified")
    return findings


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _iter_files(directory, suffix):
    """Yield matching files below a workspace directory without following links."""
    if not directory.is_dir():
        return
    for current, directories, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories
            if not (current_path / name).is_symlink()
        )
        for name in sorted(filenames):
            path = current_path / name
            if name.endswith(suffix) and path.is_file() and not path.is_symlink():
                yield path


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _text_or_default(value, default):
    value = str(value or "").strip()
    return value or default


def _operation_from_path(path):
    parts = path.stem.split("_")
    return parts[-1] if parts else "unspecified"


def _stage_from_path(path, directory):
    try:
        parts = path.relative_to(directory).parts
    except ValueError:
        parts = path.parts
    for part in parts[:-1]:
        if re.fullmatch(r"vol_\d+", part, re.IGNORECASE):
            return part
    return "unspecified"


def _set_examples(summary, examples):
    summary["examples"] = examples[:_EXAMPLE_LIMIT]
    summary["omitted_example_count"] = max(0, len(examples) - _EXAMPLE_LIMIT)


def _render_counts(counts):
    return ", ".join("%s=%s" % (name, counts[name]) for name in sorted(counts))
