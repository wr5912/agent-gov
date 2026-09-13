"""Command-line parsing for the formal container acceptance runner."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from scripts.container_acceptance_environment import PROFILES, AcceptanceProfile, resolve_env_file

_E = TypeVar("_E", bound=Exception)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="刷新 Docker Compose 运行态后执行真实容器验收。")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("必须在 `--` 后提供验收命令")
    return args


def run_cli(
    argv: list[str] | None,
    environ: dict[str, str],
    *,
    resume: Callable[[dict[str, str]], int],
    run: Callable[[AcceptanceProfile, Path, list[str], dict[str, str]], int],
    error_type: type[_E],
) -> int:
    effective_argv = sys.argv[1:] if argv is None else argv
    try:
        if effective_argv == ["--frozen-resume"]:
            return resume(environ)
        args = parse_args(effective_argv)
        profile = PROFILES[args.profile]
        env_file = resolve_env_file(profile, args.env_file, environ)
        return run(profile, env_file, args.command, environ)
    except error_type as exc:
        print(f"CONTAINER_ACCEPTANCE_FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("CONTAINER_ACCEPTANCE_FAIL: 无法读取当前工作树或所选配置", file=sys.stderr)
        return 1
