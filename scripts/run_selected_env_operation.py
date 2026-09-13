#!/usr/bin/env python3
"""Run a fixed deployment operation against one immutable selected-env snapshot."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import selected_env_deployed_browser as deployed_browser
from scripts import selected_env_image_inventory as image_inventory
from scripts import selected_env_operation_cli, selected_env_reexec
from scripts import selected_env_operation_contract as operation_contract
from scripts import selected_env_persistent_source as persistent_source
from scripts import selected_env_python_toolchain as python_toolchain
from scripts import selected_env_source_snapshot as source_snapshot
from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_atomic_cutover_daemon import HOST_FILESYSTEM_PROBE_IMAGE, CutoverDaemonSupport
from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings, read_stable_env_file, verify_stable_env_file
from scripts.agentscope_atomic_cutover_lock import run_with_global_cutover_lock

OPERATIONS = operation_contract.OPERATIONS
ComposeServices = operation_contract.ComposeServices
DaemonBoundary = operation_contract.DaemonBoundary
DockerDaemonIdentity = operation_contract.DockerDaemonIdentity
ImageReferences = operation_contract.ImageReferences
SelectedEnvError = operation_contract.SelectedEnvError
StackImageIds = operation_contract.StackImageIds
_BUILD_OPERATIONS = operation_contract.BUILD_OPERATIONS
_DOCKER_BIND_OPERATIONS = operation_contract.DOCKER_BIND_OPERATIONS
_DOCKER_MUTATING_OPERATIONS = operation_contract.DOCKER_MUTATING_OPERATIONS
_LOCAL_IMAGES = operation_contract.LOCAL_IMAGES
_MUTATING_OPERATIONS = operation_contract.MUTATING_OPERATIONS
_THIRD_PARTY_SERVICES = operation_contract.THIRD_PARTY_SERVICES

REPO_ROOT = Path(__file__).resolve().parents[1]
_OPERATION_SOURCE_ROOT_ENV = source_snapshot.SOURCE_ROOT_ENV
_write_snapshot = source_snapshot.write_operation_input


def _read_stable_regular_file(path: Path) -> tuple[bytes, tuple[int, ...]]:
    return read_stable_env_file(path, error_type=SelectedEnvError)


def _verified_command_root(child_env: Mapping[str, str] | None) -> Path:
    if child_env is None:
        return REPO_ROOT
    source_root = REPO_ROOT
    if _OPERATION_SOURCE_ROOT_ENV in child_env:
        source_root = source_snapshot.verify_command_source(child_env, hash_source=source_artifact_sha256)
    else:
        source_snapshot.verify_operation_input(child_env)
    source_snapshot.verify_docker_toolchain(child_env)
    python_toolchain.verify_python_toolchain(child_env)
    return source_root


def _bind_command(command: list[str], child_env: Mapping[str, str]) -> list[str]:
    return python_toolchain.bind_python_command(
        source_snapshot.bind_docker_command(command, child_env),
        child_env,
    )


@contextmanager
def _command_monitor(child_env: Mapping[str, str]) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(source_snapshot.mutation_monitor(child_env))
        stack.enter_context(python_toolchain.mutation_monitor(child_env))
        _verified_command_root(child_env)
        try:
            yield
        finally:
            _verified_command_root(child_env)


def _run_output(command: list[str], child_env: dict[str, str]) -> str:
    command_root = _verified_command_root(child_env)
    command = _bind_command(command, child_env)
    try:
        with _command_monitor(child_env):
            output = subprocess.run(
                command,
                cwd=command_root,
                env=child_env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        _verified_command_root(child_env)
        raise SelectedEnvError("无法核验本地部署镜像/容器身份") from exc
    return output


def _run(command: list[str], child_env: dict[str, str], *, check: bool = True) -> int:
    command_root = _verified_command_root(child_env)
    command = _bind_command(command, child_env)
    try:
        with _command_monitor(child_env):
            returncode = subprocess.run(command, cwd=command_root, env=child_env, check=check).returncode
    except (OSError, subprocess.CalledProcessError) as exc:
        _verified_command_root(child_env)
        raise SelectedEnvError(f"selected-env operation failed: {command[0]}") from exc
    return returncode


def _daemon_command(
    command: list[str],
    _label: str,
    *,
    capture: bool = False,
    child_env: dict[str, str] | None = None,
) -> str:
    command_root = _verified_command_root(child_env)
    if child_env is not None:
        command = _bind_command(command, child_env)
    try:
        with _command_monitor(child_env or {}):
            result = subprocess.run(
                command,
                cwd=command_root,
                env=child_env,
                check=True,
                capture_output=capture,
                text=capture,
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        _verified_command_root(child_env)
        raise SelectedEnvError("无法证明 Docker daemon 与当前 host filesystem 同源") from exc
    return result.stdout if capture else ""


def _local_daemon_support(child_env: dict[str, str]) -> CutoverDaemonSupport:
    try:
        socket_metadata = os.stat("/var/run/docker.sock", follow_symlinks=False)
    except OSError as exc:
        raise SelectedEnvError("本地 Docker Unix socket 不可用") from exc
    if not stat.S_ISSOCK(socket_metadata.st_mode):
        raise SelectedEnvError("本地 Docker endpoint 不是 Unix socket")
    if child_env.get("DOCKER_HOST") != "unix:///var/run/docker.sock" or child_env.get("DOCKER_CONTEXT"):
        raise SelectedEnvError("selected-env operation 仅允许固定本机 Docker Unix socket")
    return CutoverDaemonSupport(error_type=SelectedEnvError, run_command=_daemon_command)


def _capture_local_daemon(child_env: dict[str, str]) -> DockerDaemonIdentity:
    return _local_daemon_support(child_env).capture(child_env)


def _daemon_mutation_lock(
    operation: str,
    child_env: dict[str, str],
) -> AbstractContextManager[Mapping[str, object] | None]:
    return persistent_source.daemon_mutation_lock(
        operation in _MUTATING_OPERATIONS,
        child_env,
        lambda: _local_daemon_support(child_env),
        run_command=lambda command, env: _run(command, env, check=False),
        run_output=_run_output,
    )


def _verify_local_daemon(
    child_env: dict[str, str],
    expected: Mapping[str, object],
    image_id: str,
) -> None:
    daemon = _local_daemon_support(child_env)
    daemon.verify(child_env, expected)
    with tempfile.TemporaryDirectory(prefix="agentgov-daemon-probe-") as raw_probe:
        probe = Path(raw_probe)
        probe.chmod(0o700)
        daemon.verify_host_filesystem(child_env, probe, image_id)
    daemon.verify(child_env, expected)


def _compose_files(source_root: Path) -> tuple[Path, Path]:
    return source_root / "docker/docker-compose.yml", source_root / "docker/docker-compose.langfuse.yml"


def _compose(snapshot: Path, source_root: Path = REPO_ROOT, *, langfuse: bool = False) -> list[str]:
    compose_files = _compose_files(source_root)
    command = ["docker", "compose", "--env-file", snapshot.as_posix(), "-f", compose_files[0].as_posix()]
    if langfuse:
        command.extend(("-f", compose_files[1].as_posix()))
    return command


def _preflight(snapshot: Path, child_env: dict[str, str]) -> None:
    _run(["python", "scripts/check_public_bind.py", "--env-file", snapshot.as_posix()], child_env)
    _run(
        [
            "python",
            "scripts/agentscope_atomic_cutover.py",
            "inspect",
            "--env-file",
            snapshot.as_posix(),
            "--require-current-or-empty",
        ],
        child_env,
    )


def _bootstrap(
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
) -> None:
    _run(
        [
            "python",
            "scripts/bootstrap_runtime_volume.py",
            "--env-file",
            snapshot.as_posix(),
            "--env-base-dir",
            source_base.as_posix(),
            "--bootstrap-dir",
            (source_root / "docker/runtime-bootstrap").as_posix(),
        ],
        child_env,
    )


def _prepare_harnesses(snapshot: Path, source_root: Path, child_env: dict[str, str]) -> None:
    _run(
        [
            *_compose(snapshot, source_root),
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
        child_env,
    )


def _langfuse_prepare(snapshot: Path, source_root: Path, child_env: dict[str, str]) -> None:
    _run(
        [
            *_compose(snapshot, source_root, langfuse=True),
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
        child_env,
    )


def _rendered_services(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    langfuse: bool,
) -> ComposeServices:
    return image_inventory.rendered_services(
        snapshot,
        source_root,
        child_env,
        langfuse=langfuse,
        compose_builder=_compose,
        run_output=_run_output,
    )


def _required_external_image_references(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
) -> ImageReferences:
    return image_inventory.required_external_image_references(
        snapshot,
        source_root,
        child_env,
        load_services=_rendered_services,
    )


def _inspect_image_id(reference: str, child_env: dict[str, str], *, service: str) -> str:
    return image_inventory.inspect_image_id(
        reference,
        child_env,
        service=service,
        run_output=_run_output,
    )


def _prepare_required_external_images(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    daemon_identity: Mapping[str, object] | None = None,
) -> StackImageIds:
    def verify_probe(image_id: str) -> None:
        if daemon_identity is not None:
            _verify_local_daemon(child_env, daemon_identity, image_id)

    def verify_before_pull() -> None:
        if daemon_identity is not None:
            _local_daemon_support(child_env).verify(child_env, daemon_identity)

    return image_inventory.prepare_required_external_images(
        snapshot,
        source_root,
        child_env,
        load_references=_required_external_image_references,
        inspect_image=_inspect_image_id,
        run_command=_run,
        verify_inventory=_verify_required_external_images,
        verify_bootstrap_probe=verify_probe,
        verify_before_bootstrap_pull=verify_before_pull,
    )


def _verify_required_external_images(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
) -> StackImageIds:
    return image_inventory.verify_required_external_images(
        snapshot,
        source_root,
        child_env,
        load_references=_required_external_image_references,
        inspect_image=_inspect_image_id,
    )


def _start_stack(
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
    *,
    langfuse: bool,
    no_build: bool,
    force_recreate: bool,
) -> None:
    _preflight(snapshot, child_env)
    _bootstrap(snapshot, source_root, source_base, child_env)
    _prepare_harnesses(snapshot, source_root, child_env)
    if langfuse:
        _langfuse_prepare(snapshot, source_root, child_env)
    command = [*_compose(snapshot, source_root, langfuse=langfuse)]
    if langfuse:
        command.extend(("--profile", "langfuse"))
    command.extend(("up", "-d", "--wait", "--pull", "never"))
    if langfuse:
        command.append("--remove-orphans")
    if no_build:
        command.append("--no-build")
    if force_recreate:
        command.append("--force-recreate")
    if _run(command, child_env, check=False) != 0:
        _run(["bash", "scripts/compose_diagnose.sh"], child_env, check=False)
        raise SelectedEnvError("Compose stack startup failed")
    _run(["python", "scripts/diagnose_runtime_health.py", "--env-file", snapshot.as_posix(), "--require-ready"], child_env)


def _execute_runtime_operation(
    operation: str,
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
) -> int:
    if operation == "runtime-bootstrap":
        _bootstrap(snapshot, source_root, source_base, child_env)
        return 0
    if operation == "runtime-clean":
        return _run(
            ["python", "scripts/cleanup_runtime_artifacts.py", "--env-file", snapshot.as_posix(), "--runtime-artifacts"],
            child_env,
        )
    if operation in {"runtime-migrate", "runtime-migrate-scan"}:
        command = [
            "python",
            "scripts/migrate_workspace_test_assets.py",
            "--env-file",
            snapshot.as_posix(),
            "--bootstrap-dir",
            (source_root / "docker/runtime-bootstrap").as_posix(),
        ]
        if operation == "runtime-migrate":
            command.append("--apply")
        return _run(command, child_env)
    if operation == "runtime-prepare-harnesses":
        _prepare_harnesses(snapshot, source_root, child_env)
        return 0
    raise SelectedEnvError(f"unsupported selected-env runtime operation: {operation}")


def _execute_operation(
    operation: str,
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
    *,
    no_build: bool,
    force_recreate: bool,
    daemon_identity: Mapping[str, object] | None = None,
) -> int:
    base = _compose(snapshot, source_root)
    langfuse = _compose(snapshot, source_root, langfuse=True)
    if operation in operation_contract.PREFLIGHT_OPERATIONS:
        _preflight(snapshot, child_env)
    if operation == "build":
        return _run([*base, "build", "--pull=false"], child_env)
    if operation == "images-prepare":
        _prepare_required_external_images(
            snapshot,
            source_root,
            child_env,
            daemon_identity=daemon_identity,
        )
        return 0
    if operation in {"up", "all-up"}:
        _start_stack(
            snapshot,
            source_root,
            source_base,
            child_env,
            langfuse=operation == "all-up",
            no_build=no_build,
            force_recreate=force_recreate,
        )
        return 0
    commands = operation_contract.simple_operation_commands(base, langfuse)
    if operation in commands:
        return _run(commands[operation], child_env)
    if operation == "langfuse-prepare":
        _langfuse_prepare(snapshot, source_root, child_env)
        return 0
    if operation == "langfuse-up":
        _langfuse_prepare(snapshot, source_root, child_env)
        return _run([*langfuse, "--profile", "langfuse", "up", "-d", "--wait", "--remove-orphans", "--pull", "never"], child_env)
    if operation in {"runtime-validate", "check"}:
        _run(
            ["python", "scripts/bootstrap_runtime_volume.py", "--env-file", snapshot.as_posix(), "--env-base-dir", source_base.as_posix(), "--dry-run"],
            child_env,
        )
        _run(["python", "scripts/check_agentscope_cutover.py"], child_env)
        return 0 if operation == "runtime-validate" else _run([*base, "config", "--services"], child_env)
    if operation.startswith("runtime-"):
        return _execute_runtime_operation(operation, snapshot, source_root, source_base, child_env)
    raise SelectedEnvError(f"unsupported selected-env operation: {operation}")


def _verify_stack_images(
    child_env: dict[str, str],
    snapshot: Path,
    version: str,
    source_digest: str,
    *,
    source_root: Path = REPO_ROOT,
    langfuse: bool,
    running: bool,
    expected_ids: StackImageIds | None = None,
    services: tuple[str, ...] | None = None,
) -> StackImageIds:
    return image_inventory.verify_stack_images(
        child_env,
        snapshot,
        version,
        source_digest,
        source_root=source_root,
        langfuse=langfuse,
        running=running,
        expected_ids=expected_ids,
        compose_builder=_compose,
        load_services=_rendered_services,
        run_output=_run_output,
        services=services,
    )


def _prepare_daemon_boundary(
    operation: str,
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    version: str,
    source_digest: str,
    locked_identity: Mapping[str, object] | None = None,
) -> DaemonBoundary:
    if operation not in _DOCKER_MUTATING_OPERATIONS:
        return None, None, None
    identity = locked_identity if locked_identity is not None else _capture_local_daemon(child_env)
    if operation == "images-prepare":
        try:
            _inspect_image_id(
                HOST_FILESYSTEM_PROBE_IMAGE,
                child_env,
                service="langfuse-postgres",
            )
        except operation_contract.MissingImageError:
            return identity, None, None
        _verify_local_daemon(child_env, identity, HOST_FILESYSTEM_PROBE_IMAGE)
        return identity, HOST_FILESYSTEM_PROBE_IMAGE, None
    if operation in _BUILD_OPERATIONS:
        prerequisites = _verify_required_external_images(
            snapshot,
            source_root,
            child_env,
        )
        probe_image = prerequisites.get("langfuse-postgres")
        if probe_image is None:
            raise SelectedEnvError("构建 prerequisite inventory 缺少 Docker host probe 镜像")
        _verify_local_daemon(child_env, identity, probe_image)
        return identity, probe_image, None
    _verify_local_daemon(child_env, identity, HOST_FILESYSTEM_PROBE_IMAGE)
    prepared_ids: StackImageIds | None = None
    if operation in _DOCKER_BIND_OPERATIONS:
        services = operation_contract.IMAGE_SCOPES.get(operation)
        prepared_ids = _verify_stack_images(
            child_env,
            snapshot,
            version,
            source_digest,
            source_root=source_root,
            langfuse=operation in {"all-up", "langfuse-up"},
            running=False,
            services=services,
        )
        persistent_source.materialize(
            source_root,
            source_digest,
            child_env[source_snapshot.SOURCE_DIGEST_ENV],
            child_env,
            helper_image=HOST_FILESYSTEM_PROBE_IMAGE,
            run_command=_run,
            hash_source=source_artifact_sha256,
            verify_daemon=lambda: _verify_local_daemon(child_env, identity, HOST_FILESYSTEM_PROBE_IMAGE),
        )
    return identity, HOST_FILESYSTEM_PROBE_IMAGE, prepared_ids


def _verify_daemon_after_operation(
    operation: str,
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    version: str,
    source_digest: str,
    identity: Mapping[str, object] | None,
    probe_image: str | None,
    prepared_ids: StackImageIds | None,
) -> None:
    if identity is None:
        return
    verified_ids: StackImageIds | None = None
    if operation == "images-prepare":
        verified_ids = _verify_required_external_images(snapshot, source_root, child_env)
        _verify_local_daemon(child_env, identity, verified_ids["langfuse-postgres"])
        return
    if operation in {"build", "ui-build", "ui-up", "ui-recreate", "runtime-recreate", "up", "all-up", "langfuse-up"}:
        verified_ids = _verify_stack_images(
            child_env,
            snapshot,
            version,
            source_digest,
            source_root=source_root,
            langfuse=operation in {"all-up", "langfuse-up"},
            running=operation in {"ui-up", "ui-recreate", "runtime-recreate", "up", "all-up", "langfuse-up"},
            expected_ids=prepared_ids,
            services=operation_contract.IMAGE_SCOPES.get(operation),
        )
    post_probe = next(iter(verified_ids.values())) if verified_ids is not None else probe_image
    if post_probe is None:
        raise SelectedEnvError("Docker daemon 状态变更后缺少可核验 host filesystem 的本地 Python 镜像")
    _verify_local_daemon(child_env, identity, post_probe)
    if operation == "down" or (operation in _DOCKER_BIND_OPERATIONS and operation not in {"runtime-recreate", "ui-recreate"}):
        keep_digest = source_digest if operation in {"all-up", "langfuse-up", "up"} else None
        persistent_source.cleanup_obsolete(
            child_env,
            keep_digest=keep_digest,
            helper_image=HOST_FILESYSTEM_PROBE_IMAGE,
            run_command=_run,
            run_output=_run_output,
            verify_daemon=lambda: _verify_local_daemon(child_env, identity, post_probe),
        )


def _verify_daemon_after_failure(
    child_env: dict[str, str],
    identity: Mapping[str, object] | None,
    probe_image: str | None,
) -> None:
    if identity is None:
        return
    if probe_image is None:
        _local_daemon_support(child_env).verify(child_env, identity)
        return
    _verify_local_daemon(child_env, identity, probe_image)


def run_operation(
    env_file: Path,
    operation: str,
    *,
    env_base_dir: Path | None = None,
    no_build: bool = False,
    force_recreate: bool = False,
) -> int:
    deployed_browser.require_operation_opt_in(operation, os.environ)
    if "~" in env_file.parts:
        raise SelectedEnvError("所选 Compose env 路径不得依赖 shell HOME 展开")
    source = env_file if env_file.is_absolute() else REPO_ROOT / env_file
    source = Path(os.path.abspath(source))
    source_base = selected_env_reexec.resolve_source_base(source, env_base_dir)
    payload, original_identity = _read_stable_regular_file(source)
    with tempfile.TemporaryDirectory(prefix="agentgov-selected-env-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        snapshot = _write_snapshot(directory, payload)
        input_environment = source_snapshot.seal_operation_input(snapshot)
        parse_selected_env_bindings(snapshot)
        operation_contract.require_current_epoch_env(snapshot, operation)
        frozen_source, version = selected_env_reexec.freeze_deployment_source(REPO_ROOT, operation, directory, snapshot)
        source_root = frozen_source.root
        child_env = source_snapshot.operation_environment(
            directory,
            snapshot,
            frozen_source,
            version,
            compose_files=_compose_files(source_root),
            include_buildx=operation in _BUILD_OPERATIONS,
            include_python=True,
            input_environment=input_environment,
        )
        verify_stable_env_file(source, payload, original_identity, error_type=SelectedEnvError)
        stage_env = selected_env_reexec.stage_environment(
            child_env,
            original_env=source,
            original_identity=original_identity,
            original_payload=payload,
            live_repo_root=REPO_ROOT,
        )
        command = selected_env_reexec.frozen_command(
            source_root,
            snapshot,
            source_base,
            operation,
            no_build=no_build,
            force_recreate=force_recreate,
        )
        return deployed_browser.run_frozen_command(operation, directory, source_root, stage_env, command)


def _run_frozen_stage(
    env_file: Path,
    operation: str,
    *,
    env_base_dir: Path | None = None,
    no_build: bool = False,
    force_recreate: bool = False,
) -> int:
    child_env = dict(os.environ)
    state = selected_env_reexec.load_frozen_stage(child_env)
    source_root = _verified_command_root(child_env)
    selected_env_reexec.verify_running_from_frozen_source(source_root, Path(__file__))
    snapshot = source_snapshot.verify_operation_input(child_env)
    if snapshot is None or snapshot != env_file:
        raise SelectedEnvError("冻结 runner 的 selected.env 参数与已绑定 input 不一致")
    source_base = selected_env_reexec.resolve_source_base(state.original_env, env_base_dir)
    version = (source_root / "VERSION").read_text(encoding="utf-8").strip()
    digest = child_env.get("AGENTGOV_SOURCE_ARTIFACT_SHA256", "")
    if not version or child_env.get("APP_VERSION") != version or len(digest) != 64:
        raise SelectedEnvError("冻结 runner 的 version/source identity 无效")
    payload = snapshot.read_bytes()
    if hashlib.sha256(payload).hexdigest() != state.original_digest:
        raise SelectedEnvError("冻结 runner 的 selected.env bytes 不匹配 trust anchor")

    def execute() -> int:
        return _execute_frozen_lifecycle(
            operation,
            snapshot,
            source_root,
            source_base,
            child_env,
            version,
            digest,
            no_build=no_build,
            force_recreate=force_recreate,
        )

    return run_with_global_cutover_lock(SelectedEnvError, execute) if operation in _MUTATING_OPERATIONS else execute()


def _execute_frozen_lifecycle(
    operation: str,
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
    version: str,
    digest: str,
    *,
    no_build: bool,
    force_recreate: bool,
) -> int:
    if operation == deployed_browser.OPERATION:
        return deployed_browser.run_deployed_browser(snapshot, source_root, source_base, child_env, version, digest)
    with _daemon_mutation_lock(operation, child_env) as locked_identity:
        daemon_identity, probe_image, prepared_ids = _prepare_daemon_boundary(
            operation,
            snapshot,
            source_root,
            child_env,
            version,
            digest,
            locked_identity,
        )
        try:
            result = _execute_operation(
                operation,
                snapshot,
                source_root,
                source_base,
                child_env,
                no_build=no_build,
                force_recreate=force_recreate,
                daemon_identity=daemon_identity,
            )
        except BaseException:
            _verify_daemon_after_failure(child_env, daemon_identity, probe_image)
            raise
        _verify_daemon_after_operation(
            operation,
            snapshot,
            source_root,
            child_env,
            version,
            digest,
            daemon_identity,
            probe_image,
            prepared_ids,
        )
    _verify_frozen_stage_postconditions(snapshot, source_root, child_env, digest)
    return result


def _verify_frozen_stage_postconditions(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    digest: str,
) -> None:
    state = selected_env_reexec.load_frozen_stage(child_env)
    source_snapshot.verify_command_source(child_env, hash_source=source_artifact_sha256)
    payload = snapshot.read_bytes()
    verify_stable_env_file(state.original_env, payload, state.original_identity, error_type=SelectedEnvError)
    if source_artifact_sha256(state.live_repo_root) != digest:
        raise SelectedEnvError("deployable source 在部署事务期间发生变化；镜像/容器结果拒绝放行")


def main() -> int:
    return selected_env_operation_cli.run_cli(
        _run_frozen_stage if selected_env_reexec.is_frozen_stage(os.environ) else run_operation,
        OPERATIONS,
        (OSError, UnicodeError, ValueError, SelectedEnvError),
    )


if __name__ == "__main__":
    raise SystemExit(main())
