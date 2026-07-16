#!/usr/bin/env python3
"""Deterministic, budget-bounded Host Context Pack assembly.

The pack is a path-free metadata protocol between Research Core and a host
agent.  It intentionally carries references rather than raw source excerpts and
never bulk-loads curated Markdown.  Canonical compact UTF-8 JSON bytes are the
budget unit so the result is deterministic across hosts and model providers.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

# Support both ``import tools.host_context`` and direct sibling imports.
if __package__:
    from .evidence_registry import load_evidence_registry
    from .file_state import file_state_from_dict
    from .project_inventory import PROJECT_MANIFEST_VERSION, load_project_manifest
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError
    from .project_registry import load_registered_project
    from .source_registry import load_source_registry
else:
    from evidence_registry import load_evidence_registry  # type: ignore[no-redef]
    from file_state import file_state_from_dict  # type: ignore[no-redef]
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from source_registry import load_source_registry  # type: ignore[no-redef]


HOST_CONTEXT_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
HOST_CONTEXT_KIND = "llmwiki-host-context-pack"
HOST_CONTEXT_VERSION = "host-context-pack-v1"
HOST_CONTEXT_DEFAULT_MAX_BYTES = 32 * 1024
HOST_CONTEXT_MIN_MAX_BYTES = 512
HOST_CONTEXT_MAX_MAX_BYTES = 1024 * 1024
HOST_CONTEXT_BUDGET_UNIT = "canonical-json-utf8-bytes"

_POLICY_LIMIT_REASONS = frozenset(
    {"sensitive-path", "content-size-limit", "outside-scan-boundary"}
)
_RISK_REASON_CODES = (
    "sensitive-path",
    "outside-scan-boundary",
    "content-size-limit",
    "unsupported-format",
)
_RISK_MESSAGES = {
    "processing-failures": (
        "One or more inventoried files report a failed processing state."
    ),
    "sensitive-path": (
        "Sensitive files remain metadata-only and are excluded from host context."
    ),
    "outside-scan-boundary": (
        "Content outside the approved scan boundary is unavailable."
    ),
    "content-size-limit": (
        "Oversized files remain metadata-only under the active scan policy."
    ),
    "unsupported-format": (
        "Unsupported file formats require a compatible extractor or explicit review."
    ),
}
_RISK_SEVERITY = {
    "processing-failures": "high",
    "sensitive-path": "high",
    "outside-scan-boundary": "high",
    "content-size-limit": "medium",
    "unsupported-format": "medium",
}


class HostContextError(LayoutError):
    """Base error for Host Context Pack validation and assembly."""

    reason_code = "host-context-invalid"


class HostContextBudgetError(HostContextError):
    """Raised when a valid mandatory envelope cannot fit the requested budget."""

    reason_code = "context-budget-too-small"

    def __init__(self, message: str, *, minimum_required_bytes: int | None = None) -> None:
        super().__init__(message)
        self.minimum_required_bytes = minimum_required_bytes


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one JSON value using the stable Host Context budget encoding."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HostContextError("Host Context Pack contains a non-canonical value") from exc


def _validate_budget(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ValueError("max_bytes must be an integer")
    if not HOST_CONTEXT_MIN_MAX_BYTES <= max_bytes <= HOST_CONTEXT_MAX_MAX_BYTES:
        raise ValueError(
            "max_bytes must be between "
            f"{HOST_CONTEXT_MIN_MAX_BYTES} and {HOST_CONTEXT_MAX_MAX_BYTES}"
        )
    return max_bytes


def _required_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostContextError(f"{label} must be an object")
    return value


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostContextError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label=label)


def _required_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise HostContextError(f"{label} must be a boolean")
    return value


def _nonnegative(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HostContextError(f"{label} must be a non-negative integer")
    return value


def _positive(value: object, *, label: str) -> int:
    result = _nonnegative(value, label=label)
    if result == 0:
        raise HostContextError(f"{label} must be a positive integer")
    return result


def _optional_daily_hours(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostContextError("daily available hours must be a finite number")
    if not math.isfinite(value) or not 0 < value <= 24:
        raise HostContextError(
            "daily available hours must be greater than 0 and at most 24"
        )
    return value


def _state_from_core(
    project_context: object,
    coverage_view: object,
    *,
    project_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project trusted Core DTOs into the closed context-pack state shape."""

    context = _required_mapping(project_context, label="project context")
    if context.get("project_id") != project_id:
        raise HostContextError("project context identity is inconsistent")
    git = _required_mapping(context.get("git"), label="project context git")
    onboarding = _required_mapping(
        context.get("onboarding"),
        label="project context onboarding",
    )

    coverage = _required_mapping(coverage_view, label="coverage view")
    if coverage.get("project_id") != project_id:
        raise HostContextError("coverage identity is inconsistent")
    report = _required_mapping(coverage.get("report"), label="coverage report")
    manifest = _required_mapping(report.get("manifest"), label="coverage manifest")
    totals = _required_mapping(report.get("totals"), label="coverage totals")
    dimensions = _required_mapping(report.get("coverage"), label="coverage dimensions")

    def count_axis(name: str) -> dict[str, int]:
        axis = _required_mapping(dimensions.get(name), label=f"coverage {name}")
        result: dict[str, int] = {}
        for key in sorted(axis):
            bucket = _required_mapping(axis[key], label=f"coverage {name} bucket")
            result[_required_text(key, label=f"coverage {name} key")] = _nonnegative(
                bucket.get("file_count"),
                label=f"coverage {name} file_count",
            )
        return result

    state = {
        "project": {
            "name": _required_text(context.get("name"), label="project name"),
            "identity_strategy": _required_text(
                context.get("identity_strategy"),
                label="identity strategy",
            ),
            "registered_at": _required_text(
                context.get("registered_at"),
                label="registered_at",
            ),
            "git": {
                "available": _required_bool(
                    git.get("available"),
                    label="git available",
                ),
                "is_repository": _required_bool(
                    git.get("is_repository"),
                    label="git is_repository",
                ),
                "branch": _optional_text(git.get("branch"), label="git branch"),
                "head_commit": _optional_text(
                    git.get("head_commit"),
                    label="git head_commit",
                ),
            },
            "onboarding": {
                "final_goal": _optional_text(
                    onboarding.get("final_goal"),
                    label="final goal",
                ),
                "current_stage": _optional_text(
                    onboarding.get("current_stage"),
                    label="current stage",
                ),
                "important_question": _optional_text(
                    onboarding.get("important_question"),
                    label="important question",
                ),
                "deadline": _optional_text(
                    onboarding.get("deadline"),
                    label="deadline",
                ),
                "daily_available_hours": _optional_daily_hours(
                    onboarding.get("daily_available_hours")
                ),
            },
        },
        "inventory": {
            "manifest_version": _required_text(
                manifest.get("manifest_version"),
                label="manifest version",
            ),
            "scan_generation": _positive(
                manifest.get("scan_generation"),
                label="scan generation",
            ),
            "file_count": _nonnegative(
                totals.get("file_count"),
                label="file count",
            ),
            "byte_count": _nonnegative(
                totals.get("byte_count"),
                label="byte count",
            ),
            "failed_file_count": _nonnegative(
                totals.get("failed_file_count"),
                label="failed file count",
            ),
            "processing_status_counts": count_axis("processing_status"),
            "read_depth_counts": count_axis("read_depth"),
        },
    }
    return state, report


