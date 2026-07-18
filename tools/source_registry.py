#!/usr/bin/env python3
"""Persistent source identities and auditable source versions.

D-01 assigns opaque Core-owned source IDs. D-02 evolves that registry with
content-hash versions and known path history while keeping research projects
read-only. Evidence, source reopening, relocation recovery, and health remain
separate D-03 through D-06 concerns.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

if __package__:
    from .project_inventory import (
        CONTENT_HASH_ALGORITHM,
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError, schema_version_of
    from .project_registry import load_registered_project
    from .stable_file_access import (
        StableFileAccessError,
        StableFileLease,
        StableFileMissingError,
        lease_stable_regular_file,
        read_stable_regular_file,
        write_atomic_stable_file,
    )
else:
    from project_inventory import (  # type: ignore[no-redef]
        CONTENT_HASH_ALGORITHM,
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
    from stable_file_access import (  # type: ignore[no-redef]
        StableFileAccessError,
        StableFileLease,
        StableFileMissingError,
        lease_stable_regular_file,
        read_stable_regular_file,
        write_atomic_stable_file,
    )


SOURCE_REGISTRY_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SOURCE_REGISTRY_KIND = "llmwiki-source-registry"
LEGACY_SOURCE_REGISTRY_VERSION = "source-registry-v1"
SOURCE_REGISTRY_VERSION = "source-registry-v2"
SOURCE_ID_STRATEGY = "core-uuid4-v1"
SOURCE_PATH_KIND = "llmwiki-source-path"
SOURCE_VERSION_HASH_ALGORITHM = CONTENT_HASH_ALGORITHM
_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")
_CONTENT_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.01
_MAX_ID_ATTEMPTS = 128

_V1_SUMMARY_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "identity_strategy",
    "source_count",
}
_V1_SOURCE_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "source_id",
    "manifest_path",
    "first_seen_scan_generation",
}
_V2_SUMMARY_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "identity_strategy",
    "hash_algorithm",
    "source_count",
    "version_count",
}
_V2_SOURCE_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "source_id",
    "current_path",
    "path_history",
    "first_seen_scan_generation",
    "last_seen_scan_generation",
    "current_version",
}
_V2_VERSION_FIELDS = {
    "schema_version",
    "kind",
    "registry_version",
    "record_type",
    "project_id",
    "source_id",
    "version",
    "content_hash",
    "manifest_path",
    "observed_scan_generation",
}
_SOURCE_PATH_FIELDS = {
    "schema_version",
    "kind",
    "path",
    "first_seen_scan_generation",
    "last_seen_scan_generation",
}


class SourceRegistryError(LayoutError):
    """Base error for unsafe, corrupt, or conflicting source state."""


class SourceRegistryLockError(SourceRegistryError):
    """Raised when another writer keeps the source registry locked."""


class SourceRegistryConflictError(SourceRegistryError):
    """Raised when persisted source state cannot be reconciled safely."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_integer(value: object, field_name: str) -> int:
    if not _is_integer(value) or value < 1:
        raise SourceRegistryError(f"{field_name} must be a positive integer")
    return value


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise SourceRegistryError(
            "source_id must use Core-generated form 'src-' plus 32 lowercase hex digits"
        )
    return value


def _content_hash(value: object) -> str:
    if not isinstance(value, str) or _CONTENT_HASH_PATTERN.fullmatch(value) is None:
        raise SourceRegistryError("content_hash must be a lowercase SHA-256 digest")
    return value


def _manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SourceRegistryError("source path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise SourceRegistryError(
            "source path must be normalized project-relative POSIX form"
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
class SourcePathRecord:
    """One known project-relative path and its observed generation range."""

    path: str
    first_seen_scan_generation: int
    last_seen_scan_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _manifest_path(self.path))
        first = _positive_integer(
            self.first_seen_scan_generation,
            "path first_seen_scan_generation",
        )
        last = _positive_integer(
            self.last_seen_scan_generation,
            "path last_seen_scan_generation",
        )
        if last < first:
            raise SourceRegistryError(
                "path last_seen_scan_generation must not precede first_seen_scan_generation"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_PATH_KIND,
            "path": self.path,
            "first_seen_scan_generation": self.first_seen_scan_generation,
            "last_seen_scan_generation": self.last_seen_scan_generation,
        }


@dataclass(frozen=True)
class SourceVersion:
    """One content transition observed for a stable source identity."""

    version: int
    content_hash: str
    manifest_path: str
    observed_scan_generation: int

    def __post_init__(self) -> None:
        _positive_integer(self.version, "version")
        object.__setattr__(self, "content_hash", _content_hash(self.content_hash))
        object.__setattr__(self, "manifest_path", _manifest_path(self.manifest_path))
        _positive_integer(
            self.observed_scan_generation,
            "observed_scan_generation",
        )

    def as_dict(self, *, project_id: str, source_id: str) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_REGISTRY_KIND,
            "registry_version": SOURCE_REGISTRY_VERSION,
            "record_type": "version",
            "project_id": project_id,
            "source_id": source_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "manifest_path": self.manifest_path,
            "observed_scan_generation": self.observed_scan_generation,
        }


