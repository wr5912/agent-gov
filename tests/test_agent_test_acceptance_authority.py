from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
import scripts.agent_test_acceptance_support as acceptance_support

REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = acceptance_support.AcceptanceCandidateIdentity(
    git_tree_sha="a" * 40,
    selected_env_sha256="b" * 64,
)
CONTAINER_ID = "c" * 64
REPLACEMENT_CONTAINER_ID = "d" * 64
IMAGE_ID = f"sha256:{'e' * 64}"
DRIFTED_IMAGE_ID = f"sha256:{'f' * 64}"
STARTED_AT = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _isolated_cleanup_model(project_name: str, *, runs_volume: bool) -> str:
    model: dict[str, object] = {
        "name": project_name,
        "networks": {"default": {"name": f"{project_name}_default", "driver": "bridge"}},
    }
    if runs_volume:
        model["volumes"] = {
            "agent-test-runs": {
                "name": f"{project_name}_agent-test-runs",
                "driver": "local",
            }
        }
    return json.dumps(model)


def test_isolated_health_cleanup_accepts_exact_model_without_agent_test_volume() -> None:
    project = "agentgov-acceptance-health123"
    commands: list[list[str]] = []

    def docker_runner(command: list[str]) -> str:
        commands.append(command)
        if command[1:3] == ["network", "ls"]:
            return ""
        raise AssertionError(command)

    acceptance_support.cleanup_isolated_compose_residue(
        compose_config=_isolated_cleanup_model(project, runs_volume=False),
        project_name=project,
        acceptance_run_id="run-current",
        expected_services=("claude-agent-api",),
        expect_runs_volume=False,
        docker_runner=docker_runner,
    )

    assert commands == [
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        ["docker", "network", "ls", "--format", "{{.Name}}"],
    ]


@pytest.mark.parametrize(
    ("runs_volume", "expect_runs_volume"),
    [(False, True), (True, False)],
)
def test_isolated_cleanup_rejects_profile_volume_model_mismatch(
    runs_volume: bool,
    expect_runs_volume: bool,
) -> None:
    project = "agentgov-acceptance-health123"

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="resources do not match"):
        acceptance_support.cleanup_isolated_compose_residue(
            compose_config=_isolated_cleanup_model(project, runs_volume=runs_volume),
            project_name=project,
            acceptance_run_id="run-current",
            expected_services=("claude-agent-api",),
            expect_runs_volume=expect_runs_volume,
            docker_runner=lambda command: (_ for _ in ()).throw(AssertionError(command)),
        )


def test_api_runtime_bootstrap_target_is_not_mounted() -> None:
    inspect = {
        "Id": "c" * 64,
        "Name": "/agentgov-api",
        "State": {"Running": True},
        "Config": {"Labels": {acceptance_support.ACCEPTANCE_IMAGE_LABEL: "run-current"}},
        "Mounts": [{"Destination": "/data", "Type": "bind"}],
    }

    def prove() -> None:
        acceptance_support.prove_runtime_bootstrap_not_mounted(
            api_container="agentgov-api",
            acceptance_run_id="run-current",
            docker_runner=lambda _command: json.dumps([inspect]),
        )

    prove()
    inspect["Mounts"] = [{"Destination": acceptance_support.RUNTIME_BOOTSTRAP_TARGET, "Type": "bind"}]
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="unexpectedly mounts"):
        prove()


def _image_authority_runner(
    references: dict[str, str],
    image_ids: dict[str, str],
    *,
    label_run_id: str = "run-current",
) -> tuple[acceptance_support.DockerRunner, list[list[str]]]:
    commands: list[list[str]] = []

    def docker_runner(command: list[str]) -> str:
        commands.append(command)
        assert "images" not in command
        if "config" in command:
            service = command[-1]
            return json.dumps({"services": {service: {"image": references[service]}}})
        if command[1:5] == ["image", "inspect", "--format", "{{.Id}}"]:
            return image_ids[command[-1]]
        template = command[3]
        if "acceptance-run-id" in template:
            return label_run_id
        if "acceptance-candidate-tree" in template:
            return CANDIDATE.git_tree_sha
        if "acceptance-selected-env-sha256" in template:
            return CANDIDATE.selected_env_sha256
        raise AssertionError(command)

    return docker_runner, commands


