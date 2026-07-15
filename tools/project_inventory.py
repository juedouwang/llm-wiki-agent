#!/usr/bin/env python3
"""Source-read-only directory inventory for registered research projects.

B-03 consumes the B-01 project registration and B-02 scan policy, walks only
approved directory boundaries, and writes a basic Schema v1 ``manifest.jsonl``.
It deliberately does not read file contents, calculate content hashes, record
size/mtime metadata, classify formats or research roles, or call an LLM.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# Support both ``import tools.project_inventory`` and direct sibling imports.
if __package__:
    from .project_layout import LayoutError
    from .project_registry import load_registered_project
    from .scan_policy import (
        PathDecision,
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )
else:
    from project_layout import LayoutError  # type: ignore[no-redef]
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from scan_policy import (  # type: ignore[no-redef]
        PathDecision,
        ScanPolicy,
        ScanPolicyConfig,
        load_scan_policy,
    )


PROJECT_MANIFEST_SCHEMA_VERSION = 1
PROJECT_MANIFEST_KIND = "llmwiki-project-manifest"
PROJECT_MANIFEST_VERSION = "project-inventory-v1"

_RECORD_TYPES = (
    "file",
    "excluded_file",
    "excluded_directory",
    "symlink",
    "special_entry",
    "skipped_directory",
)


class ProjectInventoryError(LayoutError):
    """Base error for an incomplete or unsafe project inventory."""


class ProjectInventoryTraversalError(ProjectInventoryError):
    """Raised when an in-scope directory cannot be inventoried completely."""


@dataclass(frozen=True)
class ProjectInventoryResult:
    """Location and accountable counts for one completed basic Manifest."""

    project_id: str
    project_root: Path
    manifest_file: Path
    total_records: int
    record_counts: dict[str, int]
    directories_scanned: int
    exclusion_summary: dict[str, dict[str, int]]
    symlink_summary: dict[str, int]
    policy: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "manifest_file": str(self.manifest_file),
            "manifest_version": PROJECT_MANIFEST_VERSION,
            "total_records": self.total_records,
            "record_counts": dict(self.record_counts),
            "directories_scanned": self.directories_scanned,
            "exclusion_summary": self.exclusion_summary,
            "symlink_summary": dict(self.symlink_summary),
            "policy": self.policy,
        }


@dataclass(frozen=True)
class _BoundaryEvaluation:
    logical: PathDecision
    resolved: PathDecision | None
    included: bool
    traverse: bool
    reason_code: str
    reason: str
    reason_source: str


@dataclass(frozen=True)
class _DirectoryTask:
    logical_path: str
    physical_path: Path
    ancestor_realpaths: tuple[Path, ...]


@dataclass(frozen=True)
class _SymlinkTask:
    logical_path: str
    physical_path: Path
    ancestor_realpaths: tuple[Path, ...]


@dataclass
class _InventoryState:
    project_id: str
    writer: TextIO
    record_counts: Counter[str] = field(default_factory=Counter)
    excluded_files: Counter[str] = field(default_factory=Counter)
    excluded_directories: Counter[str] = field(default_factory=Counter)
    symlink_reasons: Counter[str] = field(default_factory=Counter)
    directories_scanned: int = 0

    def write(self, record: dict[str, Any]) -> None:
        self.writer.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        self.writer.write("\n")
        self.record_counts[record["record_type"]] += 1

    def record_excluded_file(self, reason_code: str) -> None:
        self.excluded_files[reason_code] += 1

    def record_excluded_directory(self, reason_code: str) -> None:
        self.excluded_directories[reason_code] += 1

    def record_symlink_reason(self, reason_code: str) -> None:
        self.symlink_reasons[reason_code] += 1


@dataclass
class _TraversalContext:
    project_root: Path
    policy: ScanPolicy
    state: _InventoryState
    directory_queue: deque[_DirectoryTask]
    symlink_queue: deque[_SymlinkTask]
    visited_directories: dict[str, Path]
    followed_symlink_targets: dict[str, Path]

    def policy_visited_realpaths(self) -> tuple[Path, ...]:
        combined = dict(self.visited_directories)
        combined.update(self.followed_symlink_targets)
        return tuple(combined[key] for key in sorted(combined))


def _record_base(project_id: str, record_type: str, path: str) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
        "kind": PROJECT_MANIFEST_KIND,
        "manifest_version": PROJECT_MANIFEST_VERSION,
        "record_type": record_type,
        "project_id": project_id,
        "path": path,
    }


def _summary_base(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
        "kind": PROJECT_MANIFEST_KIND,
        "manifest_version": PROJECT_MANIFEST_VERSION,
        "record_type": "summary",
        "project_id": project_id,
    }


def _canonical_path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    return unicodedata.normalize("NFC", normalized)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _join_relative(parent: str, name: str) -> str:
    normalized_name = unicodedata.normalize("NFC", name)
    return f"{parent}/{normalized_name}" if parent else normalized_name


def _relative_to_root(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise ProjectInventoryTraversalError(
            f"inventory path escaped the registered project root: {path}"
        ) from exc
    value = relative.as_posix()
    if not value or value == ".":
        raise ProjectInventoryTraversalError(
            f"inventory entry unexpectedly resolved to the project root: {path}"
        )
    return unicodedata.normalize("NFC", value)


def _evaluate_boundary(
    policy: ScanPolicy,
    logical_path: str,
    physical_relative_path: str,
    *,
    is_directory: bool,
) -> _BoundaryEvaluation:
    logical = policy.decide_path(logical_path, is_directory=is_directory)
    resolved = None
    if physical_relative_path != logical.path:
        resolved = policy.decide_path(
            physical_relative_path,
            is_directory=is_directory,
        )

    included = logical.included and (resolved is None or resolved.included)
    traverse = logical.traverse and (resolved is None or resolved.traverse)
    if is_directory:
        if not logical.traverse:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.traverse:
            selected = resolved
            source = "resolved"
        elif not logical.included:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.included:
            selected = resolved
            source = "resolved"
        else:
            selected = logical
            source = "logical"
    else:
        if not logical.included:
            selected = logical
            source = "logical"
        elif resolved is not None and not resolved.included:
            selected = resolved
            source = "resolved"
        else:
            selected = logical
            source = "logical"

    return _BoundaryEvaluation(
        logical=logical,
        resolved=resolved,
        included=included,
        traverse=traverse,
        reason_code=selected.reason_code,
        reason=selected.reason,
        reason_source=source,
    )


def _boundary_fields(evaluation: _BoundaryEvaluation) -> dict[str, Any]:
    fields: dict[str, Any] = {"boundary": evaluation.logical.as_dict()}
    if evaluation.resolved is not None:
        fields["resolved_path"] = evaluation.resolved.path
        fields["resolved_boundary"] = evaluation.resolved.as_dict()
    return fields


def _effective_reason_fields(evaluation: _BoundaryEvaluation) -> dict[str, str]:
    return {
        "effective_reason_code": evaluation.reason_code,
        "effective_reason": evaluation.reason,
        "effective_reason_source": evaluation.reason_source,
    }


def _sorted_directory_names(path: Path, *, case_sensitive: bool) -> list[str]:
    try:
        with os.scandir(path) as entries:
            names = [entry.name for entry in entries]
    except OSError as exc:
        raise ProjectInventoryTraversalError(
            f"could not enumerate in-scope directory {path}: {exc}"
        ) from exc

    def key(name: str) -> tuple[str, str]:
        normalized = unicodedata.normalize("NFC", name)
        primary = normalized if case_sensitive else normalized.casefold()
        return primary, normalized

    return sorted(names, key=key)


def _is_non_symlink_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _write_unreadable_entry(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_relative_path: str,
    error: OSError,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=False,
    )
    record = _record_base(
        context.state.project_id,
        "special_entry",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "entry_kind": "metadata-unreadable",
            "reason_code": "entry-metadata-unreadable",
            "reason": f"entry metadata could not be read: {error}",
        }
    )
    context.state.write(record)


def _write_special_entry(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_relative_path: str,
    entry_kind: str,
    is_directory: bool,
    reason_code: str,
    reason: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=is_directory,
    )
    record = _record_base(
        context.state.project_id,
        "special_entry",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "entry_kind": entry_kind,
            "reason_code": reason_code,
            "reason": reason,
        }
    )
    context.state.write(record)


def _queue_directory(
    context: _TraversalContext,
    *,
    task: _DirectoryTask,
    logical_path: str,
    physical_path: Path,
    physical_relative_path: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=True,
    )
    if not evaluation.traverse:
        record = _record_base(
            context.state.project_id,
            "excluded_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(_effective_reason_fields(evaluation))
        record["pruned"] = True
        context.state.write(record)
        context.state.record_excluded_directory(evaluation.reason_code)
        return

    try:
        real_path = physical_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectInventoryTraversalError(
            f"could not resolve in-scope directory {logical_path}: {exc}"
        ) from exc
    if not _is_within(real_path, context.project_root):
        record = _record_base(
            context.state.project_id,
            "excluded_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(
            {
                "effective_reason_code": "directory-target-outside-project",
                "effective_reason": (
                    "directory resolved outside the registered project root"
                ),
                "effective_reason_source": "inventory-safety",
                "pruned": True,
                "resolved_target": str(real_path),
            }
        )
        context.state.write(record)
        context.state.record_excluded_directory(
            "directory-target-outside-project"
        )
        return

    real_key = _canonical_path_key(real_path)
    if real_key in context.visited_directories:
        record = _record_base(
            context.state.project_id,
            "skipped_directory",
            evaluation.logical.path,
        )
        record.update(_boundary_fields(evaluation))
        record.update(
            {
                "reason_code": "directory-target-already-visited",
                "reason": "directory real path was already queued or scanned",
                "resolved_target": str(real_path),
            }
        )
        context.state.write(record)
        return

    context.visited_directories[real_key] = real_path
    context.directory_queue.append(
        _DirectoryTask(
            logical_path=evaluation.logical.path,
            physical_path=real_path,
            ancestor_realpaths=task.ancestor_realpaths + (real_path,),
        )
    )


def _write_file(
    context: _TraversalContext,
    *,
    logical_path: str,
    physical_relative_path: str,
) -> None:
    evaluation = _evaluate_boundary(
        context.policy,
        logical_path,
        physical_relative_path,
        is_directory=False,
    )
    record_type = "file" if evaluation.included else "excluded_file"
    record = _record_base(
        context.state.project_id,
        record_type,
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    if not evaluation.included:
        record.update(_effective_reason_fields(evaluation))
        context.state.record_excluded_file(evaluation.reason_code)
    context.state.write(record)


def _scan_directory(context: _TraversalContext, task: _DirectoryTask) -> None:
    context.state.directories_scanned += 1
    names = _sorted_directory_names(
        task.physical_path,
        case_sensitive=context.policy.case_sensitive,
    )
    for name in names:
        logical_path = _join_relative(task.logical_path, name)
        physical_path = task.physical_path / name
        physical_relative_path = _relative_to_root(
            physical_path,
            context.project_root,
        )
        try:
            metadata = physical_path.lstat()
        except OSError as exc:
            _write_unreadable_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                error=exc,
            )
            continue

        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            context.symlink_queue.append(
                _SymlinkTask(
                    logical_path=logical_path,
                    physical_path=physical_path,
                    ancestor_realpaths=task.ancestor_realpaths,
                )
            )
        elif _is_non_symlink_reparse_point(metadata):
            _write_special_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                entry_kind="reparse-point",
                is_directory=stat.S_ISDIR(mode),
                reason_code="non-symlink-reparse-point-not-followed",
                reason=(
                    "non-symlink filesystem reparse points are recorded but not "
                    "traversed by the B-03 inventory"
                ),
            )
        elif stat.S_ISDIR(mode):
            _queue_directory(
                context,
                task=task,
                logical_path=logical_path,
                physical_path=physical_path,
                physical_relative_path=physical_relative_path,
            )
        elif stat.S_ISREG(mode):
            _write_file(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
            )
        else:
            _write_special_entry(
                context,
                logical_path=logical_path,
                physical_relative_path=physical_relative_path,
                entry_kind="special-filesystem-entry",
                is_directory=False,
                reason_code="non-regular-filesystem-entry",
                reason=(
                    "entry is neither a regular file, directory, nor symbolic link"
                ),
            )


def _target_metadata(
    target_path: str | None,
    project_root: Path,
) -> tuple[Path | None, str, bool]:
    if target_path is None:
        return None, "unknown", False
    target = Path(target_path)
    if not _is_within(target, project_root):
        return target, "outside-project", False
    try:
        metadata = target.stat()
    except OSError:
        return target, "unreadable", False
    if stat.S_ISDIR(metadata.st_mode):
        return target, "directory", True
    if stat.S_ISREG(metadata.st_mode):
        return target, "file", False
    return target, "special", False


def _symlink_boundary(
    context: _TraversalContext,
    task: _SymlinkTask,
    *,
    target: Path | None,
    target_is_directory: bool,
) -> _BoundaryEvaluation:
    physical_relative_path = task.logical_path
    if target is not None and _is_within(target, context.project_root):
        physical_relative_path = _relative_to_root(target, context.project_root)
    return _evaluate_boundary(
        context.policy,
        task.logical_path,
        physical_relative_path,
        is_directory=target_is_directory,
    )


def _process_symlink(context: _TraversalContext, task: _SymlinkTask) -> None:
    decision = context.policy.decide_symlink(
        task.logical_path,
        ancestor_realpaths=task.ancestor_realpaths,
        visited_realpaths=context.policy_visited_realpaths(),
    )
    target, target_kind, target_is_directory = _target_metadata(
        decision.target_path,
        context.project_root,
    )
    evaluation = _symlink_boundary(
        context,
        task,
        target=target,
        target_is_directory=target_is_directory,
    )

    followed = False
    final_reason_code = decision.reason_code
    final_reason = decision.reason

    if decision.follow:
        if target is None or target_kind == "unreadable":
            final_reason_code = "symlink-target-metadata-unreadable"
            final_reason = "approved symbolic-link target metadata became unreadable"
        elif target_kind == "directory":
            if not evaluation.logical.traverse:
                final_reason_code = "symlink-link-boundary-excluded"
                final_reason = evaluation.logical.reason
            elif evaluation.resolved is not None and not evaluation.resolved.traverse:
                final_reason_code = "symlink-target-boundary-excluded"
                final_reason = evaluation.resolved.reason
            else:
                target_key = _canonical_path_key(target)
                context.visited_directories[target_key] = target
                context.directory_queue.append(
                    _DirectoryTask(
                        logical_path=evaluation.logical.path,
                        physical_path=target,
                        ancestor_realpaths=task.ancestor_realpaths + (target,),
                    )
                )
                followed = True
        elif target_kind == "file":
            if not evaluation.logical.included:
                final_reason_code = "symlink-link-boundary-excluded"
                final_reason = evaluation.logical.reason
            elif evaluation.resolved is not None and not evaluation.resolved.included:
                final_reason_code = "symlink-target-boundary-excluded"
                final_reason = evaluation.resolved.reason
            else:
                context.followed_symlink_targets[_canonical_path_key(target)] = target
                followed = True
        else:
            final_reason_code = "symlink-target-special-entry"
            final_reason = (
                "symbolic-link target is not a regular file or directory; it was "
                "recorded but not traversed"
            )

    record = _record_base(
        context.state.project_id,
        "symlink",
        evaluation.logical.path,
    )
    record.update(_boundary_fields(evaluation))
    record.update(
        {
            "symlink": decision.as_dict(),
            "target_entry_kind": target_kind,
            "followed": followed,
            "follow_reason_code": final_reason_code,
            "follow_reason": final_reason,
        }
    )
    context.state.write(record)
    context.state.record_symlink_reason(final_reason_code)


def _walk_project(
    project_root: Path,
    policy: ScanPolicy,
    state: _InventoryState,
) -> None:
    root = project_root.resolve(strict=True)
    root_key = _canonical_path_key(root)
    context = _TraversalContext(
        project_root=root,
        policy=policy,
        state=state,
        directory_queue=deque(
            [
                _DirectoryTask(
                    logical_path="",
                    physical_path=root,
                    ancestor_realpaths=(root,),
                )
            ]
        ),
        symlink_queue=deque(),
        visited_directories={root_key: root},
        followed_symlink_targets={},
    )

    # Normal project paths are exhausted before queued symbolic links. This
    # gives canonical in-tree paths precedence over aliases and lets B-02's
    # duplicate-target guard prevent an alias from replacing a real path.
    while context.directory_queue or context.symlink_queue:
        while context.directory_queue:
            _scan_directory(context, context.directory_queue.popleft())
        if context.symlink_queue:
            _process_symlink(context, context.symlink_queue.popleft())


def _build_exclusion_summary(
    state: _InventoryState,
) -> dict[str, dict[str, int]]:
    reason_codes = sorted(
        set(state.excluded_files) | set(state.excluded_directories)
    )
    summary: dict[str, dict[str, int]] = {}
    for reason_code in reason_codes:
        excluded_files = state.excluded_files[reason_code]
        pruned_directories = state.excluded_directories[reason_code]
        summary[reason_code] = {
            "excluded_files": excluded_files,
            "pruned_directories": pruned_directories,
            "total_records": excluded_files + pruned_directories,
        }
    return summary


def _build_summary_record(
    *,
    project_id: str,
    project_root: Path,
    policy: ScanPolicy,
    state: _InventoryState,
) -> dict[str, Any]:
    record_counts = {
        record_type: state.record_counts[record_type]
        for record_type in _RECORD_TYPES
    }
    total_records = sum(record_counts.values())
    summary = _summary_base(project_id)
    summary.update(
        {
            "project_root": str(project_root),
            "total_records": total_records,
            "record_counts": record_counts,
            "directories_scanned": state.directories_scanned,
            "exclusion_summary": _build_exclusion_summary(state),
            "symlink_summary": {
                reason: state.symlink_reasons[reason]
                for reason in sorted(state.symlink_reasons)
            },
            "policy": policy.as_dict(),
        }
    )
    return summary


def _create_temporary_file(directory: Path, suffix: str) -> tuple[int, Path]:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".manifest.jsonl.",
        suffix=suffix,
        dir=directory,
    )
    return descriptor, Path(raw_path)


def _write_atomic_manifest(
    manifest_file: Path,
    summary: dict[str, Any],
    spool_file: Path,
) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary = _create_temporary_file(
            manifest_file.parent,
            ".tmp",
        )
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            summary_line = json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            target.write(summary_line.encode("utf-8"))
            target.write(b"\n")
            with spool_file.open("rb") as source:
                shutil.copyfileobj(source, target)
        os.replace(temporary, manifest_file)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inventory_project(
    workspace_root: str | Path,
    project_id: str,
    *,
    policy_config: ScanPolicyConfig | None = None,
) -> ProjectInventoryResult:
    """Inventory one B-01 registered project and atomically replace its Manifest.

    The source project is only enumerated and stat'ed. The only write target is
    the registered machine-state ``manifest.jsonl`` outside the source tree.
    """

    registration = load_registered_project(workspace_root, project_id)
    policy = load_scan_policy(
        registration.project_root,
        config=policy_config,
    )
    manifest_file = registration.layout.manifest_file
    if not manifest_file.parent.is_dir():
        raise ProjectInventoryError(
            f"registered machine-state directory is unavailable: {manifest_file.parent}"
        )

    descriptor = -1
    spool_file: Path | None = None
    try:
        descriptor, spool_file = _create_temporary_file(
            manifest_file.parent,
            ".records",
        )
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as writer:
            descriptor = -1
            state = _InventoryState(
                project_id=registration.project_id,
                writer=writer,
            )
            _walk_project(registration.project_root, policy, state)

        summary = _build_summary_record(
            project_id=registration.project_id,
            project_root=registration.project_root,
            policy=policy,
            state=state,
        )
        _write_atomic_manifest(manifest_file, summary, spool_file)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if spool_file is not None:
            spool_file.unlink(missing_ok=True)

    return ProjectInventoryResult(
        project_id=registration.project_id,
        project_root=registration.project_root,
        manifest_file=manifest_file,
        total_records=summary["total_records"],
        record_counts=summary["record_counts"],
        directories_scanned=summary["directories_scanned"],
        exclusion_summary=summary["exclusion_summary"],
        symlink_summary=summary["symlink_summary"],
        policy=summary["policy"],
    )
