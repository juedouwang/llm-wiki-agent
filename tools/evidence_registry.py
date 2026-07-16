#!/usr/bin/env python3
"""Deterministic, source-version-bound Evidence records.

D-03 persists exact evidence identity without reopening source files. D-04 adds
source reopening, D-05 adds relocation recovery, and D-06 aggregates health.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

if __package__:
    from .extraction_schema import (
        EXTRACTION_SCHEMA_VERSION,
        ExtractionSchemaError,
        Locator,
        locator_from_dict,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        validate_project_id,
    )
    from .project_registry import load_registered_project
    from .source_registry import SourceRecord, SourceRegistry, load_source_registry
else:
    from extraction_schema import (  # type: ignore[no-redef]
        EXTRACTION_SCHEMA_VERSION,
        ExtractionSchemaError,
        Locator,
        locator_from_dict,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        validate_project_id,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from source_registry import (  # type: ignore[no-redef]
        SourceRecord,
        SourceRegistry,
        load_source_registry,
    )


EVIDENCE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
EVIDENCE_KIND = "llmwiki-evidence"
EVIDENCE_REGISTRY_KIND = "llmwiki-evidence-registry"
EVIDENCE_VERSION = "evidence-v1"
EVIDENCE_REGISTRY_VERSION = "evidence-registry-v1"
EVIDENCE_IDENTITY_VERSION = "evidence-identity-v1"
EVIDENCE_HASH_ALGORITHM = "sha256"
EXCERPT_TEXT_ENCODING = "utf-8"

_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")
_EVIDENCE_ID_PATTERN = re.compile(r"evd-[0-9a-f]{64}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.01

_SUMMARY_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "evidence_version",
    "identity_version",
    "hash_algorithm",
    "excerpt_text_encoding",
    "locator_schema_version",
    "evidence_count",
}
_EVIDENCE_FIELDS = {
    "schema_version",
    "kind",
    "evidence_version",
    "record_type",
    "project_id",
    "evidence_id",
    "source_id",
    "source_version",
    "content_hash",
    "locator",
    "excerpt_hash",
}


class EvidenceError(LayoutError):
    """Base error for malformed, unsafe, or unbound Evidence state."""


class EvidenceConflictError(EvidenceError):
    """Raised when Evidence state has conflicting identities or bindings."""


class EvidenceMismatchError(EvidenceError):
    """Raised when supplied bytes or hashes do not match exact Evidence data."""


class EvidenceLockError(EvidenceError):
    """Raised when an Evidence writer cannot acquire or release its lock."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_integer(value: object, field_name: str) -> int:
    if not _is_integer(value) or value < 1:
        raise EvidenceError(f"{field_name} must be a positive integer")
    return value


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise EvidenceError(
            "source_id must use Core-generated form 'src-' plus 32 lowercase hex digits"
        )
    return value


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EvidenceError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _evidence_id(value: object) -> str:
    if not isinstance(value, str) or _EVIDENCE_ID_PATTERN.fullmatch(value) is None:
        raise EvidenceError(
            "evidence_id must use form 'evd-' plus 64 lowercase hex digits"
        )
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvidenceError("Evidence contains unstable JSON data") from exc


def _canonical_json_line(value: object) -> bytes:
    return _canonical_json(value) + b"\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise EvidenceError(f"non-finite JSON constant {value!r} is not supported")


