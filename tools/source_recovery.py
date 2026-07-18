#!/usr/bin/env python3
"""D-05 deterministic, local-only recovery for moved source identities."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

if __package__:
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifestError,
        load_project_manifest,
    )
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError
    from .project_registry import load_registered_project
    from .scan_policy import ScanPolicy, ScanPolicyError, load_scan_policy
    from .source_registry import (
        SourceRecord,
        SourceRegistryError,
        load_source_registry,
        record_source_relocation,
    )
else:
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectManifestError,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from scan_policy import (  # type: ignore[no-redef]
        ScanPolicy,
        ScanPolicyError,
        load_scan_policy,
    )
    from source_registry import (  # type: ignore[no-redef]
        SourceRecord,
        SourceRegistryError,
        load_source_registry,
        record_source_relocation,
    )


SOURCE_RECOVERY_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SOURCE_RECOVERY_KIND = "llmwiki-source-recovery-result"
SOURCE_RECOVERY_ATTEMPT_KIND = "llmwiki-source-recovery-attempt"
SOURCE_RECOVERY_VERSION = "source-recovery-v1"
SOURCE_RELOCATION_INSPECTION_KIND = "llmwiki-source-relocation-inspection"
SOURCE_RELOCATION_INSPECTION_VERSION = "source-relocation-inspection-v1"
SOURCE_RECOVERY_METHODS = ("path-alias", "content-hash", "git-history")
SOURCE_RECOVERY_STATUSES = (
    "not-needed",
    "recovered",
    "unresolved",
    "ambiguous",
)
SOURCE_RELOCATION_INSPECTION_STATUSES = (
    "current",
    "relocatable",
    "unresolved",
    "ambiguous",
)
MAX_GIT_HISTORY_COMMITS = 4096
MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 15

RecoveryMethod = Literal["path-alias", "content-hash", "git-history"]
RecoveryStatus = Literal["not-needed", "recovered", "unresolved", "ambiguous"]
RelocationInspectionStatus = Literal[
    "current",
    "relocatable",
    "unresolved",
    "ambiguous",
]

_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class SourceRecoveryError(LayoutError):
    """Base error for unsafe or inconsistent source relocation recovery."""

    reason_code = "source-relocation-recovery-failed"


class SourceRecoveryNotFoundError(SourceRecoveryError):
    """Raised when the requested source identity is unknown."""

    reason_code = "source-not-registered"


@dataclass(frozen=True)
class SourceRecoveryAttempt:
    """One ordered recovery priority group and its verified observations."""

    method: RecoveryMethod
    candidate_paths: tuple[str, ...] = ()
    blocked_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in SOURCE_RECOVERY_METHODS:
            raise SourceRecoveryError(f"unsupported recovery method: {self.method}")
        object.__setattr__(self, "candidate_paths", tuple(self.candidate_paths))
        object.__setattr__(self, "blocked_paths", tuple(self.blocked_paths))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_RECOVERY_SCHEMA_VERSION,
            "kind": SOURCE_RECOVERY_ATTEMPT_KIND,
            "recovery_version": SOURCE_RECOVERY_VERSION,
            "method": self.method,
            "candidate_paths": list(self.candidate_paths),
            "blocked_paths": list(self.blocked_paths),
        }


@dataclass(frozen=True)
class SourceRecoveryResult:
    """Explicit outcome of checking and, when safe, repairing one source path."""

    project_id: str
    source_id: str
    status: RecoveryStatus
    reason_code: str
    previous_path: str
    current_path: str
    current_version: int
    content_hash: str
    recovery_method: RecoveryMethod | None
    candidate_paths: tuple[str, ...]
    attempts: tuple[SourceRecoveryAttempt, ...]
    current_failure_reason_code: str | None
    wrote_registry: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in SOURCE_RECOVERY_STATUSES:
            raise SourceRecoveryError(f"unsupported recovery status: {self.status}")
        if (
            self.recovery_method is not None
            and self.recovery_method not in SOURCE_RECOVERY_METHODS
        ):
            raise SourceRecoveryError(
                f"unsupported recovery method: {self.recovery_method}"
            )
        object.__setattr__(self, "candidate_paths", tuple(self.candidate_paths))
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def recovered(self) -> bool:
        return self.status == "recovered"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_RECOVERY_SCHEMA_VERSION,
            "kind": SOURCE_RECOVERY_KIND,
            "recovery_version": SOURCE_RECOVERY_VERSION,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "previous_path": self.previous_path,
            "current_path": self.current_path,
            "current_version": self.current_version,
            "content_hash": self.content_hash,
            "recovery_method": self.recovery_method,
            "candidate_paths": list(self.candidate_paths),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "current_failure_reason_code": self.current_failure_reason_code,
            "wrote_registry": self.wrote_registry,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SourceRelocationInspectionResult:
    """Read-only deterministic assessment of one registered source path."""

    project_id: str
    source_id: str
    status: RelocationInspectionStatus
    reason_code: str
    registered_path: str
    current_version: int
    content_hash: str
    recovery_method: RecoveryMethod | None
    candidate_paths: tuple[str, ...]
    attempts: tuple[SourceRecoveryAttempt, ...]
    current_failure_reason_code: str | None
    detail: str

    def __post_init__(self) -> None:
        if self.status not in SOURCE_RELOCATION_INSPECTION_STATUSES:
            raise SourceRecoveryError(
                f"unsupported relocation inspection status: {self.status}"
            )
        if (
            self.recovery_method is not None
            and self.recovery_method not in SOURCE_RECOVERY_METHODS
        ):
            raise SourceRecoveryError(
                f"unsupported recovery method: {self.recovery_method}"
            )
        object.__setattr__(self, "candidate_paths", tuple(self.candidate_paths))
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def current(self) -> bool:
        return self.status == "current"

    @property
    def relocatable(self) -> bool:
        return self.status == "relocatable"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_RECOVERY_SCHEMA_VERSION,
            "kind": SOURCE_RELOCATION_INSPECTION_KIND,
            "inspection_version": SOURCE_RELOCATION_INSPECTION_VERSION,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "registered_path": self.registered_path,
            "current_version": self.current_version,
            "content_hash": self.content_hash,
            "recovery_method": self.recovery_method,
            "candidate_paths": list(self.candidate_paths),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "current_failure_reason_code": self.current_failure_reason_code,
            "registry_write_performed": False,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class _CandidateObservation:
    path: str
    state: Literal["match", "missing", "mismatch", "excluded", "blocked"]
    reason_code: str
    detail: str


@dataclass(frozen=True)
class _GitRunResult:
    returncode: int
    stdout: bytes


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise SourceRecoveryNotFoundError(
            "source_id must use Core-generated form 'src-' plus 32 lowercase hex digits"
        )
    return value


def _content_hash(value: object) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SourceRecoveryError("source content hash must be a lowercase SHA-256 digest")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SourceRecoveryError("source candidate path must be a non-empty string")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise SourceRecoveryError("source candidate path must already use Unicode NFC")
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.startswith("//")
        or _WINDOWS_DRIVE_PATTERN.match(value)
    ):
        raise SourceRecoveryError(
            f"source candidate path must be project-relative POSIX: {value!r}"
        )
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceRecoveryError(
            f"source candidate path is not canonical project-relative POSIX: {value!r}"
        )
    return value


def _sort_paths(
    paths: set[str] | list[str] | tuple[str, ...],
    policy: ScanPolicy,
) -> tuple[str, ...]:
    key = (
        (lambda value: (value,))
        if policy.case_sensitive
        else (lambda value: (value.casefold(), value))
    )
    return tuple(sorted(set(paths), key=key))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_policy_included(policy: ScanPolicy, path: str) -> bool:
    parts = PurePosixPath(path).parts
    for count in range(1, len(parts)):
        directory = PurePosixPath(*parts[:count]).as_posix()
        if not policy.decide_path(directory, is_directory=True).traverse:
            return False
    return policy.decide_path(path, is_directory=False).included


def _file_signature(metadata: os.stat_result) -> tuple[object, ...]:
    signature: tuple[object, ...] = (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if os.name != "nt":
        signature += (metadata.st_ctime_ns,)
    return signature


def _hash_stable_regular_file(path: Path) -> tuple[str, str | None]:
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "", "candidate-missing"
    except OSError:
        return "", "candidate-metadata-unreadable"
    if not stat.S_ISREG(before.st_mode):
        return "", "candidate-not-regular-file"

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return "", "candidate-not-regular-file"
            if _file_signature(opened) != _file_signature(before):
                return "", "candidate-changed-during-verification"
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_descriptor = os.fstat(source.fileno())
    except OSError:
        return "", "candidate-read-failed"

    try:
        after_path = path.stat(follow_symlinks=False)
    except OSError:
        return "", "candidate-changed-during-verification"
    signature = _file_signature(before)
    if (
        _file_signature(after_descriptor) != signature
        or _file_signature(after_path) != signature
    ):
        return "", "candidate-changed-during-verification"
    return digest.hexdigest(), None


def _resolve_observation_path(
    project_root: Path,
    path: str,
    *,
    reject_reparse_points: bool,
) -> tuple[Path | None, str | None]:
    root = project_root.resolve(strict=True)
    candidate = root
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in PurePosixPath(path).parts:
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return None, "candidate-missing"
        except OSError:
            return None, "candidate-metadata-unreadable"
        is_reparse = bool(
            reparse_flag
            and getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )
        if reject_reparse_points and (stat.S_ISLNK(metadata.st_mode) or is_reparse):
            return None, "candidate-symlink-or-reparse-point"
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, "candidate-missing"
    except OSError:
        return None, "candidate-metadata-unreadable"
    if not _is_within(resolved, root):
        return None, "candidate-outside-project"
    if not resolved.is_file():
        return None, "candidate-not-regular-file"
    return resolved, None


def _observe_candidate(
    project_root: Path,
    policy: ScanPolicy,
    path: str,
    expected_hash: str,
    *,
    enforce_policy: bool,
    reject_reparse_points: bool,
) -> _CandidateObservation:
    normalized = _relative_path(path)
    if enforce_policy and not _path_policy_included(policy, normalized):
        return _CandidateObservation(
            normalized,
            "excluded",
            "candidate-outside-scan-boundary",
            "candidate is excluded by the current deterministic scan policy",
        )
    resolved, failure = _resolve_observation_path(
        project_root,
        normalized,
        reject_reparse_points=reject_reparse_points,
    )
    if resolved is None:
        state = "missing" if failure == "candidate-missing" else "blocked"
        return _CandidateObservation(
            normalized,
            state,
            failure or "candidate-resolution-failed",
            "candidate path could not be verified as a project-contained regular file",
        )
    observed, hash_failure = _hash_stable_regular_file(resolved)
    if hash_failure is not None:
        state = "missing" if hash_failure == "candidate-missing" else "blocked"
        return _CandidateObservation(
            normalized,
            state,
            hash_failure,
            "candidate bytes could not be hashed from a stable regular file",
        )
    if observed != expected_hash:
        return _CandidateObservation(
            normalized,
            "mismatch",
            "candidate-content-hash-mismatch",
            f"expected {expected_hash}; observed {observed}",
        )
    return _CandidateObservation(
        normalized,
        "match",
        "candidate-content-hash-match",
        "candidate is in scope and exactly matches the recorded current content hash",
    )


def _attempt_from_observations(
    method: RecoveryMethod,
    observations: list[_CandidateObservation],
    policy: ScanPolicy,
) -> SourceRecoveryAttempt:
    return SourceRecoveryAttempt(
        method=method,
        candidate_paths=_sort_paths(
            {item.path for item in observations if item.state == "match"},
            policy,
        ),
        blocked_paths=_sort_paths(
            {item.path for item in observations if item.state == "blocked"},
            policy,
        ),
    )


def _load_source(
    workspace_root: str | Path,
    project_id: str,
    source_id: str,
) -> SourceRecord:
    normalized = _source_id(source_id)
    try:
        registry = load_source_registry(workspace_root, project_id)
    except (LayoutError, OSError) as exc:
        raise SourceRecoveryError(f"could not load source registry: {exc}") from exc
    try:
        return registry.by_source_id[normalized]
    except KeyError as exc:
        raise SourceRecoveryNotFoundError(
            f"source_id is not registered for project {project_id}: {normalized}"
        ) from exc


def _current_failure_reason(observation: _CandidateObservation) -> str:
    mapping = {
        "candidate-missing": "source-current-path-missing",
        "candidate-content-hash-mismatch": "source-content-hash-mismatch",
        "candidate-outside-project": "source-path-outside-project",
        "candidate-not-regular-file": "source-current-path-missing",
        "candidate-metadata-unreadable": "source-read-failed",
        "candidate-read-failed": "source-read-failed",
        "candidate-changed-during-verification": "source-read-failed",
    }
    return mapping.get(observation.reason_code, "source-current-path-unavailable")


def _result(
    *,
    project_id: str,
    record: SourceRecord,
    status: RecoveryStatus,
    reason_code: str,
    previous_path: str,
    current_path: str,
    recovery_method: RecoveryMethod | None,
    candidate_paths: tuple[str, ...],
    attempts: list[SourceRecoveryAttempt],
    current_failure_reason_code: str | None,
    wrote_registry: bool,
    detail: str,
) -> SourceRecoveryResult:
    if record.current_version is None or record.current_content_hash is None:
        raise SourceRecoveryError(
            f"source {record.source_id} has no recorded content version"
        )
    return SourceRecoveryResult(
        project_id=project_id,
        source_id=record.source_id,
        status=status,
        reason_code=reason_code,
        previous_path=previous_path,
        current_path=current_path,
        current_version=record.current_version,
        content_hash=record.current_content_hash,
        recovery_method=recovery_method,
        candidate_paths=candidate_paths,
        attempts=tuple(attempts),
        current_failure_reason_code=current_failure_reason_code,
        wrote_registry=wrote_registry,
        detail=detail,
    )


def _inspection_result(
    *,
    project_id: str,
    record: SourceRecord,
    status: RelocationInspectionStatus,
    reason_code: str,
    registered_path: str,
    recovery_method: RecoveryMethod | None,
    candidate_paths: tuple[str, ...],
    attempts: list[SourceRecoveryAttempt],
    current_failure_reason_code: str | None,
    detail: str,
) -> SourceRelocationInspectionResult:
    if record.current_version is None or record.current_content_hash is None:
        raise SourceRecoveryError(
            f"source {record.source_id} has no recorded content version"
        )
    return SourceRelocationInspectionResult(
        project_id=project_id,
        source_id=record.source_id,
        status=status,
        reason_code=reason_code,
        registered_path=registered_path,
        current_version=record.current_version,
        content_hash=record.current_content_hash,
        recovery_method=recovery_method,
        candidate_paths=candidate_paths,
        attempts=tuple(attempts),
        current_failure_reason_code=current_failure_reason_code,
        detail=detail,
    )


def _inspection_from_terminal(
    terminal: SourceRecoveryResult,
) -> SourceRelocationInspectionResult:
    status: RelocationInspectionStatus = (
        "ambiguous" if terminal.status == "ambiguous" else "unresolved"
    )
    return SourceRelocationInspectionResult(
        project_id=terminal.project_id,
        source_id=terminal.source_id,
        status=status,
        reason_code=terminal.reason_code,
        registered_path=terminal.previous_path,
        current_version=terminal.current_version,
        content_hash=terminal.content_hash,
        recovery_method=terminal.recovery_method,
        candidate_paths=terminal.candidate_paths,
        attempts=terminal.attempts,
        current_failure_reason_code=terminal.current_failure_reason_code,
        detail=terminal.detail,
    )


def _terminal_attempt_result(
    *,
    project_id: str,
    record: SourceRecord,
    previous_path: str,
    method: RecoveryMethod,
    attempt: SourceRecoveryAttempt,
    attempts: list[SourceRecoveryAttempt],
    current_failure_reason_code: str,
) -> SourceRecoveryResult | None:
    if attempt.blocked_paths:
        return _result(
            project_id=project_id,
            record=record,
            status="unresolved",
            reason_code="source-relocation-candidate-unreadable",
            previous_path=previous_path,
            current_path=record.current_path,
            recovery_method=method,
            candidate_paths=attempt.candidate_paths,
            attempts=attempts,
            current_failure_reason_code=current_failure_reason_code,
            wrote_registry=False,
            detail=(
                "equal-priority candidate paths could not all be verified; "
                "no registry binding was changed"
            ),
        )
    if len(attempt.candidate_paths) > 1:
        return _result(
            project_id=project_id,
            record=record,
            status="ambiguous",
            reason_code="source-relocation-ambiguous",
            previous_path=previous_path,
            current_path=record.current_path,
            recovery_method=method,
            candidate_paths=attempt.candidate_paths,
            attempts=attempts,
            current_failure_reason_code=current_failure_reason_code,
            wrote_registry=False,
            detail=(
                "multiple equal-priority paths exactly match the recorded content hash; "
                "no registry binding was changed"
            ),
        )
    return None


def _bind_candidate(
    workspace_root: str | Path,
    project_id: str,
    record: SourceRecord,
    *,
    previous_path: str,
    method: RecoveryMethod,
    candidate_path: str,
    attempts: list[SourceRecoveryAttempt],
    current_failure_reason_code: str,
    lock_timeout_seconds: float,
) -> SourceRecoveryResult:
    if record.current_version is None or record.current_content_hash is None:
        raise SourceRecoveryError(
            f"source {record.source_id} has no recorded content version"
        )
    try:
        update = record_source_relocation(
            workspace_root,
            project_id,
            source_id=record.source_id,
            recovered_path=candidate_path,
            expected_content_hash=record.current_content_hash,
            expected_current_path=previous_path,
            expected_current_version=record.current_version,
            lock_timeout_seconds=lock_timeout_seconds,
        )
    except (SourceRegistryError, OSError) as exc:
        raise SourceRecoveryError(
            f"could not persist verified source relocation: {exc}"
        ) from exc
    reason = (
        "source-relocation-already-recorded"
        if update.already_current
        else "source-relocation-recovered"
    )
    return _result(
        project_id=update.project_id,
        record=update.record,
        status="recovered",
        reason_code=reason,
        previous_path=previous_path,
        current_path=update.record.current_path,
        recovery_method=method,
        candidate_paths=(candidate_path,),
        attempts=attempts,
        current_failure_reason_code=current_failure_reason_code,
        wrote_registry=update.wrote_registry,
        detail=(
            f"recovered the source through {method} and preserved source identity "
            "and version history"
        ),
    )


def _manifest_hash_paths(
    registration: Any,
    expected_hash: str,
    excluded_paths: set[str],
) -> tuple[str, ...]:
    try:
        manifest = load_project_manifest(
            registration.layout.manifest_file,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )
    except (ProjectManifestError, OSError) as exc:
        raise SourceRecoveryError(f"could not load current project Manifest: {exc}") from exc
    paths = {
        _relative_path(record["path"])
        for record in manifest.file_records
        if record["content_sha256"] == expected_hash
        and record["path"] not in excluded_paths
    }
    return tuple(sorted(paths))


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return environment


def _run_git(project_root: Path, arguments: list[str]) -> _GitRunResult:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(project_root),
                *arguments,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceRecoveryError("local Git path-history query timed out") from exc
    except OSError as exc:
        raise SourceRecoveryError(
            f"could not run local Git path-history query: {exc}"
        ) from exc
    if len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise SourceRecoveryError(
            "local Git path-history output exceeded the deterministic byte limit"
        )
    return _GitRunResult(completed.returncode, completed.stdout)


def _decode_git_text(payload: bytes, field_name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceRecoveryError(
            f"local Git {field_name} is not valid UTF-8"
        ) from exc


def _parse_git_renames(payload: bytes) -> list[tuple[str, str]]:
    if not payload:
        return []
    tokens = payload.split(b"\0")
    if tokens[-1] == b"":
        tokens.pop()
    renames: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        status = _decode_git_text(tokens[index], "rename status")
        index += 1
        if not status.startswith("R") or index + 1 >= len(tokens):
            raise SourceRecoveryError(
                "local Git rename history has malformed NUL records"
            )
        old_path = _relative_path(_decode_git_text(tokens[index], "old path"))
        new_path = _relative_path(
            _decode_git_text(tokens[index + 1], "new path")
        )
        renames.append((old_path, new_path))
        index += 2
    return renames


def _repo_relative_path(repo_root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise SourceRecoveryError(
            "registered project root is outside the local Git worktree"
        ) from exc
    return relative.as_posix() or "."


def _project_relative_git_path(
    repo_path: str,
    project_prefix: str,
) -> str | None:
    if project_prefix == ".":
        return _relative_path(repo_path)
    prefix = f"{project_prefix}/"
    if not repo_path.startswith(prefix):
        return None
    return _relative_path(repo_path[len(prefix) :])


def _git_history_paths(
    project_root: Path,
    source_paths: tuple[str, ...],
) -> tuple[str, ...]:
    if shutil.which("git") is None:
        return ()
    root_result = _run_git(project_root, ["rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return ()
    root_text = _decode_git_text(root_result.stdout, "worktree root").strip()
    if not root_text:
        return ()
    try:
        repo_root = Path(root_text).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SourceRecoveryError(
            f"local Git worktree root is unavailable: {root_text}"
        ) from exc
    resolved_project = project_root.resolve(strict=True)
    if not _is_within(resolved_project, repo_root):
        raise SourceRecoveryError(
            "registered project root is outside the detected local Git worktree"
        )
    project_prefix = _repo_relative_path(repo_root, resolved_project)
    pathspec = "." if project_prefix == "." else project_prefix

    log_result = _run_git(
        resolved_project,
        [
            "log",
            "--reverse",
            "--format=",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=R",
            f"--max-count={MAX_GIT_HISTORY_COMMITS}",
            "HEAD",
            "--",
            pathspec,
        ],
    )
    diff_result = _run_git(
        resolved_project,
        [
            "diff",
            "--no-ext-diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=R",
            "HEAD",
            "--",
            pathspec,
        ],
    )
    renames: list[tuple[str, str]] = []
    if log_result.returncode == 0:
        renames.extend(_parse_git_renames(log_result.stdout))
    if diff_result.returncode == 0:
        renames.extend(_parse_git_renames(diff_result.stdout))
    if not renames:
        return ()

    prefix = "" if project_prefix == "." else f"{project_prefix}/"
    current_paths = {f"{prefix}{path}" for path in source_paths}
    for old_path, new_path in renames:
        if old_path in current_paths:
            current_paths.remove(old_path)
            current_paths.add(new_path)
    project_paths = {
        project_path
        for repo_path in current_paths
        if (project_path := _project_relative_git_path(repo_path, project_prefix))
        is not None
    }
    return tuple(sorted(project_paths))


def inspect_source_relocation(
    workspace_root: str | Path,
    project_id: str,
    source_id: str,
) -> SourceRelocationInspectionResult:
    """Inspect deterministic relocation candidates without changing the registry."""

    try:
        registration = load_registered_project(workspace_root, project_id)
        policy = load_scan_policy(registration.project_root)
    except (LayoutError, ScanPolicyError, OSError) as exc:
        raise SourceRecoveryError(
            f"could not load the registered project recovery boundary: {exc}"
        ) from exc
    record = _load_source(
        workspace_root,
        registration.project_id,
        source_id,
    )
    if record.current_version is None or record.current_content_hash is None:
        raise SourceRecoveryError(
            f"source {record.source_id} has no recorded content version"
        )

    registered_path = record.current_path
    expected_hash = _content_hash(record.current_content_hash)
    current = _observe_candidate(
        registration.project_root,
        policy,
        registered_path,
        expected_hash,
        enforce_policy=False,
        reject_reparse_points=False,
    )
    if current.state == "match":
        return _inspection_result(
            project_id=registration.project_id,
            record=record,
            status="current",
            reason_code="source-current-path-valid",
            registered_path=registered_path,
            recovery_method=None,
            candidate_paths=(),
            attempts=[],
            current_failure_reason_code=None,
            detail=(
                "the current recorded path is a project-contained regular file and "
                "matches the recorded current content hash"
            ),
        )

    current_failure_reason_code = _current_failure_reason(current)
    attempts: list[SourceRecoveryAttempt] = []
    history_paths = {_relative_path(item.path) for item in record.path_history}

    alias_paths = _sort_paths(history_paths - {registered_path}, policy)
    alias_observations = [
        _observe_candidate(
            registration.project_root,
            policy,
            path,
            expected_hash,
            enforce_policy=True,
            reject_reparse_points=True,
        )
        for path in alias_paths
    ]
    alias_attempt = _attempt_from_observations(
        "path-alias",
        alias_observations,
        policy,
    )
    attempts.append(alias_attempt)
    terminal = _terminal_attempt_result(
        project_id=registration.project_id,
        record=record,
        previous_path=registered_path,
        method="path-alias",
        attempt=alias_attempt,
        attempts=attempts,
        current_failure_reason_code=current_failure_reason_code,
    )
    if terminal is not None:
        return _inspection_from_terminal(terminal)
    if len(alias_attempt.candidate_paths) == 1:
        return _inspection_result(
            project_id=registration.project_id,
            record=record,
            status="relocatable",
            reason_code="source-relocation-candidate-found",
            registered_path=registered_path,
            recovery_method="path-alias",
            candidate_paths=alias_attempt.candidate_paths,
            attempts=attempts,
            current_failure_reason_code=current_failure_reason_code,
            detail=(
                "one path-alias candidate exactly matches the recorded current "
                "content hash; the registry was not changed"
            ),
        )

    manifest_paths = _manifest_hash_paths(
        registration,
        expected_hash,
        history_paths,
    )
    manifest_observations = [
        _observe_candidate(
            registration.project_root,
            policy,
            path,
            expected_hash,
            enforce_policy=True,
            reject_reparse_points=True,
        )
        for path in manifest_paths
    ]
    manifest_attempt = _attempt_from_observations(
        "content-hash",
        manifest_observations,
        policy,
    )
    attempts.append(manifest_attempt)
    terminal = _terminal_attempt_result(
        project_id=registration.project_id,
        record=record,
        previous_path=registered_path,
        method="content-hash",
        attempt=manifest_attempt,
        attempts=attempts,
        current_failure_reason_code=current_failure_reason_code,
    )
    if terminal is not None:
        return _inspection_from_terminal(terminal)
    if len(manifest_attempt.candidate_paths) == 1:
        return _inspection_result(
            project_id=registration.project_id,
            record=record,
            status="relocatable",
            reason_code="source-relocation-candidate-found",
            registered_path=registered_path,
            recovery_method="content-hash",
            candidate_paths=manifest_attempt.candidate_paths,
            attempts=attempts,
            current_failure_reason_code=current_failure_reason_code,
            detail=(
                "one current-Manifest candidate exactly matches the recorded current "
                "content hash; the registry was not changed"
            ),
        )

    git_paths = tuple(
        path
        for path in _git_history_paths(
            registration.project_root,
            _sort_paths(history_paths, policy),
        )
        if path not in history_paths and path not in set(manifest_paths)
    )
    git_observations = [
        _observe_candidate(
            registration.project_root,
            policy,
            path,
            expected_hash,
            enforce_policy=True,
            reject_reparse_points=True,
        )
        for path in git_paths
    ]
    git_attempt = _attempt_from_observations(
        "git-history",
        git_observations,
        policy,
    )
    attempts.append(git_attempt)
    terminal = _terminal_attempt_result(
        project_id=registration.project_id,
        record=record,
        previous_path=registered_path,
        method="git-history",
        attempt=git_attempt,
        attempts=attempts,
        current_failure_reason_code=current_failure_reason_code,
    )
    if terminal is not None:
        return _inspection_from_terminal(terminal)
    if len(git_attempt.candidate_paths) == 1:
        return _inspection_result(
            project_id=registration.project_id,
            record=record,
            status="relocatable",
            reason_code="source-relocation-candidate-found",
            registered_path=registered_path,
            recovery_method="git-history",
            candidate_paths=git_attempt.candidate_paths,
            attempts=attempts,
            current_failure_reason_code=current_failure_reason_code,
            detail=(
                "one local-Git-history candidate exactly matches the recorded current "
                "content hash; the registry was not changed"
            ),
        )

    return _inspection_result(
        project_id=registration.project_id,
        record=record,
        status="unresolved",
        reason_code="source-relocation-not-found",
        registered_path=registered_path,
        recovery_method=None,
        candidate_paths=(),
        attempts=attempts,
        current_failure_reason_code=current_failure_reason_code,
        detail=(
            "no project-contained regular file matched the recorded current content "
            "hash through path aliases, the current Manifest, or local Git history"
        ),
    )


def recover_source(
    workspace_root: str | Path,
    project_id: str,
    source_id: str,
    *,
    lock_timeout_seconds: float = 10.0,
) -> SourceRecoveryResult:
    """Recover one unavailable source path by deterministic local priority groups."""

    inspection = inspect_source_relocation(workspace_root, project_id, source_id)
    attempts = list(inspection.attempts)
    if inspection.status != "relocatable":
        status: RecoveryStatus = (
            "not-needed" if inspection.status == "current" else inspection.status
        )
        return SourceRecoveryResult(
            project_id=inspection.project_id,
            source_id=inspection.source_id,
            status=status,
            reason_code=inspection.reason_code,
            previous_path=inspection.registered_path,
            current_path=inspection.registered_path,
            current_version=inspection.current_version,
            content_hash=inspection.content_hash,
            recovery_method=inspection.recovery_method,
            candidate_paths=inspection.candidate_paths,
            attempts=tuple(attempts),
            current_failure_reason_code=inspection.current_failure_reason_code,
            wrote_registry=False,
            detail=inspection.detail,
        )
    if inspection.recovery_method is None or len(inspection.candidate_paths) != 1:
        raise SourceRecoveryError(
            "relocatable inspection must identify exactly one recovery candidate"
        )
    if inspection.current_failure_reason_code is None:
        raise SourceRecoveryError(
            "relocatable inspection must retain the current-path failure reason"
        )

    candidate_path = inspection.candidate_paths[0]
    record = _load_source(workspace_root, inspection.project_id, inspection.source_id)
    if (
        record.current_version != inspection.current_version
        or record.current_content_hash != inspection.content_hash
        or record.current_path not in {inspection.registered_path, candidate_path}
    ):
        raise SourceRecoveryError(
            "source registry changed during relocation recovery inspection"
        )
    return _bind_candidate(
        workspace_root,
        inspection.project_id,
        record,
        previous_path=inspection.registered_path,
        method=inspection.recovery_method,
        candidate_path=candidate_path,
        attempts=attempts,
        current_failure_reason_code=inspection.current_failure_reason_code,
        lock_timeout_seconds=lock_timeout_seconds,
    )
