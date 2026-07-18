#!/usr/bin/env python3
"""Deterministic, bounded chunk -> file -> module -> project understanding.

E-03 accepts explicit host observations for policy-approved chunks. Core validates
and aggregates those inputs but never opens the source project, calls an LLM, or
writes curated Markdown. Missing semantic input is represented honestly by a
Manifest-metadata-only fallback.
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
    from .advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, AdvisoryFileLock
    from .project_inventory import PROJECT_MANIFEST_VERSION, ProjectManifest, load_project_manifest
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError, ProjectLayout, validate_project_id
    from .project_registry import ProjectRegistrationResult, load_registered_project
else:  # pragma: no cover
    from advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, AdvisoryFileLock  # type: ignore[no-redef]
    from project_inventory import PROJECT_MANIFEST_VERSION, ProjectManifest, load_project_manifest  # type: ignore[no-redef]
    from project_layout import CURRENT_SCHEMA_VERSION, LayoutError, ProjectLayout, validate_project_id  # type: ignore[no-redef]
    from project_registry import ProjectRegistrationResult, load_registered_project  # type: ignore[no-redef]

HIERARCHICAL_UNDERSTANDING_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
HIERARCHICAL_UNDERSTANDING_KIND = "llmwiki-hierarchical-understanding"
HIERARCHICAL_UNDERSTANDING_VERSION = "hierarchical-understanding-v1"
HIERARCHICAL_UNDERSTANDING_FILENAME = "hierarchical-understanding.json"
MAX_CHUNKS = 2048
MAX_FILES = 1024
MAX_MODULES = 256
MAX_CHUNKS_PER_FILE = 64
MAX_EVIDENCE_IDS_PER_NODE = 128
MAX_SUMMARY_UTF8_BYTES = 4096
MAX_TOTAL_SUMMARY_UTF8_BYTES = 512 * 1024
MAX_ID_UTF8_BYTES = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_RE = re.compile(r"^evd-[0-9a-f]{64}$")
_MODULE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*$")
_ID_PATTERNS = {
    "chunk": re.compile(r"^hu-[0-9a-f]{64}$"),
    "file": re.compile(r"^file-[0-9a-f]{64}$"),
    "module": re.compile(r"^mod-[0-9a-f]{64}$"),
    "project": re.compile(r"^proj-[0-9a-f]{64}$"),
}
_TOP = frozenset({"schema_version", "kind", "understanding_version", "project_id", "manifest", "derivation", "limits", "coverage", "chunks", "files", "modules", "project", "omissions"})
_MANIFEST = frozenset({"manifest_version", "scan_generation", "ordinary_file_count", "ordinary_byte_count", "sha256"})
_DERIVATION = frozenset({"mode", "source_content_read", "llm_used", "host_observations", "semantic_input"})
_LIMITS = frozenset({"max_chunks", "max_files", "max_modules", "max_chunks_per_file", "max_evidence_ids_per_node", "max_summary_utf8_bytes", "max_total_summary_utf8_bytes"})
_COVERAGE = frozenset({"manifest_files", "represented_files", "semantic_files", "metadata_only_files", "represented_modules", "selected_chunks", "supplied_observations", "evidence_backed_chunks"})
_CHUNK = frozenset({"id", "path", "module_id", "source_chunk_id", "summary", "metadata_only", "input", "evidence_ids"})
_FILE = frozenset({"id", "path", "content_sha256", "size_bytes", "classification", "summary", "chunk_ids", "evidence_ids", "metadata_only", "input"})
_MODULE = frozenset({"id", "module_id", "summary", "file_ids", "chunk_ids", "evidence_ids", "metadata_only", "input"})
_PROJECT = frozenset({"id", "summary", "module_ids", "file_ids", "chunk_ids", "evidence_ids", "metadata_only", "input"})
_INPUT = frozenset({"utf8_bytes", "token_estimate"})
_CLASSIFICATION = frozenset({"format", "language", "research_role"})
_OMISSIONS = frozenset({"observations", "chunks", "files", "modules", "summary_bytes", "evidence_ids"})


class HierarchicalUnderstandingError(LayoutError):
    """E-03 input, persistence, or integrity failure."""


@dataclass(frozen=True)
class ChunkObservation:
    """A host-supplied semantic summary of one already-grounded chunk."""

    path: str
    chunk_id: str
    module_id: str
    summary: str
    input_utf8_bytes: int
    input_token_estimate: int
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _relative_path(self.path, "observation.path")
        _text(self.chunk_id, "observation.chunk_id", MAX_ID_UTF8_BYTES)
        _module_id(self.module_id, "observation.module_id")
        _summary(self.summary, "observation.summary")
        _integer(self.input_utf8_bytes, "observation.input_utf8_bytes")
        _integer(self.input_token_estimate, "observation.input_token_estimate")
        ids = _evidence_ids(self.evidence_ids, "observation.evidence_ids")
        if not ids:
            raise HierarchicalUnderstandingError("semantic observations require Evidence input")
        object.__setattr__(self, "evidence_ids", tuple(ids))

    @classmethod
    def from_dict(cls, value: object) -> "ChunkObservation":
        item = _exact(value, {"path", "chunk_id", "module_id", "summary", "input_utf8_bytes", "input_token_estimate", "evidence_ids"}, "chunk observation")
        if type(item["evidence_ids"]) is not list:
            raise HierarchicalUnderstandingError("chunk observation.evidence_ids must be a list")
        return cls(
            path=item["path"], chunk_id=item["chunk_id"], module_id=item["module_id"],
            summary=item["summary"], input_utf8_bytes=item["input_utf8_bytes"],
            input_token_estimate=item["input_token_estimate"],
            evidence_ids=tuple(item["evidence_ids"]),
        )


@dataclass(frozen=True)
class HierarchicalUnderstandingResult:
    project_id: str
    manifest_file: Path
    understanding_file: Path
    understanding: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "manifest_file": str(self.manifest_file), "understanding_file": str(self.understanding_file), "understanding": self.understanding}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HierarchicalUnderstandingError(f"cannot serialize canonical JSON: {exc}") from exc


def _parse(payload: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise HierarchicalUnderstandingError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HierarchicalUnderstandingError(f"invalid hierarchical understanding JSON: {exc}") from exc
    if type(value) is not dict:
        raise HierarchicalUnderstandingError("hierarchical understanding must be an object")
    if _canonical_bytes(value) != payload:
        raise HierarchicalUnderstandingError("hierarchical understanding is not canonical JSON")
    return value


def _exact(value: object, fields: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise HierarchicalUnderstandingError(f"{label} must contain exactly {sorted(fields)!r}")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise HierarchicalUnderstandingError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str, max_bytes: int | None = None) -> str:
    if type(value) is not str or not value or "\x00" in value or unicodedata.normalize("NFC", value) != value:
        raise HierarchicalUnderstandingError(f"{label} must be non-empty NFC text without NUL")
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        raise HierarchicalUnderstandingError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return value


def _summary(
    value: object,
    label: str,
    maximum: int = MAX_SUMMARY_UTF8_BYTES,
) -> str:
    result = _text(value, label, maximum)
    if result != result.strip():
        raise HierarchicalUnderstandingError(f"{label} must not have surrounding whitespace")
    return result


def _relative_path(value: object, label: str) -> str:
    result = _text(value, label, MAX_ID_UTF8_BYTES)
    path = PurePosixPath(result)
    if "\\" in result or path.is_absolute() or path.as_posix() != result or any(part in {"", ".", ".."} for part in path.parts):
        raise HierarchicalUnderstandingError(f"{label} must be a canonical project-relative POSIX path")
    return result


def _module_id(value: object, label: str) -> str:
    result = _text(value, label, MAX_ID_UTF8_BYTES)
    if _MODULE_RE.fullmatch(result) is None:
        raise HierarchicalUnderstandingError(f"{label} must be a stable module identity")
    return result


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise HierarchicalUnderstandingError(f"{label} must be a lowercase SHA-256")
    return value


def _evidence_ids(
    value: object,
    label: str,
    maximum: int = MAX_EVIDENCE_IDS_PER_NODE,
) -> list[str]:
    if type(value) not in {list, tuple}:
        raise HierarchicalUnderstandingError(f"{label} must be a list")
    result = list(value)
    if any(type(item) is not str or _EVIDENCE_RE.fullmatch(item) is None for item in result):
        raise HierarchicalUnderstandingError(f"{label} contains an invalid Evidence ID")
    if result != sorted(set(result)) or len(result) > maximum:
        raise HierarchicalUnderstandingError(f"{label} must be sorted, duplicate-free, and bounded")
    return result


def _ids(value: object, kind: str, label: str) -> list[str]:
    pattern = _ID_PATTERNS[kind]
    if type(value) is not list or any(type(item) is not str or pattern.fullmatch(item) is None for item in value):
        raise HierarchicalUnderstandingError(f"{label} must contain valid {kind} IDs")
    if value != sorted(set(value)):
        raise HierarchicalUnderstandingError(f"{label} must be sorted and duplicate-free")
    return list(value)

def _input(value: object, label: str) -> dict[str, int]:
    item = _exact(value, _INPUT, label)
    return {"utf8_bytes": _integer(item["utf8_bytes"], f"{label}.utf8_bytes"), "token_estimate": _integer(item["token_estimate"], f"{label}.token_estimate")}


def _classification(value: object, label: str) -> dict[str, str]:
    item = _exact(value, _CLASSIFICATION, label)
    return {key: _text(item[key], f"{label}.{key}", MAX_ID_UTF8_BYTES) for key in ("format", "language", "research_role")}


def _manifest_summary(manifest: ProjectManifest, digest: str) -> dict[str, Any]:
    if manifest.manifest_version != PROJECT_MANIFEST_VERSION:
        raise HierarchicalUnderstandingError(f"E-03 requires {PROJECT_MANIFEST_VERSION}, found {manifest.manifest_version!r}")
    _sha256(digest, "manifest.sha256")
    counts = manifest.summary.get("record_counts")
    if type(counts) is not dict or type(counts.get("file")) is not int or counts["file"] < 0:
        raise HierarchicalUnderstandingError("Manifest record_counts.file is invalid")
    records = manifest.file_records
    if counts["file"] != len(records):
        raise HierarchicalUnderstandingError("Manifest ordinary file count does not reconcile")
    total_bytes = 0
    for record in records:
        _relative_path(record.get("path"), "Manifest file.path")
        if type(record.get("size_bytes")) is not int or record["size_bytes"] < 0:
            raise HierarchicalUnderstandingError("Manifest file size is invalid")
        _sha256(record.get("content_sha256"), "Manifest file.content_sha256")
        total_bytes += record["size_bytes"]
    if type(manifest.scan_generation) is not int or manifest.scan_generation < 1:
        raise HierarchicalUnderstandingError("Manifest scan_generation must be positive")
    return {"manifest_version": manifest.manifest_version, "scan_generation": manifest.scan_generation, "ordinary_file_count": len(records), "ordinary_byte_count": total_bytes, "sha256": digest}


def _records_by_path(manifest: ProjectManifest) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in manifest.file_records:
        path = _relative_path(record["path"], "Manifest file.path")
        if path in result:
            raise HierarchicalUnderstandingError(f"Manifest contains duplicate path {path!r}")
        result[path] = record
    return result


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()}"


def _fallback_module(path: str) -> str:
    parts = PurePosixPath(path).parts
    candidate = parts[0] if len(parts) > 1 else (PurePosixPath(path).stem or "project-root")
    if _MODULE_RE.fullmatch(candidate):
        return candidate
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate).strip("-._")
    if not slug or not slug[0].isalnum():
        slug = "module"
    suffix = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:64]}-{suffix}"


def _merge_evidence(
    values: Iterable[Iterable[str]],
    maximum: int = MAX_EVIDENCE_IDS_PER_NODE,
) -> list[str]:
    return sorted({item for group in values for item in group})[:maximum]


def _merge_evidence_with_omissions(
    values: Iterable[Iterable[str]],
    maximum: int,
) -> tuple[list[str], int]:
    complete = sorted({item for group in values for item in group})
    selected = complete[:maximum]
    return selected, len(complete) - len(selected)


def _truncate_summary(value: str, maximum: int) -> tuple[str, int]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, 0
    suffix = " [bounded]"
    suffix_bytes = suffix.encode("utf-8")
    if maximum <= len(suffix_bytes):
        bounded = raw[:maximum].decode("utf-8", "ignore").strip()
        if not bounded:
            bounded = "bounded"[:maximum]
    else:
        bounded = (
            raw[: maximum - len(suffix_bytes)].decode("utf-8", "ignore").rstrip()
            + suffix
        )
    return bounded, len(raw) - len(bounded.encode("utf-8"))


def _bounded_summary(
    values: Iterable[str],
    label: str,
    maximum: int = MAX_SUMMARY_UTF8_BYTES,
) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    result = " ".join(unique) if unique else f"{label}: no semantic summary supplied; metadata-only coverage."
    return _truncate_summary(result, maximum)[0]


def _sum_input(values: Iterable[Mapping[str, int]]) -> dict[str, int]:
    items = list(values)
    return {
        "utf8_bytes": sum(value["utf8_bytes"] for value in items),
        "token_estimate": sum(value["token_estimate"] for value in items),
    }


def _normalize_observations(values: Iterable[ChunkObservation | Mapping[str, Any]]) -> list[ChunkObservation]:
    result: list[ChunkObservation] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        item = value if isinstance(value, ChunkObservation) else ChunkObservation.from_dict(value)
        key = (item.path, item.chunk_id)
        if key in seen:
            raise HierarchicalUnderstandingError(f"duplicate observation {key!r}")
        seen.add(key)
        result.append(item)
    return sorted(result, key=lambda item: (item.path, item.module_id, item.chunk_id))


def _record_classification(record: Mapping[str, Any]) -> dict[str, str]:
    classification = record.get("classification")
    if type(classification) is not dict:
        classification = {}
    fields = {
        key: classification.get(key, "unknown")
        for key in ("format", "language", "research_role")
    }
    return {
        key: value if type(value) is str and value else "unknown"
        for key, value in fields.items()
    }


def _metadata_summary(
    path: str,
    classification: Mapping[str, str],
    size_bytes: int,
    maximum: int,
) -> str:
    return _bounded_summary(
        [
            f"Metadata-only file {path}; format={classification['format']}, "
            f"language={classification['language']}, "
            f"role={classification['research_role']}, size_bytes={size_bytes}."
        ],
        "file",
        maximum,
    )


def _metadata_file(
    record: Mapping[str, Any],
    path: str,
    *,
    max_summary_bytes: int = MAX_SUMMARY_UTF8_BYTES,
) -> dict[str, Any]:
    classification = _record_classification(record)
    return {
        "id": _stable_id("file", path, record["content_sha256"]),
        "path": path,
        "content_sha256": record["content_sha256"],
        "size_bytes": record["size_bytes"],
        "classification": classification,
        "summary": _metadata_summary(
            path, classification, record["size_bytes"], max_summary_bytes
        ),
        "chunk_ids": [],
        "evidence_ids": [],
        "metadata_only": True,
        "input": {"utf8_bytes": 0, "token_estimate": 0},
    }


def build_hierarchical_understanding(
    manifest: ProjectManifest,
    *,
    observations: Iterable[ChunkObservation | Mapping[str, Any]] = (),
    manifest_sha256: str | None = None,
    manifest_bytes: bytes | None = None,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build an E-03 artifact from Manifest metadata and explicit observations."""
    if manifest_bytes is not None:
        if type(manifest_bytes) is not bytes:
            raise HierarchicalUnderstandingError("manifest_bytes must be bytes")
        computed = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha256 is not None and manifest_sha256 != computed:
            raise HierarchicalUnderstandingError("Manifest hash arguments disagree")
        manifest_sha256 = computed
    if manifest_sha256 is None:
        raise HierarchicalUnderstandingError("manifest_sha256 or manifest_bytes is required")

    binding = _manifest_summary(manifest, manifest_sha256)
    records = _records_by_path(manifest)
    normalized = _normalize_observations(observations)
    config = {
        "max_chunks": MAX_CHUNKS,
        "max_files": MAX_FILES,
        "max_modules": MAX_MODULES,
        "max_chunks_per_file": MAX_CHUNKS_PER_FILE,
        "max_evidence_ids_per_node": MAX_EVIDENCE_IDS_PER_NODE,
        "max_summary_utf8_bytes": MAX_SUMMARY_UTF8_BYTES,
        "max_total_summary_utf8_bytes": MAX_TOTAL_SUMMARY_UTF8_BYTES,
    }
    if limits is not None:
        if type(limits) is not dict or set(limits) != set(config):
            raise HierarchicalUnderstandingError("limits must contain exactly all E-03 limits")
        for key, value in limits.items():
            _integer(value, f"limits.{key}", 1)
            if value > config[key]:
                raise HierarchicalUnderstandingError(f"limits.{key} exceeds the safety ceiling")
        config.update(limits)

    by_path: dict[str, list[ChunkObservation]] = defaultdict(list)
    for item in normalized:
        if item.path not in records:
            raise HierarchicalUnderstandingError(
                f"observation path is absent from current Manifest: {item.path!r}"
            )
        by_path[item.path].append(item)

    all_paths = sorted(records, key=lambda path: (path not in by_path, path))
    candidate_paths = all_paths[: config["max_files"]]
    observed_modules = sorted(
        {item.module_id for path in candidate_paths for item in by_path.get(path, ())}
    )
    observed_module_set = set(observed_modules)
    fallback_modules = sorted({_fallback_module(path) for path in candidate_paths})
    module_candidates = observed_modules + [
        name for name in fallback_modules if name not in observed_module_set
    ]
    selected_module_names = set(module_candidates[: config["max_modules"]])

    selected: list[ChunkObservation] = []
    for path in candidate_paths:
        candidates = [
            item
            for item in by_path.get(path, ())
            if item.module_id in selected_module_names
        ]
        selected.extend(candidates[: config["max_chunks_per_file"]])
    selected = selected[: config["max_chunks"]]

    omissions = {key: 0 for key in _OMISSIONS}
    chunks: list[dict[str, Any]] = []
    chunks_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selected_summary_bytes = 0
    for item in selected:
        summary, removed_bytes = _truncate_summary(
            item.summary, config["max_summary_utf8_bytes"]
        )
        summary_size = len(summary.encode("utf-8"))
        if selected_summary_bytes + summary_size > config["max_total_summary_utf8_bytes"]:
            omissions["summary_bytes"] += len(item.summary.encode("utf-8"))
            continue
        selected_summary_bytes += summary_size
        omissions["summary_bytes"] += removed_bytes
        evidence_ids, evidence_omitted = _merge_evidence_with_omissions(
            [item.evidence_ids], config["max_evidence_ids_per_node"]
        )
        omissions["evidence_ids"] += evidence_omitted
        node = {
            "id": _stable_id(
                "hu", item.path, item.module_id, item.chunk_id, summary,
                item.input_utf8_bytes, item.input_token_estimate, *evidence_ids,
            ),
            "path": item.path,
            "module_id": item.module_id,
            "source_chunk_id": item.chunk_id,
            "summary": summary,
            "metadata_only": False,
            "input": {
                "utf8_bytes": item.input_utf8_bytes,
                "token_estimate": item.input_token_estimate,
            },
            "evidence_ids": evidence_ids,
        }
        chunks.append(node)
        chunks_by_path[item.path].append(node)
    chunks.sort(key=lambda node: node["id"])

    file_modules: dict[str, list[str]] = {}
    represented_paths: list[str] = []
    for path in candidate_paths:
        names = sorted({node["module_id"] for node in chunks_by_path.get(path, ())})
        if names:
            file_modules[path] = names
            represented_paths.append(path)
            continue
        fallback = _fallback_module(path)
        if fallback in selected_module_names:
            file_modules[path] = [fallback]
            represented_paths.append(path)

    files: list[dict[str, Any]] = []
    for path in represented_paths:
        record = records[path]
        children = sorted(chunks_by_path.get(path, ()), key=lambda node: node["id"])
        if children:
            evidence_ids, evidence_omitted = _merge_evidence_with_omissions(
                (node["evidence_ids"] for node in children),
                config["max_evidence_ids_per_node"],
            )
            omissions["evidence_ids"] += evidence_omitted
            file_node = {
                "id": _stable_id("file", path, record["content_sha256"]),
                "path": path,
                "content_sha256": record["content_sha256"],
                "size_bytes": record["size_bytes"],
                "classification": _record_classification(record),
                "summary": _bounded_summary(
                    (node["summary"] for node in children),
                    "file", config["max_summary_utf8_bytes"],
                ),
                "chunk_ids": [node["id"] for node in children],
                "evidence_ids": evidence_ids,
                "metadata_only": False,
                "input": _sum_input(node["input"] for node in children),
            }
        else:
            file_node = _metadata_file(
                record, path, max_summary_bytes=config["max_summary_utf8_bytes"]
            )
        files.append(file_node)
    files.sort(key=lambda node: node["path"])

    files_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file_node in files:
        for module_name in file_modules[file_node["path"]]:
            files_by_module[module_name].append(file_node)

    modules: list[dict[str, Any]] = []
    for module_name in sorted(files_by_module):
        module_files = sorted(
            {node["id"]: node for node in files_by_module[module_name]}.values(),
            key=lambda node: node["id"],
        )
        module_chunks = sorted(
            (node for node in chunks if node["module_id"] == module_name),
            key=lambda node: node["id"],
        )
        evidence_ids, evidence_omitted = _merge_evidence_with_omissions(
            (node["evidence_ids"] for node in module_chunks),
            config["max_evidence_ids_per_node"],
        )
        omissions["evidence_ids"] += evidence_omitted
        modules.append({
            "id": _stable_id("mod", module_name),
            "module_id": module_name,
            "summary": _bounded_summary(
                (node["summary"] for node in module_files),
                "module", config["max_summary_utf8_bytes"],
            ),
            "file_ids": sorted(node["id"] for node in module_files),
            "chunk_ids": [node["id"] for node in module_chunks],
            "evidence_ids": evidence_ids,
            "metadata_only": not bool(module_chunks),
            "input": _sum_input(node["input"] for node in module_chunks),
        })
    modules.sort(key=lambda node: node["module_id"])

    project_evidence, project_evidence_omitted = _merge_evidence_with_omissions(
        (node["evidence_ids"] for node in modules),
        config["max_evidence_ids_per_node"],
    )
    omissions["evidence_ids"] += project_evidence_omitted
    project = {
        "id": _stable_id("proj", manifest.project_id, manifest_sha256),
        "summary": _bounded_summary(
            (node["summary"] for node in modules),
            "project", config["max_summary_utf8_bytes"],
        ),
        "module_ids": sorted(node["id"] for node in modules),
        "file_ids": sorted(node["id"] for node in files),
        "chunk_ids": sorted(node["id"] for node in chunks),
        "evidence_ids": project_evidence,
        "metadata_only": not bool(chunks),
        "input": _sum_input(node["input"] for node in modules),
    }

    omissions["observations"] = len(normalized) - len(chunks)
    omissions["chunks"] = omissions["observations"]
    omissions["files"] = len(records) - len(files)
    omissions["modules"] = len(set(module_candidates)) - len(modules)
    payload = {
        "schema_version": HIERARCHICAL_UNDERSTANDING_SCHEMA_VERSION,
        "kind": HIERARCHICAL_UNDERSTANDING_KIND,
        "understanding_version": HIERARCHICAL_UNDERSTANDING_VERSION,
        "project_id": manifest.project_id,
        "manifest": binding,
        "derivation": {
            "mode": "explicit-chunk-observations" if normalized else "manifest-metadata-fallback",
            "source_content_read": False,
            "llm_used": False,
            "host_observations": bool(normalized),
            "semantic_input": bool(normalized),
        },
        "limits": config,
        "coverage": {
            "manifest_files": len(records),
            "represented_files": len(files),
            "semantic_files": sum(not node["metadata_only"] for node in files),
            "metadata_only_files": sum(node["metadata_only"] for node in files),
            "represented_modules": len(modules),
            "selected_chunks": len(chunks),
            "supplied_observations": len(normalized),
            "evidence_backed_chunks": sum(bool(node["evidence_ids"]) for node in chunks),
        },
        "chunks": chunks,
        "files": files,
        "modules": modules,
        "project": project,
        "omissions": omissions,
    }
    validate_hierarchical_understanding(payload, project_id=manifest.project_id)
    return payload


