#!/usr/bin/env python3
"""独立看护 Workspace 回收验收中被 SIGSTOP 的 Runtime 进程。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TypedDict, cast


class WatchdogLease(TypedDict):
    schema_version: int
    owner_pid: int
    owner_start_ticks: int
    runtime_pid: int
    runtime_start_ticks: int
    token: str


def process_start_ticks(pid: int) -> int | None:
    """读取 Linux 进程不可复用的 starttime；进程消失或格式异常时返回 None。"""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        value = int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def process_is_stopped(pid: int) -> bool:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
        state = next(line for line in lines if line.startswith("State:"))
    except (OSError, StopIteration):
        return False
    return state.split()[1] in {"T", "t"}


def _load_lease(path: Path, token: str) -> WatchdogLease | None:
    try:
        metadata = path.lstat()
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    expected = {
        "schema_version",
        "owner_pid",
        "owner_start_ticks",
        "runtime_pid",
        "runtime_start_ticks",
        "token",
    }
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("token") != token
        or any(type(payload.get(field)) is not int for field in expected - {"token"})
    ):
        return None
    lease = cast(WatchdogLease, payload)
    if any(lease[field] <= 0 for field in ("owner_pid", "owner_start_ticks", "runtime_pid", "runtime_start_ticks")):
        return None
    return lease


def _write_ready(path: Path, token: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_if_owned(path: Path, token: str, *, lease: bool) -> None:
    try:
        owned = (_load_lease(path, token) is not None) if lease else path.read_text(encoding="ascii") == token
        if owned and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def run_watchdog(lease_path: Path, ready_path: Path, token: str, deadman_seconds: float) -> int:
    lease = _load_lease(lease_path, token)
    if lease is None or ready_path.parent != lease_path.parent or deadman_seconds <= 0:
        return 2
    try:
        _write_ready(ready_path, token)
    except OSError:
        return 2
    deadline = time.monotonic() + deadman_seconds
    try:
        while _load_lease(lease_path, token) == lease:
            runtime_ticks = process_start_ticks(lease["runtime_pid"])
            if runtime_ticks != lease["runtime_start_ticks"]:
                return 0
            stopped = process_is_stopped(lease["runtime_pid"])
            owner_alive = process_start_ticks(lease["owner_pid"]) == lease["owner_start_ticks"]
            if stopped and (not owner_alive or time.monotonic() >= deadline):
                with suppress(OSError):
                    os.kill(lease["runtime_pid"], signal.SIGCONT)
                resume_deadline = time.monotonic() + 5.0
                while time.monotonic() < resume_deadline:
                    runtime_ticks = process_start_ticks(lease["runtime_pid"])
                    if runtime_ticks != lease["runtime_start_ticks"] or not process_is_stopped(lease["runtime_pid"]):
                        return 0
                    time.sleep(0.01)
                return 1
            if not owner_alive or time.monotonic() >= deadline:
                return 0
            time.sleep(0.01)
        return 0
    finally:
        _remove_if_owned(ready_path, token, lease=False)
        _remove_if_owned(lease_path, token, lease=True)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--deadman-seconds", type=float, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    return run_watchdog(args.lease, args.ready, args.token, args.deadman_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
