"""Launch the bundled loopback-only research cockpit."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))

from _bootstrap import (  # noqa: E402
    BootstrapError,
    load_main,
    locate_core_root,
    locate_workspace_root,
)


def main(arguments: Sequence[str] | None = None) -> int:
    selected_arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        core_root = locate_core_root()
        workspace_root = locate_workspace_root(core_root)
        entrypoint = load_main("tools.research_cockpit", core_root)
    except BootstrapError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    if not any(
        argument == "--workspace-root" or argument.startswith("--workspace-root=")
        for argument in selected_arguments
    ):
        selected_arguments = [
            "--workspace-root",
            str(workspace_root),
            *selected_arguments,
        ]
    return entrypoint(selected_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
