"""Named tasks. The runner executes them with no model call."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from local_context.budget import MAX_CAPTURE_CHARS, page, with_header
from local_context.config import Settings, TaskSpec
from local_context.errors import LocalError
from local_context.shell import Shell


class Tasks:
    def __init__(self, settings: Settings, shell: Shell) -> None:
        self.settings = settings
        self.shell = shell
        self._by_name = {task.name: task for task in settings.tasks}

    def list_tasks(self, char_limit: int) -> str:
        if not self.settings.tasks:
            return "tasks=(none)"
        lines = []
        for task in self.settings.tasks:
            schedule = "manual" if task.every_seconds == 0 else f"every={task.every_seconds}s"
            lines.append(f"{task.name} {schedule}")
        return page("\n".join(lines), 0, char_limit)

    def run(self, name: str, char_limit: int) -> str:
        task = self._get(name)
        started = time.perf_counter()
        result = self.shell.run(task.command, task.cwd, self.settings.command_timeout)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._record(task.name, result.exit_code, result.output, duration_ms)
        header = (
            f"task={task.name} exit={result.exit_code} "
            f"timed_out={str(result.timed_out).lower()} ms={duration_ms}\n"
        )
        return with_header(header, result.output or "(no output)", 0, char_limit)

    def status(self, name: str, offset: int, char_limit: int) -> str:
        target = (name or "").strip()
        row = self._latest(target)
        if row is None:
            return "no task runs"
        header = f"task={row['name']} exit={row['exit_code']} at={row['started_at']}\n"
        return with_header(header, row["output"], offset, char_limit)

    def due(self, only: str | None, include_manual: bool) -> list[TaskSpec]:
        if only:
            return [self._get(only)]
        ready: list[TaskSpec] = []
        now = time.time()
        for task in self.settings.tasks:
            if task.every_seconds <= 0:
                if include_manual:
                    ready.append(task)
                continue
            last = self._latest_started(task.name)
            if last is None or now - last >= task.every_seconds:
                ready.append(task)
        return ready

    def _record(self, name: str, exit_code: int, output: str, duration_ms: int) -> None:
        self.settings.ensure_dirs()
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    started_unix REAL NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    exit_code INTEGER NOT NULL,
                    output TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO runs (name, started_at, started_unix, duration_ms, exit_code, output) VALUES (?, ?, ?, ?, ?, ?)",
                (name, stamp, time.time(), duration_ms, exit_code, output[:MAX_CAPTURE_CHARS]),
            )

    def _latest(self, name: str) -> sqlite3.Row | None:
        if not self._db_path().is_file():
            return None
        with self._connect() as conn:
            if not _has_runs(conn):
                return None
            if name:
                return conn.execute(
                    "SELECT name, started_at, exit_code, output FROM runs WHERE name = ? ORDER BY id DESC LIMIT 1",
                    (name,),
                ).fetchone()
            return conn.execute(
                "SELECT name, started_at, exit_code, output FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def _latest_started(self, name: str) -> float | None:
        if not self._db_path().is_file():
            return None
        with self._connect() as conn:
            if not _has_runs(conn):
                return None
            row = conn.execute(
                "SELECT started_unix FROM runs WHERE name = ? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return float(row["started_unix"])

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _db_path(self):
        return self.settings.data_dir / "tasks.sqlite"

    def _get(self, name: str) -> TaskSpec:
        task = self._by_name.get((name or "").strip())
        if task is None:
            raise LocalError("unknown task")
        return task


def _has_runs(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    return row is not None
