"""Run one local command. The child process does not inherit Claude credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from local_context.budget import MAX_CAPTURE_CHARS, with_header
from local_context.config import Settings
from local_context.errors import LocalError

_CREATE_NO_WINDOW = 0x08000000


@dataclass
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool
    capture_capped: bool
    cwd: str


class Shell:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()

    def run(self, command: str, cwd: str, timeout: int) -> CommandResult:
        if not self.settings.allow_shell:
            raise LocalError("shell is disabled")
        self.settings.policy.check_command(command)
        workdir = self.settings.policy.resolve_cwd(cwd)
        argv = _argv(self.settings.shell, command)
        with self._lock:
            return _execute(argv, workdir, timeout, self.settings.policy.child_env())

    def present(self, result: CommandResult, limit: int, stored: Path | None) -> str:
        header = (
            f"exit={result.exit_code} timed_out={str(result.timed_out).lower()} "
            f"capped={str(result.capture_capped).lower()}\n"
        )
        if stored is not None:
            header += f"stored={stored}\n"
        body = result.output if result.output else "(no output)"
        return with_header(header, body, 0, limit)


def _execute(argv: list[str], cwd: Path, timeout: int, env: dict[str, str]) -> CommandResult:
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        raise LocalError("shell executable not found") from exc
    except OSError as exc:
        raise LocalError(f"command failed to start: {exc.strerror or exc}") from exc

    stdout, stdout_state, stdout_thread = _start_drain(proc.stdout, MAX_CAPTURE_CHARS)
    stderr, stderr_state, stderr_thread = _start_drain(proc.stderr, MAX_CAPTURE_CHARS)
    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -1
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    out_text = bytes(stdout).decode("utf-8", errors="replace")
    err_text = bytes(stderr).decode("utf-8", errors="replace")
    pieces = [out_text]
    if err_text:
        pieces.append("[stderr]\n" + err_text)
    output = "".join(pieces)
    capture_capped = stdout_state["capped"] or stderr_state["capped"]
    if len(output) > MAX_CAPTURE_CHARS:
        output = output[:MAX_CAPTURE_CHARS]
        capture_capped = True
    return CommandResult(
        exit_code=-1 if code is None else int(code),
        output=output,
        timed_out=timed_out,
        capture_capped=capture_capped,
        cwd=str(cwd),
    )


def _start_drain(stream, limit: int) -> tuple[bytearray, dict[str, bool], threading.Thread]:
    bucket = bytearray()
    state = {"capped": False}
    if stream is None:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        return bucket, state, thread

    def pump() -> None:
        try:
            while True:
                block = stream.read(8192)
                if not block:
                    break
                if len(bucket) < limit:
                    room = limit - len(bucket)
                    bucket.extend(block[:room])
                    if len(block) > room:
                        state["capped"] = True
                else:
                    state["capped"] = True
        finally:
            stream.close()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return bucket, state, thread


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    else:
        proc.kill()


def _argv(shell: str, command: str) -> list[str]:
    kind = shell
    if kind == "auto":
        kind = "powershell" if os.name == "nt" else "bash" if shutil.which("bash") else "sh"
    if kind == "powershell":
        exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    if kind == "cmd":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command]
    if kind == "bash":
        return [shutil.which("bash") or "bash", "-lc", command]
    if kind == "sh":
        return [shutil.which("sh") or "sh", "-lc", command]
    raise LocalError(f"unknown shell: {shell}")
