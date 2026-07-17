#!/usr/bin/env python3
"""Deterministic B-07 reading priority and reference-promotion queue.

The artifact ranks every ordinary Manifest v4 file without changing truthful
``file_state``.  It may read a bounded set of policy-approved local text files
to resolve project-relative references, but it never extracts content, creates
Source/Evidence records, calls a model, or writes to the source project.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote

if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .file_classification import classification_from_dict
    from .file_state import PROCESSING_STATUSES, READ_DEPTHS, file_state_from_dict
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        ProjectLayout,
        parse_versioned_json_bytes,
        validate_project_id,
    )
    from .project_registry import ProjectRegistrationResult, load_registered_project
    from .scan_policy import ScanPolicy, ScanPolicyConfig, load_scan_policy
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from file_classification import (  # type: ignore[no-redef]
        classification_from_dict,
    )
    from file_state import (  # type: ignore[no-redef]
        PROCESSING_STATUSES,
        READ_DEPTHS,
        file_state_from_dict,
    )
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        ProjectLayout,
        parse_versioned_json_bytes,
        validate_project_id,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
    )
    from scan_policy import (  # type: ignore[no-redef]
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )


READING_PRIORITY_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
READING_PRIORITY_KIND = "llmwiki-reading-priority"
READING_PRIORITY_VERSION = "reading-priority-v1"
READING_PRIORITY_FILENAME = "reading-priority.json"

REFERENCE_SOURCE_MAX_FILES = 128
REFERENCE_SOURCE_MAX_FILE_BYTES = 256 * 1024
REFERENCE_SOURCE_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DEEP_READ_MAX_FILES = 128
DEEP_READ_MAX_TOTAL_BYTES = 32 * 1024 * 1024
LARGE_DATASET_MIN_FILES = 32
LARGE_DATASET_MIN_BYTES = 64 * 1024 * 1024

_PRIORITY_TIERS = ("promoted", "critical", "high", "normal", "low", "limited")
_DEEP_READ_STATUSES = ("selected", "deferred", "not-candidate", "limited")
_REFERENCE_SCAN_STATUSES = (
    "read",
    "deferred-file-size",
    "deferred-file-count",
    "deferred-total-bytes",
    "limited",
    "unsupported-format",
    "not-applicable",
)
_ROLE_ORDER = (
    "project_documentation",
    "paper",
    "configuration",
    "experiment",
    "result",
    "notebook",
    "source_code",
    "automation",
    "documentation",
    "dependency_manifest",
    "bibliography",
    "test_code",
    "project_metadata",
    "figure",
    "run_log",
    "dataset",
    "model_artifact",
    "unknown",
)
_ROLE_SCORE = {
    role: (len(_ROLE_ORDER) - index) * 100 for index, role in enumerate(_ROLE_ORDER)
}
_REFERENCE_SOURCE_ROLES = frozenset(
    {
        "project_documentation",
        "paper",
        "configuration",
        "experiment",
        "notebook",
        "source_code",
        "automation",
        "documentation",
        "dependency_manifest",
        "bibliography",
    }
)
_ORDINARY_DEEP_READ_ROLES = frozenset(
    {
        "project_documentation",
        "paper",
        "configuration",
        "experiment",
        "result",
        "notebook",
        "source_code",
        "automation",
        "documentation",
        "dependency_manifest",
        "bibliography",
        "test_code",
        "project_metadata",
        "figure",
    }
)
_TEXT_REFERENCE_FORMATS = frozenset(
    {
        "plain_text",
        "markdown",
        "restructured_text",
        "python",
        "r",
        "julia",
        "matlab",
        "c",
        "cpp",
        "java",
        "javascript",
        "typescript",
        "shell",
        "powershell",
        "batch",
        "rust",
        "go",
        "ruby",
        "perl",
        "sql",
        "html",
        "css",
        "xml",
        "json",
        "jsonl",
        "yaml",
        "toml",
        "ini",
        "latex",
        "bibtex",
        "notebook",
        "log",
        "svg",
    }
)
_UNSUPPORTED_DEEP_READ_FORMATS = frozenset(
    {"unknown", "binary", "zip", "gzip", "tar", "wav", "mp3"}
)
_MODEL_FORMATS = frozenset({"pytorch_checkpoint", "model_checkpoint", "pickle"})
_PROMOTABLE_READ_DEPTHS = frozenset({"normal_read", "sampled", "metadata_only"})
_ENTRYPOINT_NAMES = frozenset(
    {
        "main.py",
        "train.py",
        "evaluate.py",
        "eval.py",
        "run.py",
        "app.py",
        "cli.py",
        "main.r",
        "main.jl",
        "main.m",
        "index.js",
        "index.ts",
    }
)
_SIGNAL_WORDS = (
    "architecture",
    "ablation",
    "benchmark",
    "evaluation",
    "metric",
    "metrics",
    "result",
    "results",
)
_CONFIG_FIELDS = frozenset(
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
_REASON_CODE_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\r\n]*\]\(([^)\r\n]+)\)")
_QUOTED_RE = re.compile(r"[\"']([^\"'\r\n]{1,512})[\"']")
_LATEX_RE = re.compile(
    r"\\(?:includegraphics|input|include|bibliography|addbibresource)"
    r"\s*(?:\[[^\]]*\]\s*)?\{([^}\r\n]+)\}",
    re.IGNORECASE,
)
_BARE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/])(?:\.{0,2}[\\/])?"
    r"(?:[^\s<>\"'`(){}\[\],;:]+[\\/])*"
    r"[^\s<>\"'`(){}\[\],;:]+\.[A-Za-z0-9]{1,16}"
)


class ReadingPriorityError(LayoutError):
    """Raised when a B-07 priority artifact cannot be produced safely."""


@dataclass(frozen=True)
class ReadingPriorityResult:
    """Paths and persisted payload for one B-07 generation."""

    project_id: str
    manifest_file: Path
    priority_file: Path
    priority: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "priority_file": str(self.priority_file),
            "priority": self.priority,
        }


@dataclass(frozen=True)
class _FileInfo:
    path: str
    physical_path: Path
    content_sha256: str
    size_bytes: int
    mtime_ns: int
    format: str
    research_role: str
    processing_status: str
    current_read_depth: str
    local_content_access: str
    local_reason_code: str
    dataset_group: str | None
    large_dataset: bool


@dataclass(frozen=True)
class _PriorityDraft:
    info: _FileInfo
    priority_score: int
    priority_tier: str
    candidate: bool
    promotion_candidate: bool
    limited_reason: str | None
    reason_codes: tuple[str, ...]
    referenced_by: tuple[str, ...]
    reference_scan_status: str
    reference_bytes_read: int


def _limits_payload() -> dict[str, int]:
    return {
        "reference_source_max_files": REFERENCE_SOURCE_MAX_FILES,
        "reference_source_max_file_bytes": REFERENCE_SOURCE_MAX_FILE_BYTES,
        "reference_source_max_total_bytes": REFERENCE_SOURCE_MAX_TOTAL_BYTES,
        "deep_read_max_files": DEEP_READ_MAX_FILES,
        "deep_read_max_total_bytes": DEEP_READ_MAX_TOTAL_BYTES,
        "large_dataset_min_files": LARGE_DATASET_MIN_FILES,
        "large_dataset_min_bytes": LARGE_DATASET_MIN_BYTES,
    }


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReadingPriorityError(f"{label} must be a non-empty project-relative path")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ReadingPriorityError(
            f"{label} is not normalized project-relative POSIX form: {value!r}"
        )
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _physical_path(project_root: Path, relative_path: str) -> Path:
    return project_root.joinpath(*PurePosixPath(relative_path).parts)


def _ordered_reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)

def _scan_policy_for_manifest(manifest: ProjectManifest) -> ScanPolicy:
    snapshot = manifest.summary.get("policy")
    if not isinstance(snapshot, dict):
        raise ReadingPriorityError("Manifest policy snapshot is missing or malformed")
    raw_config = snapshot.get("config")
    if not isinstance(raw_config, dict) or set(raw_config) != _CONFIG_FIELDS:
        raise ReadingPriorityError("Manifest policy config is missing or malformed")
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
        current = load_scan_policy(manifest.project_root, config=config)
    except (TypeError, ValueError, OSError) as exc:
        raise ReadingPriorityError(
            f"could not reconstruct the Manifest scan policy: {exc}"
        ) from exc
    if current.as_dict() != snapshot:
        raise ReadingPriorityError(
            "current scan policy differs from the Manifest snapshot; run inventory first"
        )
    return current


def _dataset_group(path: str, role: str) -> str | None:
    if role != "dataset":
        return None
    pure = PurePosixPath(path)
    parts = pure.parts[:-1]
    matching = [
        index
        for index, part in enumerate(parts)
        if part.casefold() in {"data", "dataset", "datasets"}
    ]
    if matching:
        return PurePosixPath(*parts[: matching[-1] + 1]).as_posix()
    parent = pure.parent.as_posix()
    return parent if parent != "." else "<project-root>"


def _build_file_infos(
    manifest: ProjectManifest,
    policy: ScanPolicy,
) -> tuple[_FileInfo, ...]:
    preliminary: list[tuple[dict[str, Any], str | None]] = []
    group_counts: Counter[str] = Counter()
    group_bytes: Counter[str] = Counter()
    for row in manifest.file_records:
        classification = classification_from_dict(row["classification"])
        state = file_state_from_dict(row["file_state"])
        path = _validated_relative_path(row["path"], label="Manifest file path")
        group = _dataset_group(path, classification.research_role)
        preliminary.append((row, group))
        if group is not None:
            group_counts[group] += 1
            group_bytes[group] += row["size_bytes"]

    large_groups = {
        group
        for group in group_counts
        if group_counts[group] >= LARGE_DATASET_MIN_FILES
        or group_bytes[group] >= LARGE_DATASET_MIN_BYTES
    }
    infos: list[_FileInfo] = []
    for row, group in preliminary:
        classification = classification_from_dict(row["classification"])
        state = file_state_from_dict(row["file_state"])
        path = row["path"]
        decision = policy.decide_file(path, size_bytes=row["size_bytes"])
        infos.append(
            _FileInfo(
                path=path,
                physical_path=_physical_path(manifest.project_root, path),
                content_sha256=row["content_sha256"],
                size_bytes=row["size_bytes"],
                mtime_ns=row["mtime_ns"],
                format=classification.format,
                research_role=classification.research_role,
                processing_status=state.processing_status,
                current_read_depth=state.read_depth,
                local_content_access=decision.local_content_access,
                local_reason_code=decision.local_reason_code,
                dataset_group=group,
                large_dataset=group in large_groups if group is not None else False,
            )
        )
    return tuple(infos)


def _open_reference_source(path: Path) -> int:
    flags = os.O_RDONLY
    for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def _read_verified_reference_source(info: _FileInfo, project_root: Path) -> bytes:
    """Read one bounded local source and prove it still matches the Manifest."""

    resolved_root = project_root.resolve()
    try:
        before = info.physical_path.lstat()
        resolved_before = info.physical_path.resolve(strict=True)
    except OSError as exc:
        raise ReadingPriorityError(
            f"reference source changed or became unreadable after inventory: {info.path}"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or not _is_within(
        resolved_before, resolved_root
    ):
        raise ReadingPriorityError(
            f"reference source is no longer a regular file inside the project: {info.path}"
        )
    if before.st_size != info.size_bytes or before.st_mtime_ns != info.mtime_ns:
        raise ReadingPriorityError(
            f"reference source changed after inventory: {info.path}; run inventory first"
        )

    descriptor = -1
    try:
        descriptor = _open_reference_source(info.physical_path)
        opened_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = info.size_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        after = info.physical_path.lstat()
        resolved_after = info.physical_path.resolve(strict=True)
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not read reference source {info.path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    identity_before = (before.st_dev, before.st_ino)
    if (
        not stat.S_ISREG(opened_before.st_mode)
        or not stat.S_ISREG(opened_after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or not _is_within(resolved_after, resolved_root)
        or resolved_after != resolved_before
        or (opened_before.st_dev, opened_before.st_ino) != identity_before
        or (opened_after.st_dev, opened_after.st_ino) != identity_before
        or (after.st_dev, after.st_ino) != identity_before
        or opened_before.st_size != before.st_size
        or opened_before.st_mtime_ns != before.st_mtime_ns
        or opened_after.st_size != before.st_size
        or opened_after.st_mtime_ns != before.st_mtime_ns
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(payload) != info.size_bytes
        or hashlib.sha256(payload).hexdigest() != info.content_sha256
    ):
        raise ReadingPriorityError(
            f"reference source changed after inventory: {info.path}; run inventory first"
        )
    return payload


def _decode_reference_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return payload.decode("utf-16", errors="strict")
            except UnicodeDecodeError:
                pass
        return payload.decode("latin-1", errors="strict")


def _markdown_target(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("<") and ">" in candidate:
        return candidate[1 : candidate.index(">")]
    if not candidate:
        return candidate
    # Markdown permits a title after whitespace. Paths with spaces should use <...>.
    return candidate.split(maxsplit=1)[0]


def _reference_candidates(text: str) -> tuple[str, ...]:
    candidates: set[str] = set()
    for match in _MARKDOWN_LINK_RE.finditer(text):
        candidates.add(_markdown_target(match.group(1)))
    for regex in (_QUOTED_RE, _LATEX_RE, _BARE_PATH_RE):
        for match in regex.finditer(text):
            candidates.add(match.group(1) if match.lastindex else match.group(0))
    return tuple(sorted(value for value in candidates if value.strip()))


def _clean_reference(value: str) -> str | None:
    candidate = unquote(value.strip().strip("<>"))
    candidate = candidate.rstrip(".,;)")
    if not candidate or "\x00" in candidate:
        return None
    if _WINDOWS_DRIVE_RE.match(candidate) or _URI_SCHEME_RE.match(candidate):
        return None
    if candidate.startswith(("/", "\\", "//")):
        return None
    candidate = candidate.replace("\\", "/")
    candidate = candidate.split("#", 1)[0].split("?", 1)[0].strip()
    if not candidate or candidate.endswith("/"):
        return None
    return unicodedata.normalize("NFC", candidate)


def _reference_variants(candidate: str) -> tuple[str, ...]:
    result = [candidate]
    if not PurePosixPath(candidate).suffix:
        result.extend(
            candidate + suffix
            for suffix in (".tex", ".bib", ".md", ".rst", ".yaml", ".yml", ".json")
        )
    return tuple(result)


def _lexical_join(base: PurePosixPath, candidate: str) -> str | None:
    combined = posixpath.normpath((base / PurePosixPath(candidate)).as_posix())
    if combined in {"", ".", ".."} or combined.startswith("../"):
        return None
    pure = PurePosixPath(combined)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    return pure.as_posix()


def _path_lookup(paths: Iterable[str], *, case_sensitive: bool) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        key = path if case_sensitive else path.casefold()
        result[key].append(path)
    return result


def _lookup_unique(
    value: str,
    lookup: dict[str, list[str]],
    *,
    case_sensitive: bool,
) -> str | None:
    matches = lookup.get(value if case_sensitive else value.casefold(), [])
    return matches[0] if len(matches) == 1 else None


def _resolve_reference(
    raw_value: str,
    *,
    source_path: str,
    path_lookup: dict[str, list[str]],
    basename_lookup: dict[str, list[str]],
    case_sensitive: bool,
) -> str | None:
    candidate = _clean_reference(raw_value)
    if candidate is None:
        return None
    source_parent = PurePosixPath(source_path).parent
    for variant in _reference_variants(candidate):
        for base in (source_parent, PurePosixPath(".")):
            joined = _lexical_join(base, variant)
            if joined is None:
                continue
            target = _lookup_unique(
                joined,
                path_lookup,
                case_sensitive=case_sensitive,
            )
            if target is not None and target != source_path:
                return target
    # An explicit path that did not resolve must not silently degrade to a
    # basename match. In particular, traversal such as ``../target.ext`` must
    # never resolve to an unrelated in-project file with the same basename.
    if "/" in candidate:
        return None
    basename = PurePosixPath(candidate).name
    if not basename:
        return None
    target = _lookup_unique(
        basename,
        basename_lookup,
        case_sensitive=case_sensitive,
    )
    if target == source_path:
        return None
    return target


def _reference_source_sort_key(info: _FileInfo) -> tuple[int, int, str]:
    role_index = _ROLE_ORDER.index(info.research_role)
    depth = len(PurePosixPath(info.path).parts)
    return (role_index, depth, info.path)


def _scan_references(
    infos: tuple[_FileInfo, ...],
    *,
    project_root: Path,
    case_sensitive: bool,
    before_reference_read: Callable[[], None] | None = None,
) -> tuple[dict[str, str], dict[str, int], dict[str, set[str]]]:
    statuses: dict[str, str] = {}
    bytes_read: dict[str, int] = {info.path: 0 for info in infos}
    eligible: list[_FileInfo] = []
    for info in infos:
        if info.research_role not in _REFERENCE_SOURCE_ROLES:
            statuses[info.path] = "not-applicable"
        elif info.format not in _TEXT_REFERENCE_FORMATS:
            statuses[info.path] = "unsupported-format"
        elif info.processing_status in {"failed", "missing"} or info.current_read_depth in {
            "ignored",
            "unsupported",
        }:
            statuses[info.path] = "limited"
        elif info.local_content_access != "allowed":
            statuses[info.path] = "limited"
        elif info.size_bytes > REFERENCE_SOURCE_MAX_FILE_BYTES:
            statuses[info.path] = "deferred-file-size"
        else:
            eligible.append(info)

    path_lookup = _path_lookup(
        (info.path for info in infos),
        case_sensitive=case_sensitive,
    )
    basename_lookup = _path_lookup(
        (PurePosixPath(info.path).name for info in infos),
        case_sensitive=case_sensitive,
    )
    # Basename lookup must map names back to paths rather than to names.
    basename_lookup = defaultdict(list)
    for info in infos:
        key = PurePosixPath(info.path).name
        basename_lookup[key if case_sensitive else key.casefold()].append(info.path)

    referenced_by: dict[str, set[str]] = defaultdict(set)
    selected_count = 0
    selected_bytes = 0
    for info in sorted(eligible, key=_reference_source_sort_key):
        if selected_count >= REFERENCE_SOURCE_MAX_FILES:
            statuses[info.path] = "deferred-file-count"
            continue
        if selected_bytes + info.size_bytes > REFERENCE_SOURCE_MAX_TOTAL_BYTES:
            statuses[info.path] = "deferred-total-bytes"
            continue
        if before_reference_read is not None:
            before_reference_read()
        payload = _read_verified_reference_source(info, project_root)
        statuses[info.path] = "read"
        bytes_read[info.path] = len(payload)
        selected_count += 1
        selected_bytes += len(payload)
        text = _decode_reference_text(payload)
        for raw_value in _reference_candidates(text):
            target = _resolve_reference(
                raw_value,
                source_path=info.path,
                path_lookup=path_lookup,
                basename_lookup=basename_lookup,
                case_sensitive=case_sensitive,
            )
            if target is not None:
                referenced_by[target].add(info.path)
    return statuses, bytes_read, referenced_by

def _priority_score_values(
    research_role: str,
    path: str,
    referenced_by: tuple[str, ...],
) -> tuple[int, tuple[str, ...]]:
    score = _ROLE_SCORE.get(research_role, 0)
    reasons = [f"role-{research_role.replace('_', '-')}"]
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    stem = pure.stem.casefold()
    if len(pure.parts) == 1:
        score += 250
        reasons.append("root-level-file")
    if stem.startswith("readme"):
        score += 600
        reasons.append("readme-priority")
    if name in _ENTRYPOINT_NAMES:
        score += 450
        reasons.append("entrypoint-priority")
    if any(word in name for word in _SIGNAL_WORDS):
        score += 300
        reasons.append("research-signal-name")
    if referenced_by:
        score += 10_000 + 100 * len(referenced_by)
        reasons.append("inbound-key-reference")
    return score, tuple(reasons)


def _priority_score(
    info: _FileInfo,
    referenced_by: tuple[str, ...],
) -> tuple[int, tuple[str, ...]]:
    return _priority_score_values(info.research_role, info.path, referenced_by)


def _intrinsic_limited_reason(
    *,
    processing_status: str,
    current_read_depth: str,
    format_name: str,
    research_role: str,
    large_dataset: bool,
) -> str | None:
    """Recompute limitations carried entirely by persisted file facts."""

    if processing_status in {"failed", "missing"}:
        return "processing-state-limited"
    if current_read_depth in {"ignored", "unsupported"}:
        return "read-depth-limited"
    if format_name in _UNSUPPORTED_DEEP_READ_FORMATS:
        return "unsupported-format"
    if research_role == "model_artifact" or format_name in _MODEL_FORMATS:
        return "model-artifact-limited"
    if large_dataset:
        return "large-dataset-limited"
    return None


def _limited_reason(info: _FileInfo) -> str | None:
    if info.local_content_access != "allowed":
        return info.local_reason_code
    return _intrinsic_limited_reason(
        processing_status=info.processing_status,
        current_read_depth=info.current_read_depth,
        format_name=info.format,
        research_role=info.research_role,
        large_dataset=info.large_dataset,
    )


def _priority_tier_values(
    research_role: str,
    *,
    score: int,
    promotion_candidate: bool,
    limited: bool,
) -> str:
    if limited:
        return "limited"
    if promotion_candidate:
        return "promoted"
    if research_role in {"project_documentation", "paper", "configuration"}:
        return "critical"
    if score >= 1_450:
        return "high"
    if score >= 700:
        return "normal"
    return "low"


def _priority_tier(
    info: _FileInfo,
    *,
    score: int,
    promotion_candidate: bool,
    limited_reason: str | None,
) -> str:
    return _priority_tier_values(
        info.research_role,
        score=score,
        promotion_candidate=promotion_candidate,
        limited=limited_reason is not None,
    )


def _build_priority_drafts(
    infos: tuple[_FileInfo, ...],
    *,
    reference_statuses: dict[str, str],
    reference_bytes: dict[str, int],
    referenced_by_map: dict[str, set[str]],
) -> list[_PriorityDraft]:
    drafts: list[_PriorityDraft] = []
    for info in infos:
        inbound = tuple(sorted(referenced_by_map.get(info.path, set())))
        score, reasons = _priority_score(info, inbound)
        limited = _limited_reason(info)
        promotion = (
            limited is None
            and bool(inbound)
            and info.current_read_depth in _PROMOTABLE_READ_DEPTHS
        )
        candidate = (
            limited is None
            and info.current_read_depth != "deep_read"
            and (promotion or info.research_role in _ORDINARY_DEEP_READ_ROLES)
        )
        if limited is not None:
            reasons += (limited,)
        elif info.current_read_depth == "deep_read":
            reasons += ("already-deep-read",)
        elif candidate:
            reasons += (
                "reference-promotion-candidate"
                if promotion
                else "role-deep-read-candidate",
            )
        else:
            reasons += ("not-deep-read-candidate",)
        tier = _priority_tier(
            info,
            score=score,
            promotion_candidate=promotion,
            limited_reason=limited,
        )
        drafts.append(
            _PriorityDraft(
                info=info,
                priority_score=score,
                priority_tier=tier,
                candidate=candidate,
                promotion_candidate=promotion,
                limited_reason=limited,
                reason_codes=_ordered_reason_codes(reasons),
                referenced_by=inbound,
                reference_scan_status=reference_statuses[info.path],
                reference_bytes_read=reference_bytes[info.path],
            )
        )
    tier_order = {tier: index for index, tier in enumerate(_PRIORITY_TIERS)}
    drafts.sort(
        key=lambda draft: (
            tier_order[draft.priority_tier],
            -draft.priority_score,
            draft.info.path,
        )
    )
    return drafts


def _materialize_priority(
    *,
    project_id: str,
    manifest: ProjectManifest,
    manifest_sha256: str,
    drafts: list[_PriorityDraft],
) -> dict[str, Any]:
    file_records: list[dict[str, Any]] = []
    selected_count = 0
    selected_bytes = 0
    for priority_rank, draft in enumerate(drafts, start=1):
        info = draft.info
        reasons = list(draft.reason_codes)
        if draft.limited_reason is not None:
            deep_status = "limited"
            recommended = info.current_read_depth
        elif not draft.candidate:
            deep_status = "not-candidate"
            recommended = info.current_read_depth
        elif (
            selected_count < DEEP_READ_MAX_FILES
            and selected_bytes + info.size_bytes <= DEEP_READ_MAX_TOTAL_BYTES
        ):
            deep_status = "selected"
            recommended = "deep_read"
            selected_count += 1
            selected_bytes += info.size_bytes
            reasons.append("deep-read-selected")
        else:
            deep_status = "deferred"
            recommended = "deep_read"
            reasons.append("deep-read-budget-deferred")
        file_records.append(
            {
                "path": info.path,
                "content_sha256": info.content_sha256,
                "size_bytes": info.size_bytes,
                "format": info.format,
                "research_role": info.research_role,
                "processing_status": info.processing_status,
                "current_read_depth": info.current_read_depth,
                "priority_score": draft.priority_score,
                "priority_rank": priority_rank,
                "priority_tier": draft.priority_tier,
                "recommended_read_depth": recommended,
                "deep_read_status": deep_status,
                "reference_scan_status": draft.reference_scan_status,
                "reference_bytes_read": draft.reference_bytes_read,
                "dataset_group": info.dataset_group,
                "reason_codes": list(_ordered_reason_codes(reasons)),
                "referenced_by": list(draft.referenced_by),
            }
        )

    promotion_queue: list[dict[str, Any]] = []
    for record in file_records:
        if record["priority_tier"] != "promoted":
            continue
        promotion_queue.append(
            {
                "queue_rank": len(promotion_queue) + 1,
                "path": record["path"],
                "content_sha256": record["content_sha256"],
                "size_bytes": record["size_bytes"],
                "current_read_depth": record["current_read_depth"],
                "recommended_read_depth": record["recommended_read_depth"],
                "priority_rank": record["priority_rank"],
                "priority_score": record["priority_score"],
                "deep_read_status": record["deep_read_status"],
                "referenced_by": list(record["referenced_by"]),
                "reason_codes": list(record["reason_codes"]),
            }
        )

    tier_counts = Counter(record["priority_tier"] for record in file_records)
    deep_counts = Counter(record["deep_read_status"] for record in file_records)
    reference_counts = Counter(record["reference_scan_status"] for record in file_records)
    total_bytes = sum(record["size_bytes"] for record in file_records)
    summary = {
        "manifest_file_count": len(file_records),
        "manifest_byte_count": total_bytes,
        "ranked_file_count": len(file_records),
        "reference_source_count": reference_counts.get("read", 0),
        "reference_bytes_read": sum(
            record["reference_bytes_read"] for record in file_records
        ),
        "resolved_reference_count": sum(
            len(record["referenced_by"]) for record in file_records
        ),
        "promotion_candidate_count": tier_counts.get("promoted", 0),
        "promotion_queue_count": len(promotion_queue),
        "deep_read_candidate_count": (
            deep_counts.get("selected", 0) + deep_counts.get("deferred", 0)
        ),
        "deep_read_selected_count": deep_counts.get("selected", 0),
        "deep_read_selected_bytes": sum(
            record["size_bytes"]
            for record in file_records
            if record["deep_read_status"] == "selected"
        ),
        "deep_read_deferred_count": deep_counts.get("deferred", 0),
        "limited_file_count": deep_counts.get("limited", 0),
        "priority_tiers": dict(sorted(tier_counts.items())),
        "deep_read_statuses": dict(sorted(deep_counts.items())),
        "reference_scan_statuses": dict(sorted(reference_counts.items())),
    }
    return {
        "schema_version": READING_PRIORITY_SCHEMA_VERSION,
        "kind": READING_PRIORITY_KIND,
        "priority_version": READING_PRIORITY_VERSION,
        "project_id": project_id,
        "manifest": {
            "manifest_version": manifest.manifest_version,
            "scan_generation": manifest.scan_generation,
            "file_count": len(file_records),
            "byte_count": total_bytes,
            "content_sha256": manifest_sha256,
        },
        "limits": _limits_payload(),
        "summary": summary,
        "files": file_records,
        "promotion_queue": promotion_queue,
    }

_KNOWN_FORMATS = frozenset(
    {
        "plain_text",
        "markdown",
        "restructured_text",
        "python",
        "r",
        "julia",
        "matlab",
        "c",
        "cpp",
        "java",
        "javascript",
        "typescript",
        "shell",
        "powershell",
        "batch",
        "rust",
        "go",
        "ruby",
        "perl",
        "sql",
        "html",
        "css",
        "xml",
        "json",
        "jsonl",
        "yaml",
        "toml",
        "ini",
        "csv",
        "tsv",
        "latex",
        "bibtex",
        "notebook",
        "log",
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "png",
        "jpeg",
        "gif",
        "tiff",
        "bmp",
        "svg",
        "wav",
        "mp3",
        "zip",
        "gzip",
        "tar",
        "parquet",
        "hdf5",
        "npy",
        "npz",
        "mat_data",
        "sqlite",
        "pytorch_checkpoint",
        "model_checkpoint",
        "pickle",
        "binary",
        "unknown",
    }
)
_FILE_FIELDS = frozenset(
    {
        "path",
        "content_sha256",
        "size_bytes",
        "format",
        "research_role",
        "processing_status",
        "current_read_depth",
        "priority_score",
        "priority_rank",
        "priority_tier",
        "recommended_read_depth",
        "deep_read_status",
        "reference_scan_status",
        "reference_bytes_read",
        "dataset_group",
        "reason_codes",
        "referenced_by",
    }
)
_QUEUE_FIELDS = frozenset(
    {
        "queue_rank",
        "path",
        "content_sha256",
        "size_bytes",
        "current_read_depth",
        "recommended_read_depth",
        "priority_rank",
        "priority_score",
        "deep_read_status",
        "referenced_by",
        "reason_codes",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "manifest_file_count",
        "manifest_byte_count",
        "ranked_file_count",
        "reference_source_count",
        "reference_bytes_read",
        "resolved_reference_count",
        "promotion_candidate_count",
        "promotion_queue_count",
        "deep_read_candidate_count",
        "deep_read_selected_count",
        "deep_read_selected_bytes",
        "deep_read_deferred_count",
        "limited_file_count",
        "priority_tiers",
        "deep_read_statuses",
        "reference_scan_statuses",
    }
)


def _validate_string_list(
    value: object,
    *,
    label: str,
    reason_codes: bool = False,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReadingPriorityError(f"{label} must be an array of strings")
    if value != list(dict.fromkeys(value)):
        raise ReadingPriorityError(f"{label} must not contain duplicates")
    if reason_codes:
        if not value or any(not _REASON_CODE_RE.fullmatch(item) for item in value):
            raise ReadingPriorityError(
                f"{label} must contain unique stable kebab-case reason codes"
            )
    elif value != sorted(value):
        raise ReadingPriorityError(f"{label} must be sorted")
    return value


def _validate_count_map(
    value: object,
    *,
    label: str,
    allowed: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ReadingPriorityError(f"{label} must be a count object")
    if list(value) != sorted(value):
        raise ReadingPriorityError(f"{label} keys must be sorted")
    result: dict[str, int] = {}
    for key, count in value.items():
        if key not in allowed or not _is_integer(count) or count <= 0:
            raise ReadingPriorityError(f"{label} contains an invalid count")
        result[key] = count
    return result


def _validate_priority_payload(
    payload: object,
    *,
    project_id: str | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReadingPriorityError("reading-priority artifact must be an object")
    top_fields = {
        "schema_version",
        "kind",
        "priority_version",
        "project_id",
        "manifest",
        "limits",
        "summary",
        "files",
        "promotion_queue",
    }
    if set(payload) != top_fields:
        raise ReadingPriorityError(
            "reading-priority artifact must contain exactly the v1 top-level fields"
        )
    if payload["schema_version"] != READING_PRIORITY_SCHEMA_VERSION:
        raise ReadingPriorityError("reading-priority schema_version is unsupported")
    if payload["kind"] != READING_PRIORITY_KIND:
        raise ReadingPriorityError("reading-priority kind is invalid")
    if payload["priority_version"] != READING_PRIORITY_VERSION:
        raise ReadingPriorityError("reading-priority version is unsupported")
    artifact_project_id = payload["project_id"]
    try:
        validate_project_id(artifact_project_id)
    except (TypeError, ValueError, LayoutError) as exc:
        raise ReadingPriorityError("reading-priority project_id is invalid") from exc
    if project_id is not None and artifact_project_id != project_id:
        raise ReadingPriorityError("reading-priority project identity is inconsistent")
    if payload["limits"] != _limits_payload():
        raise ReadingPriorityError("reading-priority limits do not match v1")

    manifest = payload["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "manifest_version",
        "scan_generation",
        "file_count",
        "byte_count",
        "content_sha256",
    }:
        raise ReadingPriorityError("reading-priority Manifest identity is malformed")
    if manifest["manifest_version"] != PROJECT_MANIFEST_VERSION:
        raise ReadingPriorityError("reading-priority requires Manifest v4")
    for field in ("scan_generation", "file_count", "byte_count"):
        value = manifest[field]
        minimum = 1 if field == "scan_generation" else 0
        if not _is_integer(value) or value < minimum:
            raise ReadingPriorityError(f"reading-priority manifest {field} is invalid")
    if not isinstance(manifest["content_sha256"], str) or not _SHA256_RE.fullmatch(
        manifest["content_sha256"]
    ):
        raise ReadingPriorityError("reading-priority Manifest hash is invalid")

    files = payload["files"]
    if not isinstance(files, list):
        raise ReadingPriorityError("reading-priority files must be an array")
    if len(files) != manifest["file_count"]:
        raise ReadingPriorityError("reading-priority file count does not reconcile")
    paths: set[str] = set()
    records_by_path: dict[str, dict[str, Any]] = {}
    for expected_rank, raw_record in enumerate(files, start=1):
        if not isinstance(raw_record, dict) or set(raw_record) != _FILE_FIELDS:
            raise ReadingPriorityError("reading-priority file record is malformed")
        path = _validated_relative_path(
            raw_record["path"],
            label="reading-priority file path",
        )
        if path in paths:
            raise ReadingPriorityError("reading-priority file paths must be unique")
        paths.add(path)
        records_by_path[path] = raw_record
        if (
            not isinstance(raw_record["content_sha256"], str)
            or not _SHA256_RE.fullmatch(raw_record["content_sha256"])
        ):
            raise ReadingPriorityError("reading-priority file hash is invalid")
        if not _is_integer(raw_record["size_bytes"]) or raw_record["size_bytes"] < 0:
            raise ReadingPriorityError("reading-priority file size is invalid")
        if raw_record["format"] not in _KNOWN_FORMATS:
            raise ReadingPriorityError("reading-priority file format is invalid")
        if raw_record["research_role"] not in _ROLE_ORDER:
            raise ReadingPriorityError("reading-priority research role is invalid")
        if raw_record["processing_status"] not in PROCESSING_STATUSES:
            raise ReadingPriorityError("reading-priority processing status is invalid")
        if raw_record["current_read_depth"] not in READ_DEPTHS:
            raise ReadingPriorityError("reading-priority current read depth is invalid")
        if raw_record["recommended_read_depth"] not in READ_DEPTHS:
            raise ReadingPriorityError("reading-priority recommendation is invalid")
        if (
            not _is_integer(raw_record["priority_score"])
            or raw_record["priority_score"] < 0
            or not _is_integer(raw_record["priority_rank"])
            or raw_record["priority_rank"] != expected_rank
        ):
            raise ReadingPriorityError("reading-priority rank or score is invalid")
        if raw_record["priority_tier"] not in _PRIORITY_TIERS:
            raise ReadingPriorityError("reading-priority tier is invalid")
        deep_status = raw_record["deep_read_status"]
        if deep_status not in _DEEP_READ_STATUSES:
            raise ReadingPriorityError("reading-priority deep-read status is invalid")
        reference_status = raw_record["reference_scan_status"]
        if reference_status not in _REFERENCE_SCAN_STATUSES:
            raise ReadingPriorityError("reading-priority reference status is invalid")
        reference_bytes = raw_record["reference_bytes_read"]
        if not _is_integer(reference_bytes) or not 0 <= reference_bytes <= raw_record["size_bytes"]:
            raise ReadingPriorityError("reading-priority reference byte count is invalid")
        if reference_status == "read":
            if reference_bytes != raw_record["size_bytes"]:
                raise ReadingPriorityError("read reference sources must reconcile bytes")
        elif reference_bytes != 0:
            raise ReadingPriorityError("unread reference sources must record zero bytes")
        group = raw_record["dataset_group"]
        if group is not None and group != "<project-root>":
            _validated_relative_path(group, label="reading-priority dataset group")
        reasons = _validate_string_list(
            raw_record["reason_codes"],
            label="reading-priority reason_codes",
            reason_codes=True,
        )
        references = _validate_string_list(
            raw_record["referenced_by"],
            label="reading-priority referenced_by",
        )
        if path in references:
            raise ReadingPriorityError("a reading-priority file cannot reference itself")
        expected_score, score_reasons = _priority_score_values(
            raw_record["research_role"],
            path,
            tuple(references),
        )
        if raw_record["priority_score"] != expected_score or any(
            reason not in reasons for reason in score_reasons
        ):
            raise ReadingPriorityError(
                "reading-priority score or score reasons are inconsistent"
            )
        expected_group = _dataset_group(path, raw_record["research_role"])
        if group != expected_group:
            raise ReadingPriorityError(
                "reading-priority dataset group is inconsistent"
            )
        if deep_status in {"selected", "deferred"}:
            if raw_record["recommended_read_depth"] != "deep_read":
                raise ReadingPriorityError("deep-read candidates must recommend deep_read")
        else:
            if raw_record["recommended_read_depth"] != raw_record["current_read_depth"]:
                raise ReadingPriorityError(
                    "non-candidates must preserve their current read depth"
                )
        if raw_record["priority_tier"] == "limited" and deep_status != "limited":
            raise ReadingPriorityError("limited priority files must remain limited")
        if deep_status == "limited" and raw_record["priority_tier"] != "limited":
            raise ReadingPriorityError("limited deep reads must use the limited tier")
        if raw_record["priority_tier"] == "promoted":
            if (
                not references
                or raw_record["current_read_depth"] not in _PROMOTABLE_READ_DEPTHS
                or deep_status not in {"selected", "deferred"}
                or "reference-promotion-candidate" not in reasons
            ):
                raise ReadingPriorityError("promoted file record is inconsistent")

    dataset_counts: Counter[str] = Counter()
    dataset_bytes: Counter[str] = Counter()
    for record in files:
        group = record["dataset_group"]
        if group is not None:
            dataset_counts[group] += 1
            dataset_bytes[group] += record["size_bytes"]
    large_dataset_groups = {
        group
        for group in dataset_counts
        if dataset_counts[group] >= LARGE_DATASET_MIN_FILES
        or dataset_bytes[group] >= LARGE_DATASET_MIN_BYTES
    }

    tier_order = {tier: index for index, tier in enumerate(_PRIORITY_TIERS)}
    expected_file_order = sorted(
        files,
        key=lambda record: (
            tier_order[record["priority_tier"]],
            -record["priority_score"],
            record["path"],
        ),
    )
    if [record["path"] for record in files] != [
        record["path"] for record in expected_file_order
    ]:
        raise ReadingPriorityError(
            "reading-priority files are not in deterministic rank order"
        )

    selected_count = 0
    selected_bytes = 0
    for record in files:
        intrinsic_limited_reason = _intrinsic_limited_reason(
            processing_status=record["processing_status"],
            current_read_depth=record["current_read_depth"],
            format_name=record["format"],
            research_role=record["research_role"],
            large_dataset=record["dataset_group"] in large_dataset_groups,
        )
        limited = record["priority_tier"] == "limited"
        if intrinsic_limited_reason is not None and (
            not limited or record["deep_read_status"] != "limited"
        ):
            raise ReadingPriorityError(
                "reading-priority intrinsically limited file was promoted"
            )
        promotion = (
            not limited
            and bool(record["referenced_by"])
            and record["current_read_depth"] in _PROMOTABLE_READ_DEPTHS
        )
        expected_tier = _priority_tier_values(
            record["research_role"],
            score=record["priority_score"],
            promotion_candidate=promotion,
            limited=limited,
        )
        if record["priority_tier"] != expected_tier:
            raise ReadingPriorityError(
                "reading-priority tier is inconsistent with its inputs"
            )
        candidate = (
            not limited
            and record["current_read_depth"] != "deep_read"
            and (promotion or record["research_role"] in _ORDINARY_DEEP_READ_ROLES)
        )
        if limited:
            expected_status = "limited"
        elif not candidate:
            expected_status = "not-candidate"
        elif (
            selected_count < DEEP_READ_MAX_FILES
            and selected_bytes + record["size_bytes"] <= DEEP_READ_MAX_TOTAL_BYTES
        ):
            expected_status = "selected"
            selected_count += 1
            selected_bytes += record["size_bytes"]
        else:
            expected_status = "deferred"
        if record["deep_read_status"] != expected_status:
            raise ReadingPriorityError(
                "reading-priority deep-read decision is inconsistent"
            )
        expected_recommendation = (
            "deep_read"
            if expected_status in {"selected", "deferred"}
            else record["current_read_depth"]
        )
        if record["recommended_read_depth"] != expected_recommendation:
            raise ReadingPriorityError(
                "reading-priority read-depth recommendation is inconsistent"
            )

    for record in files:
        for source_path in record["referenced_by"]:
            if source_path not in paths:
                raise ReadingPriorityError("referenced_by points outside the ranked files")
            if records_by_path[source_path]["reference_scan_status"] != "read":
                raise ReadingPriorityError("referenced_by source was not approved and read")

    total_bytes = sum(record["size_bytes"] for record in files)
    if total_bytes != manifest["byte_count"]:
        raise ReadingPriorityError("reading-priority byte count does not reconcile")
    selected = [record for record in files if record["deep_read_status"] == "selected"]
    if len(selected) > DEEP_READ_MAX_FILES or sum(
        record["size_bytes"] for record in selected
    ) > DEEP_READ_MAX_TOTAL_BYTES:
        raise ReadingPriorityError("reading-priority deep-read budget is exceeded")
    read_sources = [
        record for record in files if record["reference_scan_status"] == "read"
    ]
    if len(read_sources) > REFERENCE_SOURCE_MAX_FILES or any(
        record["size_bytes"] > REFERENCE_SOURCE_MAX_FILE_BYTES for record in read_sources
    ) or sum(record["reference_bytes_read"] for record in read_sources) > REFERENCE_SOURCE_MAX_TOTAL_BYTES:
        raise ReadingPriorityError("reading-priority reference-read budget is exceeded")

    queue = payload["promotion_queue"]
    if not isinstance(queue, list):
        raise ReadingPriorityError("reading-priority promotion_queue must be an array")
    queue_paths: list[str] = []
    for expected_rank, raw_entry in enumerate(queue, start=1):
        if not isinstance(raw_entry, dict) or set(raw_entry) != _QUEUE_FIELDS:
            raise ReadingPriorityError("reading-priority promotion queue entry is malformed")
        if (
            not _is_integer(raw_entry["queue_rank"])
            or raw_entry["queue_rank"] != expected_rank
        ):
            raise ReadingPriorityError("promotion queue ranks must be contiguous integers")
        path = _validated_relative_path(
            raw_entry["path"],
            label="promotion queue path",
        )
        if path in queue_paths:
            raise ReadingPriorityError("promotion queue paths must be unique")
        queue_paths.append(path)
        record = records_by_path.get(path)
        if (
            record is None
            or record["priority_tier"] != "promoted"
            or record["deep_read_status"] not in {"selected", "deferred"}
        ):
            raise ReadingPriorityError("promotion queue target is not a promotion candidate")
        expected = {
            "queue_rank": expected_rank,
            "path": record["path"],
            "content_sha256": record["content_sha256"],
            "size_bytes": record["size_bytes"],
            "current_read_depth": record["current_read_depth"],
            "recommended_read_depth": record["recommended_read_depth"],
            "priority_rank": record["priority_rank"],
            "priority_score": record["priority_score"],
            "deep_read_status": record["deep_read_status"],
            "referenced_by": record["referenced_by"],
            "reason_codes": record["reason_codes"],
        }
        if raw_entry != expected:
            raise ReadingPriorityError("promotion queue entry does not match its file")
    expected_queue_paths = [
        record["path"]
        for record in files
        if record["priority_tier"] == "promoted"
    ]
    if queue_paths != expected_queue_paths:
        raise ReadingPriorityError("promotion queue does not contain every promotion candidate")

    summary = payload["summary"]
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_FIELDS:
        raise ReadingPriorityError("reading-priority summary is malformed")
    scalar_fields = _SUMMARY_FIELDS - {
        "priority_tiers",
        "deep_read_statuses",
        "reference_scan_statuses",
    }
    if any(not _is_integer(summary[field]) or summary[field] < 0 for field in scalar_fields):
        raise ReadingPriorityError("reading-priority summary count is invalid")
    tier_counts = Counter(record["priority_tier"] for record in files)
    deep_counts = Counter(record["deep_read_status"] for record in files)
    reference_counts = Counter(record["reference_scan_status"] for record in files)
    expected_summary = {
        "manifest_file_count": len(files),
        "manifest_byte_count": total_bytes,
        "ranked_file_count": len(files),
        "reference_source_count": reference_counts.get("read", 0),
        "reference_bytes_read": sum(record["reference_bytes_read"] for record in files),
        "resolved_reference_count": sum(len(record["referenced_by"]) for record in files),
        "promotion_candidate_count": tier_counts.get("promoted", 0),
        "promotion_queue_count": len(queue),
        "deep_read_candidate_count": deep_counts.get("selected", 0) + deep_counts.get("deferred", 0),
        "deep_read_selected_count": deep_counts.get("selected", 0),
        "deep_read_selected_bytes": sum(record["size_bytes"] for record in selected),
        "deep_read_deferred_count": deep_counts.get("deferred", 0),
        "limited_file_count": deep_counts.get("limited", 0),
        "priority_tiers": dict(sorted(tier_counts.items())),
        "deep_read_statuses": dict(sorted(deep_counts.items())),
        "reference_scan_statuses": dict(sorted(reference_counts.items())),
    }
    if summary != expected_summary:
        raise ReadingPriorityError("reading-priority summary does not reconcile")
    # Exercise closed enums and stable sorted count maps even when equality passed.
    _validate_count_map(summary["priority_tiers"], label="priority_tiers", allowed=_PRIORITY_TIERS)
    _validate_count_map(summary["deep_read_statuses"], label="deep_read_statuses", allowed=_DEEP_READ_STATUSES)
    _validate_count_map(summary["reference_scan_statuses"], label="reference_scan_statuses", allowed=_REFERENCE_SCAN_STATUSES)
    return payload

def load_reading_priority(
    priority_file: str | Path,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Strictly load one current B-07 artifact; legacy/future state fails closed."""

    unresolved_path = Path(priority_file).expanduser()
    if unresolved_path.is_symlink():
        raise ReadingPriorityError(
            f"reading-priority artifact must not be a symbolic link: {unresolved_path}"
        )
    path = unresolved_path.resolve()
    if path.is_symlink():
        raise ReadingPriorityError(
            f"reading-priority artifact must not be a symbolic link: {path}"
        )
    if not path.is_file():
        raise ReadingPriorityError(f"reading-priority artifact is not a file: {path}")
    try:
        document = parse_versioned_json_bytes(
            path.read_bytes(),
            path=path,
            allow_legacy=True,
            max_supported=READING_PRIORITY_SCHEMA_VERSION,
        )
    except LayoutError:
        raise
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not read reading-priority artifact {path}: {exc}"
        ) from exc
    if document.is_legacy:
        raise ReadingPriorityError(
            f"reading-priority artifact is legacy v0 and will not be rewritten: {path}"
        )
    return _validate_priority_payload(document.data, project_id=project_id)


