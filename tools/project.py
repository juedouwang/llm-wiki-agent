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
    from .coverage_report import generate_coverage_report
    from .project_inventory import inventory_project
    from .project_layout import LayoutError
    from .project_registry import register_project
    from .scan_policy import ScanPolicyConfig, ScanPolicyError
    from .source_registry import get_source_history, sync_source_registry
else:
    from coverage_report import generate_coverage_report  # type: ignore[no-redef]
    from project_inventory import inventory_project  # type: ignore[no-redef]
    from project_layout import LayoutError  # type: ignore[no-redef]
    from project_registry import register_project  # type: ignore[no-redef]
    from scan_policy import (  # type: ignore[no-redef]
        ScanPolicyConfig,
        ScanPolicyError,
    )
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
    return parser


def _run_register(args: argparse.Namespace) -> int:
    try:
        result = register_project(
            workspace_root=args.workspace_root,
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


def _run_inventory(args: argparse.Namespace) -> int:
    try:
        config = ScanPolicyConfig(
            include_patterns=tuple(args.include),
            exclude_patterns=tuple(args.exclude),
            follow_symlinks=args.follow_symlinks,
        )
        result = inventory_project(
            workspace_root=args.workspace_root,
            project_id=args.project_id,
            policy_config=config,
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
        result = generate_coverage_report(
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

    totals = result.report["totals"]
    print(f"Coverage report generated: {result.project_id}")
    print(f"Manifest:                  {result.manifest_file}")
    print(f"Report:                    {result.report_file}")
    print(f"In-scope files:            {totals['file_count']}")
    print(f"In-scope bytes:            {totals['byte_count']}")
    print(f"Failed files:              {totals['failed_file_count']}")
    return 0


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
    if args.command == "inventory":
        return _run_inventory(args)
    if args.command == "coverage":
        return _run_coverage(args)
    if args.command == "source" and args.source_command == "sync":
        return _run_source_sync(args)
    if args.command == "source" and args.source_command == "history":
        return _run_source_history(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
