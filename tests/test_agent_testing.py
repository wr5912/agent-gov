from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from app.agent_testing import runner as runner_module
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.agent_testing.router import create_agent_testing_router
from app.agent_testing.runner import FIXED_PYTEST_COMMAND, AgentTestRunner
from app.agent_testing.service import AgentTestingError, AgentTestingService
from app.agent_testing.store import AgentTestingStore, AgentTestRunAlreadyActive
from app.agent_testing.suite import inspect_agent_test_suite
from app.runtime.agent_git_store import GitAgentVersionStore
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
    return AgentTestingStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def _passed_run(store: AgentTestingStore, *, agent_id: str, commit_sha: str) -> dict:
    created = store.create_run(
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id="agc-test",
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite={"test_files": ["tests/test_agent.py"]},
        suite_digest="suite-digest",
    )
    assert store.claim_run(str(created["test_run_id"])) is not None
    return store.finish_run(
        str(created["test_run_id"]),
        status="passed",
        report={"exit_code": 0},
        items=[{"nodeid": "tests/test_agent.py::test_agent", "outcome": "passed", "phase": "call"}],
        stdout="1 passed",
        stderr="",
    )


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
    assert "AGENT_MANIFEST_ID_IGNORED" in {item.code for item in valid.diagnostics}

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


def test_test_run_cancel_and_restart_recovery_are_explicit(tmp_path: Path) -> None:
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
    assert cancelled["status"] == "cancelled"

    running = store.create_run(
        agent_id="agent-a",
        commit_sha="b" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    assert store.claim_run(str(running["test_run_id"])) is not None
    assert store.reconcile_interrupted_runs() == [running["test_run_id"]]
    recovered = store.get_run(str(running["test_run_id"]))
    assert recovered["status"] == "interrupted"
    assert recovered["error"]["error_code"] == "AGENT_TEST_RUN_INTERRUPTED"


def test_runner_persists_error_when_agent_repository_resolution_fails(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    run = store.create_run(
        agent_id="missing-agent",
        commit_sha="a" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    runner = AgentTestRunner(
        store=store,
        store_for=lambda _agent_id: (_ for _ in ()).throw(RuntimeError("repository unavailable")),
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        timeout_seconds=30,
    )
    try:
        runner.enqueue(str(run["test_run_id"]))
        deadline = time.monotonic() + 5
        current = store.get_run(str(run["test_run_id"]))
        while current and current["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            current = store.get_run(str(run["test_run_id"]))
        assert current is not None
        assert current["status"] == "error"
        assert current["error"]["error_code"] == "AGENT_TEST_RUN_ERROR"
        assert "repository unavailable" in current["error"]["message"]
    finally:
        runner.close()


def test_runner_terminates_pytest_process_group_at_platform_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# timeout Agent\n", encoding="utf-8")
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    store = _testing_store(tmp_path)
    run = store.create_run(
        agent_id="agent-a",
        commit_sha=str(git_store.current_commit_sha()),
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    monkeypatch.setattr(
        runner_module,
        "FIXED_PYTEST_COMMAND",
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    runner = AgentTestRunner(
        store=store,
        store_for=lambda _agent_id: git_store,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        timeout_seconds=1,
    )
    try:
        runner.enqueue(str(run["test_run_id"]))
        deadline = time.monotonic() + 8
        current = store.get_run(str(run["test_run_id"]))
        while current and current["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            current = store.get_run(str(run["test_run_id"]))
        assert current is not None
        assert current["status"] == "error"
        assert current["error"]["error_code"] == "AGENT_TEST_RUN_TIMEOUT"
        assert current["duration_seconds"] >= 1
    finally:
        runner.close()


def test_service_pins_omitted_commit_once_and_invokes_that_checkout(tmp_path: Path) -> None:
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
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        run_timeout_seconds=30,
    )
    enqueued: list[str] = []
    service.runner.enqueue = enqueued.append  # type: ignore[method-assign]
    try:
        run = service.create_run(
            agent_id="agent-a",
            commit_sha=None,
            change_set_id="agc-test",
            source="release_check",
        )
        assert run["commit_sha"] == pinned_commit
        assert run["command"] == FIXED_PYTEST_COMMAND
        assert enqueued == [run["test_run_id"]]

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


def test_service_publication_gate_requires_current_runnable_suite_digest(tmp_path: Path) -> None:
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

    service = AgentTestingService(
        store=store,
        store_for=lambda _agent_id: git_store,
        agent_exists=lambda agent_id: agent_id == "agent-a",
        get_change_set=lambda _change_set_id: None,
        run_candidate=unused_run_candidate,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        run_timeout_seconds=30,
    )
    try:
        suite = service.inspect_suite("agent-a", commit_sha=commit_sha)
        stale = store.create_run(
            agent_id="agent-a",
            commit_sha=commit_sha,
            change_set_id=None,
            source="manual",
            command=FIXED_PYTEST_COMMAND,
            suite=suite.model_dump(mode="json"),
            suite_digest="stale-digest",
        )
        assert store.claim_run(str(stale["test_run_id"])) is not None
        store.finish_run(str(stale["test_run_id"]), status="passed", report={}, items=[], stdout="", stderr="")
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None

        exact = store.create_run(
            agent_id="agent-a",
            commit_sha=commit_sha,
            change_set_id=None,
            source="manual",
            command=FIXED_PYTEST_COMMAND,
            suite=suite.model_dump(mode="json"),
            suite_digest=suite.suite_digest,
        )
        assert store.claim_run(str(exact["test_run_id"])) is not None
        finished = store.finish_run(str(exact["test_run_id"]), status="passed", report={}, items=[], stdout="", stderr="")
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha)["test_run_id"] == finished["test_run_id"]

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
        assert service.latest_passed_for_commit(agent_id="agent-a", commit_sha=commit_sha) is None
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
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        run_timeout_seconds=30,
    )
    try:
        with pytest.raises(AgentTestingError, match="tests/") as missing:
            service.create_run(
                agent_id="agent-a",
                commit_sha=None,
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


def test_import_receipt_warns_for_missing_tests_and_persists_url_agent_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# imported Agent\n", encoding="utf-8")
    workspace.joinpath("agent.yaml").write_text("agent:\n  id: manifest-id-is-not-authoritative\n", encoding="utf-8")
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
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        run_timeout_seconds=30,
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
        assert {item.code for item in suite.diagnostics} == {
            "AGENT_MANIFEST_ID_IGNORED",
            "AGENT_TESTS_DIRECTORY_MISSING",
        }
        with store.Session() as db:
            record = db.get(AgentWorkspaceImportRecordModel, import_id)
            assert record is not None
            assert record.agent_id == "url-agent-id"
            assert record.commit_sha == commit_sha
            assert {item["code"] for item in record.warnings_json} == {
                "AGENT_MANIFEST_ID_IGNORED",
                "AGENT_TESTS_DIRECTORY_MISSING",
            }
    finally:
        service.close()
