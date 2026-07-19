#!/usr/bin/env python3
"""Loopback-only product research cockpit with optional controlled editing.

This module is deliberately separate from ``tools.development_dashboard``. The
latter supervises repository development; this module exposes bounded views of
registered research projects, machine evidence, and curated Knowledge Schema
pages. J-04 may explicitly enable four fixed mixed-page user-region edits, each
routed through F-05A/F-05B. It never scans, opens, or writes the registered
source project and never exposes arbitrary filesystem mutation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import sys
import unicodedata
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlsplit

if __package__:
    from .controlled_markdown import (
        ControlledMarkdownError,
        parse_mixed_markdown_body,
        plan_controlled_markdown_update,
    )
    from .controlled_markdown_persistence import (
        ControlledMarkdownAuthorizationError,
        ControlledMarkdownCommitAuditUnknownError,
        ControlledMarkdownCommitUnknownError,
        ControlledMarkdownPersistenceError,
        ControlledMarkdownWriteConflictError,
        ControlledMarkdownWriteFailedError,
        ControlledMarkdownWriteRejectedError,
        TrustedHostSessionContext,
        bind_controlled_markdown_authorization,
        persist_controlled_markdown_update,
    )
    from .coverage_report import build_coverage_report
    from .evidence_registry import EvidenceRegistry, load_evidence_registry
    from .file_classification import classification_from_dict
    from .file_state import file_state_from_dict
    from .knowledge_artifacts import (
        KNOWLEDGE_SCHEMA_VERSION,
        artifact_contract_for_path,
        parse_knowledge_page,
        serialize_knowledge_frontmatter,
    )
    from .knowledge_renderer import MIXED_REGION_IDS, PRODUCT_ARTIFACT_SPECS
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )
    from .project_layout import LayoutError, ProjectLayout, validate_project_id
    from .project_registry import (
        ProjectNotRegisteredError,
        ProjectRecordError,
        ProjectRegistrationResult,
        load_registered_project,
    )
    from .project_runs import load_project_run, validate_run_id
    from .research_goals import Goal, parse_goal
    from .research_planning import InitialPlan, parse_initial_plan
    from .research_state import ProjectState, parse_project_state
    from .research_tasks import TaskCollection, parse_task_collection
    from .source_registry import SourceRecord, SourceRegistry, load_source_registry
    from .stable_file_access import StableFileAccessError, read_stable_regular_file
else:  # pragma: no cover - direct script execution
    # Keep direct ``python tools/research_cockpit.py`` invocation compatible
    # with sibling modules that import through the repository package.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.controlled_markdown import (  # type: ignore[no-redef]
        ControlledMarkdownError,
        parse_mixed_markdown_body,
        plan_controlled_markdown_update,
    )
    from tools.controlled_markdown_persistence import (  # type: ignore[no-redef]
        ControlledMarkdownAuthorizationError,
        ControlledMarkdownCommitAuditUnknownError,
        ControlledMarkdownCommitUnknownError,
        ControlledMarkdownPersistenceError,
        ControlledMarkdownWriteConflictError,
        ControlledMarkdownWriteFailedError,
        ControlledMarkdownWriteRejectedError,
        TrustedHostSessionContext,
        bind_controlled_markdown_authorization,
        persist_controlled_markdown_update,
    )
    from tools.coverage_report import build_coverage_report  # type: ignore[no-redef]
    from tools.evidence_registry import EvidenceRegistry, load_evidence_registry  # type: ignore[no-redef]
    from tools.file_classification import classification_from_dict  # type: ignore[no-redef]
    from tools.file_state import file_state_from_dict  # type: ignore[no-redef]
    from tools.knowledge_artifacts import (
        KNOWLEDGE_SCHEMA_VERSION,
        artifact_contract_for_path,
        parse_knowledge_page,
        serialize_knowledge_frontmatter,
    )  # type: ignore[no-redef]
    from tools.knowledge_renderer import (
        MIXED_REGION_IDS,
        PRODUCT_ARTIFACT_SPECS,
    )  # type: ignore[no-redef]
    from tools.project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectManifest,
        load_project_manifest,
    )  # type: ignore[no-redef]
    from tools.project_layout import LayoutError, ProjectLayout, validate_project_id  # type: ignore[no-redef]
    from tools.project_registry import (  # type: ignore[no-redef]
        ProjectNotRegisteredError,
        ProjectRecordError,
        ProjectRegistrationResult,
        load_registered_project,
    )
    from tools.project_runs import load_project_run, validate_run_id  # type: ignore[no-redef]
    from tools.research_goals import Goal, parse_goal  # type: ignore[no-redef]
    from tools.research_planning import InitialPlan, parse_initial_plan  # type: ignore[no-redef]
    from tools.research_state import ProjectState, parse_project_state  # type: ignore[no-redef]
    from tools.research_tasks import TaskCollection, parse_task_collection  # type: ignore[no-redef]
    from tools.source_registry import SourceRecord, SourceRegistry, load_source_registry  # type: ignore[no-redef]
    from tools.stable_file_access import StableFileAccessError, read_stable_regular_file  # type: ignore[no-redef]

SCHEMA_VERSION = 1
KIND = "llmwiki-research-cockpit"
SNAPSHOT_VERSION = "research-cockpit-v1"
DEFAULT_ASSET_ROOT = (
    Path(__file__).resolve().parent.parent / "assets" / "research-cockpit"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_MACHINE_BYTES = 8 * 1024 * 1024
MAX_PROJECTS = 512
MAX_FILES = 100_000
MAX_CLAIMS = 10_000
MAX_RUNS = 2_000
MAX_DAILY_PLANS = 366
MAX_PAGE_LIST = 500
MAX_REQUEST_TARGET_CHARS = 8_192
MAX_QUERY_CHARS = 4_096
MAX_QUERY_FIELDS = 8
MAX_QUERY_KEY_CHARS = 64
MAX_QUERY_VALUE_CHARS = 2_048
MAX_EDIT_REQUEST_BYTES = 512 * 1024
MAX_EDIT_CONTENT_BYTES = 384 * 1024
EDIT_TOKEN_HEADER = "X-LLMWiki-Edit-Token"
EDIT_SESSION_PATH = "/api/edit-session"

_STATUS_LABELS = {
    "draft": "DRAFT",
    "verified": "VERIFIED",
    "stale": "STALE",
    "conflicting": "CONFLICTING",
    "rejected": "REJECTED",
}
_DATE_PLAN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ControlledEditTarget:
    """One fixed J-04 Web target bound to one mixed-page user region."""

    key: str
    label: str
    path: str
    region_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "path": self.path,
            "region_id": self.region_id,
        }


CONTROLLED_EDIT_TARGETS: tuple[ControlledEditTarget, ...] = (
    ControlledEditTarget("goal", "Goal", "goals.md", "user-goals"),
    ControlledEditTarget("backlog", "Task backlog", "plans/backlog.md", "user-backlog"),
    ControlledEditTarget(
        "project_status", "Project status", "status.md", "user-status"
    ),
    ControlledEditTarget(
        "user_confirmed_conclusions",
        "User-confirmed conclusions",
        "claims/index.md",
        "user-confirmed-claims",
    ),
)
_EDIT_TARGETS_BY_KEY = {target.key: target for target in CONTROLLED_EDIT_TARGETS}


class ResearchCockpitError(RuntimeError):
    """Base error for safe cockpit construction and serving."""


class CockpitProjectUnavailable(ResearchCockpitError):
    """A registered project cannot currently be loaded safely."""


class CockpitPathError(ResearchCockpitError):
    """A requested project-relative path is not safe."""


class CockpitAssetError(ResearchCockpitError):
    """A fixed local asset is missing or unsafe."""


class CockpitEditingError(ResearchCockpitError):
    """Body-free bounded error for the optional controlled-edit surface."""

    reason_code = "controlled-edit-rejected"
    http_status = 409

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "llmwiki-research-cockpit-edit-rejection",
            "error": "controlled_edit_rejected",
            "reason_code": self.reason_code,
        }


class CockpitEditingUnavailable(CockpitEditingError):
    reason_code = "editing-unavailable"
    http_status = 405


class CockpitEditTargetUnavailable(CockpitEditingError):
    reason_code = "edit-target-unavailable"
    http_status = 409


class CockpitEditRequestError(CockpitEditingError):
    reason_code = "invalid-edit-request"
    http_status = 400


class CockpitEditConflict(CockpitEditingError):
    reason_code = "revision-conflict"
    http_status = 409

    def __init__(
        self,
        *,
        expected_current_sha256: str | None,
        observed_current_sha256: str | None,
    ) -> None:
        super().__init__("controlled edit revision conflict")
        self.expected_current_sha256 = expected_current_sha256
        self.observed_current_sha256 = observed_current_sha256

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload.update(
            {
                "expected_current_sha256": self.expected_current_sha256,
                "observed_current_sha256": self.observed_current_sha256,
                "page_commit_state": "not-committed",
            }
        )
        return payload


@dataclass(frozen=True)
class _Gap:
    code: str
    message: str
    path: str | None = None
    status: str = "DRAFT"

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": self.status,
            "status_code": self.status.casefold(),
            "reason_code": self.code,
            "reason": self.message,
        }
        if self.path is not None:
            value["path"] = self.path
        return value


@dataclass(frozen=True)
class _Page:
    path: str
    title: str
    artifact_type: str
    status: str
    ownership: str
    schema_version: int
    generated_at: str
    updated_at: str
    last_verified_at: str | None
    source_ids: tuple[str, ...]
    evidence_refs: tuple[dict[str, str], ...]
    body: str


@dataclass(frozen=True)
class _ProjectContext:
    registration: ProjectRegistrationResult
    manifest: ProjectManifest | None
    coverage: dict[str, Any] | None
    source_registry: SourceRegistry | None
    evidence_registry: EvidenceRegistry | None
    gaps: tuple[_Gap, ...]


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _message(code: str) -> str:
    return {
        "project-record-invalid": "The registered project record is unavailable or invalid.",
        "manifest-missing": "No current project inventory is available yet.",
        "manifest-invalid": "The current project inventory could not be validated.",
        "coverage-unavailable": "Coverage cannot be reconciled from the current inventory.",
        "sources-missing": "No Source registry is available for this project.",
        "sources-invalid": "The Source registry could not be validated.",
        "evidence-missing": "No Evidence registry is available for this project.",
        "evidence-invalid": "The Evidence registry could not be validated.",
        "knowledge-missing": "This curated page has not been generated yet.",
        "knowledge-invalid": "This curated page is present but failed strict validation.",
        "knowledge-unsafe": "This curated path is present but is not a regular non-redirected file.",
        "machine-artifact-missing": "This machine artifact has not been generated yet.",
        "machine-artifact-invalid": "This machine artifact failed strict validation.",
        "daily-plan-missing": "No daily plan is available for this date.",
        "daily-plan-directory-unsafe": "The daily-plan directory is present but unsafe.",
        "runs-directory-unsafe": "The run-history directory is present but unsafe.",
        "run-invalid": "This run record failed strict validation.",
    }.get(code, "This product data is not currently available.")


def _is_redirection(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        flag and getattr(metadata, "st_file_attributes", 0) & flag
    )


def _regular_file_state(target: Path) -> str:
    """Classify a leaf without treating an unsafe present entry as missing."""

    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    if _is_redirection(metadata):
        return "redirected"
    if not stat.S_ISREG(metadata.st_mode):
        return "non-regular"
    return "regular"


def _path_exists_regular(target: Path) -> bool:
    return _regular_file_state(target) == "regular"


def _directory_state(trusted_root: Path, target: Path) -> str:
    """Classify a directory path without following redirected ancestors."""

    root = Path(trusted_root)
    candidate = Path(target)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "unsafe"
    current = root
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unsafe"
        if _is_redirection(metadata) or not stat.S_ISDIR(metadata.st_mode):
            return "unsafe"
    return "directory"


def _safe_directory(trusted_root: Path, target: Path) -> bool:
    """Reject missing, non-directory, or redirected roots and descendants."""

    return _directory_state(trusted_root, target) == "directory"


def _read_bounded(trusted_root: Path, target: Path, *, maximum: int) -> bytes:
    try:
        metadata = os.lstat(target)
    except (FileNotFoundError, OSError) as exc:
        raise ResearchCockpitError("file is unavailable") from exc
    if _is_redirection(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ResearchCockpitError("file is not a regular non-redirected file")
    if metadata.st_size > maximum:
        raise ResearchCockpitError("file exceeds the cockpit display limit")
    try:
        observation = read_stable_regular_file(
            trusted_root, target, reject_redirection=True, capture_bytes=True
        )
    except (StableFileAccessError, OSError) as exc:
        raise ResearchCockpitError("stable file read failed") from exc
    if observation.data is None or len(observation.data) > maximum:
        raise ResearchCockpitError("bounded file read failed")
    return observation.data


def _project_roots(
    registration: ProjectRegistrationResult,
) -> tuple[tuple[Path, str], ...]:
    layout = registration.layout
    return (
        (registration.project_root, "source-project-root"),
        (layout.machine_root, "machine-state-root"),
        (layout.knowledge_root, "knowledge-root"),
        (layout.knowledge_projects_root, "knowledge-parent"),
        (layout.workspace_root, "workspace-root"),
    )


def _root_pattern(root: Path) -> re.Pattern[str]:
    # Match Windows paths case-insensitively and accept mixed or JSON-escaped
    # separators.  POSIX paths remain case-sensitive.
    pieces = [
        r"[\\/]+" if char in {"\\", "/"} else re.escape(char) for char in str(root)
    ]
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("".join(pieces), flags)


def _redact_text(text: str, roots: Sequence[tuple[Path, str]]) -> str:
    result = text
    ordered = sorted(roots, key=lambda item: len(str(item[0])), reverse=True)
    for root, label in ordered:
        raw = str(root)
        if raw:
            result = _root_pattern(root).sub(f"[{label}]", result)
    return result


def _contains_root(text: str, root: Path) -> bool:
    return bool(str(root)) and _root_pattern(root).search(text) is not None


def _redact(value: Any, roots: Sequence[tuple[Path, str]]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, roots)
    if isinstance(value, (list, tuple)):
        return [_redact(item, roots) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item, roots) for key, item in value.items()}
    return value


def _public_status(status: str) -> tuple[str, str]:
    code = status if status in _STATUS_LABELS else "draft"
    return _STATUS_LABELS[code], code


def _valid_page_path(value: str, *, allow_daily: bool = True) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CockpitPathError("invalid knowledge path")
    if unicodedata.normalize("NFC", value) != value:
        raise CockpitPathError("invalid knowledge path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(value) > 512
    ):
        raise CockpitPathError("invalid knowledge path")
    if allow_daily and value.startswith("plans/daily/"):
        if not _DATE_PLAN_RE.fullmatch(path.name):
            raise CockpitPathError("invalid daily plan path")
        return value
    try:
        artifact_contract_for_path(value)
    except Exception as exc:
        raise CockpitPathError("path is outside the knowledge contract") from exc
    return value


def _path_url(project_id: str, path: str) -> str:
    return f"/api/projects/{quote(project_id, safe='')}/knowledge?path={quote(path, safe='')}"


def _read_machine(layout: ProjectLayout, target: Path) -> bytes:
    layout.validate_machine_state_path(
        target, leaf_kind="file", allow_missing_leaf=False
    )
    return _read_bounded(layout.machine_root, target, maximum=MAX_MACHINE_BYTES)


def _read_knowledge(layout: ProjectLayout, relative_path: str) -> bytes:
    _valid_page_path(relative_path)
    target = layout.knowledge_root.joinpath(*PurePosixPath(relative_path).parts)
    return _read_bounded(layout.knowledge_root, target, maximum=MAX_PAGE_BYTES)


def _next_utc_timestamp(current: str) -> str:
    """Return a whole-second UTC timestamp strictly after ``current``."""

    normalized = current[:-1] + "+00:00" if current.endswith("Z") else current
    try:
        current_value = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:  # The strict page parser should make this unreachable.
        raise CockpitEditTargetUnavailable("invalid current updated_at") from exc
    if current_value.tzinfo is None:
        raise CockpitEditTargetUnavailable("invalid current updated_at")
    current_value = current_value.astimezone(dt.timezone.utc)
    candidate = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if candidate <= current_value:
        candidate = current_value + dt.timedelta(seconds=1)
    return candidate.isoformat().replace("+00:00", "Z")


def _edit_target(value: str) -> ControlledEditTarget:
    if type(value) is not str or len(value) > 128:
        raise CockpitEditRequestError("invalid controlled edit target")
    target = _EDIT_TARGETS_BY_KEY.get(value)
    if target is None:
        raise KeyError(value)
    expected = MIXED_REGION_IDS.get(target.path)
    if expected != (target.region_id,):
        raise ResearchCockpitError("controlled edit mapping is inconsistent")
    return target


def _validate_edit_content(
    value: object,
    *,
    roots: Sequence[tuple[Path, str]],
) -> str:
    if type(value) is not str:
        raise CockpitEditRequestError("controlled edit content must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise CockpitEditRequestError("controlled edit content is not UTF-8") from exc
    if len(encoded) > MAX_EDIT_CONTENT_BYTES:
        raise CockpitEditRequestError("controlled edit content exceeds its limit")
    if "\x00" in value or "llmwiki:user-region:" in value:
        raise CockpitEditRequestError("controlled edit content is structurally invalid")
    if value and not value.endswith("\n"):
        raise CockpitEditRequestError(
            "controlled edit content must end with LF so the protected marker remains delimited"
        )
    if any(_contains_root(value, root) for root, _label in roots):
        raise CockpitEditRequestError("controlled edit content contains a local root")
    return value


def _persistence_error_payload(
    exc: ControlledMarkdownPersistenceError,
) -> dict[str, Any]:
    """Project only body-free F-05B state into the Web error contract."""

    source = exc.as_dict()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "llmwiki-research-cockpit-edit-rejection",
        "error": "controlled_edit_rejected",
        "reason_code": source.get(
            "reason_code", "controlled-markdown-persistence-error"
        ),
    }
    for key in (
        "transaction_id",
        "page_commit_state",
        "audit_commit_state",
        "expected_current_sha256",
        "observed_current_sha256",
        "observed_after_sha256",
    ):
        if key in source:
            payload[key] = source[key]
    return payload


class _DuplicateJsonKeyError(ValueError):
    """Raised when an edit request contains a duplicate JSON object key."""


def _strict_json_object(value: bytes) -> dict[str, Any]:
    """Decode the bounded edit request without permissive JSON extensions."""

    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CockpitEditRequestError("edit request is not strict UTF-8 JSON") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _DuplicateJsonKeyError(key)
            result[key] = item
        return result

    def reject_constant(_value: str) -> Any:
        raise ValueError("non-finite JSON number")

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (
        _DuplicateJsonKeyError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise CockpitEditRequestError("edit request is not strict JSON") from exc
    if type(decoded) is not dict:
        raise CockpitEditRequestError("edit request must be a JSON object")
    expected = {"schema_version", "expected_current_sha256", "content"}
    if set(decoded) != expected:
        raise CockpitEditRequestError("edit request fields are invalid")
    if (
        type(decoded["schema_version"]) is not int
        or decoded["schema_version"] != SCHEMA_VERSION
    ):
        raise CockpitEditRequestError("edit request schema version is invalid")
    if (
        type(decoded["expected_current_sha256"]) is not str
        or _SHA256_RE.fullmatch(decoded["expected_current_sha256"]) is None
    ):
        raise CockpitEditRequestError("edit request revision is invalid")
    if type(decoded["content"]) is not str:
        raise CockpitEditRequestError("edit request content is invalid")
    return decoded


def _json_content_type_allowed(value: str | None) -> bool:
    if value is None:
        return False
    parts = [part.strip() for part in value.split(";")]
    if not parts or parts[0].casefold() != "application/json":
        return False
    parameters = parts[1:]
    if not parameters:
        return True
    if len(parameters) != 1 or not parameters[0]:
        return False
    if "=" not in parameters[0]:
        return False
    name, parameter_value = (part.strip() for part in parameters[0].split("=", 1))
    return name.casefold() == "charset" and parameter_value.casefold() == "utf-8"


def _parse_host_header(value: str | None) -> tuple[str, int | None] | None:
    if value is None or not request_host_allowed(value):
        return None
    try:
        parsed = urlsplit(f"//{value.strip()}")
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not hostname:
        return None
    return hostname, port


def _host_matches_server_port(value: str | None, server_port: int) -> bool:
    parsed = _parse_host_header(value)
    if parsed is None:
        return False
    _hostname, port = parsed
    return (port if port is not None else 80) == server_port


def _origin_matches_host(
    origin: str | None, host: str | None, *, server_port: int
) -> bool:
    if origin is None or host is None:
        return False
    if not _host_matches_server_port(host, server_port):
        return False
    # The browser-facing server is HTTP-only and the Origin must match the exact
    # loopback Host authority used for this request.  No CORS/wildcard origin is
    # accepted.
    return origin == f"http://{host.strip()}"


class _HttpEditRequestError(Exception):
    """Private bounded framing/header error for the JSON edit route."""

    def __init__(self, status: int, reason_code: str) -> None:
        super().__init__(reason_code)
        self.status = status
        self.reason_code = reason_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "llmwiki-research-cockpit-edit-rejection",
            "error": "controlled_edit_rejected",
            "reason_code": self.reason_code,
        }


def _persistence_http_status(exc: ControlledMarkdownPersistenceError) -> int:
    if isinstance(exc, ControlledMarkdownAuthorizationError):
        return 403
    if isinstance(exc, ControlledMarkdownWriteConflictError):
        return 409
    if isinstance(
        exc,
        (
            ControlledMarkdownCommitUnknownError,
            ControlledMarkdownCommitAuditUnknownError,
            ControlledMarkdownWriteFailedError,
        ),
    ):
        return 503
    if isinstance(exc, ControlledMarkdownWriteRejectedError):
        return 409
    return 503


class ResearchCockpit:
    """Build deterministic read-only views from machine and curated state."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        edit_context: TrustedHostSessionContext | None = None,
    ) -> None:
        try:
            root = Path(workspace_root).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ResearchCockpitError("workspace root is unavailable") from exc
        if not root.is_dir():
            raise ResearchCockpitError("workspace root is not a directory")
        if (
            edit_context is not None
            and type(edit_context) is not TrustedHostSessionContext
        ):
            raise ResearchCockpitError(
                "edit_context must be an exact TrustedHostSessionContext"
            )
        self.workspace_root = root
        self.edit_context = edit_context
        self.editing_enabled = edit_context is not None

    def _list_project_ids(self) -> list[str]:
        projects_root = self.workspace_root / ".llmwiki" / "projects"
        if not _safe_directory(self.workspace_root, projects_root):
            return []
        try:
            entries = sorted(
                os.scandir(projects_root),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except (FileNotFoundError, NotADirectoryError, OSError):
            return []
        ids: list[str] = []
        for entry in entries:
            if len(ids) >= MAX_PROJECTS:
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if not _safe_directory(projects_root, Path(entry.path)):
                    continue
                validate_project_id(entry.name)
            except Exception:
                continue
            ids.append(entry.name)
        return ids

    def _load_registration(self, project_id: str) -> ProjectRegistrationResult:
        try:
            normalized_id = validate_project_id(project_id)
            project_directory = (
                self.workspace_root / ".llmwiki" / "projects" / normalized_id
            )
            project_file = project_directory / "project.yaml"
            if not _safe_directory(self.workspace_root, project_directory):
                raise ProjectRecordError("registered project directory is unsafe")
            if _regular_file_state(project_file) != "regular":
                raise ProjectRecordError("registered project record is unsafe")
            return load_registered_project(self.workspace_root, normalized_id)
        except (
            ProjectNotRegisteredError,
            ProjectRecordError,
            LayoutError,
            OSError,
        ) as exc:
            raise CockpitProjectUnavailable(
                "registered project is unavailable"
            ) from exc

    def _require_editing(self) -> TrustedHostSessionContext:
        if not self.editing_enabled or self.edit_context is None:
            raise CockpitEditingUnavailable("controlled editing is not enabled")
        return self.edit_context

    def edit_session(self) -> dict[str, Any]:
        """Return the fixed target catalog without exposing host attribution."""

        self._require_editing()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "llmwiki-research-cockpit-edit-session",
            "editing": True,
            "mode": "controlled-markdown-user-region",
            "arbitrary_paths": False,
            "targets": [target.as_dict() for target in CONTROLLED_EDIT_TARGETS],
            "query": _capabilities(editing=True)["query"],
            "c07": "deferred/not_started",
        }

    def _editable_page(
        self,
        project_id: str,
        target_key: str,
    ) -> tuple[
        ProjectRegistrationResult,
        ControlledEditTarget,
        bytes,
        Any,
        Any,
        str,
    ]:
        self._require_editing()
        target = _edit_target(target_key)
        registration = self._load_registration(project_id)
        try:
            current = _read_knowledge(registration.layout, target.path)
            page = parse_knowledge_page(current, path=target.path)
            if page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
                raise CockpitEditTargetUnavailable(
                    "legacy Knowledge pages are read-only"
                )
            if page.frontmatter.project_id != registration.project_id:
                raise CockpitEditTargetUnavailable(
                    "Knowledge page project binding is invalid"
                )
            if page.frontmatter.ownership != "mixed":
                raise CockpitEditTargetUnavailable(
                    "controlled Web targets require mixed ownership"
                )
            if page.frontmatter.status != "draft":
                raise CockpitEditTargetUnavailable(
                    "controlled Web edits are limited to draft pages"
                )
            parsed = parse_mixed_markdown_body(page.body)
            expected_regions = MIXED_REGION_IDS.get(target.path)
            if parsed.region_ids != expected_regions or expected_regions != (
                target.region_id,
            ):
                raise CockpitEditTargetUnavailable(
                    "controlled Web target regions are invalid"
                )
            region = parsed.regions[0]
            _validate_edit_content(
                region.content,
                roots=_project_roots(registration),
            )
        except CockpitEditingError:
            raise
        except (
            ControlledMarkdownError,
            ResearchCockpitError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise CockpitEditTargetUnavailable(
                "controlled Web target is unavailable"
            ) from exc
        current_sha256 = hashlib.sha256(current).hexdigest()
        return registration, target, current, page, parsed, current_sha256

    def project_edit_snapshot(self, project_id: str, target_key: str) -> dict[str, Any]:
        """Return one exact protected user region and its full-page revision hash."""

        registration, target, _current, page, parsed, current_sha256 = (
            self._editable_page(project_id, target_key)
        )
        region = parsed.regions[0]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "llmwiki-research-cockpit-edit-target",
            "project_id": registration.project_id,
            "target": target.as_dict(),
            "title": page.frontmatter.title,
            "status": "DRAFT",
            "status_code": "draft",
            "ownership": "mixed",
            "updated_at": page.frontmatter.updated_at,
            "current_sha256": current_sha256,
            "content_sha256": region.content_sha256,
            "content": region.content,
            "write_mode": "user-edit",
            "arbitrary_paths": False,
        }
        return self._finalize(payload, registration)

    def project_edit(
        self,
        project_id: str,
        target_key: str,
        *,
        expected_current_sha256: object,
        content: object,
    ) -> dict[str, Any]:
        """Persist one fixed target through an independently recomputed F-05 plan."""

        host_context = self._require_editing()
        if (
            type(expected_current_sha256) is not str
            or _SHA256_RE.fullmatch(expected_current_sha256) is None
        ):
            raise CockpitEditRequestError(
                "expected_current_sha256 must be a lowercase SHA-256"
            )
        (
            registration,
            target,
            current,
            page,
            parsed,
            observed_current_sha256,
        ) = self._editable_page(project_id, target_key)
        if expected_current_sha256 != observed_current_sha256:
            raise CockpitEditConflict(
                expected_current_sha256=expected_current_sha256,
                observed_current_sha256=observed_current_sha256,
            )
        normalized_content = _validate_edit_content(
            content,
            roots=_project_roots(registration),
        )
        proposed_frontmatter = replace(
            page.frontmatter,
            updated_at=_next_utc_timestamp(page.frontmatter.updated_at),
        )
        proposed_body = parsed.render({target.region_id: normalized_content})
        proposed = (
            serialize_knowledge_frontmatter(
                proposed_frontmatter,
                path=target.path,
            )
            + proposed_body
        ).encode("utf-8")
        try:
            plan = plan_controlled_markdown_update(
                path=target.path,
                current=current,
                proposed=proposed,
                intent="user-edit",
                expected_current_sha256=expected_current_sha256,
            )
            authorization = bind_controlled_markdown_authorization(
                plan,
                host_context=host_context,
                decision_id=f"web-edit-{uuid.uuid4().hex}",
                authorized_at=_utc_now(),
            )
            result = persist_controlled_markdown_update(
                self.workspace_root,
                registration.project_id,
                path=target.path,
                proposed=proposed,
                intent="user-edit",
                expected_current_sha256=expected_current_sha256,
                authorization=authorization,
            )
        except ControlledMarkdownPersistenceError:
            raise
        except (ControlledMarkdownError, OSError, TypeError, ValueError) as exc:
            raise CockpitEditTargetUnavailable(
                "controlled Markdown planning rejected the edit"
            ) from exc
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "llmwiki-research-cockpit-edit-result",
            "outcome": "committed",
            "project_id": registration.project_id,
            "target": target.as_dict(),
            "current_sha256": result.output_sha256,
            "updated_at": proposed_frontmatter.updated_at,
            "write": result.as_dict(),
            "body_returned": False,
            "source_project_modified": False,
        }
        return self._finalize(payload, registration)

    def _load_context(self, registration: ProjectRegistrationResult) -> _ProjectContext:
        layout = registration.layout
        gaps: list[_Gap] = []
        manifest: ProjectManifest | None = None
        coverage: dict[str, Any] | None = None
        source_registry: SourceRegistry | None = None
        evidence_registry: EvidenceRegistry | None = None
        manifest_state = _regular_file_state(layout.manifest_file)
        if manifest_state == "missing":
            gaps.append(
                _Gap("manifest-missing", _message("manifest-missing"), "manifest")
            )
        elif manifest_state != "regular":
            gaps.append(
                _Gap("manifest-invalid", _message("manifest-invalid"), "manifest")
            )
        else:
            try:
                manifest = load_project_manifest(
                    layout.manifest_file,
                    project_id=registration.project_id,
                    project_root=registration.project_root,
                    required_manifest_version=PROJECT_MANIFEST_VERSION,
                )
            except Exception:
                gaps.append(
                    _Gap("manifest-invalid", _message("manifest-invalid"), "manifest")
                )
            if manifest is not None:
                try:
                    count = manifest.summary.get("record_counts", {}).get("file")
                    if not isinstance(count, int):
                        count = len(manifest.file_records)
                    coverage = build_coverage_report(
                        project_id=registration.project_id,
                        manifest_file=manifest.manifest_file,
                        manifest_version=manifest.manifest_version,
                        scan_generation=manifest.scan_generation,
                        manifest_file_count=count,
                        file_records=manifest.file_records,
                    )
                except Exception:
                    gaps.append(
                        _Gap(
                            "coverage-unavailable",
                            _message("coverage-unavailable"),
                            "coverage",
                        )
                    )
        if _regular_file_state(layout.sources_file) == "missing":
            gaps.append(_Gap("sources-missing", _message("sources-missing"), "sources"))
        else:
            try:
                source_registry = load_source_registry(
                    self.workspace_root, registration.project_id
                )
            except Exception:
                gaps.append(
                    _Gap("sources-invalid", _message("sources-invalid"), "sources")
                )
        if _regular_file_state(layout.evidence_file) == "missing":
            gaps.append(
                _Gap("evidence-missing", _message("evidence-missing"), "evidence")
            )
        else:
            try:
                evidence_registry = load_evidence_registry(
                    self.workspace_root, registration.project_id
                )
            except Exception:
                gaps.append(
                    _Gap("evidence-invalid", _message("evidence-invalid"), "evidence")
                )
        return _ProjectContext(
            registration,
            manifest,
            coverage,
            source_registry,
            evidence_registry,
            tuple(gaps),
        )

    def _registration_public(
        self, registration: ProjectRegistrationResult
    ) -> dict[str, Any]:
        record = registration.record
        onboarding = (
            record.get("onboarding")
            if isinstance(record.get("onboarding"), dict)
            else {}
        )
        return {
            "project_id": registration.project_id,
            "name": str(record.get("name") or registration.project_id),
            "registered_at": record.get("registered_at"),
            "onboarding": _redact(onboarding, _project_roots(registration)),
        }

    def _artifact_page(
        self, context: _ProjectContext, spec: Mapping[str, str]
    ) -> tuple[dict[str, Any], _Page | None]:
        registration = context.registration
        path = str(spec["path"])
        result: dict[str, Any] = {
            "key": str(spec["key"]),
            "path": path,
            "artifact_type": str(spec["artifact_type"]),
            "title": str(spec["title"]),
            "link": _path_url(registration.project_id, path),
        }
        target = registration.layout.knowledge_root.joinpath(*PurePosixPath(path).parts)
        entry_state = _regular_file_state(target)
        if entry_state == "missing":
            result.update(
                {
                    "availability": "missing",
                    **_Gap(
                        "knowledge-missing",
                        _message("knowledge-missing"),
                        path,
                    ).as_dict(),
                }
            )
            return result, None
        if entry_state != "regular":
            result.update(
                {
                    "availability": "invalid",
                    "entry_state": entry_state,
                    **_Gap(
                        "knowledge-unsafe",
                        _message("knowledge-unsafe"),
                        path,
                    ).as_dict(),
                }
            )
            return result, None
        try:
            page = parse_knowledge_page(
                _read_knowledge(registration.layout, path), path=path
            )
            if page.frontmatter.project_id != registration.project_id:
                raise ResearchCockpitError("knowledge page project binding is invalid")
        except Exception:
            result.update(
                {
                    "availability": "invalid",
                    **_Gap(
                        "knowledge-invalid",
                        _message("knowledge-invalid"),
                        path,
                    ).as_dict(),
                }
            )
            return result, None

        status, status_code = _public_status(str(page.frontmatter.status))
        refs = page.frontmatter.evidence_refs
        # Schema v1 evidence IDs are exposed without inventing a directional
        # stance. The legacy page remains a read-only compatibility view.
        evidence_refs = tuple(ref.as_dict() for ref in refs) if refs is not None else ()
        parsed = _Page(
            path=path,
            title=page.frontmatter.title,
            artifact_type=str(page.frontmatter.artifact_type),
            status=status,
            ownership=str(page.frontmatter.ownership),
            schema_version=page.frontmatter.schema_version,
            generated_at=page.frontmatter.generated_at,
            updated_at=page.frontmatter.updated_at,
            last_verified_at=page.frontmatter.last_verified_at,
            source_ids=tuple(page.frontmatter.source_ids),
            evidence_refs=evidence_refs,
            body=_redact(page.body, _project_roots(registration)),
        )
        result.update(
            {
                "availability": "available",
                "title": parsed.title,
                "status": parsed.status,
                "status_code": status_code,
                "ownership": parsed.ownership,
                "schema_version": parsed.schema_version,
                "generated_at": parsed.generated_at,
                "updated_at": parsed.updated_at,
                "last_verified_at": parsed.last_verified_at,
                "source_ids": list(parsed.source_ids),
                "evidence_count": len(page.frontmatter.evidence_refs or ())
                if page.frontmatter.schema_version >= 2
                else len(page.frontmatter.evidence_ids),
                "legacy_evidence_ids": list(page.frontmatter.evidence_ids)
                if page.frontmatter.schema_version == 1
                else [],
                "legacy_read_only": page.frontmatter.schema_version == 1,
            }
        )
        return result, parsed

    def _daily_plans(self, context: _ProjectContext) -> dict[str, Any]:
        daily_dir = context.registration.layout.knowledge_root / "plans" / "daily"
        plans: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        directory_state = _directory_state(
            context.registration.layout.knowledge_root, daily_dir
        )
        if directory_state != "directory":
            entries = []
            if directory_state == "unsafe":
                gaps.append(
                    _Gap(
                        "daily-plan-directory-unsafe",
                        _message("daily-plan-directory-unsafe"),
                        "plans/daily",
                    ).as_dict()
                )
        else:
            try:
                entries = list(os.scandir(daily_dir))
            except (FileNotFoundError, NotADirectoryError, OSError):
                entries = []
                gaps.append(
                    _Gap(
                        "daily-plan-directory-unsafe",
                        _message("daily-plan-directory-unsafe"),
                        "plans/daily",
                    ).as_dict()
                )
        for entry in sorted(entries, key=lambda item: item.name):
            if len(plans) >= MAX_DAILY_PLANS or not _DATE_PLAN_RE.fullmatch(entry.name):
                continue
            path = f"plans/daily/{entry.name}"
            try:
                page = parse_knowledge_page(
                    _read_knowledge(context.registration.layout, path), path=path
                )
                if page.frontmatter.project_id != context.registration.project_id:
                    raise ResearchCockpitError("daily plan project binding is invalid")
                status, code = _public_status(str(page.frontmatter.status))
                plans.append(
                    {
                        "path": path,
                        "title": page.frontmatter.title,
                        "availability": "available",
                        "status": status,
                        "status_code": code,
                        "updated_at": page.frontmatter.updated_at,
                        "link": _path_url(context.registration.project_id, path),
                    }
                )
            except Exception:
                plans.append(
                    {
                        "path": path,
                        "title": entry.name,
                        "availability": "invalid",
                        **_Gap(
                            "knowledge-invalid", _message("knowledge-invalid"), path
                        ).as_dict(),
                        "link": _path_url(context.registration.project_id, path),
                    }
                )
        today_path = f"plans/daily/{dt.date.today().isoformat()}.md"
        today = next((item for item in plans if item["path"] == today_path), None)
        if today is None:
            today = {
                "path": today_path,
                "title": "Today's plan",
                "availability": "missing",
                **_Gap(
                    "daily-plan-missing", _message("daily-plan-missing"), today_path
                ).as_dict(),
                "link": _path_url(context.registration.project_id, today_path),
            }
        return {"today": today, "items": plans, "count": len(plans), "gaps": gaps}

    def _load_claim_pages(
        self, context: _ProjectContext
    ) -> list[tuple[dict[str, Any], _Page | None]]:
        claim_dir = context.registration.layout.knowledge_root / "claims"
        if not _safe_directory(context.registration.layout.knowledge_root, claim_dir):
            return []
        try:
            entries = list(os.scandir(claim_dir))
        except (FileNotFoundError, NotADirectoryError, OSError):
            return []
        result: list[tuple[dict[str, Any], _Page | None]] = []
        for entry in sorted(
            entries, key=lambda item: (item.name.casefold(), item.name)
        ):
            if len(result) >= MAX_CLAIMS or not entry.name.endswith(".md"):
                continue
            path = f"claims/{entry.name}"
            try:
                _valid_page_path(path, allow_daily=False)
            except CockpitPathError:
                continue
            record, page = self._artifact_page(
                context,
                {
                    "key": "claim",
                    "path": path,
                    "artifact_type": "claim",
                    "title": entry.name,
                },
            )
            record["is_key_claim"] = entry.name != "index.md"
            if page is not None:
                record["evidence_refs"] = [dict(item) for item in page.evidence_refs]
            result.append((record, page))
        return result

    def _source_public(
        self, source: SourceRecord, context: _ProjectContext
    ) -> dict[str, Any]:
        return {
            "source_id": source.source_id,
            "current_path": source.current_path,
            "path_history": [item.as_dict() for item in source.path_history],
            "current_version": source.current_version,
            "current_content_hash": source.current_content_hash,
            "versions": [
                {
                    "version": item.version,
                    "content_hash": item.content_hash,
                    "recorded_path": item.manifest_path,
                    "observed_scan_generation": item.observed_scan_generation,
                }
                for item in source.versions
            ],
            "link": f"/api/projects/{quote(context.registration.project_id, safe='')}/sources/{quote(source.source_id, safe='')}",
        }

    def _evidence_public(
        self, evidence: Any, context: _ProjectContext
    ) -> dict[str, Any]:
        source = (
            context.source_registry.by_source_id.get(evidence.source_id)
            if context.source_registry is not None
            else None
        )
        source_public = (
            self._source_public(source, context) if source is not None else None
        )
        binding = "registry-unavailable"
        recorded_path = None
        if source is not None:
            version = next(
                (
                    item
                    for item in source.versions
                    if item.version == evidence.source_version
                ),
                None,
            )
            if version is None:
                binding = "unbound-version"
            else:
                recorded_path = version.manifest_path
                binding = (
                    "registry-current"
                    if source.current_version == evidence.source_version
                    and source.current_content_hash == evidence.content_hash
                    else "historical-or-mismatch"
                )
        return {
            "project_id": evidence.project_id,
            "evidence_id": evidence.evidence_id,
            "source_id": evidence.source_id,
            "source_version": evidence.source_version,
            "content_hash": evidence.content_hash,
            "excerpt_hash": evidence.excerpt_hash,
            "locator": _redact(
                evidence.locator.as_dict(), _project_roots(context.registration)
            ),
            "source": source_public,
            "recorded_path": recorded_path,
            "registry_binding": binding,
            "currentness_note": "Registry comparison only; the cockpit does not reopen source bytes.",
            "link": f"/api/projects/{quote(context.registration.project_id, safe='')}/evidence/{quote(evidence.evidence_id, safe='')}",
        }

    def _claim_trace(
        self, record: dict[str, Any], page: _Page | None, context: _ProjectContext
    ) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        refs = page.evidence_refs if page is not None else ()
        for ref in refs:
            evidence = (
                context.evidence_registry.by_evidence_id.get(ref["evidence_id"])
                if context.evidence_registry is not None
                else None
            )
            if evidence is None:
                trace.append(
                    {
                        "evidence_id": ref["evidence_id"],
                        "stance": ref["stance"],
                        "availability": "missing",
                        **_Gap(
                            "evidence-missing", _message("evidence-missing")
                        ).as_dict(),
                    }
                )
            else:
                item = self._evidence_public(evidence, context)
                item["stance"] = ref["stance"]
                item["availability"] = "available"
                trace.append(item)
        result = dict(record)
        result["evidence_trace"] = trace
        result["evidence_refs"] = [
            {"evidence_id": item["evidence_id"], "stance": item["stance"]}
            for item in trace
            if "stance" in item
        ]
        result["claim_link"] = _path_url(
            context.registration.project_id, result["path"]
        )
        return result

    def _files(self, context: _ProjectContext) -> list[dict[str, Any]]:
        if context.manifest is None:
            return []
        source_by_path: dict[str, SourceRecord] = {}
        if context.source_registry is not None:
            source_by_path = {
                source.current_path: source
                for source in context.source_registry.records
            }
        rows: list[dict[str, Any]] = []
        for raw in context.manifest.file_records[:MAX_FILES]:
            try:
                classification = classification_from_dict(raw.get("classification"))
                state = file_state_from_dict(raw.get("file_state"))
            except Exception:
                continue
            path = str(raw.get("path", ""))
            source = source_by_path.get(path)
            rows.append(
                {
                    "path": path,
                    "size_bytes": raw.get("size_bytes"),
                    "content_sha256": raw.get("content_sha256"),
                    "format": classification.format,
                    "language": classification.language,
                    "research_role": classification.research_role,
                    "processing_status": state.processing_status,
                    "read_depth": state.read_depth,
                    "reason_code": state.reason_code,
                    "reason": state.reason,
                    "source_id": source.source_id if source is not None else None,
                    "source_link": (
                        f"/api/projects/{quote(context.registration.project_id, safe='')}/sources/{quote(source.source_id, safe='')}"
                        if source is not None
                        else None
                    ),
                }
            )
        return rows

    def _planning(self, context: _ProjectContext) -> dict[str, Any]:
        registration = context.registration
        layout = registration.layout
        roots = _project_roots(registration)
        result: dict[str, Any] = {
            "goal": None,
            "tasks": [],
            "initial_plan": None,
            "project_state": None,
            "gaps": [],
        }

        def load_artifact(path: Path, parser: Any) -> Any | None:
            if _regular_file_state(path) == "missing":
                result["gaps"].append(
                    _Gap(
                        "machine-artifact-missing",
                        _message("machine-artifact-missing"),
                        path.name,
                    ).as_dict()
                )
                return None
            try:
                return parser(
                    _read_machine(layout, path), project_id=registration.project_id
                )
            except Exception:
                result["gaps"].append(
                    _Gap(
                        "machine-artifact-invalid",
                        _message("machine-artifact-invalid"),
                        path.name,
                    ).as_dict()
                )
                return None

        goal = load_artifact(layout.goals_file, parse_goal)
        if isinstance(goal, Goal):
            result["goal"] = _redact(goal.as_dict(), roots)
            result["goal"]["display_status"] = goal.display_status
            result["goal"]["is_draft"] = goal.is_draft

        tasks = load_artifact(layout.tasks_file, parse_task_collection)
        if isinstance(tasks, TaskCollection):
            for task in tasks.tasks:
                try:
                    row = task.as_dict()
                except Exception:
                    row = {
                        "task_id": task.task_id,
                        "title": task.title,
                        "status": task.status,
                        "draft_reasons": list(task.draft_reasons),
                    }
                result["tasks"].append(_redact(row, roots))
            result["tasks_updated_at"] = tasks.updated_at

        plan = load_artifact(layout.initial_plan_file, parse_initial_plan)
        if isinstance(plan, InitialPlan):
            result["initial_plan"] = _redact(plan.as_dict(), roots)

        state = load_artifact(layout.project_state_file, parse_project_state)
        if isinstance(state, ProjectState):
            result["project_state"] = _redact(state.as_dict(), roots)
        return result

    def _runs(self, context: _ProjectContext) -> dict[str, Any]:
        layout = context.registration.layout
        items: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        directory_state = _directory_state(layout.machine_root, layout.runs_dir)
        if directory_state != "directory":
            entries = []
            if directory_state == "unsafe":
                gaps.append(
                    _Gap(
                        "runs-directory-unsafe",
                        _message("runs-directory-unsafe"),
                        "runs",
                    ).as_dict()
                )
        else:
            try:
                entries = list(os.scandir(layout.runs_dir))
            except (FileNotFoundError, NotADirectoryError, OSError):
                entries = []
                gaps.append(
                    _Gap(
                        "runs-directory-unsafe",
                        _message("runs-directory-unsafe"),
                        "runs",
                    ).as_dict()
                )
        for entry in sorted(entries, key=lambda item: item.name, reverse=True)[
            :MAX_RUNS
        ]:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                validate_run_id(entry.name)
                run_file = Path(entry.path) / "run.json"
                layout.validate_machine_state_path(run_file, leaf_kind="file")
                record = load_project_run(
                    self.workspace_root, context.registration.project_id, entry.name
                ).record
            except Exception:
                gaps.append(
                    _Gap("run-invalid", _message("run-invalid"), entry.name).as_dict()
                )
                continue
            stages: list[dict[str, Any]] = []
            for stage in record.get("stages", []):
                if not isinstance(stage, dict):
                    continue
                attempts = (
                    stage.get("attempts")
                    if isinstance(stage.get("attempts"), list)
                    else []
                )
                public_attempts: list[dict[str, Any]] = []
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    token_usage = (
                        attempt.get("token_usage")
                        if isinstance(attempt.get("token_usage"), dict)
                        else {}
                    )
                    estimated_cost = (
                        attempt.get("estimated_cost")
                        if isinstance(attempt.get("estimated_cost"), dict)
                        else {}
                    )
                    public_attempts.append(
                        _redact(
                            {
                                "attempt": attempt.get("attempt"),
                                "status": attempt.get("status"),
                                "started_at": attempt.get("started_at"),
                                "completed_at": attempt.get("completed_at"),
                                "duration_ms": attempt.get("duration_ms"),
                                "provider": attempt.get("provider"),
                                "model": attempt.get("model"),
                                "input_tokens": token_usage.get("input_tokens"),
                                "output_tokens": token_usage.get("output_tokens"),
                                "cache_hits": token_usage.get("cache_hits"),
                                "estimated_cost": estimated_cost,
                                "error": attempt.get("error"),
                            },
                            _project_roots(context.registration),
                        )
                    )
                stages.append(
                    {
                        "stage_id": stage.get("stage_id"),
                        "ordinal": stage.get("ordinal"),
                        "status": stage.get("status"),
                        "attempt_count": len(public_attempts),
                        "last_error": _redact(
                            stage.get("last_error"),
                            _project_roots(context.registration),
                        ),
                        "attempts": public_attempts,
                    }
                )
            items.append(
                {
                    "run_id": record.get("run_id"),
                    "status": record.get("status"),
                    "created_at": record.get("created_at"),
                    "updated_at": record.get("updated_at"),
                    "started_at": record.get("started_at"),
                    "completed_at": record.get("completed_at"),
                    "duration_ms": record.get("duration_ms"),
                    "active_stage": record.get("active_stage"),
                    "through_stage": record.get("through_stage"),
                    "usage": _redact(
                        record.get("usage", {}), _project_roots(context.registration)
                    ),
                    "stages": stages,
                    "errors": _redact(
                        record.get("errors", []), _project_roots(context.registration)
                    ),
                    "artifacts": [
                        {
                            "artifact_type": item.get("artifact_type"),
                            "artifact_id": item.get("artifact_id"),
                            "relative_path": item.get("relative_path"),
                            "content_hash": item.get("content_hash"),
                        }
                        for item in record.get("artifacts", [])
                        if isinstance(item, dict)
                    ],
                }
            )
        return {"items": items, "count": len(items), "gaps": gaps}

    def _project_detail(self, context: _ProjectContext) -> dict[str, Any]:
        registration = context.registration
        artifacts = [
            self._artifact_page(context, spec)[0] for spec in PRODUCT_ARTIFACT_SPECS
        ]
        claims = [
            self._claim_trace(record, page, context)
            for record, page in self._load_claim_pages(context)
        ]
        files = self._files(context)
        coverage_public: dict[str, Any] | None = None
        if context.coverage is not None:
            report = context.coverage
            failures = report.get("failures", [])
            coverage_public = {
                "manifest": report.get("manifest"),
                "totals": report.get("totals"),
                "coverage": report.get("coverage"),
                "failures": failures[:MAX_PAGE_LIST],
                "failure_count": len(failures),
                "reconciliation": report.get("reconciliation"),
            }
        return {
            "project": self._registration_public(registration),
            "artifacts": artifacts,
            "daily_plans": self._daily_plans(context),
            "coverage": coverage_public,
            "files": {
                "count": len(files),
                "preview": files[:100],
                "link": f"/api/projects/{quote(registration.project_id, safe='')}/files",
            },
            "claims": {
                "count": len(claims),
                "items": claims[:MAX_PAGE_LIST],
                "link": f"/api/projects/{quote(registration.project_id, safe='')}/claims",
            },
            "sources": {
                "count": len(context.source_registry.records)
                if context.source_registry is not None
                else 0,
                "available": context.source_registry is not None,
            },
            "evidence": {
                "count": len(context.evidence_registry.records)
                if context.evidence_registry is not None
                else 0,
                "available": context.evidence_registry is not None,
            },
            "planning": self._planning(context),
            "runs": self._runs(context),
            "gaps": [gap.as_dict() for gap in context.gaps],
            "capabilities": _capabilities(editing=self.editing_enabled),
        }

    def project_snapshot(self, project_id: str) -> dict[str, Any]:
        registration = self._load_registration(project_id)
        payload = self._project_detail(self._load_context(registration))
        payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": KIND,
                "generated_at": _utc_now(),
            }
        )
        return self._finalize(payload, registration)

    def project_files(
        self,
        project_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        status: str | None = None,
    ) -> dict[str, Any]:
        context = self._load_context(self._load_registration(project_id))
        rows = self._files(context)
        if status is not None:
            rows = [item for item in rows if item["processing_status"] == status]
        limit = min(max(int(limit), 1), MAX_PAGE_LIST)
        offset = max(int(offset), 0)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "view": "project-files",
            "project_id": context.registration.project_id,
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "gaps": [gap.as_dict() for gap in context.gaps],
        }
        return self._finalize(payload, context.registration)

    def project_claims(
        self,
        project_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        status: str | None = None,
    ) -> dict[str, Any]:
        context = self._load_context(self._load_registration(project_id))
        rows = [
            self._claim_trace(record, page, context)
            for record, page in self._load_claim_pages(context)
        ]
        if status is not None:
            rows = [
                item
                for item in rows
                if item.get("status_code") == status or item.get("status") == status
            ]
        limit = min(max(int(limit), 1), MAX_PAGE_LIST)
        offset = max(int(offset), 0)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "view": "project-claims",
            "project_id": context.registration.project_id,
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "gaps": [gap.as_dict() for gap in context.gaps],
        }
        return self._finalize(payload, context.registration)

    def project_evidence(self, project_id: str, evidence_id: str) -> dict[str, Any]:
        """Return one Evidence record and its Source trace without source reopen."""

        evidence_id = _bounded_identifier(evidence_id, label="evidence_id")
        context = self._load_context(self._load_registration(project_id))
        if context.evidence_registry is None:
            raise CockpitProjectUnavailable("Evidence registry is unavailable")
        evidence = context.evidence_registry.by_evidence_id.get(evidence_id)
        if evidence is None:
            raise KeyError(evidence_id)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "view": "project-evidence",
            "project_id": context.registration.project_id,
            "evidence": self._evidence_public(evidence, context),
            "gaps": [gap.as_dict() for gap in context.gaps],
            "source_bytes_reopened": False,
        }
        return self._finalize(payload, context.registration)

    def project_source(self, project_id: str, source_id: str) -> dict[str, Any]:
        """Return one Source identity/version history without source reopen."""

        source_id = _bounded_identifier(source_id, label="source_id")
        context = self._load_context(self._load_registration(project_id))
        if context.source_registry is None:
            raise CockpitProjectUnavailable("Source registry is unavailable")
        source = context.source_registry.by_source_id.get(source_id)
        if source is None:
            raise KeyError(source_id)
        evidence_items: list[dict[str, Any]] = []
        if context.evidence_registry is not None:
            for evidence in context.evidence_registry.records:
                if evidence.source_id == source.source_id:
                    evidence_items.append(
                        {
                            "evidence_id": evidence.evidence_id,
                            "source_version": evidence.source_version,
                            "locator": _redact(
                                evidence.locator.as_dict(),
                                _project_roots(context.registration),
                            ),
                            "link": (
                                f"/api/projects/{quote(context.registration.project_id, safe='')}"
                                f"/evidence/{quote(evidence.evidence_id, safe='')}"
                            ),
                        }
                    )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "view": "project-source",
            "project_id": context.registration.project_id,
            "source": self._source_public(source, context),
            "evidence": evidence_items[:MAX_PAGE_LIST],
            "evidence_count": len(evidence_items),
            "gaps": [gap.as_dict() for gap in context.gaps],
            "source_bytes_reopened": False,
        }
        return self._finalize(payload, context.registration)

    def project_knowledge(self, project_id: str, path: str) -> dict[str, Any]:
        """Return one strict Knowledge page or an explicit DRAFT gap."""

        path = _valid_page_path(path)
        registration = self._load_registration(project_id)
        layout = registration.layout
        target = layout.knowledge_root.joinpath(*PurePosixPath(path).parts)
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "view": "project-knowledge",
            "project_id": registration.project_id,
            "path": path,
            "link": _path_url(registration.project_id, path),
            "read_only": True,
        }
        entry_state = _regular_file_state(target)
        if entry_state == "missing":
            base.update(
                {
                    "availability": "missing",
                    "body": None,
                    **_Gap(
                        "knowledge-missing", _message("knowledge-missing"), path
                    ).as_dict(),
                }
            )
            return self._finalize(base, registration)
        if entry_state != "regular":
            base.update(
                {
                    "availability": "unsafe",
                    "body": None,
                    **_Gap(
                        "knowledge-unsafe", _message("knowledge-unsafe"), path
                    ).as_dict(),
                }
            )
            return self._finalize(base, registration)
        try:
            page = parse_knowledge_page(_read_knowledge(layout, path), path=path)
            if page.frontmatter.project_id != registration.project_id:
                raise ResearchCockpitError("knowledge page project binding is invalid")
        except Exception:
            base.update(
                {
                    "availability": "invalid",
                    "body": None,
                    **_Gap(
                        "knowledge-invalid", _message("knowledge-invalid"), path
                    ).as_dict(),
                }
            )
            return self._finalize(base, registration)
        status, status_code = _public_status(str(page.frontmatter.status))
        frontmatter = page.frontmatter.as_dict()
        base.update(
            {
                "availability": "available",
                "title": page.frontmatter.title,
                "artifact_type": str(page.frontmatter.artifact_type),
                "status": status,
                "status_code": status_code,
                "ownership": str(page.frontmatter.ownership),
                "frontmatter": frontmatter,
                "body": page.body,
                "legacy_read_only": page.frontmatter.schema_version == 1,
            }
        )
        return self._finalize(base, registration)

    def _project_card(self, registration: ProjectRegistrationResult) -> dict[str, Any]:
        context = self._load_context(registration)
        artifact_rows = [
            self._artifact_page(context, spec)[0] for spec in PRODUCT_ARTIFACT_SPECS
        ]
        status_counts: dict[str, int] = {}
        for item in artifact_rows:
            status = str(item.get("status", "DRAFT"))
            status_counts[status] = status_counts.get(status, 0) + 1
        planning = self._planning(context)
        latest_runs = self._runs(context)
        payload = {
            **self._registration_public(registration),
            "link": f"/api/projects/{quote(registration.project_id, safe='')}",
            "artifact_count": len(artifact_rows),
            "artifact_status_counts": status_counts,
            "missing_artifact_count": sum(
                1 for item in artifact_rows if item.get("availability") != "available"
            ),
            "manifest_available": context.manifest is not None,
            "coverage": context.coverage.get("totals")
            if context.coverage is not None
            else None,
            "goal": planning.get("goal"),
            "task_count": len(planning.get("tasks", [])),
            "run_count": latest_runs["count"],
            "latest_run": latest_runs["items"][0] if latest_runs["items"] else None,
            "gaps": [gap.as_dict() for gap in context.gaps],
        }
        return self._finalize(payload, registration)

    def list_projects(self) -> dict[str, Any]:
        """List registered project cards while keeping invalid records explicit."""

        projects: list[dict[str, Any]] = []
        for project_id in self._list_project_ids():
            try:
                registration = self._load_registration(project_id)
                projects.append(self._project_card(registration))
            except CockpitProjectUnavailable:
                projects.append(
                    {
                        "project_id": project_id,
                        "availability": "invalid",
                        **_Gap(
                            "project-record-invalid",
                            _message("project-record-invalid"),
                            project_id,
                        ).as_dict(),
                    }
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "generated_at": _utc_now(),
            "projects": projects,
            "count": len(projects),
            "capabilities": _capabilities(editing=self.editing_enabled),
        }

    def snapshot(self, project_id: str | None = None) -> dict[str, Any]:
        """Build the product snapshot used by the fixed local frontend."""

        if project_id is None:
            listing = self.list_projects()
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": KIND,
                "snapshot_version": SNAPSHOT_VERSION,
                "generated_at": listing["generated_at"],
                "projects": listing["projects"],
                "project_count": listing["count"],
                "selected_project": None,
                "capabilities": listing["capabilities"],
            }
        registration = self._load_registration(project_id)
        detail = self._project_detail(self._load_context(registration))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "snapshot_version": SNAPSHOT_VERSION,
            "generated_at": _utc_now(),
            "projects": [self._project_card(registration)],
            "project_count": 1,
            "selected_project": detail,
            "capabilities": _capabilities(editing=self.editing_enabled),
        }
        return self._finalize(payload, registration)

    def health(self) -> dict[str, Any]:
        projects = self._list_project_ids()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "status": "ok",
            "generated_at": _utc_now(),
            "project_count": len(projects),
            "read_only": not self.editing_enabled,
            "loopback_only": True,
            "capabilities": _capabilities(editing=self.editing_enabled),
        }

    @staticmethod
    def _finalize(
        payload: Mapping[str, Any], registration: ProjectRegistrationResult
    ) -> dict[str, Any]:
        """Redact all known roots and fail if an absolute root survives."""

        public = _redact(dict(payload), _project_roots(registration))
        serialized = json.dumps(public, ensure_ascii=False)
        for root, _label in _project_roots(registration):
            if _contains_root(serialized, root):
                raise ResearchCockpitError(
                    "cockpit payload contains a forbidden absolute path"
                )
        return public


