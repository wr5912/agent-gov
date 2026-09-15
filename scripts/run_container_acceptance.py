#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_atomic_cutover_bootstrap import (
    SOURCE_ARTIFACT_LABEL,
    freeze_deployable_source,
    source_artifact_sha256,
)
from scripts.agentscope_atomic_cutover_cleanup import cleanup_runtime_root
from scripts.agentscope_atomic_cutover_daemon import HOST_FILESYSTEM_PROBE_IMAGE, CutoverDaemonSupport
from scripts.agentscope_atomic_cutover_env import read_stable_env_file
from scripts.container_acceptance_child import run_bound_child
from scripts.container_acceptance_cli import run_cli
from scripts.container_acceptance_compose_contract import (
    parse_container_created,
)
from scripts.container_acceptance_daemon_monitor import (
    DockerMutationMonitor,
    verify_frozen_running_contract,
)
from scripts.container_acceptance_environment import (  # noqa: F401
    ACTIVE_ENV,
    CORE_SERVICES,
    ISOLATED_MOUNT_PATHS,
    LOOPBACK_NO_PROXY,
    PROFILE_ENV,
    PROFILES,
    PROXY_ENV_KEYS,
    REPO_ROOT,
    RUN_ID_ENV,
    AcceptanceProfile,
    IsolatedEnvironment,
    _compose_mount_variables,
    _is_relative_to,
    build_acceptance_env,
    compose_command,
    prepare_isolated_environment,
    resolve_env_file,
)
from scripts.container_acceptance_environment import allocate_loopback_ports as _allocate_loopback_ports  # noqa: F401
from scripts.container_acceptance_environment import (
    validated_acceptance_command as _validated_acceptance_command,
)
from scripts.container_acceptance_frozen_runner import FrozenRunnerActions, resume_frozen_acceptance
from scripts.container_acceptance_identity import verify_sealed_file
from scripts.container_acceptance_inputs import (
    ACCEPTANCE_COMMAND_SHA256_ENV,
    ACCEPTANCE_CONTEXT_ENV,
    ACCEPTANCE_LABEL_KEY,
    ACCEPTANCE_TARGET_ENV,
    DEPLOYABLE_SOURCE_ROOT_ENV,
    LIVE_SOURCE_ROOT_ENV,
    AcceptanceError,
    ContainerIdentity,
    ScenarioFileSnapshot,
    acceptance_fingerprint,
    snapshot_scenario_files,
    source_fingerprint,
    verify_acceptance_context,
    write_acceptance_context,
)
from scripts.container_acceptance_materialization import (
    ExecutionMutationGuard,
    make_materialized_tree_disposable,
    materialize_acceptance_toolchain,
    scrub_private_acceptance_inputs,
    seal_materialized_input_tree,
)
from scripts.container_acceptance_reexec import (
    encode_state,
    exec_frozen_runner,
)
from scripts.container_acceptance_refresh import RefreshActions
from scripts.container_acceptance_refresh import refresh_profile as execute_refresh_profile
from scripts.container_acceptance_runtime_root import authorized_runtime_bootstrap_env
from scripts.container_acceptance_toolchain import (
    FORMAL_SOURCE_ROOT_ENV,
    TOOL_PATH_ENV_KEYS,
    AcceptanceToolchain,
    capture_acceptance_toolchain,
    verify_acceptance_toolchain,
)

LOCK_FILE = Path(f"/tmp/agentgov-container-acceptance-{os.getuid()}.lock")
LABEL_KEY = ACCEPTANCE_LABEL_KEY
_ACTIVE_INPUT_GUARD: ExecutionMutationGuard | None = None
_ACTIVE_DOCKER_MONITOR: DockerMutationMonitor | None = None


def _run_checked(
    command: list[str],
    *,
    env: dict[str, str],
    label: str,
    capture: bool = False,
    cwd: Path = REPO_ROOT,
) -> str:
    if _ACTIVE_INPUT_GUARD is not None:
        _ACTIVE_INPUT_GUARD.check()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=capture,
            text=capture,
        )
    except OSError as exc:
        raise AcceptanceError(f"{label}无法启动，验收命令未执行") from exc
    finally:
        if _ACTIVE_INPUT_GUARD is not None:
            _ACTIVE_INPUT_GUARD.check()
    if result.returncode:
        raise AcceptanceError(f"{label}失败，验收命令未执行")
    return result.stdout.strip() if capture else ""


