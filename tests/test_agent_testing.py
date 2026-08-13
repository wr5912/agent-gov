from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent_testing.execution_contracts import (
    FIXED_PYTEST_COMMAND,
    FIXED_SANDBOX_ENV,
    P0_EXACT_COMMIT_LANE,
    RECEIPT_CONTRACT,
    AgentTestCleanupReceipt,
    AgentTestExecutionReceipt,
    AgentTestInvocationReceipt,
    AgentTestIsolationReceipt,
    AgentTestResultReceipt,
    AgentTestSandboxMountReceipt,
    AgentTestTargetReceipt,
    canonical_json_digest,
    sandbox_environment_digest,
)
from app.agent_testing.models import AgentTestRunModel, AgentWorkspaceImportRecordModel
from app.agent_testing.router import create_agent_testing_router
from app.agent_testing.service import AgentTestingError, AgentTestingService
from app.agent_testing.store import AgentTestingStore, AgentTestRunAlreadyActive, AgentTestRunClaimLost
from app.agent_testing.suite import inspect_agent_test_suite
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_suite(workspace: Path, *, nested: bool = False, invalid: bool = False) -> None:
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("README.md").write_text("# Agent tests\n", encoding="utf-8")
    tests_dir.joinpath("conftest.py").write_text("VALUE = 1\n", encoding="utf-8")
    source = "def test_agent():\n    assert True\n" if not invalid else "def test_agent(:\n"
    tests_dir.joinpath("test_agent.py").write_text(source, encoding="utf-8")
    if nested:
        nested_dir = tests_dir / "nested"
        nested_dir.mkdir()
        nested_dir.joinpath("test_nested.py").write_text("def test_nested():\n    assert True\n", encoding="utf-8")


def _testing_store(tmp_path: Path) -> AgentTestingStore:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with session_factory.begin() as db:
        if db.get(AgentRegistryModel, "agent-a") is None:
            db.add(
                AgentRegistryModel(
                    agent_id="agent-a",
                    name="Agent A",
                    category="business",
                    workspace_dir=str(tmp_path / "workspace"),
                    provision_state="ready",
                    provision_completed_token="test-agent-a-instance",
                )
            )
    return AgentTestingStore(session_factory)


def _valid_isolation() -> AgentTestIsolationReceipt:
    return AgentTestIsolationReceipt(
        user="65532:65532",
        network_mode="none",
        network_disabled=True,
        pid_mode="private",
        ipc_mode="private",
        uts_mode="private",
        readonly_rootfs=True,
        cap_drop=("ALL",),
        security_opt=("no-new-privileges",),
        privileged=False,
        devices=(),
        mounts=(
            AgentTestSandboxMountReceipt(
                target="/workspace",
                read_only=True,
                mount_type="volume",
                source_scope="run_workspace_subpath",
            ),
        ),
        pids_limit=256,
        memory_bytes=536870912,
        memory_swap_bytes=536870912,
        nano_cpus=1000000000,
        tmpfs_targets=("/output", "/tmp"),
        tmpfs_size_bytes=67108864,
        tmpfs_noexec=True,
        tmpfs_nosuid=True,
        tmpfs_nodev=True,
        shm_size_bytes=16777216,
        ports_published=False,
        auto_remove=False,
        restart_policy="no",
        log_driver="local",
        log_max_bytes=1048576,
        log_max_files=1,
        log_compression=False,
        docker_socket_mounted=False,
    )


def test_sandbox_mount_receipt_contains_only_proven_named_volume_evidence() -> None:
    receipt = AgentTestSandboxMountReceipt(
        target="/workspace",
        read_only=True,
        mount_type="volume",
        source_scope="run_workspace_subpath",
    )

    assert receipt.model_dump(mode="json") == {
        "target": "/workspace",
        "read_only": True,
        "mount_type": "volume",
        "source_scope": "run_workspace_subpath",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"propagation": "rprivate"},
        {"read_only": False},
        {"mount_type": "bind"},
        {"source_scope": "entire_volume"},
    ],
)
def test_sandbox_mount_receipt_rejects_legacy_or_unproven_evidence(mutation: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "target": "/workspace",
        "read_only": True,
        "mount_type": "volume",
        "source_scope": "run_workspace_subpath",
    }
    payload.update(mutation)

    with pytest.raises(ValueError):
        AgentTestSandboxMountReceipt.model_validate(payload)


