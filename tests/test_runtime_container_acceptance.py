from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from runtime_container_acceptance_test_support import candidate_authority, image_evidence, load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "scripts/run_container_acceptance.py"
HOOK_PATH = REPO_ROOT / ".codex/hooks/container_acceptance_guard.py"
MAIN_FLOW_PATH = REPO_ROOT / "scripts/run_main_flow_tests.py"
SPEECH_VERIFIER_PATH = REPO_ROOT / "scripts/verify_speech_summary_container.py"

RUNNER = load_module("agentgov_container_acceptance", RUNNER_PATH)
HOOK = load_module("agentgov_container_acceptance_hook", HOOK_PATH)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
MAIN_FLOW = load_module("agentgov_run_main_flow_tests", MAIN_FLOW_PATH)
SPEECH_VERIFIER = load_module("agentgov_speech_summary_container", SPEECH_VERIFIER_PATH)
CANDIDATE = RUNNER.candidate_authority.AcceptanceCandidateIdentity("a" * 40, "b" * 64)
CORE_VERIFIER = RUNNER.acceptance_contract.VERIFIER_REGISTRY["core"][0]


class _TerminalWitness:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("witness-close")


def _env(run_id: str = "1700000000-a1b2c3d4e5f6") -> dict[str, str]:
    return {
        RUNNER.RUN_ID_ENV: run_id,
        RUNNER.ACTIVE_ENV: "1",
        RUNNER.acceptance_toolchain.DOCKER_EXECUTABLE_ENV: "/usr/bin/docker",
    }


