from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from tests.agent_test_container_e2e_support import (
    SourceChangedApiEvidence,
    SourceChangedDockerEvidence,
    worker_evidence_runner,
    worker_runtime_evidence,
)
from tests.agent_test_container_e2e_support import (
    terminal_run as _terminal_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/run_agent_test_container_e2e.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_module("agentgov_agent_test_container_e2e", SCRIPT_PATH)


@pytest.fixture
def acceptance_root(tmp_path: Path) -> Iterator[Path]:
    parent = tmp_path / "candidates"
    parent.mkdir(mode=0o700)
    root = parent / "agentgov-acceptance-candidate-acceptance-current-11111111111111111111111111111111"
    root.mkdir(mode=0o700)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "volumes/data").mkdir(parents=True, mode=0o755)
    (runtime / "volumes").chmod(0o755)
    root.chmod(0o500)
    try:
        yield runtime
    finally:
        root.chmod(0o700)


def _settings(acceptance_root: Path):
    return SCRIPT.AcceptanceSettings(
        base_url="http://127.0.0.1:58080",
        api_key="private-test-value",
        acceptance_run_id="acceptance-current",
        runtime_root=acceptance_root,
        api_container="agentgov-test-current-api",
        worker_container="agentgov-test-current-test-worker",
        poll_seconds=0.001,
    )


def _docker_evidence(command: list[str]) -> str:
    if command[1:3] == ["image", "inspect"]:
        return "acceptance-current"
    if command[1:3] == ["ps", "-q"]:
        return "running-container"
    if command[1:3] == ["ps", "-aq"]:
        return ""
    if command[1] == "exec":
        return "absent"
    raise AssertionError(f"unexpected Docker evidence command: {command[:3]}")


def test_settings_require_exact_agent_test_profile_and_isolated_runtime_root(acceptance_root: Path) -> None:
    environment = {
        SCRIPT.ACTIVE_ENV: "1",
        SCRIPT.RUN_ID_ENV: "acceptance-current",
        SCRIPT.PROFILE_ENV: "agent-test",
        SCRIPT.RUNTIME_ROOT_ENV: str(acceptance_root),
        "CONTAINER_NAME_PREFIX": "agentgov-test-current",
        "API_BASE": "http://127.0.0.1:58080",
        "API_KEY": "private-test-value",
    }

    settings = SCRIPT.AcceptanceSettings.from_environment(environment)

    assert settings.acceptance_run_id == "acceptance-current"
    assert settings.runtime_root == acceptance_root
    assert settings.api_key == "private-test-value"
    assert settings.api_container == "agentgov-test-current-api"
    assert settings.worker_container == "agentgov-test-current-test-worker"
    with pytest.raises(SCRIPT.AcceptanceError, match="agent-test acceptance profile"):
        SCRIPT.AcceptanceSettings.from_environment({**environment, SCRIPT.PROFILE_ENV: "core"})
    with pytest.raises(SCRIPT.AcceptanceError, match="authority is missing"):
        SCRIPT.AcceptanceSettings.from_environment({**environment, SCRIPT.ACTIVE_ENV: "0"})
    acceptance_root.parent.chmod(0o700)
    try:
        with pytest.raises(SCRIPT.AcceptanceError, match="candidate directory authority"):
            SCRIPT.AcceptanceSettings.from_environment(environment)
    finally:
        acceptance_root.parent.chmod(0o500)
    redirected_root = acceptance_root.parent.parent / "unmanaged-candidate"
    redirected_root.mkdir(mode=0o700)
    redirected = redirected_root / "runtime"
    redirected.mkdir(mode=0o700)
    (redirected / "volumes/data").mkdir(parents=True, mode=0o755)
    (redirected / "volumes").chmod(0o755)
    redirected_root.chmod(0o500)
    with pytest.raises(SCRIPT.AcceptanceError, match="candidate snapshot boundary"):
        SCRIPT.AcceptanceSettings.from_environment({**environment, SCRIPT.RUNTIME_ROOT_ENV: str(redirected)})