def _valid_receipt(run: dict, *, report: dict, stdout: str, stderr: str) -> dict:
    receipt = AgentTestExecutionReceipt(
        contract=RECEIPT_CONTRACT,
        lane=P0_EXACT_COMMIT_LANE,
        assurance_level="execution_provenance",
        test_run_id=str(run["test_run_id"]),
        worker_id="worker-test",
        container_id="a" * 64,
        target=AgentTestTargetReceipt(
            agent_id=str(run["agent_id"]),
            commit_sha=str(run["commit_sha"]),
            tree_sha=str(run["source_tree_sha"]),
            source_digest=str(run["source_digest"]),
            pre_source_digest=str(run["source_digest"]),
            post_source_digest=str(run["source_digest"]),
            source_observation="stable",
            suite_digest=str(run["suite_digest"]),
        ),
        invocation=AgentTestInvocationReceipt(
            image_id=f"sha256:{'d' * 64}",
            argv=FIXED_PYTEST_COMMAND,
            environment_keys=tuple(sorted(FIXED_SANDBOX_ENV)),
            environment_digest=sandbox_environment_digest(),
            working_directory="/workspace",
        ),
        isolation=_valid_isolation(),
        result=AgentTestResultReceipt(
            status="passed",
            exit_code=0,
            duration_ms=10,
            workspace_report_authority="agent_owned_unverified",
            workspace_report_digest=canonical_json_digest(report),
            stdout_digest=canonical_json_digest(stdout),
            stderr_digest=canonical_json_digest(stderr),
        ),
        cleanup=AgentTestCleanupReceipt(
            container_removed=True,
            label_residue_absent=True,
            temporary_paths_removed=True,
            error_codes=(),
        ),
    ).with_digest()
    return receipt.model_dump(mode="json")


def _finish_passed(store: AgentTestingStore, run: dict) -> dict:
    claimed = store.claim_run(str(run["test_run_id"]), worker_id="worker-test")
    assert claimed is not None
    store.bind_container(
        str(run["test_run_id"]),
        worker_id="worker-test",
        claim_generation=int(claimed["_claim_generation"]),
        container_id="a" * 64,
    )
    report = {"exit_code": 0, "items": [{"nodeid": "tests/test_agent.py::test_agent", "outcome": "passed", "phase": "call"}]}
    stdout = "1 passed"
    return store.finish_run(
        str(run["test_run_id"]),
        worker_id="worker-test",
        claim_generation=int(claimed["_claim_generation"]),
        status="passed",
        report=report,
        receipt=_valid_receipt(run, report=report, stdout=stdout, stderr=""),
        items=report["items"],
        stdout=stdout,
        stderr="",
    )


def _passed_run(store: AgentTestingStore, *, agent_id: str, commit_sha: str) -> dict:
    created = store.create_run(
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id="agc-test",
        source="release_check",
        command=list(FIXED_PYTEST_COMMAND),
        suite={"test_files": ["tests/test_agent.py"]},
        suite_digest="c" * 64,
        source_digest="a" * 64,
        source_tree_sha="b" * 40,
    )
    return _finish_passed(store, created)


def test_suite_inspection_treats_workspace_tests_as_versioned_source_of_truth(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    missing = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)
    assert missing.runnable is False
    assert missing.agent_id == "agent-a"
    assert missing.commit_sha == "a" * 40
    assert [item.code for item in missing.diagnostics] == ["AGENT_TESTS_DIRECTORY_MISSING"]

    _write_suite(workspace)
    workspace.joinpath("agent.yaml").write_text("agent:\n  id: ignored-id\n", encoding="utf-8")
    valid = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)
    assert valid.runnable is True
    assert valid.test_files == ["tests/test_agent.py"]
    assert valid.suite_digest
    assert valid.diagnostics == []

    workspace.joinpath("tests", "test_agent.py").write_text("def test_agent():\n    assert 2 == 2\n", encoding="utf-8")
    changed = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="b" * 40)
    assert changed.suite_digest != valid.suite_digest


@pytest.mark.parametrize(
    ("nested", "invalid", "code"),
    [
        (True, False, "AGENT_TEST_LAYOUT_NESTED"),
        (False, True, "AGENT_TEST_PYTHON_INVALID"),
    ],
)
def test_suite_inspection_rejects_non_flat_or_unparseable_python(
    tmp_path: Path,
    nested: bool,
    invalid: bool,
    code: str,
) -> None:
    workspace = tmp_path / "workspace"
    _write_suite(workspace, nested=nested, invalid=invalid)

    suite = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)

    assert suite.runnable is False
    assert code in {item.code for item in suite.diagnostics}


