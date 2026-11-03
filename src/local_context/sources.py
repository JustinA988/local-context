"""Plugged-in data: files, JSON, CSV, SQLite, and cached command output."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from local_context.budget import MAX_CAPTURE_CHARS, fit, page
from local_context.config import Settings, SourceSpec
from local_context.errors import LocalError
from local_context.files import Files
from local_context.shell import Shell

_MAX_JSON_BYTES = 2_000_000


class Sources:
    def __init__(self, settings: Settings, files: Files, shell: Shell) -> None:
        self.settings = settings
        self.files = files
        self.shell = shell
        self._by_name = {source.name: source for source in settings.sources}

    def list_sources(self, char_limit: int) -> str:
        if not self.settings.sources:
            return "sources=(none)"
        lines = []
        for source in self.settings.sources:
            detail = source.type if source.type == "command" else f"{source.type} {Path(source.path).name}"
            lines.append(f"{source.name} {detail}")
        return page("\n".join(lines), 0, char_limit)

    def read(
        self,
        name: str,
        raw_path: str,
        offset: int,
        char_limit: int,
        pointer: str,
        row_limit: int,
    ) -> str:
        source = self._get(name)
        if source.type == "command":
            cache = self._cache_file(source)
            if not cache.is_file():
                return f"source {name} is empty; call refresh_source"
            return self.files.read_resolved(cache, offset, char_limit)
        if source.type == "file":
            return self.files.read_resolved(Path(source.path), offset, char_limit)
        if source.type == "directory":
            return self._read_directory(source, raw_path, offset, char_limit)
        if source.type == "json":
            return self._read_json(source, pointer, row_limit, char_limit)
        if source.type == "csv":
            return self._read_csv(source, offset, row_limit, char_limit)
        if source.type == "sqlite":
            raise LocalError("use query_sql for a sqlite source")
        raise LocalError(f"unsupported source type: {source.type}")

    def refresh(self, name: str) -> str:
        source = self._get(name)
        if source.type != "command":
            raise LocalError("source is not a command")
        result = self.shell.run(source.command, source.cwd, self.settings.command_timeout)
        self.settings.ensure_dirs()
        cache = self._cache_file(source)
        text = result.output[:MAX_CAPTURE_CHARS]
        cache.write_text(text, encoding="utf-8")
        return (
            f"refreshed {name} exit={result.exit_code} bytes={cache.stat().st_size} "
            f"timed_out={str(result.timed_out).lower()}"
        )

    def sqlite_path(self, name_or_path: str) -> tuple[Path, bool]:
        text = (name_or_path or "").strip()
        if text and text in self._by_name:
            source = self._by_name[text]
            if source.type != "sqlite":
                raise LocalError(f"source {text} is not sqlite")
            writable = self.settings.allow_sql_writes and not source.read_only
            return Path(source.path), writable
        if not text:
            sqlite_sources = [source for source in self.settings.sources if source.type == "sqlite"]
            if len(sqlite_sources) == 1:
                source = sqlite_sources[0]
                writable = self.settings.allow_sql_writes and not source.read_only
                return Path(source.path), writable
            raise LocalError("pass a database path or sqlite source name")
        path = self.settings.policy.resolve_read(text)
        return path, False

    def _read_directory(self, source: SourceSpec, raw_path: str, offset: int, char_limit: int) -> str:
        root = Path(source.path)
        if not raw_path.strip():
            return self.files.list_dir(str(root), 80, char_limit)
        candidate = (root / raw_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise LocalError("path escapes the source") from exc
        return self.files.read_resolved(candidate, offset, char_limit)

    def _read_json(self, source: SourceSpec, pointer: str, item_limit: int, char_limit: int) -> str:
        path = Path(source.path)
        if not path.is_file():
            raise LocalError("source file not found")
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise LocalError("json source is larger than 2MB; split it locally")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise LocalError(f"invalid json: {exc}") from exc
        node = _pointer(data, pointer)
        body = _summarize(node, item_limit)
        header = f"json {source.name} pointer={pointer or '/'}\n"
        return fit(header, body, char_limit, 0)

    def _read_csv(self, source: SourceSpec, offset: int, row_limit: int, char_limit: int) -> str:
        path = Path(source.path)
        if not path.is_file():
            raise LocalError("source file not found")
        if offset < 0:
            offset = 0
        lines: list[str] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return f"csv {source.name} rows=0"
            shown = 0
            skipped = 0
            more = False
            for row in reader:
                if skipped < offset:
                    skipped += 1
                    continue
                if shown >= row_limit:
                    more = True
                    break
                cells = []
                for cell in row:
                    text = cell.replace("\n", " ").replace("|", "/")
                    if len(text) > 80:
                        text = text[:77] + "..."
                    cells.append(text)
                lines.append("|".join(cells))
                shown += 1
        head = "|".join(header)
        body = head + ("\n" + "\n".join(lines) if lines else "")
        notice = f"rows={shown} more={'yes' if more else 'no'}\n"
        return fit(f"csv {source.name} {notice}", body, char_limit, 0)

    def _cache_file(self, source: SourceSpec) -> Path:
        return self.settings.cache_dir / f"{source.name}.txt"

    def _get(self, name: str) -> SourceSpec:
        source = self._by_name.get((name or "").strip())
        if source is None:
            raise LocalError("unknown source")
        return source


def _pointer(node, pointer: str):
    text = (pointer or "").strip()
    if text in {"", "/"}:
        return node
    if not text.startswith("/"):
        raise LocalError("pointer must start with /")
    current = node
    for part in text.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise LocalError("pointer does not match") from exc
        elif isinstance(current, dict):
            if part not in current:
                raise LocalError("pointer does not match")
            current = current[part]
        else:
            raise LocalError("pointer does not match")
    return current


def _summarize(node, item_limit: int) -> str:
    if isinstance(node, list):
        preview = [_shrink(item) for item in node[:item_limit]]
        body = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
        return f"list length={len(node)} showing={len(preview)}\n{body}"
    if isinstance(node, dict):
        keys = list(node.keys())
        preview = {key: _shrink(node[key]) for key in keys[:item_limit]}
        body = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
        return f"object keys={len(keys)} showing={len(preview)}\n{body}"
    return json.dumps(node, ensure_ascii=False)


def _shrink(value):
    if isinstance(value, list):
        return {"_": "list", "length": len(value)}
    if isinstance(value, dict):
        return {"_": "object", "keys": len(value)}
    if isinstance(value, str) and len(value) > 80:
        return value[:77] + "..."
    return value
