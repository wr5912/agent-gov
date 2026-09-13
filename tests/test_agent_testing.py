from __future__ import annotations

import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.agent_testing.runner import FIXED_PYTEST_COMMAND, AgentTestRunner, _run_paths
from app.agent_testing.schemas import AgentTestMessageResponse, AgentTestRunCreateRequest
from app.agent_testing.store import AgentTestingStore, AgentTestRunAlreadyActive
from app.agent_testing.suite import inspect_agent_test_suite
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceService
from pydantic import ValidationError


def _write_suite(workspace: Path, *, nested: bool = False, invalid: bool = False) -> None:
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("README.md").write_text("# Agent tests\n", encoding="utf-8")
    source = "def test_agent():\n    assert True\n" if not invalid else "def test_agent(:\n"
    tests_dir.joinpath("test_agent.py").write_text(source, encoding="utf-8")
    if nested:
        nested_dir = tests_dir / "nested"
        nested_dir.mkdir()
        nested_dir.joinpath("test_nested.py").write_text("def test_nested():\n    assert True\n", encoding="utf-8")


def _testing_store(tmp_path: Path) -> AgentTestingStore:
    return AgentTestingStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def _version_governance(tmp_path: Path, *, agent_id: str) -> tuple[GitAgentVersionStore, AgentGovernanceService]:
    layout = business_agent_layout(tmp_path / "data", agent_id)
    git_store = GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )
    governance = AgentGovernanceService(
        feedback_store=FeedbackStore(data_dir=tmp_path / "data"),
        agent_version_store=git_store,
        runtime_mode="local-debug",
    )
    return git_store, governance


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


def test_api_image_installs_testkit_with_executable_platform_pytest_runner(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    testkit_project = tomllib.loads((repo_root / "packages/agentgov-testkit/pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (repo_root / "docker/Dockerfile").read_text(encoding="utf-8")

    assert FIXED_PYTEST_COMMAND[1:4] == ["-I", "-m", "pytest"]
    assert "pytest>=9,<10" in testkit_project["project"]["dependencies"]
    assert "COPY packages/agentgov-testkit /app/packages/agentgov-testkit" in dockerfile
    install_instruction = next(block for block in dockerfile.split("\n\n") if "uv pip install" in block)
    assert "/app/packages/agentgov-testkit" in install_instruction

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("test_dependency.py").write_text("def test_real_pytest(): assert True\n", encoding="utf-8")
    result = subprocess.run(FIXED_PYTEST_COMMAND, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


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


@pytest.mark.parametrize(
    ("path", "source", "code"),
    [
        ("conftest.py", "def pytest_configure(config): pass\n", "AGENT_TEST_CONFTEST_FORBIDDEN"),
        ("test_agent.py", "from unittest.mock import patch\ndef test_agent(): assert patch\n", "AGENT_TEST_DOUBLE_FORBIDDEN"),
        (
            "test_agent.py",
            "def test_agent(agent):\n    result = agent.run('real')\n    result.raw['answer'] = 'fabricated'\n    assert result.raw\n",
            "AGENT_TEST_DOUBLE_FORBIDDEN",
        ),
        (
            "test_agent.py",
            "pytest_plugins = ['untrusted_plugin']\ndef test_agent(): assert True\n",
            "AGENT_TEST_DOUBLE_FORBIDDEN",
        ),
        (
            "test_agent.py",
            "import pytest\n@pytest.mark.xfail\ndef test_agent(): assert False\n",
            "AGENT_TEST_DOUBLE_FORBIDDEN",
        ),
        (
            "test_agent.py",
            "from pytest import skip as avoid\ndef test_agent(): avoid('no real run')\n",
            "AGENT_TEST_DOUBLE_FORBIDDEN",
        ),
        (
            "test_agent.py",
            "import os as process\ndef test_agent(): process._exit(0)\n",
            "AGENT_TEST_DOUBLE_FORBIDDEN",
        ),
    ],
)
def test_suite_inspection_rejects_workspace_test_double_execution_controls(
    tmp_path: Path,
    path: str,
    source: str,
    code: str,
) -> None:
    workspace = tmp_path / "workspace"
    _write_suite(workspace)
    workspace.joinpath("tests", path).write_text(source, encoding="utf-8")

    suite = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)

    assert suite.runnable is False
    assert code in {item.code for item in suite.diagnostics}


def test_suite_inspection_rejects_test_asset_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_suite(workspace)
    outside = tmp_path / "outside.py"
    outside.write_text("def test_outside(): assert True\n", encoding="utf-8")
    workspace.joinpath("tests", "test_link.py").symlink_to(outside)

    suite = inspect_agent_test_suite(workspace, agent_id="agent-a", commit_sha="a" * 40)

    assert suite.runnable is False
    assert "AGENT_TEST_PATH_SYMLINK" in {item.code for item in suite.diagnostics}


def test_test_run_store_uses_independent_lifecycle_and_exact_commit_gate(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    passed = _passed_run(store, agent_id="agent-a", commit_sha="a" * 40)

    assert passed["status"] == "passed"
    assert passed["items"][0]["nodeid"] == "tests/test_agent.py::test_agent"
    assert store.latest_passed_for_commit(agent_id="agent-a", commit_sha="a" * 40)["test_run_id"] == passed["test_run_id"]
    assert store.latest_passed_for_commit(agent_id="agent-a", commit_sha="b" * 40) is None
    assert store.latest_passed_for_commit(agent_id="agent-b", commit_sha="a" * 40) is None


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"nodeid": "tests/test_agent.py::test_agent", "outcome": "skipped", "phase": "call"}],
        [{"nodeid": "tests/test_agent.py::test_agent", "outcome": "xfailed", "phase": "call"}],
    ],
)
def test_test_run_store_rejects_empty_or_nonpassing_release_evidence(tmp_path: Path, items: list[dict[str, str]]) -> None:
    store = _testing_store(tmp_path)
    created = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id="agc-test",
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite={"test_files": ["tests/test_agent.py"]},
        suite_digest="suite-digest",
    )
    test_run_id = str(created["test_run_id"])
    assert store.claim_run(test_run_id) is not None
    store.finish_run(
        test_run_id,
        status="passed",
        report={"exit_code": 0},
        items=items,
        stdout="pytest exited successfully",
        stderr="",
    )

    assert store.latest_passed_for_commit(agent_id="agent-a", commit_sha="a" * 40) is None


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