def _validate_manifest(value: object) -> dict[str, Any]:
    item = _exact(value, _MANIFEST, "manifest")
    result = {"manifest_version": _text(item["manifest_version"], "manifest.manifest_version"), "scan_generation": _integer(item["scan_generation"], "manifest.scan_generation", 1), "ordinary_file_count": _integer(item["ordinary_file_count"], "manifest.ordinary_file_count"), "ordinary_byte_count": _integer(item["ordinary_byte_count"], "manifest.ordinary_byte_count"), "sha256": _sha256(item["sha256"], "manifest.sha256")}
    if result["manifest_version"] != PROJECT_MANIFEST_VERSION:
        raise HierarchicalUnderstandingError("artifact Manifest version is not current")
    return result


def _validate_chunk(
    value: object,
    index: int,
    *,
    max_evidence_ids: int = MAX_EVIDENCE_IDS_PER_NODE,
    max_summary_bytes: int = MAX_SUMMARY_UTF8_BYTES,
) -> dict[str, Any]:
    item = _exact(value, _CHUNK, f"chunks[{index}]")
    ident = _text(item["id"], f"chunks[{index}].id", MAX_ID_UTF8_BYTES)
    if _ID_PATTERNS["chunk"].fullmatch(ident) is None:
        raise HierarchicalUnderstandingError("invalid chunk ID")
    if type(item["metadata_only"]) is not bool or item["metadata_only"]:
        raise HierarchicalUnderstandingError("semantic chunks must not be metadata-only")
    result = {
        "id": ident,
        "path": _relative_path(item["path"], f"chunks[{index}].path"),
        "module_id": _module_id(item["module_id"], f"chunks[{index}].module_id"),
        "source_chunk_id": _text(
            item["source_chunk_id"], f"chunks[{index}].source_chunk_id", MAX_ID_UTF8_BYTES
        ),
        "summary": _summary(item["summary"], f"chunks[{index}].summary", max_summary_bytes),
        "metadata_only": False,
        "input": _input(item["input"], f"chunks[{index}].input"),
        "evidence_ids": _evidence_ids(
            item["evidence_ids"], f"chunks[{index}].evidence_ids", max_evidence_ids
        ),
    }
    if not result["evidence_ids"]:
        raise HierarchicalUnderstandingError("semantic chunks require non-empty Evidence input")
    expected = _stable_id(
        "hu", result["path"], result["module_id"], result["source_chunk_id"],
        result["summary"], result["input"]["utf8_bytes"],
        result["input"]["token_estimate"], *result["evidence_ids"],
    )
    if result["id"] != expected:
        raise HierarchicalUnderstandingError("chunk ID does not bind its declared inputs")
    return result


