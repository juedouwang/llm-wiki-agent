#!/usr/bin/env python3
"""Persistent D-01 source identities for inventoried project files.

The registry is Core-owned machine state.  It assigns one opaque source ID to
an in-scope Manifest path, retains past assignments, and never opens or writes
the registered research project.  Source versions, path aliases, Evidence,
reopening, relocation, and health semantics belong to D-02 through D-06.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

if __package__:
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError, schema_version_of
    from .project_registry import load_registered_project
else:
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        schema_version_of,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]


SOURCE_REGISTRY_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SOURCE_REGISTRY_KIND = "llmwiki-source-registry"
SOURCE_REGISTRY_VERSION = "source-registry-v1"
SOURCE_ID_STRATEGY = "core-uuid4-v1"
_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")
_DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.01
_MAX_ID_ATTEMPTS = 128

_SUMMARY_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "identity_strategy",
    "source_count",
}
_SOURCE_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "source_id",
    "manifest_path",
    "first_seen_scan_generation",
}


class SourceRegistryError(LayoutError):
    """Base error for unsafe, corrupt, or conflicting source identity state."""


class SourceRegistryLockError(SourceRegistryError):
    """Raised when another writer keeps the source registry locked."""


class SourceRegistryConflictError(SourceRegistryError):
    """Raised when persisted identities cannot be reconciled safely."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise SourceRegistryError(
            "source_id must use Core-generated form 'src-' plus 32 lowercase hex digits"
        )
    return value


def _manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SourceRegistryError("manifest_path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise SourceRegistryError(
            "manifest_path must be normalized project-relative POSIX form"
        )
    return value


def _canonical_json_line(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceRegistryError("source registry contains unstable JSON data") from exc
    return payload.encode("utf-8") + b"\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceRegistryError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise SourceRegistryError(f"non-finite JSON constant {value!r} is not supported")


def _normalize_json_value(value: object) -> object:
    """Reject non-finite values before persisted dictionaries are compared."""

    if value is None or isinstance(value, (str, bool)) or _is_integer(value):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SourceRegistryError("source registry contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_json_value(item) for key, item in value.items()}
    raise SourceRegistryError("source registry must contain only JSON values")


@dataclass(frozen=True)
class SourceRecord:
    """One immutable D-01 assignment from a Manifest path to a source ID."""

    source_id: str
    manifest_path: str
    first_seen_scan_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _source_id(self.source_id))
        object.__setattr__(self, "manifest_path", _manifest_path(self.manifest_path))
        if (
            not _is_integer(self.first_seen_scan_generation)
            or self.first_seen_scan_generation < 1
        ):
            raise SourceRegistryError(
                "first_seen_scan_generation must be a positive integer"
            )

    def as_dict(self, *, project_id: str) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_REGISTRY_KIND,
            "registry_version": SOURCE_REGISTRY_VERSION,
            "record_type": "source",
            "project_id": project_id,
            "source_id": self.source_id,
            "manifest_path": self.manifest_path,
            "first_seen_scan_generation": self.first_seen_scan_generation,
        }


@dataclass(frozen=True)
class SourceRegistry:
    """A strictly validated project-local source identity registry."""

    project_id: str
    sources_file: Path
    records: tuple[SourceRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise SourceRegistryError("project_id must be a non-empty string")
        records = tuple(self.records)
        if not all(isinstance(record, SourceRecord) for record in records):
            raise SourceRegistryError("records must contain only SourceRecord objects")
        if tuple(sorted(records, key=lambda item: item.manifest_path)) != records:
            raise SourceRegistryError("source records must be sorted by manifest_path")
        source_ids = [record.source_id for record in records]
        paths = [record.manifest_path for record in records]
        if len(set(source_ids)) != len(source_ids):
            raise SourceRegistryConflictError("source registry contains duplicate source_id values")
        if len(set(paths)) != len(paths):
            raise SourceRegistryConflictError(
                "source registry contains duplicate manifest_path values"
            )
        object.__setattr__(self, "sources_file", Path(self.sources_file))
        object.__setattr__(self, "records", records)

    @property
    def by_path(self) -> dict[str, SourceRecord]:
        return {record.manifest_path: record for record in self.records}

    @property
    def by_source_id(self) -> dict[str, SourceRecord]:
        return {record.source_id: record for record in self.records}

    def serialized_bytes(self) -> bytes:
        summary = {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_REGISTRY_KIND,
            "registry_version": SOURCE_REGISTRY_VERSION,
            "record_type": "summary",
            "project_id": self.project_id,
            "identity_strategy": SOURCE_ID_STRATEGY,
            "source_count": len(self.records),
        }
        return b"".join(
            [
                _canonical_json_line(summary),
                *(
                    _canonical_json_line(record.as_dict(project_id=self.project_id))
                    for record in self.records
                ),
            ]
        )


@dataclass(frozen=True)
class SourceRegistrySyncResult:
    """Auditable outcome of assigning identities for one Manifest generation."""

    project_id: str
    project_root: Path
    manifest_file: Path
    sources_file: Path
    scan_generation: int
    source_count: int
    manifest_file_count: int
    assigned_count: int
    existing_count: int
    wrote_registry: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "manifest_file": str(self.manifest_file),
            "sources_file": str(self.sources_file),
            "registry_version": SOURCE_REGISTRY_VERSION,
            "scan_generation": self.scan_generation,
            "source_count": self.source_count,
            "manifest_file_count": self.manifest_file_count,
            "assigned_count": self.assigned_count,
            "existing_count": self.existing_count,
            "wrote_registry": self.wrote_registry,
        }


