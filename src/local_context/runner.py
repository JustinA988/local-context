"""Run due tasks locally. This process does not call a model."""

from __future__ import annotations

import sys

from local_context.config import load_settings
from local_context.errors import LocalError
from local_context.shell import Shell
from local_context.tasks import Tasks


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    only = None
    include_manual = "--all" in args
    if "--task" in args:
        index = args.index("--task")
        try:
            only = args[index + 1]
        except IndexError:
            print("local-context: --task needs a name", file=sys.stderr)
            return 2
    try:
        settings = load_settings()
        settings.ensure_dirs()
        tasks = Tasks(settings, Shell(settings))
        due = tasks.due(only, include_manual)
    except LocalError as exc:
        print(f"local-context: {exc}", file=sys.stderr)
        return 1
    if not due:
        print("no tasks due", file=sys.stderr)
        return 0
    failed = 0
    for task in due:
        try:
            text = tasks.run(task.name, settings.max_output_chars)
        except LocalError as exc:
            print(f"{task.name} error: {exc}", file=sys.stderr)
            failed = 1
            continue
        first = text.splitlines()[0] if text else task.name
        print(first, file=sys.stderr)
    return failed
