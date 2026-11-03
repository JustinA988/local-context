"""Load the plugin config, then the project overlay."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from local_context.budget import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_OUTPUT_CHARS,
    DEFAULT_ROWS,
    MAX_COMMAND_TIMEOUT,
    MAX_OUTPUT_CHARS,
    MAX_ROWS,
    MIN_OUTPUT_CHARS,
    clamp_int,
)
from local_context.errors import LocalError
from local_context.sandbox import Policy

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SOURCE_TYPES = {"file", "directory", "json", "csv", "sqlite", "command"}
_PROJECT_TYPES = {"file", "directory", "json", "csv", "sqlite"}
_SHELLS = {"auto", "powershell", "cmd", "bash", "sh"}


@dataclass(frozen=True)
class SourceSpec:
    name: str
    type: str
    path: str
    command: str
    read_only: bool
    cwd: str


@dataclass(frozen=True)
class TaskSpec:
    name: str
    command: str
    cwd: str
    every_seconds: int


@dataclass
class Settings:
    roots: tuple[Path, ...]
    project: Path | None
    data_dir: Path
    cache_dir: Path
    policy: Policy
    max_output_chars: int
    command_timeout: int
    max_rows: int
    allow_shell: bool
    allow_sql_writes: bool
    shell: str
    sources: tuple[SourceSpec, ...]
    tasks: tuple[TaskSpec, ...]
    warnings: tuple[str, ...]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


def load_settings(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    values = dict(os.environ if env is None else env)
    here = Path.cwd() if cwd is None else Path(cwd)
    warnings: list[str] = []

    config_path, config, config_dir = _load_config_file(values, here)
    roots = _roots(values, here, config, config_dir)
    if not roots:
        raise LocalError("no allowed root")

    data_dir = _data_dir(values, here)
    cache_dir = data_dir / "cache"
    policy = Policy(roots, cache_dir)
    raw_deny = config.get("extra_deny") or []
    if not isinstance(raw_deny, list):
        raise LocalError("extra_deny must be a list")
    for pattern in raw_deny:
        if not isinstance(pattern, str) or not pattern.strip():
            raise LocalError("extra_deny entries must be strings")
        try:
            policy.extra_deny.append((re.compile(pattern), "config rule"))
        except re.error as exc:
            raise LocalError(f"invalid extra_deny pattern: {exc}") from exc

    shell = str(config.get("shell") or "auto").strip().lower()
    if shell not in _SHELLS:
        raise LocalError(f"unknown shell: {shell}")

    max_chars = clamp_int(
        _override(values, "LOCAL_CONTEXT_MAX_CHARS", config.get("max_output_chars", DEFAULT_OUTPUT_CHARS)),
        DEFAULT_OUTPUT_CHARS,
        MIN_OUTPUT_CHARS,
        MAX_OUTPUT_CHARS,
    )
    timeout = clamp_int(
        config.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT),
        DEFAULT_COMMAND_TIMEOUT,
        1,
        MAX_COMMAND_TIMEOUT,
    )
    max_rows = clamp_int(config.get("max_rows", DEFAULT_ROWS), DEFAULT_ROWS, 1, MAX_ROWS)
    allow_shell = _bool_override(
        values,
        "LOCAL_CONTEXT_ALLOW_SHELL",
        bool(config.get("allow_shell", True)),
    )
    allow_sql_writes = bool(config.get("allow_sql_writes", False))

    sources = _sources(config.get("sources") or [], config_dir, roots, policy, trusted=True)
    tasks = tuple(_tasks(config.get("tasks") or [], roots, policy))

    project = _optional_dir(values.get("LOCAL_CONTEXT_PROJECT"))
    if project is not None:
        overlay, overlay_warnings = _project_overlay(project, sources, policy)
        sources = sources + overlay
        warnings.extend(overlay_warnings)

    return Settings(
        roots=tuple(roots),
        project=project,
        data_dir=data_dir,
        cache_dir=cache_dir,
        policy=policy,
        max_output_chars=max_chars,
        command_timeout=timeout,
        max_rows=max_rows,
        allow_shell=allow_shell,
        allow_sql_writes=allow_sql_writes,
        shell=shell,
        sources=sources,
        tasks=tasks,
        warnings=tuple(warnings),
    )


def _load_config_file(
    env: Mapping[str, str],
    cwd: Path,
) -> tuple[Path | None, dict, Path]:
    configured = _clean(env.get("LOCAL_CONTEXT_CONFIG"))
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    else:
        home = _clean(env.get("LOCAL_CONTEXT_HOME"))
        if home:
            candidates.append(Path(home) / "config.json")
        candidates.append(cwd / "config.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise LocalError(f"invalid config {candidate}: {exc}") from exc
        if not isinstance(data, dict):
            raise LocalError("config must be a JSON object")
        return candidate, data, candidate.parent
    return None, {}, cwd


def _roots(env: Mapping[str, str], cwd: Path, config: dict, config_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for raw in config.get("roots") or []:
        if not isinstance(raw, str) or not raw.strip():
            raise LocalError("roots must be paths")
        path = Path(raw)
        if not path.is_absolute():
            path = config_dir / path
        resolved = path.resolve()
        if resolved.exists() and not resolved.is_dir():
            raise LocalError(f"root is not a directory: {resolved}")
        roots.append(resolved)
    extra = _clean(env.get("LOCAL_CONTEXT_ROOT"))
    if extra:
        path = Path(extra).resolve()
        if path.exists() and not path.is_dir():
            raise LocalError(f"root is not a directory: {path}")
        roots.append(path)
    if roots:
        return _dedupe(roots)
    project = _clean(env.get("LOCAL_CONTEXT_PROJECT"))
    if project:
        return [Path(project).resolve()]
    return [cwd.resolve()]


def _data_dir(env: Mapping[str, str], cwd: Path) -> Path:
    configured = _clean(env.get("LOCAL_CONTEXT_DATA"))
    if configured:
        return Path(configured).resolve()
    home = _clean(env.get("LOCAL_CONTEXT_HOME"))
    if home:
        return (Path(home) / "data").resolve()
    return (cwd / "data").resolve()


def _sources(
    raw_sources: object,
    base_dir: Path,
    roots: list[Path],
    policy: Policy,
    *,
    trusted: bool,
) -> tuple[SourceSpec, ...]:
    if not isinstance(raw_sources, list):
        raise LocalError("sources must be a list")
    specs: list[SourceSpec] = []
    names: set[str] = set()
    allowed = _SOURCE_TYPES if trusted else _PROJECT_TYPES
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise LocalError("each source must be an object")
        name = str(raw.get("name") or "")
        kind = str(raw.get("type") or "").lower()
        if not _NAME.match(name):
            raise LocalError(f"invalid source name: {name!r}")
        if name in names:
            raise LocalError(f"duplicate source: {name}")
        if kind not in allowed:
            raise LocalError(f"source {name} has unsupported type {kind!r}")
        command = str(raw.get("command") or "")
        cwd = str(raw.get("cwd") or "")
        read_only = bool(raw.get("read_only", True))
        path_text = ""
        if kind == "command":
            if not command.strip():
                raise LocalError(f"source {name} needs a command")
            policy.check_command(command)
            if cwd:
                cwd = _cwd_inside(cwd, roots, f"source {name}")
        else:
            path_text = str(raw.get("path") or "")
            if not path_text.strip():
                raise LocalError(f"source {name} needs a path")
            resolved = _resolve_under(path_text, base_dir, roots, trusted)
            policy._ensure_not_credential(resolved)
            if kind == "directory":
                policy.grant_dir(resolved)
            else:
                policy.grant_file(resolved)
            path_text = str(resolved)
        names.add(name)
        specs.append(
            SourceSpec(
                name=name,
                type=kind,
                path=path_text,
                command=command,
                read_only=read_only,
                cwd=cwd,
            )
        )
    return tuple(specs)


def _tasks(raw_tasks: object, roots: list[Path], policy: Policy) -> list[TaskSpec]:
    if not isinstance(raw_tasks, list):
        raise LocalError("tasks must be a list")
    specs: list[TaskSpec] = []
    names: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise LocalError("each task must be an object")
        name = str(raw.get("name") or "")
        command = str(raw.get("command") or "")
        if not _NAME.match(name):
            raise LocalError(f"invalid task name: {name!r}")
        if name in names:
            raise LocalError(f"duplicate task: {name}")
        if not command.strip():
            raise LocalError(f"task {name} needs a command")
        policy.check_command(command)
        every = clamp_int(raw.get("every_seconds", 0), 0, 0, 86_400 * 30)
        cwd = str(raw.get("cwd") or "")
        if cwd:
            cwd = _cwd_inside(cwd, roots, f"task {name}")
        names.add(name)
        specs.append(TaskSpec(name=name, command=command, cwd=cwd, every_seconds=every))
    return specs


def _cwd_inside(cwd: str, roots: list[Path], label: str) -> str:
    path = Path(cwd)
    if not path.is_absolute():
        path = roots[0] / path
    resolved = path.resolve()
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise LocalError(f"{label} cwd is outside an allowed root")
    return str(resolved)


def _project_overlay(
    project: Path,
    trusted: tuple[SourceSpec, ...],
    policy: Policy,
) -> tuple[tuple[SourceSpec, ...], list[str]]:
    path = project / ".local-context.json"
    warnings: list[str] = []
    if not path.is_file():
        return tuple(), warnings
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return tuple(), [f"ignored project overlay: {exc}"]
    if not isinstance(data, dict):
        return tuple(), ["ignored project overlay: not an object"]
    for key in ("roots", "tasks", "allow_shell", "allow_sql_writes", "shell"):
        if key in data:
            warnings.append(f"ignored {key} in .local-context.json")
    taken = {source.name for source in trusted}
    accepted: list[SourceSpec] = []
    for raw in data.get("sources") or []:
        if not isinstance(raw, dict):
            warnings.append("ignored a project source that is not an object")
            continue
        name = str(raw.get("name") or "")
        kind = str(raw.get("type") or "").lower()
        if name in taken:
            warnings.append(f"ignored project source {name}: name already used")
            continue
        if kind == "command" or kind not in _PROJECT_TYPES:
            warnings.append(f"ignored project source {name}: type {kind or '(missing)'} is not allowed")
            continue
        try:
            parsed = _sources([raw], project, [project], policy, trusted=False)
        except LocalError as exc:
            warnings.append(f"ignored project source {name or '?'}: {exc}")
            continue
        accepted.extend(parsed)
        taken.add(parsed[0].name)
    return tuple(accepted), warnings


def _resolve_under(raw: str, base_dir: Path, roots: list[Path], trusted: bool) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    resolved = path.resolve()
    if trusted:
        return resolved
    if not _is_relative_to(resolved, roots[0].resolve()):
        raise LocalError("path escapes the project")
    return resolved


def _optional_dir(raw: str | None) -> Path | None:
    text = _clean(raw)
    if not text:
        return None
    path = Path(text).resolve()
    if path.is_dir():
        return path
    return None


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "${" in text:
        return None
    return text


def _override(env: Mapping[str, str], key: str, fallback: object) -> object:
    text = _clean(env.get(key))
    return fallback if text is None else text


def _bool_override(env: Mapping[str, str], key: str, fallback: bool) -> bool:
    text = _clean(env.get(key))
    if text is None:
        return fallback
    return text.lower() in {"1", "true", "yes", "on"}


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