def _running_container_runner(
    *,
    container_ids: tuple[str, ...] = (CONTAINER_ID,),
    running_values: tuple[str, ...] = ("true",),
    image_ids: tuple[str, ...] = (IMAGE_ID,),
    created: str = "2026-08-12T12:00:00.123456789Z",
    label_run_id: str = "run-current",
) -> tuple[acceptance_support.DockerRunner, list[list[str]]]:
    commands: list[list[str]] = []
    offsets = {"container": 0, "running": 0, "image": 0}

    def next_value(name: str, values: tuple[str, ...]) -> str:
        offset = offsets[name]
        offsets[name] += 1
        return values[min(offset, len(values) - 1)]

    def docker_runner(command: list[str]) -> str:
        commands.append(command)
        if command[-3:-1] == ["ps", "-q"]:
            return next_value("container", container_ids)
        template = command[3]
        if template == "{{.State.Running}}":
            return next_value("running", running_values)
        if template == "{{.Created}}":
            return created
        if template == "{{.Image}}":
            return next_value("image", image_ids)
        if "acceptance-run-id" in template:
            return label_run_id
        if "acceptance-candidate-tree" in template:
            return CANDIDATE.git_tree_sha
        if "acceptance-selected-env-sha256" in template:
            return CANDIDATE.selected_env_sha256
        raise AssertionError(command)

    return docker_runner, commands


def _verify_running_container(
    docker_runner: acceptance_support.DockerRunner,
    *,
    started_at: datetime = STARTED_AT,
    expected_image_id: str | None = IMAGE_ID,
    image_kind: acceptance_support.ImageAuthorityKind = "external-runtime",
) -> None:
    acceptance_support.verify_running_container(
        compose_base=["/fixed/docker", "compose", "--env-file", "/selected/env"],
        service="langfuse-postgres",
        run_id="run-current",
        candidate=CANDIDATE,
        started_at=started_at,
        expected_image_id=expected_image_id,
        docker_runner=docker_runner,
        image_kind=image_kind,
    )


def test_candidate_image_capture_uses_compose_model_when_old_container_image_is_absent() -> None:
    image_id = f"sha256:{'d' * 64}"
    docker_runner, commands = _image_authority_runner({"claude-agent-api": "agent-gov-api:current"}, {"agent-gov-api:current": image_id})

    evidence = acceptance_support.capture_local_images(
        compose_base=["/fixed/docker", "compose", "--env-file", "/selected/env"],
        services=("claude-agent-api",),
        run_id="run-current",
        candidate=CANDIDATE,
        docker_runner=docker_runner,
    )
    assert evidence == (acceptance_support.LocalImageEvidence(service="claude-agent-api", image_id=image_id),)
    assert commands[:2] == [
        ["/fixed/docker", "compose", "--env-file", "/selected/env", "config", "--format", "json", "claude-agent-api"],
        ["/fixed/docker", "image", "inspect", "--format", "{{.Id}}", "agent-gov-api:current"],
    ]

    stale_runner, _commands = _image_authority_runner(
        {"claude-agent-api": "agent-gov-api:current"},
        {"agent-gov-api:current": image_id},
        label_run_id="run-current",
    )
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="current acceptance candidate"):
        acceptance_support.capture_local_images(
            compose_base=["/fixed/docker", "compose"],
            services=("claude-agent-api",),
            run_id="stale-run",
            candidate=CANDIDATE,
            docker_runner=stale_runner,
        )


def test_inactive_sandbox_and_shared_api_worker_resolve_exact_model_images() -> None:
    shared_id = f"sha256:{'a' * 64}"
    sandbox_id = f"sha256:{'b' * 64}"
    references = {
        "claude-agent-api": "agent-gov-api:current",
        "agent-test-worker": "agent-gov-api:current",
        "agent-test-sandbox-image": "agent-gov-test-sandbox:current",
    }
    runner, commands = _image_authority_runner(
        references,
        {"agent-gov-api:current": shared_id, "agent-gov-test-sandbox:current": sandbox_id},
    )

    candidates = acceptance_support.capture_local_images(
        compose_base=["/fixed/docker", "compose"],
        services=("claude-agent-api", "agent-test-worker", "agent-test-sandbox-image"),
        run_id="run-current",
        candidate=CANDIDATE,
        docker_runner=runner,
    )

    assert [(item.service, item.image_id) for item in candidates] == [
        ("claude-agent-api", shared_id),
        ("agent-test-worker", shared_id),
        ("agent-test-sandbox-image", sandbox_id),
    ]
    config_commands = [command for command in commands if "config" in command]
    assert [command[-1] for command in config_commands] == list(references)


