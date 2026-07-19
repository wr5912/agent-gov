from __future__ import annotations

import json
import logging
import os
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
from typing import TypeAlias

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.json_types import JsonObject

from .store import AgentTestingStore

logger = logging.getLogger(__name__)

MAX_CAPTURED_OUTPUT_BYTES = 256_000
FIXED_PYTEST_COMMAND = [sys.executable, "-m", "pytest", "-q", "-p", "agentgov_testkit.pytest_plugin", "tests"]
ProcessEnvironment: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class _RunPaths:
    checkout: Path
    report: Path
    stdout: Path
    stderr: Path


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

    def close(self) -> None:
        with self._lock:
            self._closed = True
            processes = list(self._processes.values())
        self._store.reconcile_interrupted_runs()
        for process in processes:
            if process.poll() is None:
                _terminate_process_group(process)
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
        paths = _run_paths(self._artifacts_dir, test_run_id)
        store: GitAgentVersionStore | None = None
        try:
            store = self._store_for(agent_id)
            self.checkout(store=store, commit_sha=commit_sha, destination=paths.checkout)
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

    def _run_pytest(
        self,
        test_run_id: str,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        paths: _RunPaths,
    ) -> None:
        env = self._test_environment(
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
            report_path=paths.report,
        )
        with paths.stdout.open("w", encoding="utf-8") as stdout_file, paths.stderr.open("w", encoding="utf-8") as stderr_file:
            with self._lock:
                if self._closed:
                    return
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
            try:
                started_at = time.monotonic()
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
                self._finish_process(
                    test_run_id,
                    process=process,
                    paths=paths,
                    duration_seconds=time.monotonic() - started_at,
                    timed_out=timed_out,
                )
            finally:
                with self._lock:
                    self._processes.pop(test_run_id, None)
                if process.poll() is None:
                    _terminate_process_group(process, kill=True)

    def _test_environment(
        self,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        report_path: Path,
    ) -> ProcessEnvironment:
        env = dict(os.environ)
        env.update(
            {
                "AGENTGOV_API_BASE": self._api_base_url,
                "AGENTGOV_AGENT_ID": agent_id,
                "AGENTGOV_COMMIT_SHA": commit_sha,
                "AGENTGOV_TEST_REPORT_PATH": str(report_path),
                "PYTHONDONTWRITEBYTECODE": "1",
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
    ) -> None:
        cancelled = self._store.cancel_requested(test_run_id)
        status = "error" if timed_out and not cancelled else _process_status(process.returncode, cancelled=cancelled)
        report = _read_report(paths.report)
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
            items=_report_items(report),
            stdout=_truncate(_read_text_limited(paths.stdout)),
            stderr=_truncate(_read_text_limited(paths.stderr)),
            error=(
                {}
                if status in {"passed", "failed", "cancelled"}
                else {
                    "error_code": "AGENT_TEST_RUN_TIMEOUT" if timed_out else "AGENT_PYTEST_EXECUTION_ERROR",
                    "message": (
                        f"pytest exceeded the platform timeout of {self._timeout_seconds} seconds"
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