@dataclass(frozen=True)
class SourceRecord:
    """One stable source identity with known paths and content versions."""

    source_id: str
    current_path: str
    path_history: tuple[SourcePathRecord, ...]
    first_seen_scan_generation: int
    last_seen_scan_generation: int
    current_version: int | None = None
    versions: tuple[SourceVersion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _source_id(self.source_id))
        object.__setattr__(self, "current_path", _manifest_path(self.current_path))
        first = _positive_integer(
            self.first_seen_scan_generation,
            "first_seen_scan_generation",
        )
        last = _positive_integer(
            self.last_seen_scan_generation,
            "last_seen_scan_generation",
        )
        if last < first:
            raise SourceRegistryError(
                "last_seen_scan_generation must not precede first_seen_scan_generation"
            )

        history = tuple(self.path_history)
        if not history or not all(isinstance(item, SourcePathRecord) for item in history):
            raise SourceRegistryError(
                "path_history must contain at least one SourcePathRecord"
            )
        if tuple(
            sorted(
                history,
                key=lambda item: (item.first_seen_scan_generation, item.path),
            )
        ) != history:
            raise SourceRegistryError(
                "path_history must be sorted by first-seen generation and path"
            )
        paths = [item.path for item in history]
        if len(set(paths)) != len(paths):
            raise SourceRegistryConflictError("path_history contains duplicate paths")
        if self.current_path not in set(paths):
            raise SourceRegistryError("current_path must appear in path_history")
        if first != min(item.first_seen_scan_generation for item in history):
            raise SourceRegistryError(
                "source first_seen_scan_generation must match path history"
            )
        if last != max(item.last_seen_scan_generation for item in history):
            raise SourceRegistryError(
                "source last_seen_scan_generation must match path history"
            )
        history_by_path = {item.path: item for item in history}
        if history_by_path[self.current_path].last_seen_scan_generation != last:
            raise SourceRegistryError(
                "current_path must be the path observed at the source last-seen generation"
            )

        versions = tuple(self.versions)
        if not all(isinstance(item, SourceVersion) for item in versions):
            raise SourceRegistryError("versions must contain only SourceVersion objects")
        if [item.version for item in versions] != list(range(1, len(versions) + 1)):
            raise SourceRegistryError("source versions must be contiguous starting at 1")
        observed = [item.observed_scan_generation for item in versions]
        if observed != sorted(observed) or len(set(observed)) != len(observed):
            raise SourceRegistryError(
                "source version observation generations must be strictly increasing"
            )
        path_set = set(paths)
        for version in versions:
            if version.manifest_path not in path_set:
                raise SourceRegistryError(
                    "version manifest_path must appear in source path_history"
                )
            if version.observed_scan_generation < first:
                raise SourceRegistryError(
                    "version observation cannot precede source discovery"
                )
            if version.observed_scan_generation > last:
                raise SourceRegistryError(
                    "version observation cannot exceed source last-seen generation"
                )
            version_path = history_by_path[version.manifest_path]
            if not (
                version_path.first_seen_scan_generation
                <= version.observed_scan_generation
                <= version_path.last_seen_scan_generation
            ):
                raise SourceRegistryError(
                    "version observation must fall within its path history range"
                )
        expected_current = len(versions) if versions else None
        if self.current_version != expected_current:
            raise SourceRegistryError(
                "current_version must identify the latest contiguous source version"
            )
        object.__setattr__(self, "path_history", history)
        object.__setattr__(self, "versions", versions)

    @property
    def manifest_path(self) -> str:
        """D-01 compatibility alias for the current known path."""

        return self.current_path

    @property
    def current_content_hash(self) -> str | None:
        return self.versions[-1].content_hash if self.versions else None

    def source_row(self, *, project_id: str) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_REGISTRY_KIND,
            "registry_version": SOURCE_REGISTRY_VERSION,
            "record_type": "source",
            "project_id": project_id,
            "source_id": self.source_id,
            "current_path": self.current_path,
            "path_history": [item.as_dict() for item in self.path_history],
            "first_seen_scan_generation": self.first_seen_scan_generation,
            "last_seen_scan_generation": self.last_seen_scan_generation,
            "current_version": self.current_version,
        }

    def observe(
        self,
        *,
        manifest_path: str,
        content_hash: str,
        scan_generation: int,
    ) -> tuple[SourceRecord, bool]:
        """Observe the current path without performing relocation inference."""

        path = _manifest_path(manifest_path)
        digest = _content_hash(content_hash)
        generation = _positive_integer(scan_generation, "scan_generation")
        if path != self.current_path:
            raise SourceRegistryConflictError(
                "D-02 cannot change a source current_path without relocation recovery"
            )
        if generation < self.last_seen_scan_generation:
            raise SourceRegistryConflictError(
                "Manifest generation predates persisted source observations"
            )

        history = list(self.path_history)
        history_index = next(
            index for index, item in enumerate(history) if item.path == path
        )
        current_path_record = history[history_index]
        if generation > current_path_record.last_seen_scan_generation:
            history[history_index] = replace(
                current_path_record,
                last_seen_scan_generation=generation,
            )

        versions = list(self.versions)
        added_version = not versions or versions[-1].content_hash != digest
        if added_version:
            if versions and generation <= versions[-1].observed_scan_generation:
                raise SourceRegistryConflictError(
                    "a content change requires a newer Manifest generation"
                )
            versions.append(
                SourceVersion(
                    version=len(versions) + 1,
                    content_hash=digest,
                    manifest_path=path,
                    observed_scan_generation=generation,
                )
            )

        updated = SourceRecord(
            source_id=self.source_id,
            current_path=self.current_path,
            path_history=tuple(history),
            first_seen_scan_generation=self.first_seen_scan_generation,
            last_seen_scan_generation=max(self.last_seen_scan_generation, generation),
            current_version=len(versions) if versions else None,
            versions=tuple(versions),
        )
        return updated, added_version

    def relocate(self, recovered_path: str) -> SourceRecord:
        """Bind one verified path without changing source identity or versions."""

        path = _manifest_path(recovered_path)
        if path == self.current_path:
            return self
        generation = self.last_seen_scan_generation
        history = list(self.path_history)
        for index, item in enumerate(history):
            if item.path == path:
                history[index] = replace(
                    item,
                    last_seen_scan_generation=max(
                        item.last_seen_scan_generation,
                        generation,
                    ),
                )
                break
        else:
            history.append(
                SourcePathRecord(
                    path=path,
                    first_seen_scan_generation=generation,
                    last_seen_scan_generation=generation,
                )
            )
        history.sort(
            key=lambda item: (item.first_seen_scan_generation, item.path)
        )
        return SourceRecord(
            source_id=self.source_id,
            current_path=path,
            path_history=tuple(history),
            first_seen_scan_generation=self.first_seen_scan_generation,
            last_seen_scan_generation=self.last_seen_scan_generation,
            current_version=self.current_version,
            versions=self.versions,
        )


