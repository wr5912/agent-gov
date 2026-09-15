"""Workspace 回收验收外部 watchdog 与信号清理的真实进程测试。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
from scripts.workspace_reclaim_acceptance_watchdog import process_is_stopped, process_start_ticks, run_watchdog
from scripts.workspace_reclaim_acceptance_watchdog_control import (
    arm_runtime_watchdog,
    disarm_runtime_watchdog,
)

ROOT = Path(__file__).resolve().parents[1]


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _wait_stopped(pid: int, expected: bool, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_is_stopped(pid) is expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"process stopped state did not become {expected}")


def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
    with suppress(OSError):
        os.kill(process.pid, signal.SIGCONT)
    if process.poll() is None:
        process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def test_external_watchdog_deadman_resumes_a_real_stopped_process() -> None:
    target = _sleeper()
    watchdog = arm_runtime_watchdog(target.pid, deadman_seconds=0.5)
    try:
        os.kill(target.pid, signal.SIGSTOP)
        _wait_stopped(target.pid, True)
        _wait_stopped(target.pid, False)
        assert watchdog.process.wait(timeout=5) == 0
        assert not watchdog.lease_path.exists()
    finally:
        disarm_runtime_watchdog(watchdog)
        _cleanup_process(target)


def test_external_watchdog_survives_owner_sigkill_and_resumes_target() -> None:
    target = _sleeper()
    owner = _sleeper()
    watchdog = arm_runtime_watchdog(target.pid, owner_pid=owner.pid, deadman_seconds=10.0)
    try:
        os.kill(target.pid, signal.SIGSTOP)
        _wait_stopped(target.pid, True)
        owner.kill()
        owner.wait(timeout=5)
        _wait_stopped(target.pid, False)
        assert watchdog.process.wait(timeout=5) == 0
    finally:
        disarm_runtime_watchdog(watchdog)
        _cleanup_process(owner)
        _cleanup_process(target)


def test_disarm_confirms_a_real_stopped_target_is_resumed() -> None:
    target = _sleeper()
    watchdog = arm_runtime_watchdog(target.pid, deadman_seconds=10.0)
    try:
        os.kill(target.pid, signal.SIGSTOP)
        _wait_stopped(target.pid, True)
        disarm_runtime_watchdog(watchdog)
        _wait_stopped(target.pid, False)
        assert watchdog.process.poll() == 0
        assert not watchdog.lease_path.exists()
    finally:
        _cleanup_process(target)


def test_watchdog_rejects_boolean_schema_version(tmp_path: Path) -> None:
    target = _sleeper()
    token = "boolean-schema-token"
    lease = tmp_path / "watchdog.lease"
    ready = tmp_path / "watchdog.ready"
    try:
        owner_ticks = process_start_ticks(os.getpid())
        runtime_ticks = process_start_ticks(target.pid)
        assert owner_ticks is not None and runtime_ticks is not None
        lease.write_text(
            json.dumps(
                {
                    "schema_version": True,
                    "owner_pid": os.getpid(),
                    "owner_start_ticks": owner_ticks,
                    "runtime_pid": target.pid,
                    "runtime_start_ticks": runtime_ticks,
                    "token": token,
                }
            ),
            encoding="ascii",
        )
        assert run_watchdog(lease, ready, token, 0.1) == 2
        assert not ready.exists()
    finally:
        _cleanup_process(target)


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_acceptance_signal_guard_runs_finally(interrupt_signal: signal.Signals, tmp_path: Path) -> None:
    completed = tmp_path / "finally-completed"
    code = """
import asyncio
import sys
from pathlib import Path
from scripts.run_workspace_reclaim_acceptance import _termination_interrupts
async def run():
    try:
        print('READY', flush=True)
        await asyncio.sleep(30)
    finally:
        await asyncio.sleep(0)
        Path(sys.argv[1]).write_text('completed', encoding='utf-8')
with _termination_interrupts():
    asyncio.run(run())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(completed)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"READY\n"
        os.kill(process.pid, interrupt_signal)
        # 全量主流程高负载下，信号已送达但子进程的 finally/退出调度可能超过 5 秒；
        # 这里验证清理必达而非 5 秒退出 SLA，并与后续真实 Runtime 清理预算保持一致。
        assert process.wait(timeout=10) != 0
        assert completed.read_text(encoding="utf-8") == "completed"
    finally:
        _cleanup_process(process)


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_acceptance_signal_cleanup_confirms_runtime_resume(
    interrupt_signal: signal.Signals,
    tmp_path: Path,
) -> None:
    completed = tmp_path / "runtime-resume-confirmed"
    code = """
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from scripts.run_workspace_reclaim_acceptance import _termination_interrupts
from scripts.workspace_reclaim_acceptance_watchdog import process_is_stopped
from scripts.workspace_reclaim_acceptance_watchdog_control import arm_runtime_watchdog, disarm_runtime_watchdog
target = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
watchdog = arm_runtime_watchdog(target.pid, deadman_seconds=10.0)
outcome = 'not-resumed'
try:
    with _termination_interrupts():
        try:
            os.kill(target.pid, signal.SIGSTOP)
            while not process_is_stopped(target.pid):
                time.sleep(0.01)
            print(f'READY {target.pid}', flush=True)
            time.sleep(30)
        finally:
            disarm_runtime_watchdog(watchdog)
            if not process_is_stopped(target.pid):
                outcome = 'resumed'
finally:
    with suppress(OSError):
        os.kill(target.pid, signal.SIGCONT)
    if target.poll() is None:
        target.terminate()
    with suppress(subprocess.TimeoutExpired):
        target.wait(timeout=5)
    Path(sys.argv[1]).write_text(outcome, encoding='utf-8')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(completed)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    target_pid = 0
    try:
        assert process.stdout is not None
        ready = process.stdout.readline().decode("ascii").strip().split()
        assert ready[0] == "READY"
        target_pid = int(ready[1])
        os.kill(process.pid, interrupt_signal)
        assert process.wait(timeout=10) != 0
        assert completed.read_text(encoding="utf-8") == "resumed"
        assert process_start_ticks(target_pid) is None
    finally:
        _cleanup_process(process)
        if target_pid > 0:
            with suppress(OSError):
                os.kill(target_pid, signal.SIGCONT)
                os.kill(target_pid, signal.SIGTERM)