def _risk_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    totals = _required_mapping(report.get("totals"), label="coverage totals")
    dimensions = _required_mapping(report.get("coverage"), label="coverage dimensions")
    reasons = _required_mapping(dimensions.get("reason"), label="coverage reasons")
    candidates: list[dict[str, Any]] = []

    failed_count = _nonnegative(
        totals.get("failed_file_count"),
        label="failed file count",
    )
    if failed_count:
        candidates.append(
            {
                "risk_code": "processing-failures",
                "severity": _RISK_SEVERITY["processing-failures"],
                "file_count": failed_count,
                "byte_count": 0,
                "summary": _RISK_MESSAGES["processing-failures"],
            }
        )

    for reason_code in _RISK_REASON_CODES:
        raw_bucket = reasons.get(reason_code)
        if raw_bucket is None:
            continue
        bucket = _required_mapping(raw_bucket, label=f"risk {reason_code}")
        file_count = _nonnegative(
            bucket.get("file_count"),
            label=f"risk {reason_code} file_count",
        )
        if not file_count:
            continue
        candidates.append(
            {
                "risk_code": reason_code,
                "severity": _RISK_SEVERITY[reason_code],
                "file_count": file_count,
                "byte_count": _nonnegative(
                    bucket.get("byte_count"),
                    label=f"risk {reason_code} byte_count",
                ),
                "summary": _RISK_MESSAGES[reason_code],
            }
        )
    return candidates


