from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from app.runtime.advisory_lock import AdvisoryLockBusy, advisory_lock
from app.runtime.agent_paths import business_agent_layout, validate_agent_id
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir

from .container_executor import (
    SANDBOX_RUN_LABEL,
    SANDBOX_SCOPE_LABEL,
    ContainerSandboxExecutor,
    DockerEngine,
    SandboxCleanupResult,
    SandboxExecutionResult,
    SandboxRunSpec,
)
from .docker_engine import DockerEngineClient, WorkerMountAuthority, inspect_worker_mount_authority
from .execution_contracts import (
    FIXED_PYTEST_COMMAND,
    FIXED_SANDBOX_ENV,
    P0_EXACT_COMMIT_LANE,
    RECEIPT_CONTRACT,
    AgentTestCleanupReceipt,
    AgentTestExecutionReceipt,
    AgentTestInvocationReceipt,
    AgentTestResultReceipt,
    AgentTestTargetReceipt,
    SourceObservation,
    canonical_json_digest,
    sandbox_environment_digest,
)
from .materializer import MaterializationError, SourceFingerprint, materialize_git_commit
from .source_snapshot import SourceSnapshot, SourceSnapshotError, snapshot_source_tree
from .store import AgentTestingStore, AgentTestRunClaimLost
from .suite import inspect_agent_test_suite

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR: Final = Path("/data")
_DEFAULT_RUNS_DIR: Final = Path("/agent-test-runs")
_DEFAULT_DOCKER_SOCKET: Final = Path("/var/run/docker.sock")
_DEFAULT_TIMEOUT_SECONDS: Final = 1800
_DEFAULT_POLL_SECONDS: Final = 0.5
_TERMINAL_STATUS_ERROR = "error"
_WORKER_LOCK_NAME: Final = ".worker.lock"
_CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{12,64}$")


