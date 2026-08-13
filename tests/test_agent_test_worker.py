from __future__ import annotations

from pathlib import Path

import pytest
from app.agent_testing import worker as worker_module
from app.agent_testing.container_executor import (
    SANDBOX_KIND_LABEL,
    SANDBOX_RUN_LABEL,
    SANDBOX_SCOPE_LABEL,
    SandboxCleanupResult,
    SandboxExecutionResult,
)
from app.agent_testing.models import AgentTestRunModel
from app.agent_testing.store import AgentTestRunClaimLost
from app.agent_testing.worker import AgentTestWorker, AgentTestWorkerSettings, AgentTestWorkerUnsafeState
from tests.agent_test_worker_test_support import (
    AGENT_ID,
    COMMIT_SHA,
    CONTAINER_ID,
    DAEMON_DATA_ROOT,
    RUNS_VOLUME_MOUNTPOINT,
    RUNS_VOLUME_NAME,
    SOURCE_DIGEST,
    SUITE_DIGEST,
    WORKER_CONTAINER_ID,
)
from tests.agent_test_worker_test_support import (
    FailingCleanupEngine as _FailingCleanupEngine,
)
from tests.agent_test_worker_test_support import (
    FakeEngine as _FakeEngine,
)
from tests.agent_test_worker_test_support import (
    FakeExecutor as _FakeExecutor,
)
from tests.agent_test_worker_test_support import (
    NoCallExecutor as _NoCallExecutor,
)
from tests.agent_test_worker_test_support import (
    RaisingExecutor as _RaisingExecutor,
)
from tests.agent_test_worker_test_support import (
    execution_result as _execution_result,
)
from tests.agent_test_worker_test_support import (
    install_materialization_contract as _install_materialization_contract,
)
from tests.agent_test_worker_test_support import (
    queued_run as _queued_run,
)
from tests.agent_test_worker_test_support import (
    source_digest as _source_digest,
)
from tests.agent_test_worker_test_support import (
    testing_store as _store,
)
from tests.agent_test_worker_test_support import (
    typed_receipt as _typed_receipt,
)
from tests.agent_test_worker_test_support import (
    worker_settings as _settings,
)
from tests.agent_test_worker_test_support import (
    write_mountinfo as _write_mountinfo,
)


def test_worker_atomically_claims_queued_run_and_persists_trusted_pass_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch)
    report = {
        "exit_code": 0,
        "duration_seconds": 0.125,
        "items": [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed", "phase": "call"}],
    }
    executor = _FakeExecutor(store=store, result=_execution_result(report=report))
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=executor)  # type: ignore[arg-type]

    assert queued["status"] == "queued"
    assert worker.run_once() is True
    assert worker.run_once() is False

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "passed"
    assert finished["exit_code"] == 0
    assert finished["duration_seconds"] == 0.125
    assert executor.observed_claims == [{"status": "running", "worker_id": "worker-a", "claim_generation": 1}]
    assert finished["items"] == [
        {
            "nodeid": "tests/test_safe.py::test_safe",
            "outcome": "passed",
            "phase": "call",
            "duration_seconds": None,
            "detail": None,
        }
    ]
    receipt = _typed_receipt(finished)
    assert receipt.result.status == "passed"
    assert receipt.target.source_digest == SOURCE_DIGEST
    assert receipt.target.pre_source_digest == SOURCE_DIGEST
    assert receipt.target.post_source_digest == SOURCE_DIGEST
    assert receipt.target.source_observation == "stable"
    assert receipt.target.suite_digest == SUITE_DIGEST
    assert receipt.cleanup.complete is True
    assert receipt.worker_id == settings.worker_id

    spec = executor.specs[0]
    assert spec.workspace_path == settings.runs_dir / str(queued["test_run_id"]) / "workspace"
    assert spec.runs_volume_name == RUNS_VOLUME_NAME
    assert spec.scope_id == worker._sandbox_scope_id
    assert not (settings.runs_dir / str(queued["test_run_id"])).exists()


