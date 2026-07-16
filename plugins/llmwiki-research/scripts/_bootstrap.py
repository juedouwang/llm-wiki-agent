"""Portable discovery and delegation helpers for the LLM Wiki Research plugin."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable

CORE_ROOT_ENV = "LLMWIKI_CORE_ROOT"
WORKSPACE_ROOT_ENV = "LLMWIKI_WORKSPACE_ROOT"
MAX_ANCESTOR_LEVELS = 8
_CORE_MARKERS = (
    Path("tools") / "__init__.py",
    Path("tools") / "project.py",
    Path("tools") / "research_core.py",
    Path("tools") / "research_mcp.py",
)


class BootstrapError(RuntimeError):
    """Raised when a portable launcher cannot safely locate Research Core."""


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BootstrapError(
            "A configured Research Core path could not be resolved."
        ) from exc


def _is_core_root(candidate: Path) -> bool:
    return candidate.is_dir() and all(
        (candidate / marker).is_file() for marker in _CORE_MARKERS
    )


def _bounded_ancestors(start: Path) -> list[Path]:
    current = _resolved(start)
    if current.is_file():
        current = current.parent
    ancestors: list[Path] = []
    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        ancestors.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ancestors


def locate_core_root(
    *,
    environ: Mapping[str, str] | None = None,
    start: Path | None = None,
) -> Path:
    """Locate a Core checkout from explicit configuration or bounded ancestors."""

    selected_environment = os.environ if environ is None else environ
    configured = selected_environment.get(CORE_ROOT_ENV, "").strip()
    if configured:
        candidate = _resolved(Path(configured))
        if _is_core_root(candidate):
            return candidate
        raise BootstrapError(
            f"{CORE_ROOT_ENV} must name a directory containing the Research Core tools package."
        )

    search_starts = [
        start if start is not None else Path(__file__).resolve().parent,
        Path.cwd(),
    ]
    visited: set[Path] = set()
    for search_start in search_starts:
        for candidate in _bounded_ancestors(search_start):
            if candidate in visited:
                continue
            visited.add(candidate)
            if _is_core_root(candidate):
                return candidate

    raise BootstrapError(
        f"Research Core could not be located; set {CORE_ROOT_ENV} to its checkout root."
    )


def locate_workspace_root(
    core_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Use an explicit assistant workspace, defaulting to the Core root."""

    selected_environment = os.environ if environ is None else environ
    configured = selected_environment.get(WORKSPACE_ROOT_ENV, "").strip()
    return _resolved(Path(configured)) if configured else core_root


def add_core_to_import_path(core_root: Path) -> None:
    """Prepend the located Core root without changing process working directory."""

    rendered = str(core_root)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)


def inject_workspace_root(arguments: Sequence[str], workspace_root: Path) -> list[str]:
    """Append the shared project option unless the caller supplied it."""

    copied = list(arguments)
    if any(
        argument == "--workspace-root" or argument.startswith("--workspace-root=")
        for argument in copied
    ):
        return copied
    copied.extend(("--workspace-root", str(workspace_root)))
    return copied


def load_main(
    module_name: str, core_root: Path
) -> Callable[[Sequence[str] | None], int]:
    """Load an existing Core module entry point after portable discovery."""

    add_core_to_import_path(core_root)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise BootstrapError(
            f"Unable to import the configured Research Core module {module_name}."
        ) from exc
    module_file = getattr(module, "__file__", None)
    expected_file = core_root.joinpath(*module_name.split(".")).with_suffix(".py")
    if not isinstance(module_file, str) or _resolved(Path(module_file)) != _resolved(
        expected_file
    ):
        raise BootstrapError(
            f"The imported module {module_name} does not belong to the configured Research Core."
        )
    entrypoint = getattr(module, "main", None)
    if not callable(entrypoint):
        raise BootstrapError(
            f"The configured Research Core module {module_name} has no main entry point."
        )
    return entrypoint
