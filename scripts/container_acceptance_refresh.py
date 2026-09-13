"""正式容器验收的 Compose rebuild/recreate 事务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from scripts.container_acceptance_compose_contract import (
    ComposeServices,
    ImageInventory,
    capture_image_inventory,
    capture_service_image_ids,
    render_services,
    required_external_images,
    verify_running_service,
)
from scripts.container_acceptance_daemon_monitor import DockerMutationMonitor
from scripts.container_acceptance_environment import RUN_ID_ENV, AcceptanceProfile, IsolatedEnvironment, compose_command
from scripts.container_acceptance_inputs import AcceptanceError, ContainerIdentity
from scripts.container_acceptance_toolchain import TOOL_PATH_ENV_KEYS


@dataclass(frozen=True)
class RefreshPlan:
    compose: list[str]
    services: ComposeServices
    external_images: ImageInventory
    external_inventory: ImageInventory
    intended_images: ImageInventory
    daemon_probe_image: str


@dataclass(frozen=True)
class RefreshActions:
    run_checked: Callable[..., str]
    validate_service_model: Callable[[list[str], AcceptanceProfile, dict[str, str]], None]
    validate_isolated_mounts: Callable[[list[str], IsolatedEnvironment, dict[str, str]], None]
    inspect_api_image: Callable[[dict[str, str], str], str]
    verify_daemon: Callable[..., None]
    verify_container: Callable[..., ContainerIdentity]


def _contract_output(
    actions: RefreshActions,
    env: dict[str, str],
    command: list[str],
    label: str,
) -> str:
    return actions.run_checked(command, env=env, label=label, capture=True)


def _prepare_refresh_plan(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    env: dict[str, str],
    daemon_identity: dict[str, object],
    actions: RefreshActions,
) -> RefreshPlan:
    base = compose_command(
        profile,
        isolation.env_file,
        isolation.source_root,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
    )

    def output(command: list[str], label: str) -> str:
        return _contract_output(actions, env, command, label)

    rendered_services = render_services(base, run_output=output, error_type=AcceptanceError)
    actions.validate_service_model(base, profile, env)
    actions.validate_isolated_mounts(base, isolation, env)
    references = required_external_images(
        rendered_services,
        build_services=profile.build_services,
        source_root=isolation.source_root,
        error_type=AcceptanceError,
    )
    external_inventory = capture_image_inventory(
        references,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
        run_output=output,
        error_type=AcceptanceError,
    )
    actions.run_checked([*base, "build", "--pull=false", *profile.build_services], env=env, label="Compose 镜像重建")
    current_inventory = capture_image_inventory(
        references,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
        run_output=output,
        error_type=AcceptanceError,
    )
    if current_inventory != external_inventory:
        raise AcceptanceError("Compose build 期间外部镜像 inventory 发生变化")
    intended_images = capture_service_image_ids(
        rendered_services,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
        run_output=output,
        error_type=AcceptanceError,
    )
    probe_image = actions.inspect_api_image(env, "daemon probe 镜像检查")
    actions.verify_daemon(env, daemon_identity, isolation.runtime_root.parent, probe_image)
    return RefreshPlan(base, rendered_services, references, external_inventory, intended_images, probe_image)


def _prepare_profile_runtime(
    profile: AcceptanceProfile,
    base: list[str],
    env: dict[str, str],
    actions: RefreshActions,
) -> None:
    if profile.name == "langfuse":
        actions.run_checked(
            [
                *base,
                "--profile",
                "langfuse-maintenance",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--pull",
                "never",
                "langfuse-volume-init",
            ],
            env=env,
            label="隔离 Langfuse 卷初始化",
        )
    actions.run_checked(
        [
            *base,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--pull",
            "never",
            "--entrypoint",
            "python",
            "agent-gov-api",
            "-m",
            "app.runtime.published_harness_preparation",
        ],
        env=env,
        label="已发布 Harness 启动前快照准备",
    )


def _recreate_profile_containers(
    profile: AcceptanceProfile,
    base: list[str],
    env: dict[str, str],
    docker_monitor: DockerMutationMonitor,
    actions: RefreshActions,
) -> tuple[ContainerIdentity, ...]:
    started_at = datetime.now(timezone.utc)
    actions.run_checked(
        [
            *base,
            "up",
            "-d",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "180",
            "--remove-orphans",
            *profile.expected_services,
        ],
        env=env,
        label="Compose 服务 recreate",
    )
    docker_monitor.start()
    local_services = set(profile.build_services)
    return tuple(
        actions.verify_container(
            base,
            service,
            env[RUN_ID_ENV],
            started_at,
            env,
            check_image=service in local_services,
        )
        for service in profile.expected_services
    )


def _verify_refresh_result(
    isolation: IsolatedEnvironment,
    env: dict[str, str],
    daemon_identity: dict[str, object],
    docker_monitor: DockerMutationMonitor,
    plan: RefreshPlan,
    containers: tuple[ContainerIdentity, ...],
    actions: RefreshActions,
) -> None:
    def output(command: list[str], label: str) -> str:
        return _contract_output(actions, env, command, label)

    for container in containers:
        expected_image_id = plan.intended_images.get(container.service)
        if expected_image_id is None:
            raise AcceptanceError(f"服务 {container.service} 缺少构建后的镜像 identity")
        verify_running_service(
            service=container.service,
            service_config=plan.services[container.service],
            container_id=container.container_id,
            expected_image_id=expected_image_id,
            project_name=isolation.project_name,
            docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
            run_output=output,
            error_type=AcceptanceError,
        )
    current_inventory = capture_image_inventory(
        plan.external_images,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
        run_output=output,
        error_type=AcceptanceError,
    )
    if current_inventory != plan.external_inventory:
        raise AcceptanceError("Compose recreate 期间外部镜像 inventory 发生变化")
    actions.verify_daemon(env, daemon_identity, isolation.runtime_root.parent, plan.daemon_probe_image)
    docker_monitor.bind(containers)


def refresh_profile(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    env: dict[str, str],
    daemon_identity: dict[str, object],
    docker_monitor: DockerMutationMonitor,
    actions: RefreshActions,
) -> tuple[ContainerIdentity, ...]:
    plan = _prepare_refresh_plan(profile, isolation, env, daemon_identity, actions)
    _prepare_profile_runtime(profile, plan.compose, env, actions)
    containers = _recreate_profile_containers(profile, plan.compose, env, docker_monitor, actions)
    _verify_refresh_result(isolation, env, daemon_identity, docker_monitor, plan, containers, actions)
    return containers
