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
from typing import Any, Callable, Iterable, Mapping

# Support both ``import tools.research_core`` and direct sibling imports.
if __package__:
    from .controlled_markdown import ControlledMarkdownUpdatePlan
    from .controlled_markdown_persistence import (
        ControlledMarkdownWriteAuthorization,
        TrustedHostSessionContext,
    )
    from .coverage_report import (
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportResult,
        generate_coverage_report,
    )
    from .extraction_schema import Locator, locator_from_dict
    from .reading_priority import (
        READING_PRIORITY_VERSION,
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
    from .project_layout import LayoutError
    from .project_inventory import (
        PROJECT_MANIFEST_VERSION,
        ProjectInventoryResult,
        inventory_project,
    )
    from .hierarchical_understanding import (
        ChunkObservation,
        HierarchicalUnderstandingResult,
        generate_hierarchical_understanding,
    )
    from .execution_flow import (
        ExecutionFlowResult,
        FlowEdgeObservation,
        FlowNodeObservation,
        generate_execution_flow,
    )
    from .research_linkage import (
        ResearchEntityObservation,
        ResearchLinkageResult,
        ResearchRelationObservation,
        generate_research_linkage,
    )
    from .experiment_chains import (
        ClaimLinkObservation,
        ClaimObservation,
        ConfigObservation,
        ExperimentChainsResult,
        ResultObservation,
        RunObservation,
        generate_experiment_chains,
    )
    from .knowledge_renderer import KnowledgeRenderingResult
    from .project_map import ProjectMapResult, generate_project_map
    from .project_understand import (
        write_json_artifact,
        write_stage_report,
        write_static_web_index,
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
    from .research_planning import InitialPlanningResult, generate_initial_plan
    from .research_state import generate_project_state
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
        READING_PRIORITY_VERSION,
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
    from project_layout import LayoutError  # type: ignore[no-redef]
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        ProjectInventoryResult,
        inventory_project,
    )
    from hierarchical_understanding import (  # type: ignore[no-redef]
        ChunkObservation,
        HierarchicalUnderstandingResult,
        generate_hierarchical_understanding,
    )
    from execution_flow import (  # type: ignore[no-redef]
        ExecutionFlowResult,
        FlowEdgeObservation,
        FlowNodeObservation,
        generate_execution_flow,
    )
    from research_linkage import (  # type: ignore[no-redef]
        ResearchEntityObservation,
        ResearchLinkageResult,
        ResearchRelationObservation,
        generate_research_linkage,
    )
    from experiment_chains import (  # type: ignore[no-redef]
        ClaimLinkObservation,
        ClaimObservation,
        ConfigObservation,
        ExperimentChainsResult,
        ResultObservation,
        RunObservation,
        generate_experiment_chains,
    )
    from project_map import (  # type: ignore[no-redef]
        ProjectMapResult,
        generate_project_map,
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
    from research_planning import (  # type: ignore[no-redef]
        InitialPlanningResult,
        generate_initial_plan,
    )
    from research_state import generate_project_state  # type: ignore[no-redef]
    from project_understand import (  # type: ignore[no-redef]
        write_json_artifact,
        write_stage_report,
        write_static_web_index,
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
    # E-07 imports are intentionally lazy in sibling-script mode.
    ControlledMarkdownUpdatePlan = Any
    ControlledMarkdownWriteAuthorization = Any
    TrustedHostSessionContext = Any
    KnowledgeRenderingResult = Any


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

    def _project_run_stage_runners(
        self,
        *,
        complete_pipeline: bool = False,
    ) -> dict[str, StageRunner]:
        runners: dict[str, StageRunner] = {
            "register": self._run_registration_stage,
            "inventory": self._run_inventory_stage,
            "classify": self._run_classification_stage,
        }
        if complete_pipeline:
            runners.update(
                {
                    "extract": self._run_extract_stage,
                    "adaptive-read": self._run_adaptive_read_stage,
                    "synthesize": self._run_synthesize_stage,
                    "evidence": self._run_evidence_stage,
                    "status": self._run_status_stage,
                    "plan": self._run_plan_stage,
                    "index": self._run_index_stage,
                    "web-render": self._run_web_render_stage,
                }
            )
        return runners

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

    def _run_machine_file_artifact(
        self,
        project_id: str,
        path: Path,
        *,
        artifact_type: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        """Describe one current machine artifact without leaking an absolute path."""

        registration = load_registered_project(self.workspace_root, project_id)
        machine_root = registration.layout.machine_root.resolve()
        target = path.expanduser().resolve()
        try:
            relative_path = target.relative_to(machine_root).as_posix()
        except ValueError as exc:
            raise LayoutError("stage artifact escaped the project machine root") from exc
        return {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "content_hash": _sha256_file(target),
        }

    def _run_stage_report(
        self,
        context: StageContext,
        *,
        summary: str,
        status: str = "complete",
        reason_code: str = "deterministic-local-analysis",
        inputs: Iterable[str] = (),
        outputs: Iterable[str] = (),
        gaps: Iterable[str] = (),
    ) -> dict[str, Any]:
        report = write_stage_report(
            self.workspace_root,
            context.project_id,
            context.run_id,
            context.stage_id,
            summary=summary,
            status=status,
            reason_code=reason_code,
            inputs=inputs,
            outputs=outputs,
            gaps=gaps,
            created_at=context.run.get("updated_at"),
        )
        return report.as_run_artifact()

    def _run_extract_stage(self, context: StageContext) -> StageOutcome:
        """Build a deterministic structure map; do not pretend to read semantics."""

        project_map = self.project_map(context.project_id)
        report = self._run_stage_report(
            context,
            summary="Manifest-grounded project structure was extracted locally.",
            reason_code="manifest-grounded-structure",
            inputs=("current-project-manifest", "classified-file-state"),
            outputs=("project-map",),
            gaps=("semantic-source-observations-not-provided",),
        )
        map_artifact = self._run_machine_file_artifact(
            context.project_id,
            project_map.project_map_file,
            artifact_type="project-map",
            artifact_id="project-map-v1",
        )
        return StageOutcome.succeeded(
            input_versions={"project_map_version": "project-map-v1"},
            artifacts=(report, map_artifact),
        )

    def _run_adaptive_read_stage(self, context: StageContext) -> StageOutcome:
        """Publish bounded priority and hierarchy artifacts without LLM reading."""

        priority = self.prioritize(context.project_id)
        hierarchical = self.hierarchical_understanding(context.project_id)
        report = self._run_stage_report(
            context,
            summary="Reading priority and hierarchical metadata were generated locally.",
            status="draft",
            reason_code="metadata-only-adaptive-read",
            inputs=("current-project-manifest", "reading-priority-rules"),
            outputs=("reading-priority", "hierarchical-understanding"),
            gaps=("selected-files-not-semantically-read",),
        )
        priority_artifact = self._run_machine_file_artifact(
            context.project_id,
            priority.priority_file,
            artifact_type="reading-priority",
            artifact_id="reading-priority-v1",
        )
        hierarchy_artifact = self._run_machine_file_artifact(
            context.project_id,
            hierarchical.understanding_file,
            artifact_type="hierarchical-understanding",
            artifact_id="hierarchical-understanding-v1",
        )
        return StageOutcome.succeeded(
            input_versions={"reading_priority_version": READING_PRIORITY_VERSION},
            artifacts=(report, priority_artifact, hierarchy_artifact),
        )

    def _run_synthesize_stage(self, context: StageContext) -> StageOutcome:
        """Generate all deterministic synthesis candidates with explicit uncertainty."""

        execution = self.execution_flow(context.project_id)
        linkage = self.research_linkage(context.project_id)
        chains = self.experiment_chains(context.project_id)
        report = self._run_stage_report(
            context,
            summary="Execution, provenance, and experiment-chain candidates were synthesized locally.",
            status="draft",
            reason_code="deterministic-candidate-synthesis",
            inputs=("project-map", "hierarchical-understanding", "classified-file-state"),
            outputs=("execution-flow", "research-linkage", "experiment-chains"),
            gaps=("host-semantic-observations-not-provided",),
        )
        artifacts = [
            report,
            self._run_machine_file_artifact(
                context.project_id,
                execution.execution_flow_file,
                artifact_type="execution-flow",
                artifact_id="execution-flow-v1",
            ),
            self._run_machine_file_artifact(
                context.project_id,
                linkage.research_linkage_file,
                artifact_type="research-linkage",
                artifact_id="research-linkage-v2",
            ),
            self._run_machine_file_artifact(
                context.project_id,
                chains.experiment_chains_file,
                artifact_type="experiment-chains",
                artifact_id="experiment-chains-v1",
            ),
        ]
        return StageOutcome.succeeded(
            input_versions={"synthesis_mode": "deterministic-local"},
            artifacts=tuple(artifacts),
        )

    def _run_evidence_stage(self, context: StageContext) -> StageOutcome:
        """Record an honest Evidence readiness boundary without inventing Evidence."""

        report = self._run_stage_report(
            context,
            summary="Evidence readiness was recorded; no host Evidence observations were supplied.",
            status="draft",
            reason_code="no-host-evidence-observations",
            inputs=("current-manifest", "deterministic-synthesis-artifacts"),
            outputs=("evidence-readiness",),
            gaps=("source-locators-and-excerpts-require-host-observations",),
        )
        readiness = write_json_artifact(
            self.workspace_root,
            context.project_id,
            context.run_id,
            "evidence-readiness.json",
            {
                "kind": "llmwiki-project-understand-evidence-readiness",
                "artifact_version": "evidence-readiness-v1",
                "project_id": context.project_id,
                "run_id": context.run_id,
                "status": "draft",
                "reason_code": "no-host-evidence-observations",
                "evidence_ids": [],
                "source_bytes_read": False,
                "external_send": False,
            },
            artifact_type="evidence-readiness",
            artifact_id=f"{context.run_id}:evidence-readiness",
        )
        return StageOutcome.succeeded(
            input_versions={"evidence_mode": "metadata-only"},
            artifacts=(report, readiness.as_run_artifact()),
        )

    def _run_status_stage(self, context: StageContext) -> StageOutcome:
        """Publish the strict current I-03 state plus a bounded run summary."""

        generated_at = context.run.get("created_at")
        if not isinstance(generated_at, str):
            raise LayoutError("run has no canonical creation timestamp")
        state_result = generate_project_state(
            self.workspace_root,
            context.project_id,
            generated_at=generated_at,
        )
        state = state_result.state
        report = self._run_stage_report(
            context,
            summary=(
                "The strict current project-state snapshot was rebuilt from "
                "current machine and Knowledge Schema inputs."
            ),
            status="draft",
            reason_code="current-project-state-snapshot",
            inputs=("current-registration", "current-manifest", "current-machine-artifacts"),
            outputs=("project-state", "status-summary"),
            gaps=(item["code"] for item in state.gaps),
        )
        status_artifact = write_json_artifact(
            self.workspace_root,
            context.project_id,
            context.run_id,
            "status-summary.json",
            {
                "kind": "llmwiki-project-understand-status-summary",
                "artifact_version": "status-summary-v2",
                "project_id": context.project_id,
                "run_id": context.run_id,
                "status": "draft",
                "reason_code": "current-project-state-snapshot",
                "project_state_artifact_id": state.artifact_id,
                "counts": {
                    "experiments": state.experiments["total"],
                    "results": state.results["total"],
                    "open_questions": state.open_questions["total"],
                    "blockers": state.blockers["total"],
                    "stale_knowledge": state.stale_knowledge["total"],
                    "stale_evidence": state.stale_evidence["total"],
                    "recent_changes": state.recent_changes["total"],
                },
                "gap_codes": [item["code"] for item in state.gaps],
            },
            artifact_type="status-summary",
            artifact_id=f"{context.run_id}:status-summary",
        )
        state_artifact = self._run_machine_file_artifact(
            context.project_id,
            state_result.project_state_file,
            artifact_type="project-state",
            artifact_id=state.artifact_id,
        )
        return StageOutcome.succeeded(
            input_versions={"project_state_version": "project-state-v1"},
            artifacts=(report, state_artifact, status_artifact.as_run_artifact()),
        )

    def _run_plan_stage(self, context: StageContext) -> StageOutcome:
        """Generate strict I-04 DRAFT Goal, backlog, and daily-plan state."""

        generated_at = context.run.get("created_at")
        if not isinstance(generated_at, str):
            raise LayoutError("run has no canonical creation timestamp")
        planning_timestamp = (
            datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        planning = self.plan(
            context.project_id,
            generated_at=planning_timestamp,
            plan_date=planning_timestamp[:10],
        )
        report = self._run_stage_report(
            context,
            summary=(
                "A strict DRAFT Goal, backlog, and daily plan were bound to "
                "the current project-state snapshot."
            ),
            status="draft",
            reason_code="current-initial-plan-draft",
            inputs=("project-state", "onboarding-context", "current-goal-and-tasks"),
            outputs=("goal", "backlog", "daily-plan"),
            gaps=("user-confirmation-required",),
        )
        plan_artifact = write_json_artifact(
            self.workspace_root,
            context.project_id,
            context.run_id,
            "initial-planning.json",
            planning.as_dict(),
            artifact_type="initial-planning-result",
            artifact_id=f"{context.run_id}:initial-planning",
        )
        registration = load_registered_project(
            self.workspace_root,
            context.project_id,
        )
        machine_artifacts = (
            self._run_machine_file_artifact(
                context.project_id,
                registration.layout.goals_file,
                artifact_type="research-goal",
                artifact_id=planning.goal.goal_id,
            ),
            self._run_machine_file_artifact(
                context.project_id,
                registration.layout.tasks_file,
                artifact_type="research-tasks",
                artifact_id="research-tasks-v1",
            ),
            self._run_machine_file_artifact(
                context.project_id,
                registration.layout.project_state_file,
                artifact_type="project-state",
                artifact_id=planning.initial_plan.state_artifact_id,
            ),
            self._run_machine_file_artifact(
                context.project_id,
                registration.layout.initial_plan_file,
                artifact_type="initial-plan",
                artifact_id=(
                    f"initial-plan-{planning.initial_plan.plan_date}"
                ),
            ),
        )
        return StageOutcome.succeeded(
            input_versions={"initial_plan_version": "initial-plan-v1"},
            artifacts=(report, *machine_artifacts, plan_artifact.as_run_artifact()),
        )

    def _run_index_stage(self, context: StageContext) -> StageOutcome:
        """Render the complete Knowledge Schema package through the controlled writer."""

        authorized_at = context.run.get("created_at")
        if not isinstance(authorized_at, str):
            raise LayoutError("run has no canonical creation timestamp")
        host_context = TrustedHostSessionContext(
            host_id="llmwiki-core",
            actor_type="host-agent",
            actor_id="project-understand",
            session_id=context.run_id,
        )
        rendered = self.knowledge_render(
            context.project_id,
            rendered_at=authorized_at,
            plan_date=authorized_at,
            persist=True,
            host_context=host_context,
            decision_id_prefix=f"e08-{context.run_id}-{context.attempt}",
            authorized_at=authorized_at,
        )
        report_status = "draft" if rendered.status == "partial" else "complete"
        gaps = ("protected-knowledge-pages",) if rendered.status == "partial" else ()
        report = self._run_stage_report(
            context,
            summary="The fifteen product entries and navigation index were rendered.",
            status=report_status,
            reason_code="controlled-knowledge-render",
            inputs=("project-map", "hierarchical-understanding", "execution-flow", "research-linkage", "experiment-chains"),
            outputs=("fifteen-knowledge-products", "unified-index", "daily-plan"),
            gaps=gaps,
        )
        rendering_artifact = write_json_artifact(
            self.workspace_root,
            context.project_id,
            context.run_id,
            "knowledge-render.json",
            rendered.as_dict(),
            artifact_type="knowledge-render-report",
            artifact_id=f"{context.run_id}:knowledge-render",
        )
        artifacts = (report, rendering_artifact.as_run_artifact())
        if rendered.status == "failed":
            return StageOutcome.failed(
                "knowledge-render-failed",
                "Controlled Knowledge Schema rendering reported failures.",
                retryable=True,
                artifacts=artifacts,
            )
        return StageOutcome.succeeded(
            input_versions={"renderer_version": rendered.renderer_version},
            artifacts=artifacts,
        )

    def _run_web_render_stage(self, context: StageContext) -> StageOutcome:
        """Publish a self-contained read-only HTML view for the completed run."""

        registration = load_registered_project(self.workspace_root, context.project_id)
        run_path = registration.layout.runs_dir / context.run_id / "run.json"
        stage_rows = [
            {
                "stage_id": stage.get("stage_id"),
                "status": stage.get("status"),
                "detail": "checkpointed",
            }
            for stage in context.run.get("stages", [])
            if isinstance(stage, Mapping)
        ]
        web = write_static_web_index(
            self.workspace_root,
            context.project_id,
            context.run_id,
            title=f"{registration.record['name']} — Research understanding",
            status="succeeded",
            stage_rows=stage_rows,
            knowledge_root=registration.layout.knowledge_root,
            coverage_path=registration.layout.indexes_dir / "coverage-report.json",
            run_path=run_path,
            created_at=context.run.get("updated_at"),
        )
        report = self._run_stage_report(
            context,
            summary="A self-contained read-only project-understanding web index was published.",
            reason_code="self-contained-static-web",
            inputs=("knowledge-package", "run-report", "coverage-report"),
            outputs=("web-index",),
        )
        return StageOutcome.succeeded(
            input_versions={"web_render_version": "project-understand-web-v1"},
            artifacts=(report, web.as_run_artifact()),
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
        through_stage: str | None = None,
    ) -> ProjectRunResult:
        """Register or reuse a project, then run the complete R3 pipeline."""

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
                through_stage=through_stage,
                _complete_pipeline=True,
            )
        return self.project_run_start(
            registration.project_id,
            through_stage=through_stage,
            _complete_pipeline=True,
        )

    def project_run_start(
        self,
        project_id: str,
        *,
        through_stage: str | None = None,
        _complete_pipeline: bool = False,
    ) -> ProjectRunResult:
        """Start a persisted run using currently available Core stages."""

        return ProjectRunOrchestrator(
            self.workspace_root,
            runners=self._project_run_stage_runners(complete_pipeline=_complete_pipeline),
        ).start(project_id, through_stage=through_stage)

    def project_run_resume(
        self,
        project_id: str,
        run_id: str,
        *,
        through_stage: str | None = None,
        _complete_pipeline: bool = False,
    ) -> ProjectRunResult:
        """Resume one run while preserving successful stage checkpoints."""

        return ProjectRunOrchestrator(
            self.workspace_root,
            runners=self._project_run_stage_runners(complete_pipeline=_complete_pipeline),
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

    def project_map(self, project_id: str) -> ProjectMapResult:
        """Generate the deterministic, Manifest-only E-02 project map."""

        return generate_project_map(
            workspace_root=self.workspace_root,
            project_id=project_id,
        )

    def hierarchical_understanding(
        self,
        project_id: str,
        *,
        observations: Iterable[ChunkObservation | dict[str, Any]] = (),
    ) -> HierarchicalUnderstandingResult:
        """Generate bounded E-03 chunk/file/module/project understanding."""

        return generate_hierarchical_understanding(
            workspace_root=self.workspace_root,
            project_id=project_id,
            observations=observations,
        )

    def execution_flow(
        self,
        project_id: str,
        *,
        observations: Iterable[FlowNodeObservation | FlowEdgeObservation | dict[str, Any]] = (),
    ) -> ExecutionFlowResult:
        """Generate the bounded E-04 execution-flow machine artifact."""

        return generate_execution_flow(
            workspace_root=self.workspace_root,
            project_id=project_id,
            observations=observations,
        )

    def research_linkage(
        self,
        project_id: str,
        *,
        observations: Iterable[ResearchEntityObservation | ResearchRelationObservation | dict[str, Any]] = (),
    ) -> ResearchLinkageResult:
        """Generate the bounded E-05 research-provenance linkage artifact."""

        return generate_research_linkage(
            workspace_root=self.workspace_root,
            project_id=project_id,
            observations=observations,
        )

    def experiment_chains(
        self,
        project_id: str,
        *,
        observations: Iterable[
            ConfigObservation
            | RunObservation
            | ResultObservation
            | ClaimObservation
            | ClaimLinkObservation
            | dict[str, Any]
        ] = (),
    ) -> ExperimentChainsResult:
        """Generate the bounded E-06 experiment-chain machine artifact."""

        return generate_experiment_chains(
            workspace_root=self.workspace_root,
            project_id=project_id,
            observations=observations,
        )

    def plan(
        self,
        project_id: str,
        *,
        objective: str | None = None,
        generated_at: object | None = None,
        plan_date: object | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> InitialPlanningResult:
        """Generate the current strict I-04 DRAFT planning bundle.

        This writes only project machine state. Curated Markdown remains behind
        the E-07 renderer and F-05A/F-05B controlled persistence boundary.
        """

        return generate_initial_plan(
            self.workspace_root,
            project_id,
            objective=objective,
            generated_at=generated_at,
            plan_date=plan_date,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def knowledge_render(
        self,
        project_id: str,
        *,
        rendered_at: object | None = None,
        plan_date: object | None = None,
        rendering_data: Mapping[str, Any] | None = None,
        host_data: Mapping[str, Any] | None = None,
        machine_artifacts: Mapping[str, Mapping[str, Any] | None] | None = None,
        persist: bool = True,
        host_context: TrustedHostSessionContext | None = None,
        decision_id_prefix: str | None = None,
        decision_id: str | None = None,
        authorized_at: object | None = None,
        authorization_factory: Callable[
            [ControlledMarkdownUpdatePlan], ControlledMarkdownWriteAuthorization
        ]
        | None = None,
    ) -> KnowledgeRenderingResult:
        """Render the complete E-07 Knowledge Schema v2 package.

        Persistence remains bound to F-05A planning and F-05B host authorization;
        the facade adds no alternate Markdown write path.
        """

        if __package__:
            from .knowledge_renderer import render_project_knowledge
        else:
            import sys

            repo_root = str(Path(__file__).resolve().parent.parent)
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            from tools.knowledge_renderer import render_project_knowledge

        return render_project_knowledge(
            self.workspace_root,
            project_id,
            rendered_at=rendered_at,
            plan_date=plan_date,
            rendering_data=rendering_data,
            host_data=host_data,
            machine_artifacts=machine_artifacts,
            persist=persist,
            host_context=host_context,
            decision_id_prefix=decision_id_prefix,
            decision_id=decision_id,
            authorized_at=authorized_at,
            authorization_factory=authorization_factory,
        )

    render_knowledge = knowledge_render

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
