#!/usr/bin/env python3
"""Local, read-only development supervision dashboard."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ASSET_ROOT = REPO_ROOT / "assets" / "development-dashboard"
BASE_BRANCH = "research-assistant"
SCHEMA_VERSION = 1
SNAPSHOT_KIND = "development-dashboard-snapshot"
LEDGER_KIND = "development-dashboard-progress-ledger"
UNIT_CATALOG_KIND = "development-dashboard-unit-catalog"
MAX_COMMITS = 800
MAX_FILES = 80
MAX_RECORDS = 500
MAX_UNITS = 240
MAX_UNIT_COMMITS = 30
MAX_UNIT_CHECKPOINTS = 30
MAX_UNIT_ARTIFACTS = 20
MAX_ARTIFACT_BYTES = 256 * 1024
TASK_ID_RE = re.compile(r"^[A-Z]-\d{2}$")
UNIT_ID_RE = re.compile(r"^[A-Z]-\d{2}[A-Z]?$")
WORK_REF_RE = re.compile(
    r"^(?P<kind>task|checkpoint)/(?P<task>[a-z]-\d{2})(?P<unit>[a-z]?)"
    r"(?:-(?P<slug>[a-z0-9][a-z0-9-]*))?$"
)
COMMIT_TASK_RE = re.compile(r"\(([a-z]-\d{2})\)", re.I)
MILESTONE_RE = re.compile(r"^###\s+(R(?:0(?:\.5)?|[1-6]))[\uFF1A:]\s*(.*?)\s*$", re.M)
INLINE_TASK_RE = re.compile(r"[A-Z]-\d{2}")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
PROGRESS_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{7,40})?$", re.I)
ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ARTIFACT_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
PROTECTED_ARTIFACT_COMPONENTS = {"raw", ".git", ".llmwiki"}
AREA_LABELS = {
    "A": "\u5de5\u7a0b\u57fa\u7ebf",
    "B": "\u9879\u76ee\u626b\u63cf",
    "C": "\u786e\u5b9a\u6027\u63d0\u53d6",
    "D": "\u6765\u6e90\u4e0e\u8bc1\u636e",
    "E": "\u9879\u76ee\u7406\u89e3",
    "F": "\u77e5\u8bc6\u8d44\u4ea7",
    "G": "Core \u4e0e\u67e5\u8be2",
    "H": "\u589e\u91cf\u7ef4\u62a4",
    "I": "\u76ee\u6807\u4e0e\u6267\u884c",
    "J": "\u7f51\u7ad9\u4e0e\u53d1\u5e03",
    "P": "\u4ea7\u54c1\u5951\u7ea6",
}
STATUS_LABELS = {
    "completed": "\u5df2\u5b8c\u6210",
    "partial": "\u90e8\u5206\u5b8c\u6210",
    "awaiting_review": "\u7b49\u5f85\u9a8c\u6536",
    "in_progress": "\u6267\u884c\u4e2d",
    "authorized": "\u5df2\u6388\u6743",
    "needs_changes": "\u9700\u8981\u4fee\u6539",
    "blocked": "\u963b\u585e",
    "not_started": "\u672a\u5f00\u59cb",
}
PROGRESS_STATUSES = {
    "authorized",
    "in_progress",
    "awaiting_review",
    "passed",
    "failed",
    "blocked",
}
CHECK_STATUSES = {"pending", "passed", "failed", "skipped"}
UNIT_ALIASES = {
    ("J-03", "dashboard-foundation"): (
        "J-03A",
        "\u5f00\u53d1\u76d1\u7763\u9a7e\u9a76\u8231\u57fa\u7840",
    ),
    ("J-03", "unit-visualization"): (
        "J-03B",
        "\u5355\u5143\u8bc1\u636e\u53ef\u89c6\u5316",
    ),
}


class DashboardError(RuntimeError):
    pass


class DashboardStateError(DashboardError):
    pass


class DashboardArtifactNotFound(DashboardError):
    pass


class DashboardArtifactTooLarge(DashboardError):
    pass


class DashboardArtifactEncodingError(DashboardError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_markdown(value: str) -> str:
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    return re.sub(r"\s+", " ", value.replace("**", "").replace("__", " ")).strip()


def split_row(line: str) -> list[str]:
    line = line.strip()
    return (
        [c.strip() for c in line[1:-1].split("|")]
        if line.startswith("|") and line.endswith("|")
        else []
    )


def dedupe(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def partial_completion(title: str, status: str) -> bool:
    text = f"{title} {status}".casefold()
    return any(
        marker in text
        for marker in (
            "slice",
            "prefix",
            "phase",
            "partial",
            "\u9636\u6bb5",
            "\u90e8\u5206",
            "\u57fa\u7840\u94fe\u8def",
        )
    )


def parse_roadmap(text: str) -> dict[str, Any]:
    definitions: dict[str, dict[str, Any]] = {}
    completions: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        cells = split_row(line)
        if not cells:
            continue
        match = re.fullmatch(r"([A-Z]-\d{2})\s+`(P[012])/(S|M|L)`", cells[0])
        if match and len(cells) >= 4:
            task_id, priority, size = match.groups()
            description = clean_markdown(cells[1])
            definitions[task_id] = {
                "id": task_id,
                "priority": priority,
                "size": size,
                "description": description,
                "acceptance": clean_markdown(cells[2]),
                "delta": clean_markdown(cells[3]),
            }
            continue
        match = re.match(r"^([A-Z]-\d{2})\s+(.+)$", cells[0])
        if match and len(cells) >= 4:
            task_id, title = match.groups()
            commits = re.findall(r"`([0-9a-fA-F]{7,40})`", cells[2])
            checkpoints = re.findall(r"`(checkpoint/[^`]+)`", cells[3])
            if commits or checkpoints:
                status = clean_markdown(cells[1])
                completions[task_id] = {
                    "id": task_id,
                    "title": clean_markdown(title),
                    "status_text": status,
                    "commits": commits,
                    "checkpoints": checkpoints,
                    "partial": partial_completion(title, status),
                }
    headings = list(MILESTONE_RE.finditer(text))
    milestones = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[match.end() : end]
        block = re.search(r"```(?:text)?\s*(.*?)```", section, re.S)
        task_ids = dedupe(INLINE_TASK_RE.findall(block.group(1))) if block else []
        milestone_id = match.group(1)
        title = clean_markdown(match.group(2))
        accepted = (
            "\u5df2\u5b8c\u6210" in title
            or "\u672c\u6b21\u5b8c\u6210" in title
            or re.search(
                rf"\b{re.escape(milestone_id)}\s+(?:was accepted|is complete)\b",
                section,
                re.I,
            )
            is not None
        )
        milestones.append(
            {
                "id": milestone_id,
                "title": title,
                "task_ids": task_ids,
                "accepted": accepted,
            }
        )
    order = []
    memberships: dict[str, list[str]] = defaultdict(list)
    for milestone in milestones:
        for task_id in milestone["task_ids"]:
            memberships[task_id].append(milestone["id"])
            if task_id not in order:
                order.append(task_id)
    for task_id in [*definitions, *completions]:
        if task_id not in order:
            order.append(task_id)
    tasks = {}
    for index, task_id in enumerate(order):
        definition = definitions.get(task_id, {})
        completion = completions.get(task_id, {})
        description = definition.get("description", "")
        title = (
            completion.get("title")
            or description.split("\uff1b", 1)[0].split("\u3002", 1)[0]
            or task_id
        )
        tasks[task_id] = {
            "id": task_id,
            "area": AREA_LABELS.get(task_id[0], task_id[0]),
            "priority": definition.get("priority"),
            "size": definition.get("size"),
            "title": title,
            "description": description,
            "acceptance": definition.get("acceptance", ""),
            "delta": definition.get("delta", ""),
            "milestones": memberships.get(task_id, []),
            "order": index,
            "completion": completion or None,
        }
    return {
        "tasks": tasks,
        "milestones": milestones,
        "completion_count": len(completions),
    }


def run_git(
    root: Path, args: Sequence[str], *, timeout: float = 10, allow_failure: bool = False
) -> str:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DashboardError("Git metadata is unavailable.") from exc
    if result.returncode and not allow_failure:
        raise DashboardError("Git metadata is unavailable.")
    return result.stdout.strip("\n")


def resolve_root(candidate: Path) -> Path:
    root = Path(
        run_git(candidate.resolve(), ["rev-parse", "--show-toplevel"])
    ).resolve()
    if not root.is_dir():
        raise DashboardError("Repository root is unavailable.")
    return root


def parse_numstat(text: str) -> list[dict[str, Any]]:
    files = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        binary = added == "-" or deleted == "-"
        files.append(
            {
                "path": path.replace("\\", "/"),
                "additions": None if binary else int(added),
                "deletions": None if binary else int(deleted),
                "binary": binary,
            }
        )
    return files


def aggregate_files(groups: Iterable[Iterable[Mapping[str, Any]]]) -> dict[str, Any]:
    by_path: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            path = str(item["path"])
            current = by_path.setdefault(
                path, {"path": path, "additions": 0, "deletions": 0, "binary": False}
            )
            if item.get("binary"):
                current.update({"binary": True, "additions": None, "deletions": None})
            elif not current["binary"]:
                current["additions"] += int(item.get("additions") or 0)
                current["deletions"] += int(item.get("deletions") or 0)
    ordered = sorted(by_path.values(), key=lambda item: item["path"].casefold())
    visible = ordered[:MAX_FILES]
    return {
        "file_count": len(ordered),
        "additions": sum(int(i["additions"] or 0) for i in ordered if not i["binary"]),
        "deletions": sum(int(i["deletions"] or 0) for i in ordered if not i["binary"]),
        "binary_files": sum(bool(i["binary"]) for i in ordered),
        "files": visible,
        "omitted_file_count": max(0, len(ordered) - len(visible)),
    }


def parse_git_log(text: str) -> list[dict[str, Any]]:
    commits = []
    for raw in text.split("\x1e"):
        lines = raw.strip("\r\n").splitlines()
        if not lines:
            continue
        header = lines[0].split("\x1f", 3)
        if len(header) != 4:
            continue
        full_hash, short_hash, committed_at, subject = header
        commits.append(
            {
                "hash": full_hash,
                "short_hash": short_hash,
                "committed_at": committed_at,
                "subject": subject,
                "files": parse_numstat("\n".join(lines[1:])),
                "task_ids": dedupe(m.upper() for m in COMMIT_TASK_RE.findall(subject)),
            }
        )
    return commits


def collect_tags(root: Path) -> dict[str, str]:
    direct = {}
    peeled = {}
    for line in run_git(
        root, ["show-ref", "--tags", "-d"], allow_failure=True
    ).splitlines():
        try:
            object_hash, reference = line.split(" ", 1)
        except ValueError:
            continue
        if not reference.startswith("refs/tags/"):
            continue
        name = reference[len("refs/tags/") :]
        if name.endswith("^{}"):
            peeled[name[:-3]] = object_hash
        else:
            direct[name] = object_hash
    return {name: peeled.get(name, value) for name, value in direct.items()}


def collect_git_state(root: Path) -> dict[str, Any]:
    branch = (
        run_git(
            root, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True
        )
        or "DETACHED"
    )
    head_parts = run_git(
        root, ["show", "-s", "--format=%H%x1f%h%x1f%s%x1f%cI", "HEAD"]
    ).split("\x1f", 3)
    if len(head_parts) != 4:
        raise DashboardError("Git HEAD metadata is malformed.")
    head = {
        "hash": head_parts[0],
        "short_hash": head_parts[1],
        "subject": head_parts[2],
        "committed_at": head_parts[3],
    }
    porcelain = run_git(root, ["status", "--porcelain=v1", "--untracked-files=normal"])
    entries = []
    for line in porcelain.splitlines():
        if len(line) >= 3:
            entries.append({"status": line[:2], "path": line[3:].replace("\\", "/")})
    commits = parse_git_log(
        run_git(
            root,
            [
                "log",
                "--all",
                f"--max-count={MAX_COMMITS}",
                "--date=iso-strict",
                "--format=%x1e%H%x1f%h%x1f%cI%x1f%s",
                "--numstat",
            ],
            timeout=20,
        )
    )
    tags = collect_tags(root)
    base_exists = bool(
        run_git(
            root,
            ["show-ref", "--verify", f"refs/heads/{BASE_BRANCH}"],
            allow_failure=True,
        )
    )
    ahead = behind = 0
    committed = []
    if base_exists:
        counts = run_git(
            root, ["rev-list", "--left-right", "--count", f"{BASE_BRANCH}...HEAD"]
        ).split()
        if len(counts) == 2:
            behind, ahead = map(int, counts)
        committed = parse_numstat(
            run_git(root, ["diff", "--numstat", f"{BASE_BRANCH}...HEAD"])
        )
    working = parse_numstat(run_git(root, ["diff", "--numstat", "HEAD"]))
    tracked = {i["path"] for i in working}
    untracked = [
        {"path": e["path"], "additions": None, "deletions": None, "binary": True}
        for e in entries
        if e["status"] == "??" and e["path"] not in tracked
    ]
    return {
        "repository_name": root.name,
        "branch": branch,
        "head": head,
        "clean": not entries,
        "status_entries": entries[:MAX_FILES],
        "omitted_status_count": max(0, len(entries) - MAX_FILES),
        "base_branch": BASE_BRANCH if base_exists else None,
        "ahead": ahead,
        "behind": behind,
        "current_changes": aggregate_files([committed, working, untracked]),
        "commits": commits,
        "tags": tags,
    }


def validate_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DashboardStateError(f"{name} must be a string.")
    value = value.strip()
    if len(value) > limit:
        raise DashboardStateError(f"{name} exceeds its size limit.")
    return value


def validate_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise DashboardStateError(f"{name} contains unsupported fields.")


def task_id_for_unit(unit_id: str) -> str:
    if not UNIT_ID_RE.fullmatch(unit_id):
        raise DashboardStateError("Unit identifier is invalid.")
    return unit_id[:4]


def validate_artifact_path(value: Any) -> str:
    path = validate_text(value, "artifact.path", 320)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or "\x00" in path
    ):
        raise DashboardStateError("Artifact path is not a safe repository path.")
    parts = path.split("/")
    folded = [part.casefold() for part in parts]
    if any(part in {"", ".", ".."} for part in parts) or any(
        part in PROTECTED_ARTIFACT_COMPONENTS for part in folded
    ):
        raise DashboardStateError("Artifact path is protected or non-canonical.")
    return "/".join(parts)


def artifact_token(unit_id: str, artifact: Mapping[str, Any]) -> str:
    material = "\x00".join(
        (
            str(SCHEMA_VERSION),
            unit_id,
            str(artifact["artifact_id"]),
            str(artifact["commit"]),
            str(artifact["path"]),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_unit_catalog(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DashboardStateError("unit_catalog must be an object.")
    validate_keys(payload, {"schema_version", "kind", "units"}, "unit_catalog")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DashboardStateError("Unsupported unit_catalog schema_version.")
    if payload.get("kind") != UNIT_CATALOG_KIND:
        raise DashboardStateError("Unexpected unit_catalog kind.")
    units_payload = payload.get("units")
    if not isinstance(units_payload, list) or len(units_payload) > MAX_UNITS:
        raise DashboardStateError("unit_catalog units are invalid.")

    units = []
    seen_units: set[str] = set()
    seen_tokens: set[str] = set()
    for unit in units_payload:
        if not isinstance(unit, dict):
            raise DashboardStateError("Catalog unit must be an object.")
        validate_keys(
            unit,
            {
                "task_id",
                "unit_id",
                "title",
                "summary",
                "commits",
                "checkpoints",
                "artifacts",
            },
            "catalog unit",
        )
        task_id = validate_text(unit.get("task_id"), "unit.task_id", 8).upper()
        unit_id = validate_text(unit.get("unit_id"), "unit.unit_id", 8).upper()
        if (
            not TASK_ID_RE.fullmatch(task_id)
            or not UNIT_ID_RE.fullmatch(unit_id)
            or task_id_for_unit(unit_id) != task_id
            or unit_id in seen_units
        ):
            raise DashboardStateError("Catalog task or unit identifier is invalid.")
        seen_units.add(unit_id)

        commits_payload = unit.get("commits", [])
        if (
            not isinstance(commits_payload, list)
            or len(commits_payload) > MAX_UNIT_COMMITS
        ):
            raise DashboardStateError("Catalog unit commits are invalid.")
        commits = []
        for value in commits_payload:
            commit = validate_text(value, "unit.commit", 40).lower()
            if not GIT_COMMIT_RE.fullmatch(commit):
                raise DashboardStateError(
                    "Catalog commits must be explicit full Git commit hashes."
                )
            commits.append(commit)
        commits = dedupe(commits)

        checkpoints_payload = unit.get("checkpoints", [])
        if (
            not isinstance(checkpoints_payload, list)
            or len(checkpoints_payload) > MAX_UNIT_CHECKPOINTS
        ):
            raise DashboardStateError("Catalog unit checkpoints are invalid.")
        checkpoints = []
        for value in checkpoints_payload:
            checkpoint = validate_text(value, "unit.checkpoint", 160)
            parsed = work_unit(checkpoint, expected_kind="checkpoint")
            if (
                not parsed
                or parsed["task_id"] != task_id
                or parsed["unit_id"] != unit_id
            ):
                raise DashboardStateError(
                    "Catalog checkpoint does not identify its declared unit."
                )
            checkpoints.append(checkpoint)
        checkpoints = dedupe(checkpoints)

        artifacts_payload = unit.get("artifacts", [])
        if (
            not isinstance(artifacts_payload, list)
            or len(artifacts_payload) > MAX_UNIT_ARTIFACTS
        ):
            raise DashboardStateError("Catalog unit artifacts are invalid.")
        artifacts = []
        seen_artifacts: set[str] = set()
        for artifact in artifacts_payload:
            if not isinstance(artifact, dict):
                raise DashboardStateError("Catalog artifact must be an object.")
            validate_keys(
                artifact,
                {"artifact_id", "label", "description", "path", "commit"},
                "catalog artifact",
            )
            artifact_id = validate_text(
                artifact.get("artifact_id"), "artifact.artifact_id", 64
            )
            if (
                not ARTIFACT_ID_RE.fullmatch(artifact_id)
                or artifact_id in seen_artifacts
            ):
                raise DashboardStateError("Catalog artifact identifier is invalid.")
            seen_artifacts.add(artifact_id)
            commit = validate_text(
                artifact.get("commit"), "artifact.commit", 40
            ).lower()
            if not GIT_COMMIT_RE.fullmatch(commit) or commit not in commits:
                raise DashboardStateError(
                    "Catalog artifact commit must be one of the unit's explicit commits."
                )
            validated_artifact = {
                "artifact_id": artifact_id,
                "label": validate_text(
                    artifact.get("label", artifact_id), "artifact.label", 120
                ),
                "description": validate_text(
                    artifact.get("description", ""), "artifact.description", 300
                ),
                "path": validate_artifact_path(artifact.get("path")),
                "commit": commit,
            }
            token = artifact_token(unit_id, validated_artifact)
            if token in seen_tokens:
                raise DashboardStateError("Catalog artifact route collision detected.")
            seen_tokens.add(token)
            artifacts.append(validated_artifact)

        units.append(
            {
                "task_id": task_id,
                "unit_id": unit_id,
                "title": validate_text(unit.get("title", unit_id), "unit.title", 160),
                "summary": validate_text(unit.get("summary", ""), "unit.summary", 500),
                "commits": commits,
                "checkpoints": checkpoints,
                "artifacts": artifacts,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": UNIT_CATALOG_KIND,
        "units": units,
    }


def validate_ledger(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise DashboardStateError("Unsupported progress ledger schema_version.")
    validate_keys(
        payload,
        {"schema_version", "kind", "records", "unit_catalog"},
        "progress ledger",
    )
    if payload.get("kind") != LEDGER_KIND:
        raise DashboardStateError("Unexpected progress ledger kind.")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise DashboardStateError("Progress ledger records are invalid.")
    validated = []
    seen_record_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise DashboardStateError("Progress record must be an object.")
        task_id = validate_text(record.get("task_id"), "task_id", 8).upper()
        unit_id = validate_text(record.get("unit_id"), "unit_id", 8).upper()
        status = validate_text(record.get("status"), "status", 32)
        if (
            not TASK_ID_RE.fullmatch(task_id)
            or not UNIT_ID_RE.fullmatch(unit_id)
            or task_id_for_unit(unit_id) != task_id
        ):
            raise DashboardStateError("Progress record task identifiers are invalid.")
        if status not in PROGRESS_STATUSES:
            raise DashboardStateError("Progress record status is invalid.")
        checks_payload = record.get("checks", [])
        if not isinstance(checks_payload, list) or len(checks_payload) > 30:
            raise DashboardStateError("Progress record checks are invalid.")
        checks = []
        for check in checks_payload:
            if not isinstance(check, dict):
                raise DashboardStateError("Progress check must be an object.")
            check_id = validate_text(check.get("id"), "check.id", 64)
            check_status = validate_text(check.get("status"), "check.status", 16)
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9-]*", check_id)
                or check_status not in CHECK_STATUSES
            ):
                raise DashboardStateError("Progress check is invalid.")
            checks.append(
                {
                    "id": check_id,
                    "status": check_status,
                    "summary": validate_text(
                        check.get("summary", ""), "check.summary", 300
                    ),
                }
            )
        record_id = validate_text(record.get("record_id"), "record_id", 80)
        if not record_id or record_id in seen_record_ids:
            raise DashboardStateError("Progress record_id is invalid or duplicated.")
        seen_record_ids.add(record_id)
        commit = validate_text(record.get("commit", ""), "commit", 40).lower()
        if not PROGRESS_COMMIT_RE.fullmatch(commit):
            raise DashboardStateError("Progress record commit is invalid.")
        validated.append(
            {
                "record_id": record_id,
                "recorded_at": validate_text(
                    record.get("recorded_at"), "recorded_at", 40
                ),
                "task_id": task_id,
                "unit_id": unit_id,
                "status": status,
                "summary": validate_text(record.get("summary", ""), "summary", 500),
                "branch": validate_text(record.get("branch", ""), "branch", 160),
                "commit": commit,
                "checks": checks,
            }
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "records": validated,
    }
    if "unit_catalog" in payload:
        result["unit_catalog"] = validate_unit_catalog(payload["unit_catalog"])
    return result


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "kind": LEDGER_KIND, "records": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardStateError("Progress ledger cannot be read safely.") from exc
    return validate_ledger(payload)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def append_record(
    path: Path,
    *,
    task_id: str,
    unit_id: str,
    status: str,
    summary: str,
    branch: str,
    commit: str,
    checks: Sequence[Mapping[str, str]] = (),
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    task_id = task_id.upper()
    unit_id = unit_id.upper()
    if not TASK_ID_RE.fullmatch(task_id) or not UNIT_ID_RE.fullmatch(unit_id):
        raise DashboardStateError("Task or unit identifier is invalid.")
    if status not in PROGRESS_STATUSES:
        raise DashboardStateError("Progress status is invalid.")
    ledger = load_ledger(path)
    record = {
        "record_id": f"{unit_id.lower()}-{uuid.uuid4().hex}",
        "recorded_at": isoformat(recorded_at or utc_now()),
        "task_id": task_id,
        "unit_id": unit_id,
        "status": status,
        "summary": summary,
        "branch": branch,
        "commit": commit,
        "checks": [dict(c) for c in checks],
    }
    candidate_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "records": [*ledger["records"], record],
    }
    if "unit_catalog" in ledger:
        candidate_payload["unit_catalog"] = ledger["unit_catalog"]
    candidate = validate_ledger(candidate_payload)
    atomic_json(path, candidate)
    return candidate["records"][-1]


def work_unit(value: str, *, expected_kind: str | None = None) -> dict[str, str] | None:
    match = WORK_REF_RE.fullmatch(value)
    if not match or (expected_kind and match.group("kind") != expected_kind):
        return None
    task_id = match.group("task").upper()
    suffix = match.group("unit").upper()
    slug = match.group("slug") or ""
    alias = UNIT_ALIASES.get((task_id, slug)) if not suffix else None
    unit_id, title = (
        alias
        if alias
        else (
            f"{task_id}{suffix}" if suffix else task_id,
            slug.replace("-", " ").strip() or f"{task_id}{suffix}",
        )
    )
    return {
        "kind": match.group("kind"),
        "task_id": task_id,
        "unit_id": unit_id,
        "slug": slug,
        "title": title,
    }


def checkpoint_task(tag: str) -> str | None:
    parsed = work_unit(tag, expected_kind="checkpoint")
    return parsed["task_id"] if parsed else None


def checkpoint_unit(tag: str) -> dict[str, str] | None:
    return work_unit(tag, expected_kind="checkpoint")


def branch_unit(branch: str) -> dict[str, str] | None:
    parsed = work_unit(branch, expected_kind="task")
    if not parsed:
        return None
    return {key: parsed[key] for key in ("task_id", "unit_id", "slug", "title")}


def active_status(git: Mapping[str, Any], progress: Mapping[str, Any] | None) -> str:
    if progress and progress.get("status") == "failed":
        return "needs_changes"
    if progress and progress.get("status") == "blocked":
        return "blocked"
    if progress and progress.get("status") == "awaiting_review":
        return "awaiting_review"
    if not git["clean"]:
        return "in_progress"
    if int(git["ahead"]) > 0:
        return "awaiting_review"
    if progress and progress.get("status") == "in_progress":
        return "in_progress"
    return "authorized"


def task_commits(
    task_id: str,
    completion: Mapping[str, Any] | None,
    commits: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    explicit = set(completion.get("commits", []) if completion else [])
    selected = []
    for commit in commits:
        full = str(commit["hash"])
        short = str(commit["short_hash"])
        if task_id in commit["task_ids"] or any(
            full.startswith(value) or short.startswith(value) for value in explicit
        ):
            selected.append(dict(commit))
    return selected[:20]


def task_checkpoints(
    task_id: str,
    completion: Mapping[str, Any] | None,
    tags: Mapping[str, str],
    commit_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    names = [name for name in tags if checkpoint_task(name) == task_id]
    if completion:
        names.extend(completion.get("checkpoints", []))
    records = []
    for name in dedupe(names):
        commit_hash = tags.get(name, "")
        commit = commit_index.get(commit_hash, {})
        parsed = checkpoint_unit(name)
        records.append(
            {
                "name": name,
                "task_id": parsed["task_id"] if parsed else task_id,
                "unit_id": parsed["unit_id"] if parsed else None,
                "commit": commit_hash[:12],
                "committed_at": commit.get("committed_at", ""),
                "subject": commit.get("subject", ""),
            }
        )
    return sorted(
        records, key=lambda item: (item["committed_at"], item["name"]), reverse=True
    )


def unit_display_status(
    latest: Mapping[str, Any] | None,
    checkpoints: Sequence[Mapping[str, Any]],
    active: Mapping[str, Any] | None,
    unit_id: str,
) -> str:
    if active and active.get("unit_id") == unit_id:
        return str(active["status"])
    if latest:
        return {
            "passed": "completed",
            "failed": "needs_changes",
        }.get(str(latest["status"]), str(latest["status"]))
    return "partial" if checkpoints else "not_started"


def resolve_explicit_commits(
    references: Sequence[tuple[str, str]],
    commits: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for reference, source in references:
        if not reference:
            continue
        matches = [
            commit
            for commit in commits
            if str(commit["hash"]).startswith(reference)
            or str(commit["short_hash"]).startswith(reference)
        ]
        commit = matches[0] if len(matches) == 1 else None
        key = str(commit["hash"]) if commit else reference
        if key not in resolved:
            resolved[key] = {
                "commit": key,
                "short_hash": str(commit["short_hash"]) if commit else reference[:12],
                "committed_at": str(commit.get("committed_at", "")) if commit else "",
                "subject": str(commit.get("subject", "")) if commit else "",
                "sources": [],
            }
            order.append(key)
        if source not in resolved[key]["sources"]:
            resolved[key]["sources"].append(source)
    return [resolved[key] for key in order]


def public_artifact(unit_id: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    token = artifact_token(unit_id, artifact)
    route = f"/api/artifacts/{token}"
    return {
        "artifact_id": artifact["artifact_id"],
        "label": artifact["label"],
        "description": artifact["description"],
        "path": artifact["path"],
        "commit": str(artifact["commit"])[:12],
        "route": route,
        "url": route,
        "byte_limit": MAX_ARTIFACT_BYTES,
    }


def build_unit_payloads(
    ledger: Mapping[str, Any],
    git: Mapping[str, Any],
    active: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    declarations = {
        unit["unit_id"]: unit
        for unit in ledger.get("unit_catalog", {}).get("units", [])
    }
    records_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    task_ids: dict[str, str] = {}
    titles: dict[str, str] = {}

    def remember(unit_id: str, task_id: str, title: str = "") -> None:
        if unit_id not in task_ids:
            order.append(unit_id)
            task_ids[unit_id] = task_id
        if title and unit_id not in titles:
            titles[unit_id] = title

    for declaration in declarations.values():
        remember(
            str(declaration["unit_id"]),
            str(declaration["task_id"]),
            str(declaration["title"]),
        )
    for record in ledger["records"]:
        unit_id = str(record["unit_id"])
        remember(unit_id, str(record["task_id"]))
        records_by_unit[unit_id].append(dict(record))
    checkpoint_names: dict[str, list[str]] = defaultdict(list)
    for name in git["tags"]:
        parsed = checkpoint_unit(name)
        if not parsed:
            continue
        remember(parsed["unit_id"], parsed["task_id"], parsed["title"])
        checkpoint_names[parsed["unit_id"]].append(name)
    if active:
        remember(
            str(active["unit_id"]),
            str(active["task_id"]),
            str(active.get("title", "")),
        )

    commit_index = {str(commit["hash"]): commit for commit in git["commits"]}
    payloads = []
    for unit_id in order:
        declaration = declarations.get(unit_id)
        records = records_by_unit.get(unit_id, [])
        catalog_checkpoints = declaration["checkpoints"] if declaration else []
        names = dedupe([*catalog_checkpoints, *checkpoint_names.get(unit_id, [])])
        checkpoints = []
        for name in names:
            commit_hash = str(git["tags"].get(name, ""))
            commit = commit_index.get(commit_hash, {})
            checkpoints.append(
                {
                    "name": name,
                    "task_id": task_ids[unit_id],
                    "unit_id": unit_id,
                    "commit": commit_hash[:12],
                    "committed_at": commit.get("committed_at", ""),
                    "subject": commit.get("subject", ""),
                    "catalog_declared": name in catalog_checkpoints,
                }
            )
        checkpoints.sort(
            key=lambda item: (item["committed_at"], item["name"]), reverse=True
        )
        references = []
        if declaration:
            references.extend((commit, "catalog") for commit in declaration["commits"])
        references.extend(
            (str(record["commit"]), "record")
            for record in records
            if record.get("commit")
        )
        commits = resolve_explicit_commits(references, git["commits"])
        checks = []
        for record in records:
            for check in record["checks"]:
                checks.append(
                    {
                        **check,
                        "record_id": record["record_id"],
                        "recorded_at": record["recorded_at"],
                    }
                )
        artifacts = (
            [
                public_artifact(unit_id, artifact)
                for artifact in declaration["artifacts"]
            ]
            if declaration
            else []
        )
        latest = records[-1] if records else None
        status = unit_display_status(latest, checkpoints, active, unit_id)
        summary = (
            str(declaration["summary"])
            if declaration and declaration["summary"]
            else str(latest.get("summary", ""))
            if latest
            else ""
        )
        payloads.append(
            {
                "task_id": task_ids[unit_id],
                "unit_id": unit_id,
                "title": (
                    str(declaration["title"])
                    if declaration
                    else titles.get(unit_id, unit_id)
                ),
                "summary": summary,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "catalog_declared": declaration is not None,
                "latest_record": latest,
                "records": records,
                "checks": checks,
                "commits": commits,
                "explicit_commits": commits,
                "checkpoints": checkpoints,
                "artifacts": artifacts,
            }
        )
    return payloads


def artifact_declaration_index(ledger: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for unit in ledger.get("unit_catalog", {}).get("units", []):
        for artifact in unit["artifacts"]:
            token = artifact_token(str(unit["unit_id"]), artifact)
            result[token] = {
                "task_id": unit["task_id"],
                "unit_id": unit["unit_id"],
                **artifact,
            }
    return result


def run_git_bytes(root: Path, args: Sequence[str], *, timeout: float = 10) -> bytes:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DashboardArtifactNotFound("Artifact Git object is unavailable.") from exc
    if result.returncode:
        raise DashboardArtifactNotFound("Artifact Git object is unavailable.")
    return result.stdout


def read_git_text_blob(root: Path, artifact: Mapping[str, Any]) -> bytes:
    commit = str(artifact["commit"])
    path = str(artifact["path"])
    try:
        commit_type = run_git(root, ["cat-file", "-t", commit], allow_failure=True)
    except DashboardError as exc:
        raise DashboardArtifactNotFound("Artifact commit is unavailable.") from exc
    if commit_type != "commit":
        raise DashboardArtifactNotFound("Artifact commit is unavailable.")
    tree = run_git_bytes(root, ["ls-tree", "-z", "--full-tree", commit, "--", path])
    matching: list[tuple[str, str, str]] = []
    for raw_entry in tree.split(b"\x00"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        if not separator:
            continue
        try:
            returned_path = raw_path.decode("utf-8", errors="strict")
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        except (UnicodeError, ValueError):
            continue
        if returned_path == path:
            matching.append((mode, object_type, object_id))
    if len(matching) != 1:
        raise DashboardArtifactNotFound("Artifact blob is unavailable.")
    mode, object_type, object_id = matching[0]
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise DashboardArtifactNotFound("Artifact is not a regular Git blob.")
    try:
        size = int(run_git(root, ["cat-file", "-s", object_id]))
    except (DashboardError, ValueError) as exc:
        raise DashboardArtifactNotFound("Artifact size is unavailable.") from exc
    if size < 0 or size > MAX_ARTIFACT_BYTES:
        raise DashboardArtifactTooLarge("Artifact exceeds the preview limit.")
    body = run_git_bytes(root, ["cat-file", "blob", object_id])
    if len(body) != size or len(body) > MAX_ARTIFACT_BYTES:
        raise DashboardArtifactTooLarge("Artifact exceeds the preview limit.")
    try:
        body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DashboardArtifactEncodingError(
            "Artifact is not strict UTF-8 text."
        ) from exc
    return body


class DashboardSnapshotBuilder:
    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        *,
        roadmap_path: Path | None = None,
        state_path: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.repo_root = resolve_root(repo_root)
        self.roadmap_path = (
            roadmap_path or self.repo_root / "docs" / "research-assistant-roadmap.md"
        ).resolve()
        machine_root = (self.repo_root / ".llmwiki").resolve()
        try:
            machine_root.relative_to(self.repo_root)
        except ValueError as exc:
            raise DashboardStateError(
                "Repository .llmwiki directory must not resolve outside the repository."
            ) from exc
        state_candidate = (
            state_path or machine_root / "development-dashboard" / "progress.json"
        )
        if not state_candidate.is_absolute():
            state_candidate = self.repo_root / state_candidate
        self.state_path = state_candidate.resolve()
        try:
            relative_state = self.state_path.relative_to(machine_root)
        except ValueError as exc:
            raise DashboardStateError(
                "Progress ledger must stay within the repository .llmwiki directory."
            ) from exc
        if relative_state == Path("."):
            raise DashboardStateError("Progress ledger path must name a file.")
        self.clock = clock
        self._cache_lock = threading.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    def read_artifact(self, token: str) -> tuple[dict[str, Any], bytes]:
        if not ARTIFACT_TOKEN_RE.fullmatch(token):
            raise DashboardArtifactNotFound("Artifact route is not allowlisted.")
        ledger = load_ledger(self.state_path)
        artifact = artifact_declaration_index(ledger).get(token)
        if not artifact:
            raise DashboardArtifactNotFound("Artifact route is not allowlisted.")
        body = read_git_text_blob(self.repo_root, artifact)
        return public_artifact(str(artifact["unit_id"]), artifact), body

    def build(self, *, use_cache: bool = True) -> dict[str, Any]:
        now = self.clock()
        key = now.timestamp()
        with self._cache_lock:
            if use_cache and self._cache and key - self._cache[0] < 1:
                return self._cache[1]
        try:
            roadmap = parse_roadmap(self.roadmap_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise DashboardError("Roadmap cannot be read safely.") from exc
        git = collect_git_state(self.repo_root)
        ledger = load_ledger(self.state_path)
        latest = {record["unit_id"]: record for record in ledger["records"]}
        catalog = {
            unit["unit_id"]: unit
            for unit in ledger.get("unit_catalog", {}).get("units", [])
        }
        commit_index = {commit["hash"]: commit for commit in git["commits"]}
        active = branch_unit(git["branch"])
        if active:
            branch_records = [
                record
                for record in ledger["records"]
                if record["branch"] == git["branch"]
                and record["task_id"] == active["task_id"]
            ]
            if branch_records:
                active["unit_id"] = branch_records[-1]["unit_id"]
            declaration = catalog.get(active["unit_id"])
            if declaration:
                active["title"] = declaration["title"]
            progress = latest.get(active["unit_id"])
            active = {
                **active,
                "status": active_status(git, progress),
                "status_label": "",
                "progress": progress,
            }
            active["status_label"] = STATUS_LABELS[active["status"]]

        units = build_unit_payloads(ledger, git, active)
        units_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for unit in units:
            units_by_task[unit["task_id"]].append(unit)

        payloads = []
        statuses = {}
        for task_id, task in roadmap["tasks"].items():
            completion = task["completion"]
            checkpoints = task_checkpoints(
                task_id, completion, git["tags"], commit_index
            )
            status = (
                ("partial" if completion["partial"] else "completed")
                if completion
                else ("partial" if checkpoints else "not_started")
            )
            if active and active["task_id"] == task_id:
                status = active["status"]
            commits = task_commits(task_id, completion, git["commits"])
            changes = aggregate_files(commit["files"] for commit in commits)
            statuses[task_id] = status
            payloads.append(
                {
                    **{k: v for k, v in task.items() if k != "completion"},
                    "status": status,
                    "status_label": STATUS_LABELS[status],
                    "completion": completion,
                    "checkpoints": checkpoints,
                    "commits": [
                        {
                            k: v
                            for k, v in commit.items()
                            if k not in {"files", "task_ids", "hash"}
                        }
                        for commit in commits
                    ],
                    "changes": changes,
                    "units": units_by_task.get(task_id, []),
                }
            )
        milestone_payloads = []
        for milestone in roadmap["milestones"]:
            values = [
                statuses.get(task_id, "not_started")
                for task_id in milestone["task_ids"]
            ]
            completed = sum(v == "completed" for v in values)
            partial = sum(v == "partial" for v in values)
            active_count = sum(
                v
                in {
                    "authorized",
                    "in_progress",
                    "awaiting_review",
                    "needs_changes",
                    "blocked",
                }
                for v in values
            )
            if milestone["accepted"]:
                status = "completed"
                progress = 100
            elif active_count or completed or partial:
                status = "in_progress"
                progress = round(
                    100 * (completed + 0.5 * partial) / max(1, len(values))
                )
            else:
                status = "not_started"
                progress = 0
            milestone_payloads.append(
                {
                    **milestone,
                    "status": status,
                    "status_label": STATUS_LABELS[status],
                    "progress_percent": progress,
                    "completed_count": completed,
                    "partial_count": partial,
                    "active_count": active_count,
                    "task_count": len(values),
                }
            )
        counts = {status: 0 for status in STATUS_LABELS}
        for status in statuses.values():
            counts[status] += 1
        checkpoints = []
        for name, commit_hash in git["tags"].items():
            commit = commit_index.get(commit_hash, {})
            parsed = checkpoint_unit(name)
            checkpoints.append(
                {
                    "name": name,
                    "task_id": parsed["task_id"] if parsed else None,
                    "unit_id": parsed["unit_id"] if parsed else None,
                    "commit": commit_hash[:12],
                    "committed_at": commit.get("committed_at", ""),
                    "subject": commit.get("subject", ""),
                }
            )
        checkpoints.sort(
            key=lambda item: (item["committed_at"], item["name"]), reverse=True
        )
        public_git = {k: v for k, v in git.items() if k not in {"commits", "tags"}}
        unit_groups = [
            {
                "task_id": task_id,
                "units": grouped,
            }
            for task_id, grouped in units_by_task.items()
        ]
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "kind": SNAPSHOT_KIND,
            "generated_at": isoformat(now),
            "repository": public_git,
            "summary": {
                "task_count": len(payloads),
                "counts": counts,
                "checkpoint_count": len(checkpoints),
                "unit_count": len(units),
                "artifact_count": sum(len(unit["artifacts"]) for unit in units),
                "accepted_milestone_count": sum(
                    m["accepted"] for m in milestone_payloads
                ),
                "milestone_count": len(milestone_payloads),
            },
            "active_unit": active,
            "milestones": milestone_payloads,
            "tasks": sorted(payloads, key=lambda item: item["order"]),
            "units": units,
            "unit_groups": unit_groups,
            "checkpoints": checkpoints[:80],
            "progress_records": ledger["records"][-100:],
        }
        serialized = json.dumps(snapshot, ensure_ascii=False)
        if str(self.repo_root) in serialized:
            raise DashboardError("Snapshot contains a forbidden absolute path.")
        with self._cache_lock:
            self._cache = (key, snapshot)
        return snapshot


def host_is_loopback(host: str) -> bool:
    host = host.strip().strip("[]")
    if not host:
        return False
    if host.casefold() == "localhost":
        try:
            addresses = socket.getaddrinfo(host, None)
        except OSError:
            return False
        return bool(addresses) and all(
            ipaddress.ip_address(address[4][0]).is_loopback for address in addresses
        )
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str) -> str:
    if not host_is_loopback(host):
        raise DashboardError("Dashboard host must resolve only to loopback.")
    return host


def request_host_allowed(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip()
    if not value or any(char in value for char in ("@", "/", "\\", "?", "#")):
        return False
    if any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return bool(hostname) and host_is_loopback(hostname)


def security_headers(handler: BaseHTTPRequestHandler, *, api: bool) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    )
    handler.send_header("Cache-Control", "no-store" if api else "no-cache")


def make_handler(
    builder: DashboardSnapshotBuilder, asset_root: Path
) -> type[BaseHTTPRequestHandler]:
    asset_root = asset_root.resolve()
    routes = {
        "/": (asset_root / "index.html", "text/html; charset=utf-8"),
        "/index.html": (asset_root / "index.html", "text/html; charset=utf-8"),
        "/assets/app.js": (asset_root / "app.js", "text/javascript; charset=utf-8"),
        "/assets/styles.css": (asset_root / "styles.css", "text/css; charset=utf-8"),
        "/assets/favicon.svg": (
            asset_root / "favicon.svg",
            "image/svg+xml; charset=utf-8",
        ),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "LLMWikiDashboard"
        sys_version = ""

        def version_string(self) -> str:
            return self.server_version

        def send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            api: bool,
            head: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            security_headers(self, api=api)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def send_json(
            self, status: int, payload: Mapping[str, Any], *, head: bool = False
        ) -> None:
            self.send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                api=True,
                head=head,
            )

        def allowed(self) -> bool:
            if request_host_allowed(self.headers.get("Host")):
                return True
            self.send_json(
                421,
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": "misdirected_request",
                    "message": "Only loopback Host headers are accepted.",
                },
            )
            return False

        def serve(self, *, head: bool = False) -> None:
            if not self.allowed():
                return
            parsed_request = urlsplit(self.path)
            path = parsed_request.path
            if path == "/api/health":
                self.send_json(
                    200,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "kind": "development-dashboard-health",
                        "status": "ok",
                        "read_only": True,
                        "loopback_only": True,
                    },
                    head=head,
                )
                return
            if path == "/api/status":
                try:
                    snapshot = builder.build()
                except DashboardError:
                    self.send_json(
                        500,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "error": "snapshot_unavailable",
                            "message": "Dashboard status is temporarily unavailable.",
                        },
                        head=head,
                    )
                    return
                self.send_json(200, snapshot, head=head)
                return
            artifact_match = re.fullmatch(r"/api/artifacts/([0-9a-f]{64})", path)
            if artifact_match:
                reader = getattr(builder, "read_artifact", None)
                if parsed_request.query or reader is None:
                    self.send_json(
                        404,
                        {"schema_version": SCHEMA_VERSION, "error": "not_found"},
                        head=head,
                    )
                    return
                try:
                    _, body = reader(artifact_match.group(1))
                except DashboardArtifactTooLarge:
                    self.send_json(
                        413,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "error": "artifact_too_large",
                        },
                        head=head,
                    )
                    return
                except DashboardArtifactEncodingError:
                    self.send_json(
                        415,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "error": "artifact_not_utf8",
                        },
                        head=head,
                    )
                    return
                except (DashboardArtifactNotFound, DashboardStateError):
                    self.send_json(
                        404,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "error": "artifact_not_found",
                        },
                        head=head,
                    )
                    return
                self.send_bytes(
                    200,
                    body,
                    "text/plain; charset=utf-8",
                    api=True,
                    head=head,
                )
                return
            route = routes.get(path)
            if route is None:
                self.send_json(
                    404,
                    {"schema_version": SCHEMA_VERSION, "error": "not_found"},
                    head=head,
                )
                return
            file_path, content_type = route
            try:
                body = file_path.read_bytes()
            except OSError:
                self.send_json(
                    500,
                    {"schema_version": SCHEMA_VERSION, "error": "asset_unavailable"},
                    head=head,
                )
                return
            self.send_bytes(200, body, content_type, api=False, head=head)

        def do_GET(self) -> None:
            self.serve()

        def do_HEAD(self) -> None:
            self.serve(head=True)

        def reject(self) -> None:
            if self.allowed():
                self.send_json(
                    405,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "error": "read_only",
                        "message": "The development dashboard is read-only.",
                    },
                )

        do_POST = reject
        do_PUT = reject
        do_PATCH = reject
        do_DELETE = reject
        do_OPTIONS = reject

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.client_address[0], self.log_date_time_string(), fmt % args)
            )

    return Handler


def create_server(
    builder: DashboardSnapshotBuilder,
    *,
    asset_root: Path = DEFAULT_ASSET_ROOT,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    host = validate_bind_host(host)
    if not 0 <= port <= 65535:
        raise DashboardError("Dashboard port is invalid.")
    handler = make_handler(builder, asset_root)
    if ":" in host:

        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_class = IPv6Server
    else:
        server_class = ThreadingHTTPServer
    server = server_class((host, port), handler)
    server.daemon_threads = True
    return server


def parse_check(value: str) -> dict[str, str]:
    check_id, sep, remainder = value.partition("=")
    if not sep:
        raise DashboardStateError("Check must use NAME=STATUS:SUMMARY syntax.")
    status, _, summary = remainder.partition(":")
    return {"id": check_id, "status": status, "summary": summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local read-only development supervision dashboard"
    )
    subs = parser.add_subparsers(dest="command", required=True)
    serve = subs.add_parser("serve", help="serve the dashboard on loopback")
    serve.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", dest="open_browser")
    snapshot = subs.add_parser("snapshot", help="print the current dashboard snapshot")
    snapshot.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    snapshot.add_argument("--pretty", action="store_true")
    record = subs.add_parser(
        "record", help="append a bounded local progress/validation record"
    )
    record.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    record.add_argument("--task-id", required=True)
    record.add_argument("--unit-id", required=True)
    record.add_argument("--status", required=True, choices=sorted(PROGRESS_STATUSES))
    record.add_argument("--summary", default="")
    record.add_argument(
        "--check", action="append", default=[], metavar="NAME=STATUS:SUMMARY"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        builder = DashboardSnapshotBuilder(args.repo_root)
        if args.command == "snapshot":
            print(
                json.dumps(
                    builder.build(use_cache=False),
                    ensure_ascii=False,
                    indent=2 if args.pretty else None,
                )
            )
            return 0
        if args.command == "record":
            git = collect_git_state(builder.repo_root)
            record = append_record(
                builder.state_path,
                task_id=args.task_id,
                unit_id=args.unit_id,
                status=args.status,
                summary=args.summary,
                branch=git["branch"],
                commit=git["head"]["short_hash"],
                checks=[parse_check(value) for value in args.check],
            )
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
        server = create_server(builder, host=args.host, port=args.port)
        host, port = server.server_address[:2]
        display = f"[{host}]" if ":" in str(host) else host
        url = f"http://{display}:{port}/"
        print(f"Development dashboard: {url}")
        print("Read-only; loopback requests only. Press Ctrl+C to stop.")
        if args.open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
