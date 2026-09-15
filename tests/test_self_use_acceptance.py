"""自用验收入口的真实文件、参数与子进程契约；不替代浏览器/容器验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest
from scripts import run_self_use_acceptance as acceptance
from scripts.agentscope_live_acceptance_scenarios import LiveAcceptanceError


def _private(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _scenario(path: Path, agent_id: str, *, count: int = 1) -> Path:
    return _private(
        path,
        json.dumps(
            {
                "agent_id": agent_id,
                "scenarios": [
                    {
                        "scenario_id": f"document-review-{index}",
                        "purpose": "improvement",
                        "capability": "improvement_effect",
                        "input": f"请说明第 {index + 1} 项文档验收要求。",
                        "feedback_comment": "请明确测试和发布的先后关系。",
                        "source_ref": "authored-parser-contract",
                        "reviewed_by": "contract-test-author",
                        "reviewed_at": "2026-09-14T10:00:00+08:00",
                        "acceptance": {"allowed_target_paths": ["AGENT.md"], "required_test_literals": ["先测试后发布"]},
                    }
                    for index in range(count)
                ],
            },
            ensure_ascii=False,
        ),
    )


@pytest.fixture
def arguments(tmp_path: Path) -> argparse.Namespace:
    tmp_path.chmod(0o700)
    archive = tmp_path / "workspace.tar"
    with tarfile.open(archive, "w") as output:
        output.add(acceptance.REPO_ROOT / "README.md", arcname="references/README.md")
    archive.chmod(0o600)
    return argparse.Namespace(
        env_file=_private(tmp_path / "selected.env", f"API_KEY={secrets.token_hex(24)}\nLANGFUSE_ENABLED=true\n"),
        workspace_package=archive,
        docs_scenarios=_scenario(tmp_path / "docs.json", "documentation-assistant-e2e"),
        soc_scenarios=_scenario(tmp_path / "soc.json", "security-operations-expert"),
        report=tmp_path / "report.json",
        docs_agent_id="documentation-assistant-e2e",
        soc_agent_id="security-operations-expert",
        existing_docs_commit=None,
        preflight_only=False,
    )


def test_self_use_inputs_keep_selected_credentials_out_of_child_environment(arguments: argparse.Namespace) -> None:
    initial = arguments.env_file.read_bytes()
    environment = {
        **os.environ,
        "REQUIRE_LIVE_RUNTIME": "1",
        "API_KEY": secrets.token_hex(24),
        "MODEL_PROVIDER_API_KEY": secrets.token_hex(24),
        "MCP_AUTHORIZATION": secrets.token_hex(24),
        "DOCKER_HOST": "tcp://unselected.invalid:2375",
        "MAKEFLAGS": "--eval=unapproved",
    }
    arguments.existing_docs_commit = "a" * 40
    inputs, child_env, node, key = acceptance._inputs(arguments, environment)
    assert node and Path(node).is_file()
    config = inputs["config"]
    assert config["apiKey"] == key != environment["API_KEY"]
    assert config["uiBase"] == "http://localhost:50401"
    assert config["apiBase"] == "http://localhost:50400"
    assert not {"API_KEY", "MODEL_PROVIDER_API_KEY", "MCP_AUTHORIZATION", "DOCKER_HOST", "MAKEFLAGS"} & child_env.keys()
    assert child_env["COMPOSE_ENV_FILE"] == str(arguments.env_file.resolve())
    assert child_env["PYTHON"] == str(Path(sys.executable).absolute())
    assert child_env["REQUIRE_LIVE_RUNTIME"] == "1"
    workspace = inputs["plan"]["workspace"]
    assert workspace["expectedExistingCommitSha"] == arguments.existing_docs_commit
    assert workspace["packagePath"] == str(arguments.workspace_package)
    wire_input = json.loads(acceptance._browser_input_json(inputs))
    assert wire_input["config"] == config
    assert wire_input["plan"]["workspace"] == workspace
    assert len(wire_input["plan"]["cases"]) == 2
    for case, wire_case in zip(inputs["plan"]["cases"], wire_input["plan"]["cases"], strict=True):
        assert wire_case["agentId"] == case["agentId"]
        assert wire_case["scenarioFileSha256"] == case["scenarioFileSha256"]
        assert wire_case["scenario"]["input"] == case["scenario"].input_text
        assert wire_case["scenario"]["acceptance"]["allowed_target_paths"] == ["AGENT.md"]
        assert "input_text" not in wire_case["scenario"]
    assert arguments.env_file.read_bytes() == initial


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("soc_agent_id", "different-agent", "EXPECTED_SECURITY_OPERATIONS_AGENT_REQUIRED"),
        ("existing_docs_commit", "main", "EXACT_EXISTING_DOCS_COMMIT_REQUIRED"),
        ("existing_docs_commit", "A" * 40, "EXACT_EXISTING_DOCS_COMMIT_REQUIRED"),
        ("docs_agent_id", "security-operations-expert", "TWO_DISTINCT_AGENTS_REQUIRED"),
    ],
)
def test_self_use_rejects_wrong_target_before_deployment(arguments: argparse.Namespace, field: str, value: str, code: str) -> None:
    setattr(arguments, field, value)
    with pytest.raises(ValueError, match=code):
        acceptance._inputs(arguments, {"REQUIRE_LIVE_RUNTIME": "1"})


def test_self_use_requires_explicit_live_opt_in(arguments: argparse.Namespace) -> None:
    with pytest.raises(ValueError, match="EXPLICIT_LIVE_OPT_IN_REQUIRED"):
        acceptance._inputs(arguments, {})


@pytest.mark.parametrize("existing_commit", [None, "", "a" * 40])
def test_self_use_cli_parses_make_empty_commit_as_first_import(arguments: argparse.Namespace, existing_commit: str | None) -> None:
    argv = []
    for name in ("env_file", "workspace_package", "docs_scenarios", "soc_scenarios", "report"):
        argv.extend([f"--{name.replace('_', '-')}", str(getattr(arguments, name))])
    if existing_commit is not None:
        argv.extend(["--existing-docs-commit", existing_commit])
    parsed = acceptance._arguments(argv)
    expected = existing_commit or None
    assert parsed.existing_docs_commit == expected
    inputs, _, _, _ = acceptance._inputs(parsed, {"REQUIRE_LIVE_RUNTIME": "1"})
    assert inputs["plan"]["workspace"]["expectedExistingCommitSha"] == expected


@pytest.mark.parametrize("existing_commit", [" ", "main", "a" * 39, "A" * 40])
def test_self_use_cli_preserves_and_rejects_nonempty_malformed_commit(arguments: argparse.Namespace, existing_commit: str) -> None:
    argv = []
    for name in ("env_file", "workspace_package", "docs_scenarios", "soc_scenarios", "report"):
        argv.extend([f"--{name.replace('_', '-')}", str(getattr(arguments, name))])
    parsed = acceptance._arguments([*argv, "--existing-docs-commit", existing_commit])
    assert parsed.existing_docs_commit == existing_commit
    with pytest.raises(ValueError, match="EXACT_EXISTING_DOCS_COMMIT_REQUIRED"):
        acceptance._inputs(parsed, {"REQUIRE_LIVE_RUNTIME": "1"})


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ("LANGFUSE_ENABLED=true\n", "PRIVATE_API_KEY_REQUIRED"),
        ("API_KEY=\nLANGFUSE_ENABLED=true\n", "PRIVATE_API_KEY_REQUIRED"),
        ("API_KEY=contract-key\nLANGFUSE_ENABLED=false\n", "LANGFUSE_REQUIRED_FOR_GOVERNANCE_EVIDENCE"),
        ("API_KEY=contract-key\nAPI_BIND_IP=0.0.0.0\n", "loopback"),
        ("API_KEY=contract-key\nHOST_PORT=50401\n", "50400"),
        ("API_KEY=contract-key\nHOST_PORT=50399\n", "50400"),
        ("API_KEY=contract-key\nFRONTEND_RUNTIME_API_BASE=http://localhost:50402\n", "不一致"),
    ],
)
def test_self_use_rejects_selected_env_mismatch_without_ambient_key_fallback(arguments: argparse.Namespace, configuration: str, message: str) -> None:
    _private(arguments.env_file, configuration)
    with pytest.raises((ValueError, RuntimeError), match=message):
        acceptance._inputs(arguments, {"REQUIRE_LIVE_RUNTIME": "1", "API_KEY": secrets.token_hex(24)})


def test_self_use_case_uses_real_reviewed_file_digest_and_requires_one_case(tmp_path: Path) -> None:
    path = _scenario(tmp_path / "scenario.json", "docs-agent")
    result = acceptance._case(path, "docs-agent")
    assert result["scenarioFileSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["agentId"] == "docs-agent"
    assert result["scenario"].input_text == "请说明第 1 项文档验收要求。"
    with pytest.raises(LiveAcceptanceError, match="agent_id"):
        acceptance._case(path, "another-agent")
    _scenario(path, "docs-agent", count=2)
    with pytest.raises(ValueError, match="ONE_IMPROVEMENT_SCENARIO_PER_AGENT_REQUIRED"):
        acceptance._case(path, "docs-agent")


def test_self_use_private_inputs_refuse_public_files_and_symlinks(tmp_path: Path) -> None:
    path = _private(tmp_path / "private.txt", "operator input")
    assert acceptance._private_file(path) == path.resolve()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="PRIVATE_ACCEPTANCE_INPUT_REQUIRED"):
        acceptance._private_file(path)
    path.chmod(0o600)
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    for invalid in (link, tmp_path, acceptance.REPO_ROOT / "README.md"):
        with pytest.raises(ValueError, match="PRIVATE_ACCEPTANCE_INPUT_REQUIRED"):
            acceptance._private_file(invalid)


def test_self_use_report_is_private_exclusive_and_never_overwrites(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = acceptance._report_path(tmp_path / "report.json")
    report = {"scope": acceptance.SCOPE, "status": "failed"}
    assert acceptance._write_report(path, report)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == report
    assert not acceptance._write_report(path, {"status": "passed"})
    assert json.loads(path.read_text()) == report
    with pytest.raises(ValueError, match="NEW_PRIVATE_REPORT_PATH_REQUIRED"):
        acceptance._report_path(path)
    link = tmp_path / "missing-link.json"
    link.symlink_to(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="NEW_PRIVATE_REPORT_PATH_REQUIRED"):
        acceptance._report_path(link)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="NEW_PRIVATE_REPORT_PATH_REQUIRED"):
        acceptance._report_path(tmp_path / "other.json")
    with pytest.raises(ValueError, match="NEW_PRIVATE_REPORT_PATH_REQUIRED"):
        acceptance._report_path(acceptance.REPO_ROOT / "acceptance-report.json")


@pytest.mark.parametrize("returncode", [0, 7])
def test_self_use_child_stdin_stdout_and_stderr_are_separate(returncode: int, capfd: pytest.CaptureFixture[str]) -> None:
    secret = secrets.token_hex(24)
    code = f"import sys; value=sys.stdin.read(); print(len(value)); print(value,file=sys.stderr); raise SystemExit({returncode})"
    status, output = acceptance._run_child([sys.executable, "-c", code], {}, input_text=secret, timeout_seconds=10)
    assert status == returncode
    assert output.strip() == str(len(secret))
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""
    assert secret not in output


def test_self_use_child_timeout_kills_and_reaps_owned_process() -> None:
    code = "import os,time; print(os.getpid(),flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as expired:
        acceptance._run_child([sys.executable, "-c", code], {}, timeout_seconds=0.5)
    pid = int(expired.value.stdout)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_self_use_timeout_cleans_descendant_after_leader_exits(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-finished.txt"
    descendant = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text('not-stopped')"
    leader = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{descendant!r}]); print('started',flush=True)"
    with pytest.raises(subprocess.TimeoutExpired):
        acceptance._run_child([sys.executable, "-c", leader], {}, timeout_seconds=0.2)
    assert not marker.exists(), "超时后不应继续执行本轮已退出主进程的后代"


@pytest.mark.parametrize("status", ["passed", "failed"])
def test_self_use_accepts_only_explicit_browser_evidence_scope(status: str) -> None:
    report = {"scope": acceptance.SCOPE, "status": status, "agents": ["documentation-assistant-e2e", "security-operations-expert"]}
    assert acceptance._browser_outcome(json.dumps(report), secrets.token_hex(24)) == report


@pytest.mark.parametrize(
    "stdout",
    ["[]", "{}", '{"scope":"other","status":"passed"}', '{"scope":"self_use_dual_agent_governance","status":"success"}'],
)
def test_self_use_rejects_unqualified_browser_evidence(stdout: str) -> None:
    with pytest.raises(ValueError, match="INVALID_ACCEPTANCE_REPORT"):
        acceptance._browser_outcome(stdout, secrets.token_hex(24))


def test_self_use_refuses_secret_in_json_even_when_escaped_and_oversized_evidence() -> None:
    report = {"scope": acceptance.SCOPE, "status": "passed", "detail": "sensitive-value"}
    escaped = json.dumps(report).replace("sensitive", "\\u0073ensitive")
    with pytest.raises(ValueError, match="UNSAFE_ACCEPTANCE_REPORT"):
        acceptance._browser_outcome(escaped, "sensitive-value")
    with pytest.raises(ValueError, match="ACCEPTANCE_REPORT_TOO_LARGE"):
        acceptance._browser_outcome(" " * 1_000_001, "sensitive-value")
    with pytest.raises(json.JSONDecodeError):
        acceptance._browser_outcome("not JSON", "sensitive-value")


def test_self_use_source_and_env_must_match_original_bytes(tmp_path: Path) -> None:
    env_file = _private(tmp_path / "selected.env", "API_KEY=contract-input\n")
    digest = acceptance.source_artifact_sha256(acceptance.REPO_ROOT)
    env_digest = hashlib.sha256(env_file.read_bytes()).hexdigest()
    acceptance._verify_selected_inputs(env_file, digest, env_digest)
    with pytest.raises(ValueError, match="SOURCE_CHANGED_DURING_ACCEPTANCE"):
        acceptance._verify_selected_inputs(env_file, "0" * 64, env_digest)
    _private(env_file, "API_KEY=changed-contract-input\n")
    with pytest.raises(ValueError, match="SELECTED_ENV_CHANGED_DURING_ACCEPTANCE"):
        acceptance._verify_selected_inputs(env_file, digest, env_digest)
    report: dict[str, object] = {"selected_env_sha256": env_digest, "status": "passed"}
    acceptance._verify_idle_after_work(env_file, report)
    assert report["status"] == "failed"
    assert report["active_work_at_exit"] == "not_idle_or_unavailable"
    assert report["manual_follow_up_required"] is True


@pytest.mark.parametrize("fault", ["no-opt-in", "invalid-soc", "invalid-sha", "missing-env", "malformed-env"])
def test_self_use_public_cli_preflight_refuses_invalid_inputs_without_raw_diagnostics(arguments: argparse.Namespace, fault: str) -> None:
    if fault == "missing-env":
        arguments.env_file = arguments.env_file.parent / "missing-private.env"
    elif fault == "malformed-env":
        _private(arguments.env_file, "PRIVATE_VALUE='unterminated\n")
    command = [sys.executable, str(acceptance.REPO_ROOT / "scripts/run_self_use_acceptance.py")]
    for name in ("env_file", "workspace_package", "docs_scenarios", "soc_scenarios", "report"):
        command.extend([f"--{name.replace('_', '-')}", str(getattr(arguments, name))])
    if fault == "invalid-soc":
        command.extend(["--soc-agent-id", "different-agent"])
    elif fault == "invalid-sha":
        command.extend(["--existing-docs-commit", "main"])
    environment = {"REQUIRE_LIVE_RUNTIME": "0" if fault == "no-opt-in" else "1"}
    result = subprocess.run(command, cwd=acceptance.REPO_ROOT, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert result.stdout == "SELF_USE_ACCEPTANCE_FAILED\n"
    assert result.stderr == ""
    report = json.loads(arguments.report.read_text())
    expected_code = {
        "no-opt-in": "EXPLICIT_LIVE_OPT_IN_REQUIRED",
        "invalid-soc": "EXPECTED_SECURITY_OPERATIONS_AGENT_REQUIRED",
        "invalid-sha": "EXACT_EXISTING_DOCS_COMMIT_REQUIRED",
    }.get(fault, "SELF_USE_ACCEPTANCE_FAILED")
    assert report["status"] == "failed" and report["stage"] == "preflight"
    assert report["failure_code"] == expected_code
    assert "PRIVATE_VALUE" not in json.dumps(report)


def test_self_use_failure_code_keeps_only_controlled_diagnostics() -> None:
    for code in ("PUBLIC_BUILD_FAILED", "PUBLIC_ALL_UP_FAILED", "SOURCE_CHANGED_DURING_ACCEPTANCE", "SELECTED_ENV_CHANGED_DURING_ACCEPTANCE"):
        assert acceptance._safe_failure_code(ValueError(code)) == code
    secret = secrets.token_hex(24)
    assert acceptance._safe_failure_code(ValueError(secret)) == "SELF_USE_ACCEPTANCE_FAILED"
    assert acceptance._safe_failure_code(subprocess.TimeoutExpired([secret], 1)) == "ACCEPTANCE_PROCESS_TIMEOUT"
    assert acceptance._safe_failure_code(KeyboardInterrupt()) == "ACCEPTANCE_INTERRUPTED"


def _deployment_metadata() -> acceptance.StackEvidence:
    """仅为身份比较器的纯输入，不是 Docker inspect 或容器验收证据。"""
    return {
        service: {"id": str(index) * 64, "image": f"sha256:{'a' * 64}", "running": True, "source_sha256": "b" * 64, "ports": {}}
        for index, service in enumerate(("agentscope-runtime", "agent-gov-api", "agent-gov-ui"), start=1)
    }


def test_self_use_deployment_transition_requires_exact_maintenance_and_only_runtime_id_changes() -> None:
    before = _deployment_metadata()
    unchanged = acceptance._verify_deployment_transition(before, deepcopy(before), [])
    assert unchanged["runtime_recreated"] is False
    after = deepcopy(before)
    after["agentscope-runtime"]["id"] = "4" * 64
    receipt = [{"operation": "runtime-recreate", "completed": True, "stage": "candidate_test"}]
    result = acceptance._verify_deployment_transition(before, after, receipt)
    assert result == {"runtime_recreated": True, "runtime_container_before_id": "1" * 64, "runtime_container_after_id": "4" * 64}


@pytest.mark.parametrize("mismatch", ["unreported-restart", "restart-not-observed", "api", "ui", "image", "source", "running", "ports", "service"])
def test_self_use_deployment_transition_rejects_other_changes(mismatch: str) -> None:
    before = _deployment_metadata()
    after = deepcopy(before)
    after["agentscope-runtime"]["id"] = "4" * 64
    receipts = [{"operation": "runtime-recreate", "completed": True, "stage": "candidate_test"}]
    if mismatch == "unreported-restart":
        receipts = []
    elif mismatch == "restart-not-observed":
        after = deepcopy(before)
    elif mismatch in {"api", "ui"}:
        after[f"agent-gov-{mismatch}"]["id"] = "5" * 64
    elif mismatch == "service":
        after.pop("agent-gov-ui")
    else:
        key = "source_sha256" if mismatch == "source" else mismatch
        after["agentscope-runtime"][key] = {"image": "different", "source_sha256": "c" * 64, "running": False, "ports": {"9999/tcp": None}}[key]
    with pytest.raises(ValueError, match="DEPLOYMENT_CHANGED_DURING_ACCEPTANCE"):
        acceptance._verify_deployment_transition(before, after, receipts)


@pytest.mark.parametrize(
    "maintenance",
    [
        None,
        {},
        [None],
        [{"operation": "all-up", "completed": True, "stage": "candidate_test"}],
        [{"operation": "runtime-recreate", "completed": False, "stage": "candidate_test"}],
        [{"operation": "runtime-recreate", "completed": True, "stage": "arbitrary"}],
        [{"operation": "runtime-recreate", "completed": True, "stage": "publication"}],
        [{"operation": "runtime-recreate", "completed": True, "stage": "candidate_test"}] * 2,
    ],
)
def test_self_use_deployment_transition_rejects_invalid_or_repeated_maintenance(maintenance: object) -> None:
    before = _deployment_metadata()
    with pytest.raises(ValueError, match="INVALID_RUNTIME_MAINTENANCE_RECEIPT"):
        acceptance._verify_deployment_transition(before, before, maintenance)


def test_self_use_node_failure_preserves_only_owned_metadata() -> None:
    script = """
