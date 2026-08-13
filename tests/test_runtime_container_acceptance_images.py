from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from runtime_container_acceptance_test_support import image_evidence, load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = load_module(
    "agentgov_container_acceptance_images_tests",
    REPO_ROOT / "scripts/run_container_acceptance.py",
)
CANDIDATE = RUNNER.acceptance_support.AcceptanceCandidateIdentity("a" * 40, "b" * 64)


def test_isolated_health_shell_is_diagnostics_only_and_overlay_binds_candidate() -> None:
    shell = (REPO_ROOT / "scripts/run_healthcheck_container_e2e.sh").read_text(encoding="utf-8")
    forbidden = (" compose build", " compose up", " compose down", " compose rm", "docker rm", "rm -rf")
    assert all(command not in shell for command in forbidden)
    overlay = yaml.safe_load((REPO_ROOT / "docker/e2e/docker-compose.provider-health.yml").read_text(encoding="utf-8"))
    slow = overlay["services"]["slow-vllm"]
    expected = {
        "AGENT_GOV_ACCEPTANCE_RUN_ID": "${AGENT_GOV_ACCEPTANCE_RUN_ID:?managed run id required}",
        "AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE": "${AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE:?candidate tree required}",
        "AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256": "${AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256:?selected env digest required}",
    }
    assert slow["build"]["args"] == expected
    assert slow["labels"] == {
        "io.agentgov.acceptance-run-id": expected["AGENT_GOV_ACCEPTANCE_RUN_ID"],
        "io.agentgov.acceptance-candidate-tree": expected["AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE"],
        "io.agentgov.acceptance-selected-env-sha256": expected["AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256"],
    }


def test_langfuse_binds_candidate_and_external_runtime_image_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = RUNNER.PROFILES["langfuse"]
    env = {
        RUNNER.RUN_ID_ENV: "run-current",
        RUNNER.acceptance_toolchain.DOCKER_EXECUTABLE_ENV: "/usr/bin/docker",
    }
    commands: list[list[str]] = []
    verified: list[tuple[str, str, bool]] = []
    candidates = tuple(
        RUNNER.acceptance_support.LocalImageEvidence(service, f"sha256:{index:064x}") for index, service in enumerate(profile.build_services, start=1)
    )
    externals = tuple(
        RUNNER.acceptance_support.LocalImageEvidence(service, f"sha256:{index + 20:064x}", "external-runtime")
        for index, service in enumerate(profile.external_image_services, start=1)
    )
    monkeypatch.setattr(RUNNER, "_validate_service_model", lambda *_args: None)
    monkeypatch.setattr(RUNNER, "_run_volume_initializer", lambda *_args: None)
    monkeypatch.setattr(RUNNER, "_run_checked", lambda command, **_kwargs: (commands.append(command), "")[1])
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_local_images", lambda **_kwargs: candidates)
    monkeypatch.setattr(RUNNER.acceptance_support, "capture_external_images", lambda **_kwargs: externals)

    def verify(*, service: str, expected_image_id: str | None, image_kind: str, **_kwargs: object) -> None:
        verified.append((service, image_kind, expected_image_id is not None))

    monkeypatch.setattr(RUNNER.acceptance_support, "verify_running_container", verify)
    actual = RUNNER.refresh_profile(profile, tmp_path / "selected.env", CANDIDATE, env)

    assert actual == tuple(sorted((*candidates, *externals), key=lambda item: item.service))
    assert {service for service, _kind, _bound in verified} == set(profile.expected_services)
    assert {service for service, kind, _bound in verified if kind == "external-runtime"} == set(profile.external_image_services)
    assert all(bound for _service, _kind, bound in verified)
    assert next(index for index, command in enumerate(commands) if "build" in command) < next(
        index for index, command in enumerate(commands) if "up" in command
    )


def test_external_runtime_image_drift_invalidates_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = RUNNER.PROFILES["langfuse"]
    captured = image_evidence(RUNNER, profile)
    drifted = tuple(
        RUNNER.acceptance_support.LocalImageEvidence(
            item.service,
            f"sha256:{'f' * 64}" if item.kind == "external-runtime" else item.image_id,
            item.kind,
        )
        for item in captured
    )
    monkeypatch.setattr(RUNNER, "_recapture_images", lambda *_args: drifted)
    context = SimpleNamespace(profile=profile, candidate=SimpleNamespace(env_file=tmp_path / "selected.env"))

    with pytest.raises(RUNNER.AcceptanceAuthorityError, match="cleanup镜像 authority 已漂移"):
        RUNNER._require_images_current(
            context,
            CANDIDATE,
            {},
            captured,
            phase="cleanup",
        )


def test_browser_runtime_drift_invalidates_postflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = RUNNER.PROFILES["core"]
    captured = image_evidence(RUNNER, profile)
    context = SimpleNamespace(profile=profile, candidate=SimpleNamespace(env_file=tmp_path / "selected.env"))
    monkeypatch.setattr(RUNNER, "_recapture_images", lambda *_args: captured)
    monkeypatch.setattr(
        RUNNER.acceptance_toolchain,
        "validate_browser_runtime_authority",
        lambda: (_ for _ in ()).throw(RUNNER.acceptance_toolchain.ToolchainAuthorityError("browser drift")),
    )

    with pytest.raises(RUNNER.acceptance_toolchain.ToolchainAuthorityError, match="browser drift"):
        RUNNER._require_images_current(context, CANDIDATE, {}, captured, phase="postflight")


def test_terminal_image_query_failure_closes_frozen_source_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    witness = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(RUNNER.acceptance_candidate, "freeze_candidate_source_current", lambda *_args, **_kwargs: witness)
    monkeypatch.setattr(
        RUNNER.acceptance_terminal.image_authority,
        "capture_terminal_image_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(RUNNER.acceptance_terminal.TerminalFreshnessError("docker failed")),
    )

    with pytest.raises(RUNNER.acceptance_terminal.TerminalFreshnessError, match="could not be captured"):
        RUNNER.acceptance_terminal.capture_terminal_freshness(
            SimpleNamespace(),
            compose_base=["/usr/bin/docker", "compose"],
            images=(),
            running_services=(),
            run_id="1700000000-a1b2c3d4e5f6",
            identity=CANDIDATE,
            docker_runner=lambda _command: "",
        )

    assert closed == [True]


def test_terminal_docker_query_uses_stable_cwd_and_minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class Process:
        returncode = 0

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            del timeout
            return "ok", ""

    def popen(command: list[str], **kwargs: object) -> Process:
        observed.update(command=command, **kwargs)
        return Process()

    managed = {
        RUNNER.ACTIVE_ENV: "1",
        RUNNER.acceptance_toolchain.DOCKER_EXECUTABLE_ENV: "/usr/bin/docker",
        "candidate-only": "/deleted",
    }
    monkeypatch.setattr(RUNNER.acceptance_contract, "terminal_process_environment", lambda _env: {"PATH": "/usr/bin"})
    monkeypatch.setattr(RUNNER.subprocess, "Popen", popen)

    result = RUNNER._run_process(
        ["/usr/bin/docker", "inspect"],
        cwd=Path("/deleted-candidate"),
        env=managed,
        capture=True,
        terminal_authority=True,
    )

    assert result == (0, "ok")
    assert observed["cwd"] == Path("/")
    assert observed["env"] == {"PATH": "/usr/bin"}

    with pytest.raises(RUNNER.AcceptanceError, match="固定 Docker"):
        RUNNER._run_process(
            ["/usr/bin/env"],
            cwd=Path("/deleted-candidate"),
            env=managed,
            capture=True,
            terminal_authority=True,
        )