@dataclass(frozen=True)
class SourceRegistry:
    """A strictly validated project-local source identity/version registry."""

    project_id: str
    sources_file: Path
    records: tuple[SourceRecord, ...]
    loaded_registry_version: str = SOURCE_REGISTRY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise SourceRegistryError("project_id must be a non-empty string")
        if self.loaded_registry_version not in {
            LEGACY_SOURCE_REGISTRY_VERSION,
            SOURCE_REGISTRY_VERSION,
        }:
            raise SourceRegistryError("unsupported loaded source registry version")
        records = tuple(self.records)
        if not all(isinstance(record, SourceRecord) for record in records):
            raise SourceRegistryError("records must contain only SourceRecord objects")
        if tuple(sorted(records, key=lambda item: item.source_id)) != records:
            raise SourceRegistryError("source records must be sorted by source_id")
        source_ids = [record.source_id for record in records]
        if len(set(source_ids)) != len(source_ids):
            raise SourceRegistryConflictError(
                "source registry contains duplicate source_id values"
            )
        known_paths: list[str] = []
        for record in records:
            known_paths.extend(item.path for item in record.path_history)
        if len(set(known_paths)) != len(known_paths):
            raise SourceRegistryConflictError(
                "source registry contains a path assigned to multiple sources"
            )
        object.__setattr__(self, "sources_file", Path(self.sources_file))
        object.__setattr__(self, "records", records)

    @property
    def by_source_id(self) -> dict[str, SourceRecord]:
        return {record.source_id: record for record in self.records}

    @property
    def by_path(self) -> dict[str, SourceRecord]:
        return {
            path.path: record
            for record in self.records
            for path in record.path_history
        }

    @property
    def current_by_path(self) -> dict[str, SourceRecord]:
        return {record.current_path: record for record in self.records}

    @property
    def version_count(self) -> int:
        return sum(len(record.versions) for record in self.records)

    @property
    def max_scan_generation(self) -> int:
        return max((record.last_seen_scan_generation for record in self.records), default=0)

    def serialized_bytes(self) -> bytes:
        summary = {
            "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
            "kind": SOURCE_REGISTRY_KIND,
            "registry_version": SOURCE_REGISTRY_VERSION,
            "record_type": "summary",
            "project_id": self.project_id,
            "identity_strategy": SOURCE_ID_STRATEGY,
            "hash_algorithm": SOURCE_VERSION_HASH_ALGORITHM,
            "source_count": len(self.records),
            "version_count": self.version_count,
        }
        return b"".join(
            [
                _canonical_json_line(summary),
                *(
                    _canonical_json_line(record.source_row(project_id=self.project_id))
                    for record in self.records
                ),
                *(
                    _canonical_json_line(
                        version.as_dict(
                            project_id=self.project_id,
                            source_id=record.source_id,
                        )
                    )
                    for record in self.records
                    for version in record.versions
                ),
            ]
        )


