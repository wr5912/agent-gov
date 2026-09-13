from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import run_selected_env_operation as runner
from scripts import selected_env_container_contract as container_contract

_DIGEST = "a" * 64
_IMAGE_ID = "sha256:" + "b" * 64


def _hostile_ui_inspect() -> dict[str, object]:
    return {
        "Name": "/agent-gov-ui",
        "Image": _IMAGE_ID,
        "Config": {
            "Env": ["SAFE=hostile"],
            "Labels": {},
            "User": "",
            "Entrypoint": None,
            "Cmd": None,
            "Healthcheck": None,
            "ExposedPorts": {},
        },
        "HostConfig": {
            "Init": False,
            "ExtraHosts": None,
            "PortBindings": {},
            "Tmpfs": {},
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "json-file", "Config": {}},
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": None,
            "SecurityOpt": None,
            "MaskedPaths": ["/proc/kcore"],
            "ReadonlyPaths": ["/proc/sys"],
            "Devices": None,
            "ReadonlyRootfs": False,
            "NetworkMode": "none",
            "PidMode": "",
            "IpcMode": "private",
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
        "State": {"Running": True},
    }


def test_restored_live_compose_cannot_override_frozen_config(tmp_path: Path, monkeypatch) -> None:
    frozen_root = tmp_path / "frozen"
    frozen_compose = frozen_root / "docker/docker-compose.yml"
    frozen_compose.parent.mkdir(parents=True)
    frozen_compose.write_text("services: {agent-gov-ui: {}}\n", encoding="utf-8")
    live_compose = tmp_path / "live-compose.yml"
    live_compose.write_text("hostile then restored\n", encoding="utf-8")
    selected = tmp_path / "selected.env"
    selected.write_text("COMPOSE_PROJECT_NAME=agent-gov\n", encoding="utf-8")
    service_config = {
        "image": "agent-gov-ui:4.0.0",
        "container_name": "agent-gov-ui",
        "environment": {"SAFE": "1"},
        "network_mode": "none",
        "logging": {"driver": "json-file", "options": {}},
    }
    actual = _hostile_ui_inspect()

    def output(command: list[str], _env: dict[str, str]) -> str:
        if command[-3:] == ["config", "--format", "json"]:
            assert frozen_compose.as_posix() in command
            live_compose.write_text("restored\n", encoding="utf-8")
            return json.dumps({"services": {"agent-gov-ui": service_config}})
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": _IMAGE_ID,
                        "Config": {
                            "Env": [],
                            "Labels": {"io.agentgov.source-artifact-sha256": _DIGEST},
                            "User": "",
                            "Entrypoint": None,
                            "Cmd": None,
                            "Healthcheck": None,
                            "ExposedPorts": {},
                        },
                    },
                ],
            )
        if "ps" in command:
            return "container-ui"
        if command[:3] == ["docker", "container", "inspect"]:
            return json.dumps([actual])
        raise AssertionError(command)

    monkeypatch.setattr(runner, "_run_output", output)
    with pytest.raises(runner.SelectedEnvError, match="配置不匹配"):
        runner._verify_stack_images(
            {},
            selected,
            "4.0.0",
            _DIGEST,
            source_root=frozen_root,
            langfuse=False,
            running=True,
            services=("agent-gov-ui",),
        )
    assert live_compose.read_text(encoding="utf-8") == "restored\n"


def _matching_ui_container_with_image_compose_labels() -> tuple[dict[str, object], container_contract.ImageConfig]:
    container = _hostile_ui_inspect()
    container["Config"]["Env"] = ["SAFE=1"]
    image_labels = {
        "com.docker.compose.project": "image-built-project",
        "com.docker.compose.service": "image-built-service",
        "com.docker.compose.version": "image-built-version",
        "io.agentgov.source-artifact-sha256": _DIGEST,
        "org.example.release": "trusted",
    }
    container["Config"]["Labels"] = {
        **image_labels,
        "com.docker.compose.project": "agent-gov",
        "com.docker.compose.service": "agent-gov-ui",
        "com.docker.compose.version": "runtime-compose-version",
        "com.docker.compose.config-hash": "runtime-generated",
    }
    image = container_contract.ImageConfig(
        environment=(),
        labels=tuple(sorted(image_labels.items())),
        user="",
        entrypoint=None,
        command=None,
    )
    return container, image


