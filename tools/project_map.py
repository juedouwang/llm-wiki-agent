#!/usr/bin/env python3
"""Deterministic project-map artifacts derived only from a current B-06 Manifest.

E-02 establishes a bounded, local-only project outline before semantic reading.
The implementation never traverses or opens the registered source project: directory
shape, distributions, and candidates are derived exclusively from already validated
``project-inventory-v4`` ordinary-file rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping
import unicodedata

if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
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
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
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


PROJECT_MAP_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
PROJECT_MAP_KIND = "llmwiki-project-map"
PROJECT_MAP_VERSION = "project-map-v1"
PROJECT_MAP_FILENAME = "project-map.json"
PROJECT_MAP_MAX_DIRECTORIES = 4096
PROJECT_MAP_MAX_CATEGORY_CANDIDATES = 64
PROJECT_MAP_MAX_KEY_CANDIDATES = 128

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "map_version",
        "project_id",
        "manifest",
        "derivation",
        "limits",
        "directories",
        "distributions",
        "candidates",
        "omissions",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "scan_generation",
        "ordinary_file_count",
        "ordinary_byte_count",
        "sha256",
    }
)
_DERIVATION_FIELDS = frozenset(
    {"mode", "source_content_read", "llm_used"}
)
_LIMIT_FIELDS = frozenset(
    {
        "max_directories",
        "max_category_candidates",
        "max_key_candidates",
    }
)
_DIRECTORY_FIELDS = frozenset(
    {
        "path",
        "depth",
        "direct_file_count",
        "recursive_file_count",
        "direct_byte_count",
        "recursive_byte_count",
        "child_directory_count",
    }
)
_DISTRIBUTION_FIELDS = frozenset({"value", "file_count", "byte_count"})
_CANDIDATE_FIELDS = frozenset(
    {
        "path",
        "score",
        "reason_codes",
        "format",
        "language",
        "research_role",
        "size_bytes",
        "rank",
    }
)
_DISTRIBUTION_KEYS = ("formats", "languages", "research_roles")
_CANDIDATE_KEYS = (
    "entrypoints",
    "dependencies",
    "configurations",
    "run_scripts",
    "key_files",
)
_OMISSION_KEYS = (
    "directories",
    "entrypoints",
    "dependencies",
    "configurations",
    "run_scripts",
    "key_files",
)

_ENTRYPOINT_NAMES = frozenset(
    {
        "main.py",
        "train.py",
        "evaluate.py",
        "evaluation.py",
        "eval.py",
        "infer.py",
        "inference.py",
        "predict.py",
        "run.py",
        "app.py",
        "server.py",
        "cli.py",
        "main.r",
        "main.jl",
        "main.m",
        "index.js",
        "index.ts",
        "index.tsx",
        "program.cs",
        "main.go",
        "main.rs",
    }
)
_DEPENDENCY_NAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.in",
        "pyproject.toml",
        "poetry.lock",
        "pdm.lock",
        "pipfile",
        "pipfile.lock",
        "setup.py",
        "setup.cfg",
        "environment.yml",
        "environment.yaml",
        "conda-lock.yml",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gradle.lockfile",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "project.toml",
        "manifest.toml",
        "renv.lock",
        "description",
        "vcpkg.json",
        "conanfile.txt",
        "conanfile.py",
    }
)
_RUN_SCRIPT_NAMES = frozenset(
    {
        "makefile",
        "gnumakefile",
        "justfile",
        "taskfile.yml",
        "taskfile.yaml",
        "tox.ini",
        "noxfile.py",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    }
)
_CONFIG_FORMATS = frozenset({"yaml", "toml", "ini", "json", "xml"})
_SCRIPT_FORMATS = frozenset(
    {"shell", "powershell", "batch", "python", "r", "julia", "matlab"}
)
_KEY_ROLE_SCORE = {
    "project_documentation": 1200,
    "paper": 1150,
    "configuration": 1100,
    "experiment": 1050,
    "result": 1025,
    "notebook": 1000,
    "source_code": 900,
    "automation": 850,
    "dependency_manifest": 825,
    "bibliography": 800,
    "documentation": 750,
    "test_code": 600,
    "project_metadata": 550,
    "dataset": 450,
    "figure": 400,
    "run_log": 300,
    "model_artifact": 150,
    "unknown": 100,
}
_KEY_SIGNALS = (
    "readme",
    "overview",
    "architecture",
    "design",
    "method",
    "paper",
    "train",
    "evaluate",
    "experiment",
    "result",
    "benchmark",
    "ablation",
    "dataset",
    "config",
)


class ProjectMapError(LayoutError):
    """Raised when a deterministic project-map contract cannot be satisfied."""


@dataclass(frozen=True)
class ProjectMapResult:
    """One generated machine project-map and its exact Manifest binding."""

    project_id: str
    manifest_file: Path
    project_map_file: Path
    project_map: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "project_map_file": str(self.project_map_file),
            "project_map": self.project_map,
        }


@dataclass(frozen=True)
class _FileInfo:
    path: str
    size_bytes: int
    format: str
    language: str
    research_role: str

    @property
    def pure_path(self) -> PurePosixPath:
        return PurePosixPath(self.path)

    @property
    def name(self) -> str:
        return self.pure_path.name

    @property
    def lower_name(self) -> str:
        return self.name.casefold()

    @property
    def depth(self) -> int:
        return len(self.pure_path.parts)


@dataclass(frozen=True)
class _CandidateDraft:
    info: _FileInfo
    score: int
    reason_codes: tuple[str, ...]


@dataclass
class _DirectoryAggregate:
    direct_file_count: int = 0
    recursive_file_count: int = 0
    direct_byte_count: int = 0
    recursive_byte_count: int = 0
    children: set[str] | None = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = set()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _exact_mapping(value: object, fields: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ProjectMapError(f"{label} must contain exactly {sorted(fields)!r}")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProjectMapError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, *, label: str) -> str:
    if type(value) is not str or not value or unicodedata.normalize("NFC", value) != value:
        raise ProjectMapError(f"{label} must be non-empty NFC text")
    return value


def _stable_code(value: object, *, label: str) -> str:
    result = _text(value, label=label)
    if _STABLE_CODE_RE.fullmatch(result) is None:
        raise ProjectMapError(f"{label} must be a stable code")
    return result


def _relative_file_path(value: object, *, label: str) -> str:
    result = _text(value, label=label)
    path = PurePosixPath(result)
    if (
        "\\" in result
        or path.is_absolute()
        or result != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProjectMapError(f"{label} must be a normalized project-relative file path")
    return result


def _directory_path(value: object, *, label: str) -> str:
    if value == ".":
        return "."
    return _relative_file_path(value, label=label)


def _classification_value(value: object, *, label: str) -> str:
    result = _text(value, label=label)
    if any(character.isspace() for character in result):
        raise ProjectMapError(f"{label} cannot contain whitespace")
    return result


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _directory_ancestors(path: PurePosixPath) -> tuple[str, ...]:
    parents = ["."]
    for index in range(1, len(path.parts)):
        parents.append(PurePosixPath(*path.parts[:index]).as_posix())
    return tuple(parents)


def _build_file_infos(manifest: ProjectManifest) -> tuple[_FileInfo, ...]:
    infos: list[_FileInfo] = []
    for record in manifest.file_records:
        path = _relative_file_path(record.get("path"), label="Manifest file path")
        size_bytes = _integer(
            record.get("size_bytes"), label=f"Manifest size_bytes for {path}"
        )
        classification = record.get("classification")
        if type(classification) is not dict:
            raise ProjectMapError(f"Manifest classification is missing for {path}")
        infos.append(
            _FileInfo(
                path=path,
                size_bytes=size_bytes,
                format=_classification_value(
                    classification.get("format"), label=f"format for {path}"
                ),
                language=_classification_value(
                    classification.get("language"), label=f"language for {path}"
                ),
                research_role=_classification_value(
                    classification.get("research_role"),
                    label=f"research_role for {path}",
                ),
            )
        )
    return tuple(sorted(infos, key=lambda item: (item.path.casefold(), item.path)))

def _manifest_summary(manifest: ProjectManifest, manifest_sha256: str) -> dict[str, Any]:
    infos = _build_file_infos(manifest)
    expected_count = manifest.summary.get("record_counts", {}).get("file")
    if expected_count != len(infos):
        raise ProjectMapError("Manifest ordinary-file summary does not match file rows")
    expected_bytes = sum(info.size_bytes for info in infos)
    summary_bytes = manifest.summary.get("total_bytes")
    if summary_bytes is None:
        summary_bytes = manifest.summary.get("byte_count")
    if summary_bytes is not None and summary_bytes != expected_bytes:
        raise ProjectMapError("Manifest ordinary-file byte total does not match file rows")
    if not _is_sha256(manifest_sha256):
        raise ProjectMapError("Manifest SHA-256 is invalid")
    return {
        "manifest_version": manifest.manifest_version,
        "scan_generation": manifest.scan_generation,
        "ordinary_file_count": len(infos),
        "ordinary_byte_count": expected_bytes,
        "sha256": manifest_sha256,
    }


def _build_directories(infos: tuple[_FileInfo, ...]) -> tuple[list[dict[str, Any]], int]:
    aggregates: dict[str, _DirectoryAggregate] = {".": _DirectoryAggregate()}
    for info in infos:
        path = info.pure_path
        ancestors = _directory_ancestors(path)
        for directory in ancestors:
            aggregate = aggregates.setdefault(directory, _DirectoryAggregate())
            aggregate.recursive_file_count += 1
            aggregate.recursive_byte_count += info.size_bytes
        direct_parent = "." if len(path.parts) == 1 else PurePosixPath(*path.parts[:-1]).as_posix()
        direct = aggregates.setdefault(direct_parent, _DirectoryAggregate())
        direct.direct_file_count += 1
        direct.direct_byte_count += info.size_bytes
        for parent, child in zip(ancestors, ancestors[1:], strict=False):
            aggregates.setdefault(parent, _DirectoryAggregate()).children.add(child)
        # The immediate child directory is not included in _directory_ancestors
        # for a file at its root, so add it explicitly for nested files.
        if len(path.parts) > 1:
            immediate = PurePosixPath(*path.parts[:-1]).as_posix()
            parent = "." if len(path.parts) == 2 else PurePosixPath(*path.parts[:-2]).as_posix()
            aggregates.setdefault(parent, _DirectoryAggregate()).children.add(immediate)

    ordered_paths = sorted(
        aggregates,
        key=lambda value: (value.casefold(), value),
    )
    omitted = max(0, len(ordered_paths) - PROJECT_MAP_MAX_DIRECTORIES)
    # Keep the root and then the deterministic lexical prefix.  The aggregate
    # counts remain a truthful summary even when the rendered tree is bounded.
    selected = ordered_paths[:PROJECT_MAP_MAX_DIRECTORIES]
    if "." in aggregates and "." not in selected:
        selected[-1] = "."
        selected = sorted(set(selected), key=lambda value: (value.casefold(), value))
    rendered: list[dict[str, Any]] = []
    for directory in selected:
        aggregate = aggregates[directory]
        rendered.append(
            {
                "path": directory,
                "depth": 0 if directory == "." else len(PurePosixPath(directory).parts),
                "direct_file_count": aggregate.direct_file_count,
                "recursive_file_count": aggregate.recursive_file_count,
                "direct_byte_count": aggregate.direct_byte_count,
                "recursive_byte_count": aggregate.recursive_byte_count,
                "child_directory_count": len(aggregate.children),
            }
        )
    return rendered, omitted


def _distribution(infos: Iterable[_FileInfo], attribute: str) -> list[dict[str, Any]]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for info in infos:
        value = getattr(info, attribute)
        counts[value][0] += 1
        counts[value][1] += info.size_bytes
    return [
        {"value": value, "file_count": values[0], "byte_count": values[1]}
        for value, values in sorted(
            counts.items(),
            key=lambda item: (-item[1][0], -item[1][1], item[0].casefold(), item[0]),
        )
    ]


def _path_tokens(info: _FileInfo) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.split(r"[^a-z0-9]+", info.path.casefold())
        if token
    )


def _candidate(
    info: _FileInfo,
    *,
    score: int,
    reasons: Iterable[str],
) -> _CandidateDraft:
    return _CandidateDraft(
        info=info,
        score=max(0, score),
        reason_codes=tuple(sorted(set(reasons))),
    )


def _entrypoint_candidate(info: _FileInfo) -> _CandidateDraft | None:
    name = info.lower_name
    tokens = _path_tokens(info)
    reasons: list[str] = []
    score = 0
    recognized_name = name in _ENTRYPOINT_NAMES
    name_signal = any(
        token in {"main", "train", "run", "evaluate", "eval", "infer", "predict"}
        for token in tokens
    )
    executable_role = info.research_role in {"source_code", "notebook", "automation"}
    if not recognized_name and not (name_signal and executable_role):
        return None
    if recognized_name:
        score += 1000
        reasons.append("recognized-entrypoint-name")
    if executable_role:
        score += 120
        reasons.append("executable-research-role")
    if info.depth == 1:
        score += 100
        reasons.append("project-root-file")
    if any(token in {"bin", "src", "scripts", "script", "tools", "cli"} for token in tokens):
        score += 50
        reasons.append("entrypoint-directory")
    if name_signal:
        score += 60
        reasons.append("entrypoint-name-signal")
    return _candidate(info, score=score, reasons=reasons)


def _dependency_candidate(info: _FileInfo) -> _CandidateDraft | None:
    name = info.lower_name
    tokens = _path_tokens(info)
    reasons: list[str] = []
    score = 0
    if name in _DEPENDENCY_NAMES:
        score += 1100
        reasons.append("recognized-dependency-manifest")
    if info.research_role == "dependency_manifest":
        score += 500
        reasons.append("dependency-manifest-role")
    if any(token in {"requirements", "dependencies", "dependency", "environment", "vendor"} for token in tokens):
        score += 80
        reasons.append("dependency-path-signal")
    if score == 0:
        return None
    return _candidate(info, score=score, reasons=reasons)


def _configuration_candidate(info: _FileInfo) -> _CandidateDraft | None:
    tokens = _path_tokens(info)
    reasons: list[str] = []
    score = 0
    role_signal = info.research_role == "configuration"
    path_signal = any(
        token in {"config", "configs", "conf", "setting", "settings", "parameter", "params"}
        for token in tokens
    )
    name_signal = info.lower_name.startswith(("config", "default", "base", "params", "settings"))
    if not (role_signal or path_signal or name_signal):
        return None
    if role_signal:
        score += 900
        reasons.append("configuration-role")
    if info.format in _CONFIG_FORMATS:
        score += 150
        reasons.append("configuration-format")
    if path_signal:
        score += 180
        reasons.append("configuration-path-signal")
    if name_signal:
        score += 100
        reasons.append("configuration-name-signal")
    return _candidate(info, score=score, reasons=reasons)


def _run_script_candidate(info: _FileInfo) -> _CandidateDraft | None:
    name = info.lower_name
    tokens = _path_tokens(info)
    reasons: list[str] = []
    score = 0
    if name in _RUN_SCRIPT_NAMES:
        score += 1100
        reasons.append("recognized-run-script-name")
    if info.research_role == "automation":
        score += 700
        reasons.append("automation-role")
    elif info.format in _SCRIPT_FORMATS and any(
        token in {"run", "train", "evaluate", "eval", "launch", "start", "execute"}
        for token in tokens
    ):
        score += 300
        reasons.append("run-name-signal")
    if any(token in {"scripts", "script", "bin", "jobs", "workflow", "workflows"} for token in tokens):
        score += 100
        reasons.append("automation-directory")
    if score == 0:
        return None
    return _candidate(info, score=score, reasons=reasons)


def _key_candidate(info: _FileInfo) -> _CandidateDraft:
    tokens = _path_tokens(info)
    score = _KEY_ROLE_SCORE.get(info.research_role, 100)
    reasons = [f"research-role-{info.research_role}"]
    if info.depth == 1:
        score += 180
        reasons.append("project-root-file")
    elif info.depth <= 2:
        score += 80
        reasons.append("shallow-project-path")
    signal_matches = sorted({signal for signal in _KEY_SIGNALS if signal in tokens})
    if signal_matches:
        score += 140 + 20 * min(len(signal_matches), 4)
        reasons.append("path-name-signal")
    if info.lower_name in {"readme.md", "license", "license.md", "citation.cff"}:
        score += 180
        reasons.append("project-metadata-name")
    return _candidate(info, score=score, reasons=reasons)


def _materialize_candidate(draft: _CandidateDraft, *, rank: int) -> dict[str, Any]:
    info = draft.info
    return {
        "path": info.path,
        "score": draft.score,
        "reason_codes": list(draft.reason_codes),
        "format": info.format,
        "language": info.language,
        "research_role": info.research_role,
        "size_bytes": info.size_bytes,
        "rank": rank,
    }


def _candidate_list(
    infos: tuple[_FileInfo, ...],
    builder: Callable[[_FileInfo], _CandidateDraft | None],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    drafts = [draft for info in infos if (draft := builder(info)) is not None]
    ordered = sorted(
        drafts,
        key=lambda draft: (
            -draft.score,
            draft.info.path.casefold(),
            draft.info.path,
        ),
    )
    omitted = max(0, len(ordered) - limit)
    return [
        _materialize_candidate(draft, rank=index)
        for index, draft in enumerate(ordered[:limit], start=1)
    ], omitted


def _validate_manifest_binding(manifest: ProjectManifest, manifest_sha256: str) -> None:
    if manifest.manifest_version != PROJECT_MANIFEST_VERSION:
        raise ProjectMapError(
            f"project map requires {PROJECT_MANIFEST_VERSION}, found {manifest.manifest_version!r}"
        )
    if type(manifest.scan_generation) is not int or manifest.scan_generation < 1:
        raise ProjectMapError("Manifest scan_generation must be a positive integer")
    _manifest_summary(manifest, manifest_sha256)


def build_project_map(
    manifest: ProjectManifest,
    *,
    manifest_sha256: str | None = None,
    manifest_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Build a deterministic project map from validated Manifest rows only.

    ``manifest_bytes`` is accepted for callers that already hold an exact snapshot;
    the bytes are hashed but never interpreted as source-project content.  A caller
    must provide one of ``manifest_sha256`` or ``manifest_bytes``.
    """

    if manifest_bytes is not None:
        if type(manifest_bytes) is not bytes:
            raise ProjectMapError("manifest_bytes must be bytes")
        computed_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha256 is not None and manifest_sha256 != computed_hash:
            raise ProjectMapError("Manifest hash arguments disagree")
        manifest_sha256 = computed_hash
    if manifest_sha256 is None:
        raise ProjectMapError("manifest_sha256 or manifest_bytes is required")
    _validate_manifest_binding(manifest, manifest_sha256)
    infos = _build_file_infos(manifest)
    directories, omitted_directories = _build_directories(infos)
    entrypoints, omitted_entrypoints = _candidate_list(
        infos, _entrypoint_candidate, limit=PROJECT_MAP_MAX_CATEGORY_CANDIDATES
    )
    dependencies, omitted_dependencies = _candidate_list(
        infos, _dependency_candidate, limit=PROJECT_MAP_MAX_CATEGORY_CANDIDATES
    )
    configurations, omitted_configurations = _candidate_list(
        infos, _configuration_candidate, limit=PROJECT_MAP_MAX_CATEGORY_CANDIDATES
    )
    run_scripts, omitted_run_scripts = _candidate_list(
        infos, _run_script_candidate, limit=PROJECT_MAP_MAX_CATEGORY_CANDIDATES
    )
    key_files, omitted_key_files = _candidate_list(
        infos, _key_candidate, limit=PROJECT_MAP_MAX_KEY_CANDIDATES
    )
    return {
        "schema_version": PROJECT_MAP_SCHEMA_VERSION,
        "kind": PROJECT_MAP_KIND,
        "map_version": PROJECT_MAP_VERSION,
        "project_id": manifest.project_id,
        "manifest": _manifest_summary(manifest, manifest_sha256),
        "derivation": {
            "mode": "manifest-metadata-only",
            "source_content_read": False,
            "llm_used": False,
        },
        "limits": {
            "max_directories": PROJECT_MAP_MAX_DIRECTORIES,
            "max_category_candidates": PROJECT_MAP_MAX_CATEGORY_CANDIDATES,
            "max_key_candidates": PROJECT_MAP_MAX_KEY_CANDIDATES,
        },
        "directories": directories,
        "distributions": {
            "formats": _distribution(infos, "format"),
            "languages": _distribution(infos, "language"),
            "research_roles": _distribution(infos, "research_role"),
        },
        "candidates": {
            "entrypoints": entrypoints,
            "dependencies": dependencies,
            "configurations": configurations,
            "run_scripts": run_scripts,
            "key_files": key_files,
        },
        "omissions": {
            "directories": omitted_directories,
            "entrypoints": omitted_entrypoints,
            "dependencies": omitted_dependencies,
            "configurations": omitted_configurations,
            "run_scripts": omitted_run_scripts,
            "key_files": omitted_key_files,
        },
    }

