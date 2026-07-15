#!/usr/bin/env python3
"""Versioned storage layout for project-scoped research-assistant data.

The v1 contract deliberately separates local, machine-maintained state from
human-readable knowledge:

* ``.llmwiki/projects/<project_id>/`` stores machine state and derived data.
* ``wiki/projects/<project_id>/`` stores curated Markdown knowledge.

This module only defines and initializes the layout. Project registration,
scanning, manifests, extraction, and migration are implemented by later tasks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


CURRENT_SCHEMA_VERSION = 1
LEGACY_SCHEMA_VERSION = 0
WORKSPACE_SCHEMA_KIND = "llmwiki-workspace"

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_MACHINE_SUBDIRECTORIES = ("extracted", "indexes", "runs")
_KNOWLEDGE_SUBDIRECTORIES = ("sources", "papers", "experiments", "claims", "plans")


class LayoutError(ValueError):
    """Base error for invalid layout or schema data."""


class InvalidProjectIdError(LayoutError):
    """Raised when a project id could escape or ambiguously address its root."""


class SchemaVersionError(LayoutError):
    """Raised when a versioned document has an invalid schema version."""


class UnsupportedSchemaVersionError(SchemaVersionError):
    """Raised when a document was written by a newer unsupported schema."""


@dataclass(frozen=True)
class VersionedDocument:
    """A JSON object plus the schema interpretation used to read it."""

    data: dict[str, Any]
    schema_version: int
    is_legacy: bool
    path: Path


def validate_project_id(project_id: str) -> str:
    """Validate a stable, path-safe project id and return it unchanged.

    Project id generation belongs to B-01. A-03 only defines the safe value
    accepted by the storage layer, so callers cannot use absolute paths,
    separators, traversal components, spaces, or case-ambiguous ids.
    """

    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise InvalidProjectIdError(
            "project_id must be 1-64 lowercase characters using only "
            "a-z, 0-9, '.', '_' or '-', and must start with a letter or digit"
        )
    if project_id in {".", ".."}:
        raise InvalidProjectIdError("project_id cannot be '.' or '..'")
    if project_id.endswith("."):
        raise InvalidProjectIdError("project_id cannot end with a period")
    if project_id.split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        raise InvalidProjectIdError("project_id cannot use a reserved Windows filename")
    return project_id


def schema_version_of(
    data: dict[str, Any],
    *,
    allow_legacy: bool = True,
    max_supported: int = CURRENT_SCHEMA_VERSION,
) -> int:
    """Return a validated schema version.

    Documents without ``schema_version`` are treated as legacy v0 only when
    ``allow_legacy`` is true. Future versions fail closed instead of being
    silently interpreted with an older schema.
    """

    if not isinstance(data, dict):
        raise SchemaVersionError("versioned JSON must contain an object at the top level")

    if "schema_version" not in data:
        if allow_legacy:
            return LEGACY_SCHEMA_VERSION
        raise SchemaVersionError("schema_version is required")

    version = data["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise SchemaVersionError("schema_version must be a non-negative integer")
    if version == LEGACY_SCHEMA_VERSION and not allow_legacy:
        raise SchemaVersionError("legacy schema_version 0 is not allowed")
    if version > max_supported:
        raise UnsupportedSchemaVersionError(
            f"schema_version {version} is newer than supported version {max_supported}"
        )
    return version


def load_versioned_json(
    path: Path,
    *,
    allow_legacy: bool = True,
    max_supported: int = CURRENT_SCHEMA_VERSION,
) -> VersionedDocument:
    """Read a versioned JSON object without mutating or migrating it."""

    resolved = Path(path).expanduser().resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LayoutError(f"invalid JSON in {resolved}: {exc}") from exc

    if not isinstance(data, dict):
        raise SchemaVersionError("versioned JSON must contain an object at the top level")

    version = schema_version_of(
        data,
        allow_legacy=allow_legacy,
        max_supported=max_supported,
    )
    return VersionedDocument(
        data=data,
        schema_version=version,
        is_legacy=version == LEGACY_SCHEMA_VERSION,
        path=resolved,
    )


def write_versioned_json(path: Path, payload: dict[str, Any]) -> Path:
    """Atomically write a current-schema JSON object.

    Callers may supply ``schema_version`` only when it already equals the
    current version. This prevents accidental downgrades or future-version
    documents being overwritten by old code.
    """

    if not isinstance(payload, dict):
        raise SchemaVersionError("versioned JSON payload must be an object")

    supplied_version = payload.get("schema_version", CURRENT_SCHEMA_VERSION)
    if (
        isinstance(supplied_version, bool)
        or not isinstance(supplied_version, int)
        or supplied_version != CURRENT_SCHEMA_VERSION
    ):
        raise SchemaVersionError(
            f"writer only supports integer schema_version {CURRENT_SCHEMA_VERSION}"
        )

    data = {"schema_version": CURRENT_SCHEMA_VERSION}
    data.update({key: value for key, value in payload.items() if key != "schema_version"})

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


@dataclass(frozen=True)
class WorkspaceLayout:
    """Workspace-level roots shared by all registered research projects."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def machine_root(self) -> Path:
        return self.root / ".llmwiki"

    @property
    def machine_projects_root(self) -> Path:
        return self.machine_root / "projects"

    @property
    def knowledge_projects_root(self) -> Path:
        return self.root / "wiki" / "projects"

    @property
    def schema_file(self) -> Path:
        return self.machine_root / "schema.json"

    def ensure(self) -> VersionedDocument:
        """Create layout roots and a v1 marker, preserving legacy markers.

        Existing markers are validated but never rewritten implicitly. A
        separate future migration task must perform any schema upgrade.
        """

        self.machine_projects_root.mkdir(parents=True, exist_ok=True)
        self.knowledge_projects_root.mkdir(parents=True, exist_ok=True)

        if not self.schema_file.exists():
            write_versioned_json(
                self.schema_file,
                {
                    "kind": WORKSPACE_SCHEMA_KIND,
                    "machine_state_root": ".llmwiki/projects",
                    "knowledge_root": "wiki/projects",
                },
            )

        document = load_versioned_json(self.schema_file, allow_legacy=True)
        kind = document.data.get("kind")
        valid_kind = (
            kind in {None, WORKSPACE_SCHEMA_KIND}
            if document.is_legacy
            else kind == WORKSPACE_SCHEMA_KIND
        )
        if not valid_kind:
            raise LayoutError(
                f"unexpected workspace schema kind {kind!r} in {self.schema_file}"
            )
        return document