@pytest.mark.parametrize(
    "hostile",
    [None, "stopped", "stale-label", "data-source", "duplicate-volume", "volume-options", "mountpoint"],
)
def test_worker_runtime_self_proof_binds_fresh_container_data_and_named_volume(
    acceptance_root: Path,
    hostile: str | None,
) -> None:
    settings = _settings(acceptance_root)
    evidence = worker_runtime_evidence(
        acceptance_root=acceptance_root / "volumes",
        worker_container=settings.worker_container,
        acceptance_run_id=settings.acceptance_run_id,
        acceptance_image_label=SCRIPT.ACCEPTANCE_IMAGE_LABEL,
        hostile=hostile,
    )
    commands: list[list[str]] = []

    def docker_runner(command: list[str]) -> str:
        return worker_evidence_runner(evidence, commands, command)

    if hostile is None:
        authority = SCRIPT.acceptance_support.prove_worker_runtime_authority(
            worker_container=settings.worker_container,
            acceptance_run_id=settings.acceptance_run_id,
            runtime_data_dir=settings.runtime_data_dir,
            docker_runner=docker_runner,
        )
        assert authority.runs_volume_name == evidence.volume_name
        assert authority.runs_volume_mountpoint == evidence.mountpoint
        assert commands == [
            ["docker", "inspect", settings.worker_container],
            ["docker", "volume", "inspect", evidence.volume_name],
        ]
    else:
        with pytest.raises(SCRIPT.acceptance_support.AcceptanceSupportError):
            SCRIPT.acceptance_support.prove_worker_runtime_authority(
                worker_container=settings.worker_container,
                acceptance_run_id=settings.acceptance_run_id,
                runtime_data_dir=settings.runtime_data_dir,
                docker_runner=docker_runner,
            )


@pytest.mark.parametrize("hostile", [None, "stopped", "stale-label", "mounted"])
def test_api_runtime_bootstrap_self_proof_rejects_any_target_mount(
    acceptance_root: Path,
    hostile: str | None,
) -> None:
    settings = _settings(acceptance_root)
    mount = {
        "Type": "bind",
        "Source": str(acceptance_root / "foreign-bootstrap"),
        "Destination": SCRIPT.acceptance_support.RUNTIME_BOOTSTRAP_TARGET,
        "RW": False,
    }
    inspect = {
        "Id": "c" * 64,
        "Name": f"/{settings.api_container}",
        "State": {"Running": hostile != "stopped"},
        "Config": {
            "Labels": {
                SCRIPT.ACCEPTANCE_IMAGE_LABEL: "stale" if hostile == "stale-label" else settings.acceptance_run_id,
            }
        },
        "Mounts": [mount] if hostile == "mounted" else [],
    }

    def action() -> None:
        SCRIPT.acceptance_support.prove_runtime_bootstrap_not_mounted(
            api_container=settings.api_container,
            acceptance_run_id=settings.acceptance_run_id,
            docker_runner=lambda _command: json.dumps([inspect]),
        )

    if hostile is None:
        action()
    else:
        with pytest.raises(SCRIPT.acceptance_support.AcceptanceSupportError):
            action()