def test_worker_rejects_passed_result_when_executor_mutates_actual_mounted_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch)

    def mutate_workspace(workspace: Path) -> None:
        source = workspace / "CLAUDE.md"
        workspace.chmod(0o755)
        source.chmod(0o644)
        source.write_bytes(b"# tampered after sandbox start\n")
        source.chmod(0o444)
        workspace.chmod(0o555)

    executor = _FakeExecutor(
        store=store,
        result=_execution_result(
            report={
                "exit_code": 0,
                "items": [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed", "phase": "call"}],
            }
        ),
        workspace_mutator=mutate_workspace,
    )
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=executor)  # type: ignore[arg-type]

    assert worker.run_once() is True

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == "AGENT_TEST_SOURCE_CHANGED"
    receipt = _typed_receipt(finished)
    assert receipt.result.status == "error"
    assert receipt.target.source_observation == "changed"
    assert receipt.target.pre_source_digest == SOURCE_DIGEST
    assert receipt.target.post_source_digest == _source_digest(b"# tampered after sandbox start\n")
    assert store.latest_passed_for_commit(agent_id=AGENT_ID, commit_sha=COMMIT_SHA) is None


def test_queued_cancel_is_finalized_by_worker_with_cleanup_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    requested = store.request_cancel(str(queued["test_run_id"]))
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=_NoCallExecutor())  # type: ignore[arg-type]

    assert requested["status"] == "queued"
    assert requested["cancel_requested"] is True
    assert worker.run_once() is True

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "cancelled"
    receipt = _typed_receipt(finished)
    assert receipt.result.status == "cancelled"
    assert receipt.target.source_observation == "not_observed"
    assert receipt.target.pre_source_digest is None
    assert receipt.target.post_source_digest is None
    assert receipt.invocation is None
    assert receipt.isolation is None
    assert receipt.cleanup.complete is True


@pytest.mark.parametrize(
    ("case", "source_digest", "suite_digest", "requires_live_agent", "error_code", "expected_observation"),
    [
        ("source", "0" * 64, SUITE_DIGEST, False, "AGENT_TEST_SOURCE_MISMATCH", "not_observed"),
        ("suite", SOURCE_DIGEST, "1" * 64, False, "AGENT_TEST_SUITE_MISMATCH", "pre_only"),
        ("live", SOURCE_DIGEST, SUITE_DIGEST, True, "AGENT_TEST_LIVE_FIXTURE_REQUIRES_LIVE_LANE", "pre_only"),
    ],
)
def test_worker_fails_closed_before_sandbox_for_source_suite_or_live_fixture_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    source_digest: str,
    suite_digest: str,
    requires_live_agent: bool,
    error_code: str,
    expected_observation: str,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(
        monkeypatch,
        source_digest=source_digest,
        suite_digest=suite_digest,
        requires_live_agent=requires_live_agent,
    )
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=_NoCallExecutor())  # type: ignore[arg-type]

    assert worker.run_once() is True, case

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == error_code
    receipt = _typed_receipt(finished)
    assert receipt.result.status == "error"
    assert receipt.invocation is None
    assert receipt.isolation is None
    assert receipt.cleanup.complete is True
    assert receipt.target.source_observation == expected_observation
    assert receipt.target.post_source_digest is None


def test_worker_fails_closed_when_actual_materialized_directory_disagrees_with_queued_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch, written_content=b"materializer wrote different bytes\n")
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=_NoCallExecutor())  # type: ignore[arg-type]

    assert worker.run_once() is True

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == "AGENT_TEST_SOURCE_MISMATCH"
    receipt = _typed_receipt(finished)
    assert receipt.result.status == "error"
    assert receipt.target.source_observation == "pre_only"
    assert receipt.target.pre_source_digest == _source_digest(b"materializer wrote different bytes\n")
    assert receipt.target.post_source_digest is None


def test_worker_preserves_pre_only_observation_when_executor_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch)
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=_RaisingExecutor())  # type: ignore[arg-type]

    assert worker.run_once() is True

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == "AGENT_TEST_WORKER_ERROR"
    receipt = _typed_receipt(finished)
    assert receipt.target.source_observation == "pre_only"
    assert receipt.target.pre_source_digest == SOURCE_DIGEST
    assert receipt.target.post_source_digest is None


