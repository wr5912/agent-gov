"""启动、握手并收回 Workspace 回收验收的独立 watchdog。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from scripts.workspace_reclaim_acceptance_watchdog import process_is_stopped, process_start_ticks

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_DEADMAN_SECONDS = 20.0
WATCHDOG_READY_TIMEOUT_SECONDS = 5.0
WATCHDOG_SCRIPT = REPO_ROOT / "scripts/workspace_reclaim_acceptance_watchdog.py"


class WatchdogControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeWatchdog:
    process: subprocess.Popen[bytes]
    lease_path: Path
    ready_path: Path
    token: str
    runtime_pid: int
    runtime_start_ticks: int


def _lease_bytes(runtime_pid: int, owner_pid: int, token: str) -> tuple[bytes, int]:
    owner_ticks = process_start_ticks(owner_pid)
    runtime_ticks = process_start_ticks(runtime_pid)
    if owner_ticks is None or runtime_ticks is None:
        raise WatchdogControllerError("process identity is unavailable")
    payload = json.dumps(
        {
            "schema_version": 1,
            "owner_pid": owner_pid,
            "owner_start_ticks": owner_ticks,
            "runtime_pid": runtime_pid,
            "runtime_start_ticks": runtime_ticks,
            "token": token,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return payload, runtime_ticks


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def arm_runtime_watchdog(
    runtime_pid: int,
    *,
    owner_pid: int | None = None,
    deadman_seconds: float = WATCHDOG_DEADMAN_SECONDS,
) -> RuntimeWatchdog:
    """在 SIGSTOP 前启动独立进程并完成 ready 握手。"""

    if not WATCHDOG_SCRIPT.is_file() or deadman_seconds <= 0:
        raise WatchdogControllerError("watchdog input is invalid")
    token = uuid.uuid4().hex
    descriptor, raw_path = tempfile.mkstemp(prefix="agentgov-reclaim-watchdog-", suffix=".lease")
    lease_path = Path(raw_path)
    ready_path = lease_path.with_suffix(".ready")
    process: subprocess.Popen[bytes] | None = None
    error: BaseException | None = None
    try:
        payload, runtime_start_ticks = _lease_bytes(runtime_pid, owner_pid or os.getpid(), token)
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        process = subprocess.Popen(
            [
                str(Path(sys.executable).resolve()),
                str(WATCHDOG_SCRIPT),
                "--lease",
                str(lease_path),
                "--ready",
                str(ready_path),
                "--token",
                token,
                "--deadman-seconds",
                str(deadman_seconds),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env={key: os.environ[key] for key in ("HOME", "LANG", "PATH") if key in os.environ},
        )
        deadline = time.monotonic() + WATCHDOG_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if ready_path.read_text(encoding="ascii") == token and process.poll() is None:
                    return RuntimeWatchdog(
                        process,
                        lease_path,
                        ready_path,
                        token,
                        runtime_pid,
                        runtime_start_ticks,
                    )
            except OSError:
                pass
            if process.poll() is not None:
                break
            time.sleep(0.01)
    except (OSError, subprocess.SubprocessError, WatchdogControllerError) as exc:
        error = exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if process is not None:
        _terminate(process)
    with suppress(OSError):
        ready_path.unlink()
    with suppress(OSError):
        lease_path.unlink()
    raise WatchdogControllerError("watchdog did not become ready") from error


def disarm_runtime_watchdog(watchdog: RuntimeWatchdog) -> None:
    """Runtime 已恢复或旧 PID 已消失后，收回本次独立 watchdog。"""

    if process_start_ticks(watchdog.runtime_pid) == watchdog.runtime_start_ticks and process_is_stopped(watchdog.runtime_pid):
        try:
            os.kill(watchdog.runtime_pid, signal.SIGCONT)
        except OSError as exc:
            raise WatchdogControllerError("watched Runtime could not be resumed") from exc
    deadline = time.monotonic() + 5.0
    while (
        process_start_ticks(watchdog.runtime_pid) == watchdog.runtime_start_ticks and process_is_stopped(watchdog.runtime_pid) and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    if process_start_ticks(watchdog.runtime_pid) == watchdog.runtime_start_ticks and process_is_stopped(watchdog.runtime_pid):
        raise WatchdogControllerError("watched Runtime remained stopped")
    with suppress(OSError):
        watchdog.lease_path.unlink()
    try:
        watchdog.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate(watchdog.process)
    with suppress(OSError):
        watchdog.ready_path.unlink()
