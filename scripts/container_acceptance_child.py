"""Execute one formal child between toolchain and mutation-guard checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypeVar

from scripts.container_acceptance_materialization import ExecutionMutationGuard
from scripts.container_acceptance_toolchain import verify_acceptance_toolchain

_E = TypeVar("_E", bound=Exception)


def run_bound_child(
    command: list[str],
    env: dict[str, str],
    *,
    cwd: Path,
    input_guard: ExecutionMutationGuard | None,
    error_type: type[_E],
) -> int:
    if input_guard is not None:
        input_guard.check()
    verify_acceptance_toolchain(env, error_type=error_type)
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=False).returncode
    except OSError as exc:
        raise error_type("验收命令无法启动") from exc
    finally:
        verify_acceptance_toolchain(env, error_type=error_type)
        if input_guard is not None:
            input_guard.check()
