#!/usr/bin/env python3
"""Deterministic registration for external research-project directories.

B-01 assigns a stable project identity and initializes storage outside the
source project. It deliberately does not scan or read project files.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Support both ``import tools.project_registry`` and direct sibling imports.
if __package__:
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        ProjectLayout,
        WorkspaceLayout,
        load_versioned_json,
        validate_project_id,
        write_versioned_json,
    )
else:
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        ProjectLayout,
        WorkspaceLayout,
        load_versioned_json,
        validate_project_id,
        write_versioned_json,
    )


PROJECT_SCHEMA_KIND = "llmwiki-project"
PROJECT_ID_STRATEGY = "path-sha256-v1"
MANUAL_PROJECT_ID_STRATEGY = "manual-v1"
_PROJECT_ID_DIGEST_LENGTH = 12
_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


class ProjectRegistrationError(LayoutError):
    """Base error for registration, record, or storage conflicts."""


class ProjectPathError(ProjectRegistrationError):
    """Raised when project or output paths are invalid or unsafe."""


class ProjectConflictError(ProjectRegistrationError):
    """Raised when an identity or storage location is already occupied."""


class ProjectRecordError(ProjectRegistrationError):
    """Raised when a persisted project record cannot be trusted."""


@dataclass(frozen=True)
class ProjectRegistrationResult:
    """The persisted project registration and its resolved layout."""

    created: bool
    project_id: str
    project_root: Path
    layout: ProjectLayout
    project_file: Path
    record: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable command result."""

        return {
            "created": self.created,
            "project_id": self.project_id,
            "name": self.record["name"],
            "project_root": str(self.project_root),
            "project_file": str(self.project_file),
            "machine_root": str(self.layout.machine_root),
            "knowledge_root": str(self.layout.knowledge_root),
            "record": self.record,
        }