@dataclass(frozen=True)
class ProjectLayout:
    """Paths for one project under the v1 separated storage contract."""

    workspace_root: Path
    project_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_root",
            Path(self.workspace_root).expanduser().resolve(),
        )
        object.__setattr__(self, "project_id", validate_project_id(self.project_id))

    @property
    def workspace(self) -> WorkspaceLayout:
        return WorkspaceLayout(self.workspace_root)

    @property
    def machine_root(self) -> Path:
        return self.workspace.machine_projects_root / self.project_id

    @property
    def knowledge_root(self) -> Path:
        return self.workspace.knowledge_projects_root / self.project_id

    @property
    def project_file(self) -> Path:
        return self.machine_root / "project.yaml"

    @property
    def manifest_file(self) -> Path:
        return self.machine_root / "manifest.jsonl"

    @property
    def sources_file(self) -> Path:
        return self.machine_root / "sources.jsonl"

    @property
    def extracted_dir(self) -> Path:
        return self.machine_root / "extracted"

    @property
    def indexes_dir(self) -> Path:
        return self.machine_root / "indexes"

    @property
    def runs_dir(self) -> Path:
        return self.machine_root / "runs"

    @property
    def overview_file(self) -> Path:
        return self.knowledge_root / "overview.md"

    @property
    def knowledge_directories(self) -> tuple[Path, ...]:
        return tuple(self.knowledge_root / name for name in _KNOWLEDGE_SUBDIRECTORIES)

    @property
    def machine_directories(self) -> tuple[Path, ...]:
        return tuple(self.machine_root / name for name in _MACHINE_SUBDIRECTORIES)

    def ensure_directories(self) -> VersionedDocument:
        """Initialize only the directory contract, not project records/pages."""

        workspace_document = self.workspace.ensure()
        self.machine_root.mkdir(parents=True, exist_ok=True)
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        for directory in self.machine_directories + self.knowledge_directories:
            directory.mkdir(parents=True, exist_ok=True)
        return workspace_document


@dataclass(frozen=True)
class LegacyRawMdLayout:
    """Paths used by the pre-A-03 ``<project-name>-wiki`` layout."""

    wiki_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "wiki_root", Path(self.wiki_root).expanduser().resolve())

    @property
    def raw_md_root(self) -> Path:
        return self.wiki_root / "raw-md"

    @property
    def manifest_file(self) -> Path:
        return self.wiki_root / "state" / "raw-md-manifest.json"

    @property
    def report_file(self) -> Path:
        return self.wiki_root / "reports" / "raw-md-report.md"

    @property
    def exists(self) -> bool:
        return any(
            path.exists()
            for path in (self.raw_md_root, self.manifest_file, self.report_file)
        )


@dataclass(frozen=True)
class LayoutResolution:
    """Resolved project layout plus an optional legacy fallback."""

    mode: Literal["project", "legacy"]
    project: ProjectLayout
    legacy: LegacyRawMdLayout | None = None

    @property
    def manifest_candidates(self) -> tuple[Path, ...]:
        """Return preferred-to-fallback state paths without moving data."""

        modern = self.project.manifest_file
        legacy = self.legacy.manifest_file if self.legacy and self.legacy.exists else None
        if self.mode == "legacy" and legacy is not None:
            return (legacy, modern)
        if legacy is not None:
            return (modern, legacy)
        return (modern,)


def resolve_project_layout(
    workspace_root: Path,
    project_id: str,
    *,
    legacy_wiki_root: Path | None = None,
) -> LayoutResolution:
    """Prefer v1 project storage, falling back to an existing legacy layout.

    Resolution is read-only: it does not create, migrate, rename, or delete
    anything. When both layouts exist, v1 wins and the legacy manifest remains
    available as a fallback candidate for explicit migration/inspection.
    """

    project = ProjectLayout(workspace_root=workspace_root, project_id=project_id)
    legacy = LegacyRawMdLayout(legacy_wiki_root) if legacy_wiki_root else None
    project_exists = project.machine_root.exists() or project.knowledge_root.exists()

    if project_exists:
        return LayoutResolution(mode="project", project=project, legacy=legacy)
    if legacy is not None and legacy.exists:
        return LayoutResolution(mode="legacy", project=project, legacy=legacy)
    return LayoutResolution(mode="project", project=project, legacy=legacy)