def _managed_process_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    runtime = Path("/candidate/runtime")
    values = {
        RUNNER.ACTIVE_ENV: "1",
        RUNNER.RUNTIME_ROOT_ENV: str(runtime),
        "HOME": str(runtime / "home"),
        "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
        "BUILDX_CONFIG": str(runtime / "buildx"),
        "TMPDIR": str(runtime / "tmp"),
        "VERIFY_SCREENSHOT_DIR": str(runtime / "screenshots"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        RUNNER.acceptance_toolchain.FRONTEND_DEPENDENCY_ROOT_ENV: "/candidate/repository/frontend/node_modules",
        RUNNER.acceptance_toolchain.PYTHON_SITE_PACKAGES_ENV: "/candidate/dependencies/python-site-packages",
        RUNNER.acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV: "/candidate/dependencies/pnpm",
        RUNNER.acceptance_toolchain.TOOLCHAIN_SHA256_ENV: "c" * 64,
    }
    for prefix in ("FRONTEND", "PYTHON", "PNPM"):
        values[f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_SHA256"] = "d" * 64
        values[f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_ENTRIES"] = "1"
        values[f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_BYTES"] = "1"
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "toolchain_sha256", lambda: "c" * 64)
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_execution_tool_authority", lambda _environ: None)
    values[RUNNER.acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV] = RUNNER.acceptance_contract.managed_environment_sha256(values)
    return values


def test_process_environment_accepts_and_strips_exact_gate_spawn_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    environment = _managed_process_environment(monkeypatch)
    gate = RUNNER.acceptance_verifier_process.acceptance_make_gate
    environment.update(
        {
            gate.MAKE_GATE_FD_ENV: "9",
            gate.MAKE_GATE_NONCE_ENV: "a" * 32,
            "PWD": "/candidate/repository",
        }
    )

    boundary = RUNNER._process_boundary(Path("/candidate/repository"), environment)
    assert boundary.cwd == Path("/candidate/repository")
    assert boundary.environment is environment


@pytest.mark.parametrize(
    "overlay",
    (
        {"AGENT_GOV_ACCEPTANCE_MAKE_GATE_FD": "9", "PWD": "/candidate/repository"},
        {"AGENT_GOV_ACCEPTANCE_MAKE_GATE_NONCE": "a" * 32, "PWD": "/candidate/repository"},
        {
            "AGENT_GOV_ACCEPTANCE_MAKE_GATE_FD": "9",
            "AGENT_GOV_ACCEPTANCE_MAKE_GATE_NONCE": "a" * 32,
        },
        {"PWD": "/candidate/repository"},
    ),
)
def test_process_environment_rejects_partial_gate_spawn_overlay(
    monkeypatch: pytest.MonkeyPatch,
    overlay: dict[str, str],
) -> None:
    environment = _managed_process_environment(monkeypatch)
    environment.update(overlay)

    with pytest.raises(RUNNER.AcceptanceError, match="environment authority|环境 authority"):
        RUNNER._process_boundary(Path("/candidate/repository"), environment)


def test_profiles_and_compose_model_are_exact() -> None:
    core = RUNNER.PROFILES["core"]
    langfuse = RUNNER.PROFILES["langfuse"]
    health = RUNNER.PROFILES["isolated-health"]
    agent_test = RUNNER.PROFILES["agent-test"]
    command = RUNNER.compose_command(langfuse, Path("/candidate/selected.env"), _env())

    assert core.expected_services == RUNNER.CORE_SERVICES
    assert core.build_services == RUNNER.CORE_BUILD_SERVICES
    assert set(langfuse.expected_services) == set((*RUNNER.CORE_SERVICES, *RUNNER.LANGFUSE_SERVICES))
    assert health.build_services == health.expected_services == RUNNER.acceptance_profiles.ISOLATED_HEALTH_SERVICES
    assert health.compose_overlays == ("docker/e2e/docker-compose.provider-health.yml",)
    assert agent_test.services_to_run == RUNNER.CORE_SERVICES[:3]
    assert command[:4] == ["/usr/bin/docker", "compose", "--parallel", "3"]
    assert command[-2:] == ["--profile", "langfuse"]


def test_service_model_forbids_temporary_runtime_bootstrap_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = RUNNER.PROFILES["core"]
    model = {
        "services": {
            "claude-agent-api": {
                "volumes": [
                    {"type": "bind", "source": "/persistent/data", "target": "/data"},
                ]
            }
        }
    }

    def fake_run(command: list[str], **_kwargs: object) -> str:
        return "\n".join(profile.expected_services) if "--services" in command else json.dumps(model)

    monkeypatch.setattr(RUNNER, "_run_checked", fake_run)
    RUNNER._validate_service_model(["/usr/bin/docker", "compose"], profile, _env())

    model["services"]["claude-agent-api"]["volumes"].append({"type": "bind", "source": "/temporary/candidate", "target": "/app/docker/runtime-bootstrap"})
    with pytest.raises(RUNNER.AcceptanceAuthorityError, match="不得覆盖"):
        RUNNER._validate_service_model(["/usr/bin/docker", "compose"], profile, _env())


def test_refresh_builds_before_force_recreate_and_binds_running_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = RUNNER.PROFILES["core"]
    commands: list[list[str]] = []
    verified: list[tuple[str, str | None]] = []
    images = image_evidence(RUNNER, profile)
    monkeypatch.setattr(RUNNER, "_validate_service_model", lambda *_args: None)

    def fake_run(command: list[str], **_kwargs: object) -> str:
        commands.append(command)
        return ""

    def verify_running(*, service: str, expected_image_id: str | None, **_kwargs: object) -> None:
        verified.append((service, expected_image_id))

    monkeypatch.setattr(RUNNER, "_run_checked", fake_run)
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_external_images", lambda **_kwargs: ())
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_local_images", lambda **_kwargs: images)
    monkeypatch.setattr(RUNNER.acceptance_support, "verify_running_container", verify_running)

    actual = RUNNER.refresh_profile(profile, Path("/candidate/selected.env"), CANDIDATE, _env())

    build_index = next(index for index, command in enumerate(commands) if "build" in command)
    up_index = next(index for index, command in enumerate(commands) if "up" in command)
    assert build_index < up_index
    assert commands[up_index][commands[up_index].index("up") :] == [
        "up",
        "-d",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "--remove-orphans",
        *profile.services_to_run,
    ]
    assert verified == [(service, next(item.image_id for item in images if item.service == service)) for service in profile.services_to_run]
    assert actual == tuple(sorted(images, key=lambda item: item.service))


def test_langfuse_initializer_uses_sealed_exact_image_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = RUNNER.PROFILES["langfuse"]
    events: list[str] = []
    init_image = f"sha256:{'f' * 64}"
    external = tuple(
        RUNNER.acceptance_support.LocalImageEvidence(service, f"sha256:{index:064x}", "external-runtime")
        for index, service in enumerate(profile.external_image_services, start=20)
    )
    candidate_images = image_evidence(RUNNER, RUNNER.PROFILES["core"])
    monkeypatch.setattr(RUNNER, "_validate_service_model", lambda *_args: None)

    def capture_external(*, services: tuple[str, ...], **_kwargs: object) -> tuple[object, ...]:
        if services == (profile.volume_init_service,):
            events.append("capture-init")
            return (RUNNER.acceptance_support.LocalImageEvidence(services[0], init_image, "external-runtime"),)
        events.append("capture-external")
        return external

    def fake_run(command: list[str], **kwargs: object) -> str:
        if "config" in command and any(item.startswith("/proc/self/fd/") for item in command):
            events.append("pinned-config")
            assert kwargs["pass_fds"]
            return json.dumps({"services": {profile.volume_init_service: {"image": init_image, "pull_policy": "never"}}})
        if "run" in command:
            events.append("initializer")
            assert command[command.index("--pull") + 1] == "never"
            assert kwargs["pass_fds"]
        elif "build" in command:
            events.append("build")
        elif "up" in command:
            events.append("up")
        return ""

    monkeypatch.setattr(RUNNER, "_run_checked", fake_run)
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_external_images", capture_external)
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_local_images", lambda **_kwargs: candidate_images)
    monkeypatch.setattr(RUNNER.acceptance_support, "verify_running_container", lambda **_kwargs: None)

    images = RUNNER.refresh_profile(profile, Path("/candidate/selected.env"), CANDIDATE, _env())

    assert events[:5] == ["capture-external", "capture-init", "pinned-config", "initializer", "capture-external"]
    assert events.index("initializer") < events.index("build") < events.index("up")
    assert tuple(item for item in images if item.kind == "external-runtime") == tuple(sorted(external, key=lambda item: item.service))


def test_sealed_initializer_overlay_cannot_be_rewritten() -> None:
    image_id = f"sha256:{'a' * 64}"

    with RUNNER._sealed_image_overlay("langfuse-volume-init", image_id) as (path, descriptor):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"{}", 0)

    assert payload["services"]["langfuse-volume-init"] == {
        "image": image_id,
        "pull_policy": "never",
    }


def test_dependency_identity_mismatch_is_rejected_before_subprocess() -> None:
    candidate = candidate_authority(RUNNER)
    managed = RUNNER._dependency_values(candidate)
    managed[RUNNER.acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV] = "/redirected/dependencies/pnpm"

    with pytest.raises(RUNNER.AcceptanceError, match="dependency identity"):
        RUNNER._validate_dependency_identity(managed, candidate)


def test_resume_uses_lightweight_execution_tool_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = candidate_authority(RUNNER, profile="core")
    calls: list[str] = []
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_execution_tool_authority", lambda _managed: calls.append("execution"))
    monkeypatch.setattr(
        RUNNER.acceptance_toolchain,
        "validate_toolchain_authority",
        lambda *_args: pytest.fail("resume must not rescan baseline dependency trees"),
    )
    monkeypatch.setattr(RUNNER.acceptance_candidate, "verify_candidate_snapshot", lambda *_args, **_kwargs: None)
    verifier = RUNNER.acceptance_contract.VERIFIER_REGISTRY["core"][0]
    args = SimpleNamespace(profile="core", command=list(verifier.invocation_argv))
    receipt = SimpleNamespace(identity=SimpleNamespace(reserved=SimpleNamespace(candidate_reservation=object())))

    with pytest.raises(RUNNER.AcceptanceError, match="identity 不一致"):
        RUNNER._validate_resume_identity(args, {}, {}, candidate, receipt)

    assert calls == ["execution"]


def test_resume_rejects_receipt_lock_mismatch_before_any_snapshot_action(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    candidate = candidate_authority(RUNNER, profile="core")
    lock = SimpleNamespace(descriptor=17, close=lambda: events.append("lock-close"))

    def reject_lock(_descriptor: int) -> None:
        events.append("lock-check")
        raise RUNNER.acceptance_support.AcceptanceSupportError("lock mismatch")

    receipt = SimpleNamespace(verify_lifecycle_lock=reject_lock)
    transport = {
        RUNNER.candidate_authority.PREPARED_CANDIDATE_AUTHORITY_ENV: "candidate",
        RUNNER.acceptance_receipt.PREPARED_RECEIPT_AUTHORITY_ENV: "receipt",
    }
    monkeypatch.setattr(RUNNER.acceptance_contract, "split_reexec_environment", lambda _environ: (transport, {}))
    monkeypatch.setattr(RUNNER.candidate_authority.PreparedCandidateAuthority, "from_json", lambda _raw: candidate)
    monkeypatch.setattr(RUNNER.acceptance_receipt.PreparedReceiptAuthority, "from_json", lambda _raw: receipt)
    monkeypatch.setattr(RUNNER.acceptance_lock, "acquire_lifecycle_lock", lambda *_args, **_kwargs: lock)
    monkeypatch.setattr(RUNNER, "_validate_resume_identity", lambda *_args: pytest.fail("snapshot validation must not run"))
    monkeypatch.setattr(RUNNER.acceptance_environment, "open_prepared_runtime", lambda *_args: pytest.fail("runtime must not open"))

    with pytest.raises(RUNNER.acceptance_support.AcceptanceSupportError, match="lock mismatch"):
        RUNNER._resume_context(SimpleNamespace(), {})

    assert events == ["lock-check", "lock-close"]


def _fake_context(profile_name: str = "core") -> object:
    candidate = candidate_authority(RUNNER, profile=profile_name)
    return SimpleNamespace(
        profile=RUNNER.PROFILES[profile_name],
        verifier=RUNNER.acceptance_contract.VERIFIER_REGISTRY[profile_name][0],
        candidate=candidate,
        receipt=object(),
        managed=_env(candidate.snapshot.run_id),
        lock=SimpleNamespace(assert_current=lambda: None),
        runtime=object(),
    )


def test_stale_source_blocks_build_then_exact_cleanup_terminalizes_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    terminal: dict[str, object] = {}
    context = _fake_context()

    def stale(_candidate: object) -> None:
        events.append("source-failed")
        raise RUNNER.AcceptanceAuthorityError("stale")

    monkeypatch.setattr(RUNNER, "_require_source_current", stale)
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: events.append("refresh"))
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: events.append("runtime-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_candidate, "cleanup_candidate_snapshot", lambda *_args, **_kwargs: events.append("candidate-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", lambda _requirement: None)

    def publish(*_args: object, **kwargs: object) -> tuple[Path, str]:
        terminal.update(kwargs)
        events.append("terminal")
        return Path("receipt"), "d" * 64

    monkeypatch.setattr(RUNNER.acceptance_receipt, "transition_receipt", publish)

    with pytest.raises(RUNNER.AcceptanceAuthorityError) as captured:
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert RUNNER._safe_failure_phase(captured.value) == "source_preflight"
    assert "refresh" not in events
    assert terminal["status"] == "failed"
    assert events.index("runtime-cleanup") < events.index("candidate-cleanup") < events.index("terminal")
    assert events[:2] == ["source-failed", "runtime-cleanup"]


def test_image_drift_exact_cleanup_terminalizes_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    images = image_evidence(RUNNER, context.profile)
    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: events.append("source"))
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: images)
    monkeypatch.setattr(RUNNER.acceptance_verifier_process, "run_verifier_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(RUNNER.acceptance_contract, "verifier_environment", lambda env: env)
    monkeypatch.setattr(
        RUNNER,
        "_require_images_current",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RUNNER.AcceptanceAuthorityError("image drift")),
    )
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: events.append("runtime-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_candidate, "cleanup_candidate_snapshot", lambda *_args, **_kwargs: events.append("candidate-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", lambda _requirement: None)
    monkeypatch.setattr(
        RUNNER.acceptance_receipt,
        "transition_receipt",
        lambda *_args, **kwargs: (events.append(str(kwargs["status"])), (Path("receipt"), "d" * 64))[1],
    )

    with pytest.raises(RUNNER.AcceptanceAuthorityError, match="image drift"):
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert events[-3:] == ["runtime-cleanup", "candidate-cleanup", "failed"]


def test_cleanup_browser_drift_terminalizes_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    images = image_evidence(RUNNER, context.profile)
    checks = iter((None, RUNNER.acceptance_toolchain.ToolchainAuthorityError("browser drift")))
    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: None)
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: images)
    monkeypatch.setattr(RUNNER.acceptance_verifier_process, "run_verifier_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        RUNNER,
        "_require_images_current",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error) if (error := next(checks)) is not None else None,
    )
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: events.append("runtime-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_candidate, "cleanup_candidate_snapshot", lambda *_args, **_kwargs: events.append("candidate-cleanup"))
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", lambda _requirement: None)
    monkeypatch.setattr(
        RUNNER.acceptance_receipt,
        "transition_receipt",
        lambda *_args, **kwargs: (events.append(str(kwargs["status"])), (Path("receipt"), "d" * 64))[1],
    )

    with pytest.raises(RUNNER.acceptance_toolchain.ToolchainAuthorityError, match="browser drift") as captured:
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert RUNNER._safe_failure_phase(captured.value) == "cleanup_freshness"
    assert events == ["runtime-cleanup", "candidate-cleanup", "failed"]


def test_cleanup_failure_never_terminalizes_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    images = image_evidence(RUNNER, context.profile)
    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: events.append("source"))
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: images)
    monkeypatch.setattr(RUNNER.acceptance_verifier_process, "run_verifier_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(RUNNER, "_require_images_current", lambda *_args, **_kwargs: events.append("images"))

    def fail_cleanup(*_args: object) -> None:
        events.append("cleanup-failed")
        raise RUNNER.AcceptanceError("cleanup failed")

    monkeypatch.setattr(RUNNER, "_cleanup_runtime", fail_cleanup)
    monkeypatch.setattr(RUNNER.acceptance_receipt, "transition_receipt", lambda *_args, **_kwargs: events.append("terminal"))

    with pytest.raises(RUNNER.AcceptanceError, match="cleanup failed"):
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert "terminal" not in events


def test_candidate_snapshot_cleanup_failure_never_terminalizes_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: None)
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: ())
    monkeypatch.setattr(RUNNER.acceptance_verifier_process, "run_verifier_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(RUNNER.acceptance_contract, "verifier_environment", lambda env: env)
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: None)
    monkeypatch.setattr(
        RUNNER.acceptance_candidate,
        "cleanup_candidate_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RUNNER.acceptance_candidate.CandidateSnapshotError("identity drift")),
    )
    monkeypatch.setattr(RUNNER.acceptance_receipt, "transition_receipt", lambda *_args, **_kwargs: events.append("terminal"))

    with pytest.raises(RUNNER.acceptance_candidate.CandidateSnapshotError, match="identity drift"):
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert events == []


