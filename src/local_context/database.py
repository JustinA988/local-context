"""Read-only SQLite queries. Writes stay off unless a source is explicitly writable."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from local_context.budget import fit
from local_context.errors import LocalError

_WRITE_KINDS = {"select", "with", "values", "explain", "pragma"}
_PRAGMA = re.compile(
    r"(?is)^pragma\s+(table_info|table_list|index_list|index_info|database_list|foreign_key_list)\b"
)
_LIMIT_AT_END = re.compile(r"(?is)\blimit\s+\d+\s*$")
_LEADING = re.compile(r"([A-Za-z_]+)")


class Database:
    def __init__(self, allow_sql_writes: bool) -> None:
        self.allow_sql_writes = allow_sql_writes

    def query(self, path: Path, sql: str, limit: int, *, writable: bool, char_limit: int, timeout: float = 20) -> str:
        statement = _one_statement(sql)
        keyword = _keyword(statement)
        write_ok = writable and self.allow_sql_writes
        if keyword not in _WRITE_KINDS and not write_ok:
            raise LocalError("read-only SQL; use SELECT, WITH, VALUES, or EXPLAIN")
        if keyword == "pragma" and not _PRAGMA.match(statement):
            raise LocalError("pragma is not allowed")
        if re.search(r"(?is)\b(attach|detach|load_extension)\b", statement):
            raise LocalError("attach and load_extension are not allowed")
        wrapped = statement
        if keyword in {"select", "with", "values"} and not _LIMIT_AT_END.search(statement):
            wrapped = f"SELECT * FROM ({statement}) AS _q LIMIT {limit + 1}"
        if not path.is_file():
            raise LocalError("database not found")
        uri = path.resolve().as_uri()
        if not write_ok:
            uri += "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise LocalError(str(exc)[:300]) from exc
        try:
            if not write_ok:
                conn.execute("PRAGMA query_only = ON")
            try:
                conn.execute("PRAGMA trusted_schema = OFF")
            except sqlite3.Error:
                pass
            deadline = time.monotonic() + max(0.2, float(timeout))

            def _abort() -> int:
                return 1 if time.monotonic() >= deadline else 0

            conn.set_progress_handler(_abort, 10_000)
            cursor = conn.execute(wrapped)
            if cursor.description is None:
                conn.commit()
                return f"ok rows_affected={cursor.rowcount}"
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            message = str(exc)
            if "interrupt" in message.casefold():
                raise LocalError("query timed out") from exc
            raise LocalError(message[:300]) from exc
        finally:
            conn.close()
        truncated = len(rows) > limit
        lines = ["|".join(columns)]
        for row in rows[:limit]:
            cells = []
            for value in row:
                text = "" if value is None else str(value).replace("\n", " ").replace("|", "/")
                if len(text) > 80:
                    text = text[:77] + "..."
                cells.append(text)
            lines.append("|".join(cells))
        header = f"rows={min(len(rows), limit)} truncated={'yes' if truncated else 'no'}\n"
        return fit(header, "\n".join(lines), char_limit, 0)


def _one_statement(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise LocalError("sql is required")
    if len(text) > 8_000:
        raise LocalError("sql exceeds 8000 characters")
    if "\x00" in text:
        raise LocalError("sql is invalid")
    body = text[:-1].rstrip() if text.endswith(";") else text
    if ";" in body:
        raise LocalError("one statement only")
    return body.strip()


def _keyword(sql: str) -> str:
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    cleaned = re.sub(r"--.*?$", " ", cleaned, flags=re.M).strip().lstrip("(")
    match = _LEADING.match(cleaned)
    return match.group(1).lower() if match else ""
