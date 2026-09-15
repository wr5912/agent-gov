"""Session Workspace 回收公共验收入口的真实文件与防泄漏契约。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import venv
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from scripts import run_workspace_reclaim_acceptance as runner
from scripts import workspace_reclaim_acceptance_runtime as runtime
from scripts import workspace_reclaim_acceptance_sessions as sessions

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
TARGET = "runtime-workspace-reclaim-live-smoke"


def _real_artifact(tmp_path: Path) -> runtime.SessionArtifact:
    session_id = str(uuid.uuid4())
    workspace_id = session_workspace_id(f"published-contract--v-{DIGEST}", uuid.uuid4())
    target = tmp_path / workspace_id
    state = target / ".agentgov-runtime-state/.agentscope"
    environment = state / ".venv"
    environment.parent.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    (target / ".agentgov-runtime-cache").mkdir()
    marker = target / runtime.WORKSPACE_MARKER
    marker.write_text(
        json.dumps({"workspace_id": workspace_id, "harness_digest": DIGEST}),
        encoding="utf-8",
    )
    return runtime.validate_artifact(tmp_path, session_id, workspace_id, DIGEST)


def _reclaim_paths(artifact: runtime.SessionArtifact) -> tuple[Path, Path]:
    key = runtime.sha256_text(artifact.workspace_id)
    reclaim_root = artifact.target.parent / runtime.RECLAIM_DIRECTORY
    reclaim_root.mkdir(exist_ok=True)
    return reclaim_root / f"{key}.json", reclaim_root / key


def _sidecar_payload(artifact: runtime.SessionArtifact, phase: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "phase": phase,
        "workspace_id": artifact.workspace_id,
        "harness_digest": artifact.harness_digest,
        "device": artifact.device,
        "inode": artifact.inode,
        "marker_sha256": artifact.marker_sha256,
        "ignored_bindings": [["runtime-agent", artifact.session_id]],
    }


def _write_sidecar(record: Path, payload: dict[str, object]) -> None:
    record.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _selected_env(path: Path, secret: str) -> Path:
    path.write_text(
        "\n".join(
            (
                f"API_KEY={secret}",
                "HOST_PORT=50400",
                "FRONTEND_HOST_PORT=50401",
                "API_BIND_IP=127.0.0.1",
                "FRONTEND_BIND_IP=127.0.0.1",
                "FRONTEND_RUNTIME_API_BASE=http://localhost:50400",
            ),
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _preflight(path: Path, *, enabled: bool) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": str(path.parent),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if enabled:
        environment["REQUIRE_LIVE_RUNTIME"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_workspace_reclaim_acceptance.py"),
            "--env-file",
            str(path),
            "--preflight-only",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_real_venv_usage_and_reclaimed_report_are_exact_and_redacted(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    reclaimed_paths = runtime.ReclaimedPathsReport(
        target_absent=True,
        record_absent=True,
        tombstone_absent=True,
        temporary_records_absent=True,
    )
    report = runtime.artifact_report(artifact, reclaimed_paths)
    serialized = json.dumps(report, sort_keys=True)

    assert artifact.venv_usage.files > 0
    assert artifact.venv_usage.logical_bytes > 0
    assert artifact.venv_usage.allocated_bytes > 0
    assert report["workspace_before"] == runtime.tree_usage(artifact.target).report()
    assert report["venv_before"] == runtime.tree_usage(artifact.target / runtime.VENV_RELATIVE).report()
    assert report["workspace_after"] == runtime.TreeUsage().report()
    assert report["venv_after"] == runtime.TreeUsage().report()
    assert report["reclaimed_paths"] == reclaimed_paths
    assert report["session_sha256"] == hashlib.sha256(artifact.session_id.encode()).hexdigest()
    assert report["workspace_sha256"] == hashlib.sha256(artifact.workspace_id.encode()).hexdigest()
    assert artifact.session_id not in serialized
    assert artifact.workspace_id not in serialized
    assert str(artifact.target) not in serialized


@pytest.mark.parametrize("phase", ["prepared", "quarantined", "contents_removed"])
def test_sidecar_validation_uses_real_identity_marker_and_all_phases(tmp_path: Path, phase: str) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)
    if phase != "prepared":
        os.rename(artifact.target, tombstone)
    if phase == "contents_removed":
        shutil.rmtree(tombstone / ".agentgov-runtime-state")
        shutil.rmtree(tombstone / ".agentgov-runtime-cache")
    _write_sidecar(record, _sidecar_payload(artifact, phase))

    assert runtime.validate_reclaim_sidecar(record, tombstone, artifact, ("runtime-agent", artifact.session_id)) == phase


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("phase", "unknown"),
        ("workspace_id", "another-workspace"),
        ("harness_digest", "b" * 64),
        ("device", -1),
        ("inode", -1),
        ("marker_sha256", "b" * 64),
        ("ignored_bindings", [["only-one-value"]]),
        ("ignored_bindings", [["one", "two"], ["one", "two"]]),
        ("unexpected", True),
    ],
)
def test_sidecar_validation_fails_closed_for_each_record_boundary(tmp_path: Path, field: str, invalid: object) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)
    payload = _sidecar_payload(artifact, "prepared")
    payload[field] = invalid
    _write_sidecar(record, payload)

    with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
        runtime.validate_reclaim_sidecar(record, tombstone, artifact, ("runtime-agent", artifact.session_id))


def test_sidecar_rejects_changed_marker_and_false_completed_phase(tmp_path: Path) -> None:
    changed = _real_artifact(tmp_path / "changed")
    record, tombstone = _reclaim_paths(changed)
    (changed.target / runtime.WORKSPACE_MARKER).write_text("changed", encoding="utf-8")
    _write_sidecar(record, _sidecar_payload(changed, "prepared"))
    with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
        runtime.validate_reclaim_sidecar(record, tombstone, changed, ("runtime-agent", changed.session_id))

    incomplete = _real_artifact(tmp_path / "incomplete")
    record, tombstone = _reclaim_paths(incomplete)
    os.rename(incomplete.target, tombstone)
    _write_sidecar(record, _sidecar_payload(incomplete, "contents_removed"))
    with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
        runtime.validate_reclaim_sidecar(record, tombstone, incomplete, ("runtime-agent", incomplete.session_id))


def test_sidecar_requires_exact_runtime_agent_and_session_binding(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)
    _write_sidecar(record, _sidecar_payload(artifact, "prepared"))

    assert (
        runtime.validate_reclaim_sidecar(
            record,
            tombstone,
            artifact,
            ("runtime-agent", artifact.session_id),
        )
        == "prepared"
    )
    for wrong in (
        (artifact.session_id, "runtime-agent"),
        ("other-runtime", artifact.session_id),
        ("runtime-agent", "other-session"),
    ):
        with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
            runtime.validate_reclaim_sidecar(record, tombstone, artifact, wrong)

    payload = _sidecar_payload(artifact, "prepared")
    payload["ignored_bindings"] = [
        ["runtime-agent", artifact.session_id],
        ["runtime-agent", "extra-session"],
    ]
    _write_sidecar(record, payload)
    with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
        runtime.validate_reclaim_sidecar(
            record,
            tombstone,
            artifact,
            ("runtime-agent", artifact.session_id),
        )


def test_completed_natural_reclaim_between_sidecar_checks_is_a_retry_miss(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)
    _write_sidecar(record, _sidecar_payload(artifact, "prepared"))
    shutil.rmtree(artifact.target)
    record.unlink()
    mount = runtime.RuntimeMount(tmp_path, "", "", "", "", 0)

    assert (
        runtime._validated_sidecar_or_completed(
            record,
            tombstone,
            artifact,
            mount,
            ("runtime-agent", artifact.session_id),
        )
        is None
    )


def test_contents_removed_sidecar_after_tombstone_removal_is_a_retry_miss(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)
    _write_sidecar(record, _sidecar_payload(artifact, "contents_removed"))
    shutil.rmtree(artifact.target)
    mount = runtime.RuntimeMount(tmp_path, "", "", "", "", 0)

    assert (
        runtime._validated_sidecar_or_completed(
            record,
            tombstone,
            artifact,
            mount,
            ("runtime-agent", artifact.session_id),
        )
        is None
    )

    payload = _sidecar_payload(artifact, "prepared")
    _write_sidecar(record, payload)
    with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
        runtime._validated_sidecar_or_completed(
            record,
            tombstone,
            artifact,
            mount,
            ("runtime-agent", artifact.session_id),
        )


def test_sidecar_cross_snapshot_budget_covers_every_forward_phase_transition() -> None:
    assert runtime.SIDECAR_PHASES == ("prepared", "quarantined", "contents_removed")
    assert runtime.SIDECAR_SNAPSHOT_ATTEMPTS == 3
    for previous, current in (
        (None, "prepared"),
        ("prepared", "prepared"),
        ("prepared", "quarantined"),
        ("quarantined", "contents_removed"),
    ):
        runtime._require_monotonic_sidecar_phase(previous, current)
    for previous, current in (("quarantined", "prepared"), ("contents_removed", "quarantined")):
        with pytest.raises(runtime.AcceptanceFailure, match="RECLAIM_SIDECAR_INVALID"):
            runtime._require_monotonic_sidecar_phase(previous, current)


def test_session_recovery_identity_keeps_stable_request_and_unique_public_name(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    mount = runtime.RuntimeMount(tmp_path, "", "", "", "", 0)
    binding = runner.Binding("security-operations-expert", "runtime-agent", "version", DIGEST)
    handle = sessions.TrackedSession("stable-key", "workspace-reclaim-unique-name")

    assert sessions.session_create_request(handle, binding) == sessions.session_create_request(handle, binding)
    assert sessions._bind_exact_public_identity(
        handle,
        (sessions.PublicSessionRow(artifact.session_id, artifact.workspace_id, handle.name),),
        artifact.session_id,
        mount,
    )
    assert handle.session_id == artifact.session_id
    assert handle.workspace_id == artifact.workspace_id
    with pytest.raises(runtime.AcceptanceFailure, match="SESSION_CREATE_IDENTITY_CONFLICT"):
        sessions._bind_exact_public_identity(
            handle,
            (
                sessions.PublicSessionRow(artifact.session_id, artifact.workspace_id, handle.name),
                sessions.PublicSessionRow("duplicate", "duplicate-workspace", handle.name),
            ),
            artifact.session_id,
            mount,
        )


def test_wait_reclaimed_requires_target_record_tombstone_and_exact_temporary_absence(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    record, tombstone = _reclaim_paths(artifact)

    with pytest.raises(runtime.AcceptanceFailure, match="WORKSPACE_NOT_RECLAIMED"):
        asyncio.run(runtime.wait_reclaimed(runtime.RuntimeMount(tmp_path, "", "", "", "", 0), artifact, 0.01))
    shutil.rmtree(artifact.target)
    record.write_text("pending", encoding="utf-8")
    with pytest.raises(runtime.AcceptanceFailure, match="WORKSPACE_NOT_RECLAIMED"):
        asyncio.run(runtime.wait_reclaimed(runtime.RuntimeMount(tmp_path, "", "", "", "", 0), artifact, 0.01))
    record.unlink()
    tombstone.mkdir()
    with pytest.raises(runtime.AcceptanceFailure, match="WORKSPACE_NOT_RECLAIMED"):
        asyncio.run(runtime.wait_reclaimed(runtime.RuntimeMount(tmp_path, "", "", "", "", 0), artifact, 0.01))
    tombstone.rmdir()
    temporary = record.parent / f".{record.stem}.natural-window.tmp"
    temporary.write_text("pending", encoding="utf-8")
    with pytest.raises(runtime.AcceptanceFailure, match="WORKSPACE_NOT_RECLAIMED"):
        asyncio.run(runtime.wait_reclaimed(runtime.RuntimeMount(tmp_path, "", "", "", "", 0), artifact, 0.01))
    temporary.unlink()

    evidence = asyncio.run(runtime.wait_reclaimed(runtime.RuntimeMount(tmp_path, "", "", "", "", 0), artifact, 0.1))
    assert evidence == {
        "target_absent": True,
        "record_absent": True,
        "tombstone_absent": True,
        "temporary_records_absent": True,
    }


@pytest.mark.parametrize("workspace_id", ["../escape", "/absolute", "contains/slash", "汉字"])
def test_workspace_path_rejects_non_component_identifiers(tmp_path: Path, workspace_id: str) -> None:
    with pytest.raises(runtime.AcceptanceFailure, match="SESSION_WORKSPACE_INVALID"):
        runtime.safe_workspace_path(tmp_path, workspace_id)


def test_delete_evidence_rejects_all_404_and_any_remaining_public_reference(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    runner._require_concurrent_delete_statuses((204, 404))

    with pytest.raises(runtime.AcceptanceFailure, match="CONCURRENT_DELETE_FAILED"):
        runner._require_concurrent_delete_statuses((404, 404))
    with pytest.raises(runtime.AcceptanceFailure, match="CONCURRENT_DELETE_FAILED"):
        runner._require_concurrent_delete_statuses((204, 500))
    with pytest.raises(runtime.AcceptanceFailure, match="DELETED_SESSION_STILL_LISTED"):
        runner._require_public_absence((sessions.PublicSessionRow(artifact.session_id, artifact.workspace_id, "one"),), artifact)
    with pytest.raises(runtime.AcceptanceFailure, match="DELETED_WORKSPACE_STILL_REFERENCED"):
        runner._require_public_absence((sessions.PublicSessionRow("another-session", artifact.workspace_id, "one"),), artifact)
    assert runner._require_public_absence((sessions.PublicSessionRow("another-session", "another-workspace", "one"),), artifact) == (True, True)


def test_survivor_report_exposes_only_hashes_booleans_and_counts(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    dialogue = runner.SurvivorTurnReport(
        run_sha256="b" * 64,
        trace_sha256="c" * 64,
        reply_identity_sha256="d" * 64,
        reply_count=1,
        run_succeeded=True,
        binding_exact=True,
        session_exact=True,
        all_replies_persisted=True,
        canonical_assistant_completed=True,
        canonical_text_nonempty=True,
    )
    mutable = artifact.target / "conversation-state.json"
    mutable.write_text("real mutable session state", encoding="utf-8")
    refreshed = runtime.validate_artifact(tmp_path, artifact.session_id, artifact.workspace_id, artifact.harness_digest)
    report = runner._survivor_report(artifact, refreshed, [dialogue])
    serialized = json.dumps(report, sort_keys=True)

    assert report["workspace_usage_before_dialogues"] == artifact.workspace_usage.report()
    assert report["workspace_usage_after_dialogues"] == refreshed.workspace_usage.report()
    assert report["workspace_usage_before_dialogues"] != report["workspace_usage_after_dialogues"]
    assert report["venv_usage_before_dialogues"] == artifact.venv_usage.report()
    assert report["venv_usage_after_dialogues"] == refreshed.venv_usage.report()
    assert report["immutable_identity"]["marker_sha256"] == artifact.marker_sha256
    assert report["immutable_identity"]["venv_config_sha256"] == artifact.venv_config_sha256
    assert "artifact_integrity_unchanged" not in report
    assert artifact.session_id not in serialized
    assert artifact.workspace_id not in serialized
    assert str(artifact.target) not in serialized


def test_survivor_identity_rejects_concrete_venv_config_content_change(tmp_path: Path) -> None:
    artifact = _real_artifact(tmp_path)
    config = artifact.target / runtime.VENV_RELATIVE / "pyvenv.cfg"
    config.write_bytes(config.read_bytes() + b"\nchanged = true\n")
    changed = runtime.validate_artifact(tmp_path, artifact.session_id, artifact.workspace_id, artifact.harness_digest)

    with pytest.raises(runtime.AcceptanceFailure, match="SURVIVOR_SESSION_DAMAGED"):
        runtime.require_same_artifact_identity(artifact, changed)


def test_preflight_requires_explicit_live_opt_in_without_leaking_inputs(tmp_path: Path) -> None:
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    selected = _selected_env(tmp_path / "selected.env", secret)

    refused = _preflight(selected, enabled=False)
    assert refused.returncode == 1
    payload = json.loads(refused.stdout)
    assert payload["failure_code"] == "EXPLICIT_LIVE_OPT_IN_REQUIRED"
    assert payload["status"] == "failed"
    assert secret not in refused.stdout + refused.stderr
    assert str(selected) not in refused.stdout + refused.stderr

    accepted = _preflight(selected, enabled=True)
    assert accepted.returncode == 0
    assert accepted.stdout.strip() == "WORKSPACE_RECLAIM_ACCEPTANCE_PREFLIGHT_OK"
    assert accepted.stderr == ""
    assert secret not in accepted.stdout


def test_public_make_target_is_thin_and_uses_the_selected_env() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?m)^{TARGET}:\n"
        r'\t@\$\(ACCEPTANCE_PYTHON\) scripts/run_workspace_reclaim_acceptance\.py --env-file "\$\(COMPOSE_ENV_FILE\)"$',
    )
    assert pattern.search(makefile)


def test_live_runner_reuses_public_deployment_and_never_calls_runtime_port_directly() -> None:
    runner = (ROOT / "scripts/run_workspace_reclaim_acceptance.py").read_text(encoding="utf-8")
    helper = (ROOT / "scripts/workspace_reclaim_acceptance_runtime.py").read_text(encoding="utf-8")
    session_helper = (ROOT / "scripts/workspace_reclaim_acceptance_sessions.py").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts/workspace_reclaim_acceptance_watchdog.py").read_text(encoding="utf-8")
    watchdog_control = (ROOT / "scripts/workspace_reclaim_acceptance_watchdog_control.py").read_text(encoding="utf-8")
    sources = runner + helper + session_helper + watchdog + watchdog_control

    assert "_refresh_deployment" in runner
    assert '"/usr/bin/make", "--no-print-directory", "build"' not in runner
    assert "8090" not in sources
    assert "rmtree" not in sources
    assert "SIGSTOP" in sources
    assert "SIGKILL" in sources
    assert "SIGCONT" in sources
    assert "start_new_session=True" in watchdog_control
    assert "owner_start_ticks" in watchdog
    assert "submit_native_chat" in runner
    assert "_validate_terminal_run" in runner
    assert "_validate_canonical_replies" in runner
    assert "MockTransport" not in sources
    assert "TestClient" not in sources
    native_chat = (ROOT / "scripts/agentscope_live_native_chat.py").read_text(encoding="utf-8")
    assert 'client.post("/api/runtime/chat/"' in native_chat


def test_quality_policy_registers_live_scripts_and_main_flow_test() -> None:
    policy = json.loads((ROOT / "tests/quality_policy.json").read_bytes())
    selectors = set(policy["test_evidence"]["formal_live_selectors"])
    targets = set(policy["test_evidence"]["formal_live_targets"])
    assert {
        "scripts/run_workspace_reclaim_acceptance.py",
        "scripts/workspace_reclaim_acceptance_sessions.py",
        "scripts/workspace_reclaim_acceptance_runtime.py",
        "scripts/workspace_reclaim_acceptance_watchdog.py",
        "scripts/workspace_reclaim_acceptance_watchdog_control.py",
    } <= selectors
    assert TARGET in targets
    cutover = next(flow for flow in policy["main_flows"] if flow["id"] == "agentscope_runtime_cutover")
    boundary = next(scenario for scenario in cutover["scenarios"] if scenario["id"] == "harness_and_container_boundary")
    assert "tests/test_runtime_workspace_reclaim_acceptance.py" in boundary["pytest"]
    assert "tests/test_workspace_reclaim_acceptance_watchdog.py" in boundary["pytest"]