def test_runner_persists_error_when_real_git_commit_is_missing(tmp_path: Path) -> None:
    git_store, governance = _version_governance(tmp_path, agent_id="agent-a")
    workspace = git_store.repository_dir
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("CLAUDE.md").write_text("# real repository\n", encoding="utf-8")
    git_store.ensure_bootstrap()
    store = _testing_store(tmp_path)
    run = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={},
        suite_digest=None,
    )
    runner = AgentTestRunner(
        store=store,
        store_for=governance._store_for,
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
        assert current["error"]["message"].startswith("AgentGitError: fatal:")
    finally:
        runner.close()


def test_runner_rejects_zero_exit_without_structured_test_items(tmp_path: Path) -> None:
    git_store, governance = _version_governance(tmp_path, agent_id="agent-a")
    store = _testing_store(tmp_path)
    run = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id="agc-test",
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite={"test_files": ["tests/test_agent.py"]},
        suite_digest="b" * 64,
    )
    test_run_id = str(run["test_run_id"])
    assert store.claim_run(test_run_id) is not None
    runner = AgentTestRunner(
        store=store,
        store_for=lambda _agent_id: git_store,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:1",
        api_key=None,
        timeout_seconds=30,
    )
    paths = _run_paths(tmp_path / "artifacts", test_run_id)
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, "-c", "pass"], text=True)
    process.wait(timeout=10)
    try:
        runner._finish_process(
            test_run_id,
            process=process,
            paths=paths,
            duration_seconds=0.01,
            timed_out=False,
        )
        persisted = store.get_run(test_run_id)
        assert persisted is not None
        assert persisted["status"] == "error"
        assert persisted["items"] == []
        assert persisted["error"]["error_code"] == "AGENT_TEST_REPORT_INVALID"
    finally:
        runner.close()