def test_worker_unexpected_post_observation_error_preserves_execution_report_and_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch)
    report = {
        "exit_code": 0,
        "items": [{"nodeid": "tests/test_safe.py::test_safe", "outcome": "passed", "phase": "call"}],
    }
    executor = _FakeExecutor(store=store, result=_execution_result(report=report))
    original_snapshot = worker_module.snapshot_source_tree
    observations = 0

    def fail_post_observation(workspace: Path):
        nonlocal observations
        observations += 1
        if observations == 2:
            raise RuntimeError("post observation unavailable")
        return original_snapshot(workspace)

    monkeypatch.setattr(worker_module, "snapshot_source_tree", fail_post_observation)
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=executor)  # type: ignore[arg-type]

    assert worker.run_once() is True

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == "AGENT_TEST_WORKER_ERROR"
    assert finished["report"] == report
    assert finished["stdout"] == "pytest stdout"
    assert finished["stderr"] == ""
    receipt = _typed_receipt(finished)
    assert receipt.container_id == CONTAINER_ID
    assert receipt.result.exit_code == 0
    assert receipt.target.source_observation == "pre_only"


@pytest.mark.parametrize(
    ("terminal_case", "result", "cancel_before_result", "expected_status", "expected_error_code"),
    [
        ("cancel", _execution_result(exit_code=137, report={}, cancelled=True), True, "cancelled", None),
        ("timeout", _execution_result(exit_code=137, report={}, timed_out=True), False, "error", "AGENT_TEST_RUN_TIMEOUT"),
        (
            "cleanup",
            _execution_result(cleanup=SandboxCleanupResult(removed=False, labels_empty=False, error_message="remove:failed")),
            False,
            "error",
            "AGENT_TEST_CLEANUP_FAILED",
        ),
    ],
)
def test_worker_projects_cancel_timeout_and_cleanup_failure_to_terminal_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_case: str,
    result: SandboxExecutionResult,
    cancel_before_result: bool,
    expected_status: str,
    expected_error_code: str | None,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    queued = _queued_run(store)
    _install_materialization_contract(monkeypatch)
    executor = _FakeExecutor(store=store, result=result, cancel_before_result=cancel_before_result)
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=executor)  # type: ignore[arg-type]

    if terminal_case == "cleanup":
        with pytest.raises(AgentTestWorkerUnsafeState):
            worker.run_once()
    else:
        assert worker.run_once() is True, terminal_case

    finished = store.get_run(str(queued["test_run_id"]))
    assert finished is not None
    assert finished["status"] == expected_status
    if expected_error_code is None:
        assert finished["error"] == {}
    else:
        assert finished["error"]["error_code"] == expected_error_code
    receipt = _typed_receipt(finished)
    assert receipt.result.status == expected_status
    assert receipt.target.source_observation == "stable"
    assert finished["exit_code"] == receipt.result.exit_code
    assert finished["duration_seconds"] == receipt.result.duration_ms / 1000
    assert receipt.cleanup.complete is (terminal_case != "cleanup")
    if terminal_case == "cleanup":
        assert set(receipt.cleanup.error_codes) == {
            "CONTAINER_NOT_REMOVED",
            "CONTAINER_LABEL_RESIDUE",
            "CONTAINER_CLEANUP_ERROR",
        }


def test_stale_worker_claim_cannot_bind_or_overwrite_new_claim_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, worker_id="worker-old")
    store = _store(settings)
    queued = _queued_run(store)
    stale_claim = store.claim_next_run(worker_id=settings.worker_id)
    assert stale_claim is not None
    with store.Session.begin() as db:
        row = db.get(AgentTestRunModel, str(queued["test_run_id"]))
        assert row is not None
        row.worker_id = "worker-new"
        row.claim_generation += 1
    _install_materialization_contract(monkeypatch)
    executor = _FakeExecutor(store=store, result=_execution_result())
    worker = AgentTestWorker(store=store, settings=settings, engine=_FakeEngine(settings=settings), executor=executor)  # type: ignore[arg-type]

    with pytest.raises(AgentTestRunClaimLost):
        worker._execute_claim(stale_claim)

    current = store.get_run(str(queued["test_run_id"]))
    assert current is not None
    assert current["status"] == "running"
    assert current["receipt"] is None
    running = store.running_runs()[0]
    assert running["_worker_id"] == "worker-new"
    assert running["_claim_generation"] == 2