def normalize_project_root(project_root: str | Path) -> Path:
    """Resolve an existing project directory without modifying it."""

    candidate = Path(project_root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ProjectPathError(f"project path does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ProjectPathError(f"project path is not a directory: {resolved}")
    return resolved


def _normalized_output_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_key(path: str | Path) -> str:
    """Return a stable comparison/hash key for a normalized local path."""

    normalized = os.path.normcase(os.path.normpath(str(Path(path).resolve())))
    return unicodedata.normalize("NFC", normalized)


def _slugify_project_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_SEPARATOR_RE.sub("-", ascii_name).strip("-")
    return slug or "project"


def generate_project_id(project_root: str | Path) -> str:
    """Generate ``<directory-slug>-<path-hash>`` for an existing project."""

    root = normalize_project_root(project_root)
    digest = hashlib.sha256(_path_key(root).encode("utf-8")).hexdigest()
    digest = digest[:_PROJECT_ID_DIGEST_LENGTH]
    max_slug_length = 64 - len(digest) - 1
    slug = _slugify_project_name(root.name)[:max_slug_length].rstrip("-")
    slug = slug or "project"
    candidate = f"{slug}-{digest}"
    try:
        return validate_project_id(candidate)
    except LayoutError:
        fallback_slug = f"project-{slug}"[:max_slug_length].rstrip("-")
        return validate_project_id(f"{fallback_slug}-{digest}")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _validate_disjoint_output_paths(
    project_root: Path,
    machine_root: Path,
    knowledge_root: Path,
) -> None:
    project_root = project_root.resolve()
    machine_root = machine_root.resolve()
    knowledge_root = knowledge_root.resolve()

    if _paths_overlap(machine_root, knowledge_root):
        raise ProjectPathError(
            "machine state and knowledge roots must not overlap: "
            f"{machine_root} vs {knowledge_root}"
        )

    for label, output_root in (
        ("machine state", machine_root),
        ("knowledge", knowledge_root),
    ):
        if _paths_overlap(project_root, output_root):
            raise ProjectPathError(
                f"{label} output root must be outside the source project: "
                f"{output_root}"
            )


def _validate_output_boundaries(
    project_root: Path,
    workspace_root: Path,
    knowledge_projects_root: Path,
) -> None:
    machine_projects_root = WorkspaceLayout(workspace_root).machine_projects_root
    _validate_disjoint_output_paths(
        project_root,
        machine_projects_root,
        knowledge_projects_root,
    )


def _validate_layout_boundaries(project_root: Path, layout: ProjectLayout) -> None:
    """Resolve project-specific paths so existing symlinks cannot bypass safety."""

    _validate_disjoint_output_paths(
        project_root,
        layout.machine_root,
        layout.knowledge_root,
    )


def _validate_unclaimed_directory(
    root: Path,
    allowed_empty_directories: set[str],
    label: str,
) -> None:
    if not root.exists():
        return
    if not root.is_dir():
        raise ProjectConflictError(f"unregistered {label} path is not a directory: {root}")

    for entry in root.iterdir():
        if entry.name not in allowed_empty_directories or not entry.is_dir():
            raise ProjectConflictError(
                f"unregistered {label} storage is not empty: {root}"
            )
        if any(entry.iterdir()):
            raise ProjectConflictError(
                f"unregistered {label} storage contains data: {entry}"
            )


def _validate_unclaimed_layout(layout: ProjectLayout) -> None:
    _validate_unclaimed_directory(
        layout.machine_root,
        {path.name for path in layout.machine_directories},
        "machine",
    )
    _validate_unclaimed_directory(
        layout.knowledge_root,
        {path.name for path in layout.knowledge_directories},
        "knowledge",
    )


def _required_string(record: dict[str, Any], key: str, project_file: Path) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectRecordError(f"{project_file}: {key!r} must be a non-empty string")
    return value


def _absolute_record_path(value: Any, field: str, project_file: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProjectRecordError(
            f"{project_file}: {field!r} must be a non-empty absolute path"
        )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ProjectRecordError(f"{project_file}: {field!r} must be absolute")
    return candidate.resolve()


def _load_project_record(
    project_file: Path,
    workspace_root: Path,
) -> tuple[dict[str, Any], Path, ProjectLayout]:
    try:
        document = load_versioned_json(
            project_file,
            allow_legacy=False,
            max_supported=CURRENT_SCHEMA_VERSION,
        )
    except (LayoutError, OSError, UnicodeError) as exc:
        raise ProjectRecordError(
            f"cannot safely read project record {project_file}: {exc}"
        ) from exc

    record = document.data
    if record.get("kind") != PROJECT_SCHEMA_KIND:
        raise ProjectRecordError(
            f"{project_file}: unexpected project kind {record.get('kind')!r}"
        )

    try:
        project_id = validate_project_id(
            _required_string(record, "project_id", project_file)
        )
    except LayoutError as exc:
        raise ProjectRecordError(f"{project_file}: invalid project_id: {exc}") from exc
    if project_id != project_file.parent.name:
        raise ProjectRecordError(
            f"{project_file}: project_id does not match its storage directory"
        )

    _required_string(record, "name", project_file)
    _required_string(record, "registered_at", project_file)
    identity_strategy = _required_string(record, "identity_strategy", project_file)
    if identity_strategy not in {PROJECT_ID_STRATEGY, MANUAL_PROJECT_ID_STRATEGY}:
        raise ProjectRecordError(
            f"{project_file}: unsupported identity_strategy {identity_strategy!r}"
        )

    root_path = _absolute_record_path(record.get("root_path"), "root_path", project_file)
    storage = record.get("storage")
    if not isinstance(storage, dict):
        raise ProjectRecordError(f"{project_file}: 'storage' must be an object")

    stored_workspace_root = _absolute_record_path(
        storage.get("workspace_root"),
        "storage.workspace_root",
        project_file,
    )
    if _path_key(stored_workspace_root) != _path_key(workspace_root):
        raise ProjectRecordError(
            f"{project_file}: stored workspace_root does not match active workspace"
        )

    knowledge_projects_root = _absolute_record_path(
        storage.get("knowledge_projects_root"),
        "storage.knowledge_projects_root",
        project_file,
    )
    layout = ProjectLayout(
        workspace_root=workspace_root,
        project_id=project_id,
        custom_knowledge_projects_root=knowledge_projects_root,
    )
    stored_machine_root = _absolute_record_path(
        storage.get("machine_root"),
        "storage.machine_root",
        project_file,
    )
    stored_knowledge_root = _absolute_record_path(
        storage.get("knowledge_root"),
        "storage.knowledge_root",
        project_file,
    )
    if _path_key(stored_machine_root) != _path_key(layout.machine_root):
        raise ProjectRecordError(
            f"{project_file}: stored machine_root does not match workspace layout"
        )
    if _path_key(stored_knowledge_root) != _path_key(layout.knowledge_root):
        raise ProjectRecordError(
            f"{project_file}: stored knowledge_root does not match project layout"
        )

    git = record.get("git")
    if not isinstance(git, dict):
        raise ProjectRecordError(f"{project_file}: 'git' must be an object")
    if not isinstance(git.get("available"), bool) or not isinstance(
        git.get("is_repository"),
        bool,
    ):
        raise ProjectRecordError(
            f"{project_file}: Git availability fields must be booleans"
        )
    for field in ("root", "branch", "head_commit", "origin_url"):
        if git.get(field) is not None and not isinstance(git.get(field), str):
            raise ProjectRecordError(
                f"{project_file}: git.{field} must be a string or null"
            )
    origin_url = git.get("origin_url")
    if origin_url is not None and sanitize_git_remote_url(origin_url) != origin_url:
        raise ProjectRecordError(
            f"{project_file}: git.origin_url contains credentials or unsafe data"
        )

    onboarding = record.get("onboarding")
    if not isinstance(onboarding, dict):
        raise ProjectRecordError(f"{project_file}: 'onboarding' must be an object")
    for field in (
        "final_goal",
        "current_stage",
        "important_question",
        "deadline",
    ):
        if onboarding.get(field) is not None and not isinstance(
            onboarding.get(field),
            str,
        ):
            raise ProjectRecordError(
                f"{project_file}: onboarding.{field} must be a string or null"
            )
    hours = onboarding.get("daily_available_hours")
    if hours is not None and (
        isinstance(hours, bool) or not isinstance(hours, (int, float))
    ):
        raise ProjectRecordError(
            f"{project_file}: onboarding.daily_available_hours must be a number or null"
        )

    return record, root_path, layout


def _find_registration_by_root(
    workspace_root: Path,
    project_root: Path,
) -> tuple[dict[str, Any], ProjectLayout] | None:
    machine_projects_root = WorkspaceLayout(workspace_root).machine_projects_root
    if not machine_projects_root.exists():
        return None

    matches: list[tuple[dict[str, Any], ProjectLayout]] = []
    for project_file in sorted(machine_projects_root.glob("*/project.yaml")):
        record, registered_root, layout = _load_project_record(
            project_file,
            workspace_root,
        )
        if _path_key(registered_root) == _path_key(project_root):
            matches.append((record, layout))

    if len(matches) > 1:
        ids = ", ".join(record["project_id"] for record, _layout in matches)
        raise ProjectConflictError(
            f"project path is registered more than once ({ids}): {project_root}"
        )
    return matches[0] if matches else None


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ProjectRegistrationError(f"{field} cannot be empty")
    return normalized


def _normalize_deadline(value: str | None) -> str | None:
    normalized = _optional_text(value, "deadline")
    if normalized is None:
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ProjectRegistrationError("deadline must use YYYY-MM-DD") from exc


def _normalize_daily_hours(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectRegistrationError("daily_available_hours must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0 or normalized > 24:
        raise ProjectRegistrationError(
            "daily_available_hours must be greater than 0 and at most 24"
        )
    return normalized


def _validate_existing_metadata(
    record: dict[str, Any],
    *,
    name: str | None,
    final_goal: str | None,
    current_stage: str | None,
    important_question: str | None,
    deadline: str | None,
    daily_available_hours: float | None,
) -> None:
    supplied: dict[str, Any] = {}
    if name is not None:
        supplied["name"] = _optional_text(name, "name")
    onboarding_inputs = {
        "final_goal": (
            _optional_text(final_goal, "final_goal")
            if final_goal is not None
            else None
        ),
        "current_stage": (
            _optional_text(current_stage, "current_stage")
            if current_stage is not None
            else None
        ),
        "important_question": (
            _optional_text(important_question, "important_question")
            if important_question is not None
            else None
        ),
        "deadline": _normalize_deadline(deadline) if deadline is not None else None,
        "daily_available_hours": (
            _normalize_daily_hours(daily_available_hours)
            if daily_available_hours is not None
            else None
        ),
    }

    if "name" in supplied and supplied["name"] != record["name"]:
        raise ProjectConflictError(
            "project is already registered with a different name; "
            "registration does not update metadata"
        )
    onboarding = record["onboarding"]
    for field, value in onboarding_inputs.items():
        if value is not None and value != onboarding.get(field):
            raise ProjectConflictError(
                f"project is already registered with a different {field}; "
                "registration does not update metadata"
            )


def sanitize_git_remote_url(remote_url: str | None) -> str | None:
    """Remove URL credentials and HTTP query tokens before persistence."""

    if remote_url is None:
        return None
    value = remote_url.strip()
    if not value:
        return None
    if "://" not in value:
        # Preserve conventional SSH remotes such as git@github.com:org/repo.git.
        return value

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not parsed.scheme or hostname is None:
        return None

    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    query = "" if parsed.scheme.lower() in {"http", "https"} else parsed.query
    fragment = "" if parsed.scheme.lower() in {"http", "https"} else parsed.fragment
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def _run_git(project_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def collect_git_metadata(project_root: str | Path) -> dict[str, Any]:
    """Collect local, read-only Git identity without contacting a remote."""

    root = Path(project_root).expanduser().resolve()
    available = shutil.which("git") is not None
    metadata: dict[str, Any] = {
        "available": available,
        "is_repository": False,
        "root": None,
        "branch": None,
        "head_commit": None,
        "origin_url": None,
    }
    if not available:
        return metadata

    git_root = _run_git(root, "rev-parse", "--show-toplevel")
    if git_root is None:
        return metadata

    metadata.update(
        {
            "is_repository": True,
            "root": str(Path(git_root).expanduser().resolve()),
            "branch": _run_git(root, "branch", "--show-current"),
            "head_commit": _run_git(root, "rev-parse", "HEAD"),
            "origin_url": sanitize_git_remote_url(
                _run_git(root, "config", "--get", "remote.origin.url")
            ),
        }
    )
    return metadata


def _registration_result(
    *,
    created: bool,
    project_root: Path,
    record: dict[str, Any],
    layout: ProjectLayout,
) -> ProjectRegistrationResult:
    return ProjectRegistrationResult(
        created=created,
        project_id=record["project_id"],
        project_root=project_root,
        layout=layout,
        project_file=layout.project_file,
        record=record,
    )


def load_registered_project(
    workspace_root: str | Path,
    project_id: str,
) -> ProjectRegistrationResult:
    """Load one persisted B-01 registration without changing any storage."""

    workspace = _normalized_output_root(workspace_root)
    normalized_id = validate_project_id(project_id)
    project_file = (
        WorkspaceLayout(workspace).machine_projects_root
        / normalized_id
        / "project.yaml"
    )
    if not project_file.exists():
        raise ProjectRecordError(f"project is not registered: {normalized_id}")
    if not project_file.is_file():
        raise ProjectRecordError(
            f"registered project record is not a file: {project_file}"
        )

    record, stored_root, layout = _load_project_record(project_file, workspace)
    try:
        project_root = normalize_project_root(stored_root)
    except ProjectPathError as exc:
        raise ProjectRecordError(
            f"registered project root is unavailable: {stored_root}"
        ) from exc
    if _path_key(project_root) != _path_key(stored_root):
        raise ProjectRecordError(
            f"registered project root no longer resolves consistently: {stored_root}"
        )

    _validate_output_boundaries(
        project_root,
        workspace,
        layout.knowledge_projects_root,
    )
    _validate_layout_boundaries(project_root, layout)
    return _registration_result(
        created=False,
        project_root=project_root,
        record=record,
        layout=layout,
    )


def register_project(
    workspace_root: str | Path,
    project_root: str | Path,
    *,
    project_id: str | None = None,
    name: str | None = None,
    knowledge_root: str | Path | None = None,
    final_goal: str | None = None,
    current_stage: str | None = None,
    important_question: str | None = None,
    deadline: str | None = None,
    daily_available_hours: float | None = None,
) -> ProjectRegistrationResult:
    """Register one project without scanning or writing to its source tree.

    ``knowledge_root`` is the parent directory that will contain one curated
    Markdown directory per project: ``<knowledge_root>/<project_id>/``.
    """

    root = normalize_project_root(project_root)
    workspace = _normalized_output_root(workspace_root)
    requested_knowledge_root = (
        _normalized_output_root(knowledge_root)
        if knowledge_root is not None
        else None
    )
    knowledge_projects_root = (
        requested_knowledge_root
        if requested_knowledge_root is not None
        else WorkspaceLayout(workspace).knowledge_projects_root
    )

    normalized_requested_id = (
        validate_project_id(project_id) if project_id is not None else None
    )
    existing = _find_registration_by_root(workspace, root)
    if existing is not None:
        record, layout = existing
        if (
            normalized_requested_id is not None
            and normalized_requested_id != record["project_id"]
        ):
            raise ProjectConflictError(
                f"project is already registered as {record['project_id']!r}"
            )
        if (
            requested_knowledge_root is not None
            and _path_key(requested_knowledge_root)
            != _path_key(layout.knowledge_projects_root)
        ):
            raise ProjectConflictError(
                "project is already registered with a different knowledge_root"
            )
        _validate_existing_metadata(
            record,
            name=name,
            final_goal=final_goal,
            current_stage=current_stage,
            important_question=important_question,
            deadline=deadline,
            daily_available_hours=daily_available_hours,
        )
        _validate_output_boundaries(root, workspace, layout.knowledge_projects_root)
        _validate_layout_boundaries(root, layout)
        return _registration_result(
            created=False,
            project_root=root,
            record=record,
            layout=layout,
        )

    _validate_output_boundaries(root, workspace, knowledge_projects_root)
    chosen_id = normalized_requested_id or generate_project_id(root)
    layout = ProjectLayout(
        workspace_root=workspace,
        project_id=chosen_id,
        custom_knowledge_projects_root=knowledge_projects_root,
    )
    _validate_layout_boundaries(root, layout)

    if layout.project_file.exists():
        record, registered_root, existing_layout = _load_project_record(
            layout.project_file,
            workspace,
        )
        if _path_key(registered_root) != _path_key(root):
            raise ProjectConflictError(
                f"project_id {chosen_id!r} is already used by {registered_root}"
            )
        _validate_layout_boundaries(root, existing_layout)
        return _registration_result(
            created=False,
            project_root=root,
            record=record,
            layout=existing_layout,
        )

    _validate_unclaimed_layout(layout)
    project_name = _optional_text(name, "name") if name is not None else root.name
    project_name = project_name or chosen_id
    onboarding = {
        "final_goal": _optional_text(final_goal, "final_goal"),
        "current_stage": _optional_text(current_stage, "current_stage"),
        "important_question": _optional_text(
            important_question,
            "important_question",
        ),
        "deadline": _normalize_deadline(deadline),
        "daily_available_hours": _normalize_daily_hours(daily_available_hours),
    }

    record: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "kind": PROJECT_SCHEMA_KIND,
        "project_id": chosen_id,
        "identity_strategy": (
            MANUAL_PROJECT_ID_STRATEGY
            if normalized_requested_id is not None
            else PROJECT_ID_STRATEGY
        ),
        "name": project_name,
        "root_path": str(root),
        "registered_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "storage": {
            "workspace_root": str(workspace),
            "machine_root": str(layout.machine_root),
            "knowledge_projects_root": str(layout.knowledge_projects_root),
            "knowledge_root": str(layout.knowledge_root),
        },
        "git": collect_git_metadata(root),
        "onboarding": onboarding,
    }

    layout.ensure_directories()
    if layout.project_file.exists():
        raise ProjectConflictError(
            f"project record appeared during registration: {layout.project_file}"
        )
    write_versioned_json(layout.project_file, record)
    persisted, persisted_root, persisted_layout = _load_project_record(
        layout.project_file,
        workspace,
    )
    if _path_key(persisted_root) != _path_key(root):
        raise ProjectRecordError("persisted project root failed round-trip validation")
    return _registration_result(
        created=True,
        project_root=root,
        record=persisted,
        layout=persisted_layout,
    )
