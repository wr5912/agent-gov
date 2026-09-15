"""selected-env operation 命令行边界。"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

OperationRunner = Callable[..., int]


def run_cli(
    operation_runner: OperationRunner,
    operations: Sequence[str],
    error_types: tuple[type[BaseException], ...],
) -> int:
    parser = argparse.ArgumentParser(description="Run a fixed deployment operation against one immutable selected-env snapshot.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--env-base-dir", type=Path)
    parser.add_argument("--operation", choices=tuple(operations), required=True)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--force-recreate", action="store_true")
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()
    if (args.no_build or args.force_recreate) and args.operation not in {"up", "all-up"}:
        parser.error("--no-build/--force-recreate 只允许用于 up/all-up")
    if args.require_idle and args.operation != "runtime-recreate":
        parser.error("--require-idle 只允许用于 runtime-recreate")
    try:
        return operation_runner(
            args.env_file,
            args.operation,
            env_base_dir=args.env_base_dir,
            no_build=args.no_build,
            force_recreate=args.force_recreate,
            require_idle=args.require_idle,
        )
    except error_types as exc:
        parser.error(str(exc))