def _validate_machine_state_path(
    layout: ProjectLayout,
    path: Path,
    *,
    allow_missing_leaf: bool,
) -> Path:
    try:
        return layout.validate_machine_state_path(
            path,
            leaf_kind="file",
            allow_missing_leaf=allow_missing_leaf,
        )
    except LayoutError as exc:
        raise ReadingPriorityError(
            f"unsafe project machine-state path {path}: {exc}"
        ) from exc


def _acquire_current_project_lock(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float,
) -> tuple[ProjectRegistrationResult, AdvisoryFileLock]:
    """Validate, acquire, and revalidate the shared project-state lock."""

    registration = load_registered_project(workspace_root, project_id)
    layout = registration.layout
    _validate_machine_state_path(
        layout,
        layout.project_file,
        allow_missing_leaf=False,
    )
    _validate_machine_state_path(
        layout,
        layout.machine_state_lock_file,
        allow_missing_leaf=True,
    )
    lock = AdvisoryFileLock(
        layout.machine_state_lock_file,
        timeout_seconds=lock_timeout_seconds,
    )
    try:
        lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise ReadingPriorityError(
            "timed out waiting for the project machine-state lock"
        ) from exc
    except AdvisoryLockError as exc:
        raise ReadingPriorityError(
            "could not acquire the project machine-state lock"
        ) from exc

    try:
        _validate_machine_state_path(
            layout,
            layout.machine_state_lock_file,
            allow_missing_leaf=False,
        )
        current = load_registered_project(workspace_root, project_id)
        if current.layout.machine_root != layout.machine_root:
            raise ReadingPriorityError(
                "registered project machine-state root changed while locking"
            )
        _validate_machine_state_path(
            current.layout,
            current.layout.project_file,
            allow_missing_leaf=False,
        )
        _validate_machine_state_path(
            current.layout,
            current.layout.machine_state_lock_file,
            allow_missing_leaf=False,
        )
        return current, lock
    except BaseException:
        lock.release()
        raise