def _daemon_command(
    command: list[str],
    label: str,
    *,
    capture: bool = False,
    child_env: dict[str, str] | None = None,
) -> str:
    if child_env is None:
        raise AcceptanceError("Docker daemon 核验缺少隔离环境")
    return _run_checked(command, env=child_env, label=label, capture=capture)


def _daemon_support(env: dict[str, str]) -> CutoverDaemonSupport:
    try:
        metadata = os.stat("/var/run/docker.sock", follow_symlinks=False)
    except OSError as exc:
        raise AcceptanceError("本地 Docker Unix socket 不可用") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise AcceptanceError("本地 Docker endpoint 不是 Unix socket")
    if env.get("DOCKER_HOST") != "unix:///var/run/docker.sock" or env.get("DOCKER_CONTEXT"):
        raise AcceptanceError("隔离容器验收只允许固定本机 Docker Unix socket")
    return CutoverDaemonSupport(error_type=AcceptanceError, run_command=_daemon_command)


def _verify_bound_docker_plugins(env: dict[str, str]) -> None:
    raw = _run_checked(
        [env[TOOL_PATH_ENV_KEYS["docker"]], "info", "--format", "{{json .ClientInfo.Plugins}}"],
        env=env,
        label="Docker CLI plugin 解析",
        capture=True,
    )
    try:
        plugins = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("Docker CLI plugin 解析未返回 JSON") from exc
    if not isinstance(plugins, list):
        raise AcceptanceError("Docker CLI plugin 解析结果 schema 无效")
    selected = {item.get("Name"): item.get("Path") for item in plugins if isinstance(item, dict) and item.get("Name") in {"buildx", "compose"}}
    expected = {
        "buildx": env[TOOL_PATH_ENV_KEYS["docker-buildx"]],
        "compose": env[TOOL_PATH_ENV_KEYS["docker-compose"]],
    }
    if selected != expected:
        raise AcceptanceError("Docker CLI 未实际选择已物化的 Compose/Buildx plugin")
    via_docker = _run_checked(
        [env[TOOL_PATH_ENV_KEYS["docker"]], "compose", "version", "--short"],
        env=env,
        label="Docker Compose plugin 版本核验",
        capture=True,
    )
    direct = _run_checked(
        [env[TOOL_PATH_ENV_KEYS["docker-compose"]], "version", "--short"],
        env=env,
        label="物化 Compose plugin 版本核验",
        capture=True,
    )
    if via_docker != direct:
        raise AcceptanceError("Docker Compose 实际 plugin 与绑定副本版本不一致")


def _verify_daemon_boundary(
    env: dict[str, str],
    expected: dict[str, object],
    probe_root: Path,
    image_id: str = HOST_FILESYSTEM_PROBE_IMAGE,
) -> None:
    daemon = _daemon_support(env)
    daemon.verify(env, expected)
    daemon.verify_host_filesystem(env, probe_root, image_id)
    daemon.verify(env, expected)


def _validate_service_model(base: list[str], profile: AcceptanceProfile, env: dict[str, str]) -> None:
    output = _run_checked([*base, "config", "--services"], env=env, label="Compose 服务解析", capture=True)
    if set(output.splitlines()) != set(profile.expected_services):
        raise AcceptanceError("Compose 服务集合与验收 profile 不一致")


def _validate_isolated_mounts(
    base: list[str],
    isolation: IsolatedEnvironment,
    env: dict[str, str],
) -> None:
    raw = _run_checked([*base, "config", "--format", "json"], env=env, label="Compose 隔离挂载解析", capture=True)
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("Compose 隔离挂载解析未返回 JSON") from exc
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        raise AcceptanceError("Compose 隔离挂载解析缺少 services")
    bootstrap = (isolation.source_root / "docker/runtime-bootstrap").resolve()
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        volumes = service.get("volumes")
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            raw_source = volume.get("source")
            if not isinstance(raw_source, str) or not raw_source:
                raise AcceptanceError(f"服务 {service_name} 存在无法审计的 bind source")
            source = Path(raw_source).resolve()
            if source == bootstrap:
                if not volume.get("read_only"):
                    raise AcceptanceError("Runtime bootstrap 在验收容器中必须只读")
                continue
            if not _is_relative_to(source, isolation.runtime_root):
                raise AcceptanceError(f"服务 {service_name} 的 bind mount 未隔离到临时根")