def _stub_terminal_witness_after_cleanup(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    outcome = SimpleNamespace(result=0, images=())
    witness = _TerminalWitness(events)
    monkeypatch.setattr(RUNNER.acceptance_terminal, "execute_acceptance", lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(
        RUNNER.acceptance_terminal,
        "cleanup_acceptance",
        lambda *_args, **_kwargs: (None, witness),
    )
    monkeypatch.setattr(
        RUNNER.acceptance_receipt,
        "transition_receipt",
        lambda *_args, **_kwargs: events.append("terminal"),
    )


def test_terminal_witness_closes_when_lifecycle_lock_drift_blocks_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    _stub_terminal_witness_after_cleanup(monkeypatch, events)

    def reject_lock() -> None:
        events.append("lock-check")
        raise RUNNER.acceptance_support.AcceptanceSupportError("lock drift")

    context.lock.assert_current = reject_lock
    monkeypatch.setattr(
        RUNNER.acceptance_toolchain,
        "receipt_root_requirement",
        lambda: pytest.fail("receipt root must not run after lock drift"),
    )

    with pytest.raises(RUNNER.acceptance_support.AcceptanceSupportError, match="lock drift"):
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert events == ["lock-check", "witness-close"]


def test_terminal_witness_closes_when_receipt_root_drift_blocks_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _fake_context()
    _stub_terminal_witness_after_cleanup(monkeypatch, events)
    context.lock.assert_current = lambda: events.append("lock-check")
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())

    def reject_receipt_root(_requirement: object) -> None:
        events.append("receipt-root")
        raise RUNNER.acceptance_toolchain.ToolchainAuthorityError("receipt root drift")

    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", reject_receipt_root)

    with pytest.raises(RUNNER.acceptance_toolchain.ToolchainAuthorityError, match="receipt root drift"):
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())

    assert events == ["lock-check", "receipt-root", "witness-close"]