def _load_manifest_snapshot(
    registration: ProjectRegistrationResult,
) -> tuple[ProjectManifest, bytes, str]:
    layout = registration.layout
    manifest_path = layout.manifest_file
    _validate_machine_state_path(
        layout,
        manifest_path,
        allow_missing_leaf=False,
    )
    try:
        snapshot_before = manifest_path.read_bytes()
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not snapshot current Manifest {manifest_path}: {exc}"
        ) from exc
    manifest = load_project_manifest(
        manifest_path,
        project_id=registration.project_id,
        project_root=registration.project_root,
        required_manifest_version=PROJECT_MANIFEST_VERSION,
    )
    try:
        snapshot = manifest.manifest_file.read_bytes()
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not snapshot current Manifest {manifest.manifest_file}: {exc}"
        ) from exc
    _validate_machine_state_path(
        layout,
        manifest.manifest_file,
        allow_missing_leaf=False,
    )
    if snapshot != snapshot_before:
        raise ReadingPriorityError(
            "Manifest changed while it was loaded; retry inventory"
        )
    return manifest, snapshot, hashlib.sha256(snapshot).hexdigest()


def _manifest_identity(
    manifest: ProjectManifest,
    manifest_sha256: str,
) -> dict[str, Any]:
    file_records = manifest.file_records
    return {
        "manifest_version": manifest.manifest_version,
        "scan_generation": manifest.scan_generation,
        "file_count": len(file_records),
        "byte_count": sum(record["size_bytes"] for record in file_records),
        "content_sha256": manifest_sha256,
    }