def _validate_candidate(value: object, *, label: str) -> dict[str, Any]:
    item = _exact_mapping(value, _CANDIDATE_FIELDS, label=label)
    path = _relative_file_path(item["path"], label=f"{label}.path")
    score = _integer(item["score"], label=f"{label}.score")
    rank = _integer(item["rank"], label=f"{label}.rank", minimum=1)
    reasons = item["reason_codes"]
    if (
        type(reasons) is not list
        or any(type(reason) is not str or _STABLE_CODE_RE.fullmatch(reason) is None for reason in reasons)
        or reasons != sorted(set(reasons))
    ):
        raise ProjectMapError(f"{label}.reason_codes must be a sorted duplicate-free code list")
    return {
        "path": path,
        "score": score,
        "reason_codes": list(reasons),
        "format": _classification_value(item["format"], label=f"{label}.format"),
        "language": _classification_value(item["language"], label=f"{label}.language"),
        "research_role": _classification_value(
            item["research_role"], label=f"{label}.research_role"
        ),
        "size_bytes": _integer(item["size_bytes"], label=f"{label}.size_bytes"),
        "rank": rank,
    }


def _validate_project_map_payload(
    value: object,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    payload = _exact_mapping(value, _TOP_FIELDS, label="project map")
    if payload["schema_version"] != PROJECT_MAP_SCHEMA_VERSION:
        raise ProjectMapError("project map schema_version is unsupported")
    if payload["kind"] != PROJECT_MAP_KIND or payload["map_version"] != PROJECT_MAP_VERSION:
        raise ProjectMapError("project map kind or version is unsupported")
    actual_project_id = _text(payload["project_id"], label="project map project_id")
    try:
        validate_project_id(actual_project_id)
    except LayoutError as exc:
        raise ProjectMapError("project map project_id is invalid") from exc
    if project_id is not None and actual_project_id != project_id:
        raise ProjectMapError("project map project_id does not match the requested project")

    manifest = _exact_mapping(payload["manifest"], _MANIFEST_FIELDS, label="project map manifest")
    if manifest["manifest_version"] != PROJECT_MANIFEST_VERSION:
        raise ProjectMapError("project map Manifest version is unsupported")
    _integer(manifest["scan_generation"], label="project map scan_generation", minimum=1)
    _integer(manifest["ordinary_file_count"], label="project map ordinary_file_count")
    _integer(manifest["ordinary_byte_count"], label="project map ordinary_byte_count")
    if not _is_sha256(manifest["sha256"]):
        raise ProjectMapError("project map Manifest SHA-256 is invalid")

    derivation = _exact_mapping(payload["derivation"], _DERIVATION_FIELDS, label="project map derivation")
    if derivation != {
        "mode": "manifest-metadata-only",
        "source_content_read": False,
        "llm_used": False,
    }:
        raise ProjectMapError("project map derivation boundary is invalid")
    limits = _exact_mapping(payload["limits"], _LIMIT_FIELDS, label="project map limits")
    if limits != {
        "max_directories": PROJECT_MAP_MAX_DIRECTORIES,
        "max_category_candidates": PROJECT_MAP_MAX_CATEGORY_CANDIDATES,
        "max_key_candidates": PROJECT_MAP_MAX_KEY_CANDIDATES,
    }:
        raise ProjectMapError("project map limits do not match the current contract")

    directories = payload["directories"]
    if type(directories) is not list or len(directories) > PROJECT_MAP_MAX_DIRECTORIES:
        raise ProjectMapError("project map directories exceed the bound")
    previous_path: str | None = None
    seen_directories: set[str] = set()
    for index, raw_directory in enumerate(directories):
        directory = _exact_mapping(
            raw_directory, _DIRECTORY_FIELDS, label=f"project map directory {index}"
        )
        directory_path = _directory_path(directory["path"], label=f"directory {index}.path")
        if directory_path in seen_directories:
            raise ProjectMapError("project map directories contain duplicates")
        if previous_path is not None and (directory_path.casefold(), directory_path) < (
            previous_path.casefold(),
            previous_path,
        ):
            raise ProjectMapError("project map directories are not deterministically ordered")
        previous_path = directory_path
        seen_directories.add(directory_path)
        expected_depth = 0 if directory_path == "." else len(PurePosixPath(directory_path).parts)
        if directory["depth"] != expected_depth:
            raise ProjectMapError(f"directory {directory_path} has an invalid depth")
        for field in (
            "direct_file_count",
            "recursive_file_count",
            "direct_byte_count",
            "recursive_byte_count",
            "child_directory_count",
        ):
            _integer(directory[field], label=f"directory {directory_path}.{field}")
    if not directories or directories[0]["path"] != ".":
        raise ProjectMapError("project map must include the root directory first")
    root = directories[0]
    if root["recursive_file_count"] != manifest["ordinary_file_count"]:
        raise ProjectMapError("project map root file count is not Manifest-grounded")
    if root["recursive_byte_count"] != manifest["ordinary_byte_count"]:
        raise ProjectMapError("project map root byte count is not Manifest-grounded")

    distributions = _exact_mapping(
        payload["distributions"], frozenset(_DISTRIBUTION_KEYS), label="project map distributions"
    )
    for distribution_name in _DISTRIBUTION_KEYS:
        values = distributions[distribution_name]
        if type(values) is not list:
            raise ProjectMapError(f"distribution {distribution_name} must be a list")
        previous_key: tuple[int, int, str, str] | None = None
        seen_values: set[str] = set()
        for index, raw_value in enumerate(values):
            item = _exact_mapping(
                raw_value,
                _DISTRIBUTION_FIELDS,
                label=f"distribution {distribution_name}[{index}]",
            )
            item_value = _classification_value(
                item["value"], label=f"distribution {distribution_name}[{index}].value"
            )
            if item_value in seen_values:
                raise ProjectMapError("project map distribution contains duplicate values")
            seen_values.add(item_value)
            file_count = _integer(item["file_count"], label="distribution file_count")
            byte_count = _integer(item["byte_count"], label="distribution byte_count")
            order_key = (-file_count, -byte_count, item_value.casefold(), item_value)
            if previous_key is not None and order_key < previous_key:
                raise ProjectMapError("project map distribution order is not deterministic")
            previous_key = order_key
        if sum(item["file_count"] for item in values) != manifest["ordinary_file_count"]:
            raise ProjectMapError(f"distribution {distribution_name} file total is invalid")
        if sum(item["byte_count"] for item in values) != manifest["ordinary_byte_count"]:
            raise ProjectMapError(f"distribution {distribution_name} byte total is invalid")

    candidates = _exact_mapping(
        payload["candidates"], frozenset(_CANDIDATE_KEYS), label="project map candidates"
    )
    for category in _CANDIDATE_KEYS:
        values = candidates[category]
        if type(values) is not list:
            raise ProjectMapError(f"candidate category {category} must be a list")
        category_limit = (
            PROJECT_MAP_MAX_KEY_CANDIDATES
            if category == "key_files"
            else PROJECT_MAP_MAX_CATEGORY_CANDIDATES
        )
        if len(values) > category_limit:
            raise ProjectMapError(f"candidate category {category} exceeds its bound")
        previous_key: tuple[int, str, str] | None = None
        seen_paths: set[str] = set()
        for index, raw_candidate in enumerate(values):
            candidate = _validate_candidate(raw_candidate, label=f"{category}[{index}]")
            if candidate["path"] in seen_paths:
                raise ProjectMapError(f"candidate category {category} contains duplicates")
            seen_paths.add(candidate["path"])
            order_key = (
                -candidate["score"],
                candidate["path"].casefold(),
                candidate["path"],
            )
            if previous_key is not None and order_key < previous_key:
                raise ProjectMapError(f"candidate category {category} is not ordered")
            previous_key = order_key
            if candidate["rank"] != index + 1:
                raise ProjectMapError(f"candidate category {category} has invalid ranks")

    omissions = _exact_mapping(
        payload["omissions"], frozenset(_OMISSION_KEYS), label="project map omissions"
    )
    for key in _OMISSION_KEYS:
        _integer(omissions[key], label=f"project map omissions.{key}")
    return payload


def load_project_map(
    project_map_file: str | Path,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Strictly load a Schema v1 map; legacy, future, and noncanonical bytes fail."""

    unresolved = Path(project_map_file).expanduser()
    if unresolved.is_symlink():
        raise ProjectMapError(f"project map must not be a symbolic link: {unresolved}")
    path = unresolved.resolve()
    if path.is_symlink() or not path.is_file():
        raise ProjectMapError(f"project map is not a regular file: {path}")
    try:
        raw = path.read_bytes()
        document = parse_versioned_json_bytes(
            raw,
            path=path,
            allow_legacy=True,
            max_supported=PROJECT_MAP_SCHEMA_VERSION,
        )
    except (OSError, UnicodeError, LayoutError) as exc:
        raise ProjectMapError(f"could not read project map {path}: {exc}") from exc
    if document.is_legacy:
        raise ProjectMapError(f"project map is legacy v0 and cannot be used: {path}")
    payload = _validate_project_map_payload(document.data, project_id=project_id)
    if raw != _canonical_json_bytes(payload):
        raise ProjectMapError("project map is not in canonical JSON form")
    return payload


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
        raise ProjectMapError(f"unsafe project machine-state path {path}: {exc}") from exc


def _acquire_project_lock(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float,
) -> tuple[ProjectRegistrationResult, AdvisoryFileLock]:
    registration = load_registered_project(workspace_root, project_id)
    layout = registration.layout
    _validate_machine_state_path(layout, layout.project_file, allow_missing_leaf=False)
    _validate_machine_state_path(layout, layout.machine_state_lock_file, allow_missing_leaf=True)
    lock = AdvisoryFileLock(layout.machine_state_lock_file, timeout_seconds=lock_timeout_seconds)
    try:
        lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise ProjectMapError("timed out waiting for the project machine-state lock") from exc
    except AdvisoryLockError as exc:
        raise ProjectMapError("could not acquire the project machine-state lock") from exc
    try:
        current = load_registered_project(workspace_root, project_id)
        if current.layout.machine_root != layout.machine_root:
            raise ProjectMapError("registered project machine-state root changed while locking")
        _validate_machine_state_path(current.layout, current.layout.project_file, allow_missing_leaf=False)
        _validate_machine_state_path(
            current.layout, current.layout.machine_state_lock_file, allow_missing_leaf=False
        )
        return current, lock
    except BaseException:
        lock.release()
        raise


def _load_manifest_snapshot(
    registration: ProjectRegistrationResult,
) -> tuple[ProjectManifest, bytes, str]:
    layout = registration.layout
    manifest_file = layout.manifest_file
    _validate_machine_state_path(layout, manifest_file, allow_missing_leaf=False)
    try:
        snapshot = manifest_file.read_bytes()
    except OSError as exc:
        raise ProjectMapError(f"could not read current Manifest {manifest_file}: {exc}") from exc
    manifest_sha256 = hashlib.sha256(snapshot).hexdigest()
    try:
        manifest = load_project_manifest(
            manifest_file,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )
    except Exception as exc:
        if isinstance(exc, ProjectMapError):
            raise
        raise ProjectMapError(f"could not load current Manifest {manifest_file}: {exc}") from exc
    try:
        if manifest_file.read_bytes() != snapshot:
            raise ProjectMapError("Manifest changed while project map was generated; retry inventory")
    except OSError as exc:
        raise ProjectMapError(f"could not revalidate current Manifest {manifest_file}: {exc}") from exc
    _validate_manifest_binding(manifest, manifest_sha256)
    return manifest, snapshot, manifest_sha256

def _manifest_identity(project_map: Mapping[str, Any]) -> tuple[object, ...]:
    manifest = project_map["manifest"]
    return (
        manifest["manifest_version"],
        manifest["scan_generation"],
        manifest["ordinary_file_count"],
        manifest["ordinary_byte_count"],
        manifest["sha256"],
    )


def _current_expected_map(
    registration: ProjectRegistrationResult,
) -> tuple[ProjectManifest, bytes, dict[str, Any]]:
    manifest, snapshot, manifest_sha256 = _load_manifest_snapshot(registration)
    expected = build_project_map(manifest, manifest_sha256=manifest_sha256)
    _validate_project_map_payload(expected, project_id=registration.project_id)
    return manifest, snapshot, expected


def _load_current_project_map_locked(
    registration: ProjectRegistrationResult,
) -> dict[str, Any]:
    manifest, snapshot, expected = _current_expected_map(registration)
    map_file = registration.layout.project_map_file
    _validate_machine_state_path(registration.layout, map_file, allow_missing_leaf=False)
    actual = load_project_map(map_file, project_id=registration.project_id)
    if actual != expected:
        if _manifest_identity(actual) != _manifest_identity(expected):
            raise ProjectMapError(
                "project map is stale for the current Manifest; regenerate it before use"
            )
        raise ProjectMapError("project map payload does not match deterministic current truth")
    try:
        if manifest.manifest_file.read_bytes() != snapshot:
            raise ProjectMapError("Manifest changed while current project map was loaded")
    except OSError as exc:
        raise ProjectMapError(f"could not revalidate current Manifest: {exc}") from exc
    _validate_machine_state_path(registration.layout, map_file, allow_missing_leaf=False)
    return actual


def load_current_project_map(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Load a map only when its entire payload matches the current Manifest."""

    registration, lock = _acquire_project_lock(
        workspace_root,
        project_id,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    try:
        return _load_current_project_map_locked(registration)
    finally:
        lock.release()


def _write_atomic_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    layout: ProjectLayout,
    before_replace: Callable[[], None],
) -> None:
    _validate_machine_state_path(layout, path, allow_missing_leaf=True)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ProjectMapError(
            f"registered machine-state index directory is unavailable: {path.parent}"
        )
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_temporary)
        _validate_machine_state_path(layout, temporary, allow_missing_leaf=False)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(_canonical_json_bytes(payload))
            target.flush()
            os.fsync(target.fileno())
        before_replace()
        _validate_machine_state_path(layout, temporary, allow_missing_leaf=False)
        _validate_machine_state_path(layout, path, allow_missing_leaf=True)
        os.replace(temporary, path)
        temporary = None
        # Best-effort parent durability. A platform without directory fsync still
        # retains atomic file replacement semantics.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except ProjectMapError:
        raise
    except OSError as exc:
        raise ProjectMapError(f"could not write project map {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                _validate_machine_state_path(layout, temporary, allow_missing_leaf=False)
            except ProjectMapError:
                pass
            else:
                temporary.unlink(missing_ok=True)


def _generate_project_map_locked(
    registration: ProjectRegistrationResult,
) -> ProjectMapResult:
    manifest, snapshot, expected = _current_expected_map(registration)
    map_file = registration.layout.project_map_file
    _validate_machine_state_path(registration.layout, map_file, allow_missing_leaf=True)
    if map_file.exists() or map_file.is_symlink():
        existing = load_project_map(map_file, project_id=registration.project_id)
        if _manifest_identity(existing) == _manifest_identity(expected) and existing != expected:
            raise ProjectMapError("current project map is validly shaped but has been tampered with")

    def revalidate_before_replace() -> None:
        _validate_machine_state_path(
            registration.layout, manifest.manifest_file, allow_missing_leaf=False
        )
        try:
            if manifest.manifest_file.read_bytes() != snapshot:
                raise ProjectMapError(
                    "Manifest changed before project map was committed; retry inventory"
                )
        except OSError as exc:
            raise ProjectMapError(f"could not revalidate current Manifest: {exc}") from exc

    _write_atomic_json(
        map_file,
        expected,
        layout=registration.layout,
        before_replace=revalidate_before_replace,
    )
    committed = load_project_map(map_file, project_id=registration.project_id)
    if committed != expected:
        raise ProjectMapError("committed project map does not match deterministic output")
    return ProjectMapResult(
        project_id=registration.project_id,
        manifest_file=manifest.manifest_file,
        project_map_file=map_file,
        project_map=committed,
    )


def generate_project_map(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProjectMapResult:
    """Generate ``indexes/project-map.json`` under the shared machine-state lock."""

    registration, lock = _acquire_project_lock(
        workspace_root,
        project_id,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    try:
        return _generate_project_map_locked(registration)
    finally:
        lock.release()


__all__ = [
    "PROJECT_MAP_FILENAME",
    "PROJECT_MAP_KIND",
    "PROJECT_MAP_MAX_CATEGORY_CANDIDATES",
    "PROJECT_MAP_MAX_DIRECTORIES",
    "PROJECT_MAP_MAX_KEY_CANDIDATES",
    "PROJECT_MAP_SCHEMA_VERSION",
    "PROJECT_MAP_VERSION",
    "ProjectMapError",
    "ProjectMapResult",
    "build_project_map",
    "generate_project_map",
    "load_current_project_map",
    "load_project_map",
]
