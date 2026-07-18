#!/usr/bin/env python3
"""E-04 bounded execution-flow, call-chain, and data-flow analysis.

The host may provide already-grounded observations from an approved reader.  Core
only validates identities and relationships, binds them to the current B-06
Manifest, and writes a deterministic machine artifact.  With no observations it
produces an honest metadata-only flow from Manifest/project-map candidates, with
all unresolved relationships explicitly marked uncertain.  It never opens
registered source bytes, calls an LLM, writes curated Markdown, or sends data
externally.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

if __package__:
    from .advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS
    from .project_analysis import (
        ProjectAnalysisError,
        atomic_write_json,
        canonical_json_bytes,
        current_manifest_bytes,
        exact_mapping,
        locked_project,
        manifest_binding,
        manifest_records_by_path,
        parse_canonical_json,
        sha256_bytes,
        text,
    )
    from .project_layout import CURRENT_SCHEMA_VERSION, validate_project_id
else:  # pragma: no cover
    from advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS  # type: ignore[no-redef]
    from project_analysis import (  # type: ignore[no-redef]
        ProjectAnalysisError,
        atomic_write_json,
        canonical_json_bytes,
        current_manifest_bytes,
        exact_mapping,
        locked_project,
        manifest_binding,
        manifest_records_by_path,
        parse_canonical_json,
        sha256_bytes,
        text,
    )
    from project_layout import CURRENT_SCHEMA_VERSION, validate_project_id  # type: ignore[no-redef]


EXECUTION_FLOW_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
EXECUTION_FLOW_KIND = "llmwiki-execution-flow"
EXECUTION_FLOW_VERSION = "execution-flow-v1"
EXECUTION_FLOW_FILENAME = "execution-flow.json"
MAX_NODES = 2048
MAX_EDGES = 8192
MAX_EVIDENCE_IDS = 128
MAX_TEXT_BYTES = 1024
MAX_REASON_BYTES = 2048
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_EVIDENCE_RE = re.compile(r"^evd-[0-9a-f]{64}$")
_PATH_RE = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\\x00]+$")
_RELATIONS = frozenset({"calls", "data-flow", "consumes", "produces", "depends-on"})
_KINDS = frozenset({"project", "package", "module", "function", "class", "process", "file", "dataset", "service", "unknown"})
_CERTAINTIES = frozenset({"observed", "inferred", "uncertain"})
_TOP = frozenset({
    "schema_version", "kind", "flow_version", "project_id", "manifest", "derivation",
    "summary", "nodes", "edges", "entrypoints", "uncertainties", "coverage", "artifact_id",
})
_MANIFEST = frozenset({"manifest_version", "scan_generation", "ordinary_file_count", "ordinary_byte_count", "sha256"})
_DERIVATION = frozenset({"mode", "source_content_read", "llm_used", "host_observations", "semantic_input"})
_NODE = frozenset({"id", "kind", "label", "path", "symbol", "entrypoint", "evidence_ids", "certainty", "uncertainty"})
_EDGE = frozenset({"id", "source", "target", "relation", "label", "evidence_ids", "certainty", "uncertainty"})
_COVERAGE = frozenset({"manifest_files", "represented_files", "nodes", "edges", "call_edges", "data_flow_edges", "entrypoints", "uncertain_nodes", "uncertain_edges"})


class ExecutionFlowError(ProjectAnalysisError):
    """E-04 validation, persistence, or currentness failure."""


def _safe_id(value: object, label: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value):
        raise ExecutionFlowError(f"{label} must be a short path-safe stable identifier")
    return value


def _path(value: object, label: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not _PATH_RE.fullmatch(value) or value in {".", ".."}:
        raise ExecutionFlowError(f"{label} must be a canonical project-relative POSIX path")
    if "//" in value or "\\" in value:
        raise ExecutionFlowError(f"{label} must use one canonical POSIX separator")
    return value


def _evidence(value: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ExecutionFlowError(f"{label} must be a sequence of Evidence IDs")
    ids = tuple(value)
    if len(ids) > MAX_EVIDENCE_IDS or len(set(ids)) != len(ids):
        raise ExecutionFlowError(f"{label} must be duplicate-free and contain <= {MAX_EVIDENCE_IDS} IDs")
    for item in ids:
        if type(item) is not str or not _EVIDENCE_RE.fullmatch(item):
            raise ExecutionFlowError(f"{label} contains an invalid Evidence ID")
    return tuple(sorted(ids))


def _optional_text(value: object, label: str, max_bytes: int = MAX_TEXT_BYTES) -> str | None:
    if value is None:
        return None
    return text(value, label=label, max_bytes=max_bytes)


def _certainty(value: object, label: str) -> str:
    if type(value) is not str or value not in _CERTAINTIES:
        raise ExecutionFlowError(f"{label} must be one of {sorted(_CERTAINTIES)!r}")
    return value


@dataclass(frozen=True)
class FlowNodeObservation:
    """Host-declared node in an execution graph."""

    node_id: str
    kind: str
    label: str
    path: str | None = None
    symbol: str | None = None
    entrypoint: bool = False
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.node_id, "node_id")
        if self.kind not in _KINDS:
            raise ExecutionFlowError(f"node kind must be one of {sorted(_KINDS)!r}")
        text(self.label, label="node label", max_bytes=MAX_TEXT_BYTES)
        _path(self.path, "node path")
        _optional_text(self.symbol, "node symbol")
        if type(self.entrypoint) is not bool:
            raise ExecutionFlowError("node entrypoint must be boolean")
        _evidence(self.evidence_ids, "node evidence_ids")
        _certainty(self.certainty, "node certainty")
        if self.certainty == "uncertain" and not self.uncertainty:
            raise ExecutionFlowError("uncertain nodes require an uncertainty reason")
        _optional_text(self.uncertainty, "node uncertainty", MAX_REASON_BYTES)

    @classmethod
    def from_dict(cls, value: object) -> "FlowNodeObservation":
        item = exact_mapping(value, _NODE, label="flow node")
        ids = item["evidence_ids"]
        if type(ids) is not list:
            raise ExecutionFlowError("flow node evidence_ids must be a list")
        return cls(
            node_id=item["id"], kind=item["kind"], label=item["label"], path=item["path"],
            symbol=item["symbol"], entrypoint=item["entrypoint"], evidence_ids=tuple(ids),
            certainty=item["certainty"], uncertainty=item["uncertainty"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "path": self.path,
            "symbol": self.symbol,
            "entrypoint": self.entrypoint,
            "evidence_ids": list(sorted(self.evidence_ids)),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True)
class FlowEdgeObservation:
    """Host-declared directed call/data relationship."""

    source: str
    target: str
    relation: str
    label: str = ""
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.source, "edge source")
        _safe_id(self.target, "edge target")
        if self.source == self.target:
            raise ExecutionFlowError("execution-flow edges cannot be self-loops")
        if self.relation not in _RELATIONS:
            raise ExecutionFlowError(f"edge relation must be one of {sorted(_RELATIONS)!r}")
        text(self.label, label="edge label", max_bytes=MAX_TEXT_BYTES, allow_empty=True)
        _evidence(self.evidence_ids, "edge evidence_ids")
        _certainty(self.certainty, "edge certainty")
        if self.certainty == "uncertain" and not self.uncertainty:
            raise ExecutionFlowError("uncertain edges require an uncertainty reason")
        _optional_text(self.uncertainty, "edge uncertainty", MAX_REASON_BYTES)

    @classmethod
    def from_dict(cls, value: object) -> "FlowEdgeObservation":
        item = exact_mapping(value, _EDGE - {"id"}, label="flow edge observation")
        ids = item["evidence_ids"]
        if type(ids) is not list:
            raise ExecutionFlowError("flow edge evidence_ids must be a list")
        return cls(
            source=item["source"], target=item["target"], relation=item["relation"],
            label=item["label"], evidence_ids=tuple(ids), certainty=item["certainty"],
            uncertainty=item["uncertainty"],
        )


@dataclass(frozen=True)
class ExecutionFlowResult:
    project_id: str
    manifest_file: Path
    execution_flow_file: Path
    execution_flow: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "execution_flow_file": str(self.execution_flow_file),
            "execution_flow": self.execution_flow,
        }


def _node_id_fallback(path: str, record: Mapping[str, Any]) -> str:
    raw = f"file\0{path}\0{record.get('content_sha256', '')}".encode("utf-8")
    return "node-" + hashlib.sha256(raw).hexdigest()[:32]


def _edge_id(source: str, target: str, relation: str, label: str) -> str:
    return "edge-" + hashlib.sha256(f"{source}\0{target}\0{relation}\0{label}".encode("utf-8")).hexdigest()[:32]


def _artifact_id(payload_without_id: Mapping[str, Any]) -> str:
    return "flow-" + sha256_bytes(canonical_json_bytes(payload_without_id))


def _fallback_observations(manifest: Any) -> tuple[list[FlowNodeObservation], list[FlowEdgeObservation]]:
    records = manifest_records_by_path(manifest)
    nodes: list[FlowNodeObservation] = []
    for path in sorted(records):
        record = records[path]
        classification = record.get("classification", {})
        fmt = classification.get("format", "unknown") if isinstance(classification, dict) else "unknown"
        role = classification.get("research_role", "other") if isinstance(classification, dict) else "other"
        if fmt not in {"python", "javascript", "typescript", "rust", "go", "java", "c", "cpp", "shell", "markdown", "unknown"} and role not in {"code", "configuration", "run-script"}:
            continue
        name = Path(path).name
        lower = name.casefold()
        entry = lower in {"main.py", "train.py", "run.py", "cli.py", "main.js", "index.js", "index.ts", "run.sh"} or role == "run-script"
        nodes.append(FlowNodeObservation(
            node_id=_node_id_fallback(path, record),
            kind="file",
            label=path,
            path=path,
            entrypoint=entry,
            certainty="uncertain" if not entry else "inferred",
            uncertainty=None if entry else "No host call graph observation was supplied",
        ))
    return nodes[:MAX_NODES], []


def _normalize_observations(
    observations: Iterable[FlowNodeObservation | FlowEdgeObservation | Mapping[str, Any]],
) -> tuple[list[FlowNodeObservation], list[FlowEdgeObservation]]:
    nodes: list[FlowNodeObservation] = []
    edges: list[FlowEdgeObservation] = []
    for item in observations:
        if isinstance(item, FlowNodeObservation):
            nodes.append(item)
        elif isinstance(item, FlowEdgeObservation):
            edges.append(item)
        elif isinstance(item, Mapping):
            kind = item.get("record_type", item.get("kind"))
            if kind in {"node", "flow-node"} or "node_id" in item:
                data = dict(item)
                data.setdefault("id", data.pop("node_id", None))
                data.setdefault("symbol", None)
                data.setdefault("path", None)
                data.setdefault("entrypoint", False)
                data.setdefault("evidence_ids", [])
                data.setdefault("certainty", "observed")
                data.setdefault("uncertainty", None)
                data.pop("record_type", None)
                data.pop("kind", None) if data.get("kind") == "node" else None
                nodes.append(FlowNodeObservation.from_dict(data))
            elif kind in {"edge", "flow-edge"} or "source" in item:
                data = dict(item)
                data.pop("record_type", None)
                data.setdefault("label", "")
                data.setdefault("evidence_ids", [])
                data.setdefault("certainty", "observed")
                data.setdefault("uncertainty", None)
                edges.append(FlowEdgeObservation.from_dict(data))
            else:
                raise ExecutionFlowError("observation must declare a flow node or edge")
        else:
            raise ExecutionFlowError("unsupported execution-flow observation")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ExecutionFlowError("execution-flow observations exceed bounded limits")
    return nodes, edges


def _validate_file_paths(nodes: Iterable[FlowNodeObservation], records: Mapping[str, Mapping[str, Any]]) -> None:
    for node in nodes:
        if node.path is not None and node.path not in records:
            raise ExecutionFlowError(f"flow node path is absent from current Manifest: {node.path}")


def _summary(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], entrypoints: list[str], uncertain: list[str]) -> str:
    calls = sum(edge["relation"] == "calls" for edge in edges)
    data = sum(edge["relation"] in {"data-flow", "consumes", "produces"} for edge in edges)
    suffix = " Unresolved paths are marked uncertain." if uncertain else ""
    return f"{len(entrypoints)} entrypoint(s), {len(nodes)} node(s), {calls} call edge(s), and {data} data-flow edge(s)." + suffix


def build_execution_flow(
    manifest: Any,
    *,
    project_id: str,
    manifest_sha256: str,
    observations: Iterable[FlowNodeObservation | FlowEdgeObservation | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    records = manifest_records_by_path(manifest)
    raw = tuple(observations)
    if raw:
        node_obs, edge_obs = _normalize_observations(raw)
        mode = "host-observations"
        host = True
        semantic = True
    else:
        node_obs, edge_obs = _fallback_observations(manifest)
        mode = "manifest-metadata-fallback"
        host = False
        semantic = False
    _validate_file_paths(node_obs, records)
    node_by_id: dict[str, FlowNodeObservation] = {}
    for node in node_obs:
        if node.node_id in node_by_id:
            raise ExecutionFlowError(f"duplicate flow node id: {node.node_id}")
        node_by_id[node.node_id] = node
    if not node_by_id and edge_obs:
        raise ExecutionFlowError("execution-flow edges require at least one node")
    edge_payload: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    for edge in edge_obs:
        if edge.source not in node_by_id or edge.target not in node_by_id:
            raise ExecutionFlowError("execution-flow edge endpoint is not a declared node")
        key = (edge.source, edge.target, edge.relation, edge.label)
        if key in seen_edges:
            raise ExecutionFlowError("duplicate execution-flow edge")
        seen_edges.add(key)
        edge_payload.append({
            "id": _edge_id(*key),
            "source": edge.source,
            "target": edge.target,
            "relation": edge.relation,
            "label": edge.label,
            "evidence_ids": list(sorted(edge.evidence_ids)),
            "certainty": edge.certainty,
            "uncertainty": edge.uncertainty,
        })
    edge_payload.sort(key=lambda item: (item["source"], item["target"], item["relation"], item["label"]))
    node_payload = [node.as_dict() for node in sorted(node_by_id.values(), key=lambda value: value.node_id)]
    entrypoints = sorted(item["id"] for item in node_payload if item["entrypoint"])
    uncertainties = sorted({
        item["uncertainty"] for item in node_payload + edge_payload
        if item["certainty"] == "uncertain" and item["uncertainty"]
    })
    coverage = {
        "manifest_files": len(records),
        "represented_files": len({item["path"] for item in node_payload if item["path"] is not None}),
        "nodes": len(node_payload),
        "edges": len(edge_payload),
        "call_edges": sum(item["relation"] == "calls" for item in edge_payload),
        "data_flow_edges": sum(item["relation"] in {"data-flow", "consumes", "produces"} for item in edge_payload),
        "entrypoints": len(entrypoints),
        "uncertain_nodes": sum(item["certainty"] == "uncertain" for item in node_payload),
        "uncertain_edges": sum(item["certainty"] == "uncertain" for item in edge_payload),
    }
    body: dict[str, Any] = {
        "schema_version": EXECUTION_FLOW_SCHEMA_VERSION,
        "kind": EXECUTION_FLOW_KIND,
        "flow_version": EXECUTION_FLOW_VERSION,
        "project_id": project_id,
        "manifest": manifest_binding(manifest, manifest_sha256),
        "derivation": {
            "mode": mode,
            "source_content_read": False,
            "llm_used": False,
            "host_observations": host,
            "semantic_input": semantic,
        },
        "summary": _summary(node_payload, edge_payload, entrypoints, uncertainties),
        "nodes": node_payload,
        "edges": edge_payload,
        "entrypoints": entrypoints,
        "uncertainties": uncertainties,
        "coverage": coverage,
    }
    body["artifact_id"] = _artifact_id(body)
    return body


def _validate_manifest_binding(payload: Mapping[str, Any], manifest: Any, digest: str) -> None:
    if payload["manifest"] != manifest_binding(manifest, digest):
        raise ExecutionFlowError("execution-flow artifact is stale for the current Manifest")


def _validate_payload(payload: Mapping[str, Any], *, project_id: str) -> dict[str, Any]:
    top = exact_mapping(payload, _TOP, label="execution-flow artifact")
    if top["schema_version"] != EXECUTION_FLOW_SCHEMA_VERSION or top["kind"] != EXECUTION_FLOW_KIND or top["flow_version"] != EXECUTION_FLOW_VERSION:
        raise ExecutionFlowError("unsupported execution-flow schema or version")
    if top["project_id"] != validate_project_id(project_id):
        raise ExecutionFlowError("execution-flow project binding mismatch")
    exact_mapping(top["manifest"], _MANIFEST, label="execution-flow manifest")
    derivation = exact_mapping(top["derivation"], _DERIVATION, label="execution-flow derivation")
    if derivation["source_content_read"] is not False or derivation["llm_used"] is not False:
        raise ExecutionFlowError("E-04 Core artifact cannot claim source reads or LLM use")
    if derivation["mode"] not in {"host-observations", "manifest-metadata-fallback"}:
        raise ExecutionFlowError("invalid execution-flow derivation mode")
    if type(derivation["host_observations"]) is not bool or type(derivation["semantic_input"]) is not bool:
        raise ExecutionFlowError("execution-flow derivation flags must be boolean")
    text(top["summary"], label="execution-flow summary", max_bytes=4096)
    if type(top["nodes"]) is not list or type(top["edges"]) is not list or type(top["entrypoints"]) is not list or type(top["uncertainties"]) is not list:
        raise ExecutionFlowError("execution-flow collections must be arrays")
    if len(top["nodes"]) > MAX_NODES or len(top["edges"]) > MAX_EDGES:
        raise ExecutionFlowError("execution-flow exceeds bounded limits")
    node_ids: set[str] = set()
    paths: list[str] = []
    for item in top["nodes"]:
        node = exact_mapping(item, _NODE, label="execution-flow node")
        _safe_id(node["id"], "node id")
        if node["id"] in node_ids:
            raise ExecutionFlowError("duplicate execution-flow node id")
        node_ids.add(node["id"])
        if node["kind"] not in _KINDS:
            raise ExecutionFlowError("invalid execution-flow node kind")
        text(node["label"], label="node label", max_bytes=MAX_TEXT_BYTES)
        _path(node["path"], "node path")
        _optional_text(node["symbol"], "node symbol")
        if type(node["entrypoint"]) is not bool:
            raise ExecutionFlowError("node entrypoint must be boolean")
        _evidence(node["evidence_ids"], "node evidence_ids")
        _certainty(node["certainty"], "node certainty")
        _optional_text(node["uncertainty"], "node uncertainty", MAX_REASON_BYTES)
        if node["certainty"] == "uncertain" and not node["uncertainty"]:
            raise ExecutionFlowError("uncertain node lacks reason")
        if node["path"] is not None:
            paths.append(node["path"])
    edge_ids: set[str] = set()
    edge_keys: set[tuple[str, str, str, str]] = set()
    for item in top["edges"]:
        edge = exact_mapping(item, _EDGE, label="execution-flow edge")
        _safe_id(edge["id"], "edge id")
        if edge["id"] in edge_ids:
            raise ExecutionFlowError("duplicate execution-flow edge id")
        edge_ids.add(edge["id"])
        if edge["source"] not in node_ids or edge["target"] not in node_ids or edge["source"] == edge["target"]:
            raise ExecutionFlowError("invalid execution-flow edge endpoint")
        if edge["relation"] not in _RELATIONS:
            raise ExecutionFlowError("invalid execution-flow relation")
        text(edge["label"], label="edge label", max_bytes=MAX_TEXT_BYTES, allow_empty=True)
        _evidence(edge["evidence_ids"], "edge evidence_ids")
        _certainty(edge["certainty"], "edge certainty")
        _optional_text(edge["uncertainty"], "edge uncertainty", MAX_REASON_BYTES)
        if edge["certainty"] == "uncertain" and not edge["uncertainty"]:
            raise ExecutionFlowError("uncertain edge lacks reason")
        key = (edge["source"], edge["target"], edge["relation"], edge["label"])
        if key in edge_keys:
            raise ExecutionFlowError("duplicate execution-flow relationship")
        edge_keys.add(key)
        if edge["id"] != _edge_id(*key):
            raise ExecutionFlowError("execution-flow edge stable ID mismatch")
    expected_entrypoints = sorted(node["id"] for node in top["nodes"] if node["entrypoint"])
    if top["entrypoints"] != expected_entrypoints:
        raise ExecutionFlowError("execution-flow entrypoint projection is inconsistent")
    expected_uncertainties = sorted({
        item["uncertainty"] for item in top["nodes"] + top["edges"]
        if item["certainty"] == "uncertain" and item["uncertainty"]
    })
    if top["uncertainties"] != expected_uncertainties:
        raise ExecutionFlowError("execution-flow uncertainty projection is inconsistent")
    cov = exact_mapping(top["coverage"], _COVERAGE, label="execution-flow coverage")
    expected_cov = {
        "manifest_files": cov["manifest_files"],
        "represented_files": len(set(paths)),
        "nodes": len(top["nodes"]),
        "edges": len(top["edges"]),
        "call_edges": sum(e["relation"] == "calls" for e in top["edges"]),
        "data_flow_edges": sum(e["relation"] in {"data-flow", "consumes", "produces"} for e in top["edges"]),
        "entrypoints": len(expected_entrypoints),
        "uncertain_nodes": sum(n["certainty"] == "uncertain" for n in top["nodes"]),
        "uncertain_edges": sum(e["certainty"] == "uncertain" for e in top["edges"]),
    }
    if cov != expected_cov:
        raise ExecutionFlowError("execution-flow coverage projection is inconsistent")
    without_id = dict(top)
    del without_id["artifact_id"]
    if top["artifact_id"] != _artifact_id(without_id):
        raise ExecutionFlowError("execution-flow artifact stable ID mismatch")
    return top


def load_execution_flow(path: str | Path, *, project_id: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink():
        raise ExecutionFlowError("execution-flow artifact cannot be a symbolic link")
    try:
        payload = parse_canonical_json(target.read_bytes(), label=str(target))
    except ExecutionFlowError:
        raise
    except ProjectAnalysisError as exc:
        raise ExecutionFlowError(str(exc)) from exc
    except OSError as exc:
        raise ExecutionFlowError(f"could not read execution-flow artifact: {exc}") from exc
    return _validate_payload(payload, project_id=project_id)


def generate_execution_flow(
    workspace_root: str | Path,
    project_id: str,
    *,
    observations: Iterable[FlowNodeObservation | FlowEdgeObservation | Mapping[str, Any]] = (),
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ExecutionFlowResult:
    with locked_project(workspace_root, project_id, timeout_seconds=lock_timeout_seconds) as locked:
        expected = build_execution_flow(
            locked.manifest,
            project_id=locked.registration.project_id,
            manifest_sha256=locked.manifest_sha256,
            observations=observations,
        )
        target = locked.registration.layout.execution_flow_file
        atomic_write_json(
            target,
            expected,
            layout=locked.registration.layout,
            before_replace=lambda: current_manifest_bytes(locked.registration, locked.manifest_bytes),
            error_label="execution-flow",
        )
        committed = load_execution_flow(target, project_id=locked.registration.project_id)
        if committed != expected:
            raise ExecutionFlowError("committed execution-flow differs from deterministic output")
        return ExecutionFlowResult(locked.registration.project_id, locked.registration.layout.manifest_file, target, committed)


def load_current_execution_flow(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    with locked_project(workspace_root, project_id, timeout_seconds=lock_timeout_seconds) as locked:
        target = locked.registration.layout.execution_flow_file
        try:
            actual = load_execution_flow(target, project_id=locked.registration.project_id)
        except FileNotFoundError as exc:
            raise ExecutionFlowError("current execution-flow artifact is missing") from exc
        _validate_manifest_binding(actual, locked.manifest, locked.manifest_sha256)
        records = manifest_records_by_path(locked.manifest)
        for node in actual["nodes"]:
            path = node["path"]
            if path is not None and path not in records:
                raise ExecutionFlowError("execution-flow references a file absent from current Manifest")
        if actual["derivation"]["mode"] == "manifest-metadata-fallback":
            expected = build_execution_flow(
                locked.manifest,
                project_id=locked.registration.project_id,
                manifest_sha256=locked.manifest_sha256,
            )
            if actual != expected:
                raise ExecutionFlowError("metadata fallback is not deterministic current truth")
        current_manifest_bytes(locked.registration, locked.manifest_bytes)
        return actual


__all__ = [
    "EXECUTION_FLOW_FILENAME",
    "EXECUTION_FLOW_KIND",
    "EXECUTION_FLOW_VERSION",
    "ExecutionFlowError",
    "ExecutionFlowResult",
    "FlowEdgeObservation",
    "FlowNodeObservation",
    "build_execution_flow",
    "generate_execution_flow",
    "load_current_execution_flow",
    "load_execution_flow",
]
