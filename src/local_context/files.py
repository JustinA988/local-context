"""Read-only file tools. Results are always passed through the size cap."""

from __future__ import annotations

import os
import re
from pathlib import Path

from local_context.budget import MAX_READ_OFFSET, fit, page
from local_context.config import Settings
from local_context.errors import LocalError

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
_BINARY_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".exe",
    ".dll",
    ".db",
    ".sqlite",
    ".pyc",
    ".woff",
    ".ico",
    ".mp3",
    ".mp4",
    ".bin",
}
_MAX_SCAN_FILES = 400
_MAX_SCAN_BYTES = 1_000_000
_MAX_TOTAL_READ = 8_000_000
_MAX_LIST_ENTRIES = 5_000
_MAX_LINE = 200


class Files:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = settings.policy

    def list_dir(self, raw: str, entry_limit: int, char_limit: int) -> str:
        path = self.policy.resolve_read(raw or ".")
        if not path.exists():
            raise LocalError("not found")
        if not path.is_dir():
            raise LocalError("path is not a directory")
        children = []
        truncated_list = False
        try:
            for child in path.iterdir():
                children.append(child)
                if len(children) >= _MAX_LIST_ENTRIES:
                    truncated_list = True
                    break
        except OSError as exc:
            raise LocalError(f"list failed: {exc.strerror or exc}") from exc
        entries = []
        for child in children:
            kind = "d" if child.is_dir() else "f"
            entries.append((0 if kind == "d" else 1, child.name.casefold(), f"{kind} {child.name}"))
        entries.sort()
        shown = entries[:entry_limit]
        total = f"{len(entries)}+" if truncated_list else str(len(entries))
        lines = [f"dir {path.name or path} entries={len(shown)} total={total}"]
        lines.extend(item[2] for item in shown)
        if truncated_list or len(entries) > entry_limit:
            lines.append("more=yes")
        return page("\n".join(lines), 0, char_limit)

    def search(self, query: str, raw: str, regex: bool, match_limit: int, char_limit: int) -> str:
        needle = (query or "").strip()
        if not needle:
            raise LocalError("query is required")
        if len(needle) > 200:
            raise LocalError("query exceeds 200 characters")
        pattern: re.Pattern[str] | None = None
        if regex:
            if re.search(r"[*+?{]", needle):
                raise LocalError("regex quantifiers are not allowed")
            try:
                pattern = re.compile(needle)
            except re.error as exc:
                raise LocalError(f"invalid regex: {exc}") from exc
        root = self.policy.resolve_read(raw or ".")
        if not root.exists():
            raise LocalError("not found")
        hits: list[str] = []
        scanned = 0
        read_bytes = 0
        more = False
        files = [root] if root.is_file() else _walk(root)
        folded = needle.casefold()
        for file_path in files:
            scanned += 1
            if scanned > _MAX_SCAN_FILES or read_bytes > _MAX_TOTAL_READ:
                more = True
                break
            try:
                resolved = file_path.resolve()
            except OSError:
                continue
            if not self.policy.allows(resolved):
                continue
            if not _searchable(resolved):
                continue
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            read_bytes += len(text.encode("utf-8", errors="replace"))
            for number, line in enumerate(text.splitlines(), start=1):
                matched = pattern.search(line) if pattern else folded in line.casefold()
                if not matched:
                    continue
                snippet = line.strip()
                if len(snippet) > _MAX_LINE:
                    snippet = snippet[: _MAX_LINE - 3] + "..."
                try:
                    rel = file_path.relative_to(root if root.is_dir() else root.parent)
                except ValueError:
                    rel = file_path.name
                hits.append(f"{rel}:{number}:{snippet}")
                if len(hits) >= match_limit:
                    more = True
                    break
            if len(hits) >= match_limit:
                break
        header = f"matches={len(hits)} more={'yes' if more else 'no'}\n"
        return fit(header, "\n".join(hits), char_limit, 0)

    def read(self, raw: str, offset: int, char_limit: int) -> str:
        path = self.policy.resolve_read(raw)
        return self.read_resolved(path, offset, char_limit)

    def read_resolved(self, path: Path, offset: int, char_limit: int) -> str:
        if offset < 0:
            offset = 0
        if offset > MAX_READ_OFFSET:
            raise LocalError(f"offset exceeds {MAX_READ_OFFSET}")
        if not path.exists():
            raise LocalError("not found")
        if path.is_dir():
            raise LocalError("path is a directory")
        if not self.policy.allows(path):
            raise LocalError("path is outside an allowed root")
        byte_size = path.stat().st_size
        rel = _display(path, self.settings.roots)
        if _looks_binary(path):
            return f"binary {rel} bytes={byte_size}"
        header = f"file {rel} bytes={byte_size} offset={offset}\n"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                remaining = offset
                while remaining:
                    block = handle.read(min(remaining, 8192))
                    if not block:
                        return header + "[past end]"
                    remaining -= len(block)
                notice_room = 48
                take = max(1, char_limit - len(header) - notice_room)
                body = handle.read(take)
                extra = handle.read(1)
        except OSError as exc:
            raise LocalError(f"read failed: {exc.strerror or exc}") from exc
        has_more = extra != ""
        if not has_more and len(header) + len(body) <= char_limit:
            return header + body
        while True:
            notice = f"\n[more; offset={offset + len(body)}]"
            if len(header) + len(body) + len(notice) <= char_limit:
                return header + body + notice
            if not body:
                return (header + notice)[:char_limit]
            body = body[:-1]


def _walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and name != ".claude"]
        for name in filenames:
            found.append(Path(dirpath) / name)
            if len(found) > _MAX_SCAN_FILES:
                return found
    return found


def _searchable(path: Path) -> bool:
    if path.suffix.casefold() in _BINARY_EXT:
        return False
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return False
    except OSError:
        return False
    return not _looks_binary(path)


def _looks_binary(path: Path) -> bool:
    if path.suffix.casefold() in _BINARY_EXT:
        return True
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return False


def _display(path: Path, roots: tuple[Path, ...]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return path.name