def excerpt_bytes(excerpt: str | bytes) -> bytes:
    """Return exact bytes used by the excerpt hash contract.

    Bytes are hashed without transformation. Text is encoded as strict UTF-8;
    line endings and Unicode normalization are intentionally preserved.
    """

    if isinstance(excerpt, bytes):
        return excerpt
    if isinstance(excerpt, str):
        try:
            return excerpt.encode(EXCERPT_TEXT_ENCODING, errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("excerpt text must be encodable as strict UTF-8") from exc
    raise EvidenceError("excerpt must be str or bytes")


def excerpt_sha256(excerpt: str | bytes) -> str:
    """Hash exact excerpt bytes with the Evidence SHA-256 contract."""

    return hashlib.sha256(excerpt_bytes(excerpt)).hexdigest()


def _normalize_locator(locator: Locator | dict[str, Any]) -> Locator:
    raw_locator: object = locator.as_dict() if isinstance(locator, Locator) else locator
    if not isinstance(raw_locator, dict):
        raise EvidenceError("invalid Evidence locator: locator must be an object")
    locator_version = raw_locator.get("schema_version")
    if not _is_integer(locator_version):
        raise EvidenceError(
            "invalid Evidence locator: schema_version must be an integer"
        )
    try:
        return locator_from_dict(raw_locator)
    except ExtractionSchemaError as exc:
        raise EvidenceError(f"invalid Evidence locator: {exc}") from exc


def _identity_fields(
    *,
    project_id: str,
    source_id: str,
    content_hash: str,
    locator: Locator,
    excerpt_hash: str,
) -> dict[str, Any]:
    return {
        "identity_version": EVIDENCE_IDENTITY_VERSION,
        "project_id": validate_project_id(project_id),
        "source_id": _source_id(source_id),
        "content_hash": _sha256(content_hash, "content_hash"),
        "locator": _normalize_locator(locator).as_dict(),
        "excerpt_hash": _sha256(excerpt_hash, "excerpt_hash"),
    }


def evidence_id_for(
    *,
    project_id: str,
    source_id: str,
    content_hash: str,
    locator: Locator | dict[str, Any],
    excerpt_hash: str,
) -> str:
    """Derive a stable ID from the exact D-03 Evidence identity fields."""

    normalized_locator = _normalize_locator(locator)
    payload = _identity_fields(
        project_id=project_id,
        source_id=source_id,
        content_hash=content_hash,
        locator=normalized_locator,
        excerpt_hash=excerpt_hash,
    )
    return f"evd-{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


@dataclass(frozen=True)
class Evidence:
    """One exact excerpt identity bound to a recorded source version."""

    project_id: str
    evidence_id: str
    source_id: str
    source_version: int
    content_hash: str
    locator: Locator
    excerpt_hash: str

    def __post_init__(self) -> None:
        project_id = validate_project_id(self.project_id)
        evidence_id = _evidence_id(self.evidence_id)
        source_id = _source_id(self.source_id)
        source_version = _positive_integer(self.source_version, "source_version")
        content_hash = _sha256(self.content_hash, "content_hash")
        locator = _normalize_locator(self.locator)
        excerpt_hash = _sha256(self.excerpt_hash, "excerpt_hash")
        expected_id = evidence_id_for(
            project_id=project_id,
            source_id=source_id,
            content_hash=content_hash,
            locator=locator,
            excerpt_hash=excerpt_hash,
        )
        if evidence_id != expected_id:
            raise EvidenceConflictError(
                "evidence_id does not match canonical Evidence identity fields"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_version", source_version)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "excerpt_hash", excerpt_hash)

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        source_id: str,
        source_version: int,
        content_hash: str,
        locator: Locator | dict[str, Any],
        excerpt: str | bytes,
        expected_excerpt_hash: str | None = None,
    ) -> Evidence:
        """Create Evidence after hashing exact supplied text or bytes."""

        digest = excerpt_sha256(excerpt)
        if expected_excerpt_hash is not None:
            expected = _sha256(expected_excerpt_hash, "expected_excerpt_hash")
            if digest != expected:
                raise EvidenceMismatchError(
                    "supplied excerpt does not match expected_excerpt_hash"
                )
        normalized_locator = _normalize_locator(locator)
        evidence_id = evidence_id_for(
            project_id=project_id,
            source_id=source_id,
            content_hash=content_hash,
            locator=normalized_locator,
            excerpt_hash=digest,
        )
        return cls(
            project_id=project_id,
            evidence_id=evidence_id,
            source_id=source_id,
            source_version=source_version,
            content_hash=content_hash,
            locator=normalized_locator,
            excerpt_hash=digest,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "kind": EVIDENCE_KIND,
            "evidence_version": EVIDENCE_VERSION,
            "record_type": "evidence",
            "project_id": self.project_id,
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content_hash": self.content_hash,
            "locator": self.locator.as_dict(),
            "excerpt_hash": self.excerpt_hash,
        }

    def matches_excerpt(self, excerpt: str | bytes) -> bool:
        return excerpt_sha256(excerpt) == self.excerpt_hash

    def matches_content_hash(self, content_hash: str) -> bool:
        return _sha256(content_hash, "content_hash") == self.content_hash


@dataclass(frozen=True)
class EvidenceRegistry:
    """Canonical collection persisted in one project evidence.jsonl."""

    project_id: str
    evidence_file: Path
    records: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        project_id = validate_project_id(self.project_id)
        evidence_file = Path(self.evidence_file).expanduser().resolve()
        records = tuple(self.records)
        if not all(isinstance(record, Evidence) for record in records):
            raise EvidenceError("records must contain only Evidence objects")
        if any(record.project_id != project_id for record in records):
            raise EvidenceConflictError("Evidence project_id does not match registry")
        expected = tuple(sorted(records, key=lambda record: record.evidence_id))
        if records != expected:
            raise EvidenceError("Evidence records must be sorted by evidence_id")
        evidence_ids = [record.evidence_id for record in records]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise EvidenceConflictError("Evidence registry contains duplicate IDs")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "evidence_file", evidence_file)
        object.__setattr__(self, "records", records)

    @property
    def by_evidence_id(self) -> dict[str, Evidence]:
        return {record.evidence_id: record for record in self.records}

    def serialized_bytes(self) -> bytes:
        summary = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "kind": EVIDENCE_REGISTRY_KIND,
            "registry_version": EVIDENCE_REGISTRY_VERSION,
            "record_type": "summary",
            "project_id": self.project_id,
            "evidence_version": EVIDENCE_VERSION,
            "identity_version": EVIDENCE_IDENTITY_VERSION,
            "hash_algorithm": EVIDENCE_HASH_ALGORITHM,
            "excerpt_text_encoding": EXCERPT_TEXT_ENCODING,
            "locator_schema_version": EXTRACTION_SCHEMA_VERSION,
            "evidence_count": len(self.records),
        }
        return b"".join(
            [
                _canonical_json_line(summary),
                *(_canonical_json_line(record.as_dict()) for record in self.records),
            ]
        )