def test_hostile_workspace_package_covers_each_isolation_boundary_without_live_fixture(tmp_path: Path) -> None:
    marker = tmp_path / "host-only-marker"
    package = SCRIPT._workspace_package(
        "agent-test-e2e-probe",
        test_filename="test_isolation.py",
        test_source=SCRIPT._hostile_test_source(marker),
    )

    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        test_member = archive.extractfile("workspace/tests/test_isolation.py")
        module_shadow_member = archive.extractfile("workspace/pytest.py")
        package_shadow_member = archive.extractfile("workspace/pytest/__init__.py")
        assert test_member is not None
        assert module_shadow_member is not None
        assert package_shadow_member is not None
        private_asset_digests: dict[str, str] = {}
        for path, _content in SCRIPT.SYNTHETIC_PRIVATE_ASSETS:
            member = archive.extractfile(f"workspace/{path}")
            assert member is not None
            private_asset_digests[path] = hashlib.sha256(member.read()).hexdigest()
        source = test_member.read().decode("utf-8")
        shadows = {
            module_shadow_member.read().decode("utf-8"),
            package_shadow_member.read().decode("utf-8"),
        }

    assert "API_KEY" in source
    assert private_asset_digests == {path: hashlib.sha256(content).hexdigest() for path, content in SCRIPT.SYNTHETIC_PRIVATE_ASSETS}
    assert all(f"/workspace/{path}" in source for path, _content in SCRIPT.SYNTHETIC_PRIVATE_ASSETS)
    assert 'Path("/data")' in source
    assert 'Path("/var/run/docker.sock")' in source
    assert str(marker) in source
    assert 'Path("/proc/net/route")' in source
    assert 'Path("/agentgov-rootfs-probe")' in source
    assert f'Path("/workspace/{SCRIPT.VOLUME_ROOT_CANARY}")' in source
    assert f'Path("/workspace/{SCRIPT.VOLUME_SIBLING_RUN}")' in source
    assert "test_named_volume_subpath_hides_root_and_sibling_runs" in source
    assert '("docker", "git", "curl", "ssh")' in source
    assert source.count("def test_") >= 5
    assert "def test_secret_environment_is_not_inherited(agent" not in source
    assert len(shadows) == 1
    shadow = shadows.pop()
    assert "AGENTGOV_TEST_REPORT_PATH" in shadow
    assert "'items': []" in shadow
    assert "raise SystemExit(0)" in shadow


def test_host_marker_setup_places_root_and_sibling_canaries_in_worker_volume(acceptance_root: Path) -> None:
    commands: list[list[str]] = []

    def docker_runner(command: list[str]) -> str:
        commands.append(command)
        return "ready"

    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=docker_runner,
    )

    marker = verifier._create_host_marker()

    assert marker.exists()
    assert verifier._volume_canaries_created is True
    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == ["docker", "exec", verifier.settings.worker_container]
    assert command[-2:] == [SCRIPT.VOLUME_ROOT_CANARY, SCRIPT.VOLUME_SIBLING_RUN]


def test_private_assets_round_trip_through_public_export_with_exact_digests(
    acceptance_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_sha = "7" * 40
    package = SCRIPT._workspace_package(
        "agent-test-e2e-probe",
        test_filename="test_isolation.py",
        test_source=SCRIPT._hostile_test_source(acceptance_root / "host-marker"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/workspace/export")
        return httpx.Response(
            200,
            content=package,
            headers={
                "x-agent-commit-sha": commit_sha,
                "x-workspace-package-sha256": hashlib.sha256(package).hexdigest(),
                "x-workspace-tree-sha256": "8" * 64,
            },
        )

    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler)),
        docker_runner=_docker_evidence,
    )

    verifier._assert_private_assets_preserved(commit_sha)

    first_path, first_content = SCRIPT.SYNTHETIC_PRIVATE_ASSETS[0]
    monkeypatch.setattr(SCRIPT, "SYNTHETIC_PRIVATE_ASSETS", ((first_path, first_content[:-1] + b"!"), *SCRIPT.SYNTHETIC_PRIVATE_ASSETS[1:]))
    with pytest.raises(SCRIPT.AcceptanceError, match="private asset digest"):
        verifier._assert_private_assets_preserved(commit_sha)


def test_terminal_receipt_validation_checks_integrity_image_freshness_and_cleanup(acceptance_root: Path) -> None:
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=_docker_evidence,
    )
    run = _terminal_run("passed")

    verifier._validate_terminal_run(run, expected_status="passed")
    verifier._assert_no_run_residue("atr-contract")

    tampered = dict(run)
    raw_receipt = run["receipt"]
    assert isinstance(raw_receipt, dict)
    tampered_receipt = dict(raw_receipt)
    tampered_receipt["receipt_digest"] = "0" * 64
    tampered["receipt"] = tampered_receipt
    with pytest.raises(SCRIPT.AcceptanceError, match="receipt digest"):
        verifier._validate_terminal_run(tampered, expected_status="passed")