def _bootstrap_isolated_runtime(isolation: IsolatedEnvironment, env: dict[str, str]) -> None:
    with authorized_runtime_bootstrap_env(isolation.runtime_root, env) as bootstrap_env:
        _run_checked(
            [
                env[TOOL_PATH_ENV_KEYS["python"]],
                str(isolation.source_root / "scripts/bootstrap_runtime_volume.py"),
                "--env-file",
                str(isolation.env_file),
                "--runtime-root",
                str(isolation.runtime_root),
                "--quiet",
            ],
            env=bootstrap_env,
            label="隔离 Runtime 初始化",
        )


def _inspect_value(arguments: list[str], *, env: dict[str, str], label: str) -> str:
    return _run_checked(
        [env[TOOL_PATH_ENV_KEYS["docker"]], "inspect", "--format", *arguments],
        env=env,
        label=label,
        capture=True,
    )


def _inspect_api_image(env: dict[str, str], label: str) -> str:
    return _inspect_value(["{{.Id}}", f"agent-gov-api:{env['APP_VERSION']}"], env=env, label=label)


def _verify_container(
    base: list[str],
    service: str,
    run_id: str,
    started_at: datetime,
    env: dict[str, str],
    *,
    check_image: bool,
) -> ContainerIdentity:
    container_id = _run_checked([*base, "ps", "-q", service], env=env, label="容器定位", capture=True)
    if not container_id:
        raise AcceptanceError(f"服务 {service} 没有运行容器")
    container_id = _inspect_value(["{{.Id}}", container_id], env=env, label="容器身份检查")
    running = _inspect_value(["{{.State.Running}}", container_id], env=env, label="容器状态检查")
    container_label = _inspect_value(
        [f'{{{{index .Config.Labels "{LABEL_KEY}"}}}}', container_id],
        env=env,
        label="容器 freshness 检查",
    )
    created = _inspect_value(["{{.Created}}", container_id], env=env, label="容器创建时间检查")
    if running != "true" or container_label != run_id:
        raise AcceptanceError(f"服务 {service} 未加载本轮容器配置")
    try:
        created_at = parse_container_created(created)
    except ValueError as exc:
        raise AcceptanceError(str(exc)) from exc
    if created_at < started_at - timedelta(seconds=2):
        raise AcceptanceError(f"服务 {service} 没有在本轮 recreate")
    image_id = _inspect_value(["{{.Image}}", container_id], env=env, label="镜像定位")
    if check_image:
        image_label = _inspect_value(
            [f'{{{{index .Config.Labels "{LABEL_KEY}"}}}}', image_id],
            env=env,
            label="镜像 freshness 检查",
        )
        if image_label != run_id:
            raise AcceptanceError(f"服务 {service} 未使用本轮构建镜像")
        source_label = _inspect_value(
            [f'{{{{index .Config.Labels "{SOURCE_ARTIFACT_LABEL}"}}}}', image_id],
            env=env,
            label="镜像 source artifact 检查",
        )
        if source_label != env.get("AGENTGOV_SOURCE_ARTIFACT_SHA256"):
            raise AcceptanceError(f"服务 {service} 的镜像未绑定当前 source artifact")
    return ContainerIdentity(service=service, container_id=container_id, image_id=image_id)


def _refresh_actions() -> RefreshActions:
    return RefreshActions(
        run_checked=_run_checked,
        validate_service_model=_validate_service_model,
        validate_isolated_mounts=_validate_isolated_mounts,
        inspect_api_image=_inspect_api_image,
        verify_daemon=_verify_daemon_boundary,
        verify_identity=lambda env, expected: _daemon_support(env).verify(env, expected),
        verify_container=_verify_container,
    )


def refresh_profile(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    env: dict[str, str],
    daemon_identity: dict[str, object],
    docker_monitor: DockerMutationMonitor,
) -> tuple[ContainerIdentity, ...]:
    return execute_refresh_profile(
        profile,
        isolation,
        env,
        daemon_identity,
        docker_monitor,
        _refresh_actions(),
    )