@dataclass(frozen=True)
class SourceRegistrySyncResult:
    """Auditable outcome of one identity and source-version synchronization."""

    project_id: str
    project_root: Path
    manifest_file: Path
    sources_file: Path
    scan_generation: int
    source_count: int
    version_count: int
    manifest_file_count: int
    assigned_count: int
    existing_count: int
    versions_added_count: int
    upgraded_registry: bool
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
            "version_count": self.version_count,
            "manifest_file_count": self.manifest_file_count,
            "assigned_count": self.assigned_count,
            "existing_count": self.existing_count,
            "versions_added_count": self.versions_added_count,
            "upgraded_registry": self.upgraded_registry,
            "wrote_registry": self.wrote_registry,
        }


@dataclass(frozen=True)
class SourceRegistryRelocationResult:
    """Transactional source-registry update for one verified relocation."""

    project_id: str
    project_root: Path
    sources_file: Path
    previous_path: str
    recovered_path: str
    record: SourceRecord
    wrote_registry: bool
    already_current: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "sources_file": str(self.sources_file),
            "source_id": self.record.source_id,
            "previous_path": self.previous_path,
            "recovered_path": self.recovered_path,
            "current_path": self.record.current_path,
            "current_version": self.record.current_version,
            "content_hash": self.record.current_content_hash,
            "wrote_registry": self.wrote_registry,
            "already_current": self.already_current,
        }


@dataclass(frozen=True)
class SourceHistoryResult:
    """Deterministic source/version history without reading source bytes."""

    project_id: str
    sources_file: Path
    record: SourceRecord

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "sources_file": str(self.sources_file),
            "registry_version": SOURCE_REGISTRY_VERSION,
            "hash_algorithm": SOURCE_VERSION_HASH_ALGORITHM,
            "source": {
                "source_id": self.record.source_id,
                "current_path": self.record.current_path,
                "path_history": [item.as_dict() for item in self.record.path_history],
                "first_seen_scan_generation": self.record.first_seen_scan_generation,
                "last_seen_scan_generation": self.record.last_seen_scan_generation,
                "current_version": self.record.current_version,
                "versions": [
                    {
                        "version": item.version,
                        "content_hash": item.content_hash,
                        "manifest_path": item.manifest_path,
                        "observed_scan_generation": item.observed_scan_generation,
                    }
                    for item in self.record.versions
                ],
            },
        }


def _registry_error(
    path: Path,
    message: str,
    line_number: int | None = None,
) -> SourceRegistryError:
    location = f"{path}:{line_number}" if line_number is not None else str(path)
    return SourceRegistryError(f"invalid source registry at {location}: {message}")