def _evidence_candidates(
    workspace_root: str | Path,
    project_id: str,
) -> tuple[list[dict[str, Any]], Counter[tuple[str, str]]]:
    """Collect current, policy-eligible path-free Evidence references."""

    registration = load_registered_project(workspace_root, project_id)
    omissions: Counter[tuple[str, str]] = Counter()
    if not registration.layout.sources_file.exists():
        omissions[("evidence_refs", "source-registry-unavailable")] += 1
        return [], omissions
    if not registration.layout.evidence_file.exists():
        omissions[("evidence_refs", "evidence-registry-unavailable")] += 1
        return [], omissions

    manifest = load_project_manifest(
        registration.layout.manifest_file,
        project_id=registration.project_id,
        project_root=registration.project_root,
        required_manifest_version=PROJECT_MANIFEST_VERSION,
    )
    source_registry = load_source_registry(workspace_root, registration.project_id)
    evidence_registry = load_evidence_registry(workspace_root, registration.project_id)
    sources = source_registry.by_source_id
    manifest_by_path = {record["path"]: record for record in manifest.file_records}
    candidates: list[dict[str, Any]] = []

    for evidence in evidence_registry.records:
        source = sources.get(evidence.source_id)
        if source is None:
            omissions[("evidence_refs", "evidence-source-unregistered")] += 1
            continue
        if (
            source.current_version != evidence.source_version
            or source.current_content_hash != evidence.content_hash
        ):
            omissions[("evidence_refs", "evidence-stale")] += 1
            continue
        if source.last_seen_scan_generation != manifest.scan_generation:
            omissions[("evidence_refs", "source-registry-out-of-date")] += 1
            continue
        file_record = manifest_by_path.get(source.current_path)
        if file_record is None:
            omissions[("evidence_refs", "evidence-source-missing")] += 1
            continue
        if file_record.get("content_sha256") != evidence.content_hash:
            omissions[("evidence_refs", "evidence-stale")] += 1
            continue

        state = file_state_from_dict(file_record.get("file_state"))
        if state.reason_code == "sensitive-path":
            omissions[("evidence_refs", "sensitive-path-filtered")] += 1
            continue
        if state.reason_code in _POLICY_LIMIT_REASONS or state.read_depth == "ignored":
            omissions[("evidence_refs", "content-policy-filtered")] += 1
            continue

        candidates.append(
            {
                "evidence_id": evidence.evidence_id,
                "source_id": evidence.source_id,
                "source_version": evidence.source_version,
                "content_hash": evidence.content_hash,
                "locator": evidence.locator.as_dict(),
                "excerpt_hash": evidence.excerpt_hash,
            }
        )

    candidates.sort(key=lambda item: item["evidence_id"])
    return candidates, omissions


def _omission_rows(
    counts: Counter[tuple[str, str]],
) -> list[dict[str, Any]]:
    return [
        {"section": section, "reason_code": reason_code, "count": count}
        for (section, reason_code), count in sorted(counts.items())
        if count > 0
    ]


def _seal_payload(payload: dict[str, Any]) -> int:
    """Reach the fixed point where ``used_bytes`` equals serialized byte length."""

    budget = _required_mapping(payload.get("budget"), label="context budget")
    budget["used_bytes"] = 0
    for _attempt in range(12):
        size = len(canonical_json_bytes(payload))
        if budget["used_bytes"] == size:
            return size
        budget["used_bytes"] = size
    raise HostContextError("Host Context Pack byte count did not converge")


def _fits(payload: dict[str, Any], max_bytes: int) -> bool:
    return _seal_payload(payload) <= max_bytes


