#!/usr/bin/env python3
"""Shared deterministic helpers for bounded project-analysis artifacts.

The helpers deliberately operate on the registered Manifest and machine-state
root only.  They never open the registered source project and never create
curated Markdown.  E-04 through E-06 use the same lock, Manifest binding, and
canonical JSON publication rules so their machine artifacts cannot drift apart.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterator, Mapping

if __package__:
    from .advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, AdvisoryFileLock
    from .project_inventory import PROJECT_MANIFEST_VERSION, ProjectManifest, load_project_manifest
    from .project_layout import LayoutError, ProjectLayout, validate_project_id
    from .project_registry import ProjectRegistrationResult, load_registered_project
else:  # pragma: no cover
    from advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, AdvisoryFileLock  # type: ignore[no-redef]
    from project_inventory import PROJECT_MANIFEST_VERSION, ProjectManifest, load_project_manifest  # type: ignore[no-redef]
    from project_layout import LayoutError, ProjectLayout, validate_project_id  # type: ignore[no-redef]
    from project_registry import ProjectRegistrationResult, load_registered_project  # type: ignore[no-redef]


class ProjectAnalysisError(LayoutError):
    """Base error for deterministic project-analysis artifacts."""


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProjectAnalysisError(f"cannot serialize canonical JSON: {exc}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectAnalysisError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_canonical_json(payload: bytes, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProjectAnalysisError(f"invalid JSON in {label}: {exc}") from exc
    if type(value) is not dict:
        raise ProjectAnalysisError(f"{label} must contain a JSON object")
    if canonical_json_bytes(value) != payload:
        raise ProjectAnalysisError(f"{label} is not canonical JSON")
    return value


def exact_mapping(value: object, fields: set[str] | frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ProjectAnalysisError(f"{label} must contain exactly {sorted(fields)!r}")
    return value


def text(value: object, *, label: str, max_bytes: int = 4096, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value) or "\x00" in value:
        raise ProjectAnalysisError(f"{label} must be {'non-empty ' if not allow_empty else ''}text without NUL")
    if value != value.encode("utf-8", errors="strict").decode("utf-8"):
        raise ProjectAnalysisError(f"{label} must be valid UTF-8")
    if len(value.encode("utf-8")) > max_bytes:
        raise ProjectAnalysisError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return value


def nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProjectAnalysisError(f"{label} must be an integer >= 0")
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def machine_path(layout: ProjectLayout, path: Path, *, allow_missing_leaf: bool = False) -> Path:
    try:
        return layout.validate_machine_state_path(path, allow_missing_leaf=allow_missing_leaf)
    except Exception as exc:
        raise ProjectAnalysisError(str(exc)) from exc


def manifest_snapshot(
    registration: ProjectRegistrationResult,
) -> tuple[ProjectManifest, bytes, str]:
    path = registration.layout.manifest_file
    machine_path(registration.layout, path)
    try:
        snapshot = path.read_bytes()
        manifest = load_project_manifest(
            path,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )
        if path.read_bytes() != snapshot:
            raise ProjectAnalysisError("Manifest changed while analysis was generated")
    except ProjectAnalysisError:
        raise
    except Exception as exc:
        raise ProjectAnalysisError(f"could not load current Manifest: {exc}") from exc
    return manifest, snapshot, sha256_bytes(snapshot)


@dataclass(frozen=True)
class LockedProject:
    registration: ProjectRegistrationResult
    lock: AdvisoryFileLock
    manifest: ProjectManifest
    manifest_bytes: bytes
    manifest_sha256: str


@contextmanager
def locked_project(
    workspace_root: str | Path,
    project_id: str,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[LockedProject]:
    registration = load_registered_project(workspace_root, validate_project_id(project_id))
    lock_path = registration.layout.machine_state_lock_file
    machine_path(registration.layout, lock_path, allow_missing_leaf=True)
    lock = AdvisoryFileLock(lock_path, timeout_seconds=timeout_seconds)
    lock.acquire()
    try:
        manifest, snapshot, digest = manifest_snapshot(registration)
        yield LockedProject(registration, lock, manifest, snapshot, digest)
    finally:
        lock.release()


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    layout: ProjectLayout,
    before_replace: Callable[[], None],
    error_label: str,
) -> None:
    machine_path(layout, path, allow_missing_leaf=True)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ProjectAnalysisError(f"machine-state index directory is unavailable: {path.parent}")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        machine_path(layout, temporary)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        before_replace()
        machine_path(layout, path, allow_missing_leaf=True)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except ProjectAnalysisError:
        raise
    except OSError as exc:
        raise ProjectAnalysisError(f"could not write {error_label}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def manifest_binding(manifest: ProjectManifest, digest: str) -> dict[str, Any]:
    records = manifest_records_by_path(manifest)
    count = len(records)
    total_bytes = 0
    for path, record in records.items():
        size = record.get("size_bytes")
        if type(size) is not int or size < 0:
            raise ProjectAnalysisError(f"Manifest file has invalid size_bytes: {path}")
        total_bytes += size
    record_counts = manifest.summary.get("record_counts")
    if type(record_counts) is not dict or record_counts.get("file") != count:
        raise ProjectAnalysisError("Manifest ordinary-file count does not reconcile")
    if type(manifest.scan_generation) is not int or manifest.scan_generation < 1:
        raise ProjectAnalysisError("Manifest scan_generation must be positive")
    return {
        "manifest_version": manifest.manifest_version,
        "scan_generation": manifest.scan_generation,
        "ordinary_file_count": count,
        "ordinary_byte_count": total_bytes,
        "sha256": digest,
    }


def manifest_records_by_path(manifest: ProjectManifest) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in manifest.records:
        if record.get("record_type") != "file":
            continue
        path = record.get("path")
        if isinstance(path, str):
            result[path] = record
    return result


def current_manifest_bytes(registration: ProjectRegistrationResult, expected: bytes) -> None:
    path = registration.layout.manifest_file
    machine_path(registration.layout, path)
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise ProjectAnalysisError(f"could not re-read current Manifest: {exc}") from exc
    if actual != expected:
        raise ProjectAnalysisError("Manifest changed before analysis artifact was committed; retry inventory")


__all__ = [
    "ProjectAnalysisError",
    "LockedProject",
    "atomic_write_json",
    "canonical_json_bytes",
    "current_manifest_bytes",
    "exact_mapping",
    "locked_project",
    "machine_path",
    "manifest_binding",
    "manifest_records_by_path",
    "manifest_snapshot",
    "nonnegative_int",
    "parse_canonical_json",
    "sha256_bytes",
    "text",
]
