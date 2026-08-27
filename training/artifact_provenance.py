"""Atomic artifact writes and optional dependency provenance.

Sidecars are deliberately additive: artifacts from older workspaces remain valid
when no sidecar exists.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime


SIDECAR_SUFFIX = ".provenance.json"


def content_hash(content):
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def dependency_hashes(dependencies):
    """Hash a mapping of dependency labels to their current text values."""
    return {
        str(name): content_hash(value)
        for name, value in sorted((dependencies or {}).items())
    }


def sidecar_path(artifact_path):
    return str(artifact_path) + SIDECAR_SUFFIX


def atomic_write_text(path, content):
    """Replace a UTF-8 text file atomically, leaving the old file on failure."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".artifact-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(content or ""))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_provenance(artifact_path):
    """Return sidecar data, or None for legacy/malformed sidecars."""
    path = sidecar_path(artifact_path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_provenance(artifact_path, kind, content, dependencies=None, metadata=None):
    payload = {
        "version": 1,
        "kind": str(kind),
        "status": "current",
        "content_hash": content_hash(content),
        "dependencies": dependency_hashes(dependencies),
        "metadata": dict(metadata or {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_text(
        sidecar_path(artifact_path),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return payload


def write_artifact(artifact_path, content, kind, dependencies=None, metadata=None):
    """Atomically write content, then record its dependency snapshot."""
    rendered = str(content).rstrip() + "\n"
    atomic_write_text(artifact_path, rendered)
    return write_provenance(
        artifact_path, kind, rendered, dependencies=dependencies, metadata=metadata,
    )


def dependency_status(artifact_path, dependencies):
    """Compare dependencies without making legacy artifacts unusable."""
    payload = read_provenance(artifact_path)
    if payload is None:
        return {"status": "legacy", "changed": []}
    expected = payload.get("dependencies") or {}
    current = dependency_hashes(dependencies)
    changed = sorted(
        name for name in set(expected).union(current)
        if expected.get(name) != current.get(name)
    )
    try:
        artifact_hash = _file_hash(artifact_path)
    except OSError:
        artifact_hash = None
    content_changed = artifact_hash != payload.get("content_hash")
    if content_changed:
        changed.append("artifact_content")
    status = "stale" if changed or payload.get("status") == "stale" else "current"
    return {
        "status": status,
        "changed": sorted(set(changed)),
        "content_changed": content_changed,
    }


def mark_stale(artifact_path, reason, changed_dependency=None):
    """Mark a present downstream artifact stale without deleting its content."""
    if not os.path.isfile(artifact_path):
        return None
    payload = read_provenance(artifact_path) or {
        "version": 1,
        "kind": "legacy",
        "content_hash": _file_hash(artifact_path),
        "dependencies": {},
        "metadata": {},
    }
    payload["status"] = "stale"
    payload["stale_reason"] = str(reason)
    if changed_dependency:
        payload["changed_dependency"] = str(changed_dependency)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_text(
        sidecar_path(artifact_path),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return payload


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
