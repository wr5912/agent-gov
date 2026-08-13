#!/usr/bin/env python3
"""只在 immutable candidate snapshot 内恢复并执行公共容器验收。"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import agent_test_acceptance_support as acceptance_support  # noqa: E402
from scripts import container_acceptance_candidate as acceptance_candidate  # noqa: E402
from scripts import container_acceptance_candidate_authority as candidate_authority  # noqa: E402
from scripts import container_acceptance_contract as acceptance_contract  # noqa: E402
from scripts import container_acceptance_environment as acceptance_environment  # noqa: E402
from scripts import container_acceptance_lock as acceptance_lock  # noqa: E402
from scripts import container_acceptance_profiles as acceptance_profiles  # noqa: E402
from scripts import container_acceptance_receipt as acceptance_receipt  # noqa: E402
from scripts import container_acceptance_signals as acceptance_signals  # noqa: E402
from scripts import container_acceptance_terminal_freshness as acceptance_terminal  # noqa: E402
from scripts import container_acceptance_toolchain as acceptance_toolchain  # noqa: E402
from scripts import container_acceptance_verifier_process as acceptance_verifier_process  # noqa: E402
from scripts.container_acceptance_profiles import PROFILES, AcceptanceProfile  # noqa: E402

CORE_SERVICES: Final = acceptance_profiles.CORE_SERVICES
CORE_BUILD_SERVICES: Final = acceptance_profiles.CORE_BUILD_SERVICES
LANGFUSE_SERVICES: Final = acceptance_profiles.LANGFUSE_SERVICES
COMPOSE_FILE: Final = REPO_ROOT / "docker/docker-compose.yml"
ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
RUN_ID_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUN_ID"
PROFILE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE"
RUNTIME_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT"
CANCEL_FORCE_KILL_SECONDS: float = 1.0
CLEANUP_PROCESS_TIMEOUT_SECONDS: float = 60.0
PROCESS_WAIT_POLL_SECONDS: float = 0.1
_IMAGE_ID: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_F_ADD_SEALS: Final = 1033
_F_GET_SEALS: Final = 1034
_MEMFD_SEALS: Final = 0x0001 | 0x0002 | 0x0004 | 0x0008
_MFD_CLOEXEC: Final = 0x0001
_MFD_ALLOW_SEALING: Final = 0x0002
_FAILURE_CODE_PART: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_MAX_FAILURE_CODE_DEPTH: Final = 4
_FAILURE_PHASE_ATTRIBUTE: Final = "_agentgov_acceptance_phase"
_FAILURE_PHASES: Final = frozenset(
    {
        "source_preflight",
        "profile_refresh",
        "verifier",
        "source_postflight",
        "image_postflight",
        "runtime_cleanup",
        "cleanup_freshness",
        "candidate_cleanup",
        "terminal_freshness",
        "terminal_commit",
    }
)


class CandidateDependencyEnvironment(dict[str, str]):
    """从 Prepared candidate 派生的固定依赖环境。"""


@dataclass(frozen=True, slots=True)
class ProcessBoundary:
    cwd: Path
    environment: dict[str, str] | None


class AcceptanceError(RuntimeError):
    """受管 snapshot runner 验收失败。"""


class AcceptanceAuthorityError(AcceptanceError):
    """候选源码、镜像或运行态 identity 已漂移。"""


AcceptanceCancelled = acceptance_signals.AcceptanceCancelled


def _safe_failure_code(error: BaseException) -> str:
    """仅投影有界异常类型链，不泄露异常消息中的路径或配置值。"""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(parts) < _MAX_FAILURE_CODE_DEPTH and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        parts.append(name if _FAILURE_CODE_PART.fullmatch(name) is not None else "UnknownFailure")
        current = current.__cause__ or current.__context__
    return ".".join(parts)


def _mark_failure_phase(error: BaseException, phase: str) -> BaseException:
    if phase not in _FAILURE_PHASES:
        raise RuntimeError("container acceptance failure phase is not registered")
    error.__dict__[_FAILURE_PHASE_ATTRIBUTE] = phase
    return error


def _safe_failure_phase(error: BaseException) -> str:
    phase = error.__dict__.get(_FAILURE_PHASE_ATTRIBUTE)
    return phase if isinstance(phase, str) and phase in _FAILURE_PHASES else "resume"


@dataclass(slots=True)
class _ResumeContext:
    profile: AcceptanceProfile
    verifier: acceptance_contract.AcceptanceVerifierIdentity
    candidate: candidate_authority.PreparedCandidateAuthority
    receipt: acceptance_receipt.PreparedReceiptAuthority
    managed: acceptance_contract.ManagedAcceptanceEnvironment
    lock: acceptance_lock.AcceptanceLifecycleLock
    runtime: acceptance_environment.IsolatedRuntimeAuthority


_SignalController = acceptance_signals.SignalController
_ACTIVE_SIGNAL_CONTROLLER = acceptance_signals.ACTIVE_SIGNAL_CONTROLLER


def _signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
    acceptance_signals.signal_process_group(process, signum)


def _kill_and_reap_process_group(process: subprocess.Popen[str]) -> None:
    _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=CANCEL_FORCE_KILL_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceError("cleanup subprocess SIGKILL 后未退出") from exc
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def _controlled_signals() -> Iterator[_SignalController]:
    with acceptance_signals.controlled_signals(cancel_force_kill_seconds=CANCEL_FORCE_KILL_SECONDS) as controller:
        yield controller


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在 immutable snapshot 内执行公共容器验收。")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("必须在 `--` 后提供固定 verifier")
    return args


def compose_command(profile: AcceptanceProfile, env_file: Path, env: Mapping[str, str]) -> list[str]:
    docker = env.get(acceptance_toolchain.DOCKER_EXECUTABLE_ENV)
    if not isinstance(docker, str) or not docker:
        raise AcceptanceError("固定 Docker authority 不可用")
    command = [docker, "compose", "--parallel", "3", "--env-file", str(env_file), "-f", str(COMPOSE_FILE)]
    for compose_profile in profile.compose_profiles:
        command.extend(["--profile", compose_profile])
    for overlay in profile.compose_overlays:
        command.extend(["-f", str(REPO_ROOT / overlay)])
    return command


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    capture: bool,
    pass_fds: tuple[int, ...] = (),
    terminal_authority: bool = False,
) -> tuple[int, str]:
    controller = _ACTIVE_SIGNAL_CONTROLLER.get()
    if controller is not None:
        controller._raise_if_cancelled()
    if terminal_authority and (env is None or command[:1] != [env.get(acceptance_toolchain.DOCKER_EXECUTABLE_ENV)]):
        raise AcceptanceError("终态 freshness 仅允许固定 Docker authority")
    boundary = _process_boundary(cwd, env, terminal_authority=terminal_authority)
    process = subprocess.Popen(
        command,
        cwd=boundary.cwd,
        env=boundary.environment,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        start_new_session=True,
        pass_fds=pass_fds,
    )
    if controller is not None:
        controller._bind(process)
    deadline = time.monotonic() + CLEANUP_PROCESS_TIMEOUT_SECONDS if controller and controller._cleaning_up else None
    timed_out = False
    try:
        while True:
            try:
                stdout, _stderr = process.communicate(timeout=PROCESS_WAIT_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                forced = controller is not None and controller._escalate_if_needed(process)
                timed_out = deadline is not None and time.monotonic() >= deadline
                if forced or timed_out:
                    _kill_and_reap_process_group(process)
                    stdout = ""
                    break
    finally:
        if controller is not None:
            controller._unbind(process)
    if controller is not None:
        controller._raise_if_cancelled()
    if timed_out:
        raise AcceptanceError("隔离验收 cleanup subprocess 超时")
    return process.returncode, stdout or ""


def _process_boundary(cwd: Path, env: dict[str, str] | None, *, terminal_authority: bool = False) -> ProcessBoundary:
    if env is None or env.get(ACTIVE_ENV) != "1":
        return ProcessBoundary(cwd=cwd, environment=env)
    try:
        if terminal_authority:
            return ProcessBoundary(cwd=Path("/"), environment=dict(acceptance_contract.terminal_process_environment(env)))
        acceptance_contract.validate_managed_environment(env)
    except acceptance_contract.AcceptanceContractError as exc:
        raise AcceptanceError("容器验收子进程环境 authority 已漂移") from exc
    return ProcessBoundary(cwd=cwd, environment=env)


def _run_checked(
    command: list[str],
    *,
    env: dict[str, str],
    label: str,
    capture: bool = False,
    pass_fds: tuple[int, ...] = (),
    terminal_authority: bool = False,
) -> str:
    try:
        returncode, stdout = _run_process(command, cwd=REPO_ROOT, env=env, capture=capture, pass_fds=pass_fds, terminal_authority=terminal_authority)
    except OSError as exc:
        raise AcceptanceError(f"{label}无法启动，验收命令未执行") from exc
    if returncode:
        raise AcceptanceError(f"{label}失败，验收命令未执行")
    return stdout.strip() if capture else ""


def _validate_service_model(base: list[str], profile: AcceptanceProfile, env: dict[str, str]) -> None:
    services = _run_checked([*base, "config", "--services"], env=env, label="Compose 服务解析", capture=True)
    if set(services.splitlines()) != set(profile.expected_services):
        raise AcceptanceAuthorityError("Compose 服务集合与验收 profile 不一致")
    raw = _run_checked([*base, "config", "--format", "json"], env=env, label="Compose bootstrap 解析", capture=True)
    try:
        config = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError("Compose bootstrap 配置不是有效 JSON") from exc
    services_model = config.get("services") if isinstance(config, dict) else None
    api = services_model.get("claude-agent-api") if isinstance(services_model, dict) else None
    volumes = api.get("volumes") if isinstance(api, dict) else None
    matches = (
        [item for item in volumes if isinstance(item, dict) and item.get("target") == "/app/docker/runtime-bootstrap"] if isinstance(volumes, list) else []
    )
    if matches:
        raise AcceptanceAuthorityError("Compose 不得覆盖候选镜像内 runtime-bootstrap")


def _capture_profile_images(
    profile: AcceptanceProfile,
    base: list[str],
    candidate: candidate_authority.AcceptanceCandidateIdentity,
    env: dict[str, str],
    docker_runner: acceptance_support.DockerRunner,
) -> tuple[acceptance_support.LocalImageEvidence, ...]:
    candidate_images = acceptance_support.capture_local_images(
        compose_base=base,
        services=profile.build_services,
        run_id=env[RUN_ID_ENV],
        candidate=candidate,
        docker_runner=docker_runner,
    )
    external_images = acceptance_support.capture_external_images(
        compose_base=base,
        services=profile.external_image_services,
        docker_runner=docker_runner,
    )
    return tuple(sorted((*candidate_images, *external_images), key=lambda item: item.service))


@contextmanager
def _sealed_image_overlay(service: str, image_id: str) -> Iterator[tuple[str, int]]:
    if not service or _IMAGE_ID.fullmatch(image_id) is None:
        raise AcceptanceError("Langfuse initializer image authority 无效")
    libc = ctypes.CDLL(None, use_errno=True)
    create = libc.memfd_create
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = int(create(b"agentgov-acceptance-compose-overlay", _MFD_CLOEXEC | _MFD_ALLOW_SEALING))
    if descriptor < 0:
        error = ctypes.get_errno()
        raise AcceptanceError("Langfuse initializer memfd authority 不可用") from OSError(error, os.strerror(error))
    try:
        encoded = acceptance_contract.canonical_json({"services": {service: {"image": image_id, "pull_policy": "never"}}})
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fchmod(descriptor, 0o400)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _MEMFD_SEALS)
        if fcntl.fcntl(descriptor, _F_GET_SEALS) != _MEMFD_SEALS:
            raise AcceptanceError("Langfuse initializer immutable overlay authority 无效")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield f"/proc/self/fd/{descriptor}", descriptor
    finally:
        os.close(descriptor)


def _run_volume_initializer(
    profile: AcceptanceProfile,
    base: list[str],
    env: dict[str, str],
    docker_runner: acceptance_support.DockerRunner,
) -> None:
    service = profile.volume_init_service
    if service is None:
        return
    maintenance = [*base, "--profile", "langfuse-maintenance"]
    try:
        evidence = acceptance_support.capture_external_images(
            compose_base=maintenance,
            services=(service,),
            docker_runner=docker_runner,
        )
    except acceptance_support.AcceptanceSupportError as exc:
        raise AcceptanceAuthorityError("Langfuse initializer image authority 不可用") from exc
    image_id = evidence[0].image_id
    with _sealed_image_overlay(service, image_id) as (overlay, descriptor):
        pinned = [*maintenance, "-f", overlay]
        raw = _run_checked(
            [*pinned, "config", "--format", "json"],
            env=env,
            label="Langfuse initializer model 复核",
            capture=True,
            pass_fds=(descriptor,),
        )
        try:
            config = json.loads(raw)
            model = config["services"][service]
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceError("Langfuse initializer model authority 无效") from exc
        if not isinstance(model, dict) or model.get("image") != image_id or model.get("pull_policy") != "never":
            raise AcceptanceAuthorityError("Langfuse initializer 未固定 immutable image ID")
        _run_checked(
            [*pinned, "run", "--rm", "--no-deps", "-T", "--pull", "never", service],
            env=env,
            label="Langfuse volume 初始化",
            pass_fds=(descriptor,),
        )


def refresh_profile(
    profile: AcceptanceProfile,
    env_file: Path,
    candidate: candidate_authority.AcceptanceCandidateIdentity,
    env: dict[str, str],
) -> tuple[acceptance_support.LocalImageEvidence, ...]:
    base = compose_command(profile, env_file, env)
    _validate_service_model(base, profile, env)

    def docker_runner(command: list[str]) -> str:
        return _run_checked(command, env=env, label="镜像/容器 authority 复核", capture=True)

    try:
        external_before = acceptance_support.capture_external_images(
            compose_base=base,
            services=profile.external_image_services,
            docker_runner=docker_runner,
        )
        _run_volume_initializer(profile, base, env, docker_runner)
        external_after_init = acceptance_support.capture_external_images(
            compose_base=base,
            services=profile.external_image_services,
            docker_runner=docker_runner,
        )
        if external_after_init != external_before:
            raise AcceptanceAuthorityError("Langfuse initializer 期间 external image authority 漂移")
        _run_checked([*base, "build", *profile.build_services], env=env, label="Compose 镜像重建")
        candidate_images = acceptance_support.capture_local_images(
            compose_base=base,
            services=profile.build_services,
            run_id=env[RUN_ID_ENV],
            candidate=candidate,
            docker_runner=docker_runner,
        )
        external_after_build = acceptance_support.capture_external_images(
            compose_base=base,
            services=profile.external_image_services,
            docker_runner=docker_runner,
        )
        if external_after_build != external_before:
            raise AcceptanceAuthorityError("Compose build 期间 external image authority 漂移")
        images = tuple(sorted((*candidate_images, *external_before), key=lambda item: item.service))
    except acceptance_support.AcceptanceSupportError as exc:
        raise AcceptanceAuthorityError("本轮镜像未绑定候选 authority") from exc
    started_at = datetime.now(timezone.utc)
    _run_checked(
        [*base, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "180", "--remove-orphans", *profile.services_to_run],
        env=env,
        label="Compose 服务 recreate",
    )
    authorities = {item.service: item for item in images}
    for service in profile.services_to_run:
        expected = authorities.get(service)
        if expected is None:
            raise AcceptanceAuthorityError("运行服务缺少冻结镜像 authority")
        try:
            acceptance_support.verify_running_container(
                compose_base=base,
                service=service,
                run_id=env[RUN_ID_ENV],
                candidate=candidate,
                started_at=started_at,
                expected_image_id=expected.image_id,
                docker_runner=docker_runner,
                image_kind=expected.kind,
            )
        except acceptance_support.AcceptanceSupportError as exc:
            raise AcceptanceAuthorityError(f"服务 {service} 未绑定冻结镜像 authority") from exc
    return images


def _recapture_images(
    profile: AcceptanceProfile,
    env_file: Path,
    candidate: candidate_authority.AcceptanceCandidateIdentity,
    env: dict[str, str],
) -> tuple[acceptance_support.LocalImageEvidence, ...]:
    base = compose_command(profile, env_file, env)
    try:
        return _capture_profile_images(
            profile,
            base,
            candidate,
            env,
            lambda command: _run_checked(command, env=env, label="镜像 authority 摘要复核", capture=True),
        )
    except acceptance_support.AcceptanceSupportError as exc:
        raise AcceptanceAuthorityError("无法复核冻结镜像 authority") from exc


def _dependency_values(candidate: candidate_authority.PreparedCandidateAuthority) -> CandidateDependencyEnvironment:
    frontend = candidate.snapshot.frontend_dependencies
    python = candidate.snapshot.python_dependencies
    pnpm = candidate.snapshot.pnpm_dependencies
    return CandidateDependencyEnvironment(
        {
            acceptance_toolchain.NODE_EXECUTABLE_ENV: str(candidate.node_executable),
            acceptance_toolchain.FRONTEND_DEPENDENCY_ROOT_ENV: str(frontend.root),
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256": frontend.sha256,
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES": str(frontend.entries),
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES": str(frontend.regular_bytes),
            acceptance_toolchain.PYTHON_SITE_PACKAGES_ENV: str(python.root),
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256": python.sha256,
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES": str(python.entries),
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES": str(python.regular_bytes),
            acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV: str(pnpm.root),
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256": pnpm.sha256,
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES": str(pnpm.entries),
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES": str(pnpm.regular_bytes),
        }
    )


def _validate_dependency_identity(
    managed: Mapping[str, str],
    candidate: candidate_authority.PreparedCandidateAuthority,
) -> None:
    if any(managed.get(key) != value for key, value in _dependency_values(candidate).items()):
        raise AcceptanceError("managed dependency identity 不属于冻结 candidate snapshot")


def _actual_loaded_repository_sources() -> tuple[Path, ...]:
    loaded: set[Path] = {Path(__file__).resolve(strict=True)}
    for module in tuple(sys.modules.values()):
        raw = getattr(module, "__file__", None)
        if not isinstance(raw, str):
            continue
        try:
            path = Path(raw).resolve(strict=True)
        except OSError as exc:
            raise AcceptanceError("实际加载的 snapshot source path authority 无效") from exc
        try:
            path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        loaded.add(path)
    return tuple(sorted(loaded, key=lambda item: str(item).encode()))


def _validate_loaded_sources(candidate: candidate_authority.PreparedCandidateAuthority) -> None:
    actual = _actual_loaded_repository_sources()
    required = (
        PurePosixPath("scripts/container_acceptance_contract.py"),
        PurePosixPath("scripts/run_container_acceptance.py"),
    )
    try:
        acceptance_candidate.require_snapshot_loaded_file(
            candidate,
            REPO_ROOT / "scripts/container_acceptance_bootstrap.py",
            PurePosixPath("scripts/container_acceptance_bootstrap.py"),
        )
        acceptance_candidate.require_snapshot_loaded_sources(
            candidate,
            actual,
            required_relative_paths=required,
        )
    except acceptance_candidate.CandidateSnapshotError as exc:
        raise AcceptanceError("snapshot loaded-source authority 无效") from exc


def _validate_resume_identity(
    args: argparse.Namespace,
    transport: Mapping[str, str],
    managed: acceptance_contract.ManagedAcceptanceEnvironment,
    candidate: candidate_authority.PreparedCandidateAuthority,
    receipt: acceptance_receipt.PreparedReceiptAuthority,
) -> tuple[AcceptanceProfile, acceptance_contract.AcceptanceVerifierIdentity]:
    profile = PROFILES[args.profile]
    try:
        verifier = acceptance_contract.resolve_verifier(profile.name, args.command)
        acceptance_toolchain.validate_execution_tool_authority(managed)
        acceptance_candidate.verify_candidate_snapshot(
            candidate,
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
            projection_validator=acceptance_toolchain.validate_frontend_dependency_projection,
        )
    except (acceptance_contract.AcceptanceContractError, acceptance_toolchain.ToolchainAuthorityError, acceptance_candidate.CandidateSnapshotError) as exc:
        raise AcceptanceError("snapshot resume authority 无效") from exc
    reservation = receipt.identity.reserved.candidate_reservation
    source = candidate.source
    expected_managed = {
        RUN_ID_ENV: candidate.snapshot.run_id,
        PROFILE_ENV: candidate.snapshot.profile,
        RUNTIME_ROOT_ENV: str(candidate.runtime_root),
        "COMPOSE_ENV_FILE": str(candidate.env_file),
        "AGENT_GOV_COMPOSE_ENV_FILE": str(candidate.env_file),
        acceptance_support.ACCEPTANCE_CANDIDATE_TREE_ENV: candidate.snapshot.git_tree_sha,
        acceptance_support.ACCEPTANCE_ENV_DIGEST_ENV: candidate.snapshot.selected_env_sha256,
        **_dependency_values(candidate),
    }
    valid = (
        candidate.snapshot_repository_root == REPO_ROOT
        and Path(__file__).resolve(strict=True) == candidate.snapshot_runner_path
        and Path(args.env_file) == candidate.env_file
        and transport.get(acceptance_toolchain.SNAPSHOT_ROOT_ENV) == str(candidate.snapshot.root)
        and receipt.identity.profile == candidate.snapshot.profile == profile.name
        and receipt.identity.run_id == candidate.snapshot.run_id
        and receipt.identity.verifier == verifier
        and receipt.identity.candidate_snapshot == candidate.recovery
        and receipt.identity.reserved_sha256 == candidate.snapshot.reserved_receipt_sha256
        and receipt.managed_environment_sha256 == managed.get(acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV)
        and source.repository_root == reservation.repository_root
        and source.selected_env_file == reservation.selected_env_file
        and source.allow_public_env_read == reservation.allow_public_env_read
        and all(managed.get(key) == value for key, value in expected_managed.items())
    )
    if not valid:
        raise AcceptanceError("snapshot candidate/receipt/environment identity 不一致")
    _validate_dependency_identity(managed, candidate)
    requirement = acceptance_toolchain.receipt_root_requirement()
    acceptance_toolchain.validate_receipt_root(requirement)
    if receipt.path.parent != requirement.path:
        raise AcceptanceError("prepared receipt root authority 不一致")
    receipt.verify_current()
    _validate_loaded_sources(candidate)
    return profile, verifier


def _resume_context(args: argparse.Namespace, environ: Mapping[str, str]) -> _ResumeContext:
    try:
        transport, managed = acceptance_contract.split_reexec_environment(environ)
        candidate = candidate_authority.PreparedCandidateAuthority.from_json(transport[candidate_authority.PREPARED_CANDIDATE_AUTHORITY_ENV])
        receipt = acceptance_receipt.PreparedReceiptAuthority.from_json(transport[acceptance_receipt.PREPARED_RECEIPT_AUTHORITY_ENV])
        lock = acceptance_lock.acquire_lifecycle_lock(transport, timeout_seconds=0)
    except (KeyError, ValueError, acceptance_contract.AcceptanceContractError, acceptance_lock.AcceptanceLockError) as exc:
        raise AcceptanceError("snapshot reexec transport authority 无效") from exc
    try:
        receipt.verify_lifecycle_lock(lock.descriptor)
        if environ is os.environ:
            os.environ.clear()
            os.environ.update(managed)
        lock.assert_current()
        profile, verifier = _validate_resume_identity(args, transport, managed, candidate, receipt)
        runtime = acceptance_environment.open_prepared_runtime(candidate, profile)
        return _ResumeContext(profile, verifier, candidate, receipt, managed, lock, runtime)
    except BaseException:
        lock.close()
        raise


def _cleanup_runtime(context: _ResumeContext, controller: _SignalController) -> None:
    controller._begin_cleanup()
    env = dict(context.managed)
    if context.profile.isolated_runtime:
        base = compose_command(context.profile, context.candidate.env_file, env)
        acceptance_environment.cleanup_isolated_runtime(
            profile_name=context.profile.name,
            expected_services=context.profile.expected_services,
            compose_base=base,
            project_name=env.get("COMPOSE_PROJECT_NAME", ""),
            run_id=context.candidate.snapshot.run_id,
            runtime=context.runtime,
            runner=lambda command, capture=False: _run_checked(
                command,
                env=env,
                label="隔离验收精确清理",
                capture=capture,
            ),
        )
    else:
        acceptance_environment.cleanup_candidate_runtime_root(context.runtime)


def _require_source_current(candidate: candidate_authority.PreparedCandidateAuthority) -> None:
    try:
        acceptance_candidate.require_candidate_source_current(
            candidate,
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        )
    except acceptance_candidate.CandidateSnapshotError as exc:
        raise AcceptanceAuthorityError("候选 source freshness authority 已漂移") from exc


def _require_images_current(
    context: _ResumeContext,
    candidate: candidate_authority.AcceptanceCandidateIdentity,
    env: dict[str, str],
    images: tuple[acceptance_support.LocalImageEvidence, ...],
    *,
    phase: str,
) -> None:
    if _recapture_images(context.profile, context.candidate.env_file, candidate, env) != images:
        raise AcceptanceAuthorityError(f"{phase}镜像 authority 已漂移")
    acceptance_toolchain.validate_browser_runtime_authority()


def _run_acceptance_verifier(context: _ResumeContext) -> int:
    return acceptance_verifier_process.run_verifier_process(
        context,
        list(acceptance_contract.verifier_execution_argv(context.profile.name, context.verifier, REPO_ROOT)),
        process_runner=lambda command, environment, pass_fds: _run_process(
            command,
            cwd=REPO_ROOT,
            env=environment,
            capture=False,
            pass_fds=pass_fds,
        )[0],
    )


def _execute_with_cleanup(context: _ResumeContext, controller: _SignalController) -> int:
    candidate_identity = context.candidate.recovery.snapshot
    candidate = candidate_authority.AcceptanceCandidateIdentity(
        candidate_identity.git_tree_sha,
        candidate_identity.selected_env_sha256,
    )
    env = dict(context.managed)
    outcome = acceptance_terminal.execute_acceptance(
        controller,
        require_source=lambda: _require_source_current(context.candidate),
        refresh=lambda: refresh_profile(context.profile, context.candidate.env_file, candidate, env),
        verifier=lambda: _run_acceptance_verifier(context),
        require_images=lambda images: _require_images_current(context, candidate, env, images, phase="容器验收期间"),
        mark_failure=_mark_failure_phase,
    )
    failure, terminal_witness = acceptance_terminal.cleanup_acceptance(
        outcome,
        cleanup_runtime=lambda: _cleanup_runtime(context, controller),
        validate_freshness=lambda failure: acceptance_terminal.validate_post_cleanup_freshness(
            failure,
            require_images=(lambda: _require_images_current(context, candidate, env, outcome.images, phase="容器验收清理后")) if outcome.images else None,
            require_source=lambda: _require_source_current(context.candidate),
            expected_errors=(AcceptanceError, acceptance_toolchain.ToolchainAuthorityError),
        ),
        capture_witness=lambda: acceptance_terminal.capture_cleanup_witness(
            context.candidate,
            compose_base=compose_command(context.profile, context.candidate.env_file, env),
            images=outcome.images,
            running_services=() if context.profile.isolated_runtime else context.profile.services_to_run,
            run_id=env[RUN_ID_ENV],
            identity=candidate,
            docker_action=lambda command: _run_checked(command, env=env, label="终态 image authority 复核", capture=True),
        ),
        cleanup_candidate=lambda: acceptance_candidate.cleanup_candidate_snapshot(
            context.candidate,
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        ),
        mark_failure=_mark_failure_phase,
    )
    try:
        context.lock.assert_current()
        acceptance_toolchain.validate_receipt_root(acceptance_toolchain.receipt_root_requirement())
        try:
            receipt_digest, cancellation, failure = acceptance_terminal.commit_terminal_receipt(
                controller,
                context.receipt,
                result=outcome.result,
                images=outcome.images,
                failure=failure,
                witness=terminal_witness,
                identity=candidate,
                docker_runner=acceptance_terminal.safe_docker_runner(
                    lambda command: _run_checked(command, env=env, label="终态 freshness 复核", capture=True, terminal_authority=True)
                ),
                mark_failure=_mark_failure_phase,
            )
        except BaseException as exc:
            _mark_failure_phase(exc, "terminal_commit")
            raise
    finally:
        if terminal_witness is not None:
            terminal_witness.close()
    if cancellation is not None:
        raise AcceptanceCancelled(cancellation)
    if failure is not None:
        raise failure
    if outcome.result is None:
        raise AcceptanceError("容器验收未产生 child 结果")
    if outcome.result == 0:
        print(
            f"CONTAINER_ACCEPTANCE_OK profile={context.profile.name} run_id={context.candidate.snapshot.run_id} "
            f"candidate_tree={candidate.git_tree_sha} selected_env_sha256={candidate.selected_env_sha256} "
            f"receipt_sha256={receipt_digest}"
        )
    return outcome.result


def run_snapshot_acceptance(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    context = _resume_context(args, environ)
    try:
        with _controlled_signals() as controller:
            return _execute_with_cleanup(context, controller)
    finally:
        context.runtime.close()
        context.lock.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return run_snapshot_acceptance(parse_args(argv), os.environ)
    except AcceptanceCancelled as exc:
        print(f"CONTAINER_ACCEPTANCE_CANCELLED signal={exc.signum}", file=sys.stderr)
        return 128 + exc.signum
    except (
        AcceptanceError,
        acceptance_environment.AcceptanceEnvironmentError,
        acceptance_support.AcceptanceSupportError,
        acceptance_candidate.CandidateSnapshotError,
        acceptance_terminal.TerminalFreshnessError,
        acceptance_toolchain.ToolchainAuthorityError,
        OSError,
    ) as exc:
        print(
            "CONTAINER_ACCEPTANCE_FAIL: 受管容器验收未完成；回执已由持久状态机收口或保留供恢复；"
            f"failure_phase={_safe_failure_phase(exc)} failure_code={_safe_failure_code(exc)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
