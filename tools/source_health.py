#!/usr/bin/env python3
"""Deterministic aggregate health for registered sources and persisted Evidence.

D-06 verifies current source bytes, allows D-05 to repair one unique relocation,
and then reopens every current Evidence locator without writing to the source
project or calling an external service.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

if __package__:
    from .evidence_registry import (
        Evidence,
        EvidenceRegistry,
        load_evidence_registry,
        validate_evidence,
    )
    from .extraction_schema import Locator
    from .project_layout import CURRENT_SCHEMA_VERSION, LayoutError
    from .project_registry import load_registered_project
    from .source_access import (
        SourceAccessError,
        SourceBoundaryError,
        SourceContentMismatchError,
        SourceExcerptMismatchError,
        SourceFormatError,
        SourceLocatorError,
        SourceMissingError,
        SourceNotFoundError,
        SourceReadError,
        SourceRelocationAmbiguousError,
        SourceVersionMismatchError,
        open_source,
    )
    from .source_recovery import (
        SourceRecoveryError,
        SourceRecoveryResult,
        recover_source,
    )
    from .source_registry import SourceRecord, SourceRegistry, load_source_registry
else:
    from evidence_registry import (  # type: ignore[no-redef]
        Evidence,
        EvidenceRegistry,
        load_evidence_registry,
        validate_evidence,
    )
    from extraction_schema import Locator  # type: ignore[no-redef]
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from source_access import (  # type: ignore[no-redef]
        SourceAccessError,
        SourceBoundaryError,
        SourceContentMismatchError,
        SourceExcerptMismatchError,
        SourceFormatError,
        SourceLocatorError,
        SourceMissingError,
        SourceNotFoundError,
        SourceReadError,
        SourceRelocationAmbiguousError,
        SourceVersionMismatchError,
        open_source,
    )
    from source_recovery import (  # type: ignore[no-redef]
        SourceRecoveryError,
        SourceRecoveryResult,
        recover_source,
    )
    from source_registry import (  # type: ignore[no-redef]
        SourceRecord,
        SourceRegistry,
        load_source_registry,
    )


SOURCE_HEALTH_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SOURCE_HEALTH_VERSION = "source-health-v1"
SOURCE_HEALTH_REPORT_KIND = "llmwiki-source-health-report"
SOURCE_HEALTH_SOURCE_KIND = "llmwiki-source-health"
SOURCE_HEALTH_EVIDENCE_KIND = "llmwiki-evidence-health"
SOURCE_HEALTH_STATUSES = ("valid", "stale", "missing", "ambiguous")

HealthStatus = Literal["valid", "stale", "missing", "ambiguous"]

_STATUS_SEVERITY = {
    "valid": 0,
    "stale": 1,
    "missing": 2,
    "ambiguous": 3,
}


class SourceHealthError(LayoutError):
    """Raised when a complete, reconciled health result cannot be produced."""

    reason_code = "source-health-failed"


def _health_status(value: str) -> HealthStatus:
    if value not in SOURCE_HEALTH_STATUSES:
        raise SourceHealthError(f"unsupported source health status: {value}")
    return value  # type: ignore[return-value]


def _status_counts(records: Sequence[SourceHealthRecord | EvidenceHealthRecord]) -> dict[str, int]:
    counts = {status: 0 for status in SOURCE_HEALTH_STATUSES}
    for record in records:
        counts[record.status] += 1
    return counts


@dataclass(frozen=True)
class SourceHealthRecord:
    """One registered source's current availability and exact-hash state."""

    project_id: str
    source_id: str
    status: HealthStatus
    reason_code: str
    detail: str
    current_path: str
    current_version: int | None
    content_hash: str | None
    recovery: SourceRecoveryResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _health_status(self.status))
        if self.recovery is not None and self.recovery.source_id != self.source_id:
            raise SourceHealthError("source health recovery belongs to another source")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_HEALTH_SCHEMA_VERSION,
            "kind": SOURCE_HEALTH_SOURCE_KIND,
            "health_version": SOURCE_HEALTH_VERSION,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "current_path": self.current_path,
            "current_version": self.current_version,
            "content_hash": self.content_hash,
            "recovery": self.recovery.as_dict() if self.recovery is not None else None,
        }


