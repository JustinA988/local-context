"""Path and command guards for a server that runs as the local user."""

from __future__ import annotations

import os
import re
from pathlib import Path

from local_context.budget import MAX_COMMAND_CHARS
from local_context.errors import LocalError

# Removed from child processes so a command cannot spend a Claude credential
# that happens to be in the parent environment.
_STRIP_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
}

_COMMAND_DENY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\.claude(?!-)"), "Claude credential directory"),
    (re.compile(r"(?i)\.claude\.json"), "Claude credential file"),
    (re.compile(r"(?i)api\.anthropic\.com"), "Anthropic API host"),
    (re.compile(r"(?i)console\.anthropic\.com"), "Anthropic console"),
    (re.compile(r"(?i)api\.claude\.ai"), "Claude API host"),
    (re.compile(r"\bANTHROPIC_API_KEY\b", re.I), "Anthropic credential"),
    (re.compile(r"\bANTHROPIC_AUTH_TOKEN\b", re.I), "Anthropic credential"),
    (re.compile(r"\bCLAUDE_CODE_OAUTH_TOKEN\b", re.I), "Claude credential"),
    (re.compile(r"(?i)-EncodedCommand\b"), "encoded command"),
    (re.compile(r"(?i)(?:^|[\s;|&])-(?:enc|ec)(?:\s|$)"), "encoded command"),
    (re.compile(r"(?i)(^|[;&|]\s*)format\s+[a-z]:"), "disk format"),
    (re.compile(r"(?i)\bformat-volume\b"), "disk format"),
    (re.compile(r"(?i)\bclear-disk\b"), "disk wipe"),
    (re.compile(r"(?i)\bcipher\s+/w\b"), "disk wipe"),
    (re.compile(r"(?i)\bdiskpart\b"), "disk partition"),
    (re.compile(r"(?i)\bmkfs(\.[a-z0-9]+)?\b"), "disk format"),
    (re.compile(r"(?i)\bdd\s+if=.*\bof=/dev/"), "disk overwrite"),
    (re.compile(r"(?i)\brm\s+-rf\s+/(?:\s|$)"), "root delete"),
    (re.compile(r"(?i)remove-item\b.*-recurse.*\\windows\b"), "Windows directory delete"),
    (re.compile(r"(?i)\bshutdown\s+/[sr]"), "shutdown"),
    (re.compile(r"(?i)\b(stop-computer|restart-computer)\b"), "shutdown"),
)

# Matched after quotes, spaces, and concatenation are removed, so
# "$env:APPDATA\Claude" and ('api.' + 'anthropic.com') still hit.
_FLAT_DENY: tuple[tuple[str, str], ...] = (
    ("appdataclaude", "Claude credential directory"),
    ("roamingclaude", "Claude credential directory"),
    ("applicationsupportclaude", "Claude credential directory"),
    ("userprofileclaude", "Claude credential directory"),
    ("apianthropiccom", "Anthropic API host"),
    ("consoleanthropiccom", "Anthropic console"),
    ("apiclaudeai", "Claude API host"),
    ("anthropicapikey", "Anthropic credential"),
    ("anthropicauthtoken", "Anthropic credential"),
    ("claudecodeoauthtoken", "Claude credential"),
    ("claudejson", "Claude credential file"),
)


def is_claude_credential_path(path: Path) -> bool:
    """True for Claude Desktop / Claude Code credential locations."""
    parts = [part.casefold() for part in path.parts]
    for part in parts:
        if part == ".claude" or part == ".claude.json" or part.startswith(".claude.json."):
            return True
    for previous, current in zip(parts, parts[1:]):
        if previous == "application support" and current == "claude":
            return True
    for first, second, third in zip(parts, parts[1:], parts[2:]):
        if first == "appdata" and second in {"roaming", "local"} and third == "claude":
            return True
    return False


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class Policy:
    """Allowed read roots, plus an exact-file grant list for plugged-in sources."""

    def __init__(self, roots: list[Path], cache_dir: Path) -> None:
        self.roots = roots
        self.cache_dir = cache_dir
        self.extra_files: set[Path] = set()
        self.extra_dirs: list[Path] = []
        self.extra_deny: list[tuple[re.Pattern[str], str]] = []

    def grant_file(self, path: Path) -> None:
        resolved = path.resolve()
        self._ensure_not_credential(resolved)
        self.extra_files.add(resolved)

    def grant_dir(self, path: Path) -> None:
        resolved = path.resolve()
        self._ensure_not_credential(resolved)
        self.extra_dirs.append(resolved)

    def allows(self, path: Path) -> bool:
        resolved = path.resolve()
        if self._blocked(resolved):
            return False
        if resolved in self.extra_files:
            return True
        for root in (*self.roots, *self.extra_dirs, self.cache_dir):
            if is_under(resolved, root):
                return True
        return False

    def resolve_read(self, raw: str) -> Path:
        text = (raw or "").strip()
        if not text:
            raise LocalError("path is required")
        if "\x00" in text:
            raise LocalError("path is invalid")
        candidate = Path(text)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not self.allows(resolved):
                raise LocalError("path is outside an allowed root")
            return resolved
        matches: list[Path] = []
        for root in self.roots:
            resolved = (root / candidate).resolve()
            if self.allows(resolved):
                matches.append(resolved)
        existing = [item for item in matches if item.exists()]
        if existing:
            return existing[0]
        if matches:
            return matches[0]
        raise LocalError("path is outside an allowed root")

    def resolve_cwd(self, raw: str) -> Path:
        if not (raw or "").strip():
            if not self.roots:
                raise LocalError("no allowed root")
            cwd = self.roots[0]
        else:
            cwd = self.resolve_read(raw)
        if not cwd.is_dir():
            raise LocalError("cwd is not a directory")
        if self._blocked(cwd) or not any(is_under(cwd, root) for root in self.roots):
            raise LocalError("cwd is outside an allowed root")
        return cwd

    def check_command(self, command: str) -> None:
        if not command or not command.strip():
            raise LocalError("command is required")
        if "\x00" in command:
            raise LocalError("command is invalid")
        if len(command) > MAX_COMMAND_CHARS:
            raise LocalError(f"command exceeds {MAX_COMMAND_CHARS} characters")
        for pattern, reason in (*_COMMAND_DENY, *self.extra_deny):
            if pattern.search(command):
                raise LocalError(f"command blocked ({reason})")
        flat = re.sub(r"[^a-z0-9]", "", command.casefold())
        for token, reason in _FLAT_DENY:
            if token in flat:
                raise LocalError(f"command blocked ({reason})")

    def child_env(self, parent: dict[str, str] | None = None) -> dict[str, str]:
        source = os.environ if parent is None else parent
        cleaned: dict[str, str] = {}
        for key, value in source.items():
            upper = key.upper()
            if upper in _STRIP_ENV or "ANTHROPIC" in upper or upper.endswith("_OAUTH_TOKEN"):
                continue
            cleaned[key] = value
        return cleaned

    def _blocked(self, resolved: Path) -> bool:
        if is_under(resolved, self.cache_dir.resolve()):
            return False
        return is_claude_credential_path(resolved)

    @staticmethod
    def _ensure_not_credential(resolved: Path) -> None:
        if is_claude_credential_path(resolved):
            raise LocalError("path is in a Claude credential directory")