@pytest.mark.parametrize(
    ("compose_base", "services"),
    [
        ([], ("claude-agent-api",)),
        (["docker", "compose"], ("claude-agent-api",)),
        (["/fixed/docker", "not-compose"], ("claude-agent-api",)),
        (["/fixed/docker", "compose"], ("bad service",)),
        (["/fixed/docker", "compose"], ("claude-agent-api", "claude-agent-api")),
    ],
)
def test_image_authority_rejects_invalid_base_and_service_set(compose_base: list[str], services: tuple[str, ...]) -> None:
    with pytest.raises(acceptance_support.AcceptanceSupportError):
        acceptance_support.capture_external_images(compose_base=compose_base, services=services, docker_runner=lambda _command: "")


@pytest.mark.parametrize(
    "model",
    [
        "not-json",
        json.dumps([]),
        json.dumps({}),
        json.dumps({"services": {"other": {"image": "agent-gov-api:current"}}}),
        json.dumps({"services": {"claude-agent-api": {}}}),
        json.dumps({"services": {"claude-agent-api": {"image": 7}}}),
        json.dumps({"services": {"claude-agent-api": {"image": "-invalid"}}}),
        json.dumps({"services": {"claude-agent-api": {"image": "invalid ref"}}}),
    ],
)
def test_image_authority_rejects_invalid_compose_model(model: str) -> None:
    def runner(command: list[str]) -> str:
        assert "images" not in command
        return model

    with pytest.raises(acceptance_support.AcceptanceSupportError):
        acceptance_support.capture_external_images(
            compose_base=["/fixed/docker", "compose"],
            services=("claude-agent-api",),
            docker_runner=runner,
        )


@pytest.mark.parametrize("image_output", ["", "sha256:short", f"sha256:{'c' * 64}\nsha256:{'d' * 64}"])
def test_image_authority_rejects_empty_invalid_or_ambiguous_image_id(image_output: str) -> None:
    def runner(command: list[str]) -> str:
        assert "images" not in command
        if "config" in command:
            return json.dumps({"services": {"agent-test-sandbox-image": {"image": "agent-gov-test-sandbox:current"}}})
        return image_output

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="digest is ambiguous"):
        acceptance_support.capture_external_images(
            compose_base=["/fixed/docker", "compose"],
            services=("agent-test-sandbox-image",),
            docker_runner=runner,
        )


def test_external_runtime_container_binds_labels_and_remains_stable_on_fixed_docker() -> None:
    runner, commands = _running_container_runner()

    _verify_running_container(runner)

    assert len([command for command in commands if command[-3:-1] == ["ps", "-q"]]) == 3
    templates = [command[3] for command in commands if command[1:3] == ["inspect", "--format"]]
    assert all(
        label in "\n".join(templates)
        for label in (
            acceptance_support.ACCEPTANCE_IMAGE_LABEL,
            acceptance_support.ACCEPTANCE_CANDIDATE_TREE_LABEL,
            acceptance_support.ACCEPTANCE_ENV_DIGEST_LABEL,
        )
    )
    assert all(command[0] == "/fixed/docker" for command in commands)


def test_external_runtime_container_rejects_stale_acceptance_labels() -> None:
    runner, _commands = _running_container_runner(label_run_id="stale-run")

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="current acceptance candidate"):
        _verify_running_container(runner)


@pytest.mark.parametrize(
    "container_output",
    [
        "",
        "short",
        "C" * 64,
        f"{CONTAINER_ID}\n{REPLACEMENT_CONTAINER_ID}",
    ],
)
def test_running_container_rejects_missing_invalid_or_ambiguous_container_id(container_output: str) -> None:
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="container identity is ambiguous"):
        _verify_running_container(lambda _command: container_output)