def _validate_file(
    value: object,
    index: int,
    *,
    max_evidence_ids: int = MAX_EVIDENCE_IDS_PER_NODE,
    max_summary_bytes: int = MAX_SUMMARY_UTF8_BYTES,
) -> dict[str, Any]:
    item = _exact(value, _FILE, f"files[{index}]")
    metadata = item["metadata_only"]
    if type(metadata) is not bool:
        raise HierarchicalUnderstandingError("file.metadata_only must be boolean")
    result = {
        "id": _text(item["id"], f"files[{index}].id", MAX_ID_UTF8_BYTES),
        "path": _relative_path(item["path"], f"files[{index}].path"),
        "content_sha256": _sha256(
            item["content_sha256"], f"files[{index}].content_sha256"
        ),
        "size_bytes": _integer(item["size_bytes"], f"files[{index}].size_bytes"),
        "classification": _classification(
            item["classification"], f"files[{index}].classification"
        ),
        "summary": _summary(item["summary"], f"files[{index}].summary", max_summary_bytes),
        "chunk_ids": _ids(item["chunk_ids"], "chunk", f"files[{index}].chunk_ids"),
        "evidence_ids": _evidence_ids(
            item["evidence_ids"], f"files[{index}].evidence_ids", max_evidence_ids
        ),
        "metadata_only": metadata,
        "input": _input(item["input"], f"files[{index}].input"),
    }
    if result["id"] != _stable_id("file", result["path"], result["content_sha256"]):
        raise HierarchicalUnderstandingError("file ID does not bind path and content hash")
    if metadata and (
        result["chunk_ids"]
        or result["evidence_ids"]
        or result["input"] != {"utf8_bytes": 0, "token_estimate": 0}
    ):
        raise HierarchicalUnderstandingError("metadata-only file has non-empty lower closure")
    if not metadata and not result["chunk_ids"]:
        raise HierarchicalUnderstandingError("semantic file must have chunks")
    return result