def _bounded_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CockpitPathError(f"invalid {label}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise CockpitPathError(f"invalid {label}")
    if any(char in value for char in ("/", "\\", "\x00")):
        raise CockpitPathError(f"invalid {label}")
    return value


def _capabilities(*, editing: bool) -> dict[str, Any]:
    return {
        "read_only": not editing,
        "editing": {
            "available": editing,
            "available_after": None if editing else "J-04",
        },
        "query": {
            "available": False,
            "error": "capability-unavailable",
            "available_after": "G-04",
        },
        "plan": {
            "available": True,
            "execution_authorized": False,
        },
        "external_send": "local-only",
        "source_bytes_reopened": False,
        "c07": "deferred/not_started",
    }


def host_is_loopback(host: str) -> bool:
    """Return true only when a bind hostname resolves exclusively to loopback."""

    value = str(host).strip().strip("[]")
    if not value:
        return False
    if value.casefold() == "localhost":
        try:
            addresses = socket.getaddrinfo(value, None)
        except OSError:
            return False
        if not addresses:
            return False
        try:
            return all(
                ipaddress.ip_address(address[4][0]).is_loopback for address in addresses
            )
        except (IndexError, ValueError):
            return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str) -> str:
    if not host_is_loopback(host):
        raise ResearchCockpitError("Cockpit host must resolve only to loopback.")
    return host


