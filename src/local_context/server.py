"""Tool surface for the local MCP server."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Callable

from local_context import __version__
from local_context.budget import MAX_READ_OFFSET, clamp_int, page
from local_context.config import Settings, load_settings
from local_context.database import Database
from local_context.errors import LocalError
from local_context.files import Files
from local_context.protocol import serve
from local_context.shell import Shell
from local_context.sources import Sources
from local_context.tasks import Tasks

INSTRUCTIONS = (
    "Local tools only. This server never calls the Claude API. "
    "Every result is capped. Search or list before reading. "
    "Use offset to continue a capped result. SQL is read-only unless a source allows writes. "
    "refresh_source and run_task do the work locally and return a short status."
)

_LOG = logging.getLogger("local-context")
ToolFn = Callable[[dict], str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    fn: ToolFn
    read_only: bool = True
    destructive: bool = False
    open_world: bool = False

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "openWorldHint": self.open_world,
            },
        }


class App:
    def __init__(self, settings: Settings, tools: list[Tool]) -> None:
        self.settings = settings
        self.name = "local-context"
        self.version = __version__
        self.instructions = INSTRUCTIONS
        self._tools = tools
        self._calls = {tool.name: tool.fn for tool in tools}

    def tool_specs(self) -> list[dict]:
        return [tool.spec() for tool in self._tools]

    def call(self, name: str, arguments: object) -> tuple[str, bool]:
        _LOG.info("tool %s", name)
        fn = self._calls.get(name)
        if fn is None:
            return f"unknown tool: {name}", True
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return "error: arguments must be an object", True
        try:
            text = fn(arguments)
        except LocalError as exc:
            return f"error: {exc}", True
        except Exception:
            _LOG.exception("tool %s failed", name)
            return "error: internal error", True
        if not isinstance(text, str):
            text = str(text)
        if len(text) > self.settings.max_output_chars:
            text = text[: self.settings.max_output_chars]
        return text, False


def build_app(settings: Settings) -> App:
    files = Files(settings)
    shell = Shell(settings)
    database = Database(settings.allow_sql_writes)
    sources = Sources(settings, files, shell)
    tasks = Tasks(settings, shell)
    chars = settings.max_output_chars

    def read_file(args: dict) -> str:
        return files.read(
            str(args.get("path") or ""),
            _offset(args),
            _chars(args, chars),
        )

    def list_dir(args: dict) -> str:
        return files.list_dir(
            str(args.get("path") or "."),
            clamp_int(args.get("limit", 80), 80, 1, 200),
            chars,
        )

    def search_text(args: dict) -> str:
        return files.search(
            str(args.get("query") or ""),
            str(args.get("path") or "."),
            _flag(args.get("regex"), False),
            clamp_int(args.get("limit", 20), 20, 1, 50),
            chars,
        )

    def run_command(args: dict) -> str:
        result = shell.run(
            str(args.get("command") or ""),
            str(args.get("cwd") or ""),
            clamp_int(args.get("timeout_seconds", settings.command_timeout), settings.command_timeout, 1, settings.command_timeout),
        )
        stored = None
        if result.output:
            settings.ensure_dirs()
            stored = settings.cache_dir / "last-command.txt"
            stored.write_text(result.output, encoding="utf-8")
        return shell.present(result, _chars(args, chars), stored)

    def query_sql(args: dict) -> str:
        path, writable = sources.sqlite_path(str(args.get("database") or ""))
        row_limit = clamp_int(args.get("limit", settings.max_rows), settings.max_rows, 1, settings.max_rows)
        return database.query(
            path,
            str(args.get("sql") or ""),
            row_limit,
            writable=writable,
            char_limit=chars,
            timeout=settings.command_timeout,
        )

    def list_sources(args: dict) -> str:
        return sources.list_sources(_chars(args, chars))

    def read_source(args: dict) -> str:
        return sources.read(
            str(args.get("name") or ""),
            str(args.get("path") or ""),
            _offset(args),
            chars,
            str(args.get("pointer") or ""),
            clamp_int(args.get("limit", settings.max_rows), settings.max_rows, 1, settings.max_rows),
        )

    def refresh_source(args: dict) -> str:
        return sources.refresh(str(args.get("name") or ""))

    def list_tasks(args: dict) -> str:
        return tasks.list_tasks(_chars(args, chars))

    def run_task(args: dict) -> str:
        return tasks.run(str(args.get("name") or ""), _chars(args, chars))

    def task_status(args: dict) -> str:
        return tasks.status(str(args.get("name") or ""), _offset(args), _chars(args, chars))

    def server_status(_args: dict) -> str:
        root_lines = "\n".join(str(root) for root in settings.roots) or "(none)"
        source_text = ", ".join(f"{source.name}:{source.type}" for source in settings.sources) or "(none)"
        task_text = ", ".join(task.name for task in settings.tasks) or "(none)"
        lines = [
            "api_calls=0",
            "credential_paths=blocked",
            f"max_output_chars={settings.max_output_chars}",
            f"command_timeout_seconds={settings.command_timeout}",
            f"allow_shell={str(settings.allow_shell).lower()}",
            f"allow_sql_writes={str(settings.allow_sql_writes).lower()}",
            f"shell={settings.shell}",
            f"sources={source_text}",
            f"tasks={task_text}",
            "roots:",
            root_lines,
        ]
        if settings.warnings:
            lines.append("warnings:")
            lines.extend(settings.warnings)
        return page("\n".join(lines), 0, chars)

    tools = [
        Tool("list_dir", "List names in an allowed directory. Does not read file contents.", _obj({"path": _string("Directory. Defaults to the root."), "limit": _int("Max entries.")}), list_dir),
        Tool("search_text", "Search text under an allowed root. Returns capped matching lines.", _obj({"query": _string("Text or regex to find."), "path": _string("File or directory. Defaults to the root."), "regex": _bool("Treat query as a regex."), "limit": _int("Max matches.")}, ["query"]), search_text),
        Tool("read_file", "Read a capped text slice. offset is a character offset.", _obj({"path": _string("File inside an allowed root."), "offset": _int("Character offset."), "limit": _int("Max characters.")}, ["path"]), read_file),
        Tool("run_command", "Run one command inside an allowed root. Output is capped. Anthropic credentials are removed from the child environment.", _obj({"command": _string("Command line."), "cwd": _string("Working directory inside a root."), "timeout_seconds": _int("Timeout, capped by server config.")}, ["command"]), run_command, read_only=False, destructive=True, open_world=True),
        Tool("query_sql", "Run one SQLite statement. Ad-hoc files are read-only. Rows are capped.", _obj({"sql": _string("One SQL statement."), "database": _string("SQLite source name or path."), "limit": _int("Max rows.")}, ["sql"]), query_sql),
        Tool("list_sources", "List configured external data sources by name and type.", _obj({}), list_sources),
        Tool("read_source", "Read a capped slice of a configured source. pointer is a JSON pointer.", _obj({"name": _string("Source name."), "path": _string("File inside a directory source."), "pointer": _string("JSON pointer."), "offset": _int("Character or row offset."), "limit": _int("Max characters or rows.")}, ["name"]), read_source),
        Tool("refresh_source", "Run a command source locally and store its output. Returns a short status, not the output.", _obj({"name": _string("Command source name.")}, ["name"]), refresh_source, read_only=False, destructive=True, open_world=True),
        Tool("list_tasks", "List named local tasks.", _obj({}), list_tasks),
        Tool("run_task", "Run one named task locally and return a capped status.", _obj({"name": _string("Task name.")}, ["name"]), run_task, read_only=False, destructive=True, open_world=True),
        Tool("task_status", "Read a capped slice of the latest stored task output.", _obj({"name": _string("Task name. Defaults to the latest run."), "offset": _int("Character offset.")}), task_status),
        Tool("server_status", "Show roots, caps, and that this server makes no Claude API calls.", _obj({}), server_status),
    ]
    return App(settings, tools)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        settings = load_settings()
        settings.ensure_dirs()
    except LocalError as exc:
        print(f"local-context: {exc}", file=sys.stderr)
        return 1
    serve(build_app(settings))
    return 0


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": True,
    }


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


def _int(description: str) -> dict:
    return {"type": "integer", "description": description, "minimum": 0}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _chars(args: dict, ceiling: int) -> int:
    return clamp_int(args.get("limit", ceiling), ceiling, 80, ceiling)


def _offset(args: dict) -> int:
    return clamp_int(args.get("offset", 0), 0, 0, MAX_READ_OFFSET)


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)
