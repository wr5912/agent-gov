"""在加载依赖型 launcher 前捕获并激活固定工具链。"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import FrameType
from typing import NoReturn


class LauncherEntryInterrupted(SystemExit):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(128 + signum)


@contextmanager
def _controlled_signals() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, signal.Handlers] = {}

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        raise LauncherEntryInterrupted(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def launch(arguments: list[str], environ: Mapping[str, str]) -> NoReturn:
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    with _controlled_signals():
        authority = acceptance_toolchain.capture_toolchain_authority()
        acceptance_toolchain.activate_toolchain_authority(authority)
        from scripts import container_acceptance_launcher as launcher

        launcher.launch_from_toolchain(arguments, environ)