def _validate_aggregate(
    value: object,
    kind: str,
    index: int,
    *,
    max_evidence_ids: int = MAX_EVIDENCE_IDS_PER_NODE,
    max_summary_bytes: int = MAX_SUMMARY_UTF8_BYTES,
) -> dict[str, Any]:
    fields = _MODULE if kind == "module" else _PROJECT
    item = _exact(value, fields, f"{kind}s[{index}]")
    metadata = item["metadata_only"]
    if type(metadata) is not bool:
        raise HierarchicalUnderstandingError(f"{kind}.metadata_only must be boolean")
    ident = _text(item["id"], f"{kind}.id", MAX_ID_UTF8_BYTES)
    if _ID_PATTERNS[kind].fullmatch(ident) is None:
        raise HierarchicalUnderstandingError(f"invalid {kind} ID")
    result = {
        "id": ident,
        "summary": _summary(item["summary"], f"{kind}.summary", max_summary_bytes),
        "file_ids": _ids(item["file_ids"], "file", f"{kind}.file_ids"),
        "chunk_ids": _ids(item["chunk_ids"], "chunk", f"{kind}.chunk_ids"),
        "evidence_ids": _evidence_ids(
            item["evidence_ids"], f"{kind}.evidence_ids", max_evidence_ids
        ),
        "metadata_only": metadata,
        "input": _input(item["input"], f"{kind}.input"),
    }
    if kind == "module":
        result["module_id"] = _module_id(item["module_id"], f"module[{index}].module_id")
        if result["id"] != _stable_id("mod", result["module_id"]):
            raise HierarchicalUnderstandingError("module ID does not bind module identity")
    else:
        result["module_ids"] = _ids(item["module_ids"], "module", "project.module_ids")
    return result


