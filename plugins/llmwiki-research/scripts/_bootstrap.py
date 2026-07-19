"""Self-contained discovery and delegation helpers for LLM Wiki Research."""

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
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_CORE_ROOT = _PLUGIN_ROOT / "runtime" / "core"
_CORE_MARKERS = (
    Path("tools") / "__init__.py",
    Path("tools") / "project.py",
    Path("tools") / "research_core.py",
    Path("tools") / "research_mcp.py",
)


class BootstrapError(RuntimeError):
    """Raised when a launcher cannot establish a safe bundled runtime."""


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BootstrapError(
            "A configured local directory could not be resolved."
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


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def locate_core_root(
    *,
    environ: Mapping[str, str] | None = None,
    start: Path | None = None,
) -> Path:
    """Prefer the bundled Core; retain checkout discovery for source development."""

    bundled = _resolved(_BUNDLED_CORE_ROOT)
    if _is_core_root(bundled):
        return bundled

    selected_environment = os.environ if environ is None else environ
    configured = selected_environment.get(CORE_ROOT_ENV, "").strip()
    if configured:
        candidate = _resolved(Path(configured))
        if _is_core_root(candidate):
            return candidate
        raise BootstrapError(
            f"{CORE_ROOT_ENV} does not identify a compatible development Core."
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
        "The bundled Research Core is unavailable; reinstall the Plugin package."
    )


def _default_workspace_candidate(environ: Mapping[str, str]) -> Path:
    if os.name == "nt":
        local_app_data = environ.get("LOCALAPPDATA", "").strip()
        if local_app_data and Path(local_app_data).is_absolute():
            return Path(local_app_data) / "LLMWiki" / "workspace"
        return Path.home() / "AppData" / "Local" / "LLMWiki" / "workspace"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LLMWiki" / "workspace"
    xdg_data_home = environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home and Path(xdg_data_home).is_absolute():
        return Path(xdg_data_home) / "llmwiki" / "workspace"
    return Path.home() / ".local" / "share" / "llmwiki" / "workspace"


def locate_workspace_root(
    core_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return an explicit absolute workspace or a safe per-user data directory."""

    selected_environment = os.environ if environ is None else environ
    configured = selected_environment.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        raw_candidate = Path(configured)
        if not raw_candidate.is_absolute():
            raise BootstrapError(f"{WORKSPACE_ROOT_ENV} must be an absolute directory.")
        candidate = _resolved(raw_candidate)
    else:
        candidate = _resolved(_default_workspace_candidate(selected_environment))

    normalized_core = _resolved(core_root)
    normalized_plugin = _resolved(_PLUGIN_ROOT)
    if (
        candidate == normalized_core
        or _is_within(candidate, normalized_core)
        or candidate == normalized_plugin
        or _is_within(candidate, normalized_plugin)
    ):
        raise BootstrapError("The workspace must be outside the Plugin runtime.")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(
            "The local workspace directory could not be created."
        ) from exc
    if not candidate.is_dir():
        raise BootstrapError("The local workspace location is not a directory.")
    if os.name != "nt":
        try:
            candidate.chmod(0o700)
        except OSError:
            pass
    return candidate


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
        raise BootstrapError("The bundled Research Core could not be loaded.") from exc
    module_file = getattr(module, "__file__", None)
    expected_file = core_root.joinpath(*module_name.split(".")).with_suffix(".py")
    if not isinstance(module_file, str) or _resolved(Path(module_file)) != _resolved(
        expected_file
    ):
        raise BootstrapError("A Research Core module failed its origin check.")
    entrypoint = getattr(module, "main", None)
    if not callable(entrypoint):
        raise BootstrapError("A Research Core entry point is unavailable.")
    return entrypoint
