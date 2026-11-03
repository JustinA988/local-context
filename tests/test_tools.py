from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from local_context.config import load_settings
from local_context.errors import LocalError
from local_context.server import build_app


def _call(app, tool: str, **args):
    return app.call(tool, args)


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    exe = sys.executable
    if os.name == "nt":
        return f'& "{exe}" "{path}"'
    return f'"{exe}" "{path}"'


def test_read_list_and_search(app, tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("Hello local context\n", encoding="utf-8")
    listed, err = _call(app, "list_dir")
    assert not err
    assert "note.txt" in listed
    found, err = _call(app, "search_text", query="hello")
    assert not err
    assert "note.txt:1:" in found
    body, err = _call(app, "read_file", path="note.txt")
    assert not err
    assert "Hello local context" in body


def test_large_read_is_capped_and_resumes(app, tmp_path: Path) -> None:
    payload = "0123456789" * 400
    (tmp_path / "big.txt").write_text(payload, encoding="utf-8")
    first, err = _call(app, "read_file", path="big.txt", limit=180)
    assert not err
    assert len(first) <= 180
    assert "offset=" in first
    offset = int(first.rsplit("offset=", 1)[1].rstrip("]"))
    second, err = _call(app, "read_file", path="big.txt", offset=offset, limit=180)
    assert not err
    assert payload[offset : offset + 20] in second


def test_binary_and_missing_file(app, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00secret-bytes")
    text, err = _call(app, "read_file", path="blob.bin")
    assert not err
    assert text.startswith("binary ")
    assert "secret-bytes" not in text
    missing, err = _call(app, "read_file", path="nope.txt")
    assert err
    assert "not found" in missing


def test_path_escape_and_claude_directory(app, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    escaped, err = _call(app, "read_file", path=str(outside))
    assert err
    assert "outside-secret" not in escaped
    secret_dir = tmp_path / ".claude"
    secret_dir.mkdir()
    (secret_dir / "settings.json").write_text("oauth-token-value", encoding="utf-8")
    blocked, err = _call(app, "read_file", path=str(secret_dir / "settings.json"))
    assert err
    assert "oauth-token-value" not in blocked
    outside.unlink(missing_ok=True)


def test_shell_runs_and_hides_anthropic_credentials(app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    text, err = _call(
        app,
        "run_command",
        command=_script(
            tmp_path,
            "show_env.py",
            "import os\nprint(os.environ.get('ANTHROPIC_API_KEY', ''))\n",
        ),
    )
    assert not err
    assert "sk-ant-secret-value" not in text
    assert "exit=0" in text


def test_shell_blocks_anthropic_api_and_timeouts(app, tmp_path: Path) -> None:
    blocked, err = _call(app, "run_command", command="curl https://api.anthropic.com/v1/messages")
    assert err
    assert "blocked" in blocked
    timed, err = _call(
        app,
        "run_command",
        command=_script(tmp_path, "sleep.py", "import time\ntime.sleep(30)\n"),
        timeout_seconds=2,
    )
    assert not err
    assert "timed_out=true" in timed


def test_shell_can_be_disabled(tmp_path: Path) -> None:
    env = {
        "LOCAL_CONTEXT_ROOT": str(tmp_path),
        "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        "LOCAL_CONTEXT_ALLOW_SHELL": "false",
    }
    settings = load_settings(env, cwd=tmp_path)
    settings.ensure_dirs()
    app = build_app(settings)
    text, err = _call(app, "run_command", command="python -c \"print(1)\"")
    assert err
    assert "disabled" in text


def test_query_sql_is_read_only_and_capped(app, tmp_path: Path) -> None:
    database = tmp_path / "shop.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("create table t (id integer)")
    conn.executemany("insert into t values (?)", [(i,) for i in range(30)])
    conn.commit()
    conn.close()
    text, err = _call(app, "query_sql", sql="select id from t order by id", database=str(database), limit=5)
    assert not err
    assert "rows=5" in text
    assert "truncated=yes" in text
    assert "\n0\n" in f"\n{text}\n"
    assert "\n29\n" not in f"\n{text}\n"
    denied, err = _call(app, "query_sql", sql="insert into t values (99)", database=str(database))
    assert err
    assert "read-only" in denied
    check = sqlite3.connect(database)
    count = check.execute("select count(*) from t").fetchone()[0]
    check.close()
    assert count == 30
    multi, err = _call(app, "query_sql", sql="select 1; select 2", database=str(database))
    assert err


def test_json_csv_and_command_sources(tmp_path: Path) -> None:
    (tmp_path / "items.json").write_text(
        json.dumps({"items": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]}),
        encoding="utf-8",
    )
    (tmp_path / "rows.csv").write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")
    config = {
        "roots": [str(tmp_path)],
        "sources": [
            {"name": "items", "type": "json", "path": "items.json"},
            {"name": "rows", "type": "csv", "path": "rows.csv"},
            {"name": "stamp", "type": "command", "command": _script(tmp_path, "stamp.py", "print('payload-value')\n")},
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = {
        "LOCAL_CONTEXT_CONFIG": str(config_path),
        "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        "LOCAL_CONTEXT_ALLOW_SHELL": "true",
    }
    settings = load_settings(env, cwd=tmp_path)
    settings.ensure_dirs()
    app = build_app(settings)
    listed, err = _call(app, "list_sources")
    assert not err
    assert "items" in listed and "rows" in listed and "stamp" in listed
    node, err = _call(app, "read_source", name="items", pointer="/items/1")
    assert not err
    assert "beta" in node
    assert "alpha" not in node
    table, err = _call(app, "read_source", name="rows", offset=1, limit=1)
    assert not err
    assert "beta" in table
    refreshed, err = _call(app, "refresh_source", name="stamp")
    assert not err
    assert "payload-value" not in refreshed
    assert "exit=0" in refreshed
    cached, err = _call(app, "read_source", name="stamp")
    assert not err
    assert "payload-value" in cached


def test_project_overlay_cannot_add_tasks(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.json").write_text('{"ok": true}', encoding="utf-8")
    (project / ".local-context.json").write_text(
        json.dumps(
            {
                "allow_shell": True,
                "tasks": [{"name": "bad", "command": "python -c \"print(1)\""}],
                "sources": [
                    {"name": "notes", "type": "json", "path": "notes.json"},
                    {"name": "remote", "type": "command", "command": "python -c \"print(1)\""},
                ],
            }
        ),
        encoding="utf-8",
    )
    env = {
        "LOCAL_CONTEXT_PROJECT": str(project),
        "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        "LOCAL_CONTEXT_ALLOW_SHELL": "false",
    }
    settings = load_settings(env, cwd=tmp_path)
    settings.ensure_dirs()
    app = build_app(settings)
    assert settings.tasks == ()
    assert settings.allow_shell is False
    status, err = _call(app, "server_status")
    assert not err
    assert "api_calls=0" in status
    assert "ignored" in status
    body, err = _call(app, "read_source", name="notes")
    assert not err
    assert "true" in body
    missing, err = _call(app, "refresh_source", name="remote")
    assert err


def test_tasks_run_locally_and_schedule(tmp_path: Path) -> None:
    config = {
        "roots": [str(tmp_path)],
        "tasks": [
            {"name": "greet", "command": _script(tmp_path, "greet.py", "print('task-ok')\n"), "every_seconds": 3600},
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = {
        "LOCAL_CONTEXT_CONFIG": str(config_path),
        "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        "LOCAL_CONTEXT_ALLOW_SHELL": "true",
    }
    settings = load_settings(env, cwd=tmp_path)
    settings.ensure_dirs()
    from local_context.shell import Shell
    from local_context.tasks import Tasks

    tasks = Tasks(settings, Shell(settings))
    assert [task.name for task in tasks.due(None, False)] == ["greet"]
    app = build_app(settings)
    text, err = _call(app, "run_task", name="greet")
    assert not err
    assert "task-ok" in text
    assert len(text) <= settings.max_output_chars
    assert tasks.due(None, False) == []
    status, err = _call(app, "task_status", name="greet", offset=0)
    assert not err
    assert "task-ok" in status


def test_credential_source_is_rejected(tmp_path: Path) -> None:
    secret = tmp_path / ".claude" / "auth.json"
    secret.parent.mkdir()
    secret.write_text("{}", encoding="utf-8")
    config = {"roots": [str(tmp_path)], "sources": [{"name": "auth", "type": "file", "path": str(secret)}]}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = {"LOCAL_CONTEXT_CONFIG": str(config_path), "LOCAL_CONTEXT_DATA": str(tmp_path / "data")}
    with pytest.raises(LocalError):
        load_settings(env, cwd=tmp_path)


def test_tool_schema_stays_small(app) -> None:
    raw = json.dumps(app.tool_specs(), separators=(",", ":"))
    assert len(raw) < 7000
    names = {tool["name"] for tool in app.tool_specs()}
    assert names == {
        "list_dir",
        "search_text",
        "read_file",
        "run_command",
        "query_sql",
        "list_sources",
        "read_source",
        "refresh_source",
        "list_tasks",
        "run_task",
        "task_status",
        "server_status",
    }


def test_server_status_reports_no_api_calls(app) -> None:
    text, err = _call(app, "server_status")
    assert not err
    assert "api_calls=0" in text
    assert "credential_paths=blocked" in text


def test_shell_blocks_credential_paths_and_split_hosts(app) -> None:
    desktop, err = _call(app, "run_command", command=r'Get-Content "$env:APPDATA\Claude\config.json"')
    assert err
    assert "blocked" in desktop
    split, err = _call(app, "run_command", command="curl ('https://api.' + 'anthropic.com/v1/messages')")
    assert err
    assert "blocked" in split
    quoted, err = _call(app, "run_command", command="Get-Content (Join-Path $env:USERPROFILE '.claude')")
    assert err
    assert "blocked" in quoted
    encoded, err = _call(app, "run_command", command="powershell -enc QQ==")
    assert err
    assert "blocked" in encoded
    wildcard, err = _call(app, "run_command", command=r"Get-ChildItem $env:USERPROFILE\.claude*")
    assert err
    assert "blocked" in wildcard


def test_credential_filenames_are_blocked(app, tmp_path: Path) -> None:
    secret = tmp_path / ".claude.json"
    secret.write_text("oauth-file-token", encoding="utf-8")
    text, err = _call(app, "read_file", path=".claude.json")
    assert err
    assert "oauth-file-token" not in text
    desktop = tmp_path / "AppData" / "Local" / "Claude"
    desktop.mkdir(parents=True)
    (desktop / "config.json").write_text("desktop-local-token", encoding="utf-8")
    text, err = _call(app, "read_file", path=str(desktop / "config.json"))
    assert err
    assert "desktop-local-token" not in text


def test_regex_quantifiers_are_refused(app, tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("a" * 40, encoding="utf-8")
    text, err = _call(app, "search_text", query="(a+)+$", regex=True)
    assert err
    assert "quantifier" in text


def test_slow_sql_is_interrupted(app, tmp_path: Path) -> None:
    database = tmp_path / "slow.sqlite"
    sqlite3.connect(database).close()
    app.settings.command_timeout = 1
    text, err = _call(
        app,
        "query_sql",
        sql="WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 200000000) SELECT count(*) FROM c",
        database=str(database),
    )
    assert err
    assert "timed out" in text


def test_search_does_not_follow_a_symlink_outside_the_root(app, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"secret-{tmp_path.name}.txt"
    outside.write_text("symlink-secret-token", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        outside.unlink(missing_ok=True)
        pytest.skip("symlink creation is not permitted")
    try:
        text, err = _call(app, "search_text", query="symlink-secret-token")
        assert "symlink-secret-token" not in text
        direct, direct_err = _call(app, "read_file", path="link.txt")
        assert direct_err
        assert "symlink-secret-token" not in direct
    finally:
        outside.unlink(missing_ok=True)


def test_search_skips_a_path_that_resolves_outside(app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path.parent / f"resolved-{tmp_path.name}.txt"
    outside.write_text("resolved-secret-token", encoding="utf-8")
    (tmp_path / "link.txt").write_text("local text", encoding="utf-8")
    original = Path.resolve

    def resolve(self: Path, *args, **kwargs):
        if self.name == "link.txt":
            return outside
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    try:
        text, err = _call(app, "search_text", query="resolved-secret-token")
        assert not err
        assert "resolved-secret-token" not in text
        direct, direct_err = _call(app, "read_file", path="link.txt")
        assert direct_err
        assert "resolved-secret-token" not in direct
    finally:
        outside.unlink(missing_ok=True)


def test_search_skips_a_path_that_resolves_outside(app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path.parent / f"resolved-{tmp_path.name}.txt"
    outside.write_text("resolved-secret-token", encoding="utf-8")
    (tmp_path / "link.txt").write_text("local text", encoding="utf-8")
    original = Path.resolve

    def resolve(self: Path, *args, **kwargs):
        if self.name == "link.txt":
            return outside
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    try:
        text, err = _call(app, "search_text", query="resolved-secret-token")
        assert not err
        assert "resolved-secret-token" not in text
        direct, direct_err = _call(app, "read_file", path="link.txt")
        assert direct_err
        assert "resolved-secret-token" not in direct
    finally:
        outside.unlink(missing_ok=True)


def test_output_cap_stays_inside_the_hard_ceiling(tmp_path: Path) -> None:
    from local_context.budget import MAX_OUTPUT_CHARS, MIN_OUTPUT_CHARS

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"roots": [str(tmp_path)], "max_output_chars": 1_000_000}),
        encoding="utf-8",
    )
    settings = load_settings(
        {"LOCAL_CONTEXT_CONFIG": str(config_path), "LOCAL_CONTEXT_DATA": str(tmp_path / "data")},
        cwd=tmp_path,
    )
    assert settings.max_output_chars == MAX_OUTPUT_CHARS
    tiny = load_settings(
        {
            "LOCAL_CONTEXT_ROOT": str(tmp_path),
            "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
            "LOCAL_CONTEXT_MAX_CHARS": "10",
        },
        cwd=tmp_path,
    )
    assert tiny.max_output_chars == MIN_OUTPUT_CHARS