def _registry_error(path: Path, message: str, line_number: int | None = None) -> SourceRegistryError:
    location = f"{path}:{line_number}" if line_number is not None else str(path)
    return SourceRegistryError(f"invalid source registry at {location}: {message}")


def _validate_common_row(
    record: object,
    *,
    path: Path,
    line_number: int,
    project_id: str,
    expected_fields: set[str],
    record_type: str,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise _registry_error(path, "each JSONL line must contain an object", line_number)
    if set(record) != expected_fields:
        raise _registry_error(
            path,
            f"{record_type} row must contain exactly the Schema v1 fields",
            line_number,
        )
    try:
        schema_version_of(
            record,
            allow_legacy=False,
            max_supported=SOURCE_REGISTRY_SCHEMA_VERSION,
        )
    except LayoutError as exc:
        raise _registry_error(path, str(exc), line_number) from exc
    if record.get("kind") != SOURCE_REGISTRY_KIND:
        raise _registry_error(
            path,
            f"unexpected kind {record.get('kind')!r}",
            line_number,
        )
    if record.get("registry_version") != SOURCE_REGISTRY_VERSION:
        raise _registry_error(
            path,
            f"unsupported registry_version {record.get('registry_version')!r}",
            line_number,
        )
    if record.get("record_type") != record_type:
        raise _registry_error(
            path,
            f"expected record_type {record_type!r}",
            line_number,
        )
    if record.get("project_id") != project_id:
        raise _registry_error(
            path,
            "project_id does not match the registered project",
            line_number,
        )
    _normalize_json_value(record)
    return record


def _decode_registry_line(raw_line: str, *, path: Path, line_number: int) -> object:
    if not raw_line:
        raise _registry_error(path, "blank JSONL lines are not allowed", line_number)
    try:
        return json.loads(
            raw_line,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise _registry_error(path, f"invalid JSON: {exc.msg}", line_number) from exc
    except SourceRegistryError as exc:
        raise _registry_error(path, str(exc), line_number) from exc


def _load_source_registry_file(
    sources_file: Path,
    *,
    project_id: str,
    missing_ok: bool,
) -> SourceRegistry:
    path = Path(sources_file)
    if not path.exists():
        if missing_ok:
            return SourceRegistry(project_id=project_id, sources_file=path, records=())
        raise SourceRegistryError(f"source registry does not exist: {path}")
    if path.is_symlink():
        raise SourceRegistryError(f"source registry must not be a symbolic link: {path}")
    if not path.is_file():
        raise SourceRegistryError(f"source registry is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceRegistryError(f"could not read source registry {path}: {exc}") from exc
    lines = text.splitlines()
    if not lines:
        raise _registry_error(path, "the registry is empty")
    if text.endswith("\n\n") or text.endswith("\r\n\r\n"):
        raise _registry_error(path, "blank JSONL lines are not allowed")

    summary = _validate_common_row(
        _decode_registry_line(lines[0], path=path, line_number=1),
        path=path,
        line_number=1,
        project_id=project_id,
        expected_fields=_SUMMARY_FIELDS,
        record_type="summary",
    )
    if summary.get("identity_strategy") != SOURCE_ID_STRATEGY:
        raise _registry_error(
            path,
            f"unsupported identity_strategy {summary.get('identity_strategy')!r}",
            1,
        )
    source_count = summary.get("source_count")
    if not _is_integer(source_count) or source_count < 0:
        raise _registry_error(path, "source_count must be a non-negative integer", 1)

    records: list[SourceRecord] = []
    for line_number, line in enumerate(lines[1:], start=2):
        row = _validate_common_row(
            _decode_registry_line(line, path=path, line_number=line_number),
            path=path,
            line_number=line_number,
            project_id=project_id,
            expected_fields=_SOURCE_FIELDS,
            record_type="source",
        )
        try:
            records.append(
                SourceRecord(
                    source_id=row.get("source_id"),
                    manifest_path=row.get("manifest_path"),
                    first_seen_scan_generation=row.get(
                        "first_seen_scan_generation"
                    ),
                )
            )
        except SourceRegistryError as exc:
            raise _registry_error(path, str(exc), line_number) from exc

    if source_count != len(records):
        raise _registry_error(
            path,
            "source_count does not match the number of source rows",
            1,
        )
    try:
        return SourceRegistry(
            project_id=project_id,
            sources_file=path,
            records=tuple(records),
        )
    except SourceRegistryError as exc:
        raise _registry_error(path, str(exc)) from exc


def load_source_registry(
    workspace_root: str | Path,
    project_id: str,
) -> SourceRegistry:
    """Load one existing D-01 registry without modifying project or machine state."""

    registration = load_registered_project(workspace_root, project_id)
    return _load_source_registry_file(
        registration.layout.sources_file,
        project_id=registration.project_id,
        missing_ok=False,
    )


def _new_source_id(existing_ids: set[str]) -> str:
    for _attempt in range(_MAX_ID_ATTEMPTS):
        candidate = f"src-{uuid.uuid4().hex}"
        if candidate not in existing_ids:
            return candidate
    raise SourceRegistryConflictError(
        "could not allocate a unique source_id after repeated UUID collisions"
    )


@contextmanager
def _exclusive_registry_lock(
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
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise SourceRegistryLockError(
                    f"timed out waiting for source registry lock: {lock_file}"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except OSError as exc:
            raise SourceRegistryLockError(
                f"could not create source registry lock {lock_file}: {exc}"
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
            raise SourceRegistryLockError(
                f"could not release source registry lock {lock_file}: {exc}"
            ) from exc


def _write_registry_atomic(registry: SourceRegistry) -> bool:
    path = registry.sources_file
    if path.is_symlink():
        raise SourceRegistryError(f"source registry must not be a symbolic link: {path}")
    payload = registry.serialized_bytes()
    if path.exists():
        try:
            if path.read_bytes() == payload:
                return False
        except OSError as exc:
            raise SourceRegistryError(f"could not compare source registry {path}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".sources.jsonl.",
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
        raise SourceRegistryError(f"could not write source registry {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_current_manifest(
    workspace_root: str | Path,
    project_id: str,
) -> tuple[Any, ProjectManifest]:
    registration = load_registered_project(workspace_root, project_id)
    manifest = load_project_manifest(
        registration.layout.manifest_file,
        project_id=registration.project_id,
        project_root=registration.project_root,
        required_manifest_version=PROJECT_MANIFEST_VERSION,
    )
    return registration, manifest


def sync_source_registry(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> SourceRegistrySyncResult:
    """Assign persistent IDs to every in-scope regular file in the Manifest.

    Existing records are retained even when a path is absent from the current
    Manifest.  D-01 never interprets a disappearance as deletion or relocation.
    """

    registration, manifest = _load_current_manifest(workspace_root, project_id)
    sources_file = registration.layout.sources_file
    lock_file = sources_file.with_name(f"{sources_file.name}.lock")
    sources_file.parent.mkdir(parents=True, exist_ok=True)

    with _exclusive_registry_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        registry = _load_source_registry_file(
            sources_file,
            project_id=registration.project_id,
            missing_ok=True,
        )
        if any(
            record.first_seen_scan_generation > manifest.scan_generation
            for record in registry.records
        ):
            raise SourceRegistryConflictError(
                "current Manifest generation predates persisted source assignments"
            )

        records = list(registry.records)
        by_path = registry.by_path
        existing_ids = set(registry.by_source_id)
        assigned_count = 0
        manifest_paths: list[str] = []
        for file_record in manifest.file_records:
            manifest_paths.append(_manifest_path(file_record.get("path")))
        for manifest_path in sorted(manifest_paths):
            if manifest_path in by_path:
                continue
            source_id = _new_source_id(existing_ids)
            existing_ids.add(source_id)
            source_record = SourceRecord(
                source_id=source_id,
                manifest_path=manifest_path,
                first_seen_scan_generation=manifest.scan_generation,
            )
            records.append(source_record)
            by_path[manifest_path] = source_record
            assigned_count += 1

        updated = SourceRegistry(
            project_id=registration.project_id,
            sources_file=sources_file,
            records=tuple(sorted(records, key=lambda item: item.manifest_path)),
        )
        wrote_registry = _write_registry_atomic(updated)

    return SourceRegistrySyncResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        manifest_file=manifest.manifest_file,
        sources_file=sources_file,
        scan_generation=manifest.scan_generation,
        source_count=len(updated.records),
        manifest_file_count=len(manifest.file_records),
        assigned_count=assigned_count,
        existing_count=len(manifest.file_records) - assigned_count,
        wrote_registry=wrote_registry,
    )