def _validate_priority_against_current(
    priority: dict[str, Any],
    *,
    manifest: ProjectManifest,
    manifest_snapshot: bytes,
    manifest_sha256: str,
    policy: ScanPolicy,
    infos: tuple[_FileInfo, ...],
) -> None:
    """Ground structural recommendations in the exact current Manifest/policy."""

    if priority["manifest"] != _manifest_identity(manifest, manifest_sha256):
        raise ReadingPriorityError(
            "reading-priority artifact is stale for the current Manifest"
        )
    records_by_path = {record["path"]: record for record in priority["files"]}
    infos_by_path = {info.path: info for info in infos}
    if set(records_by_path) != set(infos_by_path):
        raise ReadingPriorityError(
            "reading-priority files do not match the current Manifest"
        )

    grounded_fields = (
        "content_sha256",
        "size_bytes",
        "format",
        "research_role",
        "processing_status",
        "current_read_depth",
        "dataset_group",
    )
    for path, info in infos_by_path.items():
        record = records_by_path[path]
        expected = {
            "content_sha256": info.content_sha256,
            "size_bytes": info.size_bytes,
            "format": info.format,
            "research_role": info.research_role,
            "processing_status": info.processing_status,
            "current_read_depth": info.current_read_depth,
            "dataset_group": info.dataset_group,
        }
        if any(record[field] != expected[field] for field in grounded_fields):
            raise ReadingPriorityError(
                f"reading-priority file is stale for the current Manifest: {path}"
            )

        decision = policy.decide_file(path, size_bytes=info.size_bytes)
        if (
            decision.local_content_access != info.local_content_access
            or decision.local_reason_code != info.local_reason_code
        ):
            raise ReadingPriorityError(
                f"reading-priority policy grounding changed unexpectedly: {path}"
            )
        limited_reason = _limited_reason(info)
        if limited_reason is not None:
            if (
                record["priority_tier"] != "limited"
                or record["deep_read_status"] != "limited"
                or record["recommended_read_depth"] != info.current_read_depth
                or limited_reason not in record["reason_codes"]
            ):
                raise ReadingPriorityError(
                    f"reading-priority selected a currently limited file: {path}"
                )
        elif (
            record["priority_tier"] == "limited"
            or record["deep_read_status"] == "limited"
        ):
            raise ReadingPriorityError(
                f"reading-priority limitation is stale for the current policy: {path}"
            )

    try:
        if manifest.manifest_file.read_bytes() != manifest_snapshot:
            raise ReadingPriorityError(
                "Manifest changed while reading priority was validated; retry inventory"
            )
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not revalidate current Manifest {manifest.manifest_file}: {exc}"
        ) from exc
    _scan_policy_for_manifest(manifest)


