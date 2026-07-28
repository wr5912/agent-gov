#!/usr/bin/env python3
"""Transparent stdout tee used only by the managed raw-events endpoint.

The SDK launches this file as its ``cli_path``. The wrapper executes the real
Claude Code binary with the original argv/stdin/stderr, then copies each stdout
byte to both the SDK pipe and a private Unix socket. It never writes diagnostics
to stdout.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import FrameType

_BUFFER_SIZE = 64 * 1024
_CONFIG_SUFFIX = ".json"
_PDEATHSIG = 1


def _fail(message: str, *, code: int = 70) -> int:
    print(f"agentgov raw capture: {message}", file=sys.stderr, flush=True)
    return code


def _config_path() -> Path:
    return Path(sys.argv[0]).with_suffix(_CONFIG_SUFFIX)


def _load_config() -> tuple[Path, str]:
    config = json.loads(_config_path().read_text(encoding="utf-8"))
    target = Path(str(config["target_cli"])).resolve()
    socket_path = str(config["socket_path"])
    wrapper = Path(sys.argv[0]).resolve()
    if target == wrapper or not target.is_file():
        raise ValueError("invalid target CLI")
    return target, socket_path


def _linux_child_setup() -> None:
    os.setsid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PDEATHSIG, signal.SIGTERM) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _exit_like_child(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    signum = -returncode
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return 128 + signum


def main() -> int:
    try:
        target, socket_path = _load_config()
    except Exception as exc:
        return _fail(f"cannot load capture configuration ({exc.__class__.__name__})")

    args = sys.argv[1:]
    if args in (["-v"], ["--version"]):
        os.execv(str(target), [str(target), *args])

    tap = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    child: subprocess.Popen[bytes] | None = None

    def forward_signal(signum: int, _frame: FrameType | None) -> None:
        if child is None or child.poll() is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signum)

    try:
        tap.connect(socket_path)
        child = subprocess.Popen(
            [str(target), *args],
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=None,
            close_fds=True,
            preexec_fn=_linux_child_setup,
        )
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, forward_signal)

        assert child.stdout is not None
        while chunk := os.read(child.stdout.fileno(), _BUFFER_SIZE):
            tap.sendall(chunk)
            _write_all(sys.stdout.fileno(), chunk)
        child.stdout.close()
        return _exit_like_child(child.wait())
    except (BrokenPipeError, ConnectionError, OSError) as exc:
        if child is not None and child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            child.wait()
        return _fail(f"capture channel failed ({exc.__class__.__name__})")
    finally:
        tap.close()


if __name__ == "__main__":
    raise SystemExit(main())