def test_terminal_validation_requires_timeout_code_and_rejects_image_or_container_residue(acceptance_root: Path) -> None:
    timeout_run = _terminal_run("error", error_code="AGENT_TEST_RUN_TIMEOUT")
    stale_image = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=lambda _command: "stale-acceptance",
    )
    with pytest.raises(SCRIPT.AcceptanceError, match="current acceptance run"):
        stale_image._validate_terminal_run(
            timeout_run,
            expected_status="error",
            expected_error_code="AGENT_TEST_RUN_TIMEOUT",
        )

    residue = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=lambda command: "container-id" if command[1:3] == ["ps", "-aq"] else "acceptance-current",
    )
    residue._validate_terminal_run(
        timeout_run,
        expected_status="error",
        expected_error_code="AGENT_TEST_RUN_TIMEOUT",
    )
    with pytest.raises(SCRIPT.AcceptanceError, match="label residue"):
        residue._assert_no_run_residue("atr-contract")


def test_cancel_scenario_waits_for_running_then_validates_terminal_receipt(acceptance_root: Path) -> None:
    terminal = _terminal_run(
        "cancelled",
        test_run_id="atr-cancel",
        agent_id="agent-cancel",
        commit_sha="9" * 40,
    )
    calls: list[tuple[str, str]] = []
    queued_gets = iter(({"status": "running"}, terminal))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/agent-test-runs":
            return httpx.Response(202, json={"test_run_id": "atr-cancel"})
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "running"})
        if request.method == "GET" and request.url.path == "/api/agent-test-runs/atr-cancel":
            return httpx.Response(200, json=next(queued_gets))
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler))
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=client,
        docker_runner=_docker_evidence,
        sleep=lambda _seconds: None,
    )

    result = verifier._execute_cancelled_run("agent-cancel", "9" * 40)

    assert result["status"] == "cancelled"
    assert calls == [
        ("POST", "/api/agent-test-runs"),
        ("GET", "/api/agent-test-runs/atr-cancel"),
        ("POST", "/api/agent-test-runs/atr-cancel/cancel"),
        ("GET", "/api/agent-test-runs/atr-cancel"),
    ]


def test_source_changed_scenario_mutates_real_run_projection_and_cannot_be_passed(acceptance_root: Path) -> None:
    test_run_id = "atr-source-changed"
    agent_id = "agent-source-changed"
    commit_sha = "6" * 40
    terminal = _terminal_run(
        "error",
        test_run_id=test_run_id,
        agent_id=agent_id,
        commit_sha=commit_sha,
        error_code="AGENT_TEST_SOURCE_CHANGED",
        source_observation="changed",
    )
    api_evidence = SourceChangedApiEvidence(test_run_id, commit_sha, terminal)
    docker_evidence = SourceChangedDockerEvidence(
        SCRIPT.SANDBOX_RUN_LABEL,
        SCRIPT.SOURCE_CHANGED_PROJECTED_BYTES,
    )

    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(api_evidence)),
        docker_runner=docker_evidence,
        sleep=lambda _seconds: None,
    )

    run = verifier._execute_source_changed_run(agent_id, commit_sha)

    assert run["status"] == "error"
    assert any(command[1] == "exec" for command in docker_evidence.calls)
    assert all(str(acceptance_root) not in " ".join(command) for command in docker_evidence.calls)
    assert ("GET", "/api/agent-test-runs/history") in api_evidence.calls


def test_run_residue_audit_uses_worker_only_source_root(acceptance_root: Path) -> None:
    present = False

    def docker_runner(command: list[str]) -> str:
        if command[1] == "exec":
            return "present" if present else "absent"
        return _docker_evidence(command)

    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=docker_runner,
    )
    legacy = acceptance_root / "volumes/data" / ".agent-testing" / "runs" / "atr-residue"
    legacy.mkdir(parents=True)

    verifier._assert_no_run_residue("atr-residue")

    present = True
    with pytest.raises(SCRIPT.AcceptanceError, match="temporary run directory"):
        verifier._assert_no_run_residue("atr-residue")


