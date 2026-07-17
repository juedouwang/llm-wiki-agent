#!/usr/bin/env python3
"""Host-independent Research Core service facade for project operations.

The facade is the stable Python boundary used by the CLI and host adapters. It
deliberately contains no argument parsing, terminal output, MCP, Hook, Web,
Codex, or Claude Code dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path, PurePosixPath
import re
import unicodedata
from typing import Any, Callable, Iterable

# Support both ``import tools.research_core`` and direct sibling imports.
if __package__:
    from .coverage_report import (
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportResult,
        generate_coverage_report,
    )
    from .extraction_schema import Locator, locator_from_dict
    from .reading_priority import (
        ReadingPriorityResult,
        generate_reading_priority,
    )
    from .host_context import (
        HOST_CONTEXT_DEFAULT_MAX_BYTES,
        HostContextPackResult,
        assemble_host_context_pack,
    )
    from .host_events import (
        DirtyPathQueueResult,
        HostEventSubmitResult,
        load_dirty_path_queue,
        submit_host_event,
    )
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectInventoryResult,
        inventory_project,
    )
    from .project_orchestrator import (
        ProjectRunOrchestrator,
        StageContext,
        StageRunner,
    )
    from .project_reconciliation import (
        ProjectReconciliationResult,
        reconcile_project,
    )
    from .project_registry import (
        ProjectRegistrationResult,
        load_registered_project,
        register_project,
    )
    from .project_runs import ProjectRunResult, StageOutcome
    from .scan_policy import ScanPolicyConfig, ScanPolicyError
    from .source_access import (
        SourceLocatorError,
        SourceNotFoundError,
        SourceOpenResult,
        open_evidence,
        open_source,
    )
else:
    from coverage_report import (  # type: ignore[no-redef]
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportResult,
        generate_coverage_report,
    )
    from extraction_schema import (  # type: ignore[no-redef]
        Locator,
        locator_from_dict,
    )
    from reading_priority import (  # type: ignore[no-redef]
        ReadingPriorityResult,
        generate_reading_priority,
    )
    from host_context import (  # type: ignore[no-redef]
        HOST_CONTEXT_DEFAULT_MAX_BYTES,
        HostContextPackResult,
        assemble_host_context_pack,
    )
    from host_events import (  # type: ignore[no-redef]
        DirtyPathQueueResult,
        HostEventSubmitResult,
        load_dirty_path_queue,
        submit_host_event,
    )
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectInventoryResult,
        inventory_project,
    )
    from project_orchestrator import (  # type: ignore[no-redef]
        ProjectRunOrchestrator,
        StageContext,
        StageRunner,
    )
    from project_reconciliation import (  # type: ignore[no-redef]
        ProjectReconciliationResult,
        reconcile_project,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
        register_project,
    )
    from project_runs import (  # type: ignore[no-redef]
        ProjectRunResult,
        StageOutcome,
    )
    from scan_policy import (  # type: ignore[no-redef]
        ScanPolicyConfig,
        ScanPolicyError,
    )
    from source_access import (  # type: ignore[no-redef]
        SourceLocatorError,
        SourceNotFoundError,
        SourceOpenResult,
        open_evidence,
        open_source,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


HOST_RESULT_SCHEMA_VERSION = 1
PROJECT_CONTEXT_KIND = "llmwiki-project-context"
PROJECT_CONTEXT_VERSION = "project-context-v1"
HOST_COVERAGE_KIND = "llmwiki-host-coverage"
HOST_COVERAGE_VERSION = "host-coverage-v1"
HOST_SOURCE_LOCATION_KIND = "llmwiki-host-source-location"
HOST_SOURCE_LOCATION_VERSION = "host-source-location-v1"
HOST_SOURCE_OPEN_KIND = "llmwiki-host-source-open"
HOST_SOURCE_OPEN_VERSION = "host-source-open-v1"
_PROJECT_UNDERSTAND_THROUGH_STAGE = "classify"

_COVERAGE_AXES = (
    "research_role",
    "processing_status",
    "read_depth",
    "reason",
)
_STABLE_CODE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")


def _exact_mapping(
    value: object,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} does not match the host-safe contract")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_integer(value: object, *, label: str) -> int:
    result = _nonnegative_integer(value, label=label)
    if result < 1:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _nonempty_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _stable_code(value: object, *, label: str) -> str:
    result = _nonempty_text(value, label=label)
    if _STABLE_CODE_PATTERN.fullmatch(result) is None:
        raise ValueError(f"{label} must be a stable code")
    return result


def _relative_project_path(value: object, *, label: str) -> str:
    result = _nonempty_text(value, label=label)
    path = PurePosixPath(result)
    if (
        "\\" in result
        or path.is_absolute()
        or result != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", result) != result
    ):
        raise ValueError(f"{label} must be a normalized project-relative path")
    return result


def _coverage_bucket(value: object, *, include_details: bool) -> dict[str, Any]:
    required = {"file_count", "byte_count"}
    if include_details:
        required.add("details")
    bucket = _exact_mapping(value, required, label="coverage bucket")
    result: dict[str, Any] = {
        "file_count": _nonnegative_integer(
            bucket["file_count"],
            label="coverage bucket file_count",
        ),
        "byte_count": _nonnegative_integer(
            bucket["byte_count"],
            label="coverage bucket byte_count",
        ),
    }
    if include_details:
        details = bucket["details"]
        if not isinstance(details, list) or any(
            not isinstance(item, str) or not item for item in details
        ):
            raise ValueError("coverage reason details must be non-empty strings")
        result["details"] = list(details)
    return result


def _coverage_axis(
    value: object,
    *,
    include_details: bool,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("coverage axis must be an object")
    result: dict[str, dict[str, Any]] = {}
    for raw_key, raw_bucket in value.items():
        key = _stable_code(raw_key, label="coverage axis key")
        result[key] = _coverage_bucket(raw_bucket, include_details=include_details)
    return result


def _coverage_report_for_host(
    value: object,
    *,
    project_id: str,
) -> dict[str, Any]:
    report = _exact_mapping(
        value,
        {
            "schema_version",
            "kind",
            "report_version",
            "project_id",
            "manifest",
            "totals",
            "coverage",
            "failures",
            "reconciliation",
        },
        label="coverage report",
    )
    if report["schema_version"] != COVERAGE_REPORT_SCHEMA_VERSION:
        raise ValueError("coverage report schema_version is unsupported")
    if report["kind"] != COVERAGE_REPORT_KIND:
        raise ValueError("coverage report kind is invalid")
    if report["report_version"] != COVERAGE_REPORT_VERSION:
        raise ValueError("coverage report version is unsupported")
    if report["project_id"] != project_id:
        raise ValueError("coverage report project identity is inconsistent")

    manifest = _exact_mapping(
        report["manifest"],
        {"manifest_version", "scan_generation"},
        label="coverage manifest summary",
    )
    totals = _exact_mapping(
        report["totals"],
        {"file_count", "byte_count", "failed_file_count"},
        label="coverage totals",
    )
    raw_coverage = _exact_mapping(
        report["coverage"],
        set(_COVERAGE_AXES),
        label="coverage dimensions",
    )
    raw_failures = report["failures"]
    if not isinstance(raw_failures, list):
        raise ValueError("coverage failures must be an array")
    failures: list[dict[str, Any]] = []
    failure_fields = {
        "path",
        "byte_count",
        "research_role",
        "processing_status",
        "read_depth",
        "reason_code",
        "reason",
    }
    for raw_failure in raw_failures:
        failure = _exact_mapping(
            raw_failure,
            failure_fields,
            label="coverage failure",
        )
        if failure["processing_status"] != "failed":
            raise ValueError("coverage failure processing_status must be failed")
        failures.append(
            {
                "path": _relative_project_path(
                    failure["path"],
                    label="coverage failure path",
                ),
                "byte_count": _nonnegative_integer(
                    failure["byte_count"],
                    label="coverage failure byte_count",
                ),
                "research_role": _stable_code(
                    failure["research_role"],
                    label="coverage failure research_role",
                ),
                "processing_status": "failed",
                "read_depth": _stable_code(
                    failure["read_depth"],
                    label="coverage failure read_depth",
                ),
                "reason_code": _stable_code(
                    failure["reason_code"],
                    label="coverage failure reason_code",
                ),
                "reason": _nonempty_text(
                    failure["reason"],
                    label="coverage failure reason",
                ),
            }
        )

    reconciliation = _exact_mapping(
        report["reconciliation"],
        {"manifest", "axes"},
        label="coverage reconciliation",
    )
    reconciliation_axes = _exact_mapping(
        reconciliation["axes"],
        set(_COVERAGE_AXES),
        label="coverage reconciliation axes",
    )
    return {
        "schema_version": COVERAGE_REPORT_SCHEMA_VERSION,
        "kind": COVERAGE_REPORT_KIND,
        "report_version": COVERAGE_REPORT_VERSION,
        "project_id": project_id,
        "manifest": {
            "manifest_version": _stable_code(
                manifest["manifest_version"],
                label="manifest version",
            ),
            "scan_generation": _positive_integer(
                manifest["scan_generation"],
                label="scan generation",
            ),
        },
        "totals": {
            "file_count": _nonnegative_integer(
                totals["file_count"],
                label="coverage total file_count",
            ),
            "byte_count": _nonnegative_integer(
                totals["byte_count"],
                label="coverage total byte_count",
            ),
            "failed_file_count": _nonnegative_integer(
                totals["failed_file_count"],
                label="coverage failed_file_count",
            ),
        },
        "coverage": {
            axis: _coverage_axis(
                raw_coverage[axis],
                include_details=axis == "reason",
            )
            for axis in _COVERAGE_AXES
        },
        "failures": failures,
        "reconciliation": {
            "manifest": _coverage_bucket(
                reconciliation["manifest"],
                include_details=False,
            ),
            "axes": {
                axis: _coverage_bucket(
                    reconciliation_axes[axis],
                    include_details=False,
                )
                for axis in _COVERAGE_AXES
            },
        },
    }


def _pattern_tuple(patterns: Iterable[str] | None) -> tuple[str, ...]:
    """Materialize one policy-pattern iterable without splitting a string."""

    if patterns is None:
        return ()
    if isinstance(patterns, str):
        return (patterns,)
    return tuple(patterns)


@dataclass(frozen=True)
class ProjectContextResult:
    """Path-free trusted project identity and onboarding context for hosts."""

    project_id: str
    name: str
    identity_strategy: str
    registered_at: str
    git_available: bool
    git_is_repository: bool
    git_branch: str | None
    git_head_commit: str | None
    final_goal: str | None
    current_stage: str | None
    important_question: str | None
    deadline: str | None
    daily_available_hours: float | None

    def as_dict(self) -> dict[str, Any]:
        """Return a stable host-safe Schema v1 payload."""

        return {
            "schema_version": HOST_RESULT_SCHEMA_VERSION,
            "kind": PROJECT_CONTEXT_KIND,
            "context_version": PROJECT_CONTEXT_VERSION,
            "project_id": self.project_id,
            "name": self.name,
            "identity_strategy": self.identity_strategy,
            "registered_at": self.registered_at,
            "git": {
                "available": self.git_available,
                "is_repository": self.git_is_repository,
                "branch": self.git_branch,
                "head_commit": self.git_head_commit,
            },
            "onboarding": {
                "final_goal": self.final_goal,
                "current_stage": self.current_stage,
                "important_question": self.important_question,
                "deadline": self.deadline,
                "daily_available_hours": self.daily_available_hours,
            },
        }


@dataclass(frozen=True)
class HostCoverageResult:
    """Path-free host view of a persisted deterministic coverage report."""

    project_id: str
    report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return report data without Manifest or storage paths."""

        return {
            "schema_version": HOST_RESULT_SCHEMA_VERSION,
            "kind": HOST_COVERAGE_KIND,
            "view_version": HOST_COVERAGE_VERSION,
            "project_id": self.project_id,
            "report_persisted": True,
            "report": _coverage_report_for_host(
                self.report,
                project_id=self.project_id,
            ),
        }