def request_host_allowed(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip()
    if not value or any(char in value for char in ("@", "/", "\\", "?", "#")):
        return False
    if any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return bool(hostname) and host_is_loopback(hostname)


def security_headers(handler: BaseHTTPRequestHandler, *, api: bool) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    )
    handler.send_header("Cache-Control", "no-store" if api else "no-cache")


def _absolute_lexical_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(os.fspath(path)))


def _safe_asset_root(asset_root: Path) -> Path:
    root = _absolute_lexical_path(asset_root)
    try:
        metadata = os.lstat(root)
    except (FileNotFoundError, OSError) as exc:
        raise CockpitAssetError("asset root is unavailable") from exc
    if _is_redirection(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise CockpitAssetError("asset root is not a regular local directory")
    for name in ("index.html", "app.js", "styles.css"):
        target = root / name
        if _regular_file_state(target) != "regular":
            raise CockpitAssetError(
                f"required cockpit asset is unsafe or missing: {name}"
            )
        try:
            _read_bounded(root, target, maximum=MAX_PAGE_BYTES)
        except ResearchCockpitError as exc:
            raise CockpitAssetError(
                f"required cockpit asset is unavailable: {name}"
            ) from exc
    return root


def _validate_percent_escapes(value: str) -> None:
    index = 0
    hexadecimal = frozenset("0123456789abcdefABCDEF")
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hexadecimal
            or value[index + 2] not in hexadecimal
        ):
            raise CockpitPathError("invalid percent escape")
        index += 3


