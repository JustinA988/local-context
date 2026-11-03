"""Newline-delimited JSON-RPC for MCP stdio. Nothing else is written to stdout."""

from __future__ import annotations

import json
import logging
import os
import re
import sys

_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")
_LOG = logging.getLogger("local-context")
_MAX_MESSAGE_BYTES = 1_000_000
_OUT: object | None = None


def serve(app) -> None:
    """Speak MCP on the original stdout. Later prints go to stderr."""
    global _OUT
    framed = False
    protocol_fd = os.dup(sys.stdout.fileno())
    previous_stdout = sys.stdout
    _OUT = os.fdopen(protocol_fd, "wb", buffering=0)
    sys.stdout = sys.stderr
    stdin = sys.stdin.buffer
    try:
        while True:
            message, used_frame = _read_message(stdin)
            if message is None:
                return
            framed = framed or used_frame
            try:
                response = handle_message(message, app)
            except Exception:
                _LOG.exception("failed to handle message")
                message_id = message.get("id") if isinstance(message, dict) else None
                response = _error(message_id, -32603, "internal error")
            if response is not None:
                try:
                    _write(response, framed)
                except BrokenPipeError:
                    return
    finally:
        sys.stdout = previous_stdout
        if _OUT is not None:
            _OUT.close()
            _OUT = None


def handle_message(message: object, app) -> dict | None:
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid request")
    method = message.get("method")
    has_id = "id" in message
    if not isinstance(method, str):
        return _error(message.get("id"), -32600, "Invalid request") if has_id else None
    if method.startswith("notifications/"):
        return None
    if not has_id:
        return None
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _result(message, _initialize(params, app))
    if method == "ping":
        return _result(message, {})
    if method == "tools/list":
        return _result(message, {"tools": app.tool_specs()})
    if method == "tools/call":
        return _result(message, _call(params, app))
    if method == "resources/list":
        return _result(message, {"resources": []})
    if method == "resources/templates/list":
        return _result(message, {"resourceTemplates": []})
    if method == "prompts/list":
        return _result(message, {"prompts": []})
    if method == "logging/setLevel":
        return _result(message, {})
    return _error(message.get("id"), -32601, "Method not found")


def _initialize(params: dict, app) -> dict:
    version = str(params.get("protocolVersion") or "2025-06-18")
    if not _VERSION.fullmatch(version):
        version = "2025-06-18"
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": app.name, "version": app.version},
        "instructions": app.instructions,
    }


def _call(params: dict, app) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return {"content": [{"type": "text", "text": "error: missing tool name"}], "isError": True}
    arguments = params.get("arguments") or {}
    text, is_error = app.call(name, arguments)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(message: dict, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": payload}


def _error(message_id, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def _read_message(stdin) -> tuple[dict | None, bool]:
    while True:
        line = stdin.readline()
        if line == b"":
            return None, False
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                return _read_framed(stripped, stdin), True
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                _write(_error(None, -32700, "Parse error"), True)
                continue
        try:
            return json.loads(stripped.decode("utf-8")), False
        except json.JSONDecodeError:
            _write(_error(None, -32700, "Parse error"), False)
            continue


def _read_framed(first: bytes, stdin) -> dict:
    length = int(first.split(b":", 1)[1].strip())
    while True:
        line = stdin.readline()
        if line in (b"\n", b"\r\n", b""):
            break
    if length < 0 or length > _MAX_MESSAGE_BYTES:
        remaining = max(length, 0)
        while remaining:
            chunk = stdin.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        raise json.JSONDecodeError("message too large", "", 0)
    payload = stdin.read(length)
    return json.loads(payload.decode("utf-8"))


def _write(message: dict, framed: bool) -> None:
    raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    out = _OUT if _OUT is not None else sys.stdout.buffer
    if framed:
        out.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    else:
        out.write(raw + b"\n")
    out.flush()