def test_verify_orchestrates_bootstrap_hostile_failure_cancel_timeout_and_source_change(
    acceptance_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=httpx.Client(base_url="http://test.invalid"),
        docker_runner=_docker_evidence,
    )
    events: list[tuple[str, str]] = []
    imported_commits = iter(("2" * 40, "3" * 40, "4" * 40, "6" * 40))

    monkeypatch.setattr(
        SCRIPT.acceptance_support,
        "prove_runtime_bootstrap_not_mounted",
        lambda **_kwargs: events.append(("authority", "bootstrap")),
    )
    monkeypatch.setattr(
        SCRIPT.acceptance_support,
        "prove_worker_runtime_authority",
        lambda **_kwargs: events.append(("authority", "worker")),
    )
    monkeypatch.setattr(verifier, "_request_json", lambda *_args, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(verifier, "_current_commit", lambda _agent_id: "1" * 40)
    monkeypatch.setattr(
        verifier,
        "_inspect_suite",
        lambda _agent_id, _commit: {
            "test_file_count": 1,
            "requires_live_agent": False,
            "suite_digest": "5" * 64,
        },
    )
    monkeypatch.setattr(verifier, "_create_host_marker", lambda: acceptance_root / "host-marker")
    monkeypatch.setattr(verifier, "_assert_private_assets_preserved", lambda _commit: events.append(("export", "private-assets")))

    def import_workspace(**kwargs: object) -> str:
        events.append(("import", str(kwargs["test_filename"])))
        return next(imported_commits)

    def execute_run(_agent_id: str, _commit: str, *, expected_status: str, expected_error_code: str | None = None) -> dict[str, object]:
        events.append(("run", f"{expected_status}:{expected_error_code or '-'}"))
        verifier._run_ids.append(f"atr-{len(verifier._run_ids)}")
        items = [{"nodeid": nodeid} for nodeid in SCRIPT.HOSTILE_NODEIDS]
        return {"suite_digest": "5" * 64, "items": items, "report": {"items": items}}

    def execute_cancelled(_agent_id: str, _commit: str) -> dict[str, object]:
        events.append(("run", "cancelled:-"))
        verifier._run_ids.append(f"atr-{len(verifier._run_ids)}")
        return {"status": "cancelled"}

    def execute_source_changed(_agent_id: str, _commit: str) -> dict[str, object]:
        events.append(("run", "error:AGENT_TEST_SOURCE_CHANGED"))
        verifier._run_ids.append(f"atr-{len(verifier._run_ids)}")
        return {"status": "error"}

    monkeypatch.setattr(verifier, "_import_workspace", import_workspace)
    monkeypatch.setattr(verifier, "_execute_run", execute_run)
    monkeypatch.setattr(verifier, "_execute_cancelled_run", execute_cancelled)
    monkeypatch.setattr(verifier, "_execute_source_changed_run", execute_source_changed)

    result = verifier.verify()

    assert result == SCRIPT.VerificationSummary(runs=6, terminal_contracts=6, temporary_agents=1)
    assert events == [
        ("authority", "bootstrap"),
        ("authority", "worker"),
        ("run", "passed:-"),
        ("import", "test_isolation.py"),
        ("export", "private-assets"),
        ("run", "passed:-"),
        ("import", "test_failure.py"),
        ("run", "failed:-"),
        ("import", "test_slow.py"),
        ("run", "cancelled:-"),
        ("run", "error:AGENT_TEST_RUN_TIMEOUT"),
        ("import", "test_source_change.py"),
        ("run", "error:AGENT_TEST_SOURCE_CHANGED"),
    ]


def test_cleanup_cancels_active_run_removes_agent_and_host_marker(acceptance_root: Path) -> None:
    marker = acceptance_root / "host-marker"
    marker.touch()
    statuses = iter(({"status": "running"}, {"status": "cancelled"}))
    calls: list[tuple[str, str]] = []
    delete_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/atr-cleanup"):
            return httpx.Response(200, json=next(statuses))
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "running"})
        if request.method == "DELETE" and request.url.path.startswith("/api/agent-registry/"):
            delete_headers.update(request.headers)
            return httpx.Response(
                200,
                json={
                    "operation_id": "adop-00000000-0000-4000-8000-000000000001",
                    "state": "completed",
                    "cleanup_complete": True,
                    "workspace_removed": True,
                    "last_error_code": None,
                    "attempt_count": 1,
                    "updated_at": "2026-08-09T00:00:01Z",
                    "deleted": {"agent_id": verifier._temporary_agent_id},
                    "impact": {},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler))
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=client,
        docker_runner=_docker_evidence,
        sleep=lambda _seconds: None,
    )
    verifier._run_ids.append("atr-cleanup")
    verifier._temporary_agent_touched = True
    verifier._temporary_agent_created = True
    verifier._temporary_agent_instance_etag = "a" * 64
    verifier._host_marker = marker

    errors = verifier.cleanup()

    assert errors == ()
    assert not marker.exists()
    assert ("POST", "/api/agent-test-runs/atr-cleanup/cancel") in calls
    assert any(method == "DELETE" for method, _path in calls)
    assert delete_headers["if-match"] == f'"{"a" * 64}"'
    assert delete_headers["idempotency-key"] == f"agent-delete:{'a' * 64}"


