"""容器验收的信号转发、取消与终态提交线性化。"""

from __future__ import annotations

import contextvars
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType
from typing import Final, TypeVar

_TERMINAL_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)
_T = TypeVar("_T")


class AcceptanceCancelled(RuntimeError):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"容器验收被信号 {signum} 取消")


def signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
    if process.poll() is None:
        with suppress(OSError):
            os.killpg(process.pid, signum)


class SignalController:
    def __init__(self, *, cancel_force_kill_seconds: float = 1.0) -> None:
        self.signum: int | None = None
        self._requested_at: float | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._cleaning_up = False
        self._interruptible = False
        self._sealed = False
        self._cancel_force_kill_seconds = cancel_force_kill_seconds

    @property
    def sealed(self) -> bool:
        return self._sealed

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self._sealed:
            return
        first = self.signum is None
        if first:
            self.signum = signum
            self._requested_at = time.monotonic()
        if self._active_process is not None and (first or self._cleaning_up):
            forwarded = signal.SIGKILL if self._cleaning_up and not first else signum
            signal_process_group(self._active_process, forwarded)
        elif first and self._interruptible and not self._cleaning_up and threading.current_thread() is threading.main_thread():
            raise AcceptanceCancelled(signum)

    def _bind(self, process: subprocess.Popen[str]) -> None:
        self._active_process = process
        if self.signum is not None and not self._cleaning_up:
            signal_process_group(process, self.signum)

    def _unbind(self, process: subprocess.Popen[str]) -> None:
        if self._active_process is process:
            self._active_process = None

    def _escalate_if_needed(self, process: subprocess.Popen[str]) -> bool:
        if self._cleaning_up or self._requested_at is None or process.poll() is not None:
            return False
        if time.monotonic() - self._requested_at < self._cancel_force_kill_seconds:
            return False
        signal_process_group(process, signal.SIGKILL)
        return True

    def _raise_if_cancelled(self) -> None:
        if self.signum is not None and not self._cleaning_up:
            raise AcceptanceCancelled(self.signum)

    def _run_interruptible(self, action: Callable[[], None]) -> None:
        self._interruptible = True
        try:
            self._raise_if_cancelled()
            action()
        finally:
            self._interruptible = False

    def _begin_cleanup(self) -> None:
        self._cleaning_up = True

    def _observe_pending_terminal_signals(self) -> None:
        pending = signal.sigpending()
        for signum in _TERMINAL_SIGNALS:
            if signum in pending:
                self._handle(signum, None)

    def _commit_terminal(self, action: Callable[[int | None], _T]) -> tuple[_T, int | None]:
        """在线性化点冻结取消结果；成功发布后到达的信号不得改写终态。"""
        if threading.current_thread() is not threading.main_thread() or self._sealed:
            raise RuntimeError("容器验收终态提交必须由未封存的主线程执行")
        watched = set(_TERMINAL_SIGNALS)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
        try:
            self._observe_pending_terminal_signals()
            cancellation = self.signum
            result = action(cancellation)
            self._sealed = True
            return result, cancellation
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


ACTIVE_SIGNAL_CONTROLLER: contextvars.ContextVar[SignalController | None] = contextvars.ContextVar(
    "agentgov_acceptance_signal_controller",
    default=None,
)


@contextmanager
def controlled_signals(*, cancel_force_kill_seconds: float) -> Iterator[SignalController]:
    controller = SignalController(cancel_force_kill_seconds=cancel_force_kill_seconds)
    token = ACTIVE_SIGNAL_CONTROLLER.set(controller)
    previous: dict[int, signal.Handlers] = {}
    previous_mask: set[signal.Signals] | None = None
    install = threading.current_thread() is threading.main_thread()
    if install:
        for signum in _TERMINAL_SIGNALS:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, controller._handle)
        previous_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, set(_TERMINAL_SIGNALS))
    try:
        yield controller
    finally:
        if install:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        ACTIVE_SIGNAL_CONTROLLER.reset(token)