def _load_current_reading_priority_locked(
    registration: ProjectRegistrationResult,
    *,
    manifest: ProjectManifest | None = None,
    manifest_snapshot: bytes | None = None,
    manifest_sha256: str | None = None,
    policy: ScanPolicy | None = None,
    infos: tuple[_FileInfo, ...] | None = None,
) -> dict[str, Any]:
    if manifest is None:
        manifest, manifest_snapshot, manifest_sha256 = _load_manifest_snapshot(
            registration
        )
    if manifest_snapshot is None or manifest_sha256 is None:
        raise ReadingPriorityError("current Manifest snapshot is incomplete")
    if policy is None:
        policy = _scan_policy_for_manifest(manifest)
    if infos is None:
        infos = _build_file_infos(manifest, policy)

    priority_file = registration.layout.reading_priority_file
    _validate_machine_state_path(
        registration.layout,
        priority_file,
        allow_missing_leaf=False,
    )
    priority = load_reading_priority(
        priority_file,
        project_id=registration.project_id,
    )
    _validate_machine_state_path(
        registration.layout,
        priority_file,
        allow_missing_leaf=False,
    )
    _validate_priority_against_current(
        priority,
        manifest=manifest,
        manifest_snapshot=manifest_snapshot,
        manifest_sha256=manifest_sha256,
        policy=policy,
        infos=infos,
    )
    _validate_machine_state_path(
        registration.layout,
        priority_file,
        allow_missing_leaf=False,
    )
    return priority