def test_terminal_is_published_only_after_runtime_and_candidate_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    running_sets: list[tuple[str, ...]] = []
    terminal_authority: list[bool] = []
    drifted = False
    context = _fake_context()
    images = image_evidence(RUNNER, context.profile)

    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: events.append("source"))
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: (events.append("refresh"), images)[1])
    monkeypatch.setattr(
        RUNNER.acceptance_verifier_process,
        "run_verifier_process",
        lambda *_args, **_kwargs: (events.append("child"), 0)[1],
    )
    monkeypatch.setattr(RUNNER.acceptance_contract, "verifier_environment", lambda env: env)
    monkeypatch.setattr(RUNNER, "_require_images_current", lambda *_args, **_kwargs: events.append("images"))
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: events.append("runtime-cleanup"))

    def cleanup(*_args: object, **_kwargs: object) -> None:
        nonlocal drifted
        events.append("candidate-cleanup")
        drifted = True

    monkeypatch.setattr(RUNNER.acceptance_candidate, "cleanup_candidate_snapshot", cleanup)

    def capture(candidate: object, **kwargs: object) -> object:
        running_sets.append(kwargs["running_services"])  # type: ignore[arg-type]
        return RUNNER.acceptance_terminal.TerminalFreshnessWitness(
            candidate,
            _TerminalWitness(events),
            (),
            "/usr/bin/docker",
            str(kwargs["run_id"]),
        )

    monkeypatch.setattr(RUNNER.acceptance_terminal, "capture_cleanup_witness", capture)
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_execution_tool_authority", lambda: events.append("tools"))
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_browser_runtime_authority", lambda: events.append("browser"))

    def verify_terminal_images(*_args: object, **kwargs: object) -> None:
        kwargs["docker_runner"](["/usr/bin/docker", "inspect"])  # type: ignore[index,operator]
        events.append("terminal-images")

    monkeypatch.setattr(RUNNER.acceptance_terminal.image_authority, "verify_terminal_image_evidence", verify_terminal_images)
    monkeypatch.setattr(
        RUNNER,
        "_run_checked",
        lambda *_args, **kwargs: (terminal_authority.append(bool(kwargs.get("terminal_authority"))), "")[1],
    )
    monkeypatch.setattr(
        RUNNER.acceptance_candidate,
        "require_frozen_candidate_source_current",
        lambda *_args, **_kwargs: events.append("source-semantic"),
    )
    monkeypatch.setattr(
        RUNNER.acceptance_toolchain,
        "validate_dependency_tree_generations",
        lambda: events.append("dependency-generation"),
    )

    def require_generation(*_args: object, **_kwargs: object) -> None:
        events.append("terminal-freshness")
        if drifted:
            raise RUNNER.acceptance_terminal.TerminalFreshnessError("source drift")

    monkeypatch.setattr(RUNNER.acceptance_candidate, "require_frozen_candidate_generation_current", require_generation)
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", lambda _requirement: None)
    monkeypatch.setattr(
        RUNNER.acceptance_receipt,
        "transition_receipt",
        lambda *_args, **kwargs: (events.append(str(kwargs["status"])), (Path("receipt"), "d" * 64))[1],
    )

    with pytest.raises(RUNNER.acceptance_terminal.TerminalFreshnessError, match="terminal freshness") as captured:
        RUNNER._execute_with_cleanup(context, RUNNER._SignalController())
    assert RUNNER._safe_failure_phase(captured.value) == "terminal_freshness"
    assert events.index("runtime-cleanup") < events.index("candidate-cleanup") < events.index("terminal-freshness") < events.index("failed")
    assert events.index("terminal-images") < events.index("source-semantic") < events.index("dependency-generation") < events.index("terminal-freshness")
    assert running_sets == [context.profile.services_to_run]
    assert terminal_authority == [True]
    drifted = False
    with pytest.raises(RUNNER.acceptance_terminal.TerminalFreshnessError):
        RUNNER._execute_with_cleanup(_fake_context("agent-test"), RUNNER._SignalController())
    assert running_sets[-1] == ()
    assert terminal_authority == [True, True]


