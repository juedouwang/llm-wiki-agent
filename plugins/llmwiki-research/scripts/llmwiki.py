"""Delegate plugin CLI arguments to the existing tools.project command."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from _bootstrap import (
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