@dataclass(frozen=True)
class EvidenceHealthRecord:
    """One persisted Evidence record checked against its current source bytes."""

    project_id: str
    evidence_id: str
    source_id: str
    source_version: int
    content_hash: str
    locator: Locator
    expected_excerpt_hash: str
    status: HealthStatus
    reason_code: str
    detail: str
    source_status: HealthStatus
    current_path: str | None
    observed_excerpt_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _health_status(self.status))
        object.__setattr__(self, "source_status", _health_status(self.source_status))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_HEALTH_SCHEMA_VERSION,
            "kind": SOURCE_HEALTH_EVIDENCE_KIND,
            "health_version": SOURCE_HEALTH_VERSION,
            "project_id": self.project_id,
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content_hash": self.content_hash,
            "locator": self.locator.as_dict(),
            "expected_excerpt_hash": self.expected_excerpt_hash,
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "source_status": self.source_status,
            "current_path": self.current_path,
            "observed_excerpt_hash": self.observed_excerpt_hash,
        }


@dataclass(frozen=True)
class ProjectSourceHealthResult:
    """Reconciled D-06 project aggregation over source and Evidence registries."""

    project_id: str
    project_root: Path
    sources_file: Path
    evidence_file: Path
    source_registry_count: int
    evidence_registry_count: int
    sources: tuple[SourceHealthRecord, ...]
    evidence: tuple[EvidenceHealthRecord, ...]

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        evidence = tuple(self.evidence)
        if tuple(sorted(sources, key=lambda item: item.source_id)) != sources:
            raise SourceHealthError("source health records must be sorted by source_id")
        if tuple(sorted(evidence, key=lambda item: item.evidence_id)) != evidence:
            raise SourceHealthError("Evidence health records must be sorted by evidence_id")
        if self.source_registry_count != len(sources):
            raise SourceHealthError(
                "classified source total does not reconcile with source registry"
            )
        if self.evidence_registry_count != len(evidence):
            raise SourceHealthError(
                "classified Evidence total does not reconcile with Evidence registry"
            )
        if len({record.source_id for record in sources}) != len(sources):
            raise SourceHealthError("source health records contain duplicate source IDs")
        if len({record.evidence_id for record in evidence}) != len(evidence):
            raise SourceHealthError("Evidence health records contain duplicate IDs")
        object.__setattr__(self, "project_root", Path(self.project_root).resolve())
        object.__setattr__(self, "sources_file", Path(self.sources_file).resolve())
        object.__setattr__(self, "evidence_file", Path(self.evidence_file).resolve())
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "evidence", evidence)

    @property
    def source_status_counts(self) -> dict[str, int]:
        return _status_counts(self.sources)

    @property
    def evidence_status_counts(self) -> dict[str, int]:
        return _status_counts(self.evidence)

    @property
    def recovery_write_count(self) -> int:
        return sum(
            record.recovery is not None and record.recovery.wrote_registry
            for record in self.sources
        )

    @property
    def overall_status(self) -> HealthStatus:
        statuses = [
            *(record.status for record in self.sources),
            *(record.status for record in self.evidence),
        ]
        if not statuses:
            return "valid"
        return max(statuses, key=_STATUS_SEVERITY.__getitem__)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_HEALTH_SCHEMA_VERSION,
            "kind": SOURCE_HEALTH_REPORT_KIND,
            "health_version": SOURCE_HEALTH_VERSION,
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "sources_file": str(self.sources_file),
            "evidence_file": str(self.evidence_file),
            "overall_status": self.overall_status,
            "source_totals": {
                "registry_count": self.source_registry_count,
                "classified_count": len(self.sources),
                "status_counts": self.source_status_counts,
            },
            "evidence_totals": {
                "registry_count": self.evidence_registry_count,
                "classified_count": len(self.evidence),
                "status_counts": self.evidence_status_counts,
            },
            "recovery_write_count": self.recovery_write_count,
            "sources": [record.as_dict() for record in self.sources],
            "evidence": [record.as_dict() for record in self.evidence],
        }


