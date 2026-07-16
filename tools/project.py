#!/usr/bin/env python3
"""Project-scoped research-assistant commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

# Support both ``python -m tools.project`` and ``python tools/project.py``.
if __package__:
    from .host_context import HOST_CONTEXT_DEFAULT_MAX_BYTES
    from .project_layout import LayoutError
    from .project_runs import PROJECT_UNDERSTAND_STAGES
    from .research_core import ResearchCoreService
    from .scan_policy import ScanPolicyError
    from .source_access import SourceAccessError, deserialize_locator, locate_source
    from .source_health import evaluate_source_health
    from .source_recovery import recover_source
    from .source_registry import get_source_history, sync_source_registry
else:
    from host_context import (  # type: ignore[no-redef]
        HOST_CONTEXT_DEFAULT_MAX_BYTES,
    )
    from project_layout import LayoutError  # type: ignore[no-redef]
    from project_runs import (  # type: ignore[no-redef]
        PROJECT_UNDERSTAND_STAGES,
    )
    from research_core import ResearchCoreService  # type: ignore[no-redef]
    from scan_policy import ScanPolicyError  # type: ignore[no-redef]
    from source_access import (  # type: ignore[no-redef]
        SourceAccessError,
        deserialize_locator,
        locate_source,
    )
    from source_health import evaluate_source_health  # type: ignore[no-redef]
    from source_recovery import recover_source  # type: ignore[no-redef]
    from source_registry import (  # type: ignore[no-redef]
        get_source_history,
        sync_source_registry,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage external research projects without modifying source files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser(
        "register",
        help="Assign a stable project ID and initialize project-scoped storage.",
    )
    register.add_argument("project_path", help="Existing research-project directory")
    register.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    register.add_argument(
        "--project-id",
        help="Optional lowercase path-safe ID; generated from path when omitted",
    )
    register.add_argument("--name", help="Human-readable project name")
    register.add_argument(
        "--knowledge-root",
        help=(
            "Parent directory for curated project knowledge; the project ID is "
            "appended automatically"
        ),
    )
    register.add_argument(
        "--goal",
        "--final-goal",
        dest="final_goal",
        help="Initial final research goal",
    )
    register.add_argument("--current-stage", help="Initial research stage")
    register.add_argument(
        "--important-question",
        help="Most important question at first registration",
    )
    register.add_argument("--deadline", help="Optional ISO date (YYYY-MM-DD)")
    register.add_argument(
        "--daily-hours",
        dest="daily_available_hours",
        type=float,
        help="Available research time per day, greater than 0 and at most 24",
    )
    register.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    context = subparsers.add_parser(
        "context",
        help="Load path-free trusted project identity and onboarding context.",
    )
    context.add_argument("project_id", help="B-01 registered project ID")
    context.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    context.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    context_pack = subparsers.add_parser(
        "context-pack",
        help="Build a deterministic budget-bounded Host Context Pack.",
    )
    context_pack.add_argument("project_id", help="B-01 registered project ID")
    context_pack.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    context_pack.add_argument(
        "--max-bytes",
        type=int,
        default=HOST_CONTEXT_DEFAULT_MAX_BYTES,
        help=(
            "Maximum canonical compact UTF-8 JSON bytes "
            f"(default: {HOST_CONTEXT_DEFAULT_MAX_BYTES})"
        ),
    )
    context_pack.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    inventory = subparsers.add_parser(
        "inventory",
        help="Inventory, fingerprint, classify, and assign file state.",
    )
    inventory.add_argument("project_id", help="B-01 registered project ID")
    inventory.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    inventory.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Explicit B-02 include pattern; repeat for multiple patterns",
    )
    inventory.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Explicit B-02 exclude pattern; repeat for multiple patterns",
    )
    inventory.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Enable B-02 safe in-project symbolic-link following",
    )
    inventory.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    coverage = subparsers.add_parser(
        "coverage",
        help="Generate a deterministic B-08 coverage and failure report.",
    )
    coverage.add_argument("project_id", help="B-01 registered project ID")
    coverage.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    coverage.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    run = subparsers.add_parser(
        "run",
        help="Start, resume, or inspect persisted project-understanding runs.",
    )
    run_subparsers = run.add_subparsers(dest="run_command", required=True)
    run_start = run_subparsers.add_parser(
        "start",
        help="Create and execute a new logical run.",
    )
    run_start.add_argument("project_id", help="B-01 registered project ID")
    run_start.add_argument(
        "--through",
        dest="through_stage",
        choices=PROJECT_UNDERSTAND_STAGES,
        help="Pause after this canonical stage instead of running to the end",
    )
    run_start.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    run_start.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    run_resume = run_subparsers.add_parser(
        "resume",
        help="Resume a persisted run without repeating succeeded stages.",
    )
    run_resume.add_argument("project_id", help="B-01 registered project ID")
    run_resume.add_argument("run_id", help="Existing E-01 run ID")
    run_resume.add_argument(
        "--through",
        dest="through_stage",
        choices=PROJECT_UNDERSTAND_STAGES,
        help="Pause after this canonical stage instead of running to the end",
    )
    run_resume.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    run_resume.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    run_show = run_subparsers.add_parser(
        "show",
        help="Load a run report without executing any stage.",
    )
    run_show.add_argument("project_id", help="B-01 registered project ID")
    run_show.add_argument("run_id", help="Existing E-01 run ID")
    run_show.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    run_show.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )

    source = subparsers.add_parser(
        "source",
        help="Manage persistent project source identities and evidence.",
    )
    source_subparsers = source.add_subparsers(
        dest="source_command",
        required=True,
    )
    source_sync = source_subparsers.add_parser(
        "sync",
        help="Assign persistent IDs to current Manifest files.",
    )
    source_sync.add_argument("project_id", help="B-01 registered project ID")
    source_sync.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_sync.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    source_history = source_subparsers.add_parser(
        "history",
        help="Read deterministic path and content-version history for one source.",
    )
    source_history.add_argument("project_id", help="B-01 registered project ID")
    source_history.add_argument("source_id", help="Core-generated persistent source ID")
    source_history.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_history.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    source_health = source_subparsers.add_parser(
        "health",
        help="Classify all registered sources and persisted Evidence.",
    )
    source_health.add_argument("project_id", help="B-01 registered project ID")
    source_health.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_health.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    source_recover = source_subparsers.add_parser(
        "recover",
        help="Recover one unavailable source path without changing source identity.",
    )
    source_recover.add_argument("project_id", help="B-01 registered project ID")
    source_recover.add_argument("source_id", help="Core-generated persistent source ID")
    source_recover.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_recover.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    source_locate = source_subparsers.add_parser(
        "locate",
        help="Resolve one source ID to its current recorded path and version.",
    )
    source_locate.add_argument("project_id", help="B-01 registered project ID")
    source_locate.add_argument("source_id", help="Core-generated persistent source ID")
    source_locate.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_locate.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    source_open = source_subparsers.add_parser(
        "open",
        help="Reopen an exact current source locator or persisted Evidence.",
    )
    source_open.add_argument("project_id", help="B-01 registered project ID")
    source_open.add_argument(
        "target_id",
        help="A source ID (src-*) or persisted Evidence ID (evd-*)",
    )
    source_open.add_argument(
        "--locator-json",
        help="Strict C-01 locator JSON; required when target_id is a source ID",
    )
    source_open.add_argument(
        "--expected-content-hash",
        help="Optional lowercase SHA-256 expected for a direct source open",
    )
    source_open.add_argument(
        "--expected-excerpt-hash",
        help="Optional lowercase SHA-256 expected for a direct source excerpt",
    )
    source_open.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT),
        help="Research Core workspace (default: this repository)",
    )
    source_open.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result",
    )
    return parser


def _run_register(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).register(
            project_root=args.project_path,
            project_id=args.project_id,
            name=args.name,
            knowledge_root=args.knowledge_root,
            final_goal=args.final_goal,
            current_stage=args.current_stage,
            important_question=args.important_question,
            deadline=args.deadline,
            daily_available_hours=args.daily_available_hours,
        )
    except (LayoutError, OSError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {"ok": True, **result.as_dict()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    status = "registered" if result.created else "already registered"
    print(f"Project {status}: {result.record['name']}")
    print(f"Project ID:    {result.project_id}")
    print(f"Source root:   {result.project_root}")
    print(f"Machine state: {result.layout.machine_root}")
    print(f"Knowledge:     {result.layout.knowledge_root}")
    print(f"Record:        {result.project_file}")
    return 0


def _run_context(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).project_context(
            project_id=args.project_id,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    print(f"Project context:  {result.name}")
    print(f"Project ID:       {result.project_id}")
    print(f"Registered at:    {result.registered_at}")
    print(f"Git repository:   {result.git_is_repository}")
    print(f"Git branch:       {result.git_branch or '-'}")
    print(f"Git head:         {result.git_head_commit or '-'}")
    print(f"Final goal:       {result.final_goal or '-'}")
    print(f"Current stage:    {result.current_stage or '-'}")
    print(f"Key question:     {result.important_question or '-'}")
    print(f"Deadline:         {result.deadline or '-'}")
    hours = (
        str(result.daily_available_hours)
        if result.daily_available_hours is not None
        else "-"
    )
    print(f"Daily hours:      {hours}")
    return 0


def _run_context_pack(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).host_context_pack(
            project_id=args.project_id,
            max_bytes=args.max_bytes,
        )
    except (LayoutError, OSError, ValueError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    budget = payload["budget"]
    print(f"Host context:  {payload['project_id']}")
    print(f"Budget:        {budget['used_bytes']} / {budget['max_bytes']} bytes")
    print(f"Truncated:     {budget['truncated']}")
    print(f"Tasks:        {len(payload['tasks'])}")
    print(f"Risks:        {len(payload['risks'])}")
    print(f"Evidence refs:{len(payload['evidence_refs']):>4}")
    print(f"Omissions:    {len(payload['omissions'])}")
    return 0


def _run_inventory(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).scan(
            project_id=args.project_id,
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            follow_symlinks=args.follow_symlinks,
        )
    except (LayoutError, ScanPolicyError, OSError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {"ok": True, **result.as_dict()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Project inventoried: {result.project_id}")
    print(f"Source root:         {result.project_root}")
    print(f"Manifest:            {result.manifest_file}")
    print(f"Scan generation:     {result.scan_generation}")
    print(f"In-scope files:      {result.record_counts['file']}")
    print(f"Fingerprints hashed: {result.fingerprint_summary['hashed_files']}")
    print(f"Fingerprints reused: {result.fingerprint_summary['reused_files']}")
    print(f"Files classified:    {result.classification_summary['classified_files']}")
    print(f"Classifications reused: {result.classification_summary['reused_files']}")
    print(f"Files with state:     {result.file_state_summary['state_files']}")
    print(f"File states reused:   {result.file_state_summary['reused_files']}")
    print(f"Pruned directories:  {result.record_counts['excluded_directory']}")
    print(f"Total records:       {result.total_records}")
    return 0


def _run_coverage(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).coverage(
            project_id=args.project_id,
        )
    except (LayoutError, OSError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {"ok": True, **result.as_dict()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    totals = result.report["totals"]
    print(f"Coverage report generated: {result.project_id}")
    print(f"Manifest:                  {result.manifest_file}")
    print(f"Report:                    {result.report_file}")
    print(f"In-scope files:            {totals['file_count']}")
    print(f"In-scope bytes:            {totals['byte_count']}")
    print(f"Failed files:              {totals['failed_file_count']}")
    return 0


def _print_project_run_result(result: object, *, as_json: bool) -> int:
    payload = result.as_dict()
    if as_json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    run = payload["run"]
    print(f"Project run: {run['run_id']}")
    print(f"Project:     {run['project_id']}")
    print(f"Status:      {run['status']}")
    print(f"Revision:    {run['revision']}")
    print(f"Run file:    {payload['run_file']}")
    print("Stages:")
    for stage in run["stages"]:
        print(
            f"  {stage['ordinal'] + 1:>2}. {stage['stage_id']}: "
            f"{stage['status']} ({len(stage['attempts'])} attempt(s))"
        )
    return 0


def _run_project_run_start(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).project_run_start(
            project_id=args.project_id,
            through_stage=args.through_stage,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)
    return _print_project_run_result(result, as_json=args.json)


def _run_project_run_resume(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).project_run_resume(
            project_id=args.project_id,
            run_id=args.run_id,
            through_stage=args.through_stage,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)
    return _print_project_run_result(result, as_json=args.json)


def _run_project_run_show(args: argparse.Namespace) -> int:
    try:
        result = ResearchCoreService(args.workspace_root).project_run_status(
            project_id=args.project_id,
            run_id=args.run_id,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)
    return _print_project_run_result(result, as_json=args.json)


def _run_source_sync(args: argparse.Namespace) -> int:
    try:
        result = sync_source_registry(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
        )
    except (LayoutError, OSError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {"ok": True, **result.as_dict()}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Source identities synchronized: {result.project_id}")
    print(f"Manifest:            {result.manifest_file}")
    print(f"Source registry:     {result.sources_file}")
    print(f"Manifest generation: {result.scan_generation}")
    print(f"Manifest files:      {result.manifest_file_count}")
    print(f"New source IDs:      {result.assigned_count}")
    print(f"Registry sources:    {result.source_count}")
    print(f"Registry versions:   {result.version_count}")
    print(f"Versions added:      {result.versions_added_count}")
    print(f"Registry upgraded:   {result.upgraded_registry}")
    return 0


def _print_command_error(exc: BaseException, *, as_json: bool) -> int:
    if as_json:
        payload = {"ok": False, "error": str(exc)}
        reason_code = getattr(exc, "reason_code", None)
        if isinstance(reason_code, str) and reason_code:
            payload["reason_code"] = reason_code
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"error: {exc}", file=sys.stderr)
    return 2


def _run_source_health(args: argparse.Namespace) -> int:
    try:
        result = evaluate_source_health(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    source_counts = result.source_status_counts
    evidence_counts = result.evidence_status_counts
    print(f"Source health:       {result.project_id}")
    print(f"Overall status:      {result.overall_status}")
    print(f"Source registry:     {result.sources_file}")
    print(f"Evidence registry:   {result.evidence_file}")
    print(
        "Sources:             "
        f"{result.source_registry_count} "
        f"(valid={source_counts['valid']}, stale={source_counts['stale']}, "
        f"missing={source_counts['missing']}, "
        f"ambiguous={source_counts['ambiguous']})"
    )
    print(
        "Evidence:            "
        f"{result.evidence_registry_count} "
        f"(valid={evidence_counts['valid']}, stale={evidence_counts['stale']}, "
        f"missing={evidence_counts['missing']}, "
        f"ambiguous={evidence_counts['ambiguous']})"
    )
    print(f"Relocations written: {result.recovery_write_count}")
    issues = [
        *(record for record in result.sources if record.status != "valid"),
        *(record for record in result.evidence if record.status != "valid"),
    ]
    if issues:
        print("Non-valid records:")
        for record in issues:
            record_id = getattr(record, "evidence_id", None) or record.source_id
            print(
                f"  - {record_id}: {record.status} "
                f"({record.reason_code})"
            )
    return 0


def _run_source_recover(args: argparse.Namespace) -> int:
    try:
        result = recover_source(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
            source_id=args.source_id,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    print(f"Source recovery:     {result.source_id}")
    print(f"Status:              {result.status}")
    print(f"Method:              {result.recovery_method or '-'}")
    print(f"Previous path:       {result.previous_path}")
    print(f"Current path:        {result.current_path}")
    print(f"Candidate paths:     {len(result.candidate_paths)}")
    for candidate in result.candidate_paths:
        print(f"  - {candidate}")
    print(f"Registry updated:    {result.wrote_registry}")
    print(f"Reason:              {result.reason_code}")
    print(f"Detail:              {result.detail}")
    return 0


def _run_source_locate(args: argparse.Namespace) -> int:
    try:
        result = locate_source(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
            source_id=args.source_id,
        )
    except (LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    print(f"Source located:  {result.source_id}")
    print(f"Project:         {result.project_id}")
    print(f"Current path:    {result.current_path}")
    print(f"Absolute path:   {result.absolute_path}")
    print(f"Current version: {result.current_version}")
    print(f"Content SHA-256: {result.content_hash}")
    return 0


def _run_source_open(args: argparse.Namespace) -> int:
    try:
        if args.target_id.startswith("src-"):
            locator = (
                deserialize_locator(args.locator_json)
                if args.locator_json is not None
                else None
            )
        else:
            # Preserve R1 error priority: malformed locator JSON is irrelevant for
            # Evidence overrides and unknown target IDs, but override presence is not.
            locator = {} if args.locator_json is not None else None
        result = ResearchCoreService(args.workspace_root).source_open(
            project_id=args.project_id,
            target_id=args.target_id,
            locator=locator,
            expected_content_hash=args.expected_content_hash,
            expected_excerpt_hash=args.expected_excerpt_hash,
        )
    except (SourceAccessError, LayoutError, OSError) as exc:
        return _print_command_error(exc, as_json=args.json)

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    print(f"Source opened:    {result.source.source_id}")
    print(f"Current path:     {result.source.current_path}")
    print(f"Current version:  {result.source.current_version}")
    print(f"Content SHA-256:  {result.source.content_hash}")
    print(f"Excerpt SHA-256:  {result.excerpt_hash}")
    print(f"Excerpt format:   {result.excerpt_format}")
    if result.evidence_id is not None:
        print(f"Evidence:         {result.evidence_id}")
    print("--- excerpt ---")
    print(result.excerpt, end="" if result.excerpt.endswith("\n") else "\n")
    return 0


def _run_source_history(args: argparse.Namespace) -> int:
    try:
        result = get_source_history(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
            source_id=args.source_id,
        )
    except (LayoutError, OSError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = result.as_dict()
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2, ensure_ascii=False))
        return 0

    source = payload["source"]
    print(f"Source history:      {source['source_id']}")
    print(f"Project:             {result.project_id}")
    print(f"Source registry:     {result.sources_file}")
    print(f"Current path:        {source['current_path']}")
    print(f"Current version:     {source['current_version']}")
    print(f"Known paths:         {len(source['path_history'])}")
    print(f"Recorded versions:   {len(source['versions'])}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "register":
        return _run_register(args)
    if args.command == "context":
        return _run_context(args)
    if args.command == "context-pack":
        return _run_context_pack(args)
    if args.command == "inventory":
        return _run_inventory(args)
    if args.command == "coverage":
        return _run_coverage(args)
    if args.command == "run" and args.run_command == "start":
        return _run_project_run_start(args)
    if args.command == "run" and args.run_command == "resume":
        return _run_project_run_resume(args)
    if args.command == "run" and args.run_command == "show":
        return _run_project_run_show(args)
    if args.command == "source" and args.source_command == "sync":
        return _run_source_sync(args)
    if args.command == "source" and args.source_command == "history":
        return _run_source_history(args)
    if args.command == "source" and args.source_command == "health":
        return _run_source_health(args)
    if args.command == "source" and args.source_command == "recover":
        return _run_source_recover(args)
    if args.command == "source" and args.source_command == "locate":
        return _run_source_locate(args)
    if args.command == "source" and args.source_command == "open":
        return _run_source_open(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
