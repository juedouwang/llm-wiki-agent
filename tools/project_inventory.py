#!/usr/bin/env python3
"""Incremental, source-read-only inventory for registered research projects.

B-03 consumes the B-01 project registration and B-02 scan policy, walks only
approved directory boundaries, and writes an accountable Schema v1 Manifest.
B-04 adds scan generations and deterministic regular-file fingerprints. B-05
adds bounded local format/language/research-role classification. B-06 adds a
versioned two-axis state without pretending that inventory is content extraction;
all stages remain local and source-project-read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# Support both ``import tools.project_inventory`` and direct sibling imports.
if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .file_classification import (
        CLASSIFICATION_SAMPLE_BYTES,
        FILE_CLASSIFICATION_SCHEMA_VERSION,
        FileClassification,
        classification_from_dict,
        classify_file,
    )
    from .file_state import (
        FILE_STATE_SCHEMA_VERSION,
        FileState,
        file_state_from_dict,
        inventory_file_state,
        state_is_compatible_with_inventory,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        schema_version_of,
    )
    from .project_registry import ProjectRegistrationResult, load_registered_project
    from .scan_policy import (
        FilePolicyDecision,
        PathDecision,
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from file_classification import (  # type: ignore[no-redef]
        CLASSIFICATION_SAMPLE_BYTES,
        FILE_CLASSIFICATION_SCHEMA_VERSION,
        FileClassification,
        classification_from_dict,
        classify_file,
    )
    from file_state import (  # type: ignore[no-redef]
        FILE_STATE_SCHEMA_VERSION,
        FileState,
        file_state_from_dict,
        inventory_file_state,
        state_is_compatible_with_inventory,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        schema_version_of,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
    )
    from scan_policy import (  # type: ignore[no-redef]
        FilePolicyDecision,
        PathDecision,
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )


PROJECT_MANIFEST_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
PROJECT_MANIFEST_KIND = "llmwiki-project-manifest"
LEGACY_PROJECT_MANIFEST_VERSION = "project-inventory-v1"
FINGERPRINT_PROJECT_MANIFEST_VERSION = "project-inventory-v2"
CLASSIFICATION_PROJECT_MANIFEST_VERSION = "project-inventory-v3"
PROJECT_MANIFEST_VERSION = "project-inventory-v4"
CLASSIFICATION_SUMMARY_KIND = "llmwiki-file-classification-summary"
FILE_STATE_SUMMARY_KIND = "llmwiki-file-state-summary"
CONTENT_HASH_ALGORITHM = "sha256"
FINGERPRINT_CACHE_STRATEGY = "file-identity-change-v1"
_SUPPORTED_MANIFEST_VERSIONS = {
    LEGACY_PROJECT_MANIFEST_VERSION,
    FINGERPRINT_PROJECT_MANIFEST_VERSION,
    CLASSIFICATION_PROJECT_MANIFEST_VERSION,
    PROJECT_MANIFEST_VERSION,
}
_FINGERPRINT_MANIFEST_VERSIONS = {
    FINGERPRINT_PROJECT_MANIFEST_VERSION,
    CLASSIFICATION_PROJECT_MANIFEST_VERSION,
    PROJECT_MANIFEST_VERSION,
}
_CLASSIFICATION_MANIFEST_VERSIONS = {
    CLASSIFICATION_PROJECT_MANIFEST_VERSION,
    PROJECT_MANIFEST_VERSION,
}
_FILE_STATE_MANIFEST_VERSIONS = {PROJECT_MANIFEST_VERSION}

_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_FINGERPRINT_ATTEMPTS = 3
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    class _WindowsFileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_FILE_W = _KERNEL32.CreateFileW
    _CREATE_FILE_W.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CREATE_FILE_W.restype = wintypes.HANDLE
    _GET_FILE_INFORMATION_BY_HANDLE_EX = (
        _KERNEL32.GetFileInformationByHandleEx
    )
    _GET_FILE_INFORMATION_BY_HANDLE_EX.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _GET_FILE_INFORMATION_BY_HANDLE_EX.restype = wintypes.BOOL
    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = [wintypes.HANDLE]
    _CLOSE_HANDLE.restype = wintypes.BOOL
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_RECORD_TYPES = (
    "file",
    "excluded_file",
    "excluded_directory",
    "symlink",
    "special_entry",
    "skipped_directory",
)


class ProjectInventoryError(LayoutError):
    """Base error for an incomplete or unsafe project inventory."""


class ProjectInventoryTraversalError(ProjectInventoryError):
    """Raised when an in-scope directory cannot be inventoried completely."""


class ProjectManifestError(ProjectInventoryError):
    """Raised when an existing Manifest cannot be safely reused or upgraded."""


class _FileChangedDuringFingerprint(OSError):
    """Internal retry signal for a file that changed during fingerprinting."""


@dataclass(frozen=True)
class ProjectInventoryResult:
    """Location, generation, and accountable counts for one Manifest."""

    project_id: str
    project_root: Path
    manifest_file: Path
    scan_generation: int
    total_records: int
    record_counts: dict[str, int]
    directories_scanned: int
    exclusion_summary: dict[str, dict[str, int]]
    symlink_summary: dict[str, int]
    fingerprint_summary: dict[str, Any]
    classification_summary: dict[str, Any]
    file_state_summary: dict[str, Any]
    policy: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "manifest_file": str(self.manifest_file),
            "manifest_version": PROJECT_MANIFEST_VERSION,
            "scan_generation": self.scan_generation,
            "total_records": self.total_records,
            "record_counts": dict(self.record_counts),
            "directories_scanned": self.directories_scanned,
            "exclusion_summary": self.exclusion_summary,
            "symlink_summary": dict(self.symlink_summary),
            "fingerprint_summary": dict(self.fingerprint_summary),
            "classification_summary": dict(self.classification_summary),
            "file_state_summary": dict(self.file_state_summary),
            "policy": self.policy,
        }


@dataclass(frozen=True)
class _FingerprintCacheKey:
    identity: str
    change_token: str

    def as_dict(self) -> dict[str, str]:
        return {
            "strategy": FINGERPRINT_CACHE_STRATEGY,
            "identity": self.identity,
            "change_token": self.change_token,
        }


@dataclass(frozen=True)
class _FileSnapshot:
    size_bytes: int
    mtime_ns: int
    identity: str | None
    change_token: str | None

    @property
    def cache_key(self) -> _FingerprintCacheKey | None:
        if self.identity is None or self.change_token is None:
            return None
        return _FingerprintCacheKey(
            identity=self.identity,
            change_token=self.change_token,
        )


@dataclass(frozen=True)
class _PriorFingerprint:
    content_sha256: str
    size_bytes: int
    mtime_ns: int
    cache_key: _FingerprintCacheKey | None
    classification: FileClassification | None
    file_state: FileState | None


@dataclass(frozen=True)
class ProjectManifest:
    """One strictly validated persisted Manifest and all of its JSONL rows."""

    manifest_file: Path
    project_id: str
    project_root: Path
    manifest_version: str
    scan_generation: int
    summary: dict[str, Any]
    records: tuple[dict[str, Any], ...]

    @property
    def file_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            record for record in self.records if record["record_type"] == "file"
        )


@dataclass(frozen=True)
class _PreviousManifest:
    scan_generation: int
    fingerprints: dict[str, _PriorFingerprint]
    document: ProjectManifest | None = None


@dataclass(frozen=True)
class _FileFingerprint:
    content_sha256: str
    snapshot: _FileSnapshot
    reused: bool

    def record_fields(self) -> dict[str, Any]:
        cache_key = self.snapshot.cache_key
        return {
            "content_sha256": self.content_sha256,
            "size_bytes": self.snapshot.size_bytes,
            "mtime_ns": self.snapshot.mtime_ns,
            "fingerprint_cache": (
                cache_key.as_dict() if cache_key is not None else None
            ),
        }


@dataclass(frozen=True)
class _InspectedFile:
    fingerprint: _FileFingerprint
    classification: FileClassification
    classification_reused: bool
    file_state: FileState
    file_state_reused: bool


@dataclass(frozen=True)
class _BoundaryEvaluation:
    logical: PathDecision
    resolved: PathDecision | None
    included: bool
    traverse: bool
    reason_code: str
    reason: str
    reason_source: str


@dataclass(frozen=True)
class _DirectoryTask:
    logical_path: str
    physical_path: Path
    ancestor_realpaths: tuple[Path, ...]


@dataclass(frozen=True)
class _SymlinkTask:
    logical_path: str
    physical_path: Path
    ancestor_realpaths: tuple[Path, ...]


@dataclass
class _InventoryState:
    project_id: str
    scan_generation: int
    previous_fingerprints: dict[str, _PriorFingerprint]
    writer: TextIO
    record_counts: Counter[str] = field(default_factory=Counter)
    excluded_files: Counter[str] = field(default_factory=Counter)
    excluded_directories: Counter[str] = field(default_factory=Counter)
    symlink_reasons: Counter[str] = field(default_factory=Counter)
    directories_scanned: int = 0
    fingerprinted_files: int = 0
    hashed_files: int = 0
    reused_files: int = 0
    classified_files: int = 0
    reused_classifications: int = 0
    formats: Counter[str] = field(default_factory=Counter)
    languages: Counter[str] = field(default_factory=Counter)
    research_roles: Counter[str] = field(default_factory=Counter)
    state_files: int = 0
    reused_file_states: int = 0
    processing_statuses: Counter[str] = field(default_factory=Counter)
    read_depths: Counter[str] = field(default_factory=Counter)
    state_reasons: Counter[str] = field(default_factory=Counter)

    def write(self, record: dict[str, Any]) -> None:
        self.writer.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        self.writer.write("\n")
        self.record_counts[record["record_type"]] += 1

    def record_excluded_file(self, reason_code: str) -> None:
        self.excluded_files[reason_code] += 1

    def record_excluded_directory(self, reason_code: str) -> None:
        self.excluded_directories[reason_code] += 1

    def record_symlink_reason(self, reason_code: str) -> None:
        self.symlink_reasons[reason_code] += 1

    def record_fingerprint(self, fingerprint: _FileFingerprint) -> None:
        self.fingerprinted_files += 1
        if fingerprint.reused:
            self.reused_files += 1
        else:
            self.hashed_files += 1

    def record_classification(
        self,
        classification: FileClassification,
        *,
        reused: bool,
    ) -> None:
        self.classified_files += 1
        self.formats[classification.format] += 1
        self.languages[classification.language] += 1
        self.research_roles[classification.research_role] += 1
        if reused:
            self.reused_classifications += 1

    def record_file_state(self, file_state: FileState, *, reused: bool) -> None:
        self.state_files += 1
        self.processing_statuses[file_state.processing_status] += 1
        self.read_depths[file_state.read_depth] += 1
        self.state_reasons[file_state.reason_code] += 1
        if reused:
            self.reused_file_states += 1


@dataclass
class _TraversalContext:
    project_root: Path
    policy: ScanPolicy
    state: _InventoryState
    directory_queue: deque[_DirectoryTask]
    symlink_queue: deque[_SymlinkTask]
    visited_directories: dict[str, Path]
    followed_symlink_targets: dict[str, Path]

    def policy_visited_realpaths(self) -> tuple[Path, ...]:
        combined = dict(self.visited_directories)
        combined.update(self.followed_symlink_targets)
        return tuple(combined[key] for key in sorted(combined))


def _record_base(project_id: str, record_type: str, path: str) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
        "kind": PROJECT_MANIFEST_KIND,
        "manifest_version": PROJECT_MANIFEST_VERSION,
        "record_type": record_type,
        "project_id": project_id,
        "path": path,
    }


def _summary_base(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
        "kind": PROJECT_MANIFEST_KIND,
        "manifest_version": PROJECT_MANIFEST_VERSION,
        "record_type": "summary",
        "project_id": project_id,
    }


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _manifest_error(
    manifest_file: Path,
    message: str,
    *,
    line_number: int | None = None,
) -> ProjectManifestError:
    location = (
        f"{manifest_file}:{line_number}"
        if line_number is not None
        else str(manifest_file)
    )
    return ProjectManifestError(f"invalid project Manifest at {location}: {message}")


def _validate_manifest_record_base(
    manifest_file: Path,
    record: object,
    *,
    line_number: int,
    project_id: str,
    expected_manifest_version: str | None,
) -> tuple[dict[str, Any], str]:
    if not isinstance(record, dict):
        raise _manifest_error(
            manifest_file,
            "each JSONL line must contain an object",
            line_number=line_number,
        )
    try:
        schema_version_of(
            record,
            allow_legacy=False,
            max_supported=PROJECT_MANIFEST_SCHEMA_VERSION,
        )
    except LayoutError as exc:
        raise _manifest_error(
            manifest_file,
            str(exc),
            line_number=line_number,
        ) from exc
    if record.get("kind") != PROJECT_MANIFEST_KIND:
        raise _manifest_error(
            manifest_file,
            f"unexpected kind {record.get('kind')!r}",
            line_number=line_number,
        )
    if record.get("project_id") != project_id:
        raise _manifest_error(
            manifest_file,
            "project_id does not match the registered project",
            line_number=line_number,
        )
    manifest_version = record.get("manifest_version")
    if manifest_version not in _SUPPORTED_MANIFEST_VERSIONS:
        raise _manifest_error(
            manifest_file,
            f"unsupported manifest_version {manifest_version!r}",
            line_number=line_number,
        )
    if (
        expected_manifest_version is not None
        and manifest_version != expected_manifest_version
    ):
        raise _manifest_error(
            manifest_file,
            "all rows must use the summary manifest_version",
            line_number=line_number,
        )
    return record, manifest_version


def _parse_cache_key(
    manifest_file: Path,
    value: object,
    *,
    line_number: int,
) -> _FingerprintCacheKey | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _manifest_error(
            manifest_file,
            "fingerprint_cache must be an object or null",
            line_number=line_number,
        )
    if value.get("strategy") != FINGERPRINT_CACHE_STRATEGY:
        raise _manifest_error(
            manifest_file,
            "fingerprint_cache uses an unsupported strategy",
            line_number=line_number,
        )
    identity = value.get("identity")
    change_token = value.get("change_token")
    if not isinstance(identity, str) or not identity:
        raise _manifest_error(
            manifest_file,
            "fingerprint_cache.identity must be a non-empty string",
            line_number=line_number,
        )
    if not isinstance(change_token, str) or not change_token:
        raise _manifest_error(
            manifest_file,
            "fingerprint_cache.change_token must be a non-empty string",
            line_number=line_number,
        )
    return _FingerprintCacheKey(identity=identity, change_token=change_token)


def _parse_prior_fingerprint(
    manifest_file: Path,
    record: dict[str, Any],
    *,
    line_number: int,
    manifest_version: str,
) -> _PriorFingerprint:
    content_sha256 = record.get("content_sha256")
    if not isinstance(content_sha256, str) or not _SHA256_PATTERN.fullmatch(
        content_sha256
    ):
        raise _manifest_error(
            manifest_file,
            "file content_sha256 must be 64 lowercase hexadecimal characters",
            line_number=line_number,
        )
    size_bytes = record.get("size_bytes")
    if not _is_integer(size_bytes) or size_bytes < 0:
        raise _manifest_error(
            manifest_file,
            "file size_bytes must be a non-negative integer",
            line_number=line_number,
        )
    mtime_ns = record.get("mtime_ns")
    if not _is_integer(mtime_ns):
        raise _manifest_error(
            manifest_file,
            "file mtime_ns must be an integer",
            line_number=line_number,
        )
    classification: FileClassification | None = None
    if manifest_version in _CLASSIFICATION_MANIFEST_VERSIONS:
        try:
            classification = classification_from_dict(record.get("classification"))
        except ValueError as exc:
            raise _manifest_error(
                manifest_file,
                str(exc),
                line_number=line_number,
            ) from exc
    file_state: FileState | None = None
    if manifest_version in _FILE_STATE_MANIFEST_VERSIONS:
        try:
            file_state = file_state_from_dict(record.get("file_state"))
        except ValueError as exc:
            raise _manifest_error(
                manifest_file,
                str(exc),
                line_number=line_number,
            ) from exc
    return _PriorFingerprint(
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        cache_key=_parse_cache_key(
            manifest_file,
            record.get("fingerprint_cache"),
            line_number=line_number,
        ),
        classification=classification,
        file_state=file_state,
    )


def _validate_classification_summary(
    manifest_file: Path,
    summary: dict[str, Any],
    *,
    actual_classifications: dict[str, Counter[str]],
    file_count: int,
) -> None:
    classification_summary = summary.get("classification_summary")
    if not isinstance(classification_summary, dict):
        raise _manifest_error(
            manifest_file,
            "classification_summary must be an object",
            line_number=1,
        )
    if (
        classification_summary.get("schema_version")
        != FILE_CLASSIFICATION_SCHEMA_VERSION
    ):
        raise _manifest_error(
            manifest_file,
            "classification_summary schema_version is unsupported",
            line_number=1,
        )
    if classification_summary.get("kind") != CLASSIFICATION_SUMMARY_KIND:
        raise _manifest_error(
            manifest_file,
            "classification_summary kind is unsupported",
            line_number=1,
        )
    classified_files = classification_summary.get("classified_files")
    reused_files = classification_summary.get("reused_files")
    if (
        not _is_integer(classified_files)
        or classified_files != file_count
        or not _is_integer(reused_files)
        or reused_files < 0
        or reused_files > classified_files
    ):
        raise _manifest_error(
            manifest_file,
            "classification_summary file counts do not reconcile",
            line_number=1,
        )
    for field_name in ("formats", "languages", "research_roles"):
        stored = classification_summary.get(field_name)
        actual = dict(sorted(actual_classifications[field_name].items()))
        if stored != actual or sum(actual.values()) != file_count:
            raise _manifest_error(
                manifest_file,
                f"classification_summary {field_name} do not reconcile",
                line_number=1,
            )


def _validate_file_state_summary(
    manifest_file: Path,
    summary: dict[str, Any],
    *,
    actual_file_states: dict[str, Counter[str]],
    file_count: int,
) -> None:
    file_state_summary = summary.get("file_state_summary")
    if not isinstance(file_state_summary, dict):
        raise _manifest_error(
            manifest_file,
            "file_state_summary must be an object",
            line_number=1,
        )
    if file_state_summary.get("schema_version") != FILE_STATE_SCHEMA_VERSION:
        raise _manifest_error(
            manifest_file,
            "file_state_summary schema_version is unsupported",
            line_number=1,
        )
    if file_state_summary.get("kind") != FILE_STATE_SUMMARY_KIND:
        raise _manifest_error(
            manifest_file,
            "file_state_summary kind is unsupported",
            line_number=1,
        )
    state_files = file_state_summary.get("state_files")
    reused_files = file_state_summary.get("reused_files")
    if (
        not _is_integer(state_files)
        or state_files != file_count
        or not _is_integer(reused_files)
        or reused_files < 0
        or reused_files > state_files
    ):
        raise _manifest_error(
            manifest_file,
            "file_state_summary file counts do not reconcile",
            line_number=1,
        )
    for field_name in ("processing_statuses", "read_depths", "reasons"):
        stored = file_state_summary.get(field_name)
        actual = dict(sorted(actual_file_states[field_name].items()))
        if stored != actual or sum(actual.values()) != file_count:
            raise _manifest_error(
                manifest_file,
                f"file_state_summary {field_name} do not reconcile",
                line_number=1,
            )


def _validate_manifest_summary(
    manifest_file: Path,
    summary: dict[str, Any],
    *,
    project_root: Path,
    manifest_version: str,
    actual_counts: Counter[str],
    actual_classifications: dict[str, Counter[str]],
    actual_file_states: dict[str, Counter[str]],
    total_records: int,
) -> int:
    stored_root = summary.get("project_root")
    if not isinstance(stored_root, str) or (
        _canonical_path_key(stored_root) != _canonical_path_key(project_root)
    ):
        raise _manifest_error(
            manifest_file,
            "project_root does not match the registered project",
            line_number=1,
        )
    record_counts = summary.get("record_counts")
    if not isinstance(record_counts, dict) or set(record_counts) != set(
        _RECORD_TYPES
    ):
        raise _manifest_error(
            manifest_file,
            "record_counts must contain exactly the supported record types",
            line_number=1,
        )
    for record_type in _RECORD_TYPES:
        value = record_counts[record_type]
        if not _is_integer(value) or value < 0:
            raise _manifest_error(
                manifest_file,
                f"record_counts.{record_type} must be a non-negative integer",
                line_number=1,
            )
        if value != actual_counts[record_type]:
            raise _manifest_error(
                manifest_file,
                f"record_counts.{record_type} does not match the JSONL rows",
                line_number=1,
            )
    stored_total = summary.get("total_records")
    if (
        not _is_integer(stored_total)
        or stored_total < 0
        or stored_total != total_records
    ):
        raise _manifest_error(
            manifest_file,
            "total_records does not match the JSONL rows",
            line_number=1,
        )

    if manifest_version == LEGACY_PROJECT_MANIFEST_VERSION:
        return 0

    scan_generation = summary.get("scan_generation")
    if not _is_integer(scan_generation) or scan_generation < 1:
        raise _manifest_error(
            manifest_file,
            "scan_generation must be a positive integer",
            line_number=1,
        )
    fingerprint_summary = summary.get("fingerprint_summary")
    if not isinstance(fingerprint_summary, dict):
        raise _manifest_error(
            manifest_file,
            "fingerprint_summary must be an object",
            line_number=1,
        )
    if fingerprint_summary.get("algorithm") != CONTENT_HASH_ALGORITHM:
        raise _manifest_error(
            manifest_file,
            "fingerprint_summary algorithm is unsupported",
            line_number=1,
        )
    if (
        fingerprint_summary.get("cache_strategy")
        != FINGERPRINT_CACHE_STRATEGY
    ):
        raise _manifest_error(
            manifest_file,
            "fingerprint_summary cache_strategy is unsupported",
            line_number=1,
        )
    hashed_files = fingerprint_summary.get("hashed_files")
    reused_files = fingerprint_summary.get("reused_files")
    if (
        not _is_integer(hashed_files)
        or hashed_files < 0
        or not _is_integer(reused_files)
        or reused_files < 0
        or hashed_files + reused_files != actual_counts["file"]
    ):
        raise _manifest_error(
            manifest_file,
            "fingerprint_summary counts do not reconcile with file records",
            line_number=1,
        )
    if manifest_version in _CLASSIFICATION_MANIFEST_VERSIONS:
        _validate_classification_summary(
            manifest_file,
            summary,
            actual_classifications=actual_classifications,
            file_count=actual_counts["file"],
        )
    if manifest_version in _FILE_STATE_MANIFEST_VERSIONS:
        _validate_file_state_summary(
            manifest_file,
            summary,
            actual_file_states=actual_file_states,
            file_count=actual_counts["file"],
        )
    return scan_generation


def _load_previous_manifest(
    manifest_file: Path,
    *,
    project_id: str,
    project_root: Path,
) -> _PreviousManifest:
    if not manifest_file.exists():
        return _PreviousManifest(scan_generation=0, fingerprints={})
    if not manifest_file.is_file():
        raise ProjectManifestError(
            f"registered project Manifest is not a file: {manifest_file}"
        )

    summary: dict[str, Any] | None = None
    manifest_version: str | None = None
    records: list[dict[str, Any]] = []
    actual_counts: Counter[str] = Counter()
    actual_classifications = {
        "formats": Counter(),
        "languages": Counter(),
        "research_roles": Counter(),
    }
    actual_file_states = {
        "processing_statuses": Counter(),
        "read_depths": Counter(),
        "reasons": Counter(),
    }
    fingerprints: dict[str, _PriorFingerprint] = {}
    seen_paths: set[str] = set()
    total_records = 0
    try:
        with manifest_file.open("r", encoding="utf-8", newline="") as source:
            for line_number, raw_line in enumerate(source, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    raise _manifest_error(
                        manifest_file,
                        "blank JSONL lines are not allowed",
                        line_number=line_number,
                    )
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise _manifest_error(
                        manifest_file,
                        f"invalid JSON: {exc.msg}",
                        line_number=line_number,
                    ) from exc
                record, row_version = _validate_manifest_record_base(
                    manifest_file,
                    decoded,
                    line_number=line_number,
                    project_id=project_id,
                    expected_manifest_version=manifest_version,
                )
                record_type = record.get("record_type")
                if line_number == 1:
                    if record_type != "summary":
                        raise _manifest_error(
                            manifest_file,
                            "the first row must be the summary",
                            line_number=1,
                        )
                    summary = record
                    manifest_version = row_version
                    continue
                if record_type not in _RECORD_TYPES:
                    raise _manifest_error(
                        manifest_file,
                        f"unsupported record_type {record_type!r}",
                        line_number=line_number,
                    )
                path_value = record.get("path")
                if not isinstance(path_value, str) or not path_value:
                    raise _manifest_error(
                        manifest_file,
                        "non-summary rows require a non-empty path",
                        line_number=line_number,
                    )
                if path_value in seen_paths:
                    raise _manifest_error(
                        manifest_file,
                        f"duplicate path {path_value!r}",
                        line_number=line_number,
                    )
                seen_paths.add(path_value)
                actual_counts[record_type] += 1
                total_records += 1
                records.append(record)
                if (
                    manifest_version in _FINGERPRINT_MANIFEST_VERSIONS
                    and record_type == "file"
                ):
                    fingerprint = _parse_prior_fingerprint(
                        manifest_file,
                        record,
                        line_number=line_number,
                        manifest_version=manifest_version,
                    )
                    fingerprints[path_value] = fingerprint
                    if fingerprint.classification is not None:
                        actual_classifications["formats"][
                            fingerprint.classification.format
                        ] += 1
                        actual_classifications["languages"][
                            fingerprint.classification.language
                        ] += 1
                        actual_classifications["research_roles"][
                            fingerprint.classification.research_role
                        ] += 1
                    if fingerprint.file_state is not None:
                        actual_file_states["processing_statuses"][
                            fingerprint.file_state.processing_status
                        ] += 1
                        actual_file_states["read_depths"][
                            fingerprint.file_state.read_depth
                        ] += 1
                        actual_file_states["reasons"][
                            fingerprint.file_state.reason_code
                        ] += 1
    except (OSError, UnicodeError) as exc:
        raise ProjectManifestError(
            f"could not read existing project Manifest {manifest_file}: {exc}"
        ) from exc

    if summary is None or manifest_version is None:
        raise _manifest_error(manifest_file, "the Manifest is empty")
    scan_generation = _validate_manifest_summary(
        manifest_file,
        summary,
        project_root=project_root,
        manifest_version=manifest_version,
        actual_counts=actual_counts,
        actual_classifications=actual_classifications,
        actual_file_states=actual_file_states,
        total_records=total_records,
    )
    document = ProjectManifest(
        manifest_file=manifest_file,
        project_id=project_id,
        project_root=project_root,
        manifest_version=manifest_version,
        scan_generation=scan_generation,
        summary=summary,
        records=tuple(records),
    )
    return _PreviousManifest(
        scan_generation=scan_generation,
        fingerprints=(
            fingerprints
            if manifest_version in _FINGERPRINT_MANIFEST_VERSIONS
            else {}
        ),
        document=document,
    )


def load_project_manifest(
    manifest_file: str | Path,
    *,
    project_id: str,
    project_root: str | Path,
    required_manifest_version: str | None = None,
) -> ProjectManifest:
    """Load one existing Manifest through the inventory compatibility validator.

    The reader never traverses or opens source-project files.  Unsupported Schema
    or artifact versions, corrupt rows, and summary mismatches fail closed.
    """

    path = Path(manifest_file)
    loaded = _load_previous_manifest(
        path,
        project_id=project_id,
        project_root=Path(project_root),
    )
    if loaded.document is None:
        raise ProjectManifestError(f"project Manifest does not exist: {path}")
    if (
        required_manifest_version is not None
        and loaded.document.manifest_version != required_manifest_version
    ):
        raise _manifest_error(
            path,
            "operation requires manifest_version "
            f"{required_manifest_version!r}, found "
            f"{loaded.document.manifest_version!r}",
            line_number=1,
        )
    return loaded.document


def _windows_change_time_token(path: Path) -> str | None:
    if os.name != "nt":
        return None
    file_read_attributes = 0x0080
    file_share_all = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    handle = _CREATE_FILE_W(
        str(path),
        file_read_attributes,
        file_share_all,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        return None
    try:
        information = _WindowsFileBasicInfo()
        succeeded = _GET_FILE_INFORMATION_BY_HANDLE_EX(
            handle,
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not succeeded:
            return None
        return f"windows-change-time-100ns:{information.ChangeTime}"
    finally:
        _CLOSE_HANDLE(handle)


def _stat_identity(metadata: os.stat_result) -> str | None:
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if (
        not _is_integer(device)
        or not _is_integer(inode)
        or inode == 0
    ):
        return None
    return f"{device}:{inode}"


def _stat_change_token(path: Path, metadata: os.stat_result) -> str | None:
    if os.name == "nt":
        return _windows_change_time_token(path)
    ctime_ns = getattr(metadata, "st_ctime_ns", None)
    if not _is_integer(ctime_ns):
        return None
    return f"stat-ctime-ns:{ctime_ns}"


def _stat_signature(metadata: os.stat_result) -> tuple[object, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        _stat_identity(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _capture_file_snapshot(path: Path) -> _FileSnapshot:
    first = path.lstat()
    if not stat.S_ISREG(first.st_mode):
        raise _FileChangedDuringFingerprint(
            "entry is no longer a regular file"
        )
    first_change = _stat_change_token(path, first)
    second = path.lstat()
    if not stat.S_ISREG(second.st_mode):
        raise _FileChangedDuringFingerprint(
            "entry is no longer a regular file"
        )
    second_change = _stat_change_token(path, second)
    if _stat_signature(first) != _stat_signature(second):
        raise _FileChangedDuringFingerprint(
            "file metadata changed while capturing a fingerprint snapshot"
        )
    if first_change != second_change:
        raise _FileChangedDuringFingerprint(
            "filesystem change marker changed while capturing a snapshot"
        )
    return _FileSnapshot(
        size_bytes=second.st_size,
        mtime_ns=second.st_mtime_ns,
        identity=_stat_identity(second),
        change_token=second_change,
    )


def _metadata_matches_snapshot(
    metadata: os.stat_result,
    snapshot: _FileSnapshot,
) -> bool:
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if metadata.st_size != snapshot.size_bytes:
        return False
    if metadata.st_mtime_ns != snapshot.mtime_ns:
        return False
    identity = _stat_identity(metadata)
    return snapshot.identity is None or identity == snapshot.identity


def _snapshots_match(first: _FileSnapshot, second: _FileSnapshot) -> bool:
    return (
        first.size_bytes == second.size_bytes
        and first.mtime_ns == second.mtime_ns
        and first.identity == second.identity
        and first.change_token == second.change_token
    )


def _open_regular_file(path: Path) -> int:
    flags = os.O_RDONLY
    for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def _hash_file_content(path: Path, snapshot: _FileSnapshot) -> str:
    descriptor = -1
    try:
        descriptor = _open_regular_file(path)
        opened_metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
        if not _metadata_matches_snapshot(opened_metadata, snapshot):
            raise _FileChangedDuringFingerprint(
                "opened file no longer matches the inventory snapshot"
            )
        if not _metadata_matches_snapshot(path_metadata, snapshot):
            raise _FileChangedDuringFingerprint(
                "file path changed before content hashing"
            )

        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)

        if not _metadata_matches_snapshot(os.fstat(descriptor), snapshot):
            raise _FileChangedDuringFingerprint(
                "file metadata changed during content hashing"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    after = _capture_file_snapshot(path)
    if not _snapshots_match(snapshot, after):
        raise _FileChangedDuringFingerprint(
            "file changed before its fingerprint could be committed"
        )
    return digest.hexdigest()


def _can_reuse_fingerprint(
    previous: _PriorFingerprint | None,
    snapshot: _FileSnapshot,
) -> bool:
    cache_key = snapshot.cache_key
    return bool(
        previous is not None
        and cache_key is not None
        and previous.cache_key == cache_key
        and previous.size_bytes == snapshot.size_bytes
        and previous.mtime_ns == snapshot.mtime_ns
    )


def _fingerprint_regular_file(
    path: Path,
    previous: _PriorFingerprint | None,
) -> _FileFingerprint:
    last_error: OSError | None = None
    for _attempt in range(_MAX_FINGERPRINT_ATTEMPTS):
        try:
            snapshot = _capture_file_snapshot(path)
            if _can_reuse_fingerprint(previous, snapshot):
                assert previous is not None
                return _FileFingerprint(
                    content_sha256=previous.content_sha256,
                    snapshot=snapshot,
                    reused=True,
                )
            content_sha256 = _hash_file_content(path, snapshot)
            return _FileFingerprint(
                content_sha256=content_sha256,
                snapshot=snapshot,
                reused=False,
            )
        except OSError as exc:
            last_error = exc

    detail = f": {last_error}" if last_error is not None else ""
    raise ProjectInventoryTraversalError(
        f"could not fingerprint in-scope regular file {path} after "
        f"{_MAX_FINGERPRINT_ATTEMPTS} attempts{detail}"
    ) from last_error


def _read_classification_sample(
    path: Path,
    snapshot: _FileSnapshot,
) -> bytes:
    descriptor = -1
    try:
        descriptor = _open_regular_file(path)
        if not _metadata_matches_snapshot(os.fstat(descriptor), snapshot):
            raise _FileChangedDuringFingerprint(
                "opened file no longer matches the classification snapshot"
            )
        if not _metadata_matches_snapshot(path.lstat(), snapshot):
            raise _FileChangedDuringFingerprint(
                "file path changed before deterministic classification"
            )
        chunks: list[bytes] = []
        remaining = CLASSIFICATION_SAMPLE_BYTES
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if not _metadata_matches_snapshot(os.fstat(descriptor), snapshot):
            raise _FileChangedDuringFingerprint(
                "file metadata changed during deterministic classification"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    after = _capture_file_snapshot(path)
    if not _snapshots_match(snapshot, after):
        raise _FileChangedDuringFingerprint(
            "file changed before its classification could be committed"
        )
    return b"".join(chunks)


def _classification_content_restriction(
    policy: ScanPolicy,
    logical_path: str,
    physical_relative_path: str,
    *,
    size_bytes: int,
) -> tuple[str, str] | None:
    decisions: list[tuple[str, FilePolicyDecision]] = [
        (
            "logical",
            policy.decide_file(logical_path, size_bytes=size_bytes),
        )
    ]
    if physical_relative_path != decisions[0][1].path:
        decisions.append(
            (
                "resolved",
                policy.decide_file(
                    physical_relative_path,
                    size_bytes=size_bytes,
                ),
            )
        )

    restricted = [
        (source, decision)
        for source, decision in decisions
        if decision.local_content_access != "allowed"
    ]
    if not restricted:
        return None

    access_priority = {"blocked": 0, "metadata_only": 1}
    reason_priority = {
        "sensitive-path": 0,
        "outside-scan-boundary": 1,
        "content-size-limit": 2,
    }
    _source, primary = min(
        restricted,
        key=lambda item: (
            access_priority[item[1].local_content_access],
            reason_priority.get(item[1].local_reason_code, 99),
            item[0],
        ),
    )
    explanations = "; ".join(
        f"{source} path {decision.path!r} -> "
        f"{decision.local_reason_code}: {decision.local_reason}"
        for source, decision in restricted
    )
    return primary.local_reason_code, explanations


def _classification_is_policy_path_only(
    classification: FileClassification,
) -> bool:
    return classification.reasons["format"]["source"] == "policy-path"


def _finish_inspected_file(
    *,
    fingerprint: _FileFingerprint,
    classification: FileClassification,
    classification_reused: bool,
    restriction: tuple[str, str] | None,
    previous: _PriorFingerprint | None,
) -> _InspectedFile:
    reason_code, reason = restriction if restriction is not None else (None, None)
    current_state = inventory_file_state(
        classification.format,
        content_access_reason_code=reason_code,
        content_access_reason=reason,
    )
    state_reused = (
        previous is not None
        and previous.file_state is not None
        and previous.content_sha256 == fingerprint.content_sha256
        and state_is_compatible_with_inventory(previous.file_state, current_state)
    )
    return _InspectedFile(
        fingerprint=fingerprint,
        classification=classification,
        classification_reused=classification_reused,
        file_state=previous.file_state if state_reused else current_state,
        file_state_reused=state_reused,
    )


def _inspect_regular_file(
    path: Path,
    logical_path: str,
    physical_relative_path: str,
    policy: ScanPolicy,
    previous: _PriorFingerprint | None,
) -> _InspectedFile:
    last_error: OSError | None = None
    for attempt in range(_MAX_FINGERPRINT_ATTEMPTS):
        fingerprint = _fingerprint_regular_file(
            path,
            previous if attempt == 0 else None,
        )
        restriction = _classification_content_restriction(
            policy,
            logical_path,
            physical_relative_path,
            size_bytes=fingerprint.snapshot.size_bytes,
        )
        if restriction is not None:
            reason_code, reason = restriction
            classification = classify_file(
                logical_path,
                None,
                content_access_reason_code=reason_code,
                content_access_reason=reason,
            )
            reused = (
                previous is not None
                and previous.classification == classification
                and previous.content_sha256 == fingerprint.content_sha256
            )
            return _finish_inspected_file(
                fingerprint=fingerprint,
                classification=classification,
                classification_reused=reused,
                restriction=restriction,
                previous=previous,
            )

        if (
            previous is not None
            and previous.classification is not None
            and previous.content_sha256 == fingerprint.content_sha256
            and not _classification_is_policy_path_only(previous.classification)
        ):
            return _finish_inspected_file(
                fingerprint=fingerprint,
                classification=previous.classification,
                classification_reused=True,
                restriction=None,
                previous=previous,
            )
        try:
            sample = _read_classification_sample(path, fingerprint.snapshot)
        except OSError as exc:
            last_error = exc
            continue
        return _finish_inspected_file(
            fingerprint=fingerprint,
            classification=classify_file(logical_path, sample),
            classification_reused=False,
            restriction=None,
            previous=previous,
        )

    detail = f": {last_error}" if last_error is not None else ""
    raise ProjectInventoryTraversalError(
        f"could not classify in-scope regular file {path} after "
        f"{_MAX_FINGERPRINT_ATTEMPTS} attempts{detail}"
    ) from last_error


def _canonical_path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    return unicodedata.normalize("NFC", normalized)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _join_relative(parent: str, name: str) -> str:
    normalized_name = unicodedata.normalize("NFC", name)
    return f"{parent}/{normalized_name}" if parent else normalized_name


def _relative_to_root(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise ProjectInventoryTraversalError(
            f"inventory path escaped the registered project root: {path}"
        ) from exc
    value = relative.as_posix()
    if not value or value == ".":
        raise ProjectInventoryTraversalError(
            f"inventory entry unexpectedly resolved to the project root: {path}"
        )
    return unicodedata.normalize("NFC", value)


def _evaluate_boundary(
    policy: ScanPolicy,
    logical_path: str,
    physical_relative_path: str,
    *,
    is_directory: bool,
) -> _BoundaryEvaluation:
    logical = policy.decide_path(logical_path, is_directory=is_directory)
    resolved = None
    if physical_relative_path != logical.path:
        resolved = policy.decide_path(
            physical_relative_path,
            is_directory=is_directory,
        )

    included = logical.included and (resolved is None or resolved.included)
    traverse = logical.traverse and (resolved is None or resolved.traverse)
    if is_directory:
        if not logical.traverse:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.traverse:
            selected = resolved
            source = "resolved"
        elif not logical.included:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.included:
            selected = resolved
            source = "resolved"
        else:
            selected = logical
            source = "logical"
    else:
        if not logical.included:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.included:
            selected = resolved
            source = "resolved"
        else:
            selected = logical
            source = "logical"

    return _BoundaryEvaluation(
        logical=logical,
        resolved=resolved,
        included=included,
        traverse=traverse,
        reason_code=selected.reason_code,
        reason=selected.reason,
        reason_source=source,
    )


def _boundary_fields(evaluation: _BoundaryEvaluation) -> dict[str, Any]:
    fields: dict[str, Any] = {"boundary": evaluation.logical.as_dict()}
    if evaluation.resolved is not None:
        fields["resolved_path"] = evaluation.resolved.path
        fields["resolved_boundary"] = evaluation.resolved.as_dict()
    return fields


def _effective_reason_fields(evaluation: _BoundaryEvaluation) -> dict[str, str]:
    return {
        "effective_reason_code": evaluation.reason_code,
        "effective_reason": evaluation.reason,
        "effective_reason_source": evaluation.reason_source,
    }


def _sorted_directory_names(path: Path, *, case_sensitive: bool) -> list[str]:
    try:
        with os.scandir(path) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        raise ProjectInventoryTraversalError(
            f"could not enumerate in-scope directory {path}: {exc}"
        ) from exc

    def key(name: str) -> tuple[str, str]:
        normalized = unicodedata.normalize("NFC", name)
        primary = normalized if case_sensitive else normalized.casefold()
        return primary, normalized

    return sorted(names, key=key)


def _is_non_symlink_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _write_unreadable_entry(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_relative_path: str,
    error: OSError,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=False,
    )
    record = _record_base(
        context.state.project_id,
        "special_entry",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "entry_kind": "metadata-unreadable",
            "reason_code": "entry-metadata-unreadable",
            "reason": f"entry metadata could not be read: {error}",
        }
    )
    context.state.write(record)


def _write_special_entry(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_relative_path: str,
    entry_kind: str,
    is_directory: bool,
    reason_code: str,
    reason: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=is_directory,
    )
    record = _record_base(
        context.state.project_id,
        "special_entry",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "entry_kind": entry_kind,
            "reason_code": reason_code,
            "reason": reason,
        }
    )
    context.state.write(record)


def _queue_directory(
    context: _TraversalContext,
    *,
    task: _DirectoryTask,
    logical_path: str,
    physical_path: Path,
    physical_relative_path: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=True,
    )
    if not evaluation.traverse:
        record = _record_base(
            context.state.project_id,
            "excluded_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(_effective_reason_fields(evaluation))
        record["pruned"] = True
        context.state.write(record)
        context.state.record_excluded_directory(evaluation.reason_code)
        return

    try:
        real_path = physical_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectInventoryTraversalError(
            f"could not resolve in-scope directory {logical_path}: {exc}"
        ) from exc
    if not _is_within(real_path, context.project_root):
        record = _record_base(
            context.state.project_id,
            "excluded_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(
            {
                "effective_reason_code": "directory-target-outside-project",
                "effective_reason": (
                    "directory resolved outside the registered project root"
                ),
                "effective_reason_source": "inventory-safety",
                "pruned": True,
                "resolved_target": str(real_path),
            }
        )
        context.state.write(record)
        context.state.record_excluded_directory(
            "directory-target-outside-project"
        )
        return

    real_key = _canonical_path_key(real_path)
    if real_key in context.visited_directories:
        record = _record_base(
            context.state.project_id,
            "skipped_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(
            {
                "reason_code": "directory-target-already-visited",
                "reason": "directory real path was already queued or scanned",
                "resolved_target": str(real_path),
            }
        )
        context.state.write(record)
        return

    context.visited_directories[real_key] = real_path
    context.directory_queue.append(
        _DirectoryTask(
            logical_path=evaluation.logical.path,
            physical_path=real_path,
            ancestor_realpaths=task.ancestor_realpaths + (real_path,),
        )
    )


def _write_file(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_path: Path,
    physical_relative_path: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=False,
    )
    record_type = "file" if evaluation.included else "excluded_file"
    record = _record_base(
        context.state.project_id,
        record_type,
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    if evaluation.included:
        inspected = _inspect_regular_file(
            physical_path,
            evaluation.logical.path,
            physical_relative_path,
            context.policy,
            context.state.previous_fingerprints.get(evaluation.logical.path),
        )
        record.update(inspected.fingerprint.record_fields())
        record["classification"] = inspected.classification.as_dict()
        record["file_state"] = inspected.file_state.as_dict()
        context.state.record_fingerprint(inspected.fingerprint)
        context.state.record_classification(
            inspected.classification,
            reused=inspected.classification_reused,
        )
        context.state.record_file_state(
            inspected.file_state,
            reused=inspected.file_state_reused,
        )
    else:
        record.update(_effective_reason_fields(evaluation))
        context.state.record_excluded_file(evaluation.reason_code)
    context.state.write(record)


def _scan_directory(context: _TraversalContext, task: _DirectoryTask) -> None:
    context.state.directories_scanned += 1
    names = _sorted_directory_names(
        task.physical_path,
        case_sensitive=context.policy.case_sensitive,
    )
    for name in names:
        logical_path = _join_relative(task.logical_path, name)
        physical_path = task.physical_path / name
        physical_relative_path = _relative_to_root(
            physical_path,
            context.project_root,
        )
        try:
            metadata = physical_path.lstat()
        except OSError as exc:
            _write_unreadable_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                error=exc,
            )
            continue

        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            context.symlink_queue.append(
                _SymlinkTask(
                    logical_path=logical_path,
                    physical_path=physical_path,
                    ancestor_realpaths=task.ancestor_realpaths,
                )
            )
        elif _is_non_symlink_reparse_point(metadata):
            _write_special_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                entry_kind="reparse-point",
                is_directory=stat.S_ISDIR(mode),
                reason_code="non-symlink-reparse-point-not-followed",
                reason=(
                    "non-symlink filesystem reparse points are recorded but not "
                    "traversed by the B-03 inventory"
                ),
            )
        elif stat.S_ISDIR(mode):
            _queue_directory(
                context,
                task=task,
                logical_path=logical_path,
                physical_path=physical_path,
                physical_relative_path=physical_relative_path,
            )
        elif stat.S_ISREG(mode):
            _write_file(
                context,
                logical_path=logical_path,
                physical_path=physical_path,
                physical_relative_path=physical_relative_path,
            )
        else:
            _write_special_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                entry_kind="special-filesystem-entry",
                is_directory=False,
                reason_code="non-regular-filesystem-entry",
                reason=(
                    "entry is neither a regular file, directory, nor symbolic link"
                ),
            )


def _target_metadata(
    target_path: str | None,
    project_root: Path,
) -> tuple[Path | None, str, bool]:
    if target_path is None:
        return None, "unknown", False
    target = Path(target_path)
    if not _is_within(target, project_root):
        return target, "outside-project", False
    try:
        metadata = target.stat()
    except OSError:
        return target, "unreadable", False
    if stat.S_ISDIR(metadata.st_mode):
        return target, "directory", True
    if stat.S_ISREG(metadata.st_mode):
        return target, "file", False
    return target, "special", False


def _symlink_boundary(
    context: _TraversalContext,
    task: _SymlinkTask,
    *,
    target: Path | None,
    target_is_directory: bool,
) -> _BoundaryEvaluation:
    physical_relative_path = task.logical_path
    if target is not None and _is_within(target, context.project_root):
        physical_relative_path = _relative_to_root(target, context.project_root)
    return _evaluate_boundary(
        context.policy,
        task.logical_path,
        physical_relative_path,
        is_directory=target_is_directory,
    )


def _process_symlink(context: _TraversalContext, task: _SymlinkTask) -> None:
    decision = context.policy.decide_symlink(
        task.logical_path,
        ancestor_realpaths=task.ancestor_realpaths,
        visited_realpaths=context.policy_visited_realpaths(),
    )
    target, target_kind, target_is_directory = _target_metadata(
        decision.target_path,
        context.project_root,
    )
    evaluation = _symlink_boundary(
        context,
        task,
        target=target,
        target_is_directory=target_is_directory,
    )

    followed = False
    final_reason_code = decision.reason_code
    final_reason = decision.reason

    if decision.follow:
        if target is None or target_kind == "unreadable":
            final_reason_code = "symlink-target-metadata-unreadable"
            final_reason = "approved symbolic-link target metadata became unreadable"
        elif target_kind == "directory":
            if not evaluation.logical.traverse:
                final_reason_code = "symlink-link-boundary-excluded"
                final_reason = evaluation.logical.reason
            elif evaluation.resolved is not None and not evaluation.resolved.traverse:
                final_reason_code = "symlink-target-boundary-excluded"
                final_reason = evaluation.resolved.reason
            else:
                target_key = _canonical_path_key(target)
                context.visited_directories[target_key] = target
                context.directory_queue.append(
                    _DirectoryTask(
                        logical_path=evaluation.logical.path,
                        physical_path=target,
                        ancestor_realpaths=task.ancestor_realpaths + (target,),
                    )
                )
                followed = True
        elif target_kind == "file":
            if not evaluation.logical.included:
                final_reason_code = "symlink-link-boundary-excluded"
                final_reason = evaluation.logical.reason
            elif evaluation.resolved is not None and not evaluation.resolved.included:
                final_reason_code = "symlink-target-boundary-excluded"
                final_reason = evaluation.resolved.reason
            else:
                context.followed_symlink_targets[_canonical_path_key(target)] = target
                followed = True
        else:
            final_reason_code = "symlink-target-special-entry"
            final_reason = (
                "symbolic-link target is not a regular file or directory; it was "
                "recorded but not traversed"
            )

    record = _record_base(
        context.state.project_id,
        "symlink",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "symlink": decision.as_dict(),
            "target_entry_kind": target_kind,
            "followed": followed,
            "follow_reason_code": final_reason_code,
            "follow_reason": final_reason,
        }
    )
    context.state.write(record)
    context.state.record_symlink_reason(final_reason_code)


def _walk_project(
    project_root: Path,
    policy: ScanPolicy,
    state: _InventoryState,
) -> None:
    root = project_root.resolve(strict=True)
    root_key = _canonical_path_key(root)
    context = _TraversalContext(
        project_root=root,
        policy=policy,
        state=state,
        directory_queue=deque(
            [
                _DirectoryTask(
                    logical_path="",
                    physical_path=root,
                    ancestor_realpaths=(root,),
                )
            ]
        ),
        symlink_queue=deque(),
        visited_directories={root_key: root},
        followed_symlink_targets={},
    )

    # Normal project paths are exhausted before queued symbolic links. This
    # gives canonical in-tree paths precedence over aliases and lets B-02's
    # duplicate-target guard prevent an alias from replacing a real path.
    while context.directory_queue or context.symlink_queue:
        while context.directory_queue:
            _scan_directory(context, context.directory_queue.popleft())
        if context.symlink_queue:
            _process_symlink(context, context.symlink_queue.popleft())


def _build_exclusion_summary(
    state: _InventoryState,
) -> dict[str, dict[str, int]]:
    reason_codes = sorted(
        set(state.excluded_files) | set(state.excluded_directories)
    )
    summary: dict[str, dict[str, int]] = {}
    for reason_code in reason_codes:
        excluded_files = state.excluded_files[reason_code]
        pruned_directories = state.excluded_directories[reason_code]
        summary[reason_code] = {
            "excluded_files": excluded_files,
            "pruned_directories": pruned_directories,
            "total_records": excluded_files + pruned_directories,
        }
    return summary


def _build_summary_record(
    *,
    project_id: str,
    project_root: Path,
    policy: ScanPolicy,
    state: _InventoryState,
) -> dict[str, Any]:
    record_counts = {
        record_type: state.record_counts[record_type]
        for record_type in _RECORD_TYPES
    }
    total_records = sum(record_counts.values())
    if state.fingerprinted_files != record_counts["file"]:
        raise ProjectInventoryTraversalError(
            "fingerprint count does not reconcile with in-scope file records"
        )
    if state.classified_files != record_counts["file"]:
        raise ProjectInventoryTraversalError(
            "classification count does not reconcile with in-scope file records"
        )
    if state.state_files != record_counts["file"]:
        raise ProjectInventoryTraversalError(
            "file-state count does not reconcile with in-scope file records"
        )
    summary = _summary_base(project_id)
    summary.update(
        {
            "project_root": str(project_root),
            "scan_generation": state.scan_generation,
            "total_records": total_records,
            "record_counts": record_counts,
            "directories_scanned": state.directories_scanned,
            "exclusion_summary": _build_exclusion_summary(state),
            "symlink_summary": {
                reason: state.symlink_reasons[reason]
                for reason in sorted(state.symlink_reasons)
            },
            "fingerprint_summary": {
                "algorithm": CONTENT_HASH_ALGORITHM,
                "cache_strategy": FINGERPRINT_CACHE_STRATEGY,
                "hashed_files": state.hashed_files,
                "reused_files": state.reused_files,
            },
            "classification_summary": {
                "schema_version": FILE_CLASSIFICATION_SCHEMA_VERSION,
                "kind": CLASSIFICATION_SUMMARY_KIND,
                "classified_files": state.classified_files,
                "reused_files": state.reused_classifications,
                "formats": dict(sorted(state.formats.items())),
                "languages": dict(sorted(state.languages.items())),
                "research_roles": dict(sorted(state.research_roles.items())),
            },
            "file_state_summary": {
                "schema_version": FILE_STATE_SCHEMA_VERSION,
                "kind": FILE_STATE_SUMMARY_KIND,
                "state_files": state.state_files,
                "reused_files": state.reused_file_states,
                "processing_statuses": dict(sorted(state.processing_statuses.items())),
                "read_depths": dict(sorted(state.read_depths.items())),
                "reasons": dict(sorted(state.state_reasons.items())),
            },
            "policy": policy.as_dict(),
        }
    )
    return summary


def _create_temporary_file(directory: Path, suffix: str) -> tuple[int, Path]:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".manifest.jsonl.",
        suffix=suffix,
        dir=directory,
    )
    return descriptor, Path(raw_path)


def _write_atomic_manifest(
    manifest_file: Path,
    summary: dict[str, Any],
    spool_file: Path,
) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary = _create_temporary_file(
            manifest_file.parent,
            ".tmp",
        )
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            summary_line = json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            target.write(summary_line.encode("utf-8"))
            target.write(b"\n")
            with spool_file.open("rb") as source:
                shutil.copyfileobj(source, target)
        os.replace(temporary, manifest_file)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _inventory_registered_project(
    registration: ProjectRegistrationResult,
    *,
    policy_config: ScanPolicyConfig | None = None,
) -> ProjectInventoryResult:
    """Inventory one registration while its shared machine-state lock is held."""

    manifest_file = registration.layout.manifest_file
    if not manifest_file.parent.is_dir():
        raise ProjectInventoryError(
            f"registered machine-state directory is unavailable: {manifest_file.parent}"
        )
    previous = _load_previous_manifest(
        manifest_file,
        project_id=registration.project_id,
        project_root=registration.project_root,
    )
    scan_generation = previous.scan_generation + 1
    policy = load_scan_policy(
        registration.project_root,
        config=policy_config,
    )

    descriptor = -1
    spool_file: Path | None = None
    try:
        descriptor, spool_file = _create_temporary_file(
            manifest_file.parent,
            ".records",
        )
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as writer:
            descriptor = -1
            state = _InventoryState(
                project_id=registration.project_id,
                scan_generation=scan_generation,
                previous_fingerprints=previous.fingerprints,
                writer=writer,
            )
            _walk_project(registration.project_root, policy, state)

        summary = _build_summary_record(
            project_id=registration.project_id,
            project_root=registration.project_root,
            policy=policy,
            state=state,
        )
        _write_atomic_manifest(manifest_file, summary, spool_file)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if spool_file is not None:
            spool_file.unlink(missing_ok=True)

    return ProjectInventoryResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        manifest_file=manifest_file,
        scan_generation=summary["scan_generation"],
        total_records=summary["total_records"],
        record_counts=summary["record_counts"],
        directories_scanned=summary["directories_scanned"],
        exclusion_summary=summary["exclusion_summary"],
        symlink_summary=summary["symlink_summary"],
        fingerprint_summary=summary["fingerprint_summary"],
        classification_summary=summary["classification_summary"],
        file_state_summary=summary["file_state_summary"],
        policy=summary["policy"],
    )

def inventory_project(
    workspace_root: str | Path,
    project_id: str,
    *,
    policy_config: ScanPolicyConfig | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProjectInventoryResult:
    """Fingerprint one registered project and atomically replace its Manifest.

    The stable per-project machine-state advisory lock covers the complete prior
    Manifest read, source scan, and atomic replacement. Source bytes remain local
    and source-project-read-only under the existing scan policy.
    """

    registration = load_registered_project(workspace_root, project_id)
    lock = AdvisoryFileLock(
        registration.layout.machine_state_lock_file,
        timeout_seconds=lock_timeout_seconds,
    )
    try:
        lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise ProjectInventoryError(
            "timed out waiting for the project machine-state lock"
        ) from exc
    except AdvisoryLockError as exc:
        raise ProjectInventoryError(
            "could not acquire the project machine-state lock"
        ) from exc
    try:
        return _inventory_registered_project(
            registration,
            policy_config=policy_config,
        )
    finally:
        lock.release()