@dataclass(frozen=True)
class HostSourceOpenResult:
    """Path-free host view of one policy-authorized exact source excerpt."""

    opened: SourceOpenResult

    def as_dict(self) -> dict[str, Any]:
        """Return exact source identity and content without absolute paths."""

        source = self.opened.source
        return {
            "schema_version": HOST_RESULT_SCHEMA_VERSION,
            "kind": HOST_SOURCE_OPEN_KIND,
            "view_version": HOST_SOURCE_OPEN_VERSION,
            "source": {
                "schema_version": HOST_RESULT_SCHEMA_VERSION,
                "kind": HOST_SOURCE_LOCATION_KIND,
                "view_version": HOST_SOURCE_LOCATION_VERSION,
                "project_id": source.project_id,
                "source_id": source.source_id,
                "current_path": _relative_project_path(
                    source.current_path,
                    label="source current_path",
                ),
                "current_version": source.current_version,
                "content_hash": source.content_hash,
            },
            "locator": locator_from_dict(
                self.opened.locator.as_dict()
            ).as_dict(),
            "excerpt": self.opened.excerpt,
            "excerpt_hash": self.opened.excerpt_hash,
            "excerpt_encoding": "utf-8",
            "excerpt_format": self.opened.excerpt_format,
            "content_hash_verified": True,
            "excerpt_hash_verified": self.opened.excerpt_hash_verified,
            "evidence_id": self.opened.evidence_id,
            "evidence_source_version": self.opened.evidence_source_version,
        }