def assemble_host_context_pack(
    workspace_root: str | Path,
    project_id: str,
    *,
    project_context: dict[str, Any],
    coverage_view: dict[str, Any],
    max_bytes: int = HOST_CONTEXT_DEFAULT_MAX_BYTES,
) -> HostContextPackResult:
    """Assemble one deterministic pack from trusted Core DTOs and registries."""

    budget_limit = _validate_budget(max_bytes)
    state, coverage_report = _state_from_core(
        project_context,
        coverage_view,
        project_id=project_id,
    )
    evidence, omissions = _evidence_candidates(workspace_root, project_id)
    omissions[("tasks", "task-store-unavailable")] += 1

    payload: dict[str, Any] = {
        "schema_version": HOST_CONTEXT_SCHEMA_VERSION,
        "kind": HOST_CONTEXT_KIND,
        "pack_version": HOST_CONTEXT_VERSION,
        "project_id": project_id,
        "budget": {
            "unit": HOST_CONTEXT_BUDGET_UNIT,
            "max_bytes": budget_limit,
            "used_bytes": 0,
            "truncated": False,
        },
        "state": state,
        "tasks": [],
        "risks": [],
        "evidence_refs": [],
        "omissions": _omission_rows(omissions),
    }

    mandatory_size = _seal_payload(payload)
    if mandatory_size > budget_limit:
        raise HostContextBudgetError(
            "max_bytes cannot hold the mandatory Host Context Pack envelope",
            minimum_required_bytes=mandatory_size,
        )

    included: list[tuple[str, dict[str, Any]]] = []
    for section, candidates in (
        ("risks", _risk_candidates(coverage_report)),
        ("evidence_refs", evidence),
    ):
        target = payload[section]
        assert isinstance(target, list)
        for candidate in candidates:
            target.append(candidate)
            if _fits(payload, budget_limit):
                included.append((section, candidate))
                continue
            target.pop()
            omissions[(section, "context-budget-exceeded")] += 1
            payload["omissions"] = _omission_rows(omissions)

    # Omission metadata itself is budgeted.  Remove the lowest-priority included
    # records (reverse insertion order) until the complete envelope fits.
    while not _fits(payload, budget_limit) and included:
        section, candidate = included.pop()
        target = payload[section]
        assert isinstance(target, list)
        target.remove(candidate)
        omissions[(section, "context-budget-exceeded")] += 1
        payload["omissions"] = _omission_rows(omissions)

    if not _fits(payload, budget_limit):
        minimum = _seal_payload(payload)
        raise HostContextBudgetError(
            "max_bytes cannot hold mandatory omission reasons",
            minimum_required_bytes=minimum,
        )

    payload["budget"]["truncated"] = any(
        reason == "context-budget-exceeded" and count > 0
        for (_section, reason), count in omissions.items()
    )
    final_size = _seal_payload(payload)
    if final_size > budget_limit:
        # Changing ``truncated`` from false to true can add one byte.  Apply the
        # same deterministic pruning rule once more if the pack sat on the edge.
        while final_size > budget_limit and included:
            section, candidate = included.pop()
            target = payload[section]
            assert isinstance(target, list)
            target.remove(candidate)
            omissions[(section, "context-budget-exceeded")] += 1
            payload["omissions"] = _omission_rows(omissions)
            final_size = _seal_payload(payload)
        if final_size > budget_limit:
            raise HostContextBudgetError(
                "max_bytes cannot hold mandatory omission reasons",
                minimum_required_bytes=final_size,
            )

    return HostContextPackResult(payload=payload)


def build_host_context_pack(
    workspace_root: str | Path,
    project_id: str,
    *,
    max_bytes: int = HOST_CONTEXT_DEFAULT_MAX_BYTES,
) -> HostContextPackResult:
    """Build through the host-neutral Research Core service facade."""

    # Local import avoids a module cycle: Research Core imports the assembler,
    # while this convenience entry point intentionally routes callers through
    # the public Core API rather than duplicating its context/coverage logic.
    if __package__:
        from .research_core import ResearchCoreService
    else:
        from research_core import ResearchCoreService  # type: ignore[no-redef]

    return ResearchCoreService(workspace_root).host_context_pack(
        project_id,
        max_bytes=max_bytes,
    )


@dataclass(frozen=True)
class HostContextPackResult:
    """Closed, deterministic Host Context Pack result."""

    payload: dict[str, Any]

    def __post_init__(self) -> None:
        payload = deepcopy(self.payload)
        if payload.get("schema_version") != HOST_CONTEXT_SCHEMA_VERSION:
            raise HostContextError("Host Context Pack schema_version is unsupported")
        if payload.get("kind") != HOST_CONTEXT_KIND:
            raise HostContextError("Host Context Pack kind is invalid")
        if payload.get("pack_version") != HOST_CONTEXT_VERSION:
            raise HostContextError("Host Context Pack version is unsupported")
        _required_text(payload.get("project_id"), label="project_id")
        budget = _required_mapping(payload.get("budget"), label="context budget")
        if budget.get("unit") != HOST_CONTEXT_BUDGET_UNIT:
            raise HostContextError("Host Context Pack budget unit is invalid")
        _required_bool(budget.get("truncated"), label="context budget truncated")
        max_bytes = _validate_budget(budget.get("max_bytes"))
        used_bytes = _seal_payload(payload)
        if used_bytes > max_bytes:
            raise HostContextBudgetError(
                "Host Context Pack exceeds its declared budget",
                minimum_required_bytes=used_bytes,
            )
        object.__setattr__(self, "payload", payload)

    @property
    def used_bytes(self) -> int:
        return self.payload["budget"]["used_bytes"]

    @property
    def max_bytes(self) -> int:
        return self.payload["budget"]["max_bytes"]

    def serialized_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload)

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(self.payload)