@dataclass(frozen=True)
class EvidenceRegistrationResult:
    """Auditable result of one deterministic Evidence registration."""

    project_id: str
    evidence_file: Path
    evidence: Evidence
    evidence_count: int
    wrote_registry: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "evidence_file": str(self.evidence_file),
            "registry_version": EVIDENCE_REGISTRY_VERSION,
            "evidence_count": self.evidence_count,
            "wrote_registry": self.wrote_registry,
            "evidence": self.evidence.as_dict(),
        }


@dataclass(frozen=True)
class EvidenceValidationResult:
    """Single-record D-03 hash/binding validation, without source reopening."""

    evidence_id: str
    valid: bool
    reason_code: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "valid": self.valid,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


def _validate_schema_version(value: object, *, label: str) -> None:
    if value == EVIDENCE_SCHEMA_VERSION and _is_integer(value):
        return
    if _is_integer(value) and value > EVIDENCE_SCHEMA_VERSION:
        raise EvidenceError(
            f"{label} schema_version {value} is newer than supported "
            f"version {EVIDENCE_SCHEMA_VERSION}"
        )
    raise EvidenceError(f"{label} schema_version is missing, legacy, or unsupported")


def _exact_fields(record: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise EvidenceError(f"{label} must be an object")
    if set(record) != fields:
        raise EvidenceError(f"{label} must contain exactly the Evidence v1 fields")
    return record


def _validate_common(
    record: dict[str, Any],
    *,
    kind: str,
    version_field: str,
    version: str,
    record_type: str,
    project_id: str,
    label: str,
) -> None:
    _validate_schema_version(record.get("schema_version"), label=label)
    if record.get("kind") != kind:
        raise EvidenceError(f"unexpected {label} kind {record.get('kind')!r}")
    if record.get(version_field) != version:
        raise EvidenceError(
            f"unsupported {version_field} {record.get(version_field)!r}"
        )
    if record.get("record_type") != record_type:
        raise EvidenceError(f"unexpected {label} record_type")
    if record.get("project_id") != project_id:
        raise EvidenceConflictError(f"{label} project_id does not match registration")


def _parse_evidence_row(value: object, *, project_id: str) -> Evidence:
    row = _exact_fields(value, _EVIDENCE_FIELDS, label="Evidence row")
    _validate_common(
        row,
        kind=EVIDENCE_KIND,
        version_field="evidence_version",
        version=EVIDENCE_VERSION,
        record_type="evidence",
        project_id=project_id,
        label="Evidence row",
    )
    locator = _normalize_locator(row["locator"])
    return Evidence(
        project_id=row["project_id"],
        evidence_id=row["evidence_id"],
        source_id=row["source_id"],
        source_version=row["source_version"],
        content_hash=row["content_hash"],
        locator=locator,
        excerpt_hash=row["excerpt_hash"],
    )


def _decode_line(raw_line: str, *, path: Path, line_number: int) -> object:
    try:
        return json.loads(
            raw_line,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise EvidenceError(
            f"invalid Evidence JSON in {path} line {line_number}: {exc.msg}"
        ) from exc


def _bind_record(record: Evidence, source_registry: SourceRegistry) -> None:
    try:
        source = source_registry.by_source_id[record.source_id]
    except KeyError as exc:
        raise EvidenceConflictError(
            f"Evidence references unknown source_id {record.source_id}"
        ) from exc
    if record.source_version > len(source.versions):
        raise EvidenceConflictError(
            f"Evidence references unrecorded source version {record.source_version}"
        )
    version = source.versions[record.source_version - 1]
    if version.content_hash != record.content_hash:
        raise EvidenceConflictError(
            "Evidence content_hash does not match its recorded source version"
        )


def _load_evidence_file(
    path: Path,
    *,
    project_id: str,
    source_registry: SourceRegistry,
    missing_ok: bool,
) -> EvidenceRegistry:
    evidence_file = Path(path).expanduser().resolve()
    if not evidence_file.exists():
        if missing_ok:
            return EvidenceRegistry(project_id, evidence_file)
        raise EvidenceError(f"Evidence registry does not exist: {evidence_file}")
    if evidence_file.is_symlink():
        raise EvidenceError(f"Evidence registry must not be a symbolic link: {evidence_file}")
    try:
        raw = evidence_file.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"Evidence registry must be UTF-8: {evidence_file}") from exc
    except OSError as exc:
        raise EvidenceError(f"could not read Evidence registry {evidence_file}: {exc}") from exc
    lines = text.splitlines()
    if not lines:
        raise EvidenceError(f"Evidence registry is empty: {evidence_file}")
    if any(not line.strip() for line in lines):
        raise EvidenceError("Evidence registry must not contain blank rows")
    rows = [
        _decode_line(line, path=evidence_file, line_number=index)
        for index, line in enumerate(lines, start=1)
    ]
    summary = _exact_fields(rows[0], _SUMMARY_FIELDS, label="Evidence summary")
    _validate_common(
        summary,
        kind=EVIDENCE_REGISTRY_KIND,
        version_field="registry_version",
        version=EVIDENCE_REGISTRY_VERSION,
        record_type="summary",
        project_id=project_id,
        label="Evidence summary",
    )
    if summary.get("evidence_version") != EVIDENCE_VERSION:
        raise EvidenceError("Evidence summary has unsupported evidence_version")
    if summary.get("identity_version") != EVIDENCE_IDENTITY_VERSION:
        raise EvidenceError("Evidence summary has unsupported identity_version")
    if summary.get("hash_algorithm") != EVIDENCE_HASH_ALGORITHM:
        raise EvidenceError("Evidence summary has unsupported hash_algorithm")
    if summary.get("excerpt_text_encoding") != EXCERPT_TEXT_ENCODING:
        raise EvidenceError("Evidence summary has unsupported excerpt_text_encoding")
    locator_version = summary.get("locator_schema_version")
    if locator_version != EXTRACTION_SCHEMA_VERSION or not _is_integer(locator_version):
        raise EvidenceError("Evidence summary has unsupported locator_schema_version")
    count = summary.get("evidence_count")
    if not _is_integer(count) or count < 0:
        raise EvidenceError("Evidence summary evidence_count must be non-negative")
    records = tuple(
        _parse_evidence_row(row, project_id=project_id) for row in rows[1:]
    )
    if count != len(records):
        raise EvidenceConflictError("Evidence summary count does not match rows")
    if tuple(sorted(records, key=lambda item: item.evidence_id)) != records:
        raise EvidenceConflictError("Evidence rows must be sorted by evidence_id")
    registry = EvidenceRegistry(project_id, evidence_file, records)
    for record in registry.records:
        _bind_record(record, source_registry)
    return registry


def load_evidence_registry(
    workspace_root: str | Path,
    project_id: str,
) -> EvidenceRegistry:
    """Load and bind existing Evidence without reading source-project bytes."""

    registration = load_registered_project(workspace_root, project_id)
    source_registry = load_source_registry(workspace_root, registration.project_id)
    return _load_evidence_file(
        registration.layout.evidence_file,
        project_id=registration.project_id,
        source_registry=source_registry,
        missing_ok=False,
    )


def _source_version_for(
    source: SourceRecord,
    *,
    content_hash: str,
    source_version: int | None,
) -> int:
    digest = _sha256(content_hash, "content_hash")
    if source_version is not None:
        version_number = _positive_integer(source_version, "source_version")
        if version_number > len(source.versions):
            raise EvidenceConflictError(
                f"source {source.source_id} has no version {version_number}"
            )
        if source.versions[version_number - 1].content_hash != digest:
            raise EvidenceMismatchError(
                "content_hash does not match the requested source version"
            )
        return version_number
    matches = [
        version.version for version in source.versions if version.content_hash == digest
    ]
    if not matches:
        raise EvidenceConflictError(
            f"content_hash is not recorded for source {source.source_id}"
        )
    return max(matches)


@contextmanager
def _exclusive_evidence_lock(
    lock_file: Path,
    *,
    timeout_seconds: float,
) -> Iterator[None]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("lock_timeout_seconds must be a positive finite number")
    token = f"{os.getpid()}:{uuid.uuid4().hex}\n".encode("ascii")
    deadline = time.monotonic() + float(timeout_seconds)
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except (FileExistsError, PermissionError) as exc:
            if time.monotonic() >= deadline:
                raise EvidenceLockError(
                    f"timed out waiting for Evidence registry lock: {lock_file}"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except OSError as exc:
            raise EvidenceLockError(
                f"could not create Evidence registry lock {lock_file}: {exc}"
            ) from exc
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            target.write(token)
            target.flush()
            os.fsync(target.fileno())
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock_file.read_bytes() == token:
                lock_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EvidenceLockError(
                f"could not release Evidence registry lock {lock_file}: {exc}"
            ) from exc


def _write_evidence_atomic(registry: EvidenceRegistry) -> bool:
    path = registry.evidence_file
    if path.is_symlink():
        raise EvidenceError(f"Evidence registry must not be a symbolic link: {path}")
    payload = registry.serialized_bytes()
    if path.exists():
        try:
            if path.read_bytes() == payload:
                return False
        except OSError as exc:
            raise EvidenceError(
                f"could not compare Evidence registry {path}: {exc}"
            ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".evidence.jsonl.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except OSError as exc:
        raise EvidenceError(f"could not write Evidence registry {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_evidence(
    workspace_root: str | Path,
    project_id: str,
    *,
    source_id: str,
    content_hash: str,
    locator: Locator | dict[str, Any],
    excerpt: str | bytes,
    source_version: int | None = None,
    expected_excerpt_hash: str | None = None,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> EvidenceRegistrationResult:
    """Persist exact Evidence after binding it to recorded source history."""

    registration = load_registered_project(workspace_root, project_id)
    normalized_source_id = _source_id(source_id)
    source_registry = load_source_registry(workspace_root, registration.project_id)
    try:
        source = source_registry.by_source_id[normalized_source_id]
    except KeyError as exc:
        raise EvidenceConflictError(
            f"source_id is not registered for project {project_id}: {normalized_source_id}"
        ) from exc
    version_number = _source_version_for(
        source,
        content_hash=content_hash,
        source_version=source_version,
    )
    candidate = Evidence.create(
        project_id=registration.project_id,
        source_id=normalized_source_id,
        source_version=version_number,
        content_hash=content_hash,
        locator=locator,
        excerpt=excerpt,
        expected_excerpt_hash=expected_excerpt_hash,
    )
    evidence_file = registration.layout.evidence_file
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = evidence_file.with_name(f"{evidence_file.name}.lock")
    with _exclusive_evidence_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        source_registry = load_source_registry(workspace_root, registration.project_id)
        _bind_record(candidate, source_registry)
        registry = _load_evidence_file(
            evidence_file,
            project_id=registration.project_id,
            source_registry=source_registry,
            missing_ok=True,
        )
        existing = registry.by_evidence_id.get(candidate.evidence_id)
        if existing is not None:
            wrote_registry = False
            persisted = existing
        else:
            updated = EvidenceRegistry(
                project_id=registration.project_id,
                evidence_file=evidence_file,
                records=tuple(
                    sorted(
                        (*registry.records, candidate),
                        key=lambda record: record.evidence_id,
                    )
                ),
            )
            wrote_registry = _write_evidence_atomic(updated)
            registry = updated
            persisted = candidate
    return EvidenceRegistrationResult(
        project_id=registration.project_id,
        evidence_file=evidence_file,
        evidence=persisted,
        evidence_count=len(registry.records),
        wrote_registry=wrote_registry,
    )


def validate_evidence(
    evidence: Evidence,
    source_registry: SourceRegistry,
    *,
    observed_content_hash: str | None = None,
    excerpt: str | bytes | None = None,
) -> EvidenceValidationResult:
    """Validate one Evidence binding and supplied hashes without opening a source."""

    if not isinstance(evidence, Evidence):
        raise EvidenceError("evidence must be an Evidence record")
    if not isinstance(source_registry, SourceRegistry):
        raise EvidenceError("source_registry must be a SourceRegistry")
    if evidence.project_id != source_registry.project_id:
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "project-mismatch",
            "Evidence and source registry belong to different projects.",
        )
    source = source_registry.by_source_id.get(evidence.source_id)
    if source is None:
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "unknown-source",
            "Evidence source_id is not registered.",
        )
    if evidence.source_version > len(source.versions):
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "unrecorded-source-version",
            "Evidence source version is not registered.",
        )
    bound_version = source.versions[evidence.source_version - 1]
    if bound_version.content_hash != evidence.content_hash:
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "source-version-hash-mismatch",
            "Evidence hash differs from its recorded source version.",
        )
    if source.current_content_hash != evidence.content_hash:
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "current-content-hash-mismatch",
            "Current recorded source content differs from Evidence.",
        )
    if observed_content_hash is not None and not evidence.matches_content_hash(
        observed_content_hash
    ):
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "observed-content-hash-mismatch",
            "Observed source content differs from Evidence.",
        )
    if excerpt is not None and not evidence.matches_excerpt(excerpt):
        return EvidenceValidationResult(
            evidence.evidence_id,
            False,
            "excerpt-hash-mismatch",
            "Supplied excerpt differs from Evidence.",
        )
    return EvidenceValidationResult(
        evidence.evidence_id,
        True,
        "valid",
        "Evidence source version, content hash, and supplied excerpt match.",
    )