def _source_health(record: SourceRecord, recovery: SourceRecoveryResult) -> SourceHealthRecord:
    if recovery.status in {"not-needed", "recovered"}:
        status: HealthStatus = "valid"
    elif recovery.status == "ambiguous":
        status = "ambiguous"
    else:
        status = (
            "stale"
            if recovery.current_failure_reason_code == "source-content-hash-mismatch"
            else "missing"
        )
    reason_code = (
        recovery.current_failure_reason_code
        if recovery.status == "unresolved" and recovery.current_failure_reason_code
        else recovery.reason_code
    )
    return SourceHealthRecord(
        project_id=recovery.project_id,
        source_id=record.source_id,
        status=status,
        reason_code=reason_code,
        detail=recovery.detail,
        current_path=recovery.current_path,
        current_version=recovery.current_version,
        content_hash=recovery.content_hash,
        recovery=recovery,
    )


def _evaluate_source(
    workspace_root: str | Path,
    project_id: str,
    record: SourceRecord,
) -> SourceHealthRecord:
    if record.current_version is None or record.current_content_hash is None:
        return SourceHealthRecord(
            project_id=project_id,
            source_id=record.source_id,
            status="stale",
            reason_code="source-version-unrecorded",
            detail="the registered source has no exact current content version",
            current_path=record.current_path,
            current_version=record.current_version,
            content_hash=record.current_content_hash,
        )
    try:
        recovery = recover_source(workspace_root, project_id, record.source_id)
    except SourceRecoveryError as exc:
        raise SourceHealthError(
            f"could not evaluate source health for {record.source_id}: {exc}"
        ) from exc
    return _source_health(record, recovery)


def _evidence_record(
    evidence: Evidence,
    source_health: SourceHealthRecord,
    *,
    status: HealthStatus,
    reason_code: str,
    detail: str,
    current_path: str | None,
    observed_excerpt_hash: str | None = None,
) -> EvidenceHealthRecord:
    return EvidenceHealthRecord(
        project_id=evidence.project_id,
        evidence_id=evidence.evidence_id,
        source_id=evidence.source_id,
        source_version=evidence.source_version,
        content_hash=evidence.content_hash,
        locator=evidence.locator,
        expected_excerpt_hash=evidence.excerpt_hash,
        status=status,
        reason_code=reason_code,
        detail=detail,
        source_status=source_health.status,
        current_path=current_path,
        observed_excerpt_hash=observed_excerpt_hash,
    )


def _evaluate_evidence(
    workspace_root: str | Path,
    project_id: str,
    evidence: Evidence,
    source_health: SourceHealthRecord,
    source_registry: SourceRegistry,
) -> EvidenceHealthRecord:
    if source_health.status != "valid":
        return _evidence_record(
            evidence,
            source_health,
            status=source_health.status,
            reason_code=source_health.reason_code,
            detail=(
                f"Evidence cannot be current because source {evidence.source_id} is "
                f"{source_health.status}: {source_health.detail}"
            ),
            current_path=source_health.current_path,
        )

    binding = validate_evidence(evidence, source_registry)
    if not binding.valid:
        return _evidence_record(
            evidence,
            source_health,
            status="stale",
            reason_code=binding.reason_code,
            detail=binding.reason,
            current_path=source_health.current_path,
        )

    try:
        opened = open_source(
            workspace_root,
            project_id,
            source_id=evidence.source_id,
            locator=evidence.locator,
            expected_content_hash=evidence.content_hash,
            evidence_id=evidence.evidence_id,
            evidence_source_version=evidence.source_version,
        )
    except SourceRelocationAmbiguousError as exc:
        return _evidence_record(
            evidence,
            source_health,
            status="ambiguous",
            reason_code=exc.reason_code,
            detail=str(exc),
            current_path=source_health.current_path,
        )
    except (
        SourceBoundaryError,
        SourceMissingError,
        SourceNotFoundError,
        SourceReadError,
    ) as exc:
        return _evidence_record(
            evidence,
            source_health,
            status="missing",
            reason_code=exc.reason_code,
            detail=str(exc),
            current_path=source_health.current_path,
        )
    except (
        SourceContentMismatchError,
        SourceExcerptMismatchError,
        SourceFormatError,
        SourceLocatorError,
        SourceVersionMismatchError,
    ) as exc:
        return _evidence_record(
            evidence,
            source_health,
            status="stale",
            reason_code=exc.reason_code,
            detail=str(exc),
            current_path=source_health.current_path,
        )
    except SourceAccessError as exc:
        raise SourceHealthError(
            f"could not evaluate Evidence health for {evidence.evidence_id}: {exc}"
        ) from exc

    validation = validate_evidence(
        evidence,
        source_registry,
        observed_content_hash=opened.source.content_hash,
        excerpt=opened.excerpt,
    )
    if not validation.valid:
        return _evidence_record(
            evidence,
            source_health,
            status="stale",
            reason_code=validation.reason_code,
            detail=validation.reason,
            current_path=opened.source.current_path,
            observed_excerpt_hash=opened.excerpt_hash,
        )
    return _evidence_record(
        evidence,
        source_health,
        status="valid",
        reason_code="evidence-current-excerpt-valid",
        detail=(
            "Evidence source version, exact source bytes, locator, and excerpt hash "
            "match the current source"
        ),
        current_path=opened.source.current_path,
        observed_excerpt_hash=opened.excerpt_hash,
    )