def load_current_reading_priority(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Load execution-authorized B-07 state grounded in current Manifest truth."""

    registration, lock = _acquire_current_project_lock(
        workspace_root,
        project_id,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    try:
        return _load_current_reading_priority_locked(registration)
    finally:
        lock.release()


def _write_atomic_json(
    path: Path,
    payload: dict[str, Any],
    *,
    before_replace: Callable[[], None] | None = None,
    machine_layout: ProjectLayout | None = None,
) -> None:
    if machine_layout is not None:
        _validate_machine_state_path(
            machine_layout,
            path,
            allow_missing_leaf=True,
        )
    if not path.parent.is_dir():
        raise ReadingPriorityError(
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
        if machine_layout is not None:
            _validate_machine_state_path(
                machine_layout,
                temporary,
                allow_missing_leaf=False,
            )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            descriptor = -1
            target.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        if before_replace is not None:
            before_replace()
        if machine_layout is not None:
            _validate_machine_state_path(
                machine_layout,
                temporary,
                allow_missing_leaf=False,
            )
            _validate_machine_state_path(
                machine_layout,
                path,
                allow_missing_leaf=True,
            )
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not write reading-priority artifact {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            safe_to_unlink = True
            if machine_layout is not None:
                try:
                    _validate_machine_state_path(
                        machine_layout,
                        temporary,
                        allow_missing_leaf=False,
                    )
                except ReadingPriorityError:
                    safe_to_unlink = False
            if safe_to_unlink:
                temporary.unlink(missing_ok=True)


def _generate_reading_priority_locked(
    registration: ProjectRegistrationResult,
) -> ReadingPriorityResult:
    manifest, manifest_snapshot, manifest_sha256 = _load_manifest_snapshot(
        registration
    )
    policy = _scan_policy_for_manifest(manifest)
    infos = _build_file_infos(manifest, policy)
    reference_statuses, reference_bytes, referenced_by = _scan_references(
        infos,
        project_root=registration.project_root,
        case_sensitive=policy.case_sensitive,
        before_reference_read=lambda: _scan_policy_for_manifest(manifest),
    )
    read_reference_infos = tuple(
        info for info in infos if reference_statuses[info.path] == "read"
    )
    drafts = _build_priority_drafts(
        infos,
        reference_statuses=reference_statuses,
        reference_bytes=reference_bytes,
        referenced_by_map=referenced_by,
    )
    priority = _materialize_priority(
        project_id=registration.project_id,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        drafts=drafts,
    )
    _validate_priority_payload(priority, project_id=registration.project_id)
    try:
        if manifest.manifest_file.read_bytes() != manifest_snapshot:
            raise ReadingPriorityError(
                "Manifest changed while reading priority was generated; retry inventory"
            )
    except OSError as exc:
        raise ReadingPriorityError(
            f"could not revalidate current Manifest {manifest.manifest_file}: {exc}"
        ) from exc
    _validate_machine_state_path(
        registration.layout,
        manifest.manifest_file,
        allow_missing_leaf=False,
    )
    _scan_policy_for_manifest(manifest)

    priority_file = registration.layout.reading_priority_file
    _validate_machine_state_path(
        registration.layout,
        priority_file,
        allow_missing_leaf=True,
    )
    if priority_file.exists() or priority_file.is_symlink():
        existing = load_reading_priority(
            priority_file,
            project_id=registration.project_id,
        )
        # A valid artifact from an older Manifest is replaceable but never
        # executable. A current artifact must pass full Manifest/policy grounding
        # before this writer is allowed to replace it.
        if existing["manifest"] == _manifest_identity(manifest, manifest_sha256):
            _load_current_reading_priority_locked(
                registration,
                manifest=manifest,
                manifest_snapshot=manifest_snapshot,
                manifest_sha256=manifest_sha256,
                policy=policy,
                infos=infos,
            )

    def revalidate_before_replace() -> None:
        for info in read_reference_infos:
            _scan_policy_for_manifest(manifest)
            _read_verified_reference_source(info, registration.project_root)
        try:
            if manifest.manifest_file.read_bytes() != manifest_snapshot:
                raise ReadingPriorityError(
                    "Manifest changed before reading priority was committed; retry inventory"
                )
        except OSError as exc:
            raise ReadingPriorityError(
                f"could not revalidate current Manifest {manifest.manifest_file}: {exc}"
            ) from exc
        _validate_machine_state_path(
            registration.layout,
            manifest.manifest_file,
            allow_missing_leaf=False,
        )
        _scan_policy_for_manifest(manifest)

    _write_atomic_json(
        priority_file,
        priority,
        before_replace=revalidate_before_replace,
        machine_layout=registration.layout,
    )
    return ReadingPriorityResult(
        project_id=registration.project_id,
        manifest_file=manifest.manifest_file,
        priority_file=priority_file,
        priority=priority,
    )


def generate_reading_priority(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ReadingPriorityResult:
    """Generate one deterministic priority artifact under the shared state lock."""

    registration, lock = _acquire_current_project_lock(
        workspace_root,
        project_id,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    try:
        return _generate_reading_priority_locked(registration)
    finally:
        lock.release()