def _verify_ui_container_with_image_compose_labels(
    container: dict[str, object],
    image: container_contract.ImageConfig,
    service_labels: dict[str, str] | None = None,
    healthcheck: dict[str, object] | None = None,
    security_opt: list[str] | None = None,
) -> None:
    service_config: dict[str, object] = {
        "container_name": "agent-gov-ui",
        "environment": {"SAFE": "1"},
        "labels": service_labels or {},
        "network_mode": "none",
        "logging": {"driver": "json-file", "options": {}},
    }
    if healthcheck is not None:
        service_config["healthcheck"] = healthcheck
    if security_opt is not None:
        service_config["security_opt"] = security_opt
    container_contract.verify_container_config(
        json.dumps([container]),
        service_config,
        image,
        expected_image_id=_IMAGE_ID,
        project_name=lambda: "agent-gov",
        service="agent-gov-ui",
    )


def test_image_built_compose_labels_do_not_falsely_fail_container_attestation() -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    _verify_ui_container_with_image_compose_labels(container, image)


@pytest.mark.parametrize("label", ["io.agentgov.source-artifact-sha256", "org.example.release"])
def test_non_reserved_image_label_drift_still_fails_container_attestation(label: str) -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["Config"]["Labels"][label] = "hostile"

    with pytest.raises(runner.SelectedEnvError, match="配置不匹配"):
        _verify_ui_container_with_image_compose_labels(container, image)


def test_compose_service_cannot_inject_reserved_label() -> None:
    container, image = _matching_ui_container_with_image_compose_labels()

    with pytest.raises(runner.SelectedEnvError, match="不得覆盖内部标签"):
        _verify_ui_container_with_image_compose_labels(container, image, {"com.docker.compose.project": "hostile"})


def test_compose_healthcheck_dollar_escape_matches_container_literal_dollar() -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "probe '$SECRET' '$$SECRET'"]}
    container["State"]["Health"] = {"Status": "healthy"}

    _verify_ui_container_with_image_compose_labels(container, image, healthcheck={"test": ["CMD-SHELL", "probe '$$SECRET' '$$$$SECRET'"]})


@pytest.mark.parametrize("runtime_test", ["probe 'expanded-value'", "probe '$SECRET' '$SECRET'"])
def test_compose_healthcheck_dollar_escape_rejects_other_runtime_command(runtime_test: str) -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", runtime_test]}
    container["State"]["Health"] = {"Status": "healthy"}

    with pytest.raises(runner.SelectedEnvError, match="配置不匹配"):
        _verify_ui_container_with_image_compose_labels(container, image, healthcheck={"test": ["CMD-SHELL", "probe '$$SECRET' '$$$$SECRET'"]})


def test_systempaths_unconfined_matches_only_empty_docker_path_lists() -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["HostConfig"]["SecurityOpt"] = ["no-new-privileges:true", "seccomp=unconfined", "apparmor=unconfined"]
    container["HostConfig"]["MaskedPaths"] = []
    container["HostConfig"]["ReadonlyPaths"] = []

    _verify_ui_container_with_image_compose_labels(
        container,
        image,
        security_opt=["no-new-privileges:true", "seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined"],
    )


@pytest.mark.parametrize(
    ("masked", "readonly"),
    [(["/proc/kcore"], ["/proc/sys"]), ([], ["/proc/sys"]), (None, [])],
)
def test_systempaths_unconfined_rejects_nonempty_asymmetric_or_missing_path_lists(masked: list[str] | None, readonly: list[str]) -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["HostConfig"]["MaskedPaths"] = masked
    container["HostConfig"]["ReadonlyPaths"] = readonly

    with pytest.raises(runner.SelectedEnvError, match="配置不匹配|systempaths|MaskedPaths"):
        _verify_ui_container_with_image_compose_labels(container, image, security_opt=["systempaths=unconfined"])


def test_unexpected_systempaths_unconfined_fails_without_frozen_compose_declaration() -> None:
    container, image = _matching_ui_container_with_image_compose_labels()
    container["HostConfig"]["MaskedPaths"] = []
    container["HostConfig"]["ReadonlyPaths"] = []

    with pytest.raises(runner.SelectedEnvError, match="配置不匹配"):
        _verify_ui_container_with_image_compose_labels(container, image)