def test_worker_restart_recovers_running_claim_and_removes_labeled_containers_and_temp_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_id="worker-recovery")
    store = _store(settings)
    queued = _queued_run(store)
    claimed = store.claim_next_run(worker_id="worker-before-restart")
    assert claimed is not None
    store.bind_container(
        str(queued["test_run_id"]),
        worker_id="worker-before-restart",
        claim_generation=1,
        container_id=CONTAINER_ID,
    )
    with store.Session.begin() as db:
        legacy = db.get(AgentTestRunModel, str(queued["test_run_id"]))
        assert legacy is not None
        legacy.worker_id = None
        legacy.claim_generation = 0
    run_dir = settings.runs_dir / str(queued["test_run_id"])
    run_dir.mkdir(parents=True)
    run_dir.joinpath("partial-output").write_text("partial", encoding="utf-8")
    residual_container_id = "9" * 64
    foreign_container_id = "8" * 64
    scope_label = f"{SANDBOX_SCOPE_LABEL}={worker_module.canonical_json_digest(RUNS_VOLUME_NAME)}"
    engine = _FakeEngine(
        settings=settings,
        labels_by_container={
            CONTAINER_ID: {
                f"{SANDBOX_RUN_LABEL}={queued['test_run_id']}",
                f"{SANDBOX_KIND_LABEL}=true",
            },
            residual_container_id: {f"{SANDBOX_KIND_LABEL}=true", scope_label},
            foreign_container_id: {
                f"{SANDBOX_KIND_LABEL}=true",
                f"{SANDBOX_SCOPE_LABEL}={'0' * 64}",
            },
        },
    )
    worker = AgentTestWorker(store=store, settings=settings, engine=engine, executor=_NoCallExecutor())  # type: ignore[arg-type]

    recovery = worker.recover()

    assert recovery == {
        "interrupted": [queued["test_run_id"]],
        "residual_containers_removed": True,
        "residual_labels_empty": True,
        "stale_paths_removed": True,
        "safe_to_consume": True,
    }
    assert set(engine.removed) == {CONTAINER_ID, residual_container_id}
    assert foreign_container_id in engine.labels_by_container
    assert not run_dir.exists()
    recovered = store.get_run(str(queued["test_run_id"]))
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["error"]["error_code"] == "AGENT_TEST_RUN_INTERRUPTED"
    receipt = _typed_receipt(recovered)
    assert receipt.result.status == "interrupted"
    assert receipt.target.source_observation == "not_observed"
    assert receipt.target.pre_source_digest is None
    assert receipt.target.post_source_digest is None
    assert receipt.worker_id == settings.worker_id
    assert receipt.cleanup.complete is True


def test_worker_recovery_reports_container_and_temporary_path_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, worker_id="worker-recovery")
    store = _store(settings)
    queued = _queued_run(store)
    claimed = store.claim_next_run(worker_id="worker-before-restart")
    assert claimed is not None
    test_run_id = str(queued["test_run_id"])
    run_dir = settings.runs_dir / test_run_id
    run_dir.mkdir(parents=True)
    engine = _FailingCleanupEngine(
        settings=settings,
        labels_by_container={CONTAINER_ID: {f"{SANDBOX_RUN_LABEL}={test_run_id}"}},
    )
    monkeypatch.setattr(worker_module, "_remove_tree", lambda path: path != run_dir)
    worker = AgentTestWorker(store=store, settings=settings, engine=engine, executor=_NoCallExecutor())  # type: ignore[arg-type]

    recovery = worker.recover()

    recovered = store.get_run(test_run_id)
    assert recovered is not None and recovered["status"] == "error"
    assert recovered["error"]["error_code"] == "AGENT_TEST_RECOVERY_CLEANUP_FAILED"
    receipt = _typed_receipt(recovered)
    assert receipt.cleanup.container_removed is False
    assert receipt.cleanup.label_residue_absent is False
    assert receipt.cleanup.temporary_paths_removed is False
    assert set(receipt.cleanup.error_codes) == {
        "CONTAINER_NOT_REMOVED",
        "CONTAINER_LABEL_RESIDUE",
        "CONTAINER_CLEANUP_ERROR",
        "TEMPORARY_PATHS_NOT_REMOVED",
    }
    assert recovery["safe_to_consume"] is False


def test_worker_recovery_refuses_consumption_when_residual_cleanup_is_unproven(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_id="worker-recovery")
    scope_label = f"{SANDBOX_SCOPE_LABEL}={worker_module.canonical_json_digest(RUNS_VOLUME_NAME)}"
    engine = _FailingCleanupEngine(
        settings=settings,
        labels_by_container={
            CONTAINER_ID: {f"{SANDBOX_KIND_LABEL}=true", scope_label},
        },
    )
    stale_path = settings.runs_dir / "stale-run"
    stale_path.mkdir(parents=True)
    stale_path.joinpath("partial").write_text("partial", encoding="utf-8")
    worker = AgentTestWorker(store=_store(settings), settings=settings, engine=engine, executor=_NoCallExecutor())  # type: ignore[arg-type]

    recovery = worker.recover()

    assert recovery["safe_to_consume"] is False
    assert recovery["residual_containers_removed"] is False
    assert recovery["residual_labels_empty"] is False
    assert recovery["stale_paths_removed"] is False
    assert stale_path.exists()


