#!/usr/bin/env python3
"""Current-grounded C-05 selection of visual and PDF deep-read targets.

This module is intentionally an authorization/projection layer, not an
extractor. It derives a strict in-memory Schema v1 report exclusively from the
current-grounded B-07 reading-priority view, the exact current Manifest, and a
reconstructed B-02 scan policy. It never opens candidate source content,
persists state, or authorizes deferred recommendations.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Iterable

if __package__:
    from .file_classification import classification_from_dict
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        parse_json_bytes_strict,
        validate_project_id,
    )
    from .project_registry import (
        ProjectRegistrationResult,
        load_registered_project,
    )
    from .reading_priority import load_current_reading_priority
    from .scan_policy import ScanPolicy, ScanPolicyConfig, load_scan_policy
else:
    from file_classification import (  # type: ignore[no-redef]
        classification_from_dict,
    )
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        parse_json_bytes_strict,
        validate_project_id,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
    )
    from reading_priority import (  # type: ignore[no-redef]
        load_current_reading_priority,
    )
    from scan_policy import (  # type: ignore[no-redef]
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )


VISUAL_SELECTION_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
VISUAL_SELECTION_KIND = "llmwiki-visual-selection"
VISUAL_SELECTION_RECORD_KIND = "llmwiki-visual-selection-record"
VISUAL_SELECTION_VERSION = "visual-selection-v1"

VISUAL_CANDIDATE_FORMATS = ("png", "jpeg", "gif", "tiff", "bmp", "pdf")
VISUAL_SELECTION_CANDIDATE_FORMATS = VISUAL_CANDIDATE_FORMATS
VISUAL_SELECTION_DECISIONS = (
    "selected",
    "deferred",
    "limited",
    "not-candidate",
)
VISUAL_SELECTION_STATUSES = VISUAL_SELECTION_DECISIONS

_LOCAL_CONTENT_ACCESS = ("allowed", "metadata_only", "blocked")
_RAW_EXTERNAL_SEND = ("allowed", "blocked")
_RESEARCH_ROLES = frozenset(
    {
        "project_documentation",
        "documentation",
        "source_code",
        "test_code",
        "automation",
        "configuration",
        "dependency_manifest",
        "notebook",
        "paper",
        "bibliography",
        "dataset",
        "experiment",
        "result",
        "run_log",
        "figure",
        "model_artifact",
        "project_metadata",
        "unknown",
    }
)
_POLICY_CONFIG_FIELDS = frozenset(
    {
        "include_patterns",
        "exclude_patterns",
        "sensitive_patterns",
        "external_include_patterns",
        "external_exclude_patterns",
        "max_content_file_bytes",
        "max_raw_external_send_bytes",
        "follow_symlinks",
        "external_send_mode",
        "case_sensitive",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_SNAKE_CODE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class VisualSelectionError(LayoutError):
    """Base error for unsafe, stale, or malformed C-05 selection state."""


class VisualSelectionSchemaError(VisualSelectionError):
    """Raised when an in-memory or serialized selection report is malformed."""


class VisualSelectionCurrentnessError(VisualSelectionError):
    """Raised when Manifest, B-02 policy, or B-07 authorization is not current."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VisualSelectionSchemaError(f"{label} must be a lowercase SHA-256")
    return value