def _validate_schema_record(
    record: object,
    *,
    path: Path,
    line_number: int,
    expected_fields: set[str],
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise _registry_error(path, "each JSONL line must contain an object", line_number)
    if set(record) != expected_fields:
        raise _registry_error(
            path,
            "row must contain exactly the expected Schema v1 fields",
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
    _normalize_json_value(record)
    return record


def _validate_common_row(
    record: object,
    *,
    path: Path,
    line_number: int,
    project_id: str,
    expected_fields: set[str],
    record_type: str,
    registry_version: str,
) -> dict[str, Any]:
    row = _validate_schema_record(
        record,
        path=path,
        line_number=line_number,
        expected_fields=expected_fields,
    )
    if row.get("kind") != SOURCE_REGISTRY_KIND:
        raise _registry_error(
            path,
            f"unexpected kind {row.get('kind')!r}",
            line_number,
        )
    if row.get("registry_version") != registry_version:
        raise _registry_error(
            path,
            f"unsupported registry_version {row.get('registry_version')!r}",
            line_number,
        )
    if row.get("record_type") != record_type:
        raise _registry_error(
            path,
            f"expected record_type {record_type!r}",
            line_number,
        )
    if row.get("project_id") != project_id:
        raise _registry_error(
            path,
            "project_id does not match the registered project",
            line_number,
        )
    return row


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


def _load_registry_lines(
    path: Path,
    *,
    trusted_root: Path,
) -> list[object]:
    try:
        observation = read_stable_regular_file(
            trusted_root,
            path,
            reject_redirection=True,
            capture_bytes=True,
        )
    except StableFileMissingError:
        raise
    except StableFileAccessError as exc:
        raise SourceRegistryError(
            "source registry must remain a stable, non-redirected regular file "
            f"inside the project machine-state root: {path}: {exc}"
        ) from exc
    if observation.data is None:
        raise SourceRegistryError(f"source registry read returned no bytes: {path}")
    try:
        text = observation.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceRegistryError(f"source registry must be UTF-8: {path}") from exc
    raw_lines = text.splitlines()
    if not raw_lines:
        raise _registry_error(path, "the registry is empty")
    return [
        _decode_registry_line(line, path=path, line_number=line_number)
        for line_number, line in enumerate(raw_lines, start=1)
    ]


def _load_v1_registry(
    rows: list[object],
    *,
    path: Path,
    project_id: str,
) -> SourceRegistry:
    summary = _validate_common_row(
        rows[0],
        path=path,
        line_number=1,
        project_id=project_id,
        expected_fields=_V1_SUMMARY_FIELDS,
        record_type="summary",
        registry_version=LEGACY_SOURCE_REGISTRY_VERSION,
    )
    if summary.get("identity_strategy") != SOURCE_ID_STRATEGY:
        raise _registry_error(path, "unsupported identity_strategy", 1)
    source_count = summary.get("source_count")
    if not _is_integer(source_count) or source_count < 0:
        raise _registry_error(path, "source_count must be a non-negative integer", 1)

    parsed: list[tuple[str, SourceRecord]] = []
    for line_number, raw in enumerate(rows[1:], start=2):
        row = _validate_common_row(
            raw,
            path=path,
            line_number=line_number,
            project_id=project_id,
            expected_fields=_V1_SOURCE_FIELDS,
            record_type="source",
            registry_version=LEGACY_SOURCE_REGISTRY_VERSION,
        )
        try:
            manifest_path = _manifest_path(row.get("manifest_path"))
            generation = _positive_integer(
                row.get("first_seen_scan_generation"),
                "first_seen_scan_generation",
            )
            parsed.append(
                (
                    manifest_path,
                    SourceRecord(
                        source_id=row.get("source_id"),
                        current_path=manifest_path,
                        path_history=(
                            SourcePathRecord(
                                path=manifest_path,
                                first_seen_scan_generation=generation,
                                last_seen_scan_generation=generation,
                            ),
                        ),
                        first_seen_scan_generation=generation,
                        last_seen_scan_generation=generation,
                    ),
                )
            )
        except SourceRegistryError as exc:
            raise _registry_error(path, str(exc), line_number) from exc
    if source_count != len(parsed):
        raise _registry_error(
            path,
            "source_count does not match the number of source rows",
            1,
        )
    if [item[0] for item in parsed] != sorted(item[0] for item in parsed):
        raise _registry_error(path, "v1 source rows must be sorted by manifest_path")
    try:
        records = tuple(
            sorted((item[1] for item in parsed), key=lambda item: item.source_id)
        )
        return SourceRegistry(
            project_id=project_id,
            sources_file=path,
            records=records,
            loaded_registry_version=LEGACY_SOURCE_REGISTRY_VERSION,
        )
    except SourceRegistryError as exc:
        raise _registry_error(path, str(exc)) from exc


def _parse_source_path(
    value: object,
    *,
    registry_path: Path,
    line_number: int,
) -> SourcePathRecord:
    row = _validate_schema_record(
        value,
        path=registry_path,
        line_number=line_number,
        expected_fields=_SOURCE_PATH_FIELDS,
    )
    if row.get("kind") != SOURCE_PATH_KIND:
        raise _registry_error(
            registry_path,
            f"unexpected source path kind {row.get('kind')!r}",
            line_number,
        )
    try:
        return SourcePathRecord(
            path=row.get("path"),
            first_seen_scan_generation=row.get("first_seen_scan_generation"),
            last_seen_scan_generation=row.get("last_seen_scan_generation"),
        )
    except SourceRegistryError as exc:
        raise _registry_error(registry_path, str(exc), line_number) from exc


def _load_v2_registry(
    rows: list[object],
    *,
    path: Path,
    project_id: str,
) -> SourceRegistry:
    summary = _validate_common_row(
        rows[0],
        path=path,
        line_number=1,
        project_id=project_id,
        expected_fields=_V2_SUMMARY_FIELDS,
        record_type="summary",
        registry_version=SOURCE_REGISTRY_VERSION,
    )
    if summary.get("identity_strategy") != SOURCE_ID_STRATEGY:
        raise _registry_error(path, "unsupported identity_strategy", 1)
    if summary.get("hash_algorithm") != SOURCE_VERSION_HASH_ALGORITHM:
        raise _registry_error(path, "unsupported hash_algorithm", 1)
    source_count = summary.get("source_count")
    version_count = summary.get("version_count")
    if not _is_integer(source_count) or source_count < 0:
        raise _registry_error(path, "source_count must be a non-negative integer", 1)
    if not _is_integer(version_count) or version_count < 0:
        raise _registry_error(path, "version_count must be a non-negative integer", 1)

    source_rows: list[tuple[int, dict[str, Any]]] = []
    version_rows: list[tuple[int, dict[str, Any]]] = []
    saw_version = False
    for line_number, raw in enumerate(rows[1:], start=2):
        if not isinstance(raw, dict):
            raise _registry_error(path, "each JSONL line must contain an object", line_number)
        record_type = raw.get("record_type")
        if record_type == "source":
            if saw_version:
                raise _registry_error(path, "source rows must precede version rows")
            source_rows.append(
                (
                    line_number,
                    _validate_common_row(
                        raw,
                        path=path,
                        line_number=line_number,
                        project_id=project_id,
                        expected_fields=_V2_SOURCE_FIELDS,
                        record_type="source",
                        registry_version=SOURCE_REGISTRY_VERSION,
                    ),
                )
            )
        elif record_type == "version":
            saw_version = True
            version_rows.append(
                (
                    line_number,
                    _validate_common_row(
                        raw,
                        path=path,
                        line_number=line_number,
                        project_id=project_id,
                        expected_fields=_V2_VERSION_FIELDS,
                        record_type="version",
                        registry_version=SOURCE_REGISTRY_VERSION,
                    ),
                )
            )
        else:
            raise _registry_error(path, "record_type must be source or version", line_number)
    if source_count != len(source_rows):
        raise _registry_error(path, "source_count does not match source rows", 1)
    if version_count != len(version_rows):
        raise _registry_error(path, "version_count does not match version rows", 1)

    source_data: dict[
        str,
        tuple[int, dict[str, Any], tuple[SourcePathRecord, ...]],
    ] = {}
    source_order: list[str] = []
    for line_number, row in source_rows:
        try:
            source_id = _source_id(row.get("source_id"))
            history_raw = row.get("path_history")
            if not isinstance(history_raw, list):
                raise SourceRegistryError("path_history must be a list")
            history = tuple(
                _parse_source_path(
                    item,
                    registry_path=path,
                    line_number=line_number,
                )
                for item in history_raw
            )
        except SourceRegistryError as exc:
            raise _registry_error(path, str(exc), line_number) from exc
        if source_id in source_data:
            raise _registry_error(path, "duplicate source_id", line_number)
        source_data[source_id] = (line_number, row, history)
        source_order.append(source_id)
    if source_order != sorted(source_order):
        raise _registry_error(path, "source rows must be sorted by source_id")

    versions_by_source: dict[str, list[SourceVersion]] = {
        source_id: [] for source_id in source_data
    }
    version_order: list[tuple[str, int]] = []
    for line_number, row in version_rows:
        try:
            source_id = _source_id(row.get("source_id"))
            version = SourceVersion(
                version=row.get("version"),
                content_hash=row.get("content_hash"),
                manifest_path=row.get("manifest_path"),
                observed_scan_generation=row.get("observed_scan_generation"),
            )
        except SourceRegistryError as exc:
            raise _registry_error(path, str(exc), line_number) from exc
        if source_id not in versions_by_source:
            raise _registry_error(path, "version references an unknown source_id", line_number)
        versions_by_source[source_id].append(version)
        version_order.append((source_id, version.version))
    if version_order != sorted(version_order):
        raise _registry_error(path, "version rows must be sorted by source_id and version")

    records: list[SourceRecord] = []
    for source_id in source_order:
        line_number, row, history = source_data[source_id]
        current_version = row.get("current_version")
        if current_version is not None and not _is_integer(current_version):
            raise _registry_error(path, "current_version must be an integer or null", line_number)
        try:
            records.append(
                SourceRecord(
                    source_id=source_id,
                    current_path=row.get("current_path"),
                    path_history=history,
                    first_seen_scan_generation=row.get(
                        "first_seen_scan_generation"
                    ),
                    last_seen_scan_generation=row.get("last_seen_scan_generation"),
                    current_version=current_version,
                    versions=tuple(versions_by_source[source_id]),
                )
            )
        except SourceRegistryError as exc:
            raise _registry_error(path, str(exc), line_number) from exc
    try:
        return SourceRegistry(
            project_id=project_id,
            sources_file=path,
            records=tuple(records),
        )
    except SourceRegistryError as exc:
        raise _registry_error(path, str(exc)) from exc


def _load_source_registry_file(
    sources_file: Path,
    *,
    trusted_root: Path,
    project_id: str,
    missing_ok: bool,
) -> SourceRegistry:
    path = Path(os.path.abspath(os.fspath(Path(sources_file).expanduser())))
    try:
        rows = _load_registry_lines(path, trusted_root=trusted_root)
    except StableFileMissingError as exc:
        if missing_ok:
            return SourceRegistry(
                project_id=project_id,
                sources_file=path,
                records=(),
            )
        raise SourceRegistryError(f"source registry does not exist: {path}") from exc
    if not isinstance(rows[0], dict):
        raise _registry_error(path, "summary row must contain an object", 1)
    registry_version = rows[0].get("registry_version")
    if registry_version == LEGACY_SOURCE_REGISTRY_VERSION:
        return _load_v1_registry(rows, path=path, project_id=project_id)
    if registry_version == SOURCE_REGISTRY_VERSION:
        return _load_v2_registry(rows, path=path, project_id=project_id)
    raise _registry_error(
        path,
        f"unsupported registry_version {registry_version!r}",
        1,
    )


def load_source_registry(
    workspace_root: str | Path,
    project_id: str,
) -> SourceRegistry:
    """Load existing source identity/version state without modifying it."""

    registration = load_registered_project(workspace_root, project_id)
    return _load_source_registry_file(
        registration.layout.sources_file,
        trusted_root=registration.layout.machine_root,
        project_id=registration.project_id,
        missing_ok=False,
    )


def get_source_history(
    workspace_root: str | Path,
    project_id: str,
    source_id: str,
) -> SourceHistoryResult:
    """Return one source history without opening source-project bytes."""

    registry = load_source_registry(workspace_root, project_id)
    normalized_id = _source_id(source_id)
    try:
        record = registry.by_source_id[normalized_id]
    except KeyError as exc:
        raise SourceRegistryError(
            f"source_id is not registered for project {project_id}: {normalized_id}"
        ) from exc
    return SourceHistoryResult(
        project_id=registry.project_id,
        sources_file=registry.sources_file,
        record=record,
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
        except PermissionError as exc:
            # Windows may report access denied while the previous owner is
            # deleting the just-released lock rather than FileExistsError.
            if os.name == "nt" and time.monotonic() < deadline:
                time.sleep(_LOCK_RETRY_SECONDS)
                continue
            if os.name == "nt":
                raise SourceRegistryLockError(
                    f"timed out waiting for source registry lock: {lock_file}"
                ) from exc
            raise SourceRegistryLockError(
                f"could not create source registry lock {lock_file}: {exc}"
            ) from exc
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


def _write_registry_atomic(
    registry: SourceRegistry,
    *,
    trusted_root: Path,
) -> bool:
    path = registry.sources_file
    payload = registry.serialized_bytes()
    try:
        return write_atomic_stable_file(trusted_root, path, payload)
    except StableFileAccessError as exc:
        raise SourceRegistryError(
            f"could not securely write source registry {path}: {exc}"
        ) from exc


@contextmanager
def _verified_relocation_candidate(
    project_root: Path,
    recovered_path: str,
    expected_content_hash: str,
) -> Iterator[StableFileLease]:
    relative_path = _manifest_path(recovered_path)
    expected = _content_hash(expected_content_hash)
    root = Path(os.path.abspath(os.fspath(project_root)))
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        with lease_stable_regular_file(
            root,
            candidate,
            reject_redirection=True,
        ) as lease:
            if lease.content_sha256 != expected:
                raise SourceRegistryConflictError(
                    "recovered source bytes do not match the recorded current version: "
                    f"expected {expected}; observed {lease.content_sha256}"
                )
            yield lease
    except StableFileAccessError as exc:
        raise SourceRegistryConflictError(
            f"could not securely verify recovered source path {relative_path}: {exc}"
        ) from exc


def record_source_relocation(
    workspace_root: str | Path,
    project_id: str,
    *,
    source_id: str,
    recovered_path: str,
    expected_content_hash: str,
    expected_current_path: str | None = None,
    expected_current_version: int | None = None,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> SourceRegistryRelocationResult:
    """Verify and persist one unambiguous relocation under the registry lock."""

    registration = load_registered_project(workspace_root, project_id)
    normalized_id = _source_id(source_id)
    normalized_path = _manifest_path(recovered_path)
    expected_hash = _content_hash(expected_content_hash)
    stale_path = (
        _manifest_path(expected_current_path)
        if expected_current_path is not None
        else None
    )
    stale_version = (
        _positive_integer(expected_current_version, "expected_current_version")
        if expected_current_version is not None
        else None
    )
    sources_file = registration.layout.sources_file
    lock_file = sources_file.with_name(f"{sources_file.name}.lock")
    sources_file.parent.mkdir(parents=True, exist_ok=True)

    with _exclusive_registry_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        registry = _load_source_registry_file(
            sources_file,
            trusted_root=registration.layout.machine_root,
            project_id=registration.project_id,
            missing_ok=False,
        )
        try:
            record = registry.by_source_id[normalized_id]
        except KeyError as exc:
            raise SourceRegistryConflictError(
                "source_id is not registered for project "
                f"{registration.project_id}: {normalized_id}"
            ) from exc
        if record.current_content_hash is None or record.current_version is None:
            raise SourceRegistryConflictError(
                f"source {normalized_id} has no recorded content version"
            )
        if record.current_content_hash != expected_hash:
            raise SourceRegistryConflictError(
                "source content version changed during relocation recovery"
            )
        if stale_version is not None and record.current_version != stale_version:
            raise SourceRegistryConflictError(
                "source version changed during relocation recovery"
            )
        if (
            stale_path is not None
            and record.current_path not in {stale_path, normalized_path}
        ):
            raise SourceRegistryConflictError(
                "source current path changed during relocation recovery: "
                f"expected {stale_path}; observed {record.current_path}"
            )

        previous_path = record.current_path
        with _verified_relocation_candidate(
            registration.project_root,
            normalized_path,
            expected_hash,
        ) as candidate_lease:
            if record.current_path == normalized_path:
                return SourceRegistryRelocationResult(
                    project_id=registration.project_id,
                    project_root=registration.project_root,
                    sources_file=sources_file,
                    previous_path=previous_path,
                    recovered_path=normalized_path,
                    record=record,
                    wrote_registry=False,
                    already_current=True,
                )

            path_owner = registry.by_path.get(normalized_path)
            if path_owner is not None and path_owner.source_id != normalized_id:
                raise SourceRegistryConflictError(
                    "recovered path is already assigned to another source: "
                    f"{normalized_path} -> {path_owner.source_id}"
                )
            updated_record = record.relocate(normalized_path)
            records = tuple(
                sorted(
                    (
                        updated_record if item.source_id == normalized_id else item
                        for item in registry.records
                    ),
                    key=lambda item: item.source_id,
                )
            )
            updated_registry = SourceRegistry(
                project_id=registration.project_id,
                sources_file=sources_file,
                records=records,
            )
            candidate_lease.revalidate()
            wrote_registry = _write_registry_atomic(
                updated_registry,
                trusted_root=registration.layout.machine_root,
            )
            try:
                candidate_lease.revalidate()
            except StableFileAccessError as exc:
                try:
                    _write_registry_atomic(
                        registry,
                        trusted_root=registration.layout.machine_root,
                    )
                except SourceRegistryError as rollback_exc:
                    raise SourceRegistryConflictError(
                        "recovered source changed while the relocation binding was "
                        "being committed, and the previous source registry could not "
                        f"be restored: {rollback_exc}"
                    ) from rollback_exc
                raise SourceRegistryConflictError(
                    "recovered source changed while the relocation binding was being "
                    "committed; the previous source registry was restored"
                ) from exc

    return SourceRegistryRelocationResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        sources_file=sources_file,
        previous_path=previous_path,
        recovered_path=normalized_path,
        record=updated_record,
        wrote_registry=wrote_registry,
        already_current=False,
    )


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


def _manifest_observations(manifest: ProjectManifest) -> dict[str, str]:
    observations: dict[str, str] = {}
    for file_record in manifest.file_records:
        path = _manifest_path(file_record.get("path"))
        digest = _content_hash(file_record.get("content_sha256"))
        if path in observations:
            raise SourceRegistryConflictError(
                f"Manifest contains duplicate regular-file path: {path}"
            )
        observations[path] = digest
    return observations


def sync_source_registry(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> SourceRegistrySyncResult:
    """Synchronize stable identities and content versions from the Manifest.

    D-02 matches only a source's current known path. It deliberately does not
    infer movement from hash matches; D-05 adds that recovery policy.
    """

    registration, manifest = _load_current_manifest(workspace_root, project_id)
    observations = _manifest_observations(manifest)
    sources_file = registration.layout.sources_file
    lock_file = sources_file.with_name(f"{sources_file.name}.lock")
    sources_file.parent.mkdir(parents=True, exist_ok=True)

    with _exclusive_registry_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        registry = _load_source_registry_file(
            sources_file,
            trusted_root=registration.layout.machine_root,
            project_id=registration.project_id,
            missing_ok=True,
        )
        if registry.max_scan_generation > manifest.scan_generation:
            raise SourceRegistryConflictError(
                "current Manifest generation predates persisted source observations"
            )

        records_by_id = dict(registry.by_source_id)
        current_by_path = registry.current_by_path
        existing_ids = set(records_by_id)
        assigned_count = 0
        versions_added_count = 0
        for manifest_path, digest in sorted(observations.items()):
            record = current_by_path.get(manifest_path)
            if record is None:
                source_id = _new_source_id(existing_ids)
                existing_ids.add(source_id)
                record = SourceRecord(
                    source_id=source_id,
                    current_path=manifest_path,
                    path_history=(
                        SourcePathRecord(
                            path=manifest_path,
                            first_seen_scan_generation=manifest.scan_generation,
                            last_seen_scan_generation=manifest.scan_generation,
                        ),
                    ),
                    first_seen_scan_generation=manifest.scan_generation,
                    last_seen_scan_generation=manifest.scan_generation,
                )
                assigned_count += 1
            updated, added_version = record.observe(
                manifest_path=manifest_path,
                content_hash=digest,
                scan_generation=manifest.scan_generation,
            )
            records_by_id[updated.source_id] = updated
            current_by_path[manifest_path] = updated
            versions_added_count += int(added_version)

        updated_registry = SourceRegistry(
            project_id=registration.project_id,
            sources_file=sources_file,
            records=tuple(
                sorted(records_by_id.values(), key=lambda item: item.source_id)
            ),
        )
        wrote_registry = _write_registry_atomic(
            updated_registry,
            trusted_root=registration.layout.machine_root,
        )

    return SourceRegistrySyncResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        manifest_file=manifest.manifest_file,
        sources_file=sources_file,
        scan_generation=manifest.scan_generation,
        source_count=len(updated_registry.records),
        version_count=updated_registry.version_count,
        manifest_file_count=len(manifest.file_records),
        assigned_count=assigned_count,
        existing_count=len(manifest.file_records) - assigned_count,
        versions_added_count=versions_added_count,
        upgraded_registry=(
            registry.loaded_registry_version == LEGACY_SOURCE_REGISTRY_VERSION
        ),
        wrote_registry=wrote_registry,
    )