@dataclass(frozen=True)
class ResearchCoreService:
    """Stable, host-neutral entry point for deterministic Research Core work."""

    workspace_root: Path

    def __init__(self, workspace_root: str | Path) -> None:
        normalized = Path(workspace_root).expanduser().resolve()
        object.__setattr__(self, "workspace_root", normalized)

    def register(
        self,
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
        """Register one source-read-only project without scanning it."""

        return register_project(
            workspace_root=self.workspace_root,
            project_root=project_root,
            project_id=project_id,
            name=name,
            knowledge_root=knowledge_root,
            final_goal=final_goal,
            current_stage=current_stage,
            important_question=important_question,
            deadline=deadline,
            daily_available_hours=daily_available_hours,
        )

    def project_context(self, project_id: str) -> ProjectContextResult:
        """Load trusted project identity without exposing local storage paths."""

        registration = load_registered_project(
            workspace_root=self.workspace_root,
            project_id=project_id,
        )
        record = registration.record
        git = record["git"]
        onboarding = record["onboarding"]
        return ProjectContextResult(
            project_id=registration.project_id,
            name=record["name"],
            identity_strategy=record["identity_strategy"],
            registered_at=record["registered_at"],
            git_available=git["available"],
            git_is_repository=git["is_repository"],
            git_branch=git["branch"],
            git_head_commit=git["head_commit"],
            final_goal=onboarding["final_goal"],
            current_stage=onboarding["current_stage"],
            important_question=onboarding["important_question"],
            deadline=onboarding["deadline"],
            daily_available_hours=onboarding["daily_available_hours"],
        )

    def host_event_submit(
        self,
        project_id: str,
        *,
        event_id: str,
        producer: str,
        occurred_at: str | datetime,
        operation: str,
        paths: Iterable[str | Path] | str | Path,
        clock: Callable[[], datetime] | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> HostEventSubmitResult:
        """Record one host-neutral dirty-path signal without reconciliation."""

        return submit_host_event(
            self.workspace_root,
            project_id,
            event_id=event_id,
            producer=producer,
            occurred_at=occurred_at,
            operation=operation,
            paths=paths,
            clock=clock,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def dirty_path_queue(
        self,
        project_id: str,
        *,
        lock_timeout_seconds: float = 5.0,
    ) -> DirtyPathQueueResult:
        """Load or repair the deterministic queue derived from host events."""

        return load_dirty_path_queue(
            self.workspace_root,
            project_id,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def project_reconcile(
        self,
        project_id: str,
        *,
        dirty_paths: Iterable[str | Path] | str | Path = (),
        clock: Callable[[], datetime] | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> ProjectReconciliationResult:
        """Full-scan reconciliation that treats host paths only as hints."""

        return reconcile_project(
            self.workspace_root,
            project_id,
            dirty_paths=dirty_paths,
            run_factory=self.project_run_start,
            coverage_factory=self.coverage,
            clock=clock,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def _project_run_stage_runners(self) -> dict[str, StageRunner]:
        return {
            "register": self._run_registration_stage,
            "inventory": self._run_inventory_stage,
            "classify": self._run_classification_stage,
        }

    def _run_registration_stage(self, context: StageContext) -> StageOutcome:
        project = self.project_context(context.project_id)
        return StageOutcome.succeeded(
            input_versions={"project_context_version": PROJECT_CONTEXT_VERSION},
            artifacts=(
                {
                    "artifact_type": "project-registration",
                    "artifact_id": project.project_id,
                    "relative_path": "project.yaml",
                    "content_hash": None,
                },
            ),
        )

    def _run_inventory_stage(self, context: StageContext) -> StageOutcome:
        inventory = self.scan(context.project_id)
        manifest_sha256 = _sha256_file(inventory.manifest_file)
        return StageOutcome.succeeded(
            input_versions={"inventory_contract": PROJECT_MANIFEST_VERSION},
            artifacts=(
                {
                    "artifact_type": "project-manifest",
                    "artifact_id": f"manifest-generation-{inventory.scan_generation}",
                    "relative_path": "manifest.jsonl",
                    "content_hash": manifest_sha256,
                },
            ),
        )

    def _run_classification_stage(self, context: StageContext) -> StageOutcome:
        coverage = self.coverage(context.project_id)
        manifest = coverage.report["manifest"]
        coverage_sha256 = _sha256_file(coverage.report_file)
        return StageOutcome.succeeded(
            input_versions={
                "manifest_version": manifest["manifest_version"],
                "manifest_scan_generation": manifest["scan_generation"],
                "coverage_report_version": COVERAGE_REPORT_VERSION,
            },
            artifacts=(
                {
                    "artifact_type": "classification-coverage",
                    "artifact_id": (
                        f"coverage-generation-{manifest['scan_generation']}"
                    ),
                    "relative_path": "indexes/coverage-report.json",
                    "content_hash": coverage_sha256,
                },
            ),
        )

    def project_understand(
        self,
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
        resume_run_id: str | None = None,
    ) -> ProjectRunResult:
        """Register or reuse a project, then run the deterministic prefix.

        The initial E-08 R2 slice intentionally stops after ``classify``.
        Later extraction, synthesis, planning, and rendering stages remain
        pending until their owning roadmap tasks install real handlers.
        """

        registration = self.register(
            project_root=project_root,
            project_id=project_id,
            name=name,
            knowledge_root=knowledge_root,
            final_goal=final_goal,
            current_stage=current_stage,
            important_question=important_question,
            deadline=deadline,
            daily_available_hours=daily_available_hours,
        )
        if resume_run_id is not None:
            return self.project_run_resume(
                registration.project_id,
                resume_run_id,
                through_stage=_PROJECT_UNDERSTAND_THROUGH_STAGE,
            )
        return self.project_run_start(
            registration.project_id,
            through_stage=_PROJECT_UNDERSTAND_THROUGH_STAGE,
        )

    def project_run_start(
        self,
        project_id: str,
        *,
        through_stage: str | None = None,
    ) -> ProjectRunResult:
        """Start a persisted run using currently available Core stages."""

        return ProjectRunOrchestrator(
            self.workspace_root,
            runners=self._project_run_stage_runners(),
        ).start(project_id, through_stage=through_stage)

    def project_run_resume(
        self,
        project_id: str,
        run_id: str,
        *,
        through_stage: str | None = None,
    ) -> ProjectRunResult:
        """Resume one run while preserving successful stage checkpoints."""

        return ProjectRunOrchestrator(
            self.workspace_root,
            runners=self._project_run_stage_runners(),
        ).resume(project_id, run_id, through_stage=through_stage)

    def project_run_status(
        self,
        project_id: str,
        run_id: str,
    ) -> ProjectRunResult:
        """Load one persisted run report without executing any stage."""

        return ProjectRunOrchestrator(self.workspace_root).status(project_id, run_id)

    def scan(
        self,
        project_id: str,
        *,
        policy_config: ScanPolicyConfig | None = None,
        include_patterns: Iterable[str] | None = None,
        exclude_patterns: Iterable[str] | None = None,
        follow_symlinks: bool | None = None,
    ) -> ProjectInventoryResult:
        """Inventory one project with a complete or CLI-convenience policy."""

        convenience_values = (
            include_patterns,
            exclude_patterns,
            follow_symlinks,
        )
        if policy_config is not None and any(
            value is not None for value in convenience_values
        ):
            raise ScanPolicyError(
                "policy_config cannot be combined with include_patterns, "
                "exclude_patterns, or follow_symlinks"
            )
        effective_policy = policy_config or ScanPolicyConfig(
            include_patterns=_pattern_tuple(include_patterns),
            exclude_patterns=_pattern_tuple(exclude_patterns),
            follow_symlinks=False if follow_symlinks is None else follow_symlinks,
        )
        return inventory_project(
            workspace_root=self.workspace_root,
            project_id=project_id,
            policy_config=effective_policy,
        )

    def coverage(self, project_id: str) -> CoverageReportResult:
        """Generate and persist coverage from the current Manifest."""

        return generate_coverage_report(
            workspace_root=self.workspace_root,
            project_id=project_id,
        )

    def prioritize(self, project_id: str) -> ReadingPriorityResult:
        """Generate deterministic reading priority from the current Manifest."""

        return generate_reading_priority(
            workspace_root=self.workspace_root,
            project_id=project_id,
        )

    def coverage_view(self, project_id: str) -> HostCoverageResult:
        """Generate coverage and return its path-free host representation."""

        coverage = self.coverage(project_id)
        return HostCoverageResult(
            project_id=coverage.project_id,
            report=coverage.report,
        )

    def host_context_pack(
        self,
        project_id: str,
        *,
        max_bytes: int = HOST_CONTEXT_DEFAULT_MAX_BYTES,
    ) -> HostContextPackResult:
        """Assemble a deterministic, path-free, budget-bounded host context."""

        return assemble_host_context_pack(
            self.workspace_root,
            project_id,
            project_context=self.project_context(project_id).as_dict(),
            coverage_view=self.coverage_view(project_id).as_dict(),
            max_bytes=max_bytes,
        )

    def source_open(
        self,
        project_id: str,
        target_id: str,
        *,
        locator: Locator | dict[str, Any] | None = None,
        expected_content_hash: str | None = None,
        expected_excerpt_hash: str | None = None,
        enforce_content_policy: bool = False,
    ) -> SourceOpenResult:
        """Reopen a current Source locator or a persisted Evidence target."""

        if not isinstance(target_id, str):
            raise SourceNotFoundError(
                "target_id must be a Core source ID (src-*) or Evidence ID (evd-*)"
            )

        if target_id.startswith("evd-"):
            if (
                locator is not None
                or expected_content_hash is not None
                or expected_excerpt_hash is not None
            ):
                raise SourceLocatorError(
                    "Evidence targets use their persisted locator and hashes; "
                    "source-only overrides are not accepted"
                )
            return open_evidence(
                workspace_root=self.workspace_root,
                project_id=project_id,
                evidence_id=target_id,
                enforce_content_policy=enforce_content_policy,
            )

        if target_id.startswith("src-"):
            if locator is None:
                raise SourceLocatorError(
                    "locator is required when target_id is a source ID"
                )
            return open_source(
                workspace_root=self.workspace_root,
                project_id=project_id,
                source_id=target_id,
                locator=locator,
                expected_content_hash=expected_content_hash,
                expected_excerpt_hash=expected_excerpt_hash,
                enforce_content_policy=enforce_content_policy,
            )

        raise SourceNotFoundError(
            "target_id must be a Core source ID (src-*) or Evidence ID (evd-*)"
        )

    def source_open_view(
        self,
        project_id: str,
        target_id: str,
        *,
        locator: Locator | dict[str, Any] | None = None,
        expected_content_hash: str | None = None,
        expected_excerpt_hash: str | None = None,
    ) -> HostSourceOpenResult:
        """Open through Manifest policy and return a path-free host view."""

        opened = self.source_open(
            project_id,
            target_id,
            locator=locator,
            expected_content_hash=expected_content_hash,
            expected_excerpt_hash=expected_excerpt_hash,
            enforce_content_policy=True,
        )
        return HostSourceOpenResult(opened=opened)