def test_suite_inspection_blocks_legacy_generated_weak_assertions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_suite(workspace)
    workspace.joinpath("tests", "test_agent.py").write_text(
        "# Generated from a confirmed AgentGov regression test design.\n\n"
        "def test_generated(agent):\n"
        "    expected_behavior = 'answer'\n"
        "    checkpoints = ['non-empty']\n"
        "    result = agent.run('prompt')\n"
        "    assert result.text.strip(), expected_behavior\n"
        "    assert all(checkpoint.strip() for checkpoint in checkpoints)\n",
        encoding="utf-8",
    )

    suite = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)

    assert suite.runnable is False
    assert "AGENT_TEST_LEGACY_GENERATED_ASSERTION" in {item.code for item in suite.diagnostics}


def test_test_run_store_uses_independent_lifecycle_and_exact_commit_gate(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    passed = _passed_run(store, agent_id="agent-a", commit_sha="a" * 40)

    assert passed["status"] == "passed"
    assert passed["items"][0]["nodeid"] == "tests/test_agent.py::test_agent"
    assert store.latest_passed_for_commit(agent_id="agent-a", commit_sha="a" * 40)["test_run_id"] == passed["test_run_id"]
    assert store.latest_passed_for_commit(agent_id="agent-a", commit_sha="b" * 40) is None
    assert store.latest_passed_for_commit(agent_id="agent-b", commit_sha="a" * 40) is None


def test_test_run_store_rejects_duplicate_active_exact_target(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    first = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id="agc-test",
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )

    with pytest.raises(AgentTestRunAlreadyActive) as duplicate:
        store.create_run(
            agent_id="agent-a",
            commit_sha="a" * 40,
            change_set_id="agc-test",
            source="release_check",
            command=FIXED_PYTEST_COMMAND,
            suite={},
            suite_digest=None,
        )

    assert duplicate.value.test_run_id == first["test_run_id"]


def test_test_run_cancel_and_worker_claim_fencing_are_explicit(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    queued = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    cancelled = store.request_cancel(str(queued["test_run_id"]))
    assert cancelled["status"] == "queued"
    assert cancelled["cancel_requested"] is True
    assert cancelled["receipt"] is None

    running = store.create_run(
        agent_id="agent-a",
        commit_sha="b" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    claimed = store.claim_run(str(running["test_run_id"]), worker_id="worker-a")
    assert claimed is not None
    assert store.claim_run(str(running["test_run_id"]), worker_id="worker-b") is None
    with pytest.raises(AgentTestRunClaimLost):
        store.finish_run(
            str(running["test_run_id"]),
            worker_id="worker-b",
            claim_generation=int(claimed["_claim_generation"]),
            status="error",
            report={},
            receipt=None,
            items=[],
            stdout="",
            stderr="",
        )
    assert store.running_runs()[0]["_worker_id"] == "worker-a"


def test_service_requires_exact_run_commit_and_pins_session_commit_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# test Agent\n", encoding="utf-8")
    _write_suite(workspace)
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    pinned_commit = str(git_store.current_commit_sha())
    captured: dict = {}

    async def run_candidate(request, **kwargs):
        captured.update(kwargs)
        captured["request"] = request
        return ChatResponse(run_id="run-test", session_id="session-test", answer="ok")

    service = AgentTestingService(
        store=_testing_store(tmp_path),
        store_for=lambda _agent_id: git_store,
        agent_exists=lambda agent_id: agent_id == "agent-a",
        get_change_set=lambda change_set_id: (
            {"change_set_id": change_set_id, "agent_id": "agent-a", "candidate_commit_sha": pinned_commit} if change_set_id == "agc-test" else None
        ),
        run_candidate=run_candidate,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        with pytest.raises(AgentTestingError) as missing_commit:
            service.create_run(
                agent_id="agent-a",
                commit_sha=None,
                change_set_id=None,
                source="manual",
            )
        assert missing_commit.value.error_code == "AGENT_TEST_COMMIT_REQUIRED"

        run = service.create_run(
            agent_id="agent-a",
            commit_sha=pinned_commit,
            change_set_id="agc-test",
            source="release_check",
        )
        assert run["commit_sha"] == pinned_commit
        assert run["command"] == list(FIXED_PYTEST_COMMAND)
        assert run["status"] == "queued"
        assert run["source_digest"]
        assert run["source_tree_sha"]

        with pytest.raises(AgentTestingError) as duplicate:
            service.create_run(
                agent_id="agent-a",
                commit_sha=pinned_commit,
                change_set_id="agc-test",
                source="release_check",
            )
        assert duplicate.value.status_code == 409
        assert duplicate.value.error_code == "AGENT_TEST_RUN_ALREADY_ACTIVE"

        session = service.create_session(agent_id="agent-a", commit_sha=None, change_set_id="agc-test")
        response = asyncio.run(service.invoke(str(session["test_session_id"]), message="verify", metadata={"case": "one"}))
        assert response.answer == "ok"
        assert captured["candidate_commit_sha"] == pinned_commit
        assert Path(captured["worktree_path"]).joinpath("CLAUDE.md").is_file()
        assert captured["request"].metadata["tested_commit_sha"] == pinned_commit
        service.delete_session(str(session["test_session_id"]))
    finally:
        service.close()


def test_asset_list_isolates_one_agent_inspection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stores: dict[str, GitAgentVersionStore] = {}
    for agent_id in ("agent-a", "agent-b"):
        agent_root = tmp_path / agent_id
        workspace = agent_root / "workspace"
        workspace.mkdir(parents=True)
        workspace.joinpath("CLAUDE.md").write_text(f"# {agent_id}\n", encoding="utf-8")
        _write_suite(workspace)
        git_store = GitAgentVersionStore(
            repository_dir=workspace,
            worktrees_dir=agent_root / "version" / "worktrees",
            releases_dir=agent_root / "version" / "releases",
        )
        git_store.ensure_bootstrap()
        stores[agent_id] = git_store

    async def unused_run_candidate(*_args, **_kwargs):
        raise AssertionError("must not run")

    service = AgentTestingService(
        store=_testing_store(tmp_path),
        store_for=stores.__getitem__,
        agent_exists=lambda agent_id: agent_id in stores,
        get_change_set=lambda _change_set_id: None,
        run_candidate=unused_run_candidate,
        artifacts_dir=tmp_path / "artifacts",
        list_agents=lambda: (
            SimpleNamespace(agent_id="agent-a", name="Agent A", status="active"),
            SimpleNamespace(agent_id="agent-b", name="Agent B", status="active"),
        ),
    )
    original_inspect = service.inspect_suite

    def inspect_with_one_failure(agent_id: str, *, commit_sha: str | None = None):
        if agent_id == "agent-b":
            raise AgentTestingError(422, "AGENT_SOURCE_SENSITIVE_PATH", "private detail must not escape")
        return original_inspect(agent_id, commit_sha=commit_sha)

    monkeypatch.setattr(service, "inspect_suite", inspect_with_one_failure)
    try:
        assets = service.list_test_assets()
    finally:
        service.close()

    assert [item["agent_id"] for item in assets] == ["agent-a", "agent-b"]
    assert assets[0]["suite"]["test_file_count"] == 1
    unavailable = assets[1]["suite"]
    assert unavailable["commit_sha"] == stores["agent-b"].current_commit_sha()
    assert {item["code"] for item in (unavailable["diagnostics"] or [])} == {
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        "AGENT_SOURCE_SENSITIVE_PATH",
    }
    assert "private detail must not escape" not in str(unavailable)


def _publication_gate_harness(
    tmp_path: Path,
) -> tuple[AgentTestingService, AgentTestingStore, Path, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# test Agent\n", encoding="utf-8")
    _write_suite(workspace)
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    commit_sha = str(git_store.current_commit_sha())
    store = _testing_store(tmp_path)

    async def unused_run_candidate(*_args, **_kwargs):
        raise AssertionError("must not run")

    return (
        AgentTestingService(
            store=store,
            store_for=lambda _agent_id: git_store,
            agent_exists=lambda agent_id: agent_id == "agent-a",
            get_change_set=lambda _change_set_id: None,
            run_candidate=unused_run_candidate,
            artifacts_dir=tmp_path / "artifacts",
        ),
        store,
        workspace,
        commit_sha,
    )


def test_service_publication_gate_requires_intact_receipt_and_raw_exact_source(tmp_path: Path) -> None:
    service, store, _workspace, commit_sha = _publication_gate_harness(tmp_path)
    try:
        tampered = service.create_run(
            agent_id="agent-a",
            commit_sha=commit_sha,
            change_set_id=None,
            source="manual",
        )
        claimed = store.claim_run(str(tampered["test_run_id"]), worker_id="worker-test")
        assert claimed is not None
        report = {"exit_code": 0, "items": []}
        receipt = _valid_receipt(tampered, report=report, stdout="", stderr="")
        receipt["receipt_digest"] = "0" * 64
        store.finish_run(
            str(tampered["test_run_id"]),
            worker_id="worker-test",
            claim_generation=int(claimed["_claim_generation"]),
            status="passed",
            report=report,
            receipt=receipt,
            items=[],
            stdout="",
            stderr="",
        )
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None

        exact = service.create_run(
            agent_id="agent-a",
            commit_sha=commit_sha,
            change_set_id=None,
            source="manual",
        )
        finished = _finish_passed(store, exact)
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha)["test_run_id"] == finished["test_run_id"]

        with store.Session.begin() as db:
            row = db.get(AgentTestRunModel, str(finished["test_run_id"]))
            assert row is not None
            row.container_id = "e" * 64
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None
        with store.Session.begin() as db:
            row = db.get(AgentTestRunModel, str(finished["test_run_id"]))
            assert row is not None
            row.container_id = "a" * 64
            row.worker_id = "different-worker"
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None
        with store.Session.begin() as db:
            row = db.get(AgentTestRunModel, str(finished["test_run_id"]))
            assert row is not None
            row.worker_id = "worker-test"

        with store.Session.begin() as db:
            row = db.get(AgentTestRunModel, str(finished["test_run_id"]))
            assert row is not None
            damaged = dict(row.receipt_json or {})
            damaged["receipt_digest"] = "f" * 64
            row.receipt_json = damaged
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None
    finally:
        service.close()


def test_service_publication_gate_reads_tested_commit_when_live_workspace_is_dirty(tmp_path: Path) -> None:
    service, store, workspace, commit_sha = _publication_gate_harness(tmp_path)
    try:
        exact = service.create_run(
            agent_id="agent-a",
            commit_sha=commit_sha,
            change_set_id=None,
            source="manual",
        )
        finished = _finish_passed(store, exact)
        workspace.joinpath("tests", "test_agent.py").write_text(
            "# Generated from a confirmed AgentGov regression test design.\n\n"
            "def test_generated(agent):\n"
            "    result = agent.run('prompt')\n"
            "    assert result.text.strip()\n",
            encoding="utf-8",
        )
        eligible = service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha)
        assert eligible is not None
        assert eligible["test_run_id"] == finished["test_run_id"]
    finally:
        service.close()


def test_message_endpoint_projects_runtime_chat_response() -> None:
    class FakeService:
        async def invoke(self, test_session_id: str, *, message: str, metadata: dict) -> ChatResponse:
            assert test_session_id == "ats-test"
            assert message == "verify"
            assert metadata == {"case": "one"}
            return ChatResponse(run_id="run-test", session_id="session-test", answer="ok", stop_reason="end_turn")

    app = FastAPI()
    app.include_router(create_agent_testing_router(service=FakeService(), require_api_key=lambda: None))  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/api/agent-test-sessions/ats-test/messages",
            json={"message": "verify", "metadata": {"case": "one"}},
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "ok"
    assert response.json()["stop_reason"] == "end_turn"


def test_public_run_routes_keep_target_identity_backend_owned() -> None:
    class FakeService:
        def create_run(self, **kwargs):
            assert kwargs == {
                "agent_id": "agent-a",
                "commit_sha": "a" * 40,
                "change_set_id": None,
                "source": "manual",
            }
            return _run_response(agent_id="agent-a", commit_sha="a" * 40, change_set_id=None)

        def create_change_set_run(self, change_set_id: str):
            assert change_set_id == "agc-test"
            return _run_response(agent_id="agent-a", commit_sha="b" * 40, change_set_id=change_set_id)

    app = FastAPI()
    app.include_router(create_agent_testing_router(service=FakeService(), require_api_key=lambda: None))  # type: ignore[arg-type]
    with TestClient(app) as client:
        manual = client.post("/api/agent-test-runs", json={"agent_id": "agent-a", "commit_sha": "a" * 40})
        change_set = client.post("/api/agent-change-sets/agc-test/test-runs")
        hostile = client.post(
            "/api/agent-test-runs",
            json={"agent_id": "agent-a", "commit_sha": "a" * 40, "status": "passed", "command": ["true"]},
        )

    assert manual.status_code == 202
    assert change_set.status_code == 202
    assert hostile.status_code == 422


def test_service_rejects_missing_suite_and_mismatched_change_set(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# test Agent\n", encoding="utf-8")
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    commit_sha = str(git_store.current_commit_sha())

    async def unused_run_candidate(*_args, **_kwargs):
        raise AssertionError("must not run")

    service = AgentTestingService(
        store=_testing_store(tmp_path),
        store_for=lambda _agent_id: git_store,
        agent_exists=lambda _agent_id: True,
        get_change_set=lambda change_set_id: {
            "change_set_id": change_set_id,
            "agent_id": "other-agent",
            "candidate_commit_sha": commit_sha,
        },
        run_candidate=unused_run_candidate,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        with pytest.raises(AgentTestingError, match="tests/") as missing:
            service.create_run(
                agent_id="agent-a",
                commit_sha=commit_sha,
                change_set_id=None,
                source="manual",
            )
        assert missing.value.error_code == "AGENT_TEST_SUITE_NOT_RUNNABLE"

        with pytest.raises(AgentTestingError, match="不匹配") as mismatch:
            service.create_session(agent_id="agent-a", commit_sha=commit_sha, change_set_id="agc-other")
        assert mismatch.value.error_code == "CHANGE_SET_COMMIT_MISMATCH"

        with pytest.raises(AgentTestingError) as unavailable:
            service.create_session(agent_id="agent-a", commit_sha="f" * 40, change_set_id=None)
        assert unavailable.value.status_code == 409
        assert unavailable.value.error_code == "AGENT_COMMIT_NOT_FOUND"
    finally:
        service.close()


def test_failed_workspace_import_is_persisted_as_audit_record(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    import_id = store.record_import_failure(
        agent_id="agent-a",
        action="overwrite",
        package_sha256="a" * 64,
        tree_sha256=None,
        error={"error_code": "WORKSPACE_IMPORT_CONFLICT", "detail": "conflict"},
    )

    with store.Session() as db:
        record = db.get(AgentWorkspaceImportRecordModel, import_id)
        assert record is not None
        assert record.status == "failed"
        assert record.agent_id == "agent-a"
        assert record.error_json["error_code"] == "WORKSPACE_IMPORT_CONFLICT"


def _run_response(*, agent_id: str, commit_sha: str, change_set_id: str | None) -> dict:
    return {
        "test_run_id": "atr-test",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "change_set_id": change_set_id,
        "source": "release_check" if change_set_id else "manual",
        "status": "queued",
        "created_at": "2026-07-18T00:00:00Z",
    }


def test_import_receipt_warns_for_missing_tests_and_persists_target_agent_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# imported Agent\n", encoding="utf-8")
    workspace.joinpath("agent.yaml").write_text("agent:\n  id: url-agent-id\n", encoding="utf-8")
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    commit_sha = str(git_store.current_commit_sha())
    store = _testing_store(tmp_path)

    async def unused_run_candidate(*_args, **_kwargs):
        raise AssertionError("must not run")

    service = AgentTestingService(
        store=store,
        store_for=lambda agent_id: git_store if agent_id == "url-agent-id" else (_ for _ in ()).throw(AssertionError(agent_id)),
        agent_exists=lambda agent_id: agent_id == "url-agent-id",
        get_change_set=lambda _change_set_id: None,
        run_candidate=unused_run_candidate,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        import_id, suite = service.record_import(
            agent_id="url-agent-id",
            action="created",
            package_sha256="a" * 64,
            tree_sha256="b" * 64,
            commit_sha=commit_sha,
        )
        assert suite.runnable is False
        assert {item.code for item in suite.diagnostics} == {"AGENT_TESTS_DIRECTORY_MISSING"}
        with store.Session() as db:
            record = db.get(AgentWorkspaceImportRecordModel, import_id)
            assert record is not None
            assert record.agent_id == "url-agent-id"
            assert record.commit_sha == commit_sha
            assert record.suite_status == "warning"
            assert {item["code"] for item in record.diagnostics_json} == {"AGENT_TESTS_DIRECTORY_MISSING"}
    finally:
        service.close()
