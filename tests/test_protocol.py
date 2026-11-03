from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from local_context.protocol import handle_message
from local_context.server import INSTRUCTIONS


ROOT = Path(__file__).resolve().parents[1]


def test_manifests_agree() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "local-context"
    assert "claude" not in plugin["name"]
    assert market["plugins"][0]["name"] == plugin["name"]
    assert market["plugins"][0]["source"] == "./"
    server = mcp["mcpServers"]["local-context"]
    assert server["args"][0].endswith("run.py")
    assert "LOCAL_CONTEXT_DATA" in server["env"]
    assert example["allow_sql_writes"] is False


def test_initialize_and_tools_over_the_app(app) -> None:
    started = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        app,
    )
    assert started is not None
    assert started["result"]["protocolVersion"] == "2025-06-18"
    assert started["result"]["instructions"] == INSTRUCTIONS
    assert "never calls the Claude API" in INSTRUCTIONS
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, app) is None
    listed = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, app)
    assert listed is not None
    assert any(tool["name"] == "read_file" for tool in listed["result"]["tools"])
    called = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "server_status", "arguments": {}},
        },
        app,
    )
    assert called is not None
    assert called["result"]["isError"] is False
    assert "api_calls=0" in called["result"]["content"][0]["text"]


def test_stdio_round_trip(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "LOCAL_CONTEXT_ROOT": str(tmp_path),
            "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        }
    )
    env.pop("LOCAL_CONTEXT_CONFIG", None)
    env.pop("LOCAL_CONTEXT_PROJECT", None)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        started = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}},
        )
        assert started["result"]["protocolVersion"] == "2026-07-28"
        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "query_sql" in names
        status = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "server_status", "arguments": {}}},
        )
        assert "api_calls=0" in status["result"]["content"][0]["text"]
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise AssertionError(stderr)


def _rpc(proc: subprocess.Popen, message: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
    proc.stdin.flush()
    line = _readline(proc.stdout, proc, 15)
    return json.loads(line)


def _readline(pipe, proc: subprocess.Popen, timeout: float) -> bytes:
    holder: list[bytes] = []

    def read() -> None:
        holder.append(pipe.readline())

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        proc.kill()
        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read()
        raise AssertionError(f"stdio timed out\n{stderr.decode('utf-8', errors='replace')}")
    if not holder or holder[0] in (b"", b"\n"):
        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read()
        raise AssertionError(f"empty stdio line\n{stderr.decode('utf-8', errors='replace')}")
    return holder[0]