def cleanup_profile(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    env: dict[str, str],
    daemon_identity: dict[str, object],
) -> None:
    _daemon_support(env)
    base = compose_command(
        profile,
        isolation.env_file,
        isolation.source_root,
        docker_path=env[TOOL_PATH_ENV_KEYS["docker"]],
    )
    _verify_daemon_boundary(env, daemon_identity, isolation.runtime_root.parent)
    _run_checked(
        [*base, "down", "--volumes", "--remove-orphans", "--timeout", "15"],
        env=env,
        label="隔离 Compose 清理",
    )
    probe_image = _inspect_api_image(env, "cleanup daemon probe 镜像检查")

    def final_probe() -> None:
        _verify_daemon_boundary(env, daemon_identity, isolation.runtime_root.parent, probe_image)

    cleanup_runtime_root(base, isolation.runtime_root, env, before_delete=final_probe, repair_image_id=probe_image)


def _run_child(command: list[str], env: dict[str, str], *, cwd: Path = REPO_ROOT) -> int:
    return run_bound_child(
        command,
        env,
        cwd=cwd,
        input_guard=_ACTIVE_INPUT_GUARD,
        error_type=AcceptanceError,
    )


def _freeze_acceptance_source(temp_root: Path) -> tuple[Path, str]:
    source_root = temp_root / "source-snapshot"
    try:
        digest = freeze_deployable_source(REPO_ROOT, source_root)
    except (OSError, ValueError) as exc:
        raise AcceptanceError("无法冻结本轮容器构建 source") from exc
    return source_root, digest


def _verify_refresh_inputs(
    env_file: Path,
    isolation: IsolatedEnvironment,
    child_env: dict[str, str],
    snapshots: tuple[ScenarioFileSnapshot, ...],
    initial_fingerprint: str,
) -> tuple[Path, Path, str]:
    live_root = Path(child_env[LIVE_SOURCE_ROOT_ENV])
    git_path = Path(child_env[TOOL_PATH_ENV_KEYS["git"]])
    frozen_source_sha256 = isolation.overrides["AGENTGOV_SOURCE_ARTIFACT_SHA256"]
    if source_artifact_sha256(isolation.source_root) != frozen_source_sha256:
        raise AcceptanceError("冻结的容器构建 source 在构建期间发生变化")
    if source_artifact_sha256(live_root) != frozen_source_sha256:
        raise AcceptanceError("当前 deployable source 与容器构建快照不一致")
    current = acceptance_fingerprint(
        env_file,
        isolation.env_file,
        child_env,
        snapshots,
        repo_root=live_root,
        git_path=git_path,
    )
    if current != initial_fingerprint:
        raise AcceptanceError("构建或 recreate 期间工作树/源 env 已变化")
    return live_root, git_path, frozen_source_sha256


def _seal_acceptance_context(
    profile: AcceptanceProfile,
    env_file: Path,
    isolation: IsolatedEnvironment,
    child_env: dict[str, str],
    snapshots: tuple[ScenarioFileSnapshot, ...],
    containers: tuple[ContainerIdentity, ...],
    *,
    source_sha256: str,
    frozen_source_sha256: str,
    initial_fingerprint: str,
    live_root: Path,
    git_path: Path,
) -> None:
    context_path = Path(child_env[ACCEPTANCE_CONTEXT_ENV])
    context_identity = write_acceptance_context(
        context_path,
        source_env=env_file,
        effective_env=isolation.env_file,
        environ=child_env,
        source_sha256=source_sha256,
        frozen_source_sha256=frozen_source_sha256,
        snapshots=snapshots,
        containers=containers,
    )
    context_root = context_path.parent
    seal_materialized_input_tree(context_root, error_type=AcceptanceError)
    if _ACTIVE_INPUT_GUARD is None:
        raise AcceptanceError("正式验收输入变更监视未覆盖回执")
    _ACTIVE_INPUT_GUARD.add_roots((context_root,))
    _ACTIVE_INPUT_GUARD.check()
    verify_sealed_file(
        context_path,
        context_identity,
        mode=0o400,
        error_type=AcceptanceError,
        label="验收上下文回执",
    )
    current = acceptance_fingerprint(
        env_file,
        isolation.env_file,
        child_env,
        snapshots,
        repo_root=live_root,
        git_path=git_path,
    )
    if current != initial_fingerprint:
        raise AcceptanceError("验收回执封存前工作树或源输入发生变化")
    verify_acceptance_context(child_env, require_trace_complete=profile.name == "langfuse")