def _validate_request_path(value: str) -> str:
    if not value or len(value) > MAX_REQUEST_TARGET_CHARS or not value.startswith("/"):
        raise CockpitPathError("invalid request path")
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise CockpitPathError("invalid request path")
    _validate_percent_escapes(value)
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise CockpitPathError("invalid request path") from exc
    if "\\" in decoded or decoded.count("/") != value.count("/"):
        raise CockpitPathError("encoded path separators are forbidden")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        raise CockpitPathError("invalid request path")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise CockpitPathError("path traversal is forbidden")
    return value


def _strict_query(query: str, *, allowed: frozenset[str]) -> dict[str, list[str]]:
    if len(query) > MAX_QUERY_CHARS:
        raise CockpitPathError("query string is too long")
    if not query:
        return {}
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in query):
        raise CockpitPathError("invalid query string")
    _validate_percent_escapes(query)
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_QUERY_FIELDS,
            separator="&",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise CockpitPathError("invalid query string") from exc
    values: dict[str, list[str]] = {}
    for key, value in pairs:
        if (
            not key
            or len(key) > MAX_QUERY_KEY_CHARS
            or len(value) > MAX_QUERY_VALUE_CHARS
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in key + value)
        ):
            raise CockpitPathError("invalid query parameter")
        if key not in allowed:
            raise CockpitPathError("unknown query parameter")
        if key in values:
            raise CockpitPathError("duplicate query parameter")
        values[key] = [value]
    return values