@dataclass(frozen=True, slots=True)
class AgentTestWorkerSettings:
    data_dir: Path
    runs_dir: Path
    sandbox_image: str
    docker_socket: Path
    timeout_seconds: int
    poll_seconds: float
    worker_id: str
    container_id: str
    mountinfo_path: Path = Path("/proc/self/mountinfo")

    def __post_init__(self) -> None:
        for name, path in (
            ("data_dir", self.data_dir),
            ("runs_dir", self.runs_dir),
            ("docker_socket", self.docker_socket),
        ):
            if not path.is_absolute() or path == Path("/") or ".." in path.parts:
                raise ValueError(f"{name} must be a normalized absolute path below root")
        if self.runs_dir.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(self.runs_dir):
            raise ValueError("runs_dir must be outside data_dir so the API cannot mutate sandbox source")
        if _CONTAINER_ID_PATTERN.fullmatch(self.container_id) is None:
            raise ValueError("container_id must be the current Docker container ID")

    @classmethod
    def from_environment(cls) -> AgentTestWorkerSettings:
        data_dir = _absolute_path(os.environ.get("AGENT_TEST_DATA_DIR", str(_DEFAULT_DATA_DIR)), name="AGENT_TEST_DATA_DIR")
        runs_dir = _absolute_path(os.environ.get("AGENT_TEST_RUNS_DIR", str(_DEFAULT_RUNS_DIR)), name="AGENT_TEST_RUNS_DIR")
        docker_socket = _absolute_path(
            os.environ.get("AGENT_TEST_DOCKER_SOCKET", str(_DEFAULT_DOCKER_SOCKET)),
            name="AGENT_TEST_DOCKER_SOCKET",
        )
        sandbox_image = os.environ.get("AGENT_TEST_SANDBOX_IMAGE", "").strip()
        if not sandbox_image or any(ord(character) < 32 for character in sandbox_image):
            raise ValueError("AGENT_TEST_SANDBOX_IMAGE is required")
        timeout_seconds = _bounded_int(
            os.environ.get("AGENT_TEST_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)),
            name="AGENT_TEST_TIMEOUT_SECONDS",
            minimum=1,
            maximum=86_400,
        )
        poll_seconds = _bounded_float(
            os.environ.get("AGENT_TEST_POLL_SECONDS", str(_DEFAULT_POLL_SECONDS)),
            name="AGENT_TEST_POLL_SECONDS",
            minimum=0.05,
            maximum=60.0,
        )
        raw_worker_id = os.environ.get("AGENT_TEST_WORKER_ID", "").strip()
        worker_id = raw_worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        if len(worker_id) > 128 or not worker_id or any(ord(character) < 32 for character in worker_id):
            raise ValueError("AGENT_TEST_WORKER_ID is invalid")
        return cls(
            data_dir=data_dir,
            runs_dir=runs_dir,
            sandbox_image=sandbox_image,
            docker_socket=docker_socket,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            worker_id=worker_id,
            container_id=socket.gethostname(),
        )


@dataclass(frozen=True, slots=True)
class _ClaimContext:
    test_run_id: str
    agent_id: str
    commit_sha: str
    owner_worker_id: str
    generation: int
    run_dir: Path
    workspace: Path


@dataclass(slots=True)
class _WorkerOutcome:
    target: AgentTestTargetReceipt | None = None
    execution: SandboxExecutionResult | None = None
    report: JsonObject | None = None
    stdout: str = ""
    stderr: str = ""
    status: str = _TERMINAL_STATUS_ERROR
    error: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class _RunCleanupResult:
    container: SandboxCleanupResult
    temporary_paths_removed: bool

    @property
    def error_codes(self) -> tuple[str, ...]:
        return _cleanup_error_codes(self.container, temporary_paths_removed=self.temporary_paths_removed)

    @property
    def complete(self) -> bool:
        return self.container.complete and self.temporary_paths_removed


class AgentTestWorker:
    def __init__(
        self,
        *,
        store: AgentTestingStore,
        settings: AgentTestWorkerSettings,
        engine: DockerEngine,
        executor: ContainerSandboxExecutor,
    ) -> None:
        self.store = store
        self.settings = settings
        self.engine = engine
        self.executor = executor
        self._runs_dir = settings.runs_dir
        self._mount_authority = _load_worker_mount_authority(engine, settings=settings)
        self._sandbox_scope_id = canonical_json_digest(self._mount_authority.runs_volume_name)

    def recover(self) -> JsonObject:
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        running = self.store.running_runs()
        recovered: list[str] = []
        for run in running:
            test_run_id = str(run["test_run_id"])
            adopted = self.store.adopt_running_run(test_run_id, worker_id=self.settings.worker_id)
            if adopted is None:
                continue
            cleanup = self._cleanup_run_resources(test_run_id)
            receipt = self._interrupted_receipt(adopted, cleanup=cleanup)
            status = "interrupted" if cleanup.complete else _TERMINAL_STATUS_ERROR
            error_code = "AGENT_TEST_RUN_INTERRUPTED" if cleanup.complete else "AGENT_TEST_RECOVERY_CLEANUP_FAILED"
            try:
                self.store.finish_run(
                    test_run_id,
                    worker_id=str(adopted.get("_worker_id") or ""),
                    claim_generation=int(adopted.get("_claim_generation") or 0),
                    status=status,
                    report={},
                    receipt=receipt.model_dump(mode="json") if receipt is not None else None,
                    items=[],
                    stdout="",
                    stderr="",
                    error={"error_code": error_code, "message": "Agent test worker restarted before the run completed."},
                )
                recovered.append(test_run_id)
            except AgentTestRunClaimLost:
                logger.info("Agent test recovery claim already resolved: %s", test_run_id)
        residual_cleanup = self._cleanup_all_labeled_containers()
        stale_paths_removed = residual_cleanup.complete and _remove_stale_run_paths(self._runs_dir)
        return {
            "interrupted": recovered,
            "residual_containers_removed": residual_cleanup.removed,
            "residual_labels_empty": residual_cleanup.labels_empty,
            "stale_paths_removed": stale_paths_removed,
            "safe_to_consume": residual_cleanup.complete and stale_paths_removed,
        }

    def run_once(self) -> bool:
        claimed = self.store.claim_next_run(worker_id=self.settings.worker_id)
        if claimed is None:
            return False
        if not self._execute_claim(claimed):
            raise AgentTestWorkerUnsafeState("Agent test cleanup failed; worker consumption stopped")
        return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                time.sleep(self.settings.poll_seconds)

    def _execute_claim(self, run: JsonObject) -> bool:
        context = self._claim_context(run)
        outcome = self._prepare_and_execute(run, context=context)
        cleanup = _RunCleanupResult(
            container=outcome.execution.cleanup if outcome.execution is not None else self._cleanup_labeled_run(context.test_run_id),
            temporary_paths_removed=_remove_tree(context.run_dir),
        )
        cleanup_codes = cleanup.error_codes
        if cleanup_codes:
            outcome.status = _TERMINAL_STATUS_ERROR
            outcome.error = {
                "error_code": "AGENT_TEST_CLEANUP_FAILED",
                "message": "Agent test cleanup could not be proven.",
                "cleanup_error_codes": list(cleanup_codes),
            }
        report = outcome.report or {}
        receipt = (
            _build_receipt(
                test_run_id=context.test_run_id,
                worker_id=self.settings.worker_id,
                target=outcome.target,
                execution=outcome.execution,
                status=outcome.status,
                report=report,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                cleanup=cleanup,
            )
            if outcome.target is not None
            else None
        )
        self.store.finish_run(
            context.test_run_id,
            worker_id=context.owner_worker_id,
            claim_generation=context.generation,
            status=outcome.status,
            report=report,
            receipt=receipt.model_dump(mode="json") if receipt is not None else None,
            items=_report_items(report),
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            error=outcome.error or {},
        )
        return not cleanup_codes

    def _prepare_and_execute(self, run: JsonObject, *, context: _ClaimContext) -> _WorkerOutcome:
        result: SandboxExecutionResult | None = None
        report: JsonObject = {}
        target = self._target_or_none(run)
        try:
            if self.store.cancel_requested(context.test_run_id):
                return _WorkerOutcome(target=target, status="cancelled")
            agent_id = validate_agent_id(context.agent_id)
            if list(run.get("command") or []) != list(FIXED_PYTEST_COMMAND):
                raise _WorkerRunError("AGENT_TEST_COMMAND_MISMATCH", "queued command does not match the fixed P0 lane")
            context.run_dir.mkdir(parents=True, exist_ok=False)
            fingerprint = materialize_git_commit(
                business_agent_layout(self.settings.data_dir, agent_id).workspace,
                context.commit_sha,
                context.workspace,
            )
            self._validate_materialized_target(run, fingerprint)
            _make_sandbox_readable(context.workspace)
            pre_snapshot = snapshot_source_tree(context.workspace)
            target = self._stored_target(
                run,
                source_observation="pre_only",
                pre_source_digest=pre_snapshot.source_digest,
                post_source_digest=None,
            )
            self._validate_workspace_snapshot(fingerprint, pre_snapshot)
            suite = inspect_agent_test_suite(context.workspace, agent_id=agent_id, commit_sha=context.commit_sha)
            self._validate_suite(run, suite)
            result = self._execute_sandbox(context=context, agent_id=agent_id)
            report = dict(result.report or {})
            status, error = _execution_status(result, report=report)
            post_snapshot = snapshot_source_tree(context.workspace)
            if post_snapshot != pre_snapshot:
                target = self._stored_target(
                    run,
                    source_observation="changed",
                    pre_source_digest=pre_snapshot.source_digest,
                    post_source_digest=post_snapshot.source_digest,
                )
                raise _WorkerRunError("AGENT_TEST_SOURCE_CHANGED", "mounted sandbox source changed during execution")
            target = self._stored_target(
                run,
                source_observation="stable",
                pre_source_digest=pre_snapshot.source_digest,
                post_source_digest=post_snapshot.source_digest,
            )
            return _WorkerOutcome(
                target=target,
                execution=result,
                report=report,
                stdout=result.stdout,
                stderr=result.stderr,
                status=status,
                error=error,
            )
        except (MaterializationError, SourceSnapshotError, ValidationError, ValueError, _WorkerRunError) as exc:
            return _expected_error_outcome(exc, target=target, execution=result, report=report)
        except Exception as exc:
            return _unexpected_error_outcome(
                exc,
                test_run_id=context.test_run_id,
                target=target,
                execution=result,
                report=report,
            )

    def _execute_sandbox(self, *, context: _ClaimContext, agent_id: str) -> SandboxExecutionResult:
        expected_workspace = self._runs_dir / context.test_run_id / "workspace"
        if context.workspace != expected_workspace:
            raise _WorkerRunError(
                "AGENT_TEST_WORKSPACE_AUTHORITY_INVALID",
                "worker source observation does not match the selected named-volume subpath",
            )
        return self.executor.execute(
            SandboxRunSpec(
                run_id=context.test_run_id,
                scope_id=self._sandbox_scope_id,
                agent_id=agent_id,
                commit_sha=context.commit_sha,
                workspace_path=context.workspace,
                runs_volume_name=self._mount_authority.runs_volume_name,
                image_ref=self.settings.sandbox_image,
                timeout_seconds=self.settings.timeout_seconds,
            ),
            cancel_requested=lambda: self.store.cancel_requested(context.test_run_id),
            on_container_created=lambda container_id: self.store.bind_container(
                context.test_run_id,
                worker_id=context.owner_worker_id,
                claim_generation=context.generation,
                container_id=container_id,
            ),
        )

    def _claim_context(self, run: JsonObject) -> _ClaimContext:
        test_run_id = str(run["test_run_id"])
        run_dir = self._runs_dir / test_run_id
        return _ClaimContext(
            test_run_id=test_run_id,
            agent_id=str(run["agent_id"]),
            commit_sha=str(run["commit_sha"]),
            owner_worker_id=str(run.get("_worker_id") or ""),
            generation=int(run.get("_claim_generation") or 0),
            run_dir=run_dir,
            workspace=run_dir / "workspace",
        )

    def _target_or_none(self, run: JsonObject) -> AgentTestTargetReceipt | None:
        try:
            return self._stored_target(
                run,
                source_observation="not_observed",
                pre_source_digest=None,
                post_source_digest=None,
            )
        except ValidationError:
            return None

    def _stored_target(
        self,
        run: JsonObject,
        *,
        source_observation: SourceObservation,
        pre_source_digest: str | None,
        post_source_digest: str | None,
    ) -> AgentTestTargetReceipt:
        return AgentTestTargetReceipt(
            agent_id=str(run["agent_id"]),
            commit_sha=str(run["commit_sha"]),
            tree_sha=str(run.get("source_tree_sha") or ""),
            source_digest=str(run.get("source_digest") or ""),
            pre_source_digest=pre_source_digest,
            post_source_digest=post_source_digest,
            source_observation=source_observation,
            suite_digest=str(run.get("suite_digest") or ""),
        )

    @staticmethod
    def _validate_materialized_target(run: JsonObject, fingerprint: SourceFingerprint) -> None:
        if (
            fingerprint.commit_sha != run.get("commit_sha")
            or fingerprint.tree_sha != run.get("source_tree_sha")
            or fingerprint.source_digest != run.get("source_digest")
        ):
            raise _WorkerRunError("AGENT_TEST_SOURCE_MISMATCH", "materialized source does not match the queued target")

    @staticmethod
    def _validate_workspace_snapshot(fingerprint: SourceFingerprint, snapshot: SourceSnapshot) -> None:
        if (
            snapshot.source_digest != fingerprint.source_digest
            or snapshot.file_count != fingerprint.file_count
            or snapshot.total_bytes != fingerprint.total_bytes
        ):
            raise _WorkerRunError("AGENT_TEST_SOURCE_MISMATCH", "actual sandbox source does not match the materialized target")

    @staticmethod
    def _validate_suite(run: JsonObject, suite: object) -> None:
        current = getattr(suite, "suite_digest", None)
        if not getattr(suite, "runnable", False) or current != run.get("suite_digest"):
            raise _WorkerRunError("AGENT_TEST_SUITE_MISMATCH", "materialized suite does not match the queued target")
        if getattr(suite, "requires_live_agent", False):
            raise _WorkerRunError(
                "AGENT_TEST_LIVE_FIXTURE_REQUIRES_LIVE_LANE",
                "P0 exact-commit lane cannot execute a suite that uses the live agent fixture",
            )

    def _cleanup_run_resources(self, test_run_id: str) -> _RunCleanupResult:
        return _RunCleanupResult(
            container=self._cleanup_labeled_run(test_run_id),
            temporary_paths_removed=_remove_tree(self._runs_dir / test_run_id),
        )

    def _cleanup_labeled_run(self, test_run_id: str) -> SandboxCleanupResult:
        return _remove_labeled_containers(self.engine, f"{SANDBOX_RUN_LABEL}={test_run_id}")

    def _cleanup_all_labeled_containers(self) -> SandboxCleanupResult:
        return _remove_labeled_containers(self.engine, f"{SANDBOX_SCOPE_LABEL}={self._sandbox_scope_id}")

    def _interrupted_receipt(
        self,
        run: JsonObject,
        *,
        cleanup: _RunCleanupResult,
    ) -> AgentTestExecutionReceipt | None:
        try:
            target = self._stored_target(
                run,
                source_observation="not_observed",
                pre_source_digest=None,
                post_source_digest=None,
            )
        except ValidationError:
            return None
        cleanup_codes = cleanup.error_codes
        status = "interrupted" if not cleanup_codes else _TERMINAL_STATUS_ERROR
        return _build_receipt(
            test_run_id=str(run["test_run_id"]),
            worker_id=self.settings.worker_id,
            target=target,
            execution=None,
            status=status,
            report={},
            stdout="",
            stderr="",
            cleanup=cleanup,
        )


def _load_worker_mount_authority(
    engine: DockerEngine,
    *,
    settings: AgentTestWorkerSettings,
) -> WorkerMountAuthority:
    try:
        return inspect_worker_mount_authority(
            engine,
            container_id=settings.container_id,
            data_dir=settings.data_dir,
            runs_dir=settings.runs_dir,
            mountinfo_path=settings.mountinfo_path,
        )
    except Exception as exc:
        raise AgentTestWorkerUnsafeState("worker Docker mount authority could not be proven") from exc


class _WorkerRunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AgentTestWorkerUnsafeState(RuntimeError):
    """Raised when cleanup cannot be proven and no further run may be claimed."""


def _expected_error_outcome(
    exc: Exception,
    *,
    target: AgentTestTargetReceipt | None,
    execution: SandboxExecutionResult | None,
    report: JsonObject,
) -> _WorkerOutcome:
    code = exc.code if isinstance(exc, (MaterializationError, SourceSnapshotError, _WorkerRunError)) else "AGENT_TEST_WORKER_CONTRACT_ERROR"
    return _error_outcome(target=target, execution=execution, report=report, error={"error_code": code, "message": str(exc)})


def _unexpected_error_outcome(
    exc: Exception,
    *,
    test_run_id: str,
    target: AgentTestTargetReceipt | None,
    execution: SandboxExecutionResult | None,
    report: JsonObject,
) -> _WorkerOutcome:
    logger.exception("Agent test worker failed: %s", test_run_id)
    return _error_outcome(
        target=target,
        execution=execution,
        report=report,
        error={"error_code": "AGENT_TEST_WORKER_ERROR", "message": f"{exc.__class__.__name__}: worker execution failed"},
    )


def _error_outcome(
    *,
    target: AgentTestTargetReceipt | None,
    execution: SandboxExecutionResult | None,
    report: JsonObject,
    error: JsonObject,
) -> _WorkerOutcome:
    return _WorkerOutcome(
        target=target,
        execution=execution,
        report=report,
        stdout=execution.stdout if execution is not None else "",
        stderr=execution.stderr if execution is not None else "",
        error=error,
    )


def _build_receipt(
    *,
    test_run_id: str,
    worker_id: str,
    target: AgentTestTargetReceipt,
    execution: SandboxExecutionResult | None,
    status: str,
    report: JsonObject,
    stdout: str,
    stderr: str,
    cleanup: _RunCleanupResult,
) -> AgentTestExecutionReceipt:
    invocation = None
    if execution is not None and execution.image_id is not None and execution.isolation is not None:
        invocation = AgentTestInvocationReceipt(
            image_id=execution.image_id,
            argv=FIXED_PYTEST_COMMAND,
            environment_keys=tuple(sorted(FIXED_SANDBOX_ENV)),
            environment_digest=sandbox_environment_digest(),
            working_directory="/workspace",
        )
    receipt = AgentTestExecutionReceipt(
        contract=RECEIPT_CONTRACT,
        lane=P0_EXACT_COMMIT_LANE,
        assurance_level="execution_provenance",
        test_run_id=test_run_id,
        worker_id=worker_id,
        container_id=execution.container_id if execution is not None else None,
        target=target,
        invocation=invocation,
        isolation=execution.isolation if execution is not None else None,
        result=AgentTestResultReceipt(
            status=status,
            exit_code=execution.exit_code if execution is not None else None,
            duration_ms=max(0, round((execution.duration_seconds if execution is not None else 0.0) * 1000)),
            workspace_report_authority="agent_owned_unverified",
            workspace_report_digest=canonical_json_digest(report),
            stdout_digest=canonical_json_digest(stdout),
            stderr_digest=canonical_json_digest(stderr),
        ),
        cleanup=AgentTestCleanupReceipt(
            container_removed=cleanup.container.removed,
            label_residue_absent=cleanup.container.labels_empty,
            temporary_paths_removed=cleanup.temporary_paths_removed,
            error_codes=cleanup.error_codes,
        ),
    )
    return receipt.with_digest()


def _execution_status(result: SandboxExecutionResult, *, report: JsonObject) -> tuple[str, JsonObject]:
    if result.cancelled:
        return "cancelled", {}
    if result.timed_out:
        return _TERMINAL_STATUS_ERROR, {"error_code": "AGENT_TEST_RUN_TIMEOUT", "message": "sandbox exceeded the platform timeout"}
    if result.error_code:
        return _TERMINAL_STATUS_ERROR, {"error_code": result.error_code, "message": result.error_message or "sandbox execution failed"}
    if result.report is None or report.get("exit_code") != result.exit_code:
        return _TERMINAL_STATUS_ERROR, {
            "error_code": "AGENT_TEST_REPORT_MISMATCH",
            "message": "pytest report is missing or disagrees with the container exit code",
        }
    if result.exit_code == 0:
        return "passed", {}
    if result.exit_code == 1:
        return "failed", {}
    return _TERMINAL_STATUS_ERROR, {
        "error_code": "AGENT_PYTEST_EXECUTION_ERROR",
        "message": f"pytest exited with code {result.exit_code}",
    }


def _report_items(report: JsonObject) -> list[JsonObject]:
    items = report.get("items")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _remove_labeled_containers(engine: DockerEngine, label: str) -> SandboxCleanupResult:
    errors: list[str] = []
    try:
        ids = engine.container_ids_with_label(label)
    except Exception as exc:
        return SandboxCleanupResult(removed=False, labels_empty=False, error_message=f"list:{exc.__class__.__name__}")
    for container_id in ids:
        try:
            engine.remove_container(container_id)
        except Exception as exc:
            errors.append(f"remove:{exc.__class__.__name__}")
    try:
        labels_empty = not engine.container_ids_with_label(label)
    except Exception as exc:
        labels_empty = False
        errors.append(f"audit:{exc.__class__.__name__}")
    return SandboxCleanupResult(removed=not errors and labels_empty, labels_empty=labels_empty, error_message=",".join(errors) or None)


def _cleanup_error_codes(cleanup: SandboxCleanupResult, *, temporary_paths_removed: bool) -> tuple[str, ...]:
    codes: list[str] = []
    if not cleanup.removed:
        codes.append("CONTAINER_NOT_REMOVED")
    if not cleanup.labels_empty:
        codes.append("CONTAINER_LABEL_RESIDUE")
    if cleanup.error_message:
        codes.append("CONTAINER_CLEANUP_ERROR")
    if not temporary_paths_removed:
        codes.append("TEMPORARY_PATHS_NOT_REMOVED")
    return tuple(codes)


def _make_sandbox_readable(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o555)


def _remove_tree(path: Path) -> bool:
    try:
        if path.exists() or path.is_symlink():
            _restore_directory_write_permissions(path)
            shutil.rmtree(path)
    except OSError:
        return False
    return not path.exists() and not path.is_symlink()


def _remove_stale_run_paths(runs_dir: Path) -> bool:
    try:
        children = tuple(path for path in runs_dir.iterdir() if path.name != _WORKER_LOCK_NAME)
    except OSError:
        return False
    return all(_remove_tree(path) for path in children)


def _restore_directory_write_permissions(root: Path) -> None:
    """Restore owner traversal only after the sandbox container is gone.

    The source tree is deliberately 0555 during execution.  `rmtree` still needs
    write permission on every parent directory; never follow a candidate-owned
    symlink while restoring cleanup permissions.
    """

    if root.is_symlink() or not root.is_dir():
        return
    for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if not current_path.is_symlink():
            os.chmod(current_path, 0o700, follow_symlinks=False)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]