def test_runner_failure_does_not_echo_captured_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "must-not-leak-private-value"

    class Process:
        returncode = 1
        stdout = None
        stderr = None

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            del timeout
            return sentinel, sentinel

    monkeypatch.setattr(RUNNER.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    with pytest.raises(RUNNER.AcceptanceError) as error:
        RUNNER._run_checked(["failing-command"], env={}, label="刷新", capture=True)

    assert sentinel not in str(error.value)


@pytest.mark.parametrize(
    "command",
    (
        "make _smoke",
        "python scripts/container_acceptance_bootstrap.py launch --profile core -- make --no-print-directory _smoke",
        "python -m scripts.container_acceptance_bootstrap launch --profile core -- make --no-print-directory _smoke",
        "python scripts/container_acceptance_toolchain.py launch --profile core -- make --no-print-directory _smoke",
        "python -m scripts.container_acceptance_toolchain launch --profile core -- make --no-print-directory _smoke",
        "bash scripts/run_healthcheck_container_e2e.sh",
        "python scripts/run_agent_test_container_e2e.py",
        "make _container-workspace-pytest-test",
        "make _ui-playground-cancel-smoke",
        "python -m pytest tests/test_live_runtime_acceptance.py",
        "bash -lc 'env SAFE=1 make _container-workspace-pytest-test'",
        "true\npython scripts/run_agent_test_container_e2e.py",
    ),
)
def test_pretool_guard_blocks_container_acceptance_bypasses(command: str) -> None:
    assert HOOK.bypass_reason(command)


@pytest.mark.parametrize(
    "command",
    (
        "make container-core-smoke",
        "make container-workspace-pytest-test",
        "bash -n scripts/run_healthcheck_container_e2e.sh",
        "make test",
        "pytest tests/test_runtime.py",
        "rg --files",
    ),
)
def test_pretool_guard_keeps_public_commands_available(command: str) -> None:
    assert HOOK.bypass_reason(command) is None


def test_codex_and_claude_pretool_hooks_use_shared_guard() -> None:
    codex = json.loads((REPO_ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))
    claude = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))

    for payload in (codex, claude):
        entries = payload["hooks"]["PreToolUse"]
        assert any(entry.get("matcher") == "Bash" and "container_acceptance_guard.py" in json.dumps(entry) for entry in entries)


