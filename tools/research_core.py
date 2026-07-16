#!/usr/bin/env python3
"""Host-independent Research Core service facade for project operations.

The facade is the stable Python boundary used by the CLI and future host
adapters. It deliberately contains no argument parsing, terminal output, MCP,
Hook, Web, Codex, or Claude Code dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Support both ``import tools.research_core`` and direct sibling imports.
if __package__:
    from .coverage_report import CoverageReportResult, generate_coverage_report
    from .extraction_schema import Locator
    from .project_inventory import ProjectInventoryResult, inventory_project
    from .project_registry import ProjectRegistrationResult, register_project
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
        CoverageReportResult,
        generate_coverage_report,
    )
    from extraction_schema import Locator  # type: ignore[no-redef]
    from project_inventory import (  # type: ignore[no-redef]
        ProjectInventoryResult,
        inventory_project,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        register_project,
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


def _pattern_tuple(patterns: Iterable[str] | None) -> tuple[str, ...]:
    """Materialize one policy-pattern iterable without splitting a string."""

    if patterns is None:
        return ()
    if isinstance(patterns, str):
        return (patterns,)
    return tuple(patterns)


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
        """Generate a deterministic coverage report from the current Manifest."""

        return generate_coverage_report(
            workspace_root=self.workspace_root,
            project_id=project_id,
        )

    def source_open(
        self,
        project_id: str,
        target_id: str,
        *,
        locator: Locator | dict[str, Any] | None = None,
        expected_content_hash: str | None = None,
        expected_excerpt_hash: str | None = None,
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
            )

        raise SourceNotFoundError(
            "target_id must be a Core source ID (src-*) or Evidence ID (evd-*)"
        )
