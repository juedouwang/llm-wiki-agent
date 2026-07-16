#!/usr/bin/env python3
"""Host-neutral MCP stdio adapter over :mod:`tools.research_core`.

The adapter owns MCP transport concerns only. Filesystem, schema, project,
coverage, and source-opening behavior remains in ``ResearchCoreService`` and
its domain modules. Tool calls return a schema-versioned envelope and stable
error codes; raw source content is returned only by the explicit source-open
tool.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

# Support both ``python -m tools.research_mcp`` and direct script execution.
if __package__:
    from .coverage_report import (
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportError,
    )
    from .extraction_schema import EXTRACTION_SCHEMA_VERSION, LOCATOR_KIND
    from .host_context import (
        HOST_CONTEXT_BUDGET_UNIT,
        HOST_CONTEXT_DEFAULT_MAX_BYTES,
        HOST_CONTEXT_KIND,
        HOST_CONTEXT_MAX_MAX_BYTES,
        HOST_CONTEXT_MIN_MAX_BYTES,
        HOST_CONTEXT_SCHEMA_VERSION,
        HOST_CONTEXT_VERSION,
        HostContextBudgetError,
        HostContextError,
    )
    from .project_layout import (
        InvalidProjectIdError,
        LayoutError,
        SchemaVersionError,
        UnsupportedSchemaVersionError,
    )
    from .project_registry import ProjectNotRegisteredError, ProjectRecordError
    from .research_core import (
        HOST_COVERAGE_KIND,
        HOST_COVERAGE_VERSION,
        HOST_RESULT_SCHEMA_VERSION,
        HOST_SOURCE_LOCATION_KIND,
        HOST_SOURCE_LOCATION_VERSION,
        HOST_SOURCE_OPEN_KIND,
        HOST_SOURCE_OPEN_VERSION,
        PROJECT_CONTEXT_KIND,
        PROJECT_CONTEXT_VERSION,
        ResearchCoreService,
    )
    from .source_access import SourceAccessError
else:
    from coverage_report import (  # type: ignore[no-redef]
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportError,
    )
    from extraction_schema import (  # type: ignore[no-redef]
        EXTRACTION_SCHEMA_VERSION,
        LOCATOR_KIND,
    )
    from host_context import (  # type: ignore[no-redef]
        HOST_CONTEXT_BUDGET_UNIT,
        HOST_CONTEXT_DEFAULT_MAX_BYTES,
        HOST_CONTEXT_KIND,
        HOST_CONTEXT_MAX_MAX_BYTES,
        HOST_CONTEXT_MIN_MAX_BYTES,
        HOST_CONTEXT_SCHEMA_VERSION,
        HOST_CONTEXT_VERSION,
        HostContextBudgetError,
        HostContextError,
    )
    from project_layout import (  # type: ignore[no-redef]
        InvalidProjectIdError,
        LayoutError,
        SchemaVersionError,
        UnsupportedSchemaVersionError,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectNotRegisteredError,
        ProjectRecordError,
    )
    from research_core import (  # type: ignore[no-redef]
        HOST_COVERAGE_KIND,
        HOST_COVERAGE_VERSION,
        HOST_RESULT_SCHEMA_VERSION,
        HOST_SOURCE_LOCATION_KIND,
        HOST_SOURCE_LOCATION_VERSION,
        HOST_SOURCE_OPEN_KIND,
        HOST_SOURCE_OPEN_VERSION,
        PROJECT_CONTEXT_KIND,
        PROJECT_CONTEXT_VERSION,
        ResearchCoreService,
    )
    from source_access import SourceAccessError  # type: ignore[no-redef]


MCP_ENVELOPE_SCHEMA_VERSION = 1
MCP_SERVER_NAME = "llmwiki-research-core"
MCP_SERVER_VERSION = "0.2.0"

PROJECT_CONTEXT_TOOL = "llmwiki_project_context"
HOST_CONTEXT_TOOL = "llmwiki_host_context"
COVERAGE_TOOL = "llmwiki_coverage"
SOURCE_OPEN_TOOL = "llmwiki_source_open"
QUERY_TOOL = "llmwiki_query"
RECONCILE_TOOL = "llmwiki_reconcile"
PLAN_TOOL = "llmwiki_plan"

_TOOL_CAPABILITIES = {
    PROJECT_CONTEXT_TOOL: "project-context",
    HOST_CONTEXT_TOOL: "host-context",
    COVERAGE_TOOL: "coverage",
    SOURCE_OPEN_TOOL: "source-open",
    QUERY_TOOL: "query",
    RECONCILE_TOOL: "reconcile",
    PLAN_TOOL: "plan",
}

_UNAVAILABLE_MILESTONES = {
    QUERY_TOOL: "G-04",
    RECONCILE_TOOL: "H-07",
    PLAN_TOOL: "I-04",
}

_SAFE_ERROR_MESSAGES = {
    "invalid-arguments": "Arguments do not match the published MCP tool contract.",
    "mcp-tool-not-found": "The requested MCP tool is not exposed by this server.",
    "capability-unavailable": "This Core capability is not implemented yet.",
    "context-budget-too-small": (
        "The configured budget cannot hold the mandatory Host Context Pack."
    ),
    "host-context-invalid": "The Host Context Pack could not be validated safely.",
    "project-id-invalid": "The project identifier is invalid.",
    "project-not-registered": "The requested project is not registered in this workspace.",
    "project-record-invalid": "The registered project record could not be trusted.",
    "schema-version-invalid": "A required structured record has an invalid schema version.",
    "schema-version-unsupported": "A structured record uses a future unsupported schema version.",
    "coverage-unavailable": "Coverage could not be generated from the current project state.",
    "core-layout-invalid": "The requested operation violates the Research Core storage contract.",
    "local-io-failed": "A required local operation failed.",
    "internal-error": "The MCP adapter could not complete the request safely.",
    "source-access-failed": "The requested source operation failed.",
    "source-not-registered": "The requested Source or Evidence is not registered.",
    "source-current-path-missing": "The current Source path is missing.",
    "source-relocation-ambiguous": "The Source cannot be rebound without user confirmation.",
    "source-path-outside-project": "The Source resolves outside the registered project boundary.",
    "source-content-hash-mismatch": "The current Source content does not match its recorded identity.",
    "source-content-policy-denied": "Manifest policy denies raw-content access for this Source.",
    "current-source-version-mismatch": "The Evidence does not reference the current Source version.",
    "source-locator-invalid": "The Source locator is invalid for this request.",
    "source-locator-format-unsupported": "The Source format does not support the requested locator.",
    "source-read-failed": "The current Source bytes could not be read safely.",
    "source-excerpt-hash-mismatch": "The reopened excerpt does not match the persisted Evidence.",
}


class MCPArgumentError(ValueError):
    """Raised when a call does not match a published tool input schema."""


@dataclass(frozen=True)
class CapabilityUnavailableError(RuntimeError):
    """Honest boundary for roadmap capabilities not present in Core yet."""

    capability: str
    available_after: str

    def __str__(self) -> str:
        return f"{self.capability} is unavailable until {self.available_after}"


def _object_schema(
    properties: dict[str, Any],
    *,
    required: Sequence[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _relative_path_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": r"^(?!/)(?![A-Za-z]:)(?!.*\\)(?!.*(?:^|/)\.\.?(?:/|$))(?!.*//).+$",
    }


def _locator_schema() -> dict[str, Any]:
    common = {
        "schema_version": {"const": EXTRACTION_SCHEMA_VERSION},
        "kind": {"const": LOCATOR_KIND},
    }
    positive = {"type": "integer", "minimum": 1}
    return {
        "oneOf": [
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "line_range"},
                    "start_line": positive,
                    "end_line": positive,
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "start_line",
                    "end_line",
                ),
            ),
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "pdf_page"},
                    "page_number": positive,
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "page_number",
                ),
            ),
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "notebook_cell"},
                    "cell_index": {"type": "integer", "minimum": 0},
                    "cell_id": _nullable({"type": "string", "minLength": 1}),
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "cell_index",
                    "cell_id",
                ),
            ),
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "table_range"},
                    "sheet": {"type": "string", "minLength": 1},
                    "start_cell": {
                        "type": "string",
                        "pattern": "^[A-Z]+[1-9][0-9]*$",
                    },
                    "end_cell": {
                        "type": "string",
                        "pattern": "^[A-Z]+[1-9][0-9]*$",
                    },
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "sheet",
                    "start_cell",
                    "end_cell",
                ),
            ),
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "section"},
                    "heading_path": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "start_line": positive,
                    "end_line": positive,
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "heading_path",
                    "start_line",
                    "end_line",
                ),
            ),
            _object_schema(
                {
                    **common,
                    "locator_type": {"const": "symbol"},
                    "symbol": {"type": "string", "minLength": 1},
                    "start_line": positive,
                    "end_line": positive,
                },
                required=(
                    "schema_version",
                    "kind",
                    "locator_type",
                    "symbol",
                    "start_line",
                    "end_line",
                ),
            ),
        ]
    }


def _host_context_result_schema() -> dict[str, Any]:
    nonnegative = {"type": "integer", "minimum": 0}
    positive = {"type": "integer", "minimum": 1}
    sha256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    nullable_text = _nullable({"type": "string", "minLength": 1})
    count_axis = {
        "type": "object",
        "propertyNames": {
            "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$"
        },
        "additionalProperties": nonnegative,
    }
    state = _object_schema(
        {
            "project": _object_schema(
                {
                    "name": {"type": "string", "minLength": 1},
                    "identity_strategy": {"type": "string", "minLength": 1},
                    "registered_at": {"type": "string", "minLength": 1},
                    "git": _object_schema(
                        {
                            "available": {"type": "boolean"},
                            "is_repository": {"type": "boolean"},
                            "branch": nullable_text,
                            "head_commit": nullable_text,
                        },
                        required=(
                            "available",
                            "is_repository",
                            "branch",
                            "head_commit",
                        ),
                    ),
                    "onboarding": _object_schema(
                        {
                            "final_goal": nullable_text,
                            "current_stage": nullable_text,
                            "important_question": nullable_text,
                            "deadline": nullable_text,
                            "daily_available_hours": _nullable(
                                {
                                    "type": "number",
                                    "exclusiveMinimum": 0,
                                    "maximum": 24,
                                }
                            ),
                        },
                        required=(
                            "final_goal",
                            "current_stage",
                            "important_question",
                            "deadline",
                            "daily_available_hours",
                        ),
                    ),
                },
                required=(
                    "name",
                    "identity_strategy",
                    "registered_at",
                    "git",
                    "onboarding",
                ),
            ),
            "inventory": _object_schema(
                {
                    "manifest_version": {"type": "string", "minLength": 1},
                    "scan_generation": positive,
                    "file_count": nonnegative,
                    "byte_count": nonnegative,
                    "failed_file_count": nonnegative,
                    "processing_status_counts": count_axis,
                    "read_depth_counts": count_axis,
                },
                required=(
                    "manifest_version",
                    "scan_generation",
                    "file_count",
                    "byte_count",
                    "failed_file_count",
                    "processing_status_counts",
                    "read_depth_counts",
                ),
            ),
        },
        required=("project", "inventory"),
    )
    risk = _object_schema(
        {
            "risk_code": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
            },
            "severity": {"type": "string", "enum": ["high", "medium"]},
            "file_count": positive,
            "byte_count": nonnegative,
            "summary": {"type": "string", "minLength": 1},
        },
        required=(
            "risk_code",
            "severity",
            "file_count",
            "byte_count",
            "summary",
        ),
    )
    evidence_ref = _object_schema(
        {
            "evidence_id": {
                "type": "string",
                "pattern": "^evd-[0-9a-f]{64}$",
            },
            "source_id": {
                "type": "string",
                "pattern": "^src-[0-9a-f]{32}$",
            },
            "source_version": positive,
            "content_hash": sha256,
            "locator": _locator_schema(),
            "excerpt_hash": sha256,
        },
        required=(
            "evidence_id",
            "source_id",
            "source_version",
            "content_hash",
            "locator",
            "excerpt_hash",
        ),
    )
    omission = _object_schema(
        {
            "section": {
                "type": "string",
                "enum": ["tasks", "risks", "evidence_refs"],
            },
            "reason_code": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
            },
            "count": positive,
        },
        required=("section", "reason_code", "count"),
    )
    return _object_schema(
        {
            "schema_version": {"const": HOST_CONTEXT_SCHEMA_VERSION},
            "kind": {"const": HOST_CONTEXT_KIND},
            "pack_version": {"const": HOST_CONTEXT_VERSION},
            "project_id": {"type": "string", "minLength": 1},
            "budget": _object_schema(
                {
                    "unit": {"const": HOST_CONTEXT_BUDGET_UNIT},
                    "max_bytes": {
                        "type": "integer",
                        "minimum": HOST_CONTEXT_MIN_MAX_BYTES,
                        "maximum": HOST_CONTEXT_MAX_MAX_BYTES,
                    },
                    "used_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": HOST_CONTEXT_MAX_MAX_BYTES,
                    },
                    "truncated": {"type": "boolean"},
                },
                required=("unit", "max_bytes", "used_bytes", "truncated"),
            ),
            "state": state,
            "tasks": {"type": "array", "maxItems": 0},
            "risks": {"type": "array", "items": risk, "uniqueItems": True},
            "evidence_refs": {
                "type": "array",
                "items": evidence_ref,
                "uniqueItems": True,
            },
            "omissions": {
                "type": "array",
                "items": omission,
                "uniqueItems": True,
            },
        },
        required=(
            "schema_version",
            "kind",
            "pack_version",
            "project_id",
            "budget",
            "state",
            "tasks",
            "risks",
            "evidence_refs",
            "omissions",
        ),
    )


def _project_context_result_schema() -> dict[str, Any]:
    nullable_text = _nullable({"type": "string"})
    return _object_schema(
        {
            "schema_version": {"const": HOST_RESULT_SCHEMA_VERSION},
            "kind": {"const": PROJECT_CONTEXT_KIND},
            "context_version": {"const": PROJECT_CONTEXT_VERSION},
            "project_id": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "identity_strategy": {"type": "string", "minLength": 1},
            "registered_at": {"type": "string", "minLength": 1},
            "git": _object_schema(
                {
                    "available": {"type": "boolean"},
                    "is_repository": {"type": "boolean"},
                    "branch": nullable_text,
                    "head_commit": nullable_text,
                },
                required=(
                    "available",
                    "is_repository",
                    "branch",
                    "head_commit",
                ),
            ),
            "onboarding": _object_schema(
                {
                    "final_goal": nullable_text,
                    "current_stage": nullable_text,
                    "important_question": nullable_text,
                    "deadline": nullable_text,
                    "daily_available_hours": _nullable(
                        {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "maximum": 24,
                        }
                    ),
                },
                required=(
                    "final_goal",
                    "current_stage",
                    "important_question",
                    "deadline",
                    "daily_available_hours",
                ),
            ),
        },
        required=(
            "schema_version",
            "kind",
            "context_version",
            "project_id",
            "name",
            "identity_strategy",
            "registered_at",
            "git",
            "onboarding",
        ),
    )


def _coverage_result_schema() -> dict[str, Any]:
    nonnegative = {"type": "integer", "minimum": 0}
    bucket = _object_schema(
        {"file_count": nonnegative, "byte_count": nonnegative},
        required=("file_count", "byte_count"),
    )
    reason_bucket = _object_schema(
        {
            "file_count": nonnegative,
            "byte_count": nonnegative,
            "details": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
        },
        required=("file_count", "byte_count", "details"),
    )

    def axis(value_schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "propertyNames": {
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$"
            },
            "additionalProperties": value_schema,
        }

    axes = ("research_role", "processing_status", "read_depth", "reason")
    coverage = _object_schema(
        {
            "research_role": axis(bucket),
            "processing_status": axis(bucket),
            "read_depth": axis(bucket),
            "reason": axis(reason_bucket),
        },
        required=axes,
    )
    reconciliation_axes = _object_schema(
        {name: bucket for name in axes},
        required=axes,
    )
    failure = _object_schema(
        {
            "path": _relative_path_schema(),
            "byte_count": nonnegative,
            "research_role": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
            },
            "processing_status": {"const": "failed"},
            "read_depth": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
            },
            "reason_code": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
            },
            "reason": {"type": "string", "minLength": 1},
        },
        required=(
            "path",
            "byte_count",
            "research_role",
            "processing_status",
            "read_depth",
            "reason_code",
            "reason",
        ),
    )
    report = _object_schema(
        {
            "schema_version": {"const": COVERAGE_REPORT_SCHEMA_VERSION},
            "kind": {"const": COVERAGE_REPORT_KIND},
            "report_version": {"const": COVERAGE_REPORT_VERSION},
            "project_id": {"type": "string", "minLength": 1},
            "manifest": _object_schema(
                {
                    "manifest_version": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
                    },
                    "scan_generation": {"type": "integer", "minimum": 1},
                },
                required=("manifest_version", "scan_generation"),
            ),
            "totals": _object_schema(
                {
                    "file_count": nonnegative,
                    "byte_count": nonnegative,
                    "failed_file_count": nonnegative,
                },
                required=("file_count", "byte_count", "failed_file_count"),
            ),
            "coverage": coverage,
            "failures": {"type": "array", "items": failure},
            "reconciliation": _object_schema(
                {
                    "manifest": bucket,
                    "axes": reconciliation_axes,
                },
                required=("manifest", "axes"),
            ),
        },
        required=(
            "schema_version",
            "kind",
            "report_version",
            "project_id",
            "manifest",
            "totals",
            "coverage",
            "failures",
            "reconciliation",
        ),
    )
    return _object_schema(
        {
            "schema_version": {"const": HOST_RESULT_SCHEMA_VERSION},
            "kind": {"const": HOST_COVERAGE_KIND},
            "view_version": {"const": HOST_COVERAGE_VERSION},
            "project_id": {"type": "string", "minLength": 1},
            "report_persisted": {"const": True},
            "report": report,
        },
        required=(
            "schema_version",
            "kind",
            "view_version",
            "project_id",
            "report_persisted",
            "report",
        ),
    )


def _source_open_result_schema() -> dict[str, Any]:
    sha256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    source = _object_schema(
        {
            "schema_version": {"const": HOST_RESULT_SCHEMA_VERSION},
            "kind": {"const": HOST_SOURCE_LOCATION_KIND},
            "view_version": {"const": HOST_SOURCE_LOCATION_VERSION},
            "project_id": {"type": "string", "minLength": 1},
            "source_id": {
                "type": "string",
                "pattern": "^src-[0-9a-f]{32}$",
            },
            "current_path": _relative_path_schema(),
            "current_version": {"type": "integer", "minimum": 1},
            "content_hash": sha256,
        },
        required=(
            "schema_version",
            "kind",
            "view_version",
            "project_id",
            "source_id",
            "current_path",
            "current_version",
            "content_hash",
        ),
    )
    return _object_schema(
        {
            "schema_version": {"const": HOST_RESULT_SCHEMA_VERSION},
            "kind": {"const": HOST_SOURCE_OPEN_KIND},
            "view_version": {"const": HOST_SOURCE_OPEN_VERSION},
            "source": source,
            "locator": _locator_schema(),
            "excerpt": {"type": "string"},
            "excerpt_hash": sha256,
            "excerpt_encoding": {"const": "utf-8"},
            "excerpt_format": {"type": "string", "minLength": 1},
            "content_hash_verified": {"const": True},
            "excerpt_hash_verified": _nullable({"type": "boolean"}),
            "evidence_id": _nullable(
                {"type": "string", "pattern": "^evd-[0-9a-f]{64}$"}
            ),
            "evidence_source_version": _nullable(
                {"type": "integer", "minimum": 1}
            ),
        },
        required=(
            "schema_version",
            "kind",
            "view_version",
            "source",
            "locator",
            "excerpt",
            "excerpt_hash",
            "excerpt_encoding",
            "excerpt_format",
            "content_hash_verified",
            "excerpt_hash_verified",
            "evidence_id",
            "evidence_source_version",
        ),
    )


def _output_schema(
    capability: str,
    result_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_schema = _object_schema(
        {
            "code": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
            "retryable": {"type": "boolean"},
            "details": _object_schema(
                {
                    "available_after": {
                        "type": "string",
                        "pattern": "^[A-J]-[0-9]{2}$",
                    },
                    "status": {"const": "not-implemented"},
                },
                required=("available_after", "status"),
            ),
        },
        required=("code", "message", "retryable"),
    )
    return {
        "type": "object",
        "properties": {
            "schema_version": {"const": MCP_ENVELOPE_SCHEMA_VERSION},
            "ok": {"type": "boolean"},
            "capability": {"const": capability},
            "result": result_schema or {"type": "object"},
            "error": error_schema,
        },
        "required": ["schema_version", "ok", "capability"],
        "oneOf": [
            {
                "properties": {"ok": {"const": True}},
                "required": ["result"],
                "not": {"required": ["error"]},
            },
            {
                "properties": {"ok": {"const": False}},
                "required": ["error"],
                "not": {"required": ["result"]},
            },
        ],
        "additionalProperties": False,
    }


def _read_only_annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def tool_contracts() -> tuple[Tool, ...]:
    """Return the stable G-08 MCP tool catalog."""

    project_id = {
        "type": "string",
        "minLength": 1,
        "description": "Stable Research Core project_id.",
    }
    sha256 = {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
    }
    return (
        Tool(
            name=PROJECT_CONTEXT_TOOL,
            title="Research project context",
            description=(
                "Load trusted registration and onboarding context through "
                "ResearchCoreService; this does not scan source files."
            ),
            inputSchema=_object_schema(
                {"project_id": project_id},
                required=("project_id",),
            ),
            outputSchema=_output_schema("project-context", _project_context_result_schema()),
            annotations=_read_only_annotations("Research project context"),
        ),
        Tool(
            name=HOST_CONTEXT_TOOL,
            title="Budget-bounded host context",
            description=(
                "Assemble state, risk, task-availability, and current Evidence "
                "references through Research Core without returning source excerpts "
                "or bulk wiki content."
            ),
            inputSchema=_object_schema(
                {
                    "project_id": project_id,
                    "max_bytes": {
                        "type": "integer",
                        "minimum": HOST_CONTEXT_MIN_MAX_BYTES,
                        "maximum": HOST_CONTEXT_MAX_MAX_BYTES,
                        "default": HOST_CONTEXT_DEFAULT_MAX_BYTES,
                        "description": (
                            "Maximum canonical compact UTF-8 JSON bytes for the "
                            "complete Host Context Pack envelope."
                        ),
                    },
                },
                required=("project_id",),
            ),
            outputSchema=_output_schema(
                "host-context",
                _host_context_result_schema(),
            ),
            annotations=ToolAnnotations(
                title="Budget-bounded host context",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        Tool(
            name=COVERAGE_TOOL,
            title="Research coverage",
            description=(
                "Generate the deterministic Core coverage report for the current "
                "Manifest."
            ),
            inputSchema=_object_schema(
                {"project_id": project_id},
                required=("project_id",),
            ),
            outputSchema=_output_schema("coverage", _coverage_result_schema()),
            annotations=ToolAnnotations(
                title="Research coverage",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        Tool(
            name=SOURCE_OPEN_TOOL,
            title="Open current research source",
            description=(
                "Explicitly reopen a Source locator or persisted Evidence through "
                "Research Core. This is the only G-07 tool that returns raw excerpts."
            ),
            inputSchema=_object_schema(
                {
                    "project_id": project_id,
                    "target_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Core src-* or evd-* identifier.",
                    },
                    "locator": {
                        **_locator_schema(),
                        "description": "Versioned locator; required for src-* targets.",
                    },
                    "expected_content_hash": sha256,
                    "expected_excerpt_hash": sha256,
                },
                required=("project_id", "target_id"),
            ),
            outputSchema=_output_schema("source-open", _source_open_result_schema()),
            annotations=_read_only_annotations("Open current research source"),
        ),
        Tool(
            name=QUERY_TOOL,
            title="Verified research query",
            description=(
                "Reserved Core query contract. G-07 returns capability-unavailable "
                "until the real G-04 Verified Query pipeline exists."
            ),
            inputSchema=_object_schema(
                {
                    "project_id": project_id,
                    "question": {"type": "string", "minLength": 1},
                    "mode": {
                        "type": "string",
                        "enum": ["fast", "verified", "auto"],
                        "default": "auto",
                    },
                },
                required=("project_id", "question"),
            ),
            outputSchema=_output_schema("query"),
            annotations=_read_only_annotations("Verified research query"),
        ),
        Tool(
            name=RECONCILE_TOOL,
            title="Reconcile research project",
            description=(
                "Reserved Core reconciliation contract. G-07 returns "
                "capability-unavailable until the real H-07 synchronization boundary "
                "exists."
            ),
            inputSchema=_object_schema(
                {
                    "project_id": project_id,
                    "dirty_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                },
                required=("project_id",),
            ),
            outputSchema=_output_schema("reconcile"),
            annotations=ToolAnnotations(
                title="Reconcile research project",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        Tool(
            name=PLAN_TOOL,
            title="Plan research work",
            description=(
                "Reserved Core planning contract. G-07 returns capability-unavailable "
                "until the real I-04 goal/backlog planning slice exists."
            ),
            inputSchema=_object_schema(
                {
                    "project_id": project_id,
                    "objective": {"type": "string", "minLength": 1},
                },
                required=("project_id",),
            ),
            outputSchema=_output_schema("plan"),
            annotations=ToolAnnotations(
                title="Plan research work",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
    )


_TOOL_BY_NAME = {tool.name: tool for tool in tool_contracts()}


def _schema_validator(schema: dict[str, Any]) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:  # A published contract bug must fail at import time.
        raise RuntimeError("invalid MCP JSON Schema") from exc
    return Draft202012Validator(schema)


_INPUT_VALIDATORS = {
    name: _schema_validator(tool.inputSchema) for name, tool in _TOOL_BY_NAME.items()
}
_OUTPUT_VALIDATORS = {
    name: _schema_validator(tool.outputSchema) for name, tool in _TOOL_BY_NAME.items()
}
_UNKNOWN_TOOL_OUTPUT_VALIDATOR = _schema_validator(_output_schema("mcp"))


def _validate_published_input(name: str, arguments: object) -> None:
    try:
        _INPUT_VALIDATORS[name].validate(arguments)
    except ValidationError as exc:
        raise MCPArgumentError("arguments failed the published JSON Schema") from exc


def _error_payload(
    capability: str,
    code: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": _SAFE_ERROR_MESSAGES.get(code, _SAFE_ERROR_MESSAGES["internal-error"]),
        "retryable": False,
    }
    if details:
        error["details"] = details
    return {
        "schema_version": MCP_ENVELOPE_SCHEMA_VERSION,
        "ok": False,
        "capability": capability,
        "error": error,
    }


def _success_payload(capability: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MCP_ENVELOPE_SCHEMA_VERSION,
        "ok": True,
        "capability": capability,
        "result": result,
    }


def _emit_result(payload: dict[str, Any], *, is_error: bool) -> CallToolResult:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return CallToolResult(
        content=[TextContent(type="text", text=serialized)],
        structuredContent=payload,
        isError=is_error,
    )


def _validated_result(
    name: str,
    payload: dict[str, Any],
    *,
    is_error: bool,
) -> CallToolResult:
    validator = _OUTPUT_VALIDATORS.get(name, _UNKNOWN_TOOL_OUTPUT_VALIDATOR)
    validator.validate(payload)
    return _emit_result(payload, is_error=is_error)


def _internal_error_result(name: str, capability: str) -> CallToolResult:
    payload = _error_payload(capability, "internal-error")
    validator = _OUTPUT_VALIDATORS.get(name, _UNKNOWN_TOOL_OUTPUT_VALIDATOR)
    # This constant fallback is intentionally not recursive: if it does not match
    # the published schema, import-time and focused contract tests must fail loudly.
    validator.validate(payload)
    return _emit_result(payload, is_error=True)


def _require_object(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise MCPArgumentError("arguments must be an object")
    return arguments


def _reject_unknown(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    if not set(arguments).issubset(allowed):
        raise MCPArgumentError("arguments contain unknown fields")


def _required_text(arguments: Mapping[str, Any], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MCPArgumentError(f"{field} must be a non-empty string")
    return value


def _optional_text(arguments: Mapping[str, Any], field: str) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MCPArgumentError(f"{field} must be a non-empty string when supplied")
    return value


def _validate_project_only(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    _reject_unknown(values, {"project_id"})
    return {"project_id": _required_text(values, "project_id")}


def _validate_host_context(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    _reject_unknown(values, {"project_id", "max_bytes"})
    max_bytes = values.get("max_bytes", HOST_CONTEXT_DEFAULT_MAX_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise MCPArgumentError("max_bytes must be an integer")
    if not HOST_CONTEXT_MIN_MAX_BYTES <= max_bytes <= HOST_CONTEXT_MAX_MAX_BYTES:
        raise MCPArgumentError(
            "max_bytes is outside the published Host Context budget range"
        )
    return {
        "project_id": _required_text(values, "project_id"),
        "max_bytes": max_bytes,
    }


def _validate_source_open(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    allowed = {
        "project_id",
        "target_id",
        "locator",
        "expected_content_hash",
        "expected_excerpt_hash",
    }
    _reject_unknown(values, allowed)
    locator = values.get("locator")
    if locator is not None and not isinstance(locator, dict):
        raise MCPArgumentError("locator must be an object")
    return {
        "project_id": _required_text(values, "project_id"),
        "target_id": _required_text(values, "target_id"),
        "locator": locator,
        "expected_content_hash": _optional_text(values, "expected_content_hash"),
        "expected_excerpt_hash": _optional_text(values, "expected_excerpt_hash"),
    }


def _validate_query(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    _reject_unknown(values, {"project_id", "question", "mode"})
    mode = values.get("mode", "auto")
    if mode not in {"fast", "verified", "auto"}:
        raise MCPArgumentError("mode must be fast, verified, or auto")
    return {
        "project_id": _required_text(values, "project_id"),
        "question": _required_text(values, "question"),
        "mode": mode,
    }


def _validate_reconcile(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    _reject_unknown(values, {"project_id", "dirty_paths"})
    dirty_paths = values.get("dirty_paths", [])
    if (
        not isinstance(dirty_paths, list)
        or any(not isinstance(path, str) or not path for path in dirty_paths)
    ):
        raise MCPArgumentError("dirty_paths must be an array of non-empty strings")
    return {
        "project_id": _required_text(values, "project_id"),
        "dirty_paths": tuple(dirty_paths),
    }


def _validate_plan(arguments: object) -> dict[str, Any]:
    values = _require_object(arguments)
    _reject_unknown(values, {"project_id", "objective"})
    return {
        "project_id": _required_text(values, "project_id"),
        "objective": _optional_text(values, "objective"),
    }


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _domain_error_code(exc: BaseException) -> str:
    chain = _exception_chain(exc)
    for item in chain:
        if isinstance(item, UnsupportedSchemaVersionError):
            return "schema-version-unsupported"
    for item in chain:
        if isinstance(item, HostContextBudgetError):
            return "context-budget-too-small"
    for item in chain:
        if isinstance(item, HostContextError):
            return "host-context-invalid"
    for item in chain:
        if isinstance(item, SchemaVersionError):
            return "schema-version-invalid"
    for item in chain:
        if isinstance(item, InvalidProjectIdError):
            return "project-id-invalid"
    for item in chain:
        if isinstance(item, ProjectNotRegisteredError):
            return "project-not-registered"
    for item in chain:
        reason_code = getattr(item, "reason_code", None)
        if isinstance(item, SourceAccessError) and isinstance(reason_code, str):
            return reason_code
    for item in chain:
        if isinstance(item, ProjectRecordError):
            return "project-record-invalid"
    for item in chain:
        if isinstance(item, CoverageReportError):
            return "coverage-unavailable"
    for item in chain:
        if isinstance(item, LayoutError):
            return "core-layout-invalid"
    if isinstance(exc, OSError):
        return "local-io-failed"
    return "internal-error"


@dataclass(frozen=True)
class ResearchMCPAdapter:
    """MCP-only dispatcher that delegates all real work to Research Core."""

    service: ResearchCoreService

    def __init__(self, workspace_root: str | Path) -> None:
        object.__setattr__(self, "service", ResearchCoreService(workspace_root))

    @property
    def workspace_root(self) -> Path:
        return self.service.workspace_root

    @staticmethod
    def _finalize(
        name: str,
        capability: str,
        payload: dict[str, Any],
        *,
        is_error: bool,
    ) -> CallToolResult:
        try:
            return _validated_result(name, payload, is_error=is_error)
        except (ValidationError, TypeError, ValueError, OverflowError):
            return _internal_error_result(name, capability)

    def call_tool(
        self,
        name: str,
        arguments: object,
    ) -> CallToolResult:
        capability = _TOOL_CAPABILITIES.get(name, "mcp")
        if name not in _TOOL_BY_NAME:
            return self._finalize(
                name,
                capability,
                _error_payload(capability, "mcp-tool-not-found"),
                is_error=True,
            )

        try:
            _validate_published_input(name, arguments)
            result = self._invoke(name, arguments)
        except MCPArgumentError:
            payload = _error_payload(capability, "invalid-arguments")
            return self._finalize(name, capability, payload, is_error=True)
        except CapabilityUnavailableError as exc:
            payload = _error_payload(
                capability,
                "capability-unavailable",
                details={
                    "available_after": exc.available_after,
                    "status": "not-implemented",
                },
            )
            return self._finalize(name, capability, payload, is_error=True)
        except Exception as exc:  # Core exceptions become stable transport errors.
            payload = _error_payload(capability, _domain_error_code(exc))
            return self._finalize(name, capability, payload, is_error=True)

        payload = _success_payload(capability, result)
        return self._finalize(name, capability, payload, is_error=False)

    def _invoke(self, name: str, arguments: object) -> dict[str, Any]:
        if name == PROJECT_CONTEXT_TOOL:
            values = _validate_project_only(arguments)
            return self.service.project_context(values["project_id"]).as_dict()
        if name == HOST_CONTEXT_TOOL:
            values = _validate_host_context(arguments)
            project_id = values.pop("project_id")
            return self.service.host_context_pack(
                project_id,
                **values,
            ).as_dict()
        if name == COVERAGE_TOOL:
            values = _validate_project_only(arguments)
            return self.service.coverage_view(values["project_id"]).as_dict()
        if name == SOURCE_OPEN_TOOL:
            values = _validate_source_open(arguments)
            project_id = values.pop("project_id")
            target_id = values.pop("target_id")
            return self.service.source_open_view(
                project_id,
                target_id,
                **values,
            ).as_dict()
        if name == QUERY_TOOL:
            _validate_query(arguments)
        elif name == RECONCILE_TOOL:
            _validate_reconcile(arguments)
        elif name == PLAN_TOOL:
            _validate_plan(arguments)
        else:  # Guarded by call_tool; retained as a fail-closed invariant.
            raise MCPArgumentError("unknown tool")
        raise CapabilityUnavailableError(
            capability=_TOOL_CAPABILITIES[name],
            available_after=_UNAVAILABLE_MILESTONES[name],
        )


def create_mcp_server(adapter: ResearchMCPAdapter) -> Server:
    """Create one low-level MCP server over the supplied adapter."""

    server = Server(
        MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        instructions=(
            "Use host-context for a budget-bounded project handoff, and use "
            "project-context or coverage for focused metadata. Call source-open "
            "only when exact current-source evidence is needed. Query, reconcile, "
            "and plan return explicit capability-unavailable errors until their "
            "Core slices land."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return list(tool_contracts())

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return adapter.call_tool(name, arguments)

    return server


async def serve_stdio(workspace_root: str | Path) -> None:
    """Run the G-07 MCP server over standard input/output."""

    # The SDK warns with caller-controlled unknown tool names. Suppress that
    # transport diagnostic so stderr cannot become a side channel for input.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)
    adapter = ResearchMCPAdapter(workspace_root)
    server = create_mcp_server(adapter)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the llmwiki Research Core MCP server over stdio."
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        help="Assistant workspace containing .llmwiki/projects and wiki/projects.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    anyio.run(serve_stdio, args.workspace_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