def _verify_refreshed_result(
    profile: AcceptanceProfile,
    env_file: Path,
    isolation: IsolatedEnvironment,
    child_env: dict[str, str],
    daemon_identity: dict[str, object],
    snapshots: tuple[ScenarioFileSnapshot, ...],
    containers: tuple[ContainerIdentity, ...],
    docker_monitor: DockerMutationMonitor,
    *,
    initial_fingerprint: str,
    live_root: Path,
    git_path: Path,
    frozen_source_sha256: str,
) -> None:
    docker_path = child_env[TOOL_PATH_ENV_KEYS["docker"]]

    def contract_output(command: list[str], label: str) -> str:
        return _run_checked(command, env=child_env, label=label, capture=True)

    verify_acceptance_toolchain(child_env, error_type=AcceptanceError)
    verify_acceptance_context(child_env, require_trace_complete=profile.name == "langfuse")
    verify_frozen_running_contract(
        compose_command(profile, isolation.env_file, isolation.source_root, docker_path=docker_path),
        containers,
        project_name=isolation.project_name,
        docker_path=docker_path,
        run_output=contract_output,
        error_type=AcceptanceError,
    )
    _daemon_support(child_env).verify(child_env, daemon_identity)
    current = acceptance_fingerprint(
        env_file,
        isolation.env_file,
        child_env,
        snapshots,
        repo_root=live_root,
        git_path=git_path,
    )
    if current != initial_fingerprint:
        raise AcceptanceError("容器验收期间工作树/源 env 已变化，结果无效")
    if source_artifact_sha256(live_root) != frozen_source_sha256:
        raise AcceptanceError("容器验收期间 deployable source 已变化，结果无效")
    docker_monitor.verify()
    probe_image = _inspect_api_image(child_env, "验收后 daemon probe")
    _verify_daemon_boundary(child_env, daemon_identity, isolation.runtime_root.parent, probe_image)


def _run_refreshed_acceptance(
    profile: AcceptanceProfile,
    env_file: Path,
    command: list[str],
    isolation: IsolatedEnvironment,
    child_env: dict[str, str],
    daemon_identity: dict[str, object],
    snapshots: tuple[ScenarioFileSnapshot, ...],
    initial_fingerprint: str,
    source_sha256: str,
) -> int:
    global _ACTIVE_DOCKER_MONITOR
    docker_path = child_env[TOOL_PATH_ENV_KEYS["docker"]]

    def contract_output(arguments: list[str], label: str) -> str:
        return _run_checked(arguments, env=child_env, label=label, capture=True)

    docker_monitor = DockerMutationMonitor(
        docker_path=docker_path,
        project_name=isolation.project_name,
        runtime_root=isolation.runtime_root,
        source_root=isolation.source_root,
        environ=child_env,
        run_output=contract_output,
        error_type=AcceptanceError,
    )
    _ACTIVE_DOCKER_MONITOR = docker_monitor
    verify_acceptance_toolchain(child_env, error_type=AcceptanceError)
    containers = refresh_profile(profile, isolation, child_env, daemon_identity, docker_monitor)
    verify_acceptance_toolchain(child_env, error_type=AcceptanceError)
    live_root, git_path, frozen_source_sha256 = _verify_refresh_inputs(
        env_file,
        isolation,
        child_env,
        snapshots,
        initial_fingerprint,
    )
    _seal_acceptance_context(
        profile,
        env_file,
        isolation,
        child_env,
        snapshots,
        containers,
        source_sha256=source_sha256,
        frozen_source_sha256=frozen_source_sha256,
        initial_fingerprint=initial_fingerprint,
        live_root=live_root,
        git_path=git_path,
    )
    bound_command = [child_env[TOOL_PATH_ENV_KEYS["make"]], *command[1:]]
    returncode = _run_child(bound_command, child_env, cwd=Path(child_env[FORMAL_SOURCE_ROOT_ENV]))
    _verify_refreshed_result(
        profile,
        env_file,
        isolation,
        child_env,
        daemon_identity,
        snapshots,
        containers,
        docker_monitor,
        initial_fingerprint=initial_fingerprint,
        live_root=live_root,
        git_path=git_path,
        frozen_source_sha256=frozen_source_sha256,
    )
    _ACTIVE_DOCKER_MONITOR = None
    return returncode


