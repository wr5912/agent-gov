from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from scripts.bootstrap_runtime_volume import load_runtime_env

from app.runtime.advisory_lock import AdvisoryLockBusy, AdvisoryLockError, advisory_lock
from app.runtime.api_health import api_health_ready, internal_api_health_url
from app.runtime.managed_agent_policy import ManagedAgentPolicyError, default_runtime_template_dir
from app.runtime.runtime_coordination import (
    RuntimeContractStatus,
    RuntimeCoordinationError,
    RuntimeCoordinationPaths,
    prepare_runtime_contract,
    runtime_contract_status,
)
from app.runtime.runtime_initialization import RuntimeInitializationError
from app.runtime.settings import AppSettings, get_settings

REQUIRED_FULL_STACK_RESTART_EXIT = 75
API_SINGLETON_EXIT = 73
WORKER_API_READY_TIMEOUT_SECONDS = 60.0


def _selected_env(settings: AppSettings) -> dict[str, str]:
    env_file = settings.settings_env_file
    return dict(load_runtime_env(env_file)) if env_file is not None else dict(os.environ)


def _status_payload(status: RuntimeContractStatus) -> dict[str, object]:
    return {
        "valid": status.valid,
        "reason": status.reason,
        "desired_digest": status.desired_digest,
        "managed_output_digest": status.managed_output_digest,
        "receipt_present": status.receipt is not None,
    }


def _check_status(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> RuntimeContractStatus:
    return runtime_contract_status(settings=settings, template_dir=template_dir, env=env)


def _prepare_under_exclusive_lock(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> None:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    try:
        with advisory_lock(paths.phase_lock, mode="exclusive", blocking=False) as lease:
            status = _check_status(settings, template_dir, env)
            if not status.valid:
                prepare_runtime_contract(
                    settings=settings,
                    template_dir=template_dir,
                    env=env,
                    lease=lease,
                )
    except AdvisoryLockBusy as exc:
        raise RuntimeCoordinationError("REQUIRED_FULL_STACK_RESTART: active runtime lease holder") from exc


def _run_api(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> int:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    try:
        with advisory_lock(paths.api_singleton_lock, mode="exclusive", blocking=False):
            with advisory_lock(paths.phase_lock, mode="shared"):
                status = _check_status(settings, template_dir, env)
                if status.valid:
                    return _serve_api(settings)
            _prepare_under_exclusive_lock(settings, template_dir, env)
            with advisory_lock(paths.phase_lock, mode="shared"):
                status = _check_status(settings, template_dir, env)
                if not status.valid:
                    raise RuntimeCoordinationError(f"Runtime contract remains invalid after preparation: {status.reason}")
                return _serve_api(settings)
    except AdvisoryLockBusy:
        print("API_SINGLETON_CONFLICT: another API process owns this runtime volume", file=sys.stderr, flush=True)
        return API_SINGLETON_EXIT


def _serve_api(settings: AppSettings) -> int:
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level,
    )
    return 0


def _wait_for_api(settings: AppSettings) -> str:
    url = internal_api_health_url(settings)
    deadline = time.monotonic() + WORKER_API_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if api_health_ready(url):
            return url
        time.sleep(1)
    raise RuntimeCoordinationError(f"Worker timed out waiting for API readiness: {url}")


def _run_worker(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> int:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    if not paths.root.is_dir():
        raise RuntimeCoordinationError("Worker cannot start before API runtime preparation")
    health_url = _wait_for_api(settings)
    with advisory_lock(paths.phase_lock, mode="shared"):
        status = _check_status(settings, template_dir, env)
        if not status.valid:
            raise RuntimeCoordinationError(f"Worker runtime contract is invalid: {status.reason}")
        if not api_health_ready(health_url):
            raise RuntimeCoordinationError("API readiness changed while worker acquired its runtime lease")
        from app.worker.agent_jobs import main as worker_main

        asyncio.run(worker_main())
    return 0


def _prepare(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> int:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    with advisory_lock(paths.phase_lock, mode="shared"):
        status = _check_status(settings, template_dir, env)
    if status.valid:
        print(json.dumps(_status_payload(status), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _prepare_under_exclusive_lock(settings, template_dir, env)
    status = _check_status(settings, template_dir, env)
    print(json.dumps(_status_payload(status), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status.valid else 1


def _validate(settings: AppSettings, template_dir: Path, env: dict[str, str]) -> int:
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    if not paths.root.is_dir():
        status = _check_status(settings, template_dir, env)
    else:
        with advisory_lock(paths.phase_lock, mode="shared"):
            status = _check_status(settings, template_dir, env)
    print(json.dumps(_status_payload(status), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status.valid else 1


def _run_tool(settings: AppSettings, template_dir: Path, env: dict[str, str], command: Sequence[str]) -> int:
    if not command:
        raise RuntimeCoordinationError("run-tool requires a command")
    paths = RuntimeCoordinationPaths.from_data_dir(settings.data_dir)
    with advisory_lock(paths.phase_lock, mode="shared"):
        status = _check_status(settings, template_dir, env)
        if not status.valid:
            raise RuntimeCoordinationError(f"run-tool runtime contract is invalid: {status.reason}")
        return subprocess.run(list(command), check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentGov runtime service launcher and maintenance gate.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("api")
    subparsers.add_parser("worker")
    subparsers.add_parser("prepare")
    subparsers.add_parser("validate")
    tool = subparsers.add_parser("run-tool")
    tool.add_argument("tool_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    template_dir = default_runtime_template_dir()
    env = _selected_env(settings)
    try:
        if args.command == "api":
            return _run_api(settings, template_dir, env)
        if args.command == "worker":
            return _run_worker(settings, template_dir, env)
        if args.command == "prepare":
            return _prepare(settings, template_dir, env)
        if args.command == "validate":
            return _validate(settings, template_dir, env)
        return _run_tool(settings, template_dir, env, args.tool_command)
    except (AdvisoryLockError, ManagedAgentPolicyError, RuntimeCoordinationError, RuntimeInitializationError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return REQUIRED_FULL_STACK_RESTART_EXIT if "REQUIRED_FULL_STACK_RESTART" in str(exc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
