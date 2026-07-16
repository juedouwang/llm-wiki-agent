#!/usr/bin/env python3
"""Deterministic B-08 coverage and failure reporting for Manifest v4."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .file_classification import classification_from_dict
    from .file_state import file_state_from_dict
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifestError,
        load_project_manifest,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        parse_versioned_json_bytes,
    )
    from .project_registry import ProjectRegistrationResult, load_registered_project
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from file_classification import classification_from_dict  # type: ignore[no-redef]
    from file_state import file_state_from_dict  # type: ignore[no-redef]
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectManifestError,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        parse_versioned_json_bytes,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
    )


COVERAGE_REPORT_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
COVERAGE_REPORT_KIND = "llmwiki-coverage-report"
COVERAGE_REPORT_VERSION = "coverage-report-v1"
COVERAGE_REPORT_FILENAME = "coverage-report.json"

_AXIS_NAMES = (
    "research_role",
    "processing_status",
    "read_depth",
    "reason",
)


class CoverageReportError(LayoutError):
    """Raised when a deterministic coverage report cannot be produced safely."""


@dataclass(frozen=True)
class CoverageReportResult:
    """Paths and persisted payload for one B-08 report generation."""

    project_id: str
    manifest_file: Path
    report_file: Path
    report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "report_file": str(self.report_file),
            "report": self.report,
        }


def _validated_relative_path(value: object, *, manifest_file: Path) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectManifestError(
            f"invalid project Manifest at {manifest_file}: "
            "file path must be a non-empty project-relative string"
        )
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ProjectManifestError(
            f"invalid project Manifest at {manifest_file}: "
            f"file path is not normalized project-relative POSIX form: {value!r}"
        )
    return value


def _bucket_payload(file_count: int, byte_count: int) -> dict[str, int]:
    return {"file_count": file_count, "byte_count": byte_count}


def _sorted_bucket_map(
    buckets: dict[str, list[int]],
) -> dict[str, dict[str, int]]:
    return {
        key: _bucket_payload(values[0], values[1])
        for key, values in sorted(buckets.items())
    }


def _reconcile_axis(
    axis: str,
    buckets: dict[str, dict[str, Any]],
    *,
    expected_files: int,
    expected_bytes: int,
) -> dict[str, int]:
    file_count = sum(bucket["file_count"] for bucket in buckets.values())
    byte_count = sum(bucket["byte_count"] for bucket in buckets.values())
    if file_count != expected_files or byte_count != expected_bytes:
        raise CoverageReportError(
            f"coverage axis {axis!r} does not reconcile with Manifest ordinary files"
        )
    return _bucket_payload(file_count, byte_count)


def build_coverage_report(
    *,
    project_id: str,
    manifest_file: Path,
    manifest_version: str,
    scan_generation: int,
    manifest_file_count: int,
    file_records: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Build a stable report from already validated Manifest v4 file rows."""

    if manifest_version != PROJECT_MANIFEST_VERSION:
        raise CoverageReportError(
            f"coverage reporting requires {PROJECT_MANIFEST_VERSION!r}, "
            f"found {manifest_version!r}"
        )
    if manifest_file_count != len(file_records):
        raise CoverageReportError(
            "Manifest ordinary-file count changed after validation"
        )

    buckets: dict[str, dict[str, list[int]]] = {
        axis: defaultdict(lambda: [0, 0]) for axis in _AXIS_NAMES
    }
    reason_details: dict[str, set[str]] = defaultdict(set)
    failures: list[dict[str, Any]] = []
    total_bytes = 0

    for record in file_records:
        path = _validated_relative_path(record.get("path"), manifest_file=manifest_file)
        size_bytes = record.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise ProjectManifestError(
                f"invalid project Manifest at {manifest_file}: "
                f"file {path!r} has invalid size_bytes"
            )
        classification = classification_from_dict(record.get("classification"))
        state = file_state_from_dict(record.get("file_state"))

        dimensions = {
            "research_role": classification.research_role,
            "processing_status": state.processing_status,
            "read_depth": state.read_depth,
            "reason": state.reason_code,
        }
        for axis, value in dimensions.items():
            buckets[axis][value][0] += 1
            buckets[axis][value][1] += size_bytes
        reason_details[state.reason_code].add(state.reason)
        total_bytes += size_bytes

        if state.processing_status == "failed":
            failures.append(
                {
                    "path": path,
                    "byte_count": size_bytes,
                    "research_role": classification.research_role,
                    "processing_status": state.processing_status,
                    "read_depth": state.read_depth,
                    "reason_code": state.reason_code,
                    "reason": state.reason,
                }
            )

    coverage: dict[str, dict[str, dict[str, Any]]] = {
        axis: _sorted_bucket_map(axis_buckets)
        for axis, axis_buckets in buckets.items()
    }
    for reason_code, details in sorted(reason_details.items()):
        coverage["reason"][reason_code]["details"] = sorted(details)

    totals = _bucket_payload(len(file_records), total_bytes)
    totals["failed_file_count"] = len(failures)
    reconciliation = {
        "manifest": _bucket_payload(manifest_file_count, total_bytes),
        "axes": {
            axis: _reconcile_axis(
                axis,
                coverage[axis],
                expected_files=len(file_records),
                expected_bytes=total_bytes,
            )
            for axis in _AXIS_NAMES
        },
    }
    failures.sort(key=lambda item: (item["path"].casefold(), item["path"]))

    return {
        "schema_version": COVERAGE_REPORT_SCHEMA_VERSION,
        "kind": COVERAGE_REPORT_KIND,
        "report_version": COVERAGE_REPORT_VERSION,
        "project_id": project_id,
        "manifest": {
            "manifest_version": manifest_version,
            "scan_generation": scan_generation,
        },
        "totals": totals,
        "coverage": coverage,
        "failures": failures,
        "reconciliation": reconciliation,
    }


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise CoverageReportError(
            f"registered machine-state index directory is unavailable: {path.parent}"
        )
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_temporary)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as destination:
            descriptor = -1
            destination.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise CoverageReportError(f"could not write coverage report {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_existing_report_schema(path: Path, *, project_id: str) -> None:
    """Fail closed instead of overwriting legacy, future, or malformed state."""

    if path.is_symlink():
        raise CoverageReportError(f"coverage report must not be a symbolic link: {path}")
    if not path.exists():
        return
    if not path.is_file():
        raise CoverageReportError(f"coverage report is not a file: {path}")
    try:
        payload = path.read_bytes()
        document = parse_versioned_json_bytes(
            payload,
            path=path,
            allow_legacy=True,
            max_supported=COVERAGE_REPORT_SCHEMA_VERSION,
        )
    except LayoutError:
        raise
    except OSError as exc:
        raise CoverageReportError(f"could not read coverage report {path}: {exc}") from exc
    if document.is_legacy:
        raise CoverageReportError(
            f"coverage report is legacy v0 and will not be rewritten: {path}"
        )
    record = document.data
    if (
        record.get("kind") != COVERAGE_REPORT_KIND
        or record.get("report_version") != COVERAGE_REPORT_VERSION
        or record.get("project_id") != project_id
    ):
        raise CoverageReportError(
            "existing coverage report does not match the current project contract"
        )


def _generate_coverage_report_locked(
    registration: ProjectRegistrationResult,
) -> CoverageReportResult:
    """Build coverage while the shared project machine-state lock is held."""

    manifest = load_project_manifest(
        registration.layout.manifest_file,
        project_id=registration.project_id,
        project_root=registration.project_root,
        required_manifest_version=PROJECT_MANIFEST_VERSION,
    )
    report = build_coverage_report(
        project_id=registration.project_id,
        manifest_file=manifest.manifest_file,
        manifest_version=manifest.manifest_version,
        scan_generation=manifest.scan_generation,
        manifest_file_count=manifest.summary["record_counts"]["file"],
        file_records=manifest.file_records,
    )
    report_file = registration.layout.indexes_dir / COVERAGE_REPORT_FILENAME
    _validate_existing_report_schema(
        report_file,
        project_id=registration.project_id,
    )
    _write_atomic_json(report_file, report)
    return CoverageReportResult(
        project_id=registration.project_id,
        manifest_file=manifest.manifest_file,
        report_file=report_file,
        report=report,
    )

def generate_coverage_report(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CoverageReportResult:
    """Validate Manifest v4 and atomically write its deterministic audit report.

    The shared per-project machine-state lock binds the Manifest read, report
    construction, existing-state validation, and atomic report replacement.
    """

    registration = load_registered_project(workspace_root, project_id)
    lock = AdvisoryFileLock(
        registration.layout.machine_state_lock_file,
        timeout_seconds=lock_timeout_seconds,
    )
    try:
        lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise CoverageReportError(
            "timed out waiting for the project machine-state lock"
        ) from exc
    except AdvisoryLockError as exc:
        raise CoverageReportError(
            "could not acquire the project machine-state lock"
        ) from exc
    try:
        return _generate_coverage_report_locked(registration)
    finally:
        lock.release()