def _cleanup_acceptance_run(
    profile: AcceptanceProfile,
    temp_root: Path,
    isolation: IsolatedEnvironment | None,
    child_env: dict[str, str] | None,
    daemon_identity: dict[str, object] | None,
    *,
    compose_started: bool,
    input_guard: ExecutionMutationGuard | None,
    operation_error: BaseException | None,
) -> None:
    global _ACTIVE_DOCKER_MONITOR, _ACTIVE_INPUT_GUARD
    cleanup_error: BaseException | None = None
    if _ACTIVE_DOCKER_MONITOR is not None:
        _ACTIVE_DOCKER_MONITOR.close()
        _ACTIVE_DOCKER_MONITOR = None
    if compose_started and isolation is not None and child_env is not None and daemon_identity is not None:
        try:
            verify_acceptance_toolchain(child_env, error_type=AcceptanceError)
            cleanup_profile(profile, isolation, child_env, daemon_identity)
        except (AcceptanceError, OSError) as exc:
            cleanup_error = exc
    if input_guard is not None:
        try:
            input_guard.check()
        except AcceptanceError as exc:
            cleanup_error = cleanup_error or exc
        finally:
            input_guard.close()
    _ACTIVE_INPUT_GUARD = None
    try:
        for private_root in ("execution-toolchain", "acceptance-inputs", "acceptance-context"):
            make_materialized_tree_disposable(
                temp_root / private_root,
                error_type=AcceptanceError,
            )
        if cleanup_error is None:
            shutil.rmtree(temp_root)
        else:
            scrub_private_acceptance_inputs(temp_root, error_type=AcceptanceError)
    except (AcceptanceError, OSError) as exc:
        cleanup_error = cleanup_error or exc
        try:
            scrub_private_acceptance_inputs(temp_root, error_type=AcceptanceError)
        except (AcceptanceError, OSError) as scrub_error:
            cleanup_error = cleanup_error or scrub_error
    if cleanup_error is None:
        return
    detail = str(cleanup_error) if isinstance(cleanup_error, AcceptanceError) else "隔离临时目录回收失败"
    if operation_error is None:
        raise AcceptanceError(detail) from cleanup_error
    print(f"CONTAINER_ACCEPTANCE_CLEANUP_FAIL: {detail}", file=sys.stderr)


def _resume_frozen_acceptance(environ: dict[str, str]) -> int:
    def activate_guard(guard: ExecutionMutationGuard) -> None:
        global _ACTIVE_INPUT_GUARD
        _ACTIVE_INPUT_GUARD = guard

    return resume_frozen_acceptance(
        environ,
        FrozenRunnerActions(
            activate_guard=activate_guard,
            run_checked=_run_checked,
            verify_plugins=_verify_bound_docker_plugins,
            daemon_support=_daemon_support,
            verify_daemon=_verify_daemon_boundary,
            bootstrap=_bootstrap_isolated_runtime,
            run_refreshed=_run_refreshed_acceptance,
            cleanup=_cleanup_acceptance_run,
        ),
    )