def validate_hierarchical_understanding(
    payload: Mapping[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Validate strict shape, stable identities, budgets, and all closures."""
    if type(payload) is not dict:
        raise HierarchicalUnderstandingError("hierarchical understanding must be an object")
    top = _exact(payload, _TOP, "hierarchical understanding")
    if top["schema_version"] != HIERARCHICAL_UNDERSTANDING_SCHEMA_VERSION:
        raise HierarchicalUnderstandingError(
            "legacy or future hierarchical understanding schema is unsupported"
        )
    if (
        top["kind"] != HIERARCHICAL_UNDERSTANDING_KIND
        or top["understanding_version"] != HIERARCHICAL_UNDERSTANDING_VERSION
    ):
        raise HierarchicalUnderstandingError("unsupported hierarchical understanding kind/version")
    actual_project = _text(top["project_id"], "project_id")
    if project_id is not None and actual_project != validate_project_id(project_id):
        raise HierarchicalUnderstandingError("project_id does not match requested project")
    manifest_binding = _validate_manifest(top["manifest"])

    derivation = _exact(top["derivation"], _DERIVATION, "derivation")
    if derivation["mode"] not in {
        "explicit-chunk-observations", "manifest-metadata-fallback"
    }:
        raise HierarchicalUnderstandingError("unsupported derivation.mode")
    for key in ("source_content_read", "llm_used", "host_observations", "semantic_input"):
        if type(derivation[key]) is not bool:
            raise HierarchicalUnderstandingError(f"derivation.{key} must be boolean")
    if derivation["source_content_read"] or derivation["llm_used"]:
        raise HierarchicalUnderstandingError("E-03 artifact cannot claim source reads or LLM use")

    raw_limits = _exact(top["limits"], _LIMITS, "limits")
    ceilings = {
        "max_chunks": MAX_CHUNKS,
        "max_files": MAX_FILES,
        "max_modules": MAX_MODULES,
        "max_chunks_per_file": MAX_CHUNKS_PER_FILE,
        "max_evidence_ids_per_node": MAX_EVIDENCE_IDS_PER_NODE,
        "max_summary_utf8_bytes": MAX_SUMMARY_UTF8_BYTES,
        "max_total_summary_utf8_bytes": MAX_TOTAL_SUMMARY_UTF8_BYTES,
    }
    limits: dict[str, int] = {}
    for key in _LIMITS:
        limits[key] = _integer(raw_limits[key], f"limits.{key}", 1)
        if limits[key] > ceilings[key]:
            raise HierarchicalUnderstandingError(f"limits.{key} exceeds the safety ceiling")

    coverage = _exact(top["coverage"], _COVERAGE, "coverage")
    for key in _COVERAGE:
        _integer(coverage[key], f"coverage.{key}")
    omissions = _exact(top["omissions"], _OMISSIONS, "omissions")
    for key in _OMISSIONS:
        _integer(omissions[key], f"omissions.{key}")

    raw_chunks, raw_files, raw_modules = top["chunks"], top["files"], top["modules"]
    if type(raw_chunks) is not list or type(raw_files) is not list or type(raw_modules) is not list:
        raise HierarchicalUnderstandingError("chunks/files/modules must be lists")
    chunks = [
        _validate_chunk(
            value, index,
            max_evidence_ids=limits["max_evidence_ids_per_node"],
            max_summary_bytes=limits["max_summary_utf8_bytes"],
        )
        for index, value in enumerate(raw_chunks)
    ]
    files = [
        _validate_file(
            value, index,
            max_evidence_ids=limits["max_evidence_ids_per_node"],
            max_summary_bytes=limits["max_summary_utf8_bytes"],
        )
        for index, value in enumerate(raw_files)
    ]
    modules = [
        _validate_aggregate(
            value, "module", index,
            max_evidence_ids=limits["max_evidence_ids_per_node"],
            max_summary_bytes=limits["max_summary_utf8_bytes"],
        )
        for index, value in enumerate(raw_modules)
    ]
    project = _validate_aggregate(
        top["project"], "project", 0,
        max_evidence_ids=limits["max_evidence_ids_per_node"],
        max_summary_bytes=limits["max_summary_utf8_bytes"],
    )

    if (
        len({node["id"] for node in chunks}) != len(chunks)
        or len({node["id"] for node in files}) != len(files)
        or len({node["id"] for node in modules}) != len(modules)
        or len({node["module_id"] for node in modules}) != len(modules)
    ):
        raise HierarchicalUnderstandingError("hierarchical IDs must be unique")
    if len({(node["path"], node["source_chunk_id"]) for node in chunks}) != len(chunks):
        raise HierarchicalUnderstandingError("source chunk identities must be unique per file")

    chunk_by_id = {node["id"]: node for node in chunks}
    file_by_id = {node["id"]: node for node in files}
    module_by_id = {node["id"]: node for node in modules}
    module_by_name = {node["module_id"]: node for node in modules}
    paths = [node["path"] for node in files]
    if paths != sorted(paths) or paths != sorted(set(paths)):
        raise HierarchicalUnderstandingError("files must be sorted by unique path")
    if [node["id"] for node in chunks] != sorted(node["id"] for node in chunks):
        raise HierarchicalUnderstandingError("chunks must be canonically sorted")
    if [node["module_id"] for node in modules] != sorted(node["module_id"] for node in modules):
        raise HierarchicalUnderstandingError("modules must be canonically sorted")

    file_path_set = set(paths)
    for chunk in chunks:
        if chunk["module_id"] not in module_by_name:
            raise HierarchicalUnderstandingError("chunk module does not exist")
        if chunk["path"] not in file_path_set:
            raise HierarchicalUnderstandingError("chunk path does not exist in file projection")

    for file_node in files:
        expected_chunks = sorted(
            node["id"] for node in chunks if node["path"] == file_node["path"]
        )
        if file_node["chunk_ids"] != expected_chunks:
            raise HierarchicalUnderstandingError("file chunk closure is invalid")
        if len(expected_chunks) > limits["max_chunks_per_file"]:
            raise HierarchicalUnderstandingError("file exceeds the declared chunk budget")
        if file_node["metadata_only"] != (not bool(expected_chunks)):
            raise HierarchicalUnderstandingError("file metadata-only state is invalid")
        expected_evidence = _merge_evidence(
            (chunk_by_id[chunk_id]["evidence_ids"] for chunk_id in expected_chunks),
            limits["max_evidence_ids_per_node"],
        )
        if file_node["evidence_ids"] != expected_evidence:
            raise HierarchicalUnderstandingError("file Evidence closure is invalid")
        expected_input = _sum_input(
            chunk_by_id[chunk_id]["input"] for chunk_id in expected_chunks
        )
        if file_node["input"] != expected_input:
            raise HierarchicalUnderstandingError("file input closure is invalid")
        if expected_chunks:
            expected_summary = _bounded_summary(
                (chunk_by_id[chunk_id]["summary"] for chunk_id in expected_chunks),
                "file", limits["max_summary_utf8_bytes"],
            )
        else:
            expected_summary = _metadata_summary(
                file_node["path"], file_node["classification"],
                file_node["size_bytes"], limits["max_summary_utf8_bytes"],
            )
        if file_node["summary"] != expected_summary:
            raise HierarchicalUnderstandingError("file summary closure is invalid")

    expected_module_names: set[str] = set()
    file_module_names: dict[str, set[str]] = {}
    for file_node in files:
        if file_node["chunk_ids"]:
            names = {
                chunk_by_id[chunk_id]["module_id"] for chunk_id in file_node["chunk_ids"]
            }
        else:
            names = {_fallback_module(file_node["path"])}
        file_module_names[file_node["id"]] = names
        expected_module_names.update(names)
    if set(module_by_name) != expected_module_names:
        raise HierarchicalUnderstandingError("module projection does not cover files exactly")

    for module in modules:
        expected_files = sorted(
            file_id for file_id, names in file_module_names.items()
            if module["module_id"] in names
        )
        if module["file_ids"] != expected_files:
            raise HierarchicalUnderstandingError("module file closure is invalid")
        expected_chunks = sorted(
            node["id"] for node in chunks if node["module_id"] == module["module_id"]
        )
        if module["chunk_ids"] != expected_chunks:
            raise HierarchicalUnderstandingError("module chunk closure is invalid")
        expected_evidence = _merge_evidence(
            (chunk_by_id[chunk_id]["evidence_ids"] for chunk_id in expected_chunks),
            limits["max_evidence_ids_per_node"],
        )
        if module["evidence_ids"] != expected_evidence:
            raise HierarchicalUnderstandingError("module Evidence closure is invalid")
        expected_input = _sum_input(
            chunk_by_id[chunk_id]["input"] for chunk_id in expected_chunks
        )
        if module["input"] != expected_input:
            raise HierarchicalUnderstandingError("module input closure is invalid")
        if module["metadata_only"] != (not bool(expected_chunks)):
            raise HierarchicalUnderstandingError("module metadata-only state is invalid")
        expected_summary = _bounded_summary(
            (file_by_id[file_id]["summary"] for file_id in expected_files),
            "module", limits["max_summary_utf8_bytes"],
        )
        if module["summary"] != expected_summary:
            raise HierarchicalUnderstandingError("module summary closure is invalid")

    if (
        project["file_ids"] != sorted(file_by_id)
        or project["chunk_ids"] != sorted(chunk_by_id)
        or project["module_ids"] != sorted(module_by_id)
    ):
        raise HierarchicalUnderstandingError("project lower-level closure is invalid")
    if project["evidence_ids"] != _merge_evidence(
        (module["evidence_ids"] for module in modules),
        limits["max_evidence_ids_per_node"],
    ):
        raise HierarchicalUnderstandingError("project Evidence closure is invalid")
    if project["input"] != _sum_input(module["input"] for module in modules):
        raise HierarchicalUnderstandingError("project input closure is invalid")
    if project["metadata_only"] != (not bool(chunks)):
        raise HierarchicalUnderstandingError("project metadata-only state is invalid")
    if project["summary"] != _bounded_summary(
        (module["summary"] for module in modules),
        "project", limits["max_summary_utf8_bytes"],
    ):
        raise HierarchicalUnderstandingError("project summary closure is invalid")
    if project["id"] != _stable_id("proj", actual_project, manifest_binding["sha256"]):
        raise HierarchicalUnderstandingError("project ID does not bind current identity")

    if coverage["manifest_files"] != manifest_binding["ordinary_file_count"]:
        raise HierarchicalUnderstandingError("coverage Manifest count is invalid")
    if coverage["manifest_files"] < len(files):
        raise HierarchicalUnderstandingError("represented files exceed Manifest coverage")
    if (
        coverage["selected_chunks"] != len(chunks)
        or coverage["represented_files"] != len(files)
        or coverage["represented_modules"] != len(modules)
    ):
        raise HierarchicalUnderstandingError("coverage counts do not reconcile")
    if (
        coverage["semantic_files"] != sum(not node["metadata_only"] for node in files)
        or coverage["metadata_only_files"] != sum(node["metadata_only"] for node in files)
        or coverage["semantic_files"] + coverage["metadata_only_files"] != len(files)
    ):
        raise HierarchicalUnderstandingError("file coverage does not reconcile")
    if coverage["evidence_backed_chunks"] != len(chunks):
        raise HierarchicalUnderstandingError("Evidence coverage does not reconcile")
    if coverage["supplied_observations"] < len(chunks):
        raise HierarchicalUnderstandingError("selected chunks exceed supplied observations")
    expected_omissions = coverage["supplied_observations"] - len(chunks)
    if omissions["observations"] != expected_omissions or omissions["chunks"] != expected_omissions:
        raise HierarchicalUnderstandingError("observation omissions do not reconcile")
    if omissions["files"] != coverage["manifest_files"] - len(files):
        raise HierarchicalUnderstandingError("file omissions do not reconcile")

    has_observations = coverage["supplied_observations"] > 0
    expected_mode = "explicit-chunk-observations" if has_observations else "manifest-metadata-fallback"
    if (
        derivation["mode"] != expected_mode
        or derivation["host_observations"] != has_observations
        or derivation["semantic_input"] != has_observations
    ):
        raise HierarchicalUnderstandingError("derivation mode does not match observations")
    if len(chunks) > limits["max_chunks"] or len(files) > limits["max_files"] or len(modules) > limits["max_modules"]:
        raise HierarchicalUnderstandingError("artifact exceeds declared budgets")
    if sum(len(node["summary"].encode("utf-8")) for node in chunks) > limits["max_total_summary_utf8_bytes"]:
        raise HierarchicalUnderstandingError("chunk summaries exceed total summary budget")
    return dict(payload)


def load_hierarchical_understanding(path: str | Path, *, project_id: str | None = None) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink():
        raise HierarchicalUnderstandingError("hierarchical understanding path is a symbolic link")
    try:
        payload = _parse(target.read_bytes())
    except OSError as exc:
        raise HierarchicalUnderstandingError(f"could not read hierarchical understanding: {exc}") from exc
    validate_hierarchical_understanding(payload, project_id=project_id)
    return payload


def _machine_path(layout: ProjectLayout, path: Path, *, allow_missing_leaf: bool) -> None:
    try:
        layout.validate_machine_state_path(path, allow_missing_leaf=allow_missing_leaf)
    except Exception as exc:
        raise HierarchicalUnderstandingError(str(exc)) from exc


def _manifest_snapshot(registration: ProjectRegistrationResult) -> tuple[ProjectManifest, bytes, str]:
    path = registration.layout.manifest_file
    _machine_path(registration.layout, path, allow_missing_leaf=False)
    try:
        snapshot = path.read_bytes()
        manifest = load_project_manifest(path, project_id=registration.project_id, project_root=registration.project_root, required_manifest_version=PROJECT_MANIFEST_VERSION)
        if path.read_bytes() != snapshot:
            raise HierarchicalUnderstandingError("Manifest changed while understanding was generated")
    except HierarchicalUnderstandingError:
        raise
    except Exception as exc:
        raise HierarchicalUnderstandingError(f"could not load current Manifest: {exc}") from exc
    digest = hashlib.sha256(snapshot).hexdigest()
    _manifest_summary(manifest, digest)
    return manifest, snapshot, digest


def _acquire(workspace_root: str | Path, project_id: str, timeout: float) -> tuple[ProjectRegistrationResult, AdvisoryFileLock]:
    registration = load_registered_project(workspace_root, validate_project_id(project_id))
    lock_path = registration.layout.machine_state_lock_file
    _machine_path(registration.layout, lock_path, allow_missing_leaf=True)
    lock = AdvisoryFileLock(lock_path, timeout_seconds=timeout)
    lock.acquire()
    return registration, lock


def _write_atomic(path: Path, payload: Mapping[str, Any], *, layout: ProjectLayout, before_replace: Callable[[], None]) -> None:
    _machine_path(layout, path, allow_missing_leaf=True)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise HierarchicalUnderstandingError(f"machine-state index directory is unavailable: {path.parent}")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        _machine_path(layout, temporary, allow_missing_leaf=False)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_canonical_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        before_replace()
        _machine_path(layout, path, allow_missing_leaf=True)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except HierarchicalUnderstandingError:
        raise
    except OSError as exc:
        raise HierarchicalUnderstandingError(f"could not write hierarchical understanding: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def generate_hierarchical_understanding(
    workspace_root: str | Path,
    project_id: str,
    *,
    observations: Iterable[ChunkObservation | Mapping[str, Any]] = (),
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> HierarchicalUnderstandingResult:
    """Publish ``indexes/hierarchical-understanding.json`` under the shared lock."""
    registration, lock = _acquire(workspace_root, project_id, lock_timeout_seconds)
    try:
        manifest, snapshot, digest = _manifest_snapshot(registration)
        expected = build_hierarchical_understanding(manifest, observations=observations, manifest_sha256=digest)
        target = registration.layout.hierarchical_understanding_file
        def revalidate() -> None:
            _machine_path(registration.layout, manifest.manifest_file, allow_missing_leaf=False)
            if manifest.manifest_file.read_bytes() != snapshot:
                raise HierarchicalUnderstandingError("Manifest changed before hierarchical understanding was committed; retry inventory")
        _write_atomic(target, expected, layout=registration.layout, before_replace=revalidate)
        committed = load_hierarchical_understanding(target, project_id=registration.project_id)
        if committed != expected:
            raise HierarchicalUnderstandingError("committed hierarchical understanding differs from deterministic output")
        return HierarchicalUnderstandingResult(registration.project_id, manifest.manifest_file, target, committed)
    finally:
        lock.release()


def _validate_current_projection(
    payload: Mapping[str, Any],
    manifest: ProjectManifest,
    digest: str,
) -> None:
    expected_binding = _manifest_summary(manifest, digest)
    if payload["manifest"] != expected_binding:
        raise HierarchicalUnderstandingError(
            "hierarchical understanding is stale for the current Manifest"
        )
    records = _records_by_path(manifest)
    if payload["coverage"]["manifest_files"] != len(records):
        raise HierarchicalUnderstandingError(
            "hierarchical understanding coverage is stale for the current Manifest"
        )
    for file_node in payload["files"]:
        record = records.get(file_node["path"])
        if record is None:
            raise HierarchicalUnderstandingError(
                "hierarchical understanding references a file absent from the current Manifest"
            )
        expected = {
            "content_sha256": record["content_sha256"],
            "size_bytes": record["size_bytes"],
            "classification": _record_classification(record),
        }
        actual = {
            "content_sha256": file_node["content_sha256"],
            "size_bytes": file_node["size_bytes"],
            "classification": file_node["classification"],
        }
        if actual != expected:
            raise HierarchicalUnderstandingError(
                "hierarchical understanding file projection does not match the current Manifest"
            )
    if payload["derivation"]["mode"] == "manifest-metadata-fallback":
        expected = build_hierarchical_understanding(manifest, manifest_sha256=digest)
        if payload != expected:
            raise HierarchicalUnderstandingError(
                "metadata-only understanding does not match deterministic current truth"
            )


def load_current_hierarchical_understanding(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Load E-03 only when its exact Manifest binding remains current."""
    registration, lock = _acquire(workspace_root, project_id, lock_timeout_seconds)
    try:
        manifest, snapshot, digest = _manifest_snapshot(registration)
        target = registration.layout.hierarchical_understanding_file
        _machine_path(registration.layout, target, allow_missing_leaf=False)
        actual = load_hierarchical_understanding(target, project_id=registration.project_id)
        _validate_current_projection(actual, manifest, digest)
        if manifest.manifest_file.read_bytes() != snapshot:
            raise HierarchicalUnderstandingError(
                "Manifest changed while current understanding was loaded"
            )
        return actual
    finally:
        lock.release()


__all__ = [
    "HIERARCHICAL_UNDERSTANDING_FILENAME", "HIERARCHICAL_UNDERSTANDING_KIND",
    "HIERARCHICAL_UNDERSTANDING_SCHEMA_VERSION", "HIERARCHICAL_UNDERSTANDING_VERSION",
    "MAX_CHUNKS", "MAX_CHUNKS_PER_FILE", "MAX_FILES", "MAX_MODULES",
    "ChunkObservation", "HierarchicalUnderstandingError", "HierarchicalUnderstandingResult",
    "build_hierarchical_understanding", "generate_hierarchical_understanding",
    "load_current_hierarchical_understanding", "load_hierarchical_understanding",
    "validate_hierarchical_understanding",
]
