#!/usr/bin/env python3
"""Host-neutral append-only file-change events and dirty-path projection.

H-04 deliberately records untrusted host signals only. Event submission never
scans the source project, mutates curated knowledge, or performs reconciliation.
The append-only ledger is authoritative; ``indexes/dirty-paths.json`` is a
rebuildable deterministic projection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping, cast
import unicodedata
import uuid

if __package__:
    from .project_layout import LayoutError
    from .project_registry import load_registered_project
else:
    from project_layout import LayoutError  # type: ignore[no-redef]
    from project_registry import (  # type: ignore[no-redef]
        load_registered_project,
    )


HOST_EVENT_SCHEMA_VERSION = 1
HOST_EVENT_KIND = "llmwiki-host-event"
HOST_EVENT_VERSION = "host-event-v1"
HOST_EVENT_RECORD_TYPE = "dirty-path-event"
HOST_EVENT_SUBMIT_RESULT_KIND = "llmwiki-host-event-submit-result"
DIRTY_PATH_QUEUE_KIND = "llmwiki-dirty-path-queue"
DIRTY_PATH_QUEUE_VERSION = "dirty-paths-v1"
DIRTY_PATH_QUEUE_RESULT_KIND = "llmwiki-dirty-path-queue-result"
HOST_EVENT_LEDGER_RELATIVE_PATH = "events.jsonl"
DIRTY_PATH_QUEUE_RELATIVE_PATH = "indexes/dirty-paths.json"

HostEventOperation = Literal[
    "created",
    "modified",
    "deleted",
    "moved",
    "unknown",
]
HostEventDisposition = Literal["appended", "duplicate"]

HOST_EVENT_OPERATIONS: tuple[HostEventOperation, ...] = (
    "created",
    "modified",
    "deleted",
    "moved",
    "unknown",
)

_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRODUCER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_PROTECTED_TOP_LEVEL = frozenset({".git", ".hg", ".svn", ".llmwiki"})
_EVENT_FIELDS = {
    "schema_version",
    "kind",
    "event_version",
    "record_type",
    "project_id",
    "sequence",
    "event_id",
    "producer",
    "occurred_at",
    "ingested_at",
    "operation",
    "paths",
}
_QUEUE_FIELDS = {
    "schema_version",
    "kind",
    "queue_version",
    "project_id",
    "ledger_event_count",
    "ledger_last_sequence",
    "ledger_sha256",
    "dirty_path_count",
    "dirty_paths",
}
_QUEUE_ENTRY_FIELDS = {
    "path",
    "first_sequence",
    "last_sequence",
    "event_count",
    "operations",
    "producers",
}
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.02
_MAX_EVENT_PATHS = 256
_MAX_PATH_UTF8_BYTES = 4096
_MAX_EVENT_LINE_BYTES = 1024 * 1024


class HostEventError(LayoutError):
    """Base error for host-event state and submissions."""

    reason_code = "host-event-invalid"


class HostEventConflictError(HostEventError):
    """Raised when an event ID is reused for a different canonical payload."""

    reason_code = "host-event-conflict"


class HostEventStateError(HostEventError):
    """Raised when persisted ledger or projection state cannot be trusted."""

    reason_code = "host-event-state-invalid"


class HostEventLockError(HostEventError):
    """Raised when the event writer lock cannot be acquired or released."""

    reason_code = "host-event-lock-failed"


class DirtyPathQueueError(HostEventError):
    """Raised when the derived dirty-path projection cannot be maintained."""

    reason_code = "dirty-path-queue-invalid"


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_integer(value: object, *, label: str) -> int:
    if not _is_integer(value) or value < 1:
        raise HostEventStateError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    if not _is_integer(value) or value < 0:
        raise HostEventStateError(f"{label} must be a non-negative integer")
    return value


def _exact_mapping(
    value: object,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise HostEventStateError(f"{label} does not match the closed Schema v1 contract")
    return value


def _canonical_json(value: object, *, error_type: type[HostEventError]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise error_type("host-event state contains unstable JSON data") from exc


def _canonical_json_line(value: object) -> bytes:
    return _canonical_json(value, error_type=HostEventStateError) + b"\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostEventStateError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise HostEventStateError(f"non-finite JSON constant {value!r} is not supported")


def _parse_json_bytes(payload: bytes, *, label: str) -> Any:
    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise HostEventStateError(f"{label} must be strict UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise HostEventStateError(f"invalid JSON in {label}: {exc}") from exc


def _schema_version(value: Mapping[str, Any], *, label: str) -> int:
    if "schema_version" not in value:
        raise HostEventStateError(
            f"{label} is legacy v0; no H-04 compatibility reader is available"
        )
    version = value["schema_version"]
    if not _is_integer(version) or version < 1:
        raise HostEventStateError(f"{label} schema_version must be a positive integer")
    if version > HOST_EVENT_SCHEMA_VERSION:
        raise HostEventStateError(
            f"{label} schema_version {version} is newer than supported version "
            f"{HOST_EVENT_SCHEMA_VERSION}"
        )
    return version


def _event_id(value: object) -> str:
    if not isinstance(value, str) or _EVENT_ID_PATTERN.fullmatch(value) is None:
        raise HostEventError(
            "event_id must be 1-128 ASCII characters using letters, digits, "
            "'.', '_', ':', or '-', and must start with a letter or digit"
        )
    return value


def _producer(value: object) -> str:
    if (
        not isinstance(value, str)
        or _PRODUCER_PATTERN.fullmatch(value) is None
        or value.endswith("-")
        or "--" in value
    ):
        raise HostEventError(
            "producer must be 1-64 lowercase kebab-case characters"
        )
    return value


def _operation(value: object) -> HostEventOperation:
    if not isinstance(value, str) or value not in HOST_EVENT_OPERATIONS:
        raise HostEventError(
            "operation must be one of: " + ", ".join(HOST_EVENT_OPERATIONS)
        )
    return cast(HostEventOperation, value)


def normalize_dirty_path(value: str | Path) -> str:
    """Return one safe, normalized project-relative POSIX dirty path."""

    if not isinstance(value, (str, Path)):
        raise HostEventError("dirty path must be text or a Path")
    raw = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise HostEventError("dirty path contains a control character")
    normalized = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    if not normalized or normalized in {".", "/"}:
        raise HostEventError("dirty path must identify an entry below the project root")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
    ):
        raise HostEventError(f"dirty path must be project-relative: {raw!r}")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    normalized = normalized.rstrip("/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or path.as_posix() != normalized
        or any(segment in {"", ".", ".."} for segment in path.parts)
    ):
        raise HostEventError(
            f"dirty path cannot contain empty, '.', or '..' segments: {raw!r}"
        )
    if path.parts[0].lower() in _PROTECTED_TOP_LEVEL:
        raise HostEventError(
            f"dirty path targets protected project state: {path.parts[0]!r}"
        )
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise HostEventError("dirty path must be strict UTF-8") from exc
    if len(encoded) > _MAX_PATH_UTF8_BYTES:
        raise HostEventError(
            f"dirty path exceeds the {_MAX_PATH_UTF8_BYTES}-byte UTF-8 limit"
        )
    return normalized


def _paths(values: Iterable[str | Path] | str | Path) -> tuple[str, ...]:
    raw_values: Iterable[str | Path]
    if isinstance(values, (str, Path)):
        raw_values = (values,)
    else:
        raw_values = values
    try:
        normalized = tuple(sorted({normalize_dirty_path(item) for item in raw_values}))
    except TypeError as exc:
        raise HostEventError("paths must be an iterable of text paths") from exc
    if not normalized:
        raise HostEventError("at least one dirty path is required")
    if len(normalized) > _MAX_EVENT_PATHS:
        raise HostEventError(
            f"one event may contain at most {_MAX_EVENT_PATHS} unique dirty paths"
        )
    return normalized


def _timestamp(value: str | datetime, *, label: str) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise HostEventError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise HostEventError(f"{label} must be an ISO-8601 timestamp")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise HostEventError(f"{label} must include a timezone offset")
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _clock_timestamp(clock: Callable[[], datetime] | None) -> str:
    if clock is None:
        return _timestamp(datetime.now(timezone.utc), label="event clock")
    moment = clock()
    if not isinstance(moment, datetime):
        raise HostEventError("event clock must return a datetime")
    return _timestamp(moment, label="event clock")


@dataclass(frozen=True)
class HostEventDraft:
    """Validated caller-controlled event fields used for idempotency."""

    event_id: str
    producer: str
    occurred_at: str
    operation: HostEventOperation
    paths: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        producer: str,
        occurred_at: str | datetime,
        operation: str,
        paths: Iterable[str | Path] | str | Path,
    ) -> HostEventDraft:
        return cls(
            event_id=_event_id(event_id),
            producer=_producer(producer),
            occurred_at=_timestamp(occurred_at, label="occurred_at"),
            operation=_operation(operation),
            paths=_paths(paths),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "producer": self.producer,
            "occurred_at": self.occurred_at,
            "operation": self.operation,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class HostEventRecord:
    """One authoritative append-only host-event ledger record."""

    project_id: str
    sequence: int
    event_id: str
    producer: str
    occurred_at: str
    ingested_at: str
    operation: HostEventOperation
    paths: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> HostEventRecord:
        record = _exact_mapping(value, _EVENT_FIELDS, label="host event record")
        _schema_version(record, label="host event record")
        if record["kind"] != HOST_EVENT_KIND:
            raise HostEventStateError(
                f"unexpected host event kind {record['kind']!r}"
            )
        if record["event_version"] != HOST_EVENT_VERSION:
            raise HostEventStateError(
                f"unexpected host event version {record['event_version']!r}"
            )
        if record["record_type"] != HOST_EVENT_RECORD_TYPE:
            raise HostEventStateError(
                f"unexpected host event record_type {record['record_type']!r}"
            )
        project_id = record["project_id"]
        if not isinstance(project_id, str) or not project_id:
            raise HostEventStateError("host event project_id must be non-empty text")
        try:
            draft = HostEventDraft.create(
                event_id=record["event_id"],
                producer=record["producer"],
                occurred_at=record["occurred_at"],
                operation=record["operation"],
                paths=record["paths"],
            )
            ingested_at = _timestamp(record["ingested_at"], label="ingested_at")
        except HostEventError as exc:
            raise HostEventStateError(str(exc)) from exc
        if (
            not isinstance(record["occurred_at"], str)
            or draft.occurred_at != record["occurred_at"]
        ):
            raise HostEventStateError("occurred_at must use canonical UTC form")
        if (
            not isinstance(record["ingested_at"], str)
            or ingested_at != record["ingested_at"]
        ):
            raise HostEventStateError("ingested_at must use canonical UTC form")
        if not isinstance(record["paths"], list) or list(draft.paths) != record["paths"]:
            raise HostEventStateError("event paths must be sorted unique canonical paths")
        return cls(
            project_id=project_id,
            sequence=_positive_integer(record["sequence"], label="event sequence"),
            event_id=draft.event_id,
            producer=draft.producer,
            occurred_at=draft.occurred_at,
            ingested_at=ingested_at,
            operation=draft.operation,
            paths=draft.paths,
        )

    @classmethod
    def from_draft(
        cls,
        project_id: str,
        sequence: int,
        draft: HostEventDraft,
        *,
        ingested_at: str,
    ) -> HostEventRecord:
        return cls(
            project_id=project_id,
            sequence=_positive_integer(sequence, label="event sequence"),
            event_id=draft.event_id,
            producer=draft.producer,
            occurred_at=draft.occurred_at,
            ingested_at=_timestamp(ingested_at, label="ingested_at"),
            operation=draft.operation,
            paths=draft.paths,
        )

    @property
    def draft(self) -> HostEventDraft:
        return HostEventDraft(
            event_id=self.event_id,
            producer=self.producer,
            occurred_at=self.occurred_at,
            operation=self.operation,
            paths=self.paths,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HOST_EVENT_SCHEMA_VERSION,
            "kind": HOST_EVENT_KIND,
            "event_version": HOST_EVENT_VERSION,
            "record_type": HOST_EVENT_RECORD_TYPE,
            "project_id": self.project_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "producer": self.producer,
            "occurred_at": self.occurred_at,
            "ingested_at": self.ingested_at,
            "operation": self.operation,
            "paths": list(self.paths),
        }

    def serialized_line(self) -> bytes:
        return _canonical_json_line(self.as_dict())


@dataclass(frozen=True)
class HostEventLedgerSnapshot:
    """Validated immutable snapshot of the authoritative event ledger."""

    project_id: str
    events: tuple[HostEventRecord, ...]
    ledger_sha256: str

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def last_sequence(self) -> int:
        return self.events[-1].sequence if self.events else 0

    @property
    def serialized_bytes(self) -> bytes:
        return b"".join(event.serialized_line() for event in self.events)


@dataclass(frozen=True)
class DirtyPathEntry:
    """Deterministic aggregate for one path across the current ledger."""

    path: str
    first_sequence: int
    last_sequence: int
    event_count: int
    operations: tuple[HostEventOperation, ...]
    producers: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> DirtyPathEntry:
        record = _exact_mapping(value, _QUEUE_ENTRY_FIELDS, label="dirty path entry")
        path = normalize_dirty_path(record["path"])
        if path != record["path"]:
            raise HostEventStateError("dirty path entry path must be canonical")
        first_sequence = _positive_integer(
            record["first_sequence"], label="dirty path first_sequence"
        )
        last_sequence = _positive_integer(
            record["last_sequence"], label="dirty path last_sequence"
        )
        event_count = _positive_integer(
            record["event_count"], label="dirty path event_count"
        )
        if last_sequence < first_sequence:
            raise HostEventStateError(
                "dirty path last_sequence must be >= first_sequence"
            )
        if not isinstance(record["operations"], list):
            raise HostEventStateError("dirty path operations must be an array")
        operations = tuple(_operation(item) for item in record["operations"])
        if tuple(sorted(set(operations))) != operations:
            raise HostEventStateError(
                "dirty path operations must be sorted and unique"
            )
        if not isinstance(record["producers"], list):
            raise HostEventStateError("dirty path producers must be an array")
        producers = tuple(_producer(item) for item in record["producers"])
        if tuple(sorted(set(producers))) != producers:
            raise HostEventStateError("dirty path producers must be sorted and unique")
        return cls(
            path=path,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            event_count=event_count,
            operations=operations,
            producers=producers,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "event_count": self.event_count,
            "operations": list(self.operations),
            "producers": list(self.producers),
        }


@dataclass(frozen=True)
class DirtyPathQueue:
    """Schema v1 deterministic projection derived only from the ledger."""

    project_id: str
    ledger_event_count: int
    ledger_last_sequence: int
    ledger_sha256: str
    dirty_paths: tuple[DirtyPathEntry, ...]

    @classmethod
    def from_dict(cls, value: object) -> DirtyPathQueue:
        record = _exact_mapping(value, _QUEUE_FIELDS, label="dirty path queue")
        _schema_version(record, label="dirty path queue")
        if record["kind"] != DIRTY_PATH_QUEUE_KIND:
            raise HostEventStateError(
                f"unexpected dirty path queue kind {record['kind']!r}"
            )
        if record["queue_version"] != DIRTY_PATH_QUEUE_VERSION:
            raise HostEventStateError(
                f"unexpected dirty path queue version {record['queue_version']!r}"
            )
        project_id = record["project_id"]
        if not isinstance(project_id, str) or not project_id:
            raise HostEventStateError("dirty path queue project_id must be non-empty text")
        ledger_event_count = _nonnegative_integer(
            record["ledger_event_count"], label="ledger_event_count"
        )
        ledger_last_sequence = _nonnegative_integer(
            record["ledger_last_sequence"], label="ledger_last_sequence"
        )
        ledger_sha256 = record["ledger_sha256"]
        if (
            not isinstance(ledger_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", ledger_sha256) is None
        ):
            raise HostEventStateError("ledger_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(record["dirty_paths"], list):
            raise HostEventStateError("dirty_paths must be an array")
        dirty_paths = tuple(
            DirtyPathEntry.from_dict(item) for item in record["dirty_paths"]
        )
        if [entry.path for entry in dirty_paths] != sorted(
            {entry.path for entry in dirty_paths}
        ):
            raise HostEventStateError("dirty_paths must be sorted and unique by path")
        dirty_path_count = _nonnegative_integer(
            record["dirty_path_count"], label="dirty_path_count"
        )
        if dirty_path_count != len(dirty_paths):
            raise HostEventStateError("dirty_path_count does not match dirty_paths")
        if (ledger_event_count == 0) != (ledger_last_sequence == 0):
            raise HostEventStateError(
                "empty ledger count and last sequence must both be zero"
            )
        if ledger_event_count and ledger_last_sequence != ledger_event_count:
            raise HostEventStateError(
                "contiguous H-04 ledger requires last sequence to equal event count"
            )
        return cls(
            project_id=project_id,
            ledger_event_count=ledger_event_count,
            ledger_last_sequence=ledger_last_sequence,
            ledger_sha256=ledger_sha256,
            dirty_paths=dirty_paths,
        )

    @classmethod
    def from_ledger(cls, snapshot: HostEventLedgerSnapshot) -> DirtyPathQueue:
        aggregates: dict[str, dict[str, Any]] = {}
        for event in snapshot.events:
            for path in event.paths:
                aggregate = aggregates.setdefault(
                    path,
                    {
                        "first_sequence": event.sequence,
                        "last_sequence": event.sequence,
                        "event_count": 0,
                        "operations": set(),
                        "producers": set(),
                    },
                )
                aggregate["last_sequence"] = event.sequence
                aggregate["event_count"] += 1
                aggregate["operations"].add(event.operation)
                aggregate["producers"].add(event.producer)
        entries = tuple(
            DirtyPathEntry(
                path=path,
                first_sequence=aggregate["first_sequence"],
                last_sequence=aggregate["last_sequence"],
                event_count=aggregate["event_count"],
                operations=tuple(sorted(aggregate["operations"])),
                producers=tuple(sorted(aggregate["producers"])),
            )
            for path, aggregate in sorted(aggregates.items())
        )
        return cls(
            project_id=snapshot.project_id,
            ledger_event_count=snapshot.event_count,
            ledger_last_sequence=snapshot.last_sequence,
            ledger_sha256=snapshot.ledger_sha256,
            dirty_paths=entries,
        )

    @property
    def dirty_path_count(self) -> int:
        return len(self.dirty_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HOST_EVENT_SCHEMA_VERSION,
            "kind": DIRTY_PATH_QUEUE_KIND,
            "queue_version": DIRTY_PATH_QUEUE_VERSION,
            "project_id": self.project_id,
            "ledger_event_count": self.ledger_event_count,
            "ledger_last_sequence": self.ledger_last_sequence,
            "ledger_sha256": self.ledger_sha256,
            "dirty_path_count": self.dirty_path_count,
            "dirty_paths": [entry.as_dict() for entry in self.dirty_paths],
        }

    def serialized_bytes(self) -> bytes:
        return _canonical_json(self.as_dict(), error_type=DirtyPathQueueError) + b"\n"


@dataclass(frozen=True)
class HostEventSubmitResult:
    """Path-free Core/CLI result for one append or idempotent duplicate."""

    project_id: str
    event_id: str
    sequence: int
    disposition: HostEventDisposition
    queue: DirtyPathQueue
    projection_updated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HOST_EVENT_SCHEMA_VERSION,
            "kind": HOST_EVENT_SUBMIT_RESULT_KIND,
            "project_id": self.project_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "disposition": self.disposition,
            "ledger_relative_path": HOST_EVENT_LEDGER_RELATIVE_PATH,
            "queue_relative_path": DIRTY_PATH_QUEUE_RELATIVE_PATH,
            "projection_updated": self.projection_updated,
            "queue": self.queue.as_dict(),
        }


@dataclass(frozen=True)
class DirtyPathQueueResult:
    """Path-free result for loading or repairing the derived projection."""

    project_id: str
    queue: DirtyPathQueue
    projection_rebuilt: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HOST_EVENT_SCHEMA_VERSION,
            "kind": DIRTY_PATH_QUEUE_RESULT_KIND,
            "project_id": self.project_id,
            "ledger_relative_path": HOST_EVENT_LEDGER_RELATIVE_PATH,
            "queue_relative_path": DIRTY_PATH_QUEUE_RELATIVE_PATH,
            "projection_rebuilt": self.projection_rebuilt,
            "queue": self.queue.as_dict(),
        }


def _validate_lock_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("lock_timeout_seconds must be a positive finite number")
    return float(value)


@contextmanager
def _exclusive_event_lock(lock_file: Path, *, timeout_seconds: float) -> Iterator[None]:
    timeout = _validate_lock_timeout(timeout_seconds)
    if lock_file.is_symlink():
        raise HostEventLockError(f"event lock must not be a symbolic link: {lock_file}")
    token = f"{os.getpid()}:{uuid.uuid4().hex}\n".encode("ascii")
    deadline = time.monotonic() + timeout
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
                raise HostEventLockError(
                    f"timed out waiting for host-event lock: {lock_file}"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except OSError as exc:
            raise HostEventLockError(
                f"could not create host-event lock {lock_file}: {exc}"
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
            raise HostEventLockError(
                f"could not release host-event lock {lock_file}: {exc}"
            ) from exc



def _load_ledger_unlocked(
    events_file: Path,
    *,
    project_id: str,
) -> HostEventLedgerSnapshot:
    """Load and strictly validate the authoritative canonical JSONL ledger."""

    if events_file.is_symlink():
        raise HostEventStateError(
            f"host event ledger must not be a symbolic link: {events_file}"
        )
    if not events_file.exists():
        return HostEventLedgerSnapshot(
            project_id=project_id,
            events=(),
            ledger_sha256=hashlib.sha256(b"").hexdigest(),
        )
    if not events_file.is_file():
        raise HostEventStateError(f"host event ledger is not a file: {events_file}")
    try:
        payload = events_file.read_bytes()
    except OSError as exc:
        raise HostEventStateError(
            f"could not read host event ledger {events_file}: {exc}"
        ) from exc
    if payload and not payload.endswith(b"\n"):
        raise HostEventStateError(
            f"host event ledger must end with a newline: {events_file}"
        )

    events: list[HostEventRecord] = []
    event_ids: set[str] = set()
    for line_number, line in enumerate(payload.splitlines(keepends=True), start=1):
        if len(line) > _MAX_EVENT_LINE_BYTES:
            raise HostEventStateError(
                f"host event ledger line {line_number} exceeds the "
                f"{_MAX_EVENT_LINE_BYTES}-byte limit"
            )
        if not line[:-1].strip():
            raise HostEventStateError(
                f"host event ledger contains a blank line at {line_number}"
            )
        record = HostEventRecord.from_dict(
            _parse_json_bytes(line[:-1], label=f"host event ledger line {line_number}")
        )
        if record.project_id != project_id:
            raise HostEventStateError(
                f"host event ledger line {line_number} belongs to project "
                f"{record.project_id!r}, expected {project_id!r}"
            )
        expected_sequence = line_number
        if record.sequence != expected_sequence:
            raise HostEventStateError(
                f"host event ledger sequence {record.sequence} is not contiguous; "
                f"expected {expected_sequence}"
            )
        if record.event_id in event_ids:
            raise HostEventStateError(
                f"host event ledger contains duplicate event_id {record.event_id!r}"
            )
        if line != record.serialized_line():
            raise HostEventStateError(
                f"host event ledger line {line_number} is not canonical JSONL"
            )
        event_ids.add(record.event_id)
        events.append(record)

    return HostEventLedgerSnapshot(
        project_id=project_id,
        events=tuple(events),
        ledger_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _append_event_unlocked(events_file: Path, event: HostEventRecord) -> None:
    """Append and fsync exactly one canonical record without rewriting history."""

    if events_file.is_symlink():
        raise HostEventStateError(
            f"host event ledger must not be a symbolic link: {events_file}"
        )
    if events_file.exists() and not events_file.is_file():
        raise HostEventStateError(f"host event ledger is not a file: {events_file}")
    events_file.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(events_file, flags, 0o600)
        payload = event.serialized_line()
        with os.fdopen(descriptor, "ab", closefd=True) as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except OSError as exc:
        raise HostEventStateError(
            f"could not append host event ledger {events_file}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _projection_schema_is_rebuildable(payload: bytes, *, queue_file: Path) -> bool:
    """Return true for replaceable projection data, failing closed on schemas."""

    try:
        parsed = _parse_json_bytes(payload, label=f"dirty path queue {queue_file}")
    except HostEventStateError:
        # The queue is derived and malformed bytes carry no usable schema claim.
        return True
    if not isinstance(parsed, dict):
        return True
    if "schema_version" not in parsed:
        raise DirtyPathQueueError(
            f"dirty path queue is legacy v0 and will not be rewritten: {queue_file}"
        )
    version = parsed["schema_version"]
    if not _is_integer(version) or version < 1:
        raise DirtyPathQueueError(
            f"dirty path queue has unsupported legacy/invalid schema_version "
            f"{version!r}: {queue_file}"
        )
    if version > HOST_EVENT_SCHEMA_VERSION:
        raise DirtyPathQueueError(
            f"dirty path queue schema_version {version} is newer than supported "
            f"version {HOST_EVENT_SCHEMA_VERSION}: {queue_file}"
        )
    kind = parsed.get("kind")
    if kind is not None and kind != DIRTY_PATH_QUEUE_KIND:
        raise DirtyPathQueueError(
            f"dirty path queue has unsupported kind {kind!r}: {queue_file}"
        )
    queue_version = parsed.get("queue_version")
    if queue_version is not None and queue_version != DIRTY_PATH_QUEUE_VERSION:
        raise DirtyPathQueueError(
            f"dirty path queue has unsupported queue_version {queue_version!r}: "
            f"{queue_file}"
        )
    return True


def _write_queue_atomic(queue_file: Path, queue: DirtyPathQueue) -> bool:
    """Atomically materialize the deterministic ledger projection."""

    if queue_file.is_symlink():
        raise DirtyPathQueueError(
            f"dirty path queue must not be a symbolic link: {queue_file}"
        )
    payload = queue.serialized_bytes()
    if queue_file.exists():
        if not queue_file.is_file():
            raise DirtyPathQueueError(
                f"dirty path queue is not a file: {queue_file}"
            )
        try:
            current = queue_file.read_bytes()
        except OSError as exc:
            raise DirtyPathQueueError(
                f"could not read dirty path queue {queue_file}: {exc}"
            ) from exc
        if current == payload:
            return False
        _projection_schema_is_rebuildable(current, queue_file=queue_file)

    queue_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".dirty-paths.json.",
            suffix=".tmp",
            dir=queue_file.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, queue_file)
        temporary = None
        return True
    except OSError as exc:
        raise DirtyPathQueueError(
            f"could not write dirty path queue {queue_file}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _registration_paths(
    workspace_root: str | Path,
    project_id: str,
) -> tuple[str, Path, Path]:
    registration = load_registered_project(workspace_root, project_id)
    return (
        registration.project_id,
        registration.layout.events_file,
        registration.layout.dirty_paths_file,
    )


def load_host_event_ledger(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> HostEventLedgerSnapshot:
    """Load a consistent validated snapshot without reading project sources."""

    normalized_id, events_file, _ = _registration_paths(workspace_root, project_id)
    lock_file = events_file.with_name(events_file.name + ".lock")
    with _exclusive_event_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        return _load_ledger_unlocked(events_file, project_id=normalized_id)


def load_dirty_path_queue(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> DirtyPathQueueResult:
    """Load or repair the derived queue from the authoritative ledger."""

    normalized_id, events_file, queue_file = _registration_paths(
        workspace_root,
        project_id,
    )
    lock_file = events_file.with_name(events_file.name + ".lock")
    with _exclusive_event_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        snapshot = _load_ledger_unlocked(events_file, project_id=normalized_id)
        queue = DirtyPathQueue.from_ledger(snapshot)
        rebuilt = _write_queue_atomic(queue_file, queue)
    return DirtyPathQueueResult(
        project_id=normalized_id,
        queue=queue,
        projection_rebuilt=rebuilt,
    )


def submit_host_event(
    workspace_root: str | Path,
    project_id: str,
    *,
    event_id: str,
    producer: str,
    occurred_at: str | datetime,
    operation: str,
    paths: Iterable[str | Path] | str | Path,
    clock: Callable[[], datetime] | None = None,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> HostEventSubmitResult:
    """Append one idempotent host event and refresh only its derived queue.

    The ledger append is committed and fsynced before projection replacement.
    Therefore, a queue-write failure can be repaired by retrying the same event.
    """

    draft = HostEventDraft.create(
        event_id=event_id,
        producer=producer,
        occurred_at=occurred_at,
        operation=operation,
        paths=paths,
    )
    normalized_id, events_file, queue_file = _registration_paths(
        workspace_root,
        project_id,
    )
    lock_file = events_file.with_name(events_file.name + ".lock")
    with _exclusive_event_lock(
        lock_file,
        timeout_seconds=lock_timeout_seconds,
    ):
        snapshot = _load_ledger_unlocked(events_file, project_id=normalized_id)
        existing = next(
            (event for event in snapshot.events if event.event_id == draft.event_id),
            None,
        )
        if existing is not None:
            if existing.draft != draft:
                raise HostEventConflictError(
                    f"event_id {draft.event_id!r} was already used for a different "
                    "canonical host-event payload"
                )
            disposition: HostEventDisposition = "duplicate"
            sequence = existing.sequence
        else:
            event = HostEventRecord.from_draft(
                normalized_id,
                snapshot.last_sequence + 1,
                draft,
                ingested_at=_clock_timestamp(clock),
            )
            if len(event.serialized_line()) > _MAX_EVENT_LINE_BYTES:
                raise HostEventError(
                    f"canonical host event exceeds the {_MAX_EVENT_LINE_BYTES}-byte "
                    "ledger line limit"
                )
            _append_event_unlocked(events_file, event)
            snapshot = _load_ledger_unlocked(events_file, project_id=normalized_id)
            if not snapshot.events or snapshot.events[-1] != event:
                raise HostEventStateError(
                    "host event append did not produce the expected ledger tail"
                )
            disposition = "appended"
            sequence = event.sequence

        queue = DirtyPathQueue.from_ledger(snapshot)
        projection_updated = _write_queue_atomic(queue_file, queue)

    return HostEventSubmitResult(
        project_id=normalized_id,
        event_id=draft.event_id,
        sequence=sequence,
        disposition=disposition,
        queue=queue,
        projection_updated=projection_updated,
    )
