"""CLI entry. Use --runner to execute due tasks with no model call."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--runner":
        from local_context.runner import main as run_tasks

        return run_tasks(args[1:])
    from local_context.server import main as serve

    return serve()