def _absolute_path(raw: str, *, name: str) -> Path:
    value = Path(raw.strip()) if raw.strip() else Path()
    if not raw.strip() or not value.is_absolute() or value == Path("/") or ".." in value.parts:
        raise ValueError(f"{name} must be a normalized absolute path below root")
    return value


def _bounded_int(raw: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(raw: str, *, name: str, minimum: float, maximum: float) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def run_agent_test_worker() -> int:
    settings = AgentTestWorkerSettings.from_environment()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.runs_dir / _WORKER_LOCK_NAME
    try:
        with advisory_lock(lock_path, mode="exclusive", blocking=False):
            store = AgentTestingStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
            with DockerEngineClient(socket_path=settings.docker_socket) as engine:
                worker = AgentTestWorker(
                    store=store,
                    settings=settings,
                    engine=engine,
                    executor=ContainerSandboxExecutor(engine=engine),
                )
                recovery = worker.recover()
                if recovery["interrupted"]:
                    logger.warning("Recovered interrupted Agent test runs: %s", recovery)
                if not recovery["safe_to_consume"]:
                    logger.error("Agent test recovery cleanup is incomplete; refusing to consume queued runs")
                    return 3
                worker.run_forever()
    except AdvisoryLockBusy:
        logger.error("Another agent-test-worker already owns the runtime volume")
        return 2
    except AgentTestWorkerUnsafeState:
        logger.exception("Agent test worker stopped after an unproven cleanup")
        return 3
    return 0
