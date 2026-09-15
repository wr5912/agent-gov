"""报告校验契约：真实 pytest 报告与负向畸形输入，不证明 Agent 调用或发布通过。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from app.agent_testing.report_validation import attested_release_invocation_errors, passed_report_errors

_TEST_RUN = "report-validation-contract"
_COMMIT = "a" * 40
_INVOCATION_ERROR = "release test has invalid server-attested Agent invocation identity or result"


@pytest.fixture(scope="module")
def real_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    directory = tmp_path_factory.mktemp("real-pytest-report")
    source = directory / "test_real_files.py"
    source.write_text(
        "from pathlib import Path\n\n"
        "def test_read_written_bytes(tmp_path: Path):\n"
        "    path = tmp_path / 'content.txt'\n"
        "    path.write_bytes(b'report-contract')\n"
        "    assert path.read_bytes() == b'report-contract'\n\n"
        "def test_file_lifecycle(tmp_path: Path):\n"
        "    path = tmp_path / 'owned.txt'\n"
        "    path.touch()\n"
        "    assert path.is_file()\n"
        "    path.unlink()\n"
        "    assert not path.exists()\n",
        encoding="utf-8",
    )
    report_path = directory / "report.json"
    package_src = Path(__file__).resolve().parents[1] / "packages/agentgov-testkit/src"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(package_src),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "AGENTGOV_TEST_REPORT_PATH": str(report_path),
    }
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "agentgov_testkit.pytest_plugin", str(source)],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_code"] == process.returncode
    assert len(report["collected_nodeids"]) == len(report["items"]) == 2
    assert report["invocations"] == []
    return report


def _errors(report: dict[str, object], *, exit_code: int | None = 0, release_check: bool = False) -> list[str]:
    return passed_report_errors(
        report,
        actual_exit_code=exit_code,
        release_check=release_check,
        attested_invocations=[],
        test_run_id=_TEST_RUN,
        commit_sha=_COMMIT,
    )


def test_real_pytest_report_passes_host_validation_but_cannot_attest_release(real_report: dict[str, object]) -> None:
    original = deepcopy(real_report)
    assert _errors(real_report) == []
    assert _errors(real_report, release_check=True) == ["release test has no server-attested Agent invocation"]
    assert real_report == original


@pytest.mark.parametrize(("reported", "actual"), [(False, 0), (None, 0), ("0", 0), (1, 0), (0, 1), (0, None)])
def test_report_exit_must_match_completed_successful_process(real_report: dict[str, object], reported: object, actual: int | None) -> None:
    report = {**real_report, "exit_code": reported}
    assert "pytest report exit code does not match the completed process" in _errors(report, exit_code=actual)


@pytest.mark.parametrize("collected", [None, [], "test_leaf", [""], ["test_leaf", None]])
def test_report_requires_nonempty_string_collected_leaves(real_report: dict[str, object], collected: object) -> None:
    errors = _errors({**real_report, "collected_nodeids": collected})
    assert "pytest report has no complete collected leaf list" in errors
    assert "pytest report leaf results do not cover the collected suite" in errors


@pytest.mark.parametrize("items", [None, [], "test_leaf"])
def test_report_requires_leaf_result_list(real_report: dict[str, object], items: object) -> None:
    errors = _errors({**real_report, "items": items})
    assert "pytest report has no leaf results" in errors
    assert "pytest report leaf results do not cover the collected suite" in errors


def test_report_rejects_non_object_leaf_results(real_report: dict[str, object]) -> None:
    errors = _errors({**real_report, "items": [None, "test_leaf", 42]})
    assert errors.count("pytest report contains a non-object leaf result") == 3
    assert "pytest report leaf results do not cover the collected suite" in errors


@pytest.mark.parametrize("nodeid", [None, "", 42])
def test_report_rejects_missing_or_invalid_leaf_identity(real_report: dict[str, object], nodeid: object) -> None:
    report = deepcopy(real_report)
    report["items"][0]["nodeid"] = nodeid
    errors = _errors(report)
    assert "pytest report contains a leaf without nodeid" in errors
    assert "pytest report leaf results do not cover the collected suite" in errors


def test_report_rejects_duplicate_collected_leaves_and_results(real_report: dict[str, object]) -> None:
    report = deepcopy(real_report)
    report["collected_nodeids"].append(report["collected_nodeids"][0])
    report["items"].append(deepcopy(report["items"][0]))
    errors = _errors(report)
    assert "pytest report contains duplicate collected leaves" in errors
    assert "pytest report contains duplicate leaf results" in errors


@pytest.mark.parametrize("mismatch", ["missing", "foreign", "duplicate"])
def test_report_results_must_cover_each_collected_leaf_exactly_once(real_report: dict[str, object], mismatch: str) -> None:
    report = deepcopy(real_report)
    if mismatch == "missing":
        report["items"].pop()
    elif mismatch == "foreign":
        report["items"][0]["nodeid"] = "test_uncollected.py::test_extra"
    else:
        report["items"].append(deepcopy(report["items"][0]))
    assert "pytest report leaf results do not cover the collected suite" in _errors(report)


@pytest.mark.parametrize(("outcome", "phase"), [("failed", "call"), ("skipped", "setup"), ("passed", "teardown"), (None, "call")])
def test_report_requires_passing_call_outcome(real_report: dict[str, object], outcome: object, phase: str) -> None:
    report = deepcopy(real_report)
    leaf = report["items"][0]
    leaf.update({"outcome": outcome, "phase": phase})
    assert f"pytest leaf did not pass: {leaf['nodeid']}" in _errors(report)


@pytest.mark.parametrize(
    "phases",
    [
        None,
        ["setup", "call", "teardown"],
        {"setup": "passed", "call": "passed"},
        {"setup": "passed", "call": "passed", "teardown": "passed", "collection": "passed"},
        {"setup": "skipped", "call": "passed", "teardown": "passed"},
        {"setup": "passed", "call": "failed", "teardown": "passed"},
        {"setup": "passed", "call": "passed", "teardown": "failed"},
    ],
)
def test_report_requires_exactly_three_passing_phases(real_report: dict[str, object], phases: object) -> None:
    report = deepcopy(real_report)
    leaf = report["items"][0]
    leaf["phase_outcomes"] = phases
    assert f"pytest leaf has incomplete or non-passing phases: {leaf['nodeid']}" in _errors(report)


def _invocation_metadata(run_id: str = "contract-run-a") -> dict[str, object]:
    """仅构造字段校验输入，不签发服务端 attestation，不写入测试/发布记录。"""
    return {"test_run_id": _TEST_RUN, "agent_version_id": _COMMIT, "run_id": run_id, "session_id": "contract-session", "errors": []}


def test_invocation_field_validation_accepts_distinct_runs_in_same_session() -> None:
    invocations = [_invocation_metadata(), _invocation_metadata("contract-run-b")]
    original = deepcopy(invocations)
    assert attested_release_invocation_errors(invocations, test_run_id=_TEST_RUN, commit_sha=_COMMIT) == []
    assert invocations == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_run_id", "another-test-run"),
        ("agent_version_id", "b" * 40),
        ("run_id", None),
        ("run_id", ""),
        ("run_id", []),
        ("session_id", None),
        ("session_id", ""),
        ("session_id", 42),
        ("errors", None),
        ("errors", ""),
        ("errors", ["runtime invocation failed"]),
    ],
)
def test_invocation_field_validation_rejects_identity_or_result_mismatch(field: str, value: object) -> None:
    invocation = {**_invocation_metadata(), field: value}
    assert attested_release_invocation_errors([invocation], test_run_id=_TEST_RUN, commit_sha=_COMMIT) == [_INVOCATION_ERROR]


@pytest.mark.parametrize("field", ["test_run_id", "agent_version_id", "run_id", "session_id", "errors"])
def test_invocation_field_validation_requires_all_identity_and_result_fields(field: str) -> None:
    invocation = _invocation_metadata()
    invocation.pop(field)
    assert attested_release_invocation_errors([invocation], test_run_id=_TEST_RUN, commit_sha=_COMMIT) == [_INVOCATION_ERROR]


def test_invocation_field_validation_rejects_reusing_run_across_sessions() -> None:
    first = _invocation_metadata()
    reused = {**first, "session_id": "another-contract-session"}
    assert attested_release_invocation_errors([first, reused], test_run_id=_TEST_RUN, commit_sha=_COMMIT) == [_INVOCATION_ERROR]
