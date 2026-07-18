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
import os
import re
import stat
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
_KNOWLEDGE_DIRECTORY_PARTS = (
    ("papers",),
    ("methods",),
    ("datasets",),
    ("experiments",),
    ("results",),
    ("claims",),
    ("plans",),
    ("plans", "daily"),
    ("decisions",),
    ("sources",),
)


class LayoutError(ValueError):
    """Base error for invalid layout or schema data."""


class InvalidProjectIdError(LayoutError):
    """Raised when a project id could escape or ambiguously address its root."""


class SchemaVersionError(LayoutError):
    """Raised when a versioned document has an invalid schema version."""


class UnsupportedSchemaVersionError(SchemaVersionError):
    """Raised when a document was written by a newer unsupported schema."""


class MachineStatePathError(LayoutError):
    """Raised when a machine-state path escapes or traverses redirection."""


@dataclass(frozen=True)
class VersionedDocument:
    """A JSON object plus the schema interpretation used to read it."""

    data: dict[str, Any]
    schema_version: int
    is_legacy: bool
    path: Path


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LayoutError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise LayoutError(f"non-finite JSON constant {value!r} is not supported")


def parse_json_bytes_strict(payload: bytes, *, label: str) -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate keys and NaN values."""

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LayoutError(f"{label} must be strict UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise LayoutError(f"invalid JSON in {label}: {exc}") from exc


def parse_versioned_json_bytes(
    payload: bytes,
    *,
    path: Path,
    allow_legacy: bool = True,
    max_supported: int = CURRENT_SCHEMA_VERSION,
) -> VersionedDocument:
    """Validate one already-snapshotted versioned JSON document."""

    resolved = Path(path).expanduser().resolve()
    data = parse_json_bytes_strict(payload, label=str(resolved))
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
        payload = resolved.read_bytes()
    except OSError as exc:
        raise LayoutError(f"could not read versioned JSON {resolved}: {exc}") from exc
    return parse_versioned_json_bytes(
        payload,
        path=resolved,
        allow_legacy=allow_legacy,
        max_supported=max_supported,
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

    def ensure(
        self,
        *,
        create_default_knowledge_root: bool = True,
    ) -> VersionedDocument:
        """Create machine roots, an optional default knowledge root, and marker.

        Existing markers are validated but never rewritten implicitly. A
        separate future migration task must perform any schema upgrade.
        """

        self.machine_projects_root.mkdir(parents=True, exist_ok=True)
        if create_default_knowledge_root:
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
    """Paths for one project under the v1 separated storage contract.

    ``custom_knowledge_projects_root`` is the parent directory that holds
    one curated knowledge directory per project.
    """

    workspace_root: Path
    project_id: str
    custom_knowledge_projects_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_root",
            Path(self.workspace_root).expanduser().resolve(),
        )
        object.__setattr__(self, "project_id", validate_project_id(self.project_id))
        if self.custom_knowledge_projects_root is not None:
            object.__setattr__(
                self,
                "custom_knowledge_projects_root",
                Path(self.custom_knowledge_projects_root).expanduser().resolve(),
            )

    @property
    def workspace(self) -> WorkspaceLayout:
        return WorkspaceLayout(self.workspace_root)

    @property
    def machine_root(self) -> Path:
        return self.workspace.machine_projects_root / self.project_id

    @property
    def knowledge_projects_root(self) -> Path:
        return (
            self.custom_knowledge_projects_root
            if self.custom_knowledge_projects_root is not None
            else self.workspace.knowledge_projects_root
        )

    @property
    def knowledge_root(self) -> Path:
        return self.knowledge_projects_root / self.project_id

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
    def evidence_file(self) -> Path:
        return self.machine_root / "evidence.jsonl"

    @property
    def events_file(self) -> Path:
        return self.machine_root / "events.jsonl"

    @property
    def extracted_dir(self) -> Path:
        return self.machine_root / "extracted"

    @property
    def indexes_dir(self) -> Path:
        return self.machine_root / "indexes"

    @property
    def dirty_paths_file(self) -> Path:
        return self.indexes_dir / "dirty-paths.json"

    @property
    def reconciliation_state_file(self) -> Path:
        return self.indexes_dir / "reconciliation-state.json"

    @property
    def reading_priority_file(self) -> Path:
        return self.indexes_dir / "reading-priority.json"

    @property
    def project_map_file(self) -> Path:
        return self.indexes_dir / "project-map.json"

    @property
    def hierarchical_understanding_file(self) -> Path:
        return self.indexes_dir / "hierarchical-understanding.json"

    @property
    def research_linkage_file(self) -> Path:
        """E-05 paper/method/dataset/implementation linkage artifact."""

        return self.indexes_dir / "research-linkage.json"

    @property
    def execution_flow_file(self) -> Path:
        """E-04 deterministic execution-flow machine artifact."""

        return self.indexes_dir / "execution-flow.json"

    @property
    def experiment_chains_file(self) -> Path:
        """E-06 deterministic config-to-run-to-result-to-Claim artifact."""

        return self.indexes_dir / "experiment-chains.json"

    @property
    def goals_file(self) -> Path:
        """I-01 strict project Goal/Milestone machine artifact."""

        return self.indexes_dir / "goals.json"

    @property
    def machine_state_lock_file(self) -> Path:
        """Stable advisory lock shared by per-project machine-state mutations."""

        return self.indexes_dir / "machine-state.lock"

    @property
    def reconciliation_lock_file(self) -> Path:
        """Compatibility alias for the shared machine-state mutation lock."""

        return self.machine_state_lock_file

    @property
    def runs_dir(self) -> Path:
        return self.machine_root / "runs"

    @property
    def overview_file(self) -> Path:
        return self.knowledge_root / "overview.md"

    @property
    def knowledge_directories(self) -> tuple[Path, ...]:
        return tuple(
            self.knowledge_root.joinpath(*parts)
            for parts in _KNOWLEDGE_DIRECTORY_PARTS
        )

    @property
    def machine_directories(self) -> tuple[Path, ...]:
        return tuple(self.machine_root / name for name in _MACHINE_SUBDIRECTORIES)

    def validate_machine_state_path(
        self,
        path: str | Path,
        *,
        leaf_kind: Literal["file", "directory"] = "file",
        allow_missing_leaf: bool = False,
    ) -> Path:
        """Validate one lexical path beneath this project's machine-state root.

        The workspace root is canonicalized when the layout is constructed. From
        that trusted starting point this check walks every existing component
        with ``lstat`` and rejects symbolic links, Windows reparse points,
        non-directory ancestors, and any component whose resolved identity does
        not match its lexical path. Missing parents always fail; callers may
        explicitly allow only the final file/directory to be absent.
        """

        if leaf_kind not in {"file", "directory"}:
            raise ValueError("leaf_kind must be 'file' or 'directory'")
        if not isinstance(allow_missing_leaf, bool):
            raise TypeError("allow_missing_leaf must be a bool")
        try:
            target = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
            workspace_root = Path(
                os.path.abspath(os.fspath(self.workspace_root))
            )
            machine_root = Path(os.path.abspath(os.fspath(self.machine_root)))
            target.relative_to(machine_root)
            relative_to_workspace = target.relative_to(workspace_root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise MachineStatePathError(
                f"machine-state path is outside the registered project root: {path}"
            ) from exc

        def path_key(candidate: Path) -> str:
            return os.path.normcase(os.path.normpath(os.fspath(candidate)))

        current = workspace_root
        components = (workspace_root,)
        if relative_to_workspace.parts:
            built: list[Path] = []
            for part in relative_to_workspace.parts:
                current = current / part
                built.append(current)
            components += tuple(built)

        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        for current in components:
            is_leaf = path_key(current) == path_key(target)
            try:
                metadata = os.lstat(current)
            except FileNotFoundError as exc:
                if is_leaf and allow_missing_leaf:
                    return target
                label = "leaf" if is_leaf else "ancestor"
                raise MachineStatePathError(
                    f"machine-state {label} is unavailable: {current}"
                ) from exc
            except OSError as exc:
                raise MachineStatePathError(
                    f"could not inspect machine-state path {current}: {exc}"
                ) from exc

            file_attributes = getattr(metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(metadata.st_mode) or (
                reparse_flag and file_attributes & reparse_flag
            ):
                raise MachineStatePathError(
                    f"machine-state path must not traverse a symbolic link or "
                    f"reparse point: {current}"
                )
            if is_leaf:
                valid_leaf = (
                    stat.S_ISREG(metadata.st_mode)
                    if leaf_kind == "file"
                    else stat.S_ISDIR(metadata.st_mode)
                )
                if not valid_leaf:
                    raise MachineStatePathError(
                        f"machine-state {leaf_kind} has the wrong type: {current}"
                    )
            elif not stat.S_ISDIR(metadata.st_mode):
                raise MachineStatePathError(
                    f"machine-state ancestor is not a directory: {current}"
                )

            try:
                resolved = current.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise MachineStatePathError(
                    f"could not resolve machine-state path {current}: {exc}"
                ) from exc
            if path_key(resolved) != path_key(current):
                raise MachineStatePathError(
                    f"machine-state path resolves through redirection: {current}"
                )

        return target

    def ensure_directories(self) -> VersionedDocument:
        """Initialize only the directory contract, not project records/pages."""

        workspace_document = self.workspace.ensure(
            create_default_knowledge_root=(
                self.custom_knowledge_projects_root is None
            )
        )
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
    knowledge_projects_root: Path | None = None,
) -> LayoutResolution:
    """Prefer v1 project storage, falling back to an existing legacy layout.

    Resolution is read-only: it does not create, migrate, rename, or delete
    anything. ``knowledge_projects_root`` optionally overrides the default
    curated-knowledge parent. When both layouts exist, v1 wins and the legacy
    manifest remains available for explicit migration or inspection.
    """

    project = ProjectLayout(
        workspace_root=workspace_root,
        project_id=project_id,
        custom_knowledge_projects_root=knowledge_projects_root,
    )
    legacy = LegacyRawMdLayout(legacy_wiki_root) if legacy_wiki_root else None
    project_exists = project.machine_root.exists() or project.knowledge_root.exists()

    if project_exists:
        return LayoutResolution(mode="project", project=project, legacy=legacy)
    if legacy is not None and legacy.exists:
        return LayoutResolution(mode="legacy", project=project, legacy=legacy)
    return LayoutResolution(mode="project", project=project, legacy=legacy)