def test_runner_terminates_pytest_process_group_at_platform_timeout(
    tmp_path: Path,
) -> None:
    git_store, governance = _version_governance(tmp_path, agent_id="agent-a")
    workspace = git_store.repository_dir
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("CLAUDE.md").write_text("# timeout Agent\n", encoding="utf-8")
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("test_timeout.py").write_text(
        "import time\n\ndef test_platform_timeout():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    git_store.ensure_bootstrap()
    suite = inspect_agent_test_suite(
        workspace,
        agent_id="agent-a",
        commit_sha=str(git_store.current_commit_sha()),
    )
    assert suite.runnable and suite.suite_digest
    store = _testing_store(tmp_path)
    run = store.create_run(
        agent_id="agent-a",
        commit_sha=str(git_store.current_commit_sha()),
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite=suite.model_dump(mode="json"),
        suite_digest=suite.suite_digest,
    )
    runner = AgentTestRunner(
        store=store,
        store_for=governance._store_for,
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


def test_store_only_publishes_server_attested_invocations(tmp_path: Path) -> None:
    store = _testing_store(tmp_path)
    created = store.create_run(
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={"test_files": ["tests/test_agent.py"]},
        suite_digest="a" * 64,
    )
    test_run_id = str(created["test_run_id"])
    assert store.claim_run(test_run_id) is not None
    attested = {
        "test_run_id": test_run_id,
        "run_id": "run-real",
        "session_id": "session-real",
        "agent_version_id": "a" * 40,
        "langfuse_trace_id": "b" * 32,
        "langfuse_trace_url": None,
        "errors": [],
    }
    store.record_attested_invocation(test_run_id, attested)

    finished = store.finish_run(
        test_run_id,
        status="passed",
        report={"invocations": [{"run_id": "run-fabricated"}], "_attested_invocations": [{"run_id": "also-fabricated"}]},
        items=[{"nodeid": "tests/test_agent.py::test_agent", "outcome": "passed", "phase": "call"}],
        stdout="1 passed",
        stderr="",
    )

    assert finished["invocations"] == [attested]
    assert finished["report"]["invocations"] == [attested]
    assert "_attested_invocations" not in finished["report"]


def test_runner_attestation_is_exact_and_ephemeral(tmp_path: Path) -> None:
    _git_store, governance = _version_governance(tmp_path, agent_id="agent-a")
    runner = AgentTestRunner(
        store=_testing_store(tmp_path),
        store_for=governance._store_for,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:50400",
        api_key=None,
        timeout_seconds=30,
    )
    token = runner._register_attestation(
        "atr-real",
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id="agc-real",
    )
    try:
        runner.require_attestation(
            test_run_id="atr-real",
            token=token,
            agent_id="agent-a",
            commit_sha="a" * 40,
            change_set_id="agc-real",
        )
        with pytest.raises(PermissionError, match="invalid or no longer active"):
            runner.require_attestation(
                test_run_id="atr-real",
                token="wrong-token",
                agent_id="agent-a",
                commit_sha="a" * 40,
                change_set_id="agc-real",
            )
    finally:
        runner.close()

    with pytest.raises(PermissionError, match="invalid or no longer active"):
        runner.require_attestation(
            test_run_id="atr-real",
            token=token,
            agent_id="agent-a",
            commit_sha="a" * 40,
            change_set_id="agc-real",
        )


def test_message_response_schema_projects_runtime_chat_response() -> None:
    runtime_response = ChatResponse(
        run_id="run-test",
        session_id="session-test",
        answer="ok",
        stop_reason="end_turn",
    )

    projected = AgentTestMessageResponse.model_validate(runtime_response.model_dump(mode="python"))

    assert projected.answer == "ok"
    assert projected.stop_reason == "end_turn"
    assert projected.run_id == "run-test"
    assert projected.session_id == "session-test"


def test_public_run_request_keeps_execution_fields_backend_owned() -> None:
    request = AgentTestRunCreateRequest.model_validate({"agent_id": "agent-a", "commit_sha": "a" * 40})
    assert request.agent_id == "agent-a"
    assert request.commit_sha == "a" * 40

    with pytest.raises(ValidationError):
        AgentTestRunCreateRequest.model_validate(
            {
                "agent_id": "agent-a",
                "commit_sha": "a" * 40,
                "status": "passed",
                "command": ["true"],
                "change_set_id": "agc-hostile",
                "source": "release_check",
            }
        )


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
    suite = inspect_agent_test_suite(
        workspace,
        agent_id="url-agent-id",
        commit_sha=commit_sha,
    )
    import_id = store.record_import(
        agent_id="url-agent-id",
        action="created",
        package_sha256="a" * 64,
        tree_sha256="b" * 64,
        commit_sha=commit_sha,
        suite=suite.model_dump(mode="json"),
    )
    assert suite.runnable is False
    assert {item.code for item in suite.diagnostics} == {"AGENT_TESTS_DIRECTORY_MISSING"}
    with store.Session() as db:
        record = db.get(AgentWorkspaceImportRecordModel, import_id)
        assert record is not None
        assert record.agent_id == "url-agent-id"
        assert record.commit_sha == commit_sha
        assert {item["code"] for item in record.warnings_json} == {"AGENT_TESTS_DIRECTORY_MISSING"}