def test_compose_and_images_carry_acceptance_labels() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    labels = {
        "io.agentgov.acceptance-run-id": "${AGENT_GOV_ACCEPTANCE_RUN_ID:-unmanaged}",
        "io.agentgov.acceptance-candidate-tree": "${AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE:-unmanaged}",
        "io.agentgov.acceptance-selected-env-sha256": "${AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256:-unmanaged}",
    }
    for service in (*RUNNER.CORE_SERVICES, *RUNNER.LANGFUSE_SERVICES):
        assert services[service]["labels"] == labels
    api_mounts = services["claude-agent-api"]["volumes"]
    assert all(
        not (isinstance(mount, dict) and mount.get("target") == "/app/docker/runtime-bootstrap") and "/app/docker/runtime-bootstrap" not in str(mount)
        for mount in api_mounts
    )
    assert "COPY docker/runtime-bootstrap /app/docker/runtime-bootstrap" in (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    slow_vllm = (REPO_ROOT / "docker/e2e/slow-vllm.Dockerfile").read_text(encoding="utf-8")
    assert "ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged" in slow_vllm
    assert 'LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}"' in slow_vllm


def test_frontend_real_container_scripts_expose_only_guarded_impls() -> None:
    package = json.loads((REPO_ROOT / "frontend/package.json").read_text(encoding="utf-8"))["scripts"]
    assert package["verify:real-container"] == "cd .. && make ui-feedback-smoke"
    assert package["verify:provider-health-container"] == "cd .. && make container-health-e2e"


def test_javascript_guard_rejects_unguarded_real_mode() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    module_uri = (REPO_ROOT / "scripts/container_acceptance_guard.mjs").as_uri()
    script = (
        f'import {{ requireContainerAcceptance }} from "{module_uri}";'
        "requireContainerAcceptance(false);"
        "try { requireContainerAcceptance(true); process.exit(9); } "
        "catch (error) { if (!String(error).includes('public Make target')) process.exit(8); }"
    )
    env = os.environ.copy()
    env.pop("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", None)
    env.pop("AGENT_GOV_ACCEPTANCE_RUN_ID", None)

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_host_main_flow_scrubs_container_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in MAIN_FLOW.CONTAINER_ACCEPTANCE_ENV_KEYS:
        monkeypatch.setenv(key, "must-be-removed")
    env = MAIN_FLOW._host_test_env()
    assert all(key not in env for key in MAIN_FLOW.CONTAINER_ACCEPTANCE_ENV_KEYS)


def test_speech_summary_verifier_requires_public_target() -> None:
    with pytest.raises(SPEECH_VERIFIER.AcceptanceError, match="container-speech-summary-test"):
        SPEECH_VERIFIER._require_container_acceptance({})


def test_speech_summary_verifier_keeps_each_surface_best_effort_but_requires_one_round_event() -> None:
    terminal_only = [SPEECH_VERIFIER.SseEvent(name="done", data={}, event_id=None)]
    sdk_success = [
        SPEECH_VERIFIER.SseEvent(name="agentgov.result", data={"errors": []}, event_id=None),
        SPEECH_VERIFIER.SseEvent(name="agentgov.done", data={}, event_id=None),
    ]
    chat_success = [
        SPEECH_VERIFIER.SseEvent(name="result", data={"errors": []}, event_id=None),
        SPEECH_VERIFIER.SseEvent(name="done", data={}, event_id=None),
    ]

    assert SPEECH_VERIFIER.RuntimeAcceptance._validate_speech(terminal_only, terminal_name="done") == 0
    SPEECH_VERIFIER.RuntimeAcceptance._require_sdk_success(sdk_success)
    SPEECH_VERIFIER.RuntimeAcceptance._require_chat_success(chat_success)
    with pytest.raises(SPEECH_VERIFIER.AcceptanceError, match="acceptance round emitted no"):
        SPEECH_VERIFIER.RuntimeAcceptance._require_any_speech(0)
    SPEECH_VERIFIER.RuntimeAcceptance._require_any_speech(1)

    with pytest.raises(SPEECH_VERIFIER.AcceptanceError, match="did not emit agentgov.result exactly once"):
        SPEECH_VERIFIER.RuntimeAcceptance._require_sdk_success(sdk_success[1:])
    with pytest.raises(SPEECH_VERIFIER.AcceptanceError, match="Chat stream emitted error"):
        SPEECH_VERIFIER.RuntimeAcceptance._require_chat_success(
            [
                SPEECH_VERIFIER.SseEvent(name="error", data={"errors": ["failed"]}, event_id=None),
                SPEECH_VERIFIER.SseEvent(name="done", data={}, event_id=None),
            ]
        )


def test_runner_failure_output_exposes_only_bounded_exception_types(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "private-path-and-secret-must-not-appear"
    failure = RUNNER.AcceptanceAuthorityError(secret)
    failure.__cause__ = OSError(secret)

    def fail(_args: object, _environ: object) -> int:
        raise failure

    monkeypatch.setattr(RUNNER, "parse_args", lambda _argv: SimpleNamespace())
    monkeypatch.setattr(RUNNER, "run_snapshot_acceptance", fail)

    assert RUNNER.main([]) == 1
    stderr = capsys.readouterr().err
    assert "failure_phase=resume" in stderr
    assert "failure_code=AcceptanceAuthorityError.OSError" in stderr
    assert secret not in stderr

    terminal_failure = RUNNER.acceptance_terminal.TerminalFreshnessError(secret)
    terminal_failure.__dict__[RUNNER._FAILURE_PHASE_ATTRIBUTE] = "terminal_freshness"
    monkeypatch.setattr(RUNNER, "run_snapshot_acceptance", lambda *_args: (_ for _ in ()).throw(terminal_failure))
    assert RUNNER.main([]) == 1
    terminal_stderr = capsys.readouterr().err
    assert "failure_phase=terminal_freshness failure_code=TerminalFreshnessError" in terminal_stderr
    assert secret not in terminal_stderr
