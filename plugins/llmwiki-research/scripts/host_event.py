"""Fail-open PostToolUse adapter that records only H-04 dirty-path hints."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_ROOT = Path(__file__).resolve().parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))
from _bootstrap import (  # noqa: E402
    add_core_to_import_path,
    locate_core_root,
    locate_workspace_root,
)

_PROJECT_ID_ENV = "LLMWIKI_PROJECT_ID"
_PROJECT_ROOT_ENV = "LLMWIKI_PROJECT_ROOT"
_PRODUCER = "codex-plugin"
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_PATHS = 256


def _read_payload() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > _MAX_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _successful_post_tool_use(payload: dict[str, Any]) -> bool:
    if payload.get("hook_event_name") != "PostToolUse":
        return False
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return False
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("ok") is False or response.get("success") is False:
            return False
        if response.get("isError") is True or response.get("is_error") is True:
            return False
    return True


def _path_values(tool_input: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values


def _resolved_directory(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        candidate = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    return candidate if candidate.is_absolute() else None


def _is_within(candidate: Path, project_root: Path) -> bool:
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return False
    return True


def _base_directory(payload: dict[str, Any], project_root: Path) -> Path | None:
    configured = os.environ.get(_PROJECT_ROOT_ENV, "").strip()
    if configured:
        candidate = _resolved_directory(configured)
        return candidate if candidate == project_root else None

    raw_cwd = payload.get("cwd")
    if raw_cwd is None:
        return project_root
    candidate = _resolved_directory(raw_cwd)
    if candidate is None or not _is_within(candidate, project_root):
        return None
    return candidate


def _project_relative_path(
    raw: str,
    *,
    project_root: Path,
    base_directory: Path | None,
) -> str | None:
    if not raw.strip() or "\x00" in raw:
        return None
    try:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            if base_directory is None:
                return None
            resolved = (base_directory / candidate).resolve(strict=False)
        relative = resolved.relative_to(project_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative.parts:
        return None
    rendered = relative.as_posix()
    return rendered if rendered not in ("", ".") else None


def _extract_paths(payload: dict[str, Any], project_root: Path) -> list[str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    base_directory = _base_directory(payload, project_root)
    paths: list[str] = []
    seen: set[str] = set()
    for raw in _path_values(tool_input):
        relative = _project_relative_path(
            raw,
            project_root=project_root,
            base_directory=base_directory,
        )
        if relative is None or relative in seen:
            continue
        seen.add(relative)
        paths.append(relative)
        if len(paths) >= _MAX_PATHS:
            break
    return paths


def _operation(tool_name: str) -> str:
    lowered = tool_name.lower()
    if "delete" in lowered or "remove" in lowered:
        return "deleted"
    if "move" in lowered or "rename" in lowered:
        return "moved"
    if "create" in lowered:
        return "created"
    if "write" in lowered or "edit" in lowered or "patch" in lowered:
        return "modified"
    return "unknown"


def _occurred_at(payload: dict[str, Any]) -> str:
    for key in ("occurred_at", "timestamp"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _event_id(payload: dict[str, Any], paths: list[str]) -> str:
    identity = {
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "paths": paths,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("ascii")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"codex-plugin:{digest}"


def _submit(payload: dict[str, Any], project_id: str) -> None:
    if not _successful_post_tool_use(payload):
        return
    core_root = locate_core_root()
    workspace_root = locate_workspace_root(core_root)
    add_core_to_import_path(core_root)
    from tools.project_registry import load_registered_project
    from tools.research_core import ResearchCoreService

    registration = load_registered_project(workspace_root, project_id)
    paths = _extract_paths(payload, registration.project_root)
    if not paths:
        return
    tool_name = payload["tool_name"]
    ResearchCoreService(workspace_root).host_event_submit(
        project_id=registration.project_id,
        event_id=_event_id(payload, paths),
        producer=_PRODUCER,
        occurred_at=_occurred_at(payload),
        operation=_operation(tool_name),
        paths=paths,
    )


def main() -> int:
    project_id = os.environ.get(_PROJECT_ID_ENV, "").strip()
    if not project_id:
        return 0
    try:
        payload = _read_payload()
        if payload is not None:
            _submit(payload, project_id)
    except BaseException:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