@pytest.mark.parametrize("created", ["not-a-timestamp", "2026-08-12T12:00:00.123456"])
def test_running_container_rejects_invalid_or_timezone_naive_creation_timestamp(created: str) -> None:
    runner, _commands = _running_container_runner(created=created)

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="Docker creation timestamp is invalid"):
        _verify_running_container(runner)


def test_running_container_rejects_timezone_naive_acceptance_start() -> None:
    runner, commands = _running_container_runner()

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="Acceptance start timestamp is invalid"):
        _verify_running_container(runner, started_at=datetime(2026, 8, 12, 12, 0))
    assert commands == []


@pytest.mark.parametrize(
    ("container_ids", "running_values", "image_ids", "message"),
    [
        ((CONTAINER_ID, REPLACEMENT_CONTAINER_ID), ("true",), (IMAGE_ID,), "container was replaced"),
        ((CONTAINER_ID,), ("true", "false"), (IMAGE_ID,), "stopped during"),
        ((CONTAINER_ID,), ("true",), (IMAGE_ID, DRIFTED_IMAGE_ID), "image drifted"),
        ((CONTAINER_ID, CONTAINER_ID, REPLACEMENT_CONTAINER_ID), ("true",), (IMAGE_ID,), "container was replaced"),
    ],
)
def test_running_container_rejects_final_identity_state_or_image_drift(
    container_ids: tuple[str, ...],
    running_values: tuple[str, ...],
    image_ids: tuple[str, ...],
    message: str,
) -> None:
    runner, _commands = _running_container_runner(
        container_ids=container_ids,
        running_values=running_values,
        image_ids=image_ids,
    )

    with pytest.raises(acceptance_support.AcceptanceSupportError, match=message):
        _verify_running_container(runner)


@pytest.mark.parametrize(
    ("expected_image_id", "image_kind", "message"),
    [
        ("sha256:short", "external-runtime", "captured image digest is invalid"),
        (IMAGE_ID, "unknown", "image authority kind is invalid"),
    ],
)
def test_running_container_rejects_invalid_expected_image_or_authority_kind(
    expected_image_id: str,
    image_kind: str,
    message: str,
) -> None:
    runner, commands = _running_container_runner()

    with pytest.raises(acceptance_support.AcceptanceSupportError, match=message):
        acceptance_support.verify_running_container(
            compose_base=["/fixed/docker", "compose"],
            service="langfuse-postgres",
            run_id="run-current",
            candidate=CANDIDATE,
            started_at=STARTED_AT,
            expected_image_id=expected_image_id,
            docker_runner=runner,
            image_kind=cast(acceptance_support.ImageAuthorityKind, image_kind),
        )
    assert commands == []


def test_sidecar_and_sandbox_build_contexts_are_deny_all_exact_allowlists() -> None:
    expected = {
        "docker/litellm-sidecar.Dockerfile.dockerignore": (
            "**",
            "!docker/",
            "!docker/litellm-sidecar.Dockerfile",
            "!docker/litellm_sidecar_entrypoint.py",
        ),
        "docker/agent-test-sandbox.Dockerfile.dockerignore": (
            "**",
            "!VERSION",
            "!docker/",
            "!docker/agent-test-sandbox.Dockerfile",
            "!packages/",
            "!packages/agentgov-testkit/",
            "!packages/agentgov-testkit/pyproject.toml",
            "!packages/agentgov-testkit/src/",
            "!packages/agentgov-testkit/src/agentgov_testkit/",
            "!packages/agentgov-testkit/src/agentgov_testkit/*.py",
        ),
    }
    for relative, allowlist in expected.items():
        lines = tuple(line for line in REPO_ROOT.joinpath(relative).read_text(encoding="utf-8").splitlines() if line)
        assert lines == allowlist

    for relative in ("docker/Dockerfile.dockerignore", "docker/frontend.Dockerfile.dockerignore"):
        patterns = REPO_ROOT.joinpath(relative).read_text(encoding="utf-8")
        assert all(item in patterns for item in (".obsidian/", ".venv/", "artifacts/", ".claude/settings.local.json"))