def make_handler(
    cockpit: ResearchCockpit, asset_root: Path = DEFAULT_ASSET_ROOT
) -> type[BaseHTTPRequestHandler]:
    asset_root = _safe_asset_root(asset_root)
    asset_specs = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    }
    routes = {
        route: (
            _read_bounded(asset_root, asset_root / name, maximum=MAX_PAGE_BYTES),
            content_type,
        )
        for route, (name, content_type) in asset_specs.items()
    }
    # This token is created once per HTTP server/handler class, never accepted
    # from the request body, and never persisted into project state or audit.
    edit_token = secrets.token_urlsafe(32) if cockpit.editing_enabled else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "LLMWikiResearchCockpit"
        sys_version = ""

        def version_string(self) -> str:
            return self.server_version

        def send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            api: bool,
            head: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            security_headers(self, api=api)
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        def send_json(
            self, status: int, payload: Mapping[str, Any], *, head: bool = False
        ) -> None:
            self.send_bytes(
                status,
                _json_bytes(payload),
                "application/json; charset=utf-8",
                api=True,
                head=head,
            )

        def _header_values(self, name: str) -> tuple[str, ...]:
            return tuple(self.headers.get_all(name, []))

        def allowed(self) -> bool:
            host_values = self._header_values("Host")
            port = int(self.server.server_address[1])
            if (
                len(host_values) == 1
                and request_host_allowed(host_values[0])
                and _host_matches_server_port(host_values[0], port)
            ):
                return True
            self.send_json(
                421,
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": "misdirected_request",
                    "message": "Only the current loopback Host is accepted.",
                },
            )
            return False

        @staticmethod
        def _decode_segment(value: str) -> str:
            if len(value) > 768:
                raise CockpitPathError("invalid URL component")
            _validate_percent_escapes(value)
            try:
                decoded = unquote(value, encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError) as exc:
                raise CockpitPathError("invalid URL component") from exc
            return _bounded_identifier(decoded, label="path component")

        @staticmethod
        def _query(parsed: Any, *, allowed: frozenset[str]) -> dict[str, list[str]]:
            return _strict_query(parsed.query, allowed=allowed)

        @staticmethod
        def _one_query(
            values: Mapping[str, list[str]], key: str, *, required: bool = False
        ) -> str | None:
            items = values.get(key, [])
            if not items:
                if required:
                    raise CockpitPathError(f"missing query parameter: {key}")
                return None
            return items[0]

        @staticmethod
        def _integer_query(
            values: Mapping[str, list[str]], key: str, default: int
        ) -> int:
            raw = Handler._one_query(values, key)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw, 10)
            except (TypeError, ValueError) as exc:
                raise CockpitPathError(f"invalid query parameter: {key}") from exc
            if value < 0:
                raise CockpitPathError(f"invalid query parameter: {key}")
            return value

        def _request_target(self) -> tuple[Any, str]:
            if len(self.path) > MAX_REQUEST_TARGET_CHARS:
                raise CockpitPathError("request target is too long")
            parsed = urlsplit(self.path)
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or not parsed.path.startswith("/")
            ):
                raise CockpitPathError("invalid request target")
            return parsed, _validate_request_path(parsed.path)

        def _fetch_site_allowed(self) -> bool:
            values = self._header_values("Sec-Fetch-Site")
            return not values or (len(values) == 1 and values[0] == "same-origin")

        def _edit_request_authorized(self) -> bool:
            if edit_token is None or not self._fetch_site_allowed():
                return False
            host_values = self._header_values("Host")
            origin_values = self._header_values("Origin")
            token_values = self._header_values(EDIT_TOKEN_HEADER)
            if (
                len(host_values) != 1
                or len(origin_values) != 1
                or len(token_values) != 1
            ):
                return False
            if not _origin_matches_host(
                origin_values[0],
                host_values[0],
                server_port=int(self.server.server_address[1]),
            ):
                return False
            try:
                return secrets.compare_digest(token_values[0], edit_token)
            except (TypeError, ValueError):
                return False

        def _read_edit_body(self) -> bytes:
            if self._header_values("Transfer-Encoding"):
                self.close_connection = True
                raise _HttpEditRequestError(400, "transfer-encoding-forbidden")
            lengths = self._header_values("Content-Length")
            if not lengths:
                self.close_connection = True
                raise _HttpEditRequestError(411, "content-length-required")
            if (
                len(lengths) != 1
                or len(lengths[0]) > 20
                or re.fullmatch(r"[0-9]+", lengths[0]) is None
            ):
                self.close_connection = True
                raise _HttpEditRequestError(400, "content-length-invalid")
            length = int(lengths[0], 10)
            if length > MAX_EDIT_REQUEST_BYTES:
                self.close_connection = True
                raise _HttpEditRequestError(413, "edit-request-too-large")
            content_types = self._header_values("Content-Type")
            if len(content_types) != 1 or not _json_content_type_allowed(
                content_types[0]
            ):
                self.close_connection = True
                raise _HttpEditRequestError(415, "content-type-invalid")
            body = self.rfile.read(length)
            if len(body) != length:
                self.close_connection = True
                raise _HttpEditRequestError(400, "edit-request-truncated")
            return body

        def _not_found(self, *, head: bool = False) -> None:
            self.send_json(
                404,
                {"schema_version": SCHEMA_VERSION, "error": "not_found"},
                head=head,
            )

        def _invalid_request(self, *, head: bool = False) -> None:
            self.send_json(
                400,
                {"schema_version": SCHEMA_VERSION, "error": "invalid_request"},
                head=head,
            )

        def serve(self, *, head: bool = False) -> None:
            if not self.allowed():
                return
            try:
                parsed, path = self._request_target()
                if path in routes:
                    self._query(parsed, allowed=frozenset())
                    body, content_type = routes[path]
                    self.send_bytes(200, body, content_type, api=False, head=head)
                    return
                if path in {"/api/health", "/api/status"}:
                    self._query(parsed, allowed=frozenset())
                    self.send_json(200, cockpit.health(), head=head)
                    return
                if path == EDIT_SESSION_PATH:
                    self._query(parsed, allowed=frozenset())
                    if not cockpit.editing_enabled or edit_token is None:
                        self._not_found(head=head)
                        return
                    if not self._fetch_site_allowed():
                        self.send_json(
                            403,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "error": "forbidden",
                                "reason_code": "same-origin-required",
                            },
                            head=head,
                        )
                        return
                    session = dict(cockpit.edit_session())
                    session["edit_token"] = edit_token
                    session["token_header"] = EDIT_TOKEN_HEADER
                    self.send_json(200, session, head=head)
                    return
                if path == "/api/projects":
                    self._query(parsed, allowed=frozenset())
                    self.send_json(200, cockpit.list_projects(), head=head)
                    return
                if path == "/api/snapshot":
                    query = self._query(parsed, allowed=frozenset({"project_id"}))
                    selected = self._one_query(query, "project_id")
                    if selected is not None and selected == "":
                        raise CockpitPathError("invalid project_id")
                    self.send_json(200, cockpit.snapshot(selected), head=head)
                    return
                segments = path.split("/")[1:]
                if (
                    len(segments) < 2
                    or segments[0] != "api"
                    or segments[1] != "projects"
                ):
                    self._query(parsed, allowed=frozenset())
                    self._not_found(head=head)
                    return
                if len(segments) < 3 or not segments[2]:
                    self._query(parsed, allowed=frozenset())
                    self._not_found(head=head)
                    return
                project_id = self._decode_segment(segments[2])
                if len(segments) == 3:
                    self._query(parsed, allowed=frozenset())
                    self.send_json(200, cockpit.project_snapshot(project_id), head=head)
                    return
                resource = segments[3]
                if resource == "files" and len(segments) == 4:
                    query = self._query(
                        parsed, allowed=frozenset({"offset", "limit", "status"})
                    )
                    self.send_json(
                        200,
                        cockpit.project_files(
                            project_id,
                            offset=self._integer_query(query, "offset", 0),
                            limit=self._integer_query(query, "limit", 100),
                            status=self._one_query(query, "status"),
                        ),
                        head=head,
                    )
                    return
                if resource == "claims" and len(segments) == 4:
                    query = self._query(
                        parsed, allowed=frozenset({"offset", "limit", "status"})
                    )
                    self.send_json(
                        200,
                        cockpit.project_claims(
                            project_id,
                            offset=self._integer_query(query, "offset", 0),
                            limit=self._integer_query(query, "limit", 100),
                            status=self._one_query(query, "status"),
                        ),
                        head=head,
                    )
                    return
                if resource == "knowledge" and len(segments) == 4:
                    query = self._query(parsed, allowed=frozenset({"path"}))
                    knowledge_path = self._one_query(query, "path", required=True)
                    assert knowledge_path is not None
                    self.send_json(
                        200,
                        cockpit.project_knowledge(project_id, knowledge_path),
                        head=head,
                    )
                    return
                if resource == "edits" and len(segments) == 5:
                    self._query(parsed, allowed=frozenset())
                    if not cockpit.editing_enabled or not self._fetch_site_allowed():
                        self._not_found(head=head)
                        return
                    target_key = self._decode_segment(segments[4])
                    if target_key not in _EDIT_TARGETS_BY_KEY:
                        self._not_found(head=head)
                        return
                    self.send_json(
                        200,
                        cockpit.project_edit_snapshot(project_id, target_key),
                        head=head,
                    )
                    return
                if resource in {"evidence", "sources"} and len(segments) == 5:
                    self._query(parsed, allowed=frozenset())
                    identifier = self._decode_segment(segments[4])
                    if resource == "evidence":
                        payload = cockpit.project_evidence(project_id, identifier)
                    else:
                        payload = cockpit.project_source(project_id, identifier)
                    self.send_json(200, payload, head=head)
                    return
                self._query(parsed, allowed=frozenset())
                self._not_found(head=head)
            except CockpitEditingError as exc:
                self.send_json(exc.http_status, exc.as_dict(), head=head)
            except CockpitPathError:
                self._invalid_request(head=head)
            except (CockpitProjectUnavailable, KeyError):
                self._not_found(head=head)
            except CockpitAssetError:
                self.send_json(
                    500,
                    {"schema_version": SCHEMA_VERSION, "error": "asset_unavailable"},
                    head=head,
                )
            except ResearchCockpitError:
                self.send_json(
                    503,
                    {"schema_version": SCHEMA_VERSION, "error": "data_unavailable"},
                    head=head,
                )
            except (OSError, ValueError, TypeError):
                self._invalid_request(head=head)

        def do_GET(self) -> None:
            self.serve()

        def do_HEAD(self) -> None:
            self.serve(head=True)

        def do_POST(self) -> None:
            if not self.allowed():
                return
            if not cockpit.editing_enabled:
                self.reject(already_allowed=True)
                return
            try:
                parsed, path = self._request_target()
                segments = path.split("/")[1:]
                if not (
                    len(segments) == 5
                    and segments[0] == "api"
                    and segments[1] == "projects"
                    and segments[3] == "edits"
                ):
                    if (
                        len(segments) >= 4
                        and segments[0] == "api"
                        and segments[1] == "projects"
                        and segments[3] == "edits"
                    ):
                        self._not_found()
                    else:
                        self.reject(already_allowed=True)
                    return
                self._query(parsed, allowed=frozenset())
                project_id = self._decode_segment(segments[2])
                target_key = self._decode_segment(segments[4])
                if target_key not in _EDIT_TARGETS_BY_KEY:
                    self._not_found()
                    return
                if not self._edit_request_authorized():
                    self.send_json(
                        403,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "error": "forbidden",
                            "reason_code": "edit-authorization-required",
                        },
                    )
                    return
                request = _strict_json_object(self._read_edit_body())
                result = cockpit.project_edit(
                    project_id,
                    target_key,
                    expected_current_sha256=request["expected_current_sha256"],
                    content=request["content"],
                )
                self.send_json(200, result)
            except _HttpEditRequestError as exc:
                self.send_json(exc.status, exc.as_dict())
            except CockpitEditingError as exc:
                self.send_json(exc.http_status, exc.as_dict())
            except ControlledMarkdownPersistenceError as exc:
                self.send_json(
                    _persistence_http_status(exc), _persistence_error_payload(exc)
                )
            except CockpitPathError:
                self._invalid_request()
            except (CockpitProjectUnavailable, KeyError):
                self._not_found()
            except ResearchCockpitError:
                self.send_json(
                    503,
                    {"schema_version": SCHEMA_VERSION, "error": "data_unavailable"},
                )
            except (OSError, ValueError, TypeError):
                self._invalid_request()

        def reject(self, *, already_allowed: bool = False) -> None:
            if not already_allowed and not self.allowed():
                return
            read_only = not cockpit.editing_enabled
            self.send_json(
                405,
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": "read_only" if read_only else "method_not_allowed",
                    "message": (
                        "The research cockpit is read-only."
                        if read_only
                        else "Only the fixed controlled-edit POST route may mutate state."
                    ),
                },
            )

        def __getattr__(self, name: str) -> Any:
            # ``BaseHTTPRequestHandler`` normally emits 501 for an unknown
            # method. Resolve every non-GET/HEAD/POST method to the same bounded
            # no-mutation rejection instead.
            if name.startswith("do_"):
                return self.reject
            raise AttributeError(name)

        do_PUT = reject
        do_PATCH = reject
        do_DELETE = reject
        do_OPTIONS = reject
        do_TRACE = reject
        do_CONNECT = reject

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.client_address[0], self.log_date_time_string(), fmt % args)
            )

    return Handler