def test_cleanup_polls_pending_durable_deletion_location(acceptance_root: Path) -> None:
    operation_id = "adop-00000000-0000-4000-8000-000000000002"
    location = f"/api/agent-deletion-operations/{operation_id}"
    calls: list[tuple[str, str]] = []

    def receipt(state: str) -> dict[str, object]:
        completed = state == "completed"
        return {
            "operation_id": operation_id,
            "state": state,
            "cleanup_complete": completed,
            "workspace_removed": completed,
            "last_error_code": None if completed else "AGENT_DELETION_FILESYSTEM_FENCE",
            "attempt_count": 2 if completed else 1,
            "updated_at": "2026-08-09T00:00:02Z",
            "deleted": {"agent_id": verifier._temporary_agent_id},
            "impact": {},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(202, headers={"Location": location}, json=receipt("cleanup_pending"))
        if request.method == "GET" and request.url.path == location:
            return httpx.Response(200, json=receipt("completed"))
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler))
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=client,
        docker_runner=_docker_evidence,
        sleep=lambda _seconds: None,
    )
    verifier._temporary_agent_created = True
    verifier._temporary_agent_instance_etag = "b" * 64

    assert verifier.cleanup() == ()
    assert calls == [
        ("DELETE", f"/api/agent-registry/{verifier._temporary_agent_id}"),
        ("GET", location),
    ]


def test_cleanup_rejects_internal_deletion_evidence_without_echoing_it(acceptance_root: Path) -> None:
    private_evidence = "/data/business-agents/private-agent/workspace"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(
            200,
            json={
                "operation_id": "adop-00000000-0000-4000-8000-000000000003",
                "state": "completed",
                "cleanup_complete": True,
                "workspace_removed": True,
                "last_error_code": None,
                "attempt_count": 1,
                "updated_at": "2026-08-09T00:00:03Z",
                "deleted": {"agent_id": verifier._temporary_agent_id},
                "impact": {},
                "quarantine_path": private_evidence,
            },
        )

    client = httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(handler))
    verifier = SCRIPT.AgentTestContainerAcceptance(
        _settings(acceptance_root),
        client=client,
        docker_runner=_docker_evidence,
    )
    verifier._temporary_agent_created = True
    verifier._temporary_agent_instance_etag = "c" * 64

    errors = verifier.cleanup()

    assert errors == ("delete_agent:DurableAgentCleanupError",)
    assert private_evidence not in str(errors)