def _source_registry_state(
    registry: SourceRegistry,
) -> tuple[tuple[str, str, int | None, str | None], ...]:
    return tuple(
        (
            record.source_id,
            record.current_path,
            record.current_version,
            record.current_content_hash,
        )
        for record in registry.records
    )


def _health_source_state(
    records: Sequence[SourceHealthRecord],
) -> tuple[tuple[str, str, int | None, str | None], ...]:
    return tuple(
        (
            record.source_id,
            record.current_path,
            record.current_version,
            record.content_hash,
        )
        for record in records
    )


def _load_evidence_or_empty(
    workspace_root: str | Path,
    project_id: str,
    evidence_file: Path,
) -> EvidenceRegistry:
    if evidence_file.exists() or evidence_file.is_symlink():
        return load_evidence_registry(workspace_root, project_id)
    return EvidenceRegistry(project_id, evidence_file)


def _source_health_for_evidence(
    evidence: Evidence,
    source_health_by_id: Mapping[str, SourceHealthRecord],
) -> SourceHealthRecord:
    source_health = source_health_by_id.get(evidence.source_id)
    if source_health is None:
        raise SourceHealthError(
            f"Evidence {evidence.evidence_id} references unregistered source "
            f"{evidence.source_id}"
        )
    return source_health


def evaluate_source_health(
    workspace_root: str | Path,
    project_id: str,
) -> ProjectSourceHealthResult:
    """Classify every registered source and persisted Evidence record exactly once."""

    registration = load_registered_project(workspace_root, project_id)
    source_registry = load_source_registry(workspace_root, registration.project_id)
    source_records = tuple(
        _evaluate_source(
            workspace_root,
            registration.project_id,
            record,
        )
        for record in source_registry.records
    )

    current_source_registry = load_source_registry(
        workspace_root,
        registration.project_id,
    )
    if _source_registry_state(current_source_registry) != _health_source_state(
        source_records
    ):
        raise SourceHealthError(
            "source registry changed unexpectedly while health was being evaluated"
        )

    evidence_registry = _load_evidence_or_empty(
        workspace_root,
        registration.project_id,
        registration.layout.evidence_file,
    )
    source_health_by_id = {record.source_id: record for record in source_records}
    evidence_records = tuple(
        _evaluate_evidence(
            workspace_root,
            registration.project_id,
            evidence,
            _source_health_for_evidence(evidence, source_health_by_id),
            current_source_registry,
        )
        for evidence in evidence_registry.records
    )

    final_source_registry = load_source_registry(
        workspace_root,
        registration.project_id,
    )
    if _source_registry_state(final_source_registry) != _health_source_state(
        source_records
    ):
        raise SourceHealthError(
            "source registry changed unexpectedly during Evidence health evaluation"
        )
    final_evidence_registry = _load_evidence_or_empty(
        workspace_root,
        registration.project_id,
        registration.layout.evidence_file,
    )
    if final_evidence_registry.records != evidence_registry.records:
        raise SourceHealthError(
            "Evidence registry changed while health was being evaluated"
        )

    return ProjectSourceHealthResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        sources_file=registration.layout.sources_file,
        evidence_file=registration.layout.evidence_file,
        source_registry_count=len(final_source_registry.records),
        evidence_registry_count=len(final_evidence_registry.records),
        sources=source_records,
        evidence=evidence_records,
    )