def test_worker_recovery_removes_terminal_stale_paths_before_consumption(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_id="worker-recovery")
    stale_path = settings.runs_dir / "finished-run"
    stale_path.mkdir(parents=True)
    stale_path.joinpath("partial").write_text("partial", encoding="utf-8")
    worker_lock = settings.runs_dir / ".worker.lock"
    worker_lock.write_text("held by worker", encoding="utf-8")
    worker = AgentTestWorker(store=_store(settings), settings=settings, engine=_FakeEngine(settings=settings), executor=_NoCallExecutor())  # type: ignore[arg-type]

    recovery = worker.recover()

    assert recovery["safe_to_consume"] is True
    assert recovery["stale_paths_removed"] is True
    assert not stale_path.exists()
    assert worker_lock.read_text(encoding="utf-8") == "held by worker"


def test_worker_rejects_observation_workspace_outside_its_authoritative_runs_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    worker = AgentTestWorker(
        store=_store(settings),
        settings=settings,
        engine=_FakeEngine(settings=settings),
        executor=_NoCallExecutor(),  # type: ignore[arg-type]
    )
    arbitrary_workspace = tmp_path / "other-root" / "atr-test" / "workspace"
    context = worker_module._ClaimContext(
        test_run_id="atr-test",
        agent_id=AGENT_ID,
        commit_sha=COMMIT_SHA,
        owner_worker_id=settings.worker_id,
        generation=1,
        run_dir=arbitrary_workspace.parent,
        workspace=arbitrary_workspace,
    )

    with pytest.raises(worker_module._WorkerRunError, match="named-volume subpath") as error:
        worker._execute_sandbox(context=context, agent_id=AGENT_ID)

    assert error.value.code == "AGENT_TEST_WORKSPACE_AUTHORITY_INVALID"


def test_worker_settings_read_only_the_explicit_agent_test_environment_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = {
        "AGENT_TEST_DATA_DIR",
        "AGENT_TEST_RUNS_DIR",
        "AGENT_TEST_SANDBOX_IMAGE",
        "AGENT_TEST_DOCKER_SOCKET",
        "AGENT_TEST_TIMEOUT_SECONDS",
        "AGENT_TEST_POLL_SECONDS",
        "AGENT_TEST_WORKER_ID",
    }

    class _AuditedEnvironment(dict[str, str]):
        def __init__(self, values: dict[str, str]) -> None:
            super().__init__(values)
            self.accessed: list[str] = []

        def get(self, key: str, default: str | None = None) -> str | None:
            if key not in allowed:
                raise AssertionError(f"worker read an environment key outside its allowlist: {key}")
            self.accessed.append(key)
            return super().get(key, default)

    environment = _AuditedEnvironment(
        {
            "AGENT_TEST_DATA_DIR": str(tmp_path / "data"),
            "AGENT_TEST_RUNS_DIR": str(tmp_path / "worker-runs"),
            "AGENT_TEST_SANDBOX_IMAGE": "agentgov-test-sandbox:dev",
            "AGENT_TEST_DOCKER_SOCKET": str(tmp_path / "docker.sock"),
            "AGENT_TEST_TIMEOUT_SECONDS": "45",
            "AGENT_TEST_POLL_SECONDS": "0.25",
            "AGENT_TEST_WORKER_ID": "worker-explicit",
            "MODEL_PROVIDER_API_KEY": "must-not-be-read",
        }
    )
    monkeypatch.setattr(worker_module.os, "environ", environment)
    monkeypatch.setattr(worker_module.socket, "gethostname", lambda: WORKER_CONTAINER_ID[:12])

    settings = AgentTestWorkerSettings.from_environment()

    assert set(environment.accessed) == allowed
    assert settings == AgentTestWorkerSettings(
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "worker-runs",
        sandbox_image="agentgov-test-sandbox:dev",
        docker_socket=tmp_path / "docker.sock",
        timeout_seconds=45,
        poll_seconds=0.25,
        worker_id="worker-explicit",
        container_id=WORKER_CONTAINER_ID[:12],
        mountinfo_path=Path("/proc/self/mountinfo"),
    )
    assert "AppSettings" not in worker_module.__dict__


