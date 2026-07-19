"""Delegate Plugin CLI arguments to the bundled project command."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))

from _bootstrap import (  # noqa: E402
    BootstrapError,
    inject_workspace_root,
    load_main,
    locate_core_root,
    locate_workspace_root,
)


def main(arguments: Sequence[str] | None = None) -> int:
    selected_arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        core_root = locate_core_root()
        workspace_root = locate_workspace_root(core_root)
        entrypoint = load_main("tools.project", core_root)
    except BootstrapError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return entrypoint(inject_workspace_root(selected_arguments, workspace_root))


if __name__ == "__main__":
    raise SystemExit(main())