def _prepare_frozen_acceptance(
    env_file: Path,
    environ: dict[str, str],
    acceptance_target: str,
    temp_root: Path,
) -> tuple[IsolatedEnvironment, AcceptanceToolchain, tuple[ScenarioFileSnapshot, ...], str, tuple[int, ...], str]:
    temp_root.chmod(0o700)
    run_id = f"{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(6)}"
    source_toolchain = capture_acceptance_toolchain(
        environ,
        acceptance_target,
        error_type=AcceptanceError,
    )
    source_env_payload, source_env_identity = read_stable_env_file(env_file, error_type=AcceptanceError)
    snapshots = snapshot_scenario_files(temp_root, environ)
    pre_freeze_source_sha256 = source_fingerprint(env_file)
    source_root, source_digest = _freeze_acceptance_source(temp_root)
    if source_fingerprint(env_file) != pre_freeze_source_sha256:
        raise AcceptanceError("工作树或源 env 在冻结容器构建 source 期间发生变化")
    if source_artifact_sha256(REPO_ROOT) != source_digest:
        raise AcceptanceError("当前 deployable source 与容器构建快照不一致")
    toolchain, _formal_root = materialize_acceptance_toolchain(
        source_toolchain,
        temp_root / "execution-toolchain",
        source_root,
        error_type=AcceptanceError,
    )
    isolation = prepare_isolated_environment(
        env_file,
        run_id,
        temp_root,
        source_env_payload=source_env_payload,
        source_root=source_root,
        source_digest=source_digest,
        allocated_ports=_allocate_loopback_ports(5),
    )
    return isolation, toolchain, snapshots, pre_freeze_source_sha256, source_env_identity, run_id


def run_acceptance(profile: AcceptanceProfile, env_file: Path, command: list[str], environ: dict[str, str]) -> int:
    global _ACTIVE_INPUT_GUARD
    acceptance_target, command_sha256 = _validated_acceptance_command(profile, command)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        temp_root = Path(tempfile.mkdtemp(prefix=f"agentgov-acceptance-{os.getuid()}-"))
        isolation: IsolatedEnvironment | None = None
        child_env: dict[str, str] | None = None
        daemon_identity: dict[str, object] | None = None
        compose_started = False
        input_guard: ExecutionMutationGuard | None = None
        operation_error: BaseException | None = None
        try:
            isolation, toolchain, snapshots, pre_freeze_source_sha256, source_env_identity, run_id = _prepare_frozen_acceptance(
                env_file,
                environ,
                acceptance_target,
                temp_root,
            )
            frozen_inputs_root = temp_root / "acceptance-inputs"
            frozen_inputs_root.mkdir(mode=0o700, exist_ok=True)
            frozen_env_file = frozen_inputs_root / "compose.acceptance.env"
            os.replace(isolation.env_file, frozen_env_file)
            isolation = replace(isolation, env_file=frozen_env_file)
            context_root = temp_root / "acceptance-context"
            context_root.mkdir(mode=0o700)
            child_env = build_acceptance_env(
                profile,
                isolation,
                run_id,
                environ,
                snapshots,
                acceptance_target=acceptance_target,
                toolchain=toolchain,
                live_source_root=REPO_ROOT,
            )
            child_env[ACCEPTANCE_CONTEXT_ENV] = str(context_root / "acceptance-context.json")
            child_env[ACCEPTANCE_TARGET_ENV] = acceptance_target
            child_env[ACCEPTANCE_COMMAND_SHA256_ENV] = command_sha256
            seal_materialized_input_tree(frozen_inputs_root, error_type=AcceptanceError)
            input_guard = ExecutionMutationGuard(
                (isolation.source_root, Path(toolchain["execution_root"]), frozen_inputs_root),
                error_type=AcceptanceError,
            )
            _ACTIVE_INPUT_GUARD = input_guard
            if child_env[LIVE_SOURCE_ROOT_ENV] != str(REPO_ROOT) or child_env[DEPLOYABLE_SOURCE_ROOT_ENV] != str(isolation.source_root):
                raise AcceptanceError("验收源码根环境未绑定本轮副本")
            state = encode_state(
                profile,
                env_file,
                command,
                isolation,
                snapshots,
                pre_freeze_source_sha256,
                source_env_identity,
            )
            exec_frozen_runner(
                state,
                child_env,
                input_guard,
                lock_stream.fileno(),
                error_type=AcceptanceError,
            )
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            _cleanup_acceptance_run(
                profile,
                temp_root,
                isolation,
                child_env,
                daemon_identity,
                compose_started=compose_started,
                input_guard=input_guard,
                operation_error=operation_error,
            )
    raise AcceptanceError("冻结验收 runner 意外返回")


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv, dict(os.environ), resume=_resume_frozen_acceptance, run=run_acceptance, error_type=AcceptanceError)


if __name__ == "__main__":
    raise SystemExit(main())