def create_server(
    cockpit: ResearchCockpit,
    *,
    asset_root: Path = DEFAULT_ASSET_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    host = validate_bind_host(host)
    if not 0 <= port <= 65535:
        raise ResearchCockpitError("Cockpit port is invalid.")
    handler = make_handler(cockpit, asset_root)
    if ":" in host:

        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_class: type[ThreadingHTTPServer] = IPv6Server
    else:
        server_class = ThreadingHTTPServer
    server = server_class((host, port), handler)
    server.daemon_threads = True
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local research cockpit with optional controlled editing"
    )
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    subs = parser.add_subparsers(dest="command", required=True)
    serve = subs.add_parser("serve", help="serve the cockpit on loopback")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    serve.add_argument("--open", action="store_true", dest="open_browser")
    serve.add_argument(
        "--enable-editing",
        action="store_true",
        help="enable the four fixed F-05 controlled Markdown edit targets",
    )
    serve.add_argument(
        "--actor-id",
        default="local-user",
        help="bounded trusted local actor identifier for body-free audit attribution",
    )
    snapshot = subs.add_parser("snapshot", help="print a cockpit snapshot")
    snapshot.add_argument("--project-id")
    snapshot.add_argument("--pretty", action="store_true")
    health = subs.add_parser("health", help="print cockpit health")
    health.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        edit_context = None
        if args.command == "serve" and args.enable_editing:
            edit_context = TrustedHostSessionContext(
                host_id="research-cockpit",
                actor_type="user",
                actor_id=args.actor_id,
                session_id=f"web-session-{secrets.token_hex(16)}",
            )
        cockpit = ResearchCockpit(args.workspace_root, edit_context=edit_context)
        if args.command == "snapshot":
            payload = cockpit.snapshot(args.project_id)
            print(
                json.dumps(
                    payload, ensure_ascii=False, indent=2 if args.pretty else None
                )
            )
            return 0
        if args.command == "health":
            payload = cockpit.health()
            print(
                json.dumps(
                    payload, ensure_ascii=False, indent=2 if args.pretty else None
                )
            )
            return 0
        server = create_server(
            cockpit,
            asset_root=args.asset_root,
            host=args.host,
            port=args.port,
        )
        host, bound_port = server.server_address[:2]
        display = f"[{host}]" if ":" in str(host) else host
        url = f"http://{display}:{bound_port}/"
        print(f"Research cockpit: {url}")
        if cockpit.editing_enabled:
            print(
                "Controlled editing enabled for four fixed mixed-page user regions; "
                "loopback requests only. Press Ctrl+C to stop."
            )
        else:
            print("Read-only; loopback requests only. Press Ctrl+C to stop.")
        if args.open_browser:
            import webbrowser

            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ResearchCockpitError, ControlledMarkdownAuthorizationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
