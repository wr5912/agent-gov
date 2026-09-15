"""候选效果人工对照 CLI 的隐私与身份契约；真实交互由独立 live 验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from scripts.compare_agent_candidate import (
    ComparisonError,
    ComparisonReport,
    _base_url,
    _digest,
    _private_report,
    _private_scenarios,
    _validate_run,
    comparison_connection,
)


def test_private_scenario_set_requires_real_private_file_and_exact_fields(tmp_path) -> None:
    scenario = tmp_path / "cases.jsonl"
    scenario.write_text(json.dumps({"case_id": "case-a", "message": "你好"}, ensure_ascii=False) + "\n", encoding="utf-8")
    scenario.chmod(0o600)
    cases, suite_sha256 = _private_scenarios(scenario)
    assert [(case.case_id, case.message) for case in cases] == [("case-a", "你好")]
    assert len(suite_sha256) == 64
    assert _digest("你好")[1] == 6

    scenario.chmod(0o644)
    with pytest.raises(ComparisonError, match="scenario_file_not_private"):
        _private_scenarios(scenario)
    scenario.chmod(0o600)
    scenario.write_text('{"case_id":"case-a","message":"你好","claim":"passed"}\n', encoding="utf-8")
    with pytest.raises(ComparisonError, match="invalid_scenario_fields"):
        _private_scenarios(scenario)


def test_exact_runtime_terminal_identity_rejects_false_success() -> None:
    chat = {"run_id": "run-a", "session_id": "session-a", "agent_version_id": "a" * 40, "answer": "回答", "errors": []}
    run = {
        "run_id": "run-a",
        "session_id": "session-a",
        "agent_id": "agent-a",
        "agent_version_id": "a" * 40,
        "status": "succeeded",
        "completed_at": "2026-09-13T00:00:00Z",
        "error": None,
    }
    assert _validate_run(chat, run, agent_id="agent-a", commit_sha="a" * 40) == "回答"
    for changed in ({**run, "status": "running"}, {**run, "agent_version_id": "b" * 40}, {**run, "completed_at": None}):
        with pytest.raises(ComparisonError):
            _validate_run(chat, changed, agent_id="agent-a", commit_sha="a" * 40)


def test_report_is_owner_only_and_never_implicitly_overwrites(tmp_path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    output = private_dir / "report.json"
    report = ComparisonReport(
        kind="agent_candidate_manual_comparison",
        purpose="human_reference_only_not_release_evidence",
        created_at="2026-09-13T00:00:00Z",
        agent_id="agent-a",
        change_set_id="change-a",
        baseline_commit_sha="a" * 40,
        candidate_commit_sha="b" * 40,
        scenario_set_sha256="c" * 64,
        cases=[],
        status="incomplete",
    )
    _private_report(output, report)
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == asdict(report)
    with pytest.raises(ComparisonError, match="report_already_exists"):
        _private_report(output, report)

    private_dir.chmod(0o755)
    with pytest.raises(ComparisonError, match="report_directory_not_private"):
        _private_report(private_dir / "other.json", report)


def test_api_base_rejects_cleartext_nonlocal_and_embedded_credentials() -> None:
    assert _base_url("http://127.0.0.1:50400") == "http://127.0.0.1:50400"
    assert _base_url("https://example.internal") == "https://example.internal"
    for value in ("http://example.internal", "http://user:password@127.0.0.1:50400", "https://example.internal/private"):
        with pytest.raises(ComparisonError):
            _base_url(value)


@pytest.mark.parametrize(
    ("payload", "expected_base"),
    [
        ("API_KEY=selected-contract-key\nHOST_PORT=50499\n", "http://127.0.0.1:50499"),
        ("API_KEY=selected-contract-key\n", "http://127.0.0.1:50400"),
        ("API_KEY=selected-contract-key\nHOST_PORT=\n", "http://127.0.0.1:50400"),
        ("API_KEY=selected-contract-key\nAPI_BASE_URL=https://api.example.internal/\nHOST_PORT=invalid\n", "https://api.example.internal"),
        ("API_KEY=selected-contract-key\nAPI_BASE_URL=\nHOST_PORT=50401\n", "http://127.0.0.1:50401"),
    ],
)
def test_selected_connection_reads_one_real_env_without_process_overrides(tmp_path, payload, expected_base) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text(payload, encoding="utf-8")
    connection = comparison_connection(
        env_file=selected,
        api_base_url=None,
        environ={"API_KEY": "ambient-contract-key", "API_BASE_URL": "https://other.example.internal", "HOST_PORT": "50498"},
    )
    assert connection.api_base_url == expected_base
    assert connection.api_key == "selected-contract-key"
    assert connection.api_key not in repr(connection)


@pytest.mark.parametrize("payload", ["HOST_PORT=50400\n", "API_KEY=\n", "API_KEY='   '\n", "API_KEY=${API_KEY}\n"])
def test_selected_env_missing_key_never_borrows_process_credentials(tmp_path, payload) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text(payload, encoding="utf-8")
    with pytest.raises(ComparisonError, match="selected_env_api_key_missing"):
        comparison_connection(env_file=selected, api_base_url=None, environ={"API_KEY": "ambient-contract-key"})


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"API_KEY=contract-key\nAPI_KEY=another\n", "selected_env_invalid"),
        (b"API_KEY='unterminated\n", "selected_env_invalid"),
        (b"API_KEY=\xff\n", "selected_env_invalid"),
        (b"API_KEY=contract-key\nHOST_PORT=58001\n", "selected_env_invalid_host_port"),
        (b"API_KEY=contract-key\nAPI_BASE_URL=http://[invalid\n", "invalid_api_base_url"),
        (b"API_KEY=contract-key\nAPI_BASE_URL=http://localhost:invalid\n", "invalid_api_base_url"),
        (b"API_KEY=contract-key\nAPI_BASE_URL=http://localhost:0\n", "invalid_api_base_url"),
        (b'API_KEY="contract\nkey"\n', "invalid_api_key"),
    ],
)
def test_invalid_selected_env_yields_fixed_error_codes(tmp_path, payload, expected_code) -> None:
    selected = tmp_path / "selected.env"
    selected.write_bytes(payload)
    with pytest.raises(ComparisonError) as failure:
        comparison_connection(env_file=selected, api_base_url=None, environ={})
    assert str(failure.value) == expected_code


def test_selected_connection_rejects_missing_file_directory_and_ambiguous_mode(tmp_path) -> None:
    with pytest.raises(ComparisonError, match="selected_env_unreadable"):
        comparison_connection(env_file=tmp_path / "missing.env", api_base_url=None, environ={})
    with pytest.raises(ComparisonError, match="selected_env_invalid"):
        comparison_connection(env_file=tmp_path, api_base_url=None, environ={})
    for selected, base in ((tmp_path, "http://localhost:50400"), (None, None)):
        with pytest.raises(ComparisonError, match="connection_mode_required"):
            comparison_connection(env_file=selected, api_base_url=base, environ={})


def test_direct_connection_keeps_explicit_url_and_process_key_contract() -> None:
    connection = comparison_connection(env_file=None, api_base_url="http://localhost:50409", environ={"API_KEY": "direct-contract-key"})
    assert connection.api_base_url == "http://localhost:50409"
    assert connection.api_key == "direct-contract-key"
    with pytest.raises(ComparisonError, match="api_key_env_missing"):
        comparison_connection(env_file=None, api_base_url="http://localhost:50409", environ={})


def test_real_cli_sanitizes_selected_env_parse_failure_without_api_calls(tmp_path) -> None:
    selected = tmp_path / "private-selected.env"
    selected.write_text("API_KEY=private-contract-token\nPRIVATE_DUPLICATE_KEY=one\nPRIVATE_DUPLICATE_KEY=two\n", encoding="utf-8")
    scenarios = tmp_path / "cases.jsonl"
    scenarios.write_text('{"case_id":"case-a","message":"未发送的输入"}\n', encoding="utf-8")
    scenarios.chmod(0o600)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    report = private / "comparison.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.compare_agent_candidate",
            "--env-file",
            str(selected),
            "--agent-id",
            "agent-a",
            "--change-set-id",
            "change-a",
            "--scenarios",
            str(scenarios),
            "--report",
            str(report),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "API_KEY": "ambient-contract-key"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "comparison failed: selected_env_invalid\n"
    assert not report.exists()
    assert all(value not in result.stderr for value in ("private-contract-token", "PRIVATE_DUPLICATE_KEY", str(selected), "Traceback"))
