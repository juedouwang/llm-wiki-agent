#!/usr/bin/env python3
"""E-06 deterministic config -> run -> result -> claim machine artifact."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
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
    )
    from project_layout import CURRENT_SCHEMA_VERSION, validate_project_id  # type: ignore[no-redef]

EXPERIMENT_CHAINS_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
EXPERIMENT_CHAINS_KIND = "llmwiki-experiment-chains"
EXPERIMENT_CHAINS_VERSION = "experiment-chains-v1"
EXPERIMENT_CHAINS_FILENAME = "experiment-chains.json"
MAX_CONFIGS, MAX_RUNS, MAX_RESULTS, MAX_CLAIMS, MAX_LINKS = 1024, 2048, 4096, 2048, 8192
MAX_CANDIDATES, MAX_EVIDENCE_IDS = 4096, 128
MAX_TEXT_BYTES, MAX_STRUCTURE_BYTES, MAX_ITEMS, MAX_DEPTH = 4096, 32768, 512, 6
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_EVIDENCE_RE = re.compile(r"^evd-[0-9a-f]{64}$")
_PATH_RE = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\\x00]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CERTAINTIES = frozenset({"observed", "inferred", "uncertain"})
_RUN_STATUSES = frozenset(
    {"planned", "running", "completed", "failed", "cancelled", "unknown"}
)
_RESULT_STATUSES = frozenset({"reported", "partial", "failed", "unknown"})
_RELATIONS = frozenset({"supports", "contradicts", "inconclusive", "contextualizes"})
_TOP = frozenset(
    {
        "schema_version",
        "kind",
        "chain_version",
        "project_id",
        "manifest",
        "derivation",
        "configs",
        "runs",
        "results",
        "claims",
        "result_claim_links",
        "chains",
        "candidates",
        "coverage",
        "gaps",
        "artifact_id",
    }
)
_MANIFEST = frozenset(
    {
        "manifest_version",
        "scan_generation",
        "ordinary_file_count",
        "ordinary_byte_count",
        "sha256",
    }
)
_DERIVATION = frozenset(
    {"mode", "source_content_read", "llm_used", "host_observations", "semantic_input"}
)
_CONFIG = frozenset(
    {"id", "title", "path", "parameters", "evidence_ids", "certainty", "uncertainty"}
)
_RUN = frozenset(
    {
        "id",
        "title",
        "config_id",
        "path",
        "status",
        "conditions",
        "evidence_ids",
        "certainty",
        "uncertainty",
    }
)
_RESULT = frozenset(
    {
        "id",
        "title",
        "run_id",
        "path",
        "status",
        "conditions",
        "metrics",
        "evidence_ids",
        "certainty",
        "uncertainty",
    }
)
_CLAIM = frozenset({"id", "title", "path", "evidence_ids", "certainty", "uncertainty"})
_LINK = frozenset(
    {
        "id",
        "result_id",
        "claim_id",
        "relation",
        "summary",
        "evidence_ids",
        "certainty",
        "uncertainty",
    }
)
_CHAIN = frozenset(
    {
        "id",
        "config_id",
        "run_id",
        "result_id",
        "claim_id",
        "result_claim_link_id",
        "claim_relation",
        "conditions",
        "evidence_ids",
    }
)
_CANDIDATE = frozenset(
    {"id", "kind", "title", "path", "manifest_role", "certainty", "uncertainty"}
)
_COVERAGE = frozenset(
    {
        "manifest_files",
        "represented_files",
        "configs",
        "runs",
        "results",
        "claims",
        "result_claim_links",
        "complete_chains",
        "evidence_backed_results",
        "results_with_metrics",
        "unlinked_results",
        "metadata_candidates",
    }
)


class ExperimentChainError(ProjectAnalysisError):
    """E-06 validation, persistence, or currentness failure."""


ExperimentChainsError = ExperimentChainError


def _exact(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    try:
        return exact_mapping(value, fields, label=label)
    except ProjectAnalysisError as exc:
        raise ExperimentChainError(str(exc)) from exc


def _sid(value: object, label: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value):
        raise ExperimentChainError(f"{label} must be a bounded path-safe ID")
    return value


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value) or "\x00" in value:
        raise ExperimentChainError(
            f"{label} must be {'possibly-empty ' if empty else 'non-empty '}text"
        )
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ExperimentChainError(f"{label} must be UTF-8") from exc
    if len(raw) > MAX_TEXT_BYTES:
        raise ExperimentChainError(f"{label} is too large")
    return value


def _path(value: object, label: str) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not _PATH_RE.fullmatch(value)
        or value in {".", ".."}
        or "//" in value
        or "\\" in value
    ):
        raise ExperimentChainError(
            f"{label} must be a canonical project-relative POSIX path"
        )
    return value


def _evidence(
    value: Iterable[str], label: str, *, required: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ExperimentChainError(f"{label} must be an array")
    values = tuple(value)
    if required and not values:
        raise ExperimentChainError(f"{label} must contain Evidence")
    if len(values) > MAX_EVIDENCE_IDS:
        raise ExperimentChainError(f"{label} must be bounded")
    if any(
        type(item) is not str or not _EVIDENCE_RE.fullmatch(item) for item in values
    ):
        raise ExperimentChainError(f"{label} contains an invalid Evidence ID")
    if len(values) != len(set(values)):
        raise ExperimentChainError(f"{label} must be duplicate-free")
    return tuple(sorted(values))


def _certainty(
    value: object, uncertainty: object, label: str
) -> tuple[str, str | None]:
    if type(value) is not str or value not in _CERTAINTIES:
        raise ExperimentChainError(f"{label} certainty is invalid")
    reason = None if uncertainty is None else _text(uncertainty, f"{label} uncertainty")
    if value == "uncertain" and not reason:
        raise ExperimentChainError(f"uncertain {label} requires a reason")
    return value, reason


def _json(value: object, label: str, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        raise ExperimentChainError(f"{label} is too deeply nested")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ExperimentChainError(f"{label} contains a non-finite number")
        return value
    if type(value) is str:
        return _text(value, label, empty=True)
    if isinstance(value, Mapping):
        if len(value) > MAX_ITEMS:
            raise ExperimentChainError(f"{label} has too many fields")
        if any(type(key) is not str for key in value):
            raise ExperimentChainError(f"{label} keys must be text")
        result: dict[str, Any] = {}
        for key in sorted(value):
            _text(key, f"{label} key")
            result[key] = _json(value[key], f"{label}.{key}", depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise ExperimentChainError(f"{label} has too many items")
        return [_json(item, f"{label}[{i}]", depth + 1) for i, item in enumerate(value)]
    raise ExperimentChainError(f"{label} contains an unsupported JSON value")


def _structure(value: object, label: str, *, required: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentChainError(f"{label} must be an object")
    result = _json(value, label)
    if required and not result:
        raise ExperimentChainError(f"{label} must not be empty")
    try:
        size = len(canonical_json_bytes({"value": result}))
    except ProjectAnalysisError as exc:
        raise ExperimentChainError(f"{label} cannot be serialized canonically") from exc
    if size > MAX_STRUCTURE_BYTES:
        raise ExperimentChainError(f"{label} is too large")
    return result


@dataclass(frozen=True)
class ExperimentConfigObservation:
    config_id: str
    title: str
    path: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _sid(self.config_id, "config_id")
        _text(self.title, "config title")
        _path(self.path, "config path")
        _structure(self.parameters, "config parameters")
        _evidence(self.evidence_ids, "config evidence_ids")
        _certainty(self.certainty, self.uncertainty, "config")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.config_id,
            "title": self.title,
            "path": self.path,
            "parameters": _structure(self.parameters, "config parameters"),
            "evidence_ids": list(_evidence(self.evidence_ids, "config evidence_ids")),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ExperimentConfigObservation":
        item = _exact(value, _CONFIG, "experiment config")
        if type(item["evidence_ids"]) is not list:
            raise ExperimentChainError("config evidence_ids must be an array")
        return cls(
            item["id"],
            item["title"],
            item["path"],
            item["parameters"],
            tuple(item["evidence_ids"]),
            item["certainty"],
            item["uncertainty"],
        )


@dataclass(frozen=True)
class ExperimentRunObservation:
    run_id: str
    title: str
    config_id: str
    path: str | None = None
    status: str = "unknown"
    conditions: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _sid(self.run_id, "run_id")
        _sid(self.config_id, "run config_id")
        _text(self.title, "run title")
        _path(self.path, "run path")
        if self.status not in _RUN_STATUSES:
            raise ExperimentChainError("run status is invalid")
        _structure(self.conditions, "run conditions")
        _evidence(self.evidence_ids, "run evidence_ids")
        _certainty(self.certainty, self.uncertainty, "run")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.run_id,
            "title": self.title,
            "config_id": self.config_id,
            "path": self.path,
            "status": self.status,
            "conditions": _structure(self.conditions, "run conditions"),
            "evidence_ids": list(_evidence(self.evidence_ids, "run evidence_ids")),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ExperimentRunObservation":
        item = _exact(value, _RUN, "experiment run")
        if type(item["evidence_ids"]) is not list:
            raise ExperimentChainError("run evidence_ids must be an array")
        return cls(
            item["id"],
            item["title"],
            item["config_id"],
            item["path"],
            item["status"],
            item["conditions"],
            tuple(item["evidence_ids"]),
            item["certainty"],
            item["uncertainty"],
        )


@dataclass(frozen=True)
class ExperimentResultObservation:
    result_id: str
    title: str
    run_id: str
    path: str | None = None
    status: str = "reported"
    conditions: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _sid(self.result_id, "result_id")
        _sid(self.run_id, "result run_id")
        _text(self.title, "result title")
        _path(self.path, "result path")
        if self.status not in _RESULT_STATUSES:
            raise ExperimentChainError("result status is invalid")
        _structure(self.conditions, "result conditions", required=True)
        _structure(self.metrics, "result metrics", required=True)
        _evidence(self.evidence_ids, "result evidence_ids", required=True)
        _certainty(self.certainty, self.uncertainty, "result")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.result_id,
            "title": self.title,
            "run_id": self.run_id,
            "path": self.path,
            "status": self.status,
            "conditions": _structure(
                self.conditions, "result conditions", required=True
            ),
            "metrics": _structure(self.metrics, "result metrics", required=True),
            "evidence_ids": list(
                _evidence(self.evidence_ids, "result evidence_ids", required=True)
            ),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ExperimentResultObservation":
        item = _exact(value, _RESULT, "experiment result")
        if type(item["evidence_ids"]) is not list:
            raise ExperimentChainError("result evidence_ids must be an array")
        return cls(
            item["id"],
            item["title"],
            item["run_id"],
            item["path"],
            item["status"],
            item["conditions"],
            item["metrics"],
            tuple(item["evidence_ids"]),
            item["certainty"],
            item["uncertainty"],
        )


@dataclass(frozen=True)
class ExperimentClaimObservation:
    claim_id: str
    title: str
    path: str | None = None
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _sid(self.claim_id, "claim_id")
        _text(self.title, "claim title")
        _path(self.path, "claim path")
        _evidence(self.evidence_ids, "claim evidence_ids")
        _certainty(self.certainty, self.uncertainty, "claim")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.claim_id,
            "title": self.title,
            "path": self.path,
            "evidence_ids": list(_evidence(self.evidence_ids, "claim evidence_ids")),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ExperimentClaimObservation":
        item = _exact(value, _CLAIM, "experiment claim")
        if type(item["evidence_ids"]) is not list:
            raise ExperimentChainError("claim evidence_ids must be an array")
        return cls(
            item["id"],
            item["title"],
            item["path"],
            tuple(item["evidence_ids"]),
            item["certainty"],
            item["uncertainty"],
        )


@dataclass(frozen=True)
class ResultClaimLinkObservation:
    result_id: str
    claim_id: str
    relation: str
    summary: str = ""
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _sid(self.result_id, "link result_id")
        _sid(self.claim_id, "link claim_id")
        if self.relation not in _RELATIONS:
            raise ExperimentChainError("result-claim relation is invalid")
        _text(self.summary, "link summary", empty=True)
        _evidence(self.evidence_ids, "link evidence_ids")
        _certainty(self.certainty, self.uncertainty, "link")

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "claim_id": self.claim_id,
            "relation": self.relation,
            "summary": self.summary,
            "evidence_ids": list(_evidence(self.evidence_ids, "link evidence_ids")),
            "certainty": self.certainty,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ResultClaimLinkObservation":
        item = _exact(value, _LINK - {"id"}, "result-claim link")
        if type(item["evidence_ids"]) is not list:
            raise ExperimentChainError("link evidence_ids must be an array")
        return cls(
            item["result_id"],
            item["claim_id"],
            item["relation"],
            item["summary"],
            tuple(item["evidence_ids"]),
            item["certainty"],
            item["uncertainty"],
        )


ConfigObservation = ExperimentConfigObservation
RunObservation = ExperimentRunObservation
ResultObservation = ExperimentResultObservation
ClaimObservation = ExperimentClaimObservation
ClaimLinkObservation = ResultClaimLinkObservation


@dataclass(frozen=True)
class ExperimentChainsResult:
    project_id: str
    manifest_file: Path
    experiment_chains_file: Path
    experiment_chains: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "experiment_chains_file": str(self.experiment_chains_file),
            "experiment_chains": self.experiment_chains,
        }


ExperimentChainResult = ExperimentChainsResult


def _stable(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


def _link_id(result_id: str, claim_id: str, relation: str) -> str:
    return _stable("rcl-", result_id, claim_id, relation)


def _chain_id(
    link_id: str, config_id: str, run_id: str, result_id: str, claim_id: str
) -> str:
    return _stable("chain-", link_id, config_id, run_id, result_id, claim_id)


def _artifact_id(body: Mapping[str, Any]) -> str:
    return "experiments-" + sha256_bytes(canonical_json_bytes(body))


def _fallback_candidates(manifest: Any) -> list[dict[str, Any]]:
    role_kind = {
        "configuration": "config",
        "experiment": "run",
        "notebook": "run",
        "run_log": "run",
        "result": "result",
        "figure": "result",
    }
    rows: list[dict[str, Any]] = []
    for path, record in sorted(manifest_records_by_path(manifest).items()):
        classification = record.get("classification")
        role = (
            classification.get("research_role", "unknown")
            if isinstance(classification, dict)
            else "unknown"
        )
        kind = role_kind.get(role)
        if kind is None:
            continue
        digest = record.get("content_sha256")
        digest = (
            digest if type(digest) is str and _SHA_RE.fullmatch(digest) else "0" * 64
        )
        rows.append(
            {
                "id": _stable("candidate-", kind, path, digest),
                "kind": kind,
                "title": Path(path).name,
                "path": path,
                "manifest_role": role,
                "certainty": "inferred",
                "uncertainty": "Manifest classification candidate; no experiment semantics were read",
            }
        )
    return rows[:MAX_CANDIDATES]


def _mapping_observation(item: Mapping[str, Any]) -> Any:
    data = dict(item)
    discriminator = data.pop(
        "record_type", data.pop("observation_type", data.get("kind"))
    )
    if data.get("kind") == discriminator:
        data.pop("kind", None)
    if discriminator in {"config", "experiment-config"}:
        data.setdefault("id", data.pop("config_id", None))
        data.setdefault("path", None)
        data.setdefault("parameters", {})
        data.setdefault("evidence_ids", [])
        data.setdefault("certainty", "observed")
        data.setdefault("uncertainty", None)
        return ExperimentConfigObservation.from_dict(data)
    if discriminator in {"run", "experiment-run"}:
        data.setdefault("id", data.pop("run_id", None))
        data.setdefault("path", None)
        data.setdefault("status", "unknown")
        data.setdefault("conditions", {})
        data.setdefault("evidence_ids", [])
        data.setdefault("certainty", "observed")
        data.setdefault("uncertainty", None)
        return ExperimentRunObservation.from_dict(data)
    if discriminator in {"result", "experiment-result"}:
        data.setdefault("id", data.pop("result_id", None))
        data.setdefault("path", None)
        data.setdefault("status", "reported")
        data.setdefault("conditions", {})
        data.setdefault("metrics", {})
        data.setdefault("evidence_ids", [])
        data.setdefault("certainty", "observed")
        data.setdefault("uncertainty", None)
        return ExperimentResultObservation.from_dict(data)
    if discriminator in {"claim", "experiment-claim"}:
        data.setdefault("id", data.pop("claim_id", None))
        data.setdefault("path", None)
        data.setdefault("evidence_ids", [])
        data.setdefault("certainty", "observed")
        data.setdefault("uncertainty", None)
        return ExperimentClaimObservation.from_dict(data)
    if discriminator in {"result-claim", "result-claim-link", "claim-link"}:
        data.pop("id", None)
        data.setdefault("summary", "")
        data.setdefault("evidence_ids", [])
        data.setdefault("certainty", "observed")
        data.setdefault("uncertainty", None)
        return ResultClaimLinkObservation.from_dict(data)
    raise ExperimentChainError(
        "observation must declare config, run, result, claim, or claim-link"
    )


def _normalize(
    observations: Iterable[Any],
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    groups: tuple[list[Any], ...] = ([], [], [], [], [])
    classes = (
        ExperimentConfigObservation,
        ExperimentRunObservation,
        ExperimentResultObservation,
        ExperimentClaimObservation,
        ResultClaimLinkObservation,
    )
    for raw in observations:
        item = _mapping_observation(raw) if isinstance(raw, Mapping) else raw
        for index, cls in enumerate(classes):
            if isinstance(item, cls):
                groups[index].append(item)
                break
        else:
            raise ExperimentChainError("unsupported experiment-chain observation")
    limits = (MAX_CONFIGS, MAX_RUNS, MAX_RESULTS, MAX_CLAIMS, MAX_LINKS)
    if any(len(group) > limit for group, limit in zip(groups, limits)):
        raise ExperimentChainError("experiment observations exceed bounded limits")
    return groups


def _by_id(items: Iterable[Any], attribute: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        identifier = getattr(item, attribute)
        if identifier in result:
            raise ExperimentChainError(f"duplicate {label} ID: {identifier}")
        result[identifier] = item
    return result


def _links(
    items: Iterable[ResultClaimLinkObservation],
    results: Mapping[str, Any],
    claims: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows, seen = [], set()
    for item in items:
        if item.result_id not in results or item.claim_id not in claims:
            raise ExperimentChainError("result-claim link endpoint is undeclared")
        key = (item.result_id, item.claim_id, item.relation)
        if key in seen:
            raise ExperimentChainError("duplicate result-to-claim interpretation")
        seen.add(key)
        row = item.as_dict()
        row["id"] = _link_id(*key)
        rows.append(row)
    return sorted(
        rows, key=lambda row: (row["result_id"], row["claim_id"], row["relation"])
    )


def _chains(
    configs: Mapping[str, Any],
    runs: Mapping[str, Any],
    results: Mapping[str, Any],
    claims: Mapping[str, Any],
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link in links:
        result = results[link["result_id"]]
        run = runs[result.run_id]
        config = configs[run.config_id]
        claim = claims[link["claim_id"]]
        evidence = sorted(
            set(config.evidence_ids)
            | set(run.evidence_ids)
            | set(result.evidence_ids)
            | set(claim.evidence_ids)
            | set(link["evidence_ids"])
        )
        rows.append(
            {
                "id": _chain_id(
                    link["id"],
                    config.config_id,
                    run.run_id,
                    result.result_id,
                    claim.claim_id,
                ),
                "config_id": config.config_id,
                "run_id": run.run_id,
                "result_id": result.result_id,
                "claim_id": claim.claim_id,
                "result_claim_link_id": link["id"],
                "claim_relation": link["relation"],
                "conditions": _structure(
                    result.conditions, "result conditions", required=True
                ),
                "evidence_ids": evidence,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["config_id"],
            row["run_id"],
            row["result_id"],
            row["claim_id"],
            row["claim_relation"],
        ),
    )


def _coverage(
    manifest_files: int,
    represented_files: int,
    configs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    results: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    links: list[dict[str, Any]],
    chains: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    linked = {item["result_id"] for item in links}
    return {
        "manifest_files": manifest_files,
        "represented_files": represented_files,
        "configs": len(configs),
        "runs": len(runs),
        "results": len(results),
        "claims": len(claims),
        "result_claim_links": len(links),
        "complete_chains": len(chains),
        "evidence_backed_results": sum(bool(item["evidence_ids"]) for item in results),
        "results_with_metrics": sum(bool(item["metrics"]) for item in results),
        "unlinked_results": sum(item["id"] not in linked for item in results),
        "metadata_candidates": len(candidates),
    }


def _gaps(mode: str, coverage: Mapping[str, int]) -> list[str]:
    rows: list[str] = []
    if mode == "manifest-metadata-fallback":
        rows.append("No grounded experiment observations were supplied")
    messages = (
        ("configs", "No grounded experiment configuration was supplied"),
        ("runs", "No grounded experiment run was supplied"),
        (
            "results",
            "No grounded experiment result with conditions and Evidence was supplied",
        ),
        ("claims", "No grounded Claim target was supplied"),
        (
            "result_claim_links",
            "No host-declared result-to-Claim interpretation was supplied",
        ),
        (
            "complete_chains",
            "No complete config-to-run-to-result-to-claim chain was established",
        ),
    )
    rows.extend(message for key, message in messages if coverage[key] == 0)
    if coverage["unlinked_results"]:
        rows.append(
            f"{coverage['unlinked_results']} result(s) have no host-declared Claim interpretation"
        )
    if mode == "manifest-metadata-fallback" and coverage["metadata_candidates"] == 0:
        rows.append("No experiment-related Manifest candidates were classified")
    return rows


def build_experiment_chains(
    manifest: Any,
    *,
    project_id: str,
    manifest_sha256: str,
    observations: Iterable[Any] = (),
) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    records = manifest_records_by_path(manifest)
    raw = tuple(observations)
    if raw:
        config_obs, run_obs, result_obs, claim_obs, link_obs = _normalize(raw)
        mode, candidates = "host-observations", []
    else:
        config_obs, run_obs, result_obs, claim_obs, link_obs = [], [], [], [], []
        mode, candidates = "manifest-metadata-fallback", _fallback_candidates(manifest)
    for item in (*config_obs, *run_obs, *result_obs, *claim_obs):
        path = getattr(item, "path", None)
        if path is not None and path not in records:
            raise ExperimentChainError(
                f"experiment path is absent from current Manifest: {path}"
            )
    configs = _by_id(config_obs, "config_id", "config")
    runs = _by_id(run_obs, "run_id", "run")
    results = _by_id(result_obs, "result_id", "result")
    claims = _by_id(claim_obs, "claim_id", "claim")
    for item in runs.values():
        if item.config_id not in configs:
            raise ExperimentChainError("experiment run references an undeclared config")
    for item in results.values():
        if item.run_id not in runs:
            raise ExperimentChainError("experiment result references an undeclared run")
    config_rows = [
        item.as_dict() for item in sorted(configs.values(), key=lambda x: x.config_id)
    ]
    run_rows = [
        item.as_dict() for item in sorted(runs.values(), key=lambda x: x.run_id)
    ]
    result_rows = [
        item.as_dict() for item in sorted(results.values(), key=lambda x: x.result_id)
    ]
    claim_rows = [
        item.as_dict() for item in sorted(claims.values(), key=lambda x: x.claim_id)
    ]
    link_rows = _links(link_obs, results, claims)
    chain_rows = _chains(configs, runs, results, claims, link_rows)
    paths = {
        item["path"]
        for item in config_rows + run_rows + result_rows + claim_rows + candidates
        if item["path"] is not None
    }
    coverage = _coverage(
        len(records),
        len(paths),
        config_rows,
        run_rows,
        result_rows,
        claim_rows,
        link_rows,
        chain_rows,
        candidates,
    )
    body: dict[str, Any] = {
        "schema_version": EXPERIMENT_CHAINS_SCHEMA_VERSION,
        "kind": EXPERIMENT_CHAINS_KIND,
        "chain_version": EXPERIMENT_CHAINS_VERSION,
        "project_id": project_id,
        "manifest": manifest_binding(manifest, manifest_sha256),
        "derivation": {
            "mode": mode,
            "source_content_read": False,
            "llm_used": False,
            "host_observations": bool(raw),
            "semantic_input": bool(raw),
        },
        "configs": config_rows,
        "runs": run_rows,
        "results": result_rows,
        "claims": claim_rows,
        "result_claim_links": link_rows,
        "chains": chain_rows,
        "candidates": candidates,
        "coverage": coverage,
        "gaps": _gaps(mode, coverage),
    }
    body["artifact_id"] = _artifact_id(body)
    return body


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ExperimentChainError(f"{label} must be a non-negative integer")
    return value


def _validate_manifest_binding(payload: object) -> dict[str, Any]:
    manifest = _exact(payload, _MANIFEST, "experiment-chains Manifest binding")
    _text(manifest["manifest_version"], "Manifest version")
    if type(manifest["scan_generation"]) is not int or manifest["scan_generation"] < 1:
        raise ExperimentChainError("Manifest scan_generation must be positive")
    _nonnegative_int(manifest["ordinary_file_count"], "Manifest ordinary_file_count")
    _nonnegative_int(manifest["ordinary_byte_count"], "Manifest ordinary_byte_count")
    if type(manifest["sha256"]) is not str or not _SHA_RE.fullmatch(manifest["sha256"]):
        raise ExperimentChainError("Manifest sha256 is invalid")
    return manifest


def _validate_collection(value: object, label: str, limit: int) -> list[Any]:
    if type(value) is not list:
        raise ExperimentChainError(f"{label} must be an array")
    if len(value) > limit:
        raise ExperimentChainError(f"{label} exceeds its bounded limit")
    return value


def _validate_experiment_rows(
    top: Mapping[str, Any],
) -> tuple[
    dict[str, ExperimentConfigObservation],
    dict[str, ExperimentRunObservation],
    dict[str, ExperimentResultObservation],
    dict[str, ExperimentClaimObservation],
]:
    configs: dict[str, ExperimentConfigObservation] = {}
    config_rows = _validate_collection(top["configs"], "configs", MAX_CONFIGS)
    for row in config_rows:
        item = ExperimentConfigObservation.from_dict(row)
        if item.config_id in configs:
            raise ExperimentChainError("duplicate experiment config ID")
        if row != item.as_dict():
            raise ExperimentChainError("experiment config is not canonical")
        configs[item.config_id] = item
    if config_rows != sorted(config_rows, key=lambda row: row["id"]):
        raise ExperimentChainError("configs are not in canonical order")

    runs: dict[str, ExperimentRunObservation] = {}
    run_rows = _validate_collection(top["runs"], "runs", MAX_RUNS)
    for row in run_rows:
        item = ExperimentRunObservation.from_dict(row)
        if item.run_id in runs:
            raise ExperimentChainError("duplicate experiment run ID")
        if item.config_id not in configs:
            raise ExperimentChainError("experiment run references an undeclared config")
        if row != item.as_dict():
            raise ExperimentChainError("experiment run is not canonical")
        runs[item.run_id] = item
    if run_rows != sorted(run_rows, key=lambda row: row["id"]):
        raise ExperimentChainError("runs are not in canonical order")

    results: dict[str, ExperimentResultObservation] = {}
    result_rows = _validate_collection(top["results"], "results", MAX_RESULTS)
    for row in result_rows:
        item = ExperimentResultObservation.from_dict(row)
        if item.result_id in results:
            raise ExperimentChainError("duplicate experiment result ID")
        if item.run_id not in runs:
            raise ExperimentChainError("experiment result references an undeclared run")
        if row != item.as_dict():
            raise ExperimentChainError("experiment result is not canonical")
        results[item.result_id] = item
    if result_rows != sorted(result_rows, key=lambda row: row["id"]):
        raise ExperimentChainError("results are not in canonical order")

    claims: dict[str, ExperimentClaimObservation] = {}
    claim_rows = _validate_collection(top["claims"], "claims", MAX_CLAIMS)
    for row in claim_rows:
        item = ExperimentClaimObservation.from_dict(row)
        if item.claim_id in claims:
            raise ExperimentChainError("duplicate experiment Claim ID")
        if row != item.as_dict():
            raise ExperimentChainError("experiment Claim is not canonical")
        claims[item.claim_id] = item
    if claim_rows != sorted(claim_rows, key=lambda row: row["id"]):
        raise ExperimentChainError("claims are not in canonical order")
    return configs, runs, results, claims


def _validate_links_and_chains(
    top: Mapping[str, Any],
    configs: Mapping[str, ExperimentConfigObservation],
    runs: Mapping[str, ExperimentRunObservation],
    results: Mapping[str, ExperimentResultObservation],
    claims: Mapping[str, ExperimentClaimObservation],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in _validate_collection(
        top["result_claim_links"], "result_claim_links", MAX_LINKS
    ):
        item = _exact(row, _LINK, "result-claim link")
        observation = ResultClaimLinkObservation.from_dict(
            {key: value for key, value in item.items() if key != "id"}
        )
        if observation.result_id not in results or observation.claim_id not in claims:
            raise ExperimentChainError("result-claim link endpoint is undeclared")
        key = (observation.result_id, observation.claim_id, observation.relation)
        if key in seen:
            raise ExperimentChainError("duplicate result-to-Claim interpretation")
        seen.add(key)
        expected = observation.as_dict()
        expected["id"] = _link_id(*key)
        if item != expected:
            raise ExperimentChainError(
                "result-claim link stable identity or fields mismatch"
            )
        links.append(expected)
    expected_links = sorted(
        links, key=lambda row: (row["result_id"], row["claim_id"], row["relation"])
    )
    if links != expected_links:
        raise ExperimentChainError("result_claim_links are not in canonical order")

    expected_chains = _chains(configs, runs, results, claims, links)
    chain_rows = _validate_collection(top["chains"], "chains", MAX_LINKS)
    for row in chain_rows:
        _exact(row, _CHAIN, "experiment chain")
    if chain_rows != expected_chains:
        raise ExperimentChainError(
            "experiment chains are inconsistent or not canonical"
        )
    return links, chain_rows


def validate_experiment_chains(
    payload: Mapping[str, Any], *, project_id: str
) -> dict[str, Any]:
    top = _exact(payload, _TOP, "experiment-chains artifact")
    if (
        top["schema_version"] != EXPERIMENT_CHAINS_SCHEMA_VERSION
        or top["kind"] != EXPERIMENT_CHAINS_KIND
        or top["chain_version"] != EXPERIMENT_CHAINS_VERSION
    ):
        raise ExperimentChainError("unsupported experiment-chains schema or version")
    if top["project_id"] != validate_project_id(project_id):
        raise ExperimentChainError("experiment-chains project binding mismatch")
    manifest = _validate_manifest_binding(top["manifest"])
    derivation = _exact(top["derivation"], _DERIVATION, "experiment-chains derivation")
    if derivation["mode"] not in {"host-observations", "manifest-metadata-fallback"}:
        raise ExperimentChainError("invalid experiment-chains derivation mode")
    for key in (
        "source_content_read",
        "llm_used",
        "host_observations",
        "semantic_input",
    ):
        if type(derivation[key]) is not bool:
            raise ExperimentChainError(
                f"experiment-chains derivation {key} must be boolean"
            )
    if derivation["source_content_read"] or derivation["llm_used"]:
        raise ExperimentChainError("E-06 cannot claim source reads or LLM use")
    if derivation["host_observations"] != (derivation["mode"] == "host-observations"):
        raise ExperimentChainError(
            "experiment-chains host-observation flag is inconsistent"
        )
    if derivation["semantic_input"] != derivation["host_observations"]:
        raise ExperimentChainError(
            "experiment-chains semantic-input flag is inconsistent"
        )

    configs, runs, results, claims = _validate_experiment_rows(top)
    links, chains = _validate_links_and_chains(top, configs, runs, results, claims)

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for row in _validate_collection(top["candidates"], "candidates", MAX_CANDIDATES):
        item = _exact(row, _CANDIDATE, "experiment candidate")
        identifier = _sid(item["id"], "candidate id")
        if identifier in seen_candidates:
            raise ExperimentChainError("duplicate experiment candidate ID")
        seen_candidates.add(identifier)
        if item["kind"] not in {"config", "run", "result"}:
            raise ExperimentChainError("invalid experiment candidate kind")
        _text(item["title"], "candidate title")
        _path(item["path"], "candidate path")
        _text(item["manifest_role"], "candidate Manifest role")
        certainty, uncertainty = _certainty(
            item["certainty"], item["uncertainty"], "candidate"
        )
        if certainty != "inferred" or not uncertainty:
            raise ExperimentChainError(
                "Manifest candidates must be inferred with an uncertainty reason"
            )
        candidates.append(item)
    if candidates != sorted(
        candidates, key=lambda row: (row["path"], row["kind"], row["id"])
    ):
        raise ExperimentChainError("candidates are not in canonical order")

    coverage = _exact(top["coverage"], _COVERAGE, "experiment-chains coverage")
    for key, value in coverage.items():
        _nonnegative_int(value, f"coverage {key}")
    represented_paths = {
        row["path"]
        for row in list(top["configs"])
        + list(top["runs"])
        + list(top["results"])
        + list(top["claims"])
        + candidates
        if row["path"] is not None
    }
    expected_coverage = _coverage(
        coverage["manifest_files"],
        len(represented_paths),
        list(top["configs"]),
        list(top["runs"]),
        list(top["results"]),
        list(top["claims"]),
        links,
        chains,
        candidates,
    )
    if coverage != expected_coverage:
        raise ExperimentChainError("experiment-chains coverage is inconsistent")
    if coverage["manifest_files"] != manifest["ordinary_file_count"]:
        raise ExperimentChainError(
            "experiment-chains Manifest coverage is inconsistent"
        )

    if type(top["gaps"]) is not list or any(
        type(gap) is not str for gap in top["gaps"]
    ):
        raise ExperimentChainError("experiment-chains gaps must be an array of text")
    if top["gaps"] != _gaps(derivation["mode"], coverage):
        raise ExperimentChainError("experiment-chains gaps are inconsistent")

    without_id = dict(top)
    del without_id["artifact_id"]
    if type(top["artifact_id"]) is not str or not _SHA_RE.fullmatch(
        top["artifact_id"].removeprefix("experiments-")
    ):
        raise ExperimentChainError("experiment-chains artifact_id is invalid")
    if top["artifact_id"] != _artifact_id(without_id):
        raise ExperimentChainError(
            "experiment-chains artifact stable identity mismatch"
        )
    return top


def load_experiment_chains(path: str | Path, *, project_id: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink():
        raise ExperimentChainError(
            "experiment-chains artifact cannot be a symbolic link"
        )
    try:
        payload = parse_canonical_json(target.read_bytes(), label=str(target))
    except ExperimentChainError:
        raise
    except ProjectAnalysisError as exc:
        raise ExperimentChainError(str(exc)) from exc
    except OSError as exc:
        raise ExperimentChainError(
            f"could not read experiment-chains artifact: {exc}"
        ) from exc
    return validate_experiment_chains(payload, project_id=project_id)


def generate_experiment_chains(
    workspace_root: str | Path,
    project_id: str,
    *,
    observations: Iterable[Any] = (),
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ExperimentChainsResult:
    with locked_project(
        workspace_root, project_id, timeout_seconds=lock_timeout_seconds
    ) as locked:
        expected = build_experiment_chains(
            locked.manifest,
            project_id=locked.registration.project_id,
            manifest_sha256=locked.manifest_sha256,
            observations=observations,
        )
        target = locked.registration.layout.experiment_chains_file
        atomic_write_json(
            target,
            expected,
            layout=locked.registration.layout,
            before_replace=lambda: current_manifest_bytes(
                locked.registration, locked.manifest_bytes
            ),
            error_label="experiment-chains",
        )
        committed = load_experiment_chains(
            target, project_id=locked.registration.project_id
        )
        if committed != expected:
            raise ExperimentChainError(
                "committed experiment-chains differs from deterministic output"
            )
        return ExperimentChainsResult(
            locked.registration.project_id,
            locked.registration.layout.manifest_file,
            target,
            committed,
        )


def load_current_experiment_chains(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    with locked_project(
        workspace_root, project_id, timeout_seconds=lock_timeout_seconds
    ) as locked:
        target = locked.registration.layout.experiment_chains_file
        try:
            actual = load_experiment_chains(
                target, project_id=locked.registration.project_id
            )
        except FileNotFoundError as exc:
            raise ExperimentChainError(
                "current experiment-chains artifact is missing"
            ) from exc
        expected_binding = manifest_binding(locked.manifest, locked.manifest_sha256)
        if actual["manifest"] != expected_binding:
            raise ExperimentChainError(
                "experiment-chains artifact is stale for the current Manifest"
            )
        records = manifest_records_by_path(locked.manifest)
        if actual["coverage"]["manifest_files"] != len(records):
            raise ExperimentChainError("experiment-chains Manifest coverage is stale")
        for collection in ("configs", "runs", "results", "claims", "candidates"):
            for item in actual[collection]:
                if item["path"] is not None and item["path"] not in records:
                    raise ExperimentChainError(
                        "experiment-chains path is absent from current Manifest"
                    )
        if actual["derivation"]["mode"] == "manifest-metadata-fallback":
            expected = build_experiment_chains(
                locked.manifest,
                project_id=locked.registration.project_id,
                manifest_sha256=locked.manifest_sha256,
            )
            if actual != expected:
                raise ExperimentChainError(
                    "metadata-only experiment chains are not deterministic current truth"
                )
        current_manifest_bytes(locked.registration, locked.manifest_bytes)
        return actual


__all__ = [
    "EXPERIMENT_CHAINS_FILENAME",
    "EXPERIMENT_CHAINS_KIND",
    "EXPERIMENT_CHAINS_VERSION",
    "ClaimLinkObservation",
    "ClaimObservation",
    "ConfigObservation",
    "ExperimentChainError",
    "ExperimentChainsError",
    "ExperimentChainsResult",
    "ExperimentClaimObservation",
    "ExperimentConfigObservation",
    "ExperimentResultObservation",
    "ExperimentRunObservation",
    "ResultClaimLinkObservation",
    "ResultObservation",
    "RunObservation",
    "build_experiment_chains",
    "generate_experiment_chains",
    "load_current_experiment_chains",
    "load_experiment_chains",
    "validate_experiment_chains",
]