def _stable_code(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _STABLE_CODE_RE.fullmatch(value) is None:
        raise VisualSelectionSchemaError(
            f"{label} must be a stable kebab-case code"
        )
    return value


def _relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VisualSelectionSchemaError(
            f"{label} must be a non-empty project-relative path"
        )
    path = PurePosixPath(value)
    if (
        "\\" in value
        or _WINDOWS_DRIVE_RE.match(value) is not None
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise VisualSelectionSchemaError(
            f"{label} is not normalized project-relative POSIX form: {value!r}"
        )
    return value


def _string_tuple(
    value: object,
    *,
    label: str,
    paths: bool = False,
    reason_codes: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise VisualSelectionSchemaError(f"{label} must be an array of strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise VisualSelectionSchemaError(f"{label} must not contain duplicates")
    if paths:
        for item in result:
            _relative_path(item, label=label)
        if result != tuple(sorted(result)):
            raise VisualSelectionSchemaError(f"{label} must be sorted")
    if reason_codes:
        if not result:
            raise VisualSelectionSchemaError(f"{label} must not be empty")
        for item in result:
            _stable_code(item, label=label)
    return result


def _schema_object(
    value: object,
    *,
    expected_fields: set[str],
    kind: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisualSelectionSchemaError(f"{label} must be an object")
    if "schema_version" not in value:
        raise VisualSelectionSchemaError(
            f"{label} is legacy v0 and is not accepted as Schema v1"
        )
    version = value["schema_version"]
    if not _is_integer(version):
        raise VisualSelectionSchemaError(
            f"{label} schema_version must be an integer"
        )
    if version > VISUAL_SELECTION_SCHEMA_VERSION:
        raise VisualSelectionSchemaError(
            f"{label} uses unsupported future schema_version {version}"
        )
    if version != VISUAL_SELECTION_SCHEMA_VERSION:
        raise VisualSelectionSchemaError(
            f"{label} schema_version {version} is unsupported"
        )
    if set(value) != expected_fields:
        raise VisualSelectionSchemaError(
            f"{label} must contain exactly the Schema v1 fields"
        )
    if value["kind"] != kind:
        raise VisualSelectionSchemaError(f"{label} kind is invalid")
    return value

@dataclass(frozen=True)
class VisualSelectionManifestIdentity:
    """Exact B-07 identity of the current project-inventory-v4 bytes."""

    manifest_version: str
    scan_generation: int
    file_count: int
    byte_count: int
    content_sha256: str

    def __post_init__(self) -> None:
        if self.manifest_version != PROJECT_MANIFEST_VERSION:
            raise VisualSelectionSchemaError(
                "visual selection requires the current Manifest version"
            )
        if not _is_integer(self.scan_generation) or self.scan_generation < 1:
            raise VisualSelectionSchemaError(
                "Manifest scan_generation must be a positive integer"
            )
        for name in ("file_count", "byte_count"):
            value = getattr(self, name)
            if not _is_integer(value) or value < 0:
                raise VisualSelectionSchemaError(
                    f"Manifest {name} must be a non-negative integer"
                )
        _sha256(self.content_sha256, label="Manifest content_sha256")

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "scan_generation": self.scan_generation,
            "file_count": self.file_count,
            "byte_count": self.byte_count,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class VisualSelectionSummary:
    """Reconciled counts for the visual/PDF candidate projection."""

    candidate_count: int
    selected_count: int
    deferred_count: int
    limited_count: int
    not_candidate_count: int
    selected_bytes: int

    def __post_init__(self) -> None:
        for name in (
            "candidate_count",
            "selected_count",
            "deferred_count",
            "limited_count",
            "not_candidate_count",
            "selected_bytes",
        ):
            value = getattr(self, name)
            if not _is_integer(value) or value < 0:
                raise VisualSelectionSchemaError(
                    f"visual selection summary {name} must be non-negative"
                )

    def as_dict(self) -> dict[str, int]:
        return {
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "deferred_count": self.deferred_count,
            "limited_count": self.limited_count,
            "not_candidate_count": self.not_candidate_count,
            "selected_bytes": self.selected_bytes,
        }


@dataclass(frozen=True)
class VisualSelectionRecord:
    """One policy-grounded visual/PDF candidate and its B-07 decision."""

    path: str
    content_sha256: str
    size_bytes: int
    mtime_ns: int
    format: str
    research_role: str
    priority_rank: int
    deep_read_status: str
    decision: str
    local_content_access: str
    raw_external_send: str
    local_reason_code: str
    external_reason_code: str
    reason_codes: tuple[str, ...]
    referenced_by: tuple[str, ...]

    schema_version: ClassVar[int] = VISUAL_SELECTION_SCHEMA_VERSION
    kind: ClassVar[str] = VISUAL_SELECTION_RECORD_KIND

    def __post_init__(self) -> None:
        _relative_path(self.path, label="visual selection path")
        _sha256(self.content_sha256, label="visual selection content_sha256")
        if not _is_integer(self.size_bytes) or self.size_bytes < 0:
            raise VisualSelectionSchemaError(
                "visual selection size_bytes must be a non-negative integer"
            )
        if not _is_integer(self.mtime_ns) or self.mtime_ns < 0:
            raise VisualSelectionSchemaError(
                "visual selection mtime_ns must be a non-negative integer"
            )
        if self.format not in VISUAL_CANDIDATE_FORMATS:
            raise VisualSelectionSchemaError(
                f"unsupported visual candidate format {self.format!r}"
            )
        if (
            not isinstance(self.research_role, str)
            or _SNAKE_CODE_RE.fullmatch(self.research_role) is None
            or self.research_role not in _RESEARCH_ROLES
        ):
            raise VisualSelectionSchemaError(
                f"unsupported research_role {self.research_role!r}"
            )
        if not _is_integer(self.priority_rank) or self.priority_rank < 1:
            raise VisualSelectionSchemaError(
                "visual selection priority_rank must be a positive integer"
            )
        if self.deep_read_status not in VISUAL_SELECTION_DECISIONS:
            raise VisualSelectionSchemaError(
                f"unsupported deep_read_status {self.deep_read_status!r}"
            )
        if self.decision not in VISUAL_SELECTION_DECISIONS:
            raise VisualSelectionSchemaError(
                f"unsupported visual selection decision {self.decision!r}"
            )
        if self.decision != self.deep_read_status:
            raise VisualSelectionSchemaError(
                "visual selection decision must equal B-07 deep_read_status"
            )
        if self.local_content_access not in _LOCAL_CONTENT_ACCESS:
            raise VisualSelectionSchemaError(
                "visual selection local_content_access is invalid"
            )
        if self.raw_external_send not in _RAW_EXTERNAL_SEND:
            raise VisualSelectionSchemaError(
                "visual selection raw_external_send is invalid"
            )
        _stable_code(self.local_reason_code, label="local_reason_code")
        _stable_code(self.external_reason_code, label="external_reason_code")
        reasons = _string_tuple(
            self.reason_codes,
            label="visual selection reason_codes",
            reason_codes=True,
        )
        references = _string_tuple(
            self.referenced_by,
            label="visual selection referenced_by",
            paths=True,
        )
        if self.path in references:
            raise VisualSelectionSchemaError(
                "a visual selection record cannot reference itself"
            )
        if self.decision == "selected" and self.local_content_access != "allowed":
            raise VisualSelectionSchemaError(
                "selected visual content must have allowed local content access"
            )
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "referenced_by", references)

    @property
    def executable(self) -> bool:
        """Whether this record is an executable local C-05 target."""

        return self.decision == "selected"

    @property
    def execution_authorized(self) -> bool:
        return self.executable

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "format": self.format,
            "research_role": self.research_role,
            "priority_rank": self.priority_rank,
            "deep_read_status": self.deep_read_status,
            "decision": self.decision,
            "local_content_access": self.local_content_access,
            "raw_external_send": self.raw_external_send,
            "local_reason_code": self.local_reason_code,
            "external_reason_code": self.external_reason_code,
            "reason_codes": list(self.reason_codes),
            "referenced_by": list(self.referenced_by),
        }

@dataclass(frozen=True)
class VisualSelectionReport:
    """Strict Schema v1 current-grounded C-05 authorization report."""

    project_id: str
    manifest: VisualSelectionManifestIdentity
    priority_payload_sha256: str
    summary: VisualSelectionSummary
    records: tuple[VisualSelectionRecord, ...]

    schema_version: ClassVar[int] = VISUAL_SELECTION_SCHEMA_VERSION
    kind: ClassVar[str] = VISUAL_SELECTION_KIND
    version: ClassVar[str] = VISUAL_SELECTION_VERSION

    def __post_init__(self) -> None:
        try:
            validate_project_id(self.project_id)
        except (TypeError, ValueError, LayoutError) as exc:
            raise VisualSelectionSchemaError(
                "visual selection project_id is invalid"
            ) from exc
        if not isinstance(self.manifest, VisualSelectionManifestIdentity):
            raise VisualSelectionSchemaError(
                "visual selection manifest must be a Manifest identity"
            )
        _sha256(
            self.priority_payload_sha256,
            label="visual selection priority_payload_sha256",
        )
        if not isinstance(self.summary, VisualSelectionSummary):
            raise VisualSelectionSchemaError(
                "visual selection summary must be a VisualSelectionSummary"
            )
        if not isinstance(self.records, (list, tuple)) or not all(
            isinstance(record, VisualSelectionRecord) for record in self.records
        ):
            raise VisualSelectionSchemaError(
                "visual selection records must be VisualSelectionRecord objects"
            )
        records = tuple(self.records)
        paths = [record.path for record in records]
        ranks = [record.priority_rank for record in records]
        if len(paths) != len(set(paths)):
            raise VisualSelectionSchemaError(
                "visual selection records contain duplicate paths"
            )
        if len(ranks) != len(set(ranks)):
            raise VisualSelectionSchemaError(
                "visual selection records contain duplicate priority ranks"
            )
        if records != tuple(sorted(records, key=lambda record: record.priority_rank)):
            raise VisualSelectionSchemaError(
                "visual selection records must be sorted by priority_rank"
            )
        counts = Counter(record.decision for record in records)
        expected_summary = VisualSelectionSummary(
            candidate_count=len(records),
            selected_count=counts.get("selected", 0),
            deferred_count=counts.get("deferred", 0),
            limited_count=counts.get("limited", 0),
            not_candidate_count=counts.get("not-candidate", 0),
            selected_bytes=sum(
                record.size_bytes
                for record in records
                if record.decision == "selected"
            ),
        )
        if self.summary != expected_summary:
            raise VisualSelectionSchemaError(
                "visual selection summary does not reconcile with records"
            )
        object.__setattr__(self, "records", records)

    @property
    def executable_records(self) -> tuple[VisualSelectionRecord, ...]:
        return tuple(record for record in self.records if record.executable)

    @property
    def selected_records(self) -> tuple[VisualSelectionRecord, ...]:
        return self.executable_records

    @property
    def priority_sha256(self) -> str:
        return self.priority_payload_sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "version": self.version,
            "project_id": self.project_id,
            "manifest": self.manifest.as_dict(),
            "priority_payload_sha256": self.priority_payload_sha256,
            "summary": self.summary.as_dict(),
            "records": [record.as_dict() for record in self.records],
        }


def visual_selection_manifest_identity_from_dict(
    value: object,
) -> VisualSelectionManifestIdentity:
    if not isinstance(value, dict) or set(value) != {
        "manifest_version",
        "scan_generation",
        "file_count",
        "byte_count",
        "content_sha256",
    }:
        raise VisualSelectionSchemaError(
            "visual selection Manifest identity is malformed"
        )
    return VisualSelectionManifestIdentity(
        manifest_version=value["manifest_version"],
        scan_generation=value["scan_generation"],
        file_count=value["file_count"],
        byte_count=value["byte_count"],
        content_sha256=value["content_sha256"],
    )


def visual_selection_summary_from_dict(value: object) -> VisualSelectionSummary:
    if not isinstance(value, dict) or set(value) != {
        "candidate_count",
        "selected_count",
        "deferred_count",
        "limited_count",
        "not_candidate_count",
        "selected_bytes",
    }:
        raise VisualSelectionSchemaError("visual selection summary is malformed")
    return VisualSelectionSummary(
        candidate_count=value["candidate_count"],
        selected_count=value["selected_count"],
        deferred_count=value["deferred_count"],
        limited_count=value["limited_count"],
        not_candidate_count=value["not_candidate_count"],
        selected_bytes=value["selected_bytes"],
    )


def visual_selection_record_from_dict(value: object) -> VisualSelectionRecord:
    record = _schema_object(
        value,
        expected_fields={
            "schema_version",
            "kind",
            "path",
            "content_sha256",
            "size_bytes",
            "mtime_ns",
            "format",
            "research_role",
            "priority_rank",
            "deep_read_status",
            "decision",
            "local_content_access",
            "raw_external_send",
            "local_reason_code",
            "external_reason_code",
            "reason_codes",
            "referenced_by",
        },
        kind=VISUAL_SELECTION_RECORD_KIND,
        label="visual selection record",
    )
    return VisualSelectionRecord(
        path=record["path"],
        content_sha256=record["content_sha256"],
        size_bytes=record["size_bytes"],
        mtime_ns=record["mtime_ns"],
        format=record["format"],
        research_role=record["research_role"],
        priority_rank=record["priority_rank"],
        deep_read_status=record["deep_read_status"],
        decision=record["decision"],
        local_content_access=record["local_content_access"],
        raw_external_send=record["raw_external_send"],
        local_reason_code=record["local_reason_code"],
        external_reason_code=record["external_reason_code"],
        reason_codes=record["reason_codes"],
        referenced_by=record["referenced_by"],
    )

def visual_selection_report_from_dict(
    value: object,
    *,
    project_id: str | None = None,
) -> VisualSelectionReport:
    report = _schema_object(
        value,
        expected_fields={
            "schema_version",
            "kind",
            "version",
            "project_id",
            "manifest",
            "priority_payload_sha256",
            "summary",
            "records",
        },
        kind=VISUAL_SELECTION_KIND,
        label="visual selection report",
    )
    if report["version"] != VISUAL_SELECTION_VERSION:
        raise VisualSelectionSchemaError(
            "visual selection artifact version is unsupported"
        )
    raw_records = report["records"]
    if not isinstance(raw_records, list):
        raise VisualSelectionSchemaError("visual selection records must be an array")
    result = VisualSelectionReport(
        project_id=report["project_id"],
        manifest=visual_selection_manifest_identity_from_dict(report["manifest"]),
        priority_payload_sha256=report["priority_payload_sha256"],
        summary=visual_selection_summary_from_dict(report["summary"]),
        records=tuple(
            visual_selection_record_from_dict(record) for record in raw_records
        ),
    )
    if project_id is not None and result.project_id != project_id:
        raise VisualSelectionSchemaError(
            "visual selection project identity is inconsistent"
        )
    return result


def serialize_visual_selection(report: VisualSelectionReport) -> str:
    """Serialize one report to deterministic UTF-8-compatible JSON text."""

    if not isinstance(report, VisualSelectionReport):
        raise VisualSelectionSchemaError(
            "report must be a VisualSelectionReport"
        )
    return (
        json.dumps(
            report.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def deserialize_visual_selection(
    payload: str | bytes,
    *,
    project_id: str | None = None,
) -> VisualSelectionReport:
    """Deserialize strict JSON, rejecting duplicates, legacy, and future data."""

    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise VisualSelectionSchemaError(
                "visual selection JSON must be valid UTF-8 text"
            ) from exc
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise VisualSelectionSchemaError(
            "visual selection JSON must be str or bytes"
        )
    try:
        decoded = parse_json_bytes_strict(raw, label="visual selection report")
    except LayoutError as exc:
        raise VisualSelectionSchemaError(str(exc)) from exc
    return visual_selection_report_from_dict(decoded, project_id=project_id)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise VisualSelectionCurrentnessError(
            "B-07 priority payload is not canonical JSON data"
        ) from exc


def canonical_priority_payload_sha256(payload: object) -> str:
    """Return the canonical compact-JSON SHA-256 for a B-07 payload."""

    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _policy_for_manifest(manifest: ProjectManifest) -> ScanPolicy:
    snapshot = manifest.summary.get("policy")
    if not isinstance(snapshot, dict):
        raise VisualSelectionCurrentnessError(
            "Manifest policy snapshot is missing or malformed"
        )
    raw_config = snapshot.get("config")
    if not isinstance(raw_config, dict) or set(raw_config) != _POLICY_CONFIG_FIELDS:
        raise VisualSelectionCurrentnessError(
            "Manifest policy config is missing or malformed"
        )
    try:
        config = ScanPolicyConfig(
            include_patterns=raw_config["include_patterns"],
            exclude_patterns=raw_config["exclude_patterns"],
            sensitive_patterns=raw_config["sensitive_patterns"],
            external_include_patterns=raw_config["external_include_patterns"],
            external_exclude_patterns=raw_config["external_exclude_patterns"],
            max_content_file_bytes=raw_config["max_content_file_bytes"],
            max_raw_external_send_bytes=raw_config[
                "max_raw_external_send_bytes"
            ],
            follow_symlinks=raw_config["follow_symlinks"],
            external_send_mode=raw_config["external_send_mode"],
            case_sensitive=raw_config["case_sensitive"],
        )
        policy = load_scan_policy(manifest.project_root, config=config)
    except (OSError, TypeError, ValueError) as exc:
        raise VisualSelectionCurrentnessError(
            f"could not reconstruct the Manifest scan policy: {exc}"
        ) from exc
    if policy.as_dict() != snapshot:
        raise VisualSelectionCurrentnessError(
            "current scan policy differs from the Manifest snapshot; run inventory first"
        )
    return policy


def _load_manifest_snapshot(
    registration: ProjectRegistrationResult,
) -> tuple[ProjectManifest, bytes, str]:
    manifest_file = registration.layout.validate_machine_state_path(
        registration.layout.manifest_file,
        allow_missing_leaf=False,
    )
    try:
        snapshot_before = manifest_file.read_bytes()
        manifest = load_project_manifest(
            manifest_file,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )
        snapshot_after = manifest_file.read_bytes()
    except OSError as exc:
        raise VisualSelectionCurrentnessError(
            f"could not read current Manifest: {exc}"
        ) from exc
    registration.layout.validate_machine_state_path(
        manifest_file,
        allow_missing_leaf=False,
    )
    if snapshot_before != snapshot_after:
        raise VisualSelectionCurrentnessError(
            "Manifest changed while visual selection was constructed"
        )
    return (
        manifest,
        snapshot_before,
        hashlib.sha256(snapshot_before).hexdigest(),
    )

def _manifest_identity(
    manifest: ProjectManifest,
    manifest_sha256: str,
) -> VisualSelectionManifestIdentity:
    records = manifest.file_records
    return VisualSelectionManifestIdentity(
        manifest_version=manifest.manifest_version,
        scan_generation=manifest.scan_generation,
        file_count=len(records),
        byte_count=sum(record["size_bytes"] for record in records),
        content_sha256=manifest_sha256,
    )


def _validate_priority_manifest_identity(
    priority: dict[str, Any],
    manifest: ProjectManifest,
    manifest_sha256: str,
) -> VisualSelectionManifestIdentity:
    identity = _manifest_identity(manifest, manifest_sha256)
    if priority.get("manifest") != identity.as_dict():
        raise VisualSelectionCurrentnessError(
            "B-07 priority is stale for the exact current Manifest bytes"
        )
    files = priority.get("files")
    summary = priority.get("summary")
    if not isinstance(files, list) or not isinstance(summary, dict):
        raise VisualSelectionCurrentnessError(
            "current-grounded B-07 payload is malformed"
        )
    if (
        len(files) != identity.file_count
        or summary.get("manifest_file_count") != identity.file_count
        or summary.get("manifest_byte_count") != identity.byte_count
        or summary.get("ranked_file_count") != identity.file_count
    ):
        raise VisualSelectionCurrentnessError(
            "B-07 Manifest counts do not reconcile with current Manifest records"
        )
    return identity


def _summary_for_records(
    records: Iterable[VisualSelectionRecord],
) -> VisualSelectionSummary:
    materialized = tuple(records)
    counts = Counter(record.decision for record in materialized)
    return VisualSelectionSummary(
        candidate_count=len(materialized),
        selected_count=counts.get("selected", 0),
        deferred_count=counts.get("deferred", 0),
        limited_count=counts.get("limited", 0),
        not_candidate_count=counts.get("not-candidate", 0),
        selected_bytes=sum(
            record.size_bytes
            for record in materialized
            if record.decision == "selected"
        ),
    )


def _build_grounded_records(
    priority: dict[str, Any],
    manifest: ProjectManifest,
    policy: ScanPolicy,
) -> tuple[VisualSelectionRecord, ...]:
    manifest_by_path = {record["path"]: record for record in manifest.file_records}
    priority_files = priority["files"]
    priority_paths = [record["path"] for record in priority_files]
    if len(priority_paths) != len(set(priority_paths)):
        raise VisualSelectionCurrentnessError(
            "B-07 priority contains duplicate file paths"
        )
    if set(priority_paths) != set(manifest_by_path):
        raise VisualSelectionCurrentnessError(
            "B-07 files do not match the current Manifest"
        )

    grounded: list[VisualSelectionRecord] = []
    prior_rank = 0
    for priority_record in priority_files:
        path = _relative_path(priority_record["path"], label="B-07 file path")
        rank = priority_record["priority_rank"]
        if not _is_integer(rank) or rank <= prior_rank:
            raise VisualSelectionCurrentnessError(
                "B-07 files are not sorted by a unique increasing priority_rank"
            )
        prior_rank = rank
        manifest_record = manifest_by_path[path]
        classification = classification_from_dict(manifest_record["classification"])
        expected = {
            "content_sha256": manifest_record["content_sha256"],
            "size_bytes": manifest_record["size_bytes"],
            "format": classification.format,
            "research_role": classification.research_role,
        }
        if any(priority_record.get(field) != value for field, value in expected.items()):
            raise VisualSelectionCurrentnessError(
                f"B-07 file facts are stale for current Manifest path {path}"
            )
        if classification.format not in VISUAL_CANDIDATE_FORMATS:
            continue
        decision = policy.decide_file(path, size_bytes=manifest_record["size_bytes"])
        grounded.append(
            VisualSelectionRecord(
                path=path,
                content_sha256=manifest_record["content_sha256"],
                size_bytes=manifest_record["size_bytes"],
                mtime_ns=manifest_record["mtime_ns"],
                format=classification.format,
                research_role=classification.research_role,
                priority_rank=rank,
                deep_read_status=priority_record["deep_read_status"],
                decision=priority_record["deep_read_status"],
                local_content_access=decision.local_content_access,
                raw_external_send=decision.raw_external_send,
                local_reason_code=decision.local_reason_code,
                external_reason_code=decision.external_reason_code,
                reason_codes=tuple(priority_record["reason_codes"]),
                referenced_by=tuple(priority_record["referenced_by"]),
            )
        )
    return tuple(grounded)


def _same_registration(
    first: ProjectRegistrationResult,
    second: ProjectRegistrationResult,
) -> bool:
    return (
        first.project_id == second.project_id
        and first.project_root == second.project_root
        and first.layout.machine_root == second.layout.machine_root
        and first.layout.knowledge_root == second.layout.knowledge_root
    )


def _build_visual_selection(
    workspace_root: str | Path,
    project_id: str,
) -> VisualSelectionReport:
    # This public current-grounded loader is the only B-07 authorization seam.
    priority_before = load_current_reading_priority(workspace_root, project_id)
    priority_before_bytes = _canonical_json_bytes(priority_before)
    priority_sha256 = hashlib.sha256(priority_before_bytes).hexdigest()

    registration = load_registered_project(workspace_root, project_id)
    manifest, manifest_snapshot, manifest_sha256 = _load_manifest_snapshot(
        registration
    )
    identity = _validate_priority_manifest_identity(
        priority_before,
        manifest,
        manifest_sha256,
    )
    policy = _policy_for_manifest(manifest)
    records = _build_grounded_records(priority_before, manifest, policy)
    report = VisualSelectionReport(
        project_id=registration.project_id,
        manifest=identity,
        priority_payload_sha256=priority_sha256,
        summary=_summary_for_records(records),
        records=records,
    )

    # Construction does not inherit a lock from B-07. Reauthorize after the
    # report exists, then independently prove the exact Manifest/policy did not
    # move while that report was being assembled.
    priority_after = load_current_reading_priority(workspace_root, project_id)
    priority_after_bytes = _canonical_json_bytes(priority_after)
    if (
        priority_after_bytes != priority_before_bytes
        or priority_after != priority_before
        or hashlib.sha256(priority_after_bytes).hexdigest() != priority_sha256
    ):
        raise VisualSelectionCurrentnessError(
            "B-07 priority changed while visual selection was constructed"
        )

    current_registration = load_registered_project(workspace_root, project_id)
    if not _same_registration(registration, current_registration):
        raise VisualSelectionCurrentnessError(
            "project registration changed while visual selection was constructed"
        )
    final_manifest, final_snapshot, final_sha256 = _load_manifest_snapshot(
        current_registration
    )
    if final_snapshot != manifest_snapshot or final_sha256 != manifest_sha256:
        raise VisualSelectionCurrentnessError(
            "Manifest changed while visual selection was constructed"
        )
    if _manifest_identity(final_manifest, final_sha256) != identity:
        raise VisualSelectionCurrentnessError(
            "Manifest identity changed while visual selection was constructed"
        )
    final_policy = _policy_for_manifest(final_manifest)
    if final_policy.as_dict() != policy.as_dict():
        raise VisualSelectionCurrentnessError(
            "scan policy changed while visual selection was constructed"
        )
    return report


def build_visual_selection(
    workspace_root: str | Path,
    project_id: str,
) -> VisualSelectionReport:
    """Build a non-persisted current-grounded visual/PDF selection report.

    Authorization comes exclusively from ``load_current_reading_priority`` and
    only records whose preserved decision is ``selected`` are executable.
    """

    try:
        return _build_visual_selection(workspace_root, project_id)
    except VisualSelectionError:
        raise
    except (KeyError, LayoutError, OSError, TypeError, ValueError) as exc:
        raise VisualSelectionCurrentnessError(
            f"could not build current-grounded visual selection: {exc}"
        ) from exc