def test_worker_settings_reject_run_storage_inside_shared_runtime_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with pytest.raises(ValueError, match="runs_dir must be outside data_dir"):
        AgentTestWorkerSettings(
            data_dir=data_dir,
            runs_dir=data_dir / ".agent-testing" / "runs",
            sandbox_image="agentgov-test-sandbox:dev",
            docker_socket=tmp_path / "docker.sock",
            timeout_seconds=30,
            poll_seconds=0.1,
            worker_id="worker-invalid",
            container_id=WORKER_CONTAINER_ID[:12],
        )


@pytest.mark.parametrize(
    ("mount_type", "mount_rw", "volume_name"),
    [
        ("bind", True, RUNS_VOLUME_NAME),
        ("volume", False, RUNS_VOLUME_NAME),
        ("volume", True, "../../host-path"),
    ],
)
def test_worker_rejects_runs_mount_without_writable_named_volume_authority(
    tmp_path: Path,
    mount_type: object,
    mount_rw: object,
    volume_name: str,
) -> None:
    settings = _settings(tmp_path)
    engine = _FakeEngine(
        settings=settings,
        runs_mount_type=mount_type,
        runs_mount_rw=mount_rw,
        runs_volume_name=volume_name,
    )

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=engine,
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("driver", "options", "scope", "inspected_name", "mountpoint"),
    [
        ("nfs", None, "local", RUNS_VOLUME_NAME, str(RUNS_VOLUME_MOUNTPOINT)),
        ("local", {"type": "none", "o": "bind", "device": str(DAEMON_DATA_ROOT)}, "local", RUNS_VOLUME_NAME, str(RUNS_VOLUME_MOUNTPOINT)),
        ("local", None, "global", RUNS_VOLUME_NAME, str(RUNS_VOLUME_MOUNTPOINT)),
        ("local", None, "local", "other-volume", str(RUNS_VOLUME_MOUNTPOINT)),
        ("local", None, "local", RUNS_VOLUME_NAME, "/var/lib/docker/volumes/other/_data"),
    ],
)
def test_worker_rejects_untrusted_volume_metadata(
    tmp_path: Path,
    driver: object,
    options: object,
    scope: object,
    inspected_name: object,
    mountpoint: object,
) -> None:
    settings = _settings(tmp_path)
    engine = _FakeEngine(
        settings=settings,
        volume_driver=driver,
        volume_options=options,
        volume_scope=scope,
        volume_inspected_name=inspected_name,
        volume_mountpoint=mountpoint,
    )

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=engine,
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


def test_worker_rejects_mount_evidence_from_a_non_running_container(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=_FakeEngine(settings=settings, running=False),
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


def test_worker_rejects_data_bind_alias_to_the_runs_volume_backing_tree(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_mountinfo(
        settings.mountinfo_path,
        data_destination=settings.data_dir,
        runs_destination=settings.runs_dir,
        data_root=RUNS_VOLUME_MOUNTPOINT,
        runs_root=RUNS_VOLUME_MOUNTPOINT,
    )
    engine = _FakeEngine(settings=settings, data_source=Path("/host/configured-data-symlink"))

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=engine,
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


def test_worker_rejects_symlinked_mount_view_before_consuming_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.runs_dir.rmdir()
    settings.runs_dir.symlink_to(settings.data_dir, target_is_directory=True)

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=_FakeEngine(settings=settings),
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


def test_worker_rejects_nested_mount_that_changes_the_hashed_runs_view(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    nested_destination = settings.runs_dir / "run-1" / "workspace"
    engine = _FakeEngine(
        settings=settings,
        extra_mounts=[
            {
                "Type": "bind",
                "Source": "/daemon/other-workspace",
                "Destination": str(nested_destination),
                "RW": True,
                "Propagation": "rprivate",
            }
        ],
    )

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=engine,
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )


def test_worker_rejects_inspect_response_for_a_different_container(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = _FakeEngine(settings=settings, inspected_id="2" * 64)

    with pytest.raises(AgentTestWorkerUnsafeState, match="mount authority could not be proven"):
        AgentTestWorker(
            store=_store(settings),
            settings=settings,
            engine=engine,
            executor=_NoCallExecutor(),  # type: ignore[arg-type]
        )
