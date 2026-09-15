from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, TypeAlias

from pydantic.types import JsonValue

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.json_types import JsonObject

from .report_validation import passed_report_errors
from .store import AgentTestingStore
from .suite import inspect_agent_test_suite

logger = logging.getLogger(__name__)

MAX_CAPTURED_OUTPUT_BYTES = 256_000
FIXED_PYTEST_COMMAND = [
    sys.executable,
    "-I",
    "-m",
    "pytest",
    "-q",
    "-p",
    "agentgov_testkit.pytest_plugin",
    "--noconftest",
    "--import-mode=importlib",
    "-c",
    "/dev/null",
    "tests",
]
ProcessEnvironment: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class _RunPaths:
    checkout: Path
    report: Path
    stdout: Path
    stderr: Path


@dataclass(frozen=True)
class _RunAttestation:
    token: str
    agent_id: str
    commit_sha: str
    change_set_id: str | None


class AgentTestRunner:
    def __init__(
        self,
        *,
        store: AgentTestingStore,
        store_for: Callable[[str], GitAgentVersionStore],
        artifacts_dir: Path,
        api_base_url: str,
        api_key: str | None,
        timeout_seconds: int,
    ) -> None:
        self._store = store
        self._store_for = store_for
        self._artifacts_dir = artifacts_dir
        self._api_base_url = api_base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._executor = self._new_executor()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._attestations: dict[str, _RunAttestation] = {}
        self._lock = threading.RLock()
        self._closed = False
        artifacts_dir.mkdir(parents=True, exist_ok=True)

    def recover(self) -> JsonObject:
        with self._lock:
            if self._closed:
                self._executor = self._new_executor()
                self._closed = False
        interrupted = self._store.reconcile_interrupted_runs()
        queued = self._store.queued_run_ids()
        for test_run_id in queued:
            self.enqueue(test_run_id)
        return {"interrupted": interrupted, "requeued": queued}

    def enqueue(self, test_run_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent test runner is closed")
            self._executor.submit(self._execute, test_run_id)

    def cancel(self, test_run_id: str) -> JsonObject:
        payload = self._store.request_cancel(test_run_id)
        with self._lock:
            process = self._processes.get(test_run_id)
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
        return payload

    def require_attestation(
        self,
        *,
        test_run_id: str,
        token: str,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
    ) -> None:
        with self._lock:
            attestation = self._attestations.get(test_run_id)
        valid = (
            attestation is not None
            and secrets.compare_digest(attestation.token, token)
            and attestation.agent_id == agent_id
            and attestation.commit_sha == commit_sha
            and attestation.change_set_id == change_set_id
        )
        if not valid:
            raise PermissionError("Agent test run attestation is invalid or no longer active")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            processes = list(self._processes.values())
        self._store.reconcile_interrupted_runs()
        for process in processes:
            if process.poll() is None:
                _terminate_process_group(process)
        with self._lock:
            self._attestations.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-test-runner")

    def checkout(self, *, store: GitAgentVersionStore, commit_sha: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with store.mutation_guard():
            store.ensure_bootstrap()
            _run_git(store.repository_dir, ["rev-parse", "--verify", f"{commit_sha}^{{commit}}"])
            _run_git(store.repository_dir, ["worktree", "prune"], check=False)
            _run_git(store.repository_dir, ["worktree", "add", "--detach", str(destination), commit_sha])

    def remove_checkout(self, *, store: GitAgentVersionStore, destination: Path) -> None:
        with store.mutation_guard():
            _run_git(store.repository_dir, ["worktree", "remove", "--force", str(destination)], check=False)
            _run_git(store.repository_dir, ["worktree", "prune"], check=False)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)

    def _execute(self, test_run_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            claimed = self._store.claim_run(test_run_id)
        if claimed is None:
            return
        agent_id = str(claimed["agent_id"])
        commit_sha = str(claimed["commit_sha"])
        change_set_id = str(claimed.get("change_set_id") or "") or None
        suite_digest = str(claimed.get("suite_digest") or "")
        paths = _run_paths(self._artifacts_dir, test_run_id)
        store: GitAgentVersionStore | None = None
        try:
            store = self._store_for(agent_id)
            self.checkout(store=store, commit_sha=commit_sha, destination=paths.checkout)
            self._verify_checked_out_suite(
                paths.checkout,
                agent_id=agent_id,
                commit_sha=commit_sha,
                expected_digest=suite_digest,
            )
            self._run_pytest(
                test_run_id,
                agent_id=agent_id,
                commit_sha=commit_sha,
                change_set_id=change_set_id,
                paths=paths,
            )
        except Exception as exc:
            self._finish_with_error(test_run_id, exc)
        finally:
            if store is not None:
                self._remove_checkout_safely(test_run_id, store=store, destination=paths.checkout)

    @staticmethod
    def _verify_checked_out_suite(
        checkout: Path,
        *,
        agent_id: str,
        commit_sha: str,
        expected_digest: str,
    ) -> None:
        suite = inspect_agent_test_suite(checkout, agent_id=agent_id, commit_sha=commit_sha)
        if not suite.runnable or not suite.suite_digest:
            codes = ",".join(item.code for item in suite.diagnostics) or "empty suite"
            raise RuntimeError(f"checked-out Agent test suite is not runnable: {codes}")
        if not expected_digest or suite.suite_digest != expected_digest:
            raise RuntimeError("checked-out Agent test suite digest differs from the scheduled run")

    def _run_pytest(
        self,
        test_run_id: str,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        paths: _RunPaths,
    ) -> None:
        attestation_token = self._register_attestation(
            test_run_id,
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
        )
        try:
            env = self._test_environment(
                test_run_id=test_run_id,
                attestation_token=attestation_token,
                agent_id=agent_id,
                commit_sha=commit_sha,
                change_set_id=change_set_id,
                report_path=paths.report,
            )
            self._execute_pytest_process(
                test_run_id,
                paths=paths,
                env=env,
                redactions=(attestation_token,),
            )
        finally:
            with self._lock:
                self._attestations.pop(test_run_id, None)

    def _execute_pytest_process(
        self,
        test_run_id: str,
        *,
        paths: _RunPaths,
        env: ProcessEnvironment,
        redactions: tuple[str, ...],
    ) -> None:
        with paths.stdout.open("w", encoding="utf-8") as stdout_file, paths.stderr.open("w", encoding="utf-8") as stderr_file:
            process = self._start_pytest_process(test_run_id, paths=paths, env=env, stdout_file=stdout_file, stderr_file=stderr_file)
            try:
                started_at = time.monotonic()
                timed_out = self._wait_for_pytest(test_run_id, process, started_at=started_at)
                self._finish_process(
                    test_run_id,
                    process=process,
                    paths=paths,
                    duration_seconds=time.monotonic() - started_at,
                    timed_out=timed_out,
                    redactions=redactions,
                )
            finally:
                with self._lock:
                    self._processes.pop(test_run_id, None)
                if process.poll() is None:
                    _terminate_process_group(process, kill=True)

    def _start_pytest_process(
        self,
        test_run_id: str,
        *,
        paths: _RunPaths,
        env: ProcessEnvironment,
        stdout_file: TextIO,
        stderr_file: TextIO,
    ) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent test runner is closed")
            process = subprocess.Popen(
                FIXED_PYTEST_COMMAND,
                cwd=paths.checkout,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            self._processes[test_run_id] = process
            return process

    def _wait_for_pytest(self, test_run_id: str, process: subprocess.Popen[str], *, started_at: float) -> bool:
        timed_out = False
        while process.poll() is None:
            if self._store.cancel_requested(test_run_id):
                _terminate_process_group(process)
                break
            if time.monotonic() - started_at >= self._timeout_seconds:
                timed_out = True
                _terminate_process_group(process)
                break
            time.sleep(0.2)
        _wait_for_process(process)
        return timed_out

    def _register_attestation(
        self,
        test_run_id: str,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._attestations[test_run_id] = _RunAttestation(token, agent_id, commit_sha, change_set_id)
        return token

    def _test_environment(
        self,
        *,
        test_run_id: str,
        attestation_token: str,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        report_path: Path,
    ) -> ProcessEnvironment:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTGOV_", "PYTEST_", "PYTHON"))}
        env.update(
            {
                "AGENTGOV_API_BASE": self._api_base_url,
                "AGENTGOV_AGENT_ID": agent_id,
                "AGENTGOV_COMMIT_SHA": commit_sha,
                "AGENTGOV_TEST_REPORT_PATH": str(report_path),
                "AGENTGOV_TEST_RUN_ATTESTATION": attestation_token,
                "AGENTGOV_TEST_RUN_ID": test_run_id,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_ADDOPTS": "",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
        )
        if self._api_key:
            env["AGENTGOV_API_KEY"] = self._api_key
        if change_set_id:
            env["AGENTGOV_CHANGE_SET_ID"] = change_set_id
        return env

    def _finish_process(
        self,
        test_run_id: str,
        *,
        process: subprocess.Popen[str],
        paths: _RunPaths,
        duration_seconds: float,
        timed_out: bool,
        redactions: tuple[str, ...] = (),
    ) -> None:
        cancelled = self._store.cancel_requested(test_run_id)
        status = "error" if timed_out and not cancelled else _process_status(process.returncode, cancelled=cancelled)
        report = _redact_report(_read_report(paths.report), redactions)
        items = _report_items(report)
        run = self._store.get_run(test_run_id) or {}
        validation_errors = (
            passed_report_errors(
                report,
                actual_exit_code=process.returncode,
                release_check=run.get("source") == "release_check",
                attested_invocations=self._store.attested_invocations(test_run_id),
                test_run_id=test_run_id,
                commit_sha=str(run.get("commit_sha") or ""),
            )
            if status == "passed"
            else []
        )
        invalid_success_report = bool(validation_errors)
        if invalid_success_report:
            status = "error"
            report["validation_errors"] = validation_errors
        report.update(
            {
                "duration_seconds": duration_seconds,
                "exit_code": process.returncode,
            }
        )
        self._store.finish_run(
            test_run_id,
            status=status,
            report=report,
            items=items,
            stdout=_truncate(_redact(_read_text_limited(paths.stdout), redactions)),
            stderr=_truncate(_redact(_read_text_limited(paths.stderr), redactions)),
            error=(
                {}
                if status in {"passed", "failed", "cancelled"}
                else {
                    "error_code": (
                        "AGENT_TEST_REPORT_INVALID" if invalid_success_report else "AGENT_TEST_RUN_TIMEOUT" if timed_out else "AGENT_PYTEST_EXECUTION_ERROR"
                    ),
                    "message": (
                        "pytest exited successfully without a complete all-passed structured report"
                        if invalid_success_report
                        else f"pytest exceeded the platform timeout of {self._timeout_seconds} seconds"
                        if timed_out
                        else f"pytest exited with code {process.returncode}"
                    ),
                }
            ),
        )

    def _finish_with_error(self, test_run_id: str, exc: Exception) -> None:
        self._store.finish_run(
            test_run_id,
            status="cancelled" if self._store.cancel_requested(test_run_id) else "error",
            report={},
            items=[],
            stdout="",
            stderr="",
            error={"error_code": "AGENT_TEST_RUN_ERROR", "message": f"{exc.__class__.__name__}: {exc}"},
        )

    def _remove_checkout_safely(
        self,
        test_run_id: str,
        *,
        store: GitAgentVersionStore,
        destination: Path,
    ) -> None:
        try:
            self.remove_checkout(store=store, destination=destination)
        except Exception:
            logger.warning(
                "Failed to remove Agent test checkout: test_run_id=%s path=%s",
                test_run_id,
                destination,
                exc_info=True,
            )


def _run_paths(artifacts_dir: Path, test_run_id: str) -> _RunPaths:
    run_dir = artifacts_dir / test_run_id
    return _RunPaths(
        checkout=run_dir / "workspace",
        report=run_dir / "pytest-report.json",
        stdout=run_dir / "stdout.log",
        stderr=run_dir / "stderr.log",
    )


def _process_status(returncode: int | None, *, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if returncode == 0:
        return "passed"
    if returncode == 1:
        return "failed"
    return "error"


def _run_git(repository: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AgentGitError(detail or f"git {' '.join(args)} failed")
    return result


def _read_report(path: Path) -> JsonObject:
    try:
        if path.stat().st_size > MAX_CAPTURED_OUTPUT_BYTES:
            return {"error": "pytest report exceeded output limit"}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _report_items(report: JsonObject) -> list[JsonObject]:
    items = report.get("items")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _truncate(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURED_OUTPUT_BYTES:
        return value
    return encoded[:MAX_CAPTURED_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n[output truncated]"


def _redact(value: str, secrets_to_remove: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets_to_remove:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_report(report: JsonObject, secrets_to_remove: tuple[str, ...]) -> JsonObject:
    return {_redact(key, secrets_to_remove): _redact_json_value(value, secrets_to_remove) for key, value in report.items()}


def _redact_json_value(value: JsonValue, secrets_to_remove: tuple[str, ...]) -> JsonValue:
    if isinstance(value, str):
        return _redact(value, secrets_to_remove)
    if isinstance(value, list):
        return [_redact_json_value(item, secrets_to_remove) for item in value]
    if isinstance(value, dict):
        return {_redact(str(key), secrets_to_remove): _redact_json_value(item, secrets_to_remove) for key, item in value.items()}
    return value


def _read_text_limited(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_CAPTURED_OUTPUT_BYTES + 1)
    except OSError:
        return ""
    suffix = b"\n[output truncated]" if len(payload) > MAX_CAPTURED_OUTPUT_BYTES else b""
    return (payload[:MAX_CAPTURED_OUTPUT_BYTES] + suffix).decode("utf-8", errors="replace")


def _terminate_process_group(process: subprocess.Popen[str], *, kill: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except OSError:
        return


def _wait_for_process(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, kill=True)
        process.wait(timeout=5)