import assert from 'node:assert/strict';
import {selfUseFailureReport} from './scripts/verify_self_use_governance.mjs';
import {recordCandidateReceiptProgress} from './scripts/verify_agent_candidate_lifecycle.mjs';
import {CandidateRuntimeRestartError} from './scripts/improvement_ui_e2e/candidate_runtime_restart.mjs';
const progress = {};
const candidate = {agent: {agent_id: 'docs-agent'}, change_set_id: 'agc-owned', candidate_commit_sha: 'a'.repeat(40)};
const receipt = {agent_id: 'docs-agent', change_set_id: 'agc-owned', commit_sha: 'a'.repeat(40), test_run_id: 'atr-owned'};
recordCandidateReceiptProgress(progress, candidate, {...receipt, agent_id: 'another-agent'}, 'test_run_id');
assert.deepEqual(progress, {});
recordCandidateReceiptProgress(progress, candidate, receipt, 'test_run_id');
recordCandidateReceiptProgress(progress, candidate, {...receipt, release_id: 'release-owned'}, 'release_id');
assert.deepEqual(progress, {test_run_id: 'atr-owned', release_id: 'release-owned'});
assert.throws(() => recordCandidateReceiptProgress(progress, candidate, receipt, 'raw'));
const error = new Error('PRIVATE_RAW_RESPONSE');
Object.assign(error, {
  code: 'WORKSPACE_BOOTSTRAP_FAILED', status: 409, kind: 'timeout', acceptanceStage: 'candidate_publish',
  cause: 'PRIVATE_CAUSE', maintenanceCode: 'RUNTIME_RESTART_MAKE_FAILED',
  bootstrapEvidence: {
    agent_id: 'docs-agent', change_set_id: 'agc-owned', test_run_id: 'atr-owned', release_id: 'release-owned',
    candidate_commit_sha: 'a'.repeat(40), suite_digest: 'b'.repeat(64), retained: true,
    raw: 'PRIVATE_RAW_RESPONSE', packagePath: '/private/operator/package', import_record_id: '/invalid/id',
  },
  acceptanceEvidence: {
    stage: 'governance_soc', current_case: {agent_id: 'soc-agent', feedback_case_id: 'fc-owned', raw: 'PRIVATE_RAW_RESPONSE'},
    initial_workspace_release: {agent_id: 'docs-agent', candidate_commit_sha: 'a'.repeat(40)},
    completed_cases: [{agent_id: 'docs-agent', test_run_id: 'atr-owned'}, {}, {agent_id: 'must-not-appear'}],
  },
});
const report = selfUseFailureReport(error);
assert.equal(report.code, 'WORKSPACE_BOOTSTRAP_FAILED');
assert.equal(report.stage, 'candidate_publish');
assert.equal(report.http_status, 409);
assert.equal(report.reason_code, 'RUNTIME_RESTART_MAKE_FAILED');
assert.equal(selfUseFailureReport(new CandidateRuntimeRestartError('RUNTIME_RESTART_CONTEXT_REQUIRED')).code, 'RUNTIME_RESTART_CONTEXT_REQUIRED');
assert.equal(selfUseFailureReport(new CandidateRuntimeRestartError('PRIVATE')).code, 'DUAL_GOVERNANCE_FLOW_FAILED');
assert.equal(selfUseFailureReport(Object.assign(new Error('PRIVATE'), {code: 'SOURCE_RUN_TRACE_INCOMPLETE'})).code, 'SOURCE_RUN_TRACE_INCOMPLETE');
assert.equal(selfUseFailureReport(Object.assign(new Error('PRIVATE'), {code: 'SOURCE_RUN_TRACE_TIMEOUT'})).code, 'SOURCE_RUN_TRACE_TIMEOUT');
assert.equal(report.bootstrap_evidence.test_run_id, 'atr-owned');
assert.equal(report.bootstrap_evidence.release_id, 'release-owned');
assert.equal(report.bootstrap_evidence.candidate_commit_sha, 'a'.repeat(40));
assert.equal(report.bootstrap_evidence.suite_digest, 'b'.repeat(64));
assert.deepEqual(report.acceptance_evidence.current_case, {agent_id: 'soc-agent', feedback_case_id: 'fc-owned'});
assert.equal(report.acceptance_evidence.completed_cases.length, 2);
assert(!JSON.stringify(report).includes('PRIVATE'));
assert(!JSON.stringify(report).includes('/private'));
assert(!JSON.stringify(report).includes('/invalid'));
const rejected = selfUseFailureReport({code: 'PRIVATE', acceptanceStage: 'PRIVATE', kind: 'PRIVATE', status: 200,
  bootstrapEvidence: {candidate_commit_sha: 'bad', suite_digest: 'bad', raw: 'PRIVATE'}});
assert.equal(rejected.code, 'DUAL_GOVERNANCE_FLOW_FAILED');
assert.equal(rejected.http_status, null);
assert(!('stage' in rejected));
assert(!('bootstrap_evidence' in rejected));
console.log('SELF_USE_FAILURE_METADATA_OK');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=acceptance.REPO_ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "SELF_USE_FAILURE_METADATA_OK\n"
    assert result.stderr == ""


def test_self_use_main_missing_file_fails_without_private_path_or_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = [part for name in ("env-file", "workspace-package", "docs-scenarios", "soc-scenarios", "report") for part in (f"--{name}", str(tmp_path / name))]
    assert acceptance.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "SELF_USE_ACCEPTANCE_FAILED\n"
    assert captured.err == ""
