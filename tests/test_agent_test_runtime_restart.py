"""真实 SQLite 验证测试执行的恢复错误投影；不代表真实 Runtime 验收。"""

from pathlib import Path

import pytest
from app.agent_testing.runner import FIXED_PYTEST_COMMAND
from app.agent_testing.store import AgentTestingStore, AgentTestRunNotFound
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.release_activation import RuntimeActivationRestartRequired
from app.runtime_gateway.store import RuntimeStateConflict, RuntimeTemplateRestartRequired
from app.services.agent_governance_errors import AgentGovernanceError


@pytest.fixture
def active_test_run(tmp_path: Path):
    store = AgentTestingStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    created = store.create_run(
        agent_id="documentation-assistant",
        commit_sha="a" * 40,
        change_set_id=None,
        source="manual",
        command=FIXED_PYTEST_COMMAND,
        suite={"test_files": ["tests/test_documentation.py"]},
        suite_digest="suite-digest",
    )
    test_run_id = str(created["test_run_id"])
    assert store.claim_run(test_run_id)
    return store, test_run_id


@pytest.mark.parametrize("process_status", ["passed", "failed", "error"])
def test_server_runtime_restart_error_survives_process_completion(active_test_run, process_status: str):
    store, run_id = active_test_run
    store.record_runtime_restart_required(run_id, commit_sha="a" * 40)
    finished = store.finish_run(
        run_id,
        status=process_status,
        report={},
        items=[],
        stdout="",
        stderr="",
        error={"error_code": "AGENT_PYTEST_EXECUTION_ERROR"},
    )
    assert finished["status"] == "error"
    assert finished["error"]["error_code"] == RuntimeTemplateRestartRequired.error_code
    assert store.get_run(run_id)["error"] == finished["error"]
    assert store.latest_passed_for_commit(agent_id="documentation-assistant", commit_sha="a" * 40) is None


def test_pytest_report_cannot_invent_runtime_restart_authority(active_test_run):
    store, run_id = active_test_run
    finished = store.finish_run(
        run_id,
        status="failed",
        report={"error": {"error_code": RuntimeTemplateRestartRequired.error_code}},
        items=[],
        stdout="",
        stderr="",
    )
    assert finished["status"] == "failed"
    assert finished["error"] == {}


def test_cancel_does_not_become_a_restart_request(active_test_run):
    store, run_id = active_test_run
    store.record_runtime_restart_required(run_id, commit_sha="a" * 40)
    finished = store.finish_run(run_id, status="cancelled", report={}, items=[], stdout="", stderr="")
    assert finished["status"] == "cancelled"
    assert finished["error"] == {}


def test_restart_error_rejects_different_commit_and_terminal_run(active_test_run):
    store, run_id = active_test_run
    with pytest.raises(RuntimeError, match="active tested commit"):
        store.record_runtime_restart_required(run_id, commit_sha="b" * 40)
    assert store.get_run(run_id)["error"] == {}
    store.finish_run(run_id, status="failed", report={}, items=[], stdout="", stderr="")
    with pytest.raises(RuntimeError, match="active tested commit"):
        store.record_runtime_restart_required(run_id, commit_sha="a" * 40)
    with pytest.raises(AgentTestRunNotFound):
        store.record_runtime_restart_required("absent", commit_sha="a" * 40)


def test_only_template_restart_subtypes_carry_the_public_recovery_code():
    assert RuntimeStateConflict("unrelated conflict").error_code is None
    for error in (RuntimeTemplateRestartRequired("prepared"), RuntimeActivationRestartRequired("prepared")):
        assert error.status_code == 409
        assert error.error_code == "RUNTIME_TEMPLATE_RESTART_REQUIRED"
        published_error = AgentGovernanceError(409, str(error), error_code=error.error_code)
        assert published_error.error_code == error.error_code
