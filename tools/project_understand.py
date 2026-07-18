"""Deterministic artifacts used by the complete project-understand pipeline.

This module is intentionally host-neutral.  It writes only machine state below
``.llmwiki/projects/<project_id>/`` and never reads or mutates the registered
research source.  Semantic observations are not invented: when the host has not
provided them, reports say so explicitly and remain draft/metadata-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

if __package__:
    from .project_layout import LayoutError, write_versioned_json
    from .project_registry import load_registered_project
    from .project_runs import validate_run_id, validate_stage_id
else:  # pragma: no cover - direct sibling-module execution
    from project_layout import LayoutError, write_versioned_json  # type: ignore
    from project_registry import load_registered_project  # type: ignore
    from project_runs import validate_run_id, validate_stage_id  # type: ignore


PROJECT_UNDERSTAND_ARTIFACT_KIND = "llmwiki-project-understand-stage"
PROJECT_UNDERSTAND_ARTIFACT_VERSION = "project-understand-stage-v1"
PROJECT_UNDERSTAND_STAGE_SCHEMA_VERSION = 1
WEB_RENDER_KIND = "llmwiki-project-understand-web"
WEB_RENDER_VERSION = "project-understand-web-v1"


@dataclass(frozen=True)
class MachineArtifact:
    """A path/hash pair suitable for a ProjectRun ``StageOutcome``."""

    relative_path: str
    content_hash: str
    artifact_type: str
    artifact_id: str

    def as_run_artifact(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
        }


def _utc_timestamp(value: object | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text.endswith("Z"):
            raise ValueError("timestamp must be canonical UTC text")
        try:
            current = datetime.fromisoformat(text[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("timestamp is invalid") from exc
        current = current.astimezone(timezone.utc)
    else:
        raise TypeError("timestamp must be a datetime, canonical string, or None")
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized.encode("utf-8")) > maximum:
        raise ValueError(f"{label} must be non-empty and bounded")
    return normalized


def _bounded_codes(values: Iterable[str], label: str) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{label} contains an invalid code")
        if value not in result:
            result.append(value)
    return result


def _safe_run_root(workspace_root: str | Path, project_id: str, run_id: str) -> tuple[Path, Path]:
    registration = load_registered_project(workspace_root, project_id)
    normalized_run_id = validate_run_id(run_id)
    root = registration.layout.runs_dir.resolve()
    run_root = (root / normalized_run_id).resolve()
    if root not in run_root.parents:
        raise LayoutError("run artifact path escaped the registered runs directory")
    run_root.mkdir(parents=True, exist_ok=True)
    return registration.layout.machine_root.resolve(), run_root


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_stage_report(
    workspace_root: str | Path,
    project_id: str,
    run_id: str,
    stage_id: str,
    *,
    summary: str,
    status: str = "complete",
    reason_code: str = "deterministic-local-analysis",
    inputs: Iterable[str] = (),
    outputs: Iterable[str] = (),
    gaps: Iterable[str] = (),
    created_at: object | None = None,
) -> MachineArtifact:
    """Persist one bounded, versioned stage report under the run directory."""

    validate_stage_id(stage_id)
    if status not in {"complete", "draft"}:
        raise ValueError("stage report status must be complete or draft")
    payload = {
        "schema_version": PROJECT_UNDERSTAND_STAGE_SCHEMA_VERSION,
        "kind": PROJECT_UNDERSTAND_ARTIFACT_KIND,
        "artifact_version": PROJECT_UNDERSTAND_ARTIFACT_VERSION,
        "project_id": project_id,
        "run_id": validate_run_id(run_id),
        "stage_id": stage_id,
        "created_at": _utc_timestamp(created_at),
        "status": status,
        "mode": "deterministic-local",
        "reason_code": _bounded_text(reason_code, "reason_code", maximum=128),
        "summary": _bounded_text(summary, "summary"),
        "inputs": _bounded_codes(inputs, "inputs"),
        "outputs": _bounded_codes(outputs, "outputs"),
        "gaps": _bounded_codes(gaps, "gaps"),
    }
    machine_root, run_root = _safe_run_root(workspace_root, project_id, run_id)
    target = run_root / "artifacts" / f"{stage_id}.json"
    write_versioned_json(target, payload)
    relative = PurePosixPath(target.relative_to(machine_root)).as_posix()
    return MachineArtifact(
        relative_path=relative,
        content_hash=_hash_file(target),
        artifact_type="project-understand-stage-report",
        artifact_id=f"{run_id}:{stage_id}",
    )


def write_json_artifact(
    workspace_root: str | Path,
    project_id: str,
    run_id: str,
    filename: str,
    payload: Mapping[str, Any],
    *,
    artifact_type: str,
    artifact_id: str,
) -> MachineArtifact:
    """Write a versioned JSON artifact beneath the durable run directory."""

    if not filename or Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("run artifact filename must be a simple .json name")
    machine_root, run_root = _safe_run_root(workspace_root, project_id, run_id)
    target = run_root / "artifacts" / filename
    write_versioned_json(target, dict(payload))
    return MachineArtifact(
        relative_path=PurePosixPath(target.relative_to(machine_root)).as_posix(),
        content_hash=_hash_file(target),
        artifact_type=artifact_type,
        artifact_id=artifact_id,
    )


def _relative_link(from_dir: Path, target: Path, workspace_root: Path) -> str | None:
    """Return a safe relative file link, or None for an external custom root."""

    try:
        target_resolved = target.resolve()
        target_resolved.relative_to(workspace_root.resolve())
    except (OSError, ValueError):
        return None
    return Path(
        os.path.relpath(target_resolved, start=from_dir.resolve())
    ).as_posix()


def write_static_web_index(
    workspace_root: str | Path,
    project_id: str,
    run_id: str,
    *,
    title: str,
    status: str,
    stage_rows: Iterable[Mapping[str, Any]],
    knowledge_root: Path,
    coverage_path: Path | None = None,
    run_path: Path | None = None,
    created_at: object | None = None,
) -> MachineArtifact:
    """Write a self-contained, read-only HTML index for one completed run."""

    machine_root, run_root = _safe_run_root(workspace_root, project_id, run_id)
    web_dir = run_root / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    index = web_dir / "index.html"
    workspace = Path(workspace_root).expanduser().resolve()
    links: list[tuple[str, str | None]] = []
    links.append(("Knowledge overview", _relative_link(web_dir, knowledge_root / "overview.md", workspace)))
    links.append(("Knowledge index", _relative_link(web_dir, knowledge_root / "index.md", workspace)))
    if coverage_path is not None:
        links.append(("Coverage report", _relative_link(web_dir, coverage_path, workspace)))
    if run_path is not None:
        links.append(("Run report", _relative_link(web_dir, run_path, workspace)))

    rows: list[str] = []
    for row in stage_rows:
        stage = escape(str(row.get("stage_id", "")))
        stage_status = escape(str(row.get("status", "")))
        detail = escape(str(row.get("detail", "")))
        rows.append(f"<tr><td>{stage}</td><td><code>{stage_status}</code></td><td>{detail}</td></tr>")
    link_markup = "".join(
        f'<li><a href="{escape(href, quote=True)}">{escape(label)}</a></li>'
        if href
        else f"<li>{escape(label)} <em>not available in this workspace view</em></li>"
        for label, href in links
    )
    generated = _utc_timestamp(created_at)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
code {{ font-family: ui-monospace, monospace; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #8886; padding: .55rem; text-align: left; vertical-align: top; }}
.badge {{ display: inline-block; border: 1px solid #8888; border-radius: .4rem; padding: .15rem .45rem; }}
small {{ opacity: .75; }}
</style>
</head>
<body>
<h1>{escape(title)}</h1>
<p><span class="badge">{escape(status)}</span> <small>Generated {escape(generated)} · deterministic local view · read-only</small></p>
<h2>Project navigation</h2>
<ul>{link_markup}</ul>
<h2>Pipeline stages</h2>
<table><thead><tr><th>Stage</th><th>Status</th><th>Detail</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p><small>This page contains no source-file contents and no external resources.</small></p>
</body>
</html>
"""
    index.write_text(html, encoding="utf-8", newline="\n")
    relative = PurePosixPath(index.relative_to(machine_root)).as_posix()
    return MachineArtifact(
        relative_path=relative,
        content_hash=_hash_file(index),
        artifact_type="project-understand-web-index",
        artifact_id=f"{run_id}:web-index",
    )


__all__ = [
    "MachineArtifact",
    "PROJECT_UNDERSTAND_ARTIFACT_KIND",
    "PROJECT_UNDERSTAND_ARTIFACT_VERSION",
    "PROJECT_UNDERSTAND_STAGE_SCHEMA_VERSION",
    "WEB_RENDER_KIND",
    "WEB_RENDER_VERSION",
    "write_json_artifact",
    "write_stage_report",
    "write_static_web_index",
]
