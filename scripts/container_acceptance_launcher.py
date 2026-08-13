"""公共容器验收首阶段的 reserved -> prepared -> snapshot re-exec 编排。"""

from __future__ import annotations

import importlib
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

from scripts import container_acceptance_candidate as acceptance_candidate
from scripts import container_acceptance_contract as acceptance_contract
from scripts import container_acceptance_environment as acceptance_environment
from scripts import container_acceptance_lock as acceptance_lock
from scripts import container_acceptance_profiles as acceptance_profiles
from scripts import container_acceptance_receipt as acceptance_receipt
from scripts import container_acceptance_tool_authority as tool_authority
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts.container_acceptance_candidate_authority import LoadedSourceIdentity, PreparedCandidateAuthority

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNAL_HANDOFF_ENV = "AGENT_GOV_ACCEPTANCE_SIGNAL_HANDOFF"
_SIGNAL_HANDOFF_VALUE = "blocked-v1"


class AcceptanceLauncherError(RuntimeError):
    """公共验收尚未进入受管 snapshot runner。"""


class AcceptanceLauncherInterrupted(SystemExit):
    """保留 POSIX signal 退出码，同时允许状态机先精确收口。"""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(128 + signum)


@contextmanager
def controlled_launcher_signals() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, signal.Handlers] = {}

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        raise AcceptanceLauncherInterrupted(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _defer_launcher_signals() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    watched = {signal.SIGINT, signal.SIGTERM}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _request(arguments: list[str]) -> tuple[acceptance_profiles.AcceptanceProfile, acceptance_contract.AcceptanceVerifierIdentity]:
    if len(arguments) < 4 or arguments[0] != "--profile" or "--" not in arguments:
        raise AcceptanceLauncherError("container acceptance launcher arguments are invalid")
    separator = arguments.index("--")
    if separator != 2:
        raise AcceptanceLauncherError("container acceptance launcher arguments are invalid")
    profile = acceptance_profiles.PROFILES.get(arguments[1])
    if profile is None:
        raise AcceptanceLauncherError("container acceptance profile is invalid")
    try:
        verifier = acceptance_contract.resolve_verifier(profile.name, arguments[separator + 1 :])
    except acceptance_contract.AcceptanceContractError as exc:
        raise AcceptanceLauncherError("container acceptance verifier is invalid") from exc
    return profile, verifier


def _bootstrap_authority(environ: Mapping[str, str]) -> None:
    python_digest = environ.get(acceptance_toolchain.BOOTSTRAP_PYTHON_SHA256_ENV, "")
    source_digest = environ.get(acceptance_toolchain.BOOTSTRAP_TOOLCHAIN_SHA256_ENV, "")
    stage = environ.get(acceptance_toolchain.BOOTSTRAP_STAGE_ENV, "")
    python_records = tuple(item for item in acceptance_toolchain.active_toolchain_authority().payload["tools"] if item["command"] == "python")
    bootstrap_records = tuple(item for item in acceptance_toolchain.active_toolchain_authority().payload["tools"] if item["command"] == "bootstrap-python")
    try:
        current_sha256 = acceptance_toolchain.actual_loaded_source_sha256("scripts/container_acceptance_toolchain.py")
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceLauncherError("bootstrap toolchain source authority is unavailable") from exc
    if (
        len(python_records) != 1
        or len(bootstrap_records) != 1
        or python_records[0]["sha256"] != python_digest
        or bootstrap_records[0]["sha256"] != stage
        or current_sha256 != source_digest
    ):
        raise AcceptanceLauncherError("bootstrap authority does not match the captured toolchain")


def _source_file(path: Path, expected_sha256: str | None = None) -> LoadedSourceIdentity:
    try:
        relative = path.relative_to(acceptance_toolchain.REPO_ROOT).as_posix()
        authority, _encoded = tool_authority.capture_small_file(
            path,
            "loaded-container-acceptance-source",
            allow_sticky_ancestor=True,
        )
    except (ValueError, tool_authority.ToolFileAuthorityError) as exc:
        raise AcceptanceLauncherError("loaded acceptance source authority is invalid") from exc
    if not relative.endswith(".py") and relative != "VERSION":
        raise AcceptanceLauncherError("loaded acceptance source is outside the fixed source set")
    if expected_sha256 is not None and authority["sha256"] != expected_sha256:
        raise AcceptanceLauncherError("bootstrap actual-loaded authority drifted")
    return LoadedSourceIdentity(relative, authority["sha256"])


def _loaded_sources(environ: Mapping[str, str]) -> tuple[LoadedSourceIdentity, ...]:
    loaded_bootstrap_sha256 = environ.get("AGENT_GOV_ACCEPTANCE_LOADED_BOOTSTRAP_SHA256", "")
    if _SHA256.fullmatch(loaded_bootstrap_sha256) is None:
        raise AcceptanceLauncherError("bootstrap actual-loaded authority is missing")
    try:
        importlib.import_module("scripts.container_acceptance_snapshot_authority")
        runner = importlib.import_module("scripts.run_container_acceptance")
        if not callable(getattr(runner, "main", None)):
            raise AcceptanceLauncherError("snapshot runner import closure is invalid")
        actual = acceptance_toolchain.freeze_actual_loaded_source_digests()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise AcceptanceLauncherError("actual-loaded source authority is unavailable") from exc
    required = {
        "scripts/container_acceptance_bootstrap.py": loaded_bootstrap_sha256,
        "scripts/container_acceptance_import_authority.py": environ.get("AGENT_GOV_ACCEPTANCE_BOOTSTRAP_IMPORT_AUTHORITY_SHA256", ""),
        "scripts/container_acceptance_toolchain.py": environ.get(acceptance_toolchain.BOOTSTRAP_TOOLCHAIN_SHA256_ENV, ""),
    }
    if any(_SHA256.fullmatch(value or "") is None or actual.get(path) != value for path, value in required.items()):
        raise AcceptanceLauncherError("actual-loaded bootstrap source authority is invalid")
    sources = [_source_file(acceptance_toolchain.REPO_ROOT / path, digest) for path, digest in actual.items()]
    sources.append(_source_file(acceptance_toolchain.REPO_ROOT / "VERSION"))
    result = tuple(sorted(sources, key=lambda item: item.relative_path.encode()))
    if not result or len(result) > 64 or len({item.relative_path for item in result}) != len(result):
        raise AcceptanceLauncherError("loaded acceptance source set is invalid")
    return result


def _new_run_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(6)}"


def _selected_env(
    profile: acceptance_profiles.AcceptanceProfile,
    environ: Mapping[str, str],
) -> tuple[Path, bool]:
    requested = environ.get("COMPOSE_ENV_FILE")
    default = acceptance_toolchain.REPO_ROOT / "docker/.env"
    selection = dict(environ)
    if profile.name == "isolated-health" and requested in {"docker/.env", str(default)} and not default.exists() and not default.is_symlink():
        selection["COMPOSE_ENV_FILE"] = "docker/.env.example"
    selected = acceptance_toolchain.selected_env_path(selection)
    public_example = acceptance_toolchain.REPO_ROOT / "docker/.env.example"
    return selected, profile.name == "isolated-health" and selected == public_example


def _cleanup_reserved(authority: acceptance_receipt.ReservedReceiptAuthority) -> None:
    acceptance_candidate.cleanup_reserved_candidate(
        authority.identity.candidate_reservation,
        authority.reserved_sha256,
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )


def _fixed_docker_runner(command: list[str]) -> str:
    if not command or command[0] != "docker" or any(not item or "\x00" in item for item in command):
        raise AcceptanceLauncherError("stale cleanup Docker invocation is invalid")
    acceptance_toolchain.validate_execution_tool_authority(commands=("docker",))
    tools = acceptance_toolchain.managed_tool_environment()
    environment = {
        "PATH": "/usr/bin",
        "LC_ALL": "C.UTF-8",
        "DOCKER_HOST": tools["DOCKER_HOST"],
        "DOCKER_CONFIG": tools["DOCKER_CONFIG"],
        "DOCKER_CLI_PLUGIN_EXTRA_DIRS": tools["DOCKER_CLI_PLUGIN_EXTRA_DIRS"],
    }
    completed = subprocess.run(
        [tools[acceptance_toolchain.DOCKER_EXECUTABLE_ENV], *command[1:]],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=120,
    )
    acceptance_toolchain.validate_execution_tool_authority(commands=("docker",))
    if completed.returncode != 0:
        raise AcceptanceLauncherError("stale cleanup Docker command failed")
    return completed.stdout


def _cleanup_stale_prepared(identity: acceptance_receipt.PreparedReceiptIdentity) -> None:
    acceptance_environment.cleanup_stale_prepared(identity, _fixed_docker_runner)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _cleanup_prepared_candidate(
    candidate: PreparedCandidateAuthority,
    runtime: acceptance_environment.IsolatedRuntimeAuthority,
) -> None:
    acceptance_environment.cleanup_candidate_runtime_root(runtime)
    acceptance_candidate.cleanup_candidate_snapshot(
        candidate,
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )


def _close_candidate_failure(
    reserved: acceptance_receipt.ReservedReceiptAuthority,
    candidate: PreparedCandidateAuthority | None,
    runtime: acceptance_environment.IsolatedRuntimeAuthority | None,
    primary: BaseException,
) -> None:
    try:
        if candidate is None:
            _cleanup_reserved(reserved)
        else:
            if runtime is None:
                acceptance_candidate.cleanup_candidate_snapshot(
                    candidate,
                    parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
                )
            else:
                _cleanup_prepared_candidate(candidate, runtime)
        acceptance_receipt.transition_reserved_failure(reserved)
    except BaseException as secondary:
        primary.add_note(f"secondary cleanup failure: {type(secondary).__name__}: {secondary}")


def _close_prepared_failure(
    prepared: acceptance_receipt.PreparedReceiptAuthority,
    candidate: PreparedCandidateAuthority,
    runtime: acceptance_environment.IsolatedRuntimeAuthority,
    primary: BaseException,
) -> None:
    try:
        _cleanup_prepared_candidate(candidate, runtime)
        acceptance_receipt.transition_receipt(prepared, status="failed", images=())
    except BaseException as secondary:
        primary.add_note(f"secondary cleanup failure: {type(secondary).__name__}: {secondary}")


def _prepared_transport_env(
    lock: acceptance_lock.AcceptanceLifecycleLock,
    candidate: PreparedCandidateAuthority,
    prepared: acceptance_receipt.PreparedReceiptAuthority,
) -> dict[str, str]:
    return {
        **lock.inheritance_env,
        acceptance_toolchain.SNAPSHOT_ROOT_ENV: str(candidate.snapshot.root),
        _SIGNAL_HANDOFF_ENV: _SIGNAL_HANDOFF_VALUE,
        "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY": candidate.to_json(),
        "AGENT_GOV_PREPARED_RECEIPT_AUTHORITY": prepared.to_json(),
    }


def _reserve_candidate(
    profile: acceptance_profiles.AcceptanceProfile,
    verifier: acceptance_contract.AcceptanceVerifierIdentity,
    selected_env: Path,
    allow_public_env_read: bool,
    lifecycle_lock_sha256: str,
) -> acceptance_receipt.ReservedReceiptAuthority:
    receipt_root = acceptance_receipt.default_receipt_root()
    reservation = acceptance_candidate.reserve_candidate_snapshot(
        acceptance_toolchain.REPO_ROOT,
        selected_env,
        run_id=_new_run_id(),
        profile=profile.name,
        allow_public_env_read=allow_public_env_read,
        parent_requirement=acceptance_toolchain.candidate_snapshot_parent_requirement(),
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )
    return acceptance_receipt.write_reserved_receipt(
        receipt_root=receipt_root,
        reservation=reservation,
        verifier=verifier,
        lifecycle_lock_sha256=lifecycle_lock_sha256,
    )


def _recover_stale(lock: acceptance_lock.AcceptanceLifecycleLock) -> None:
    acceptance_receipt.recover_stale_receipts(
        acceptance_receipt.default_receipt_root(),
        lock=lock,
        cleanup_reserved=_cleanup_reserved,
        cleanup_prepared=_cleanup_stale_prepared,
    )


def _prepare_candidate(
    reserved: acceptance_receipt.ReservedReceiptAuthority,
    profile: acceptance_profiles.AcceptanceProfile,
    environ: Mapping[str, str],
    loaded_sources: tuple[LoadedSourceIdentity, ...],
) -> tuple[
    PreparedCandidateAuthority,
    acceptance_environment.IsolatedRuntimeAuthority,
    acceptance_contract.ManagedAcceptanceEnvironment,
]:
    reservation = reserved.identity.candidate_reservation
    candidate: PreparedCandidateAuthority | None = None
    runtime: acceptance_environment.IsolatedRuntimeAuthority | None = None
    try:
        candidate = acceptance_candidate.prepare_candidate_snapshot(
            reservation,
            reserved_receipt_sha256=reserved.reserved_sha256,
            loaded_sources=loaded_sources,
            dependency_projection=acceptance_toolchain.frontend_dependency_projection_requirement(),
            python_dependencies=acceptance_toolchain.python_dependency_snapshot_requirement(),
            pnpm_dependencies=acceptance_toolchain.pnpm_dependency_snapshot_requirement(),
            node_executable=acceptance_toolchain.node_executable_snapshot_requirement(),
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
            projection_validator=acceptance_toolchain.validate_frontend_dependency_projection,
            python_validator=acceptance_toolchain.validate_python_dependency_snapshot,
            pnpm_validator=acceptance_toolchain.validate_pnpm_dependency_snapshot,
            node_validator=acceptance_toolchain.validate_node_executable_snapshot,
        )
        runtime = acceptance_environment.prepare_isolated_runtime(candidate, profile)
        managed = acceptance_environment.build_acceptance_env(
            profile,
            candidate,
            runtime,
            environ,
            available_port=_available_port,
        )
        return candidate, runtime, managed
    except BaseException as primary:
        with _defer_launcher_signals():
            _close_candidate_failure(reserved, candidate, runtime, primary)
        raise


def launch(arguments: list[str], environ: Mapping[str, str]) -> NoReturn:
    profile, verifier = _request(arguments)
    _bootstrap_authority(environ)
    selected_env, allow_public_env_read = _selected_env(profile, environ)
    loaded_sources = _loaded_sources(environ)
    with acceptance_lock.lifecycle_lock({}) as lock:
        lock.assert_current()
        with _defer_launcher_signals():
            _recover_stale(lock)
        reserved_holder: dict[str, acceptance_receipt.ReservedReceiptAuthority] = {}
        try:
            with _defer_launcher_signals():
                reserved_holder["value"] = _reserve_candidate(
                    profile,
                    verifier,
                    selected_env,
                    allow_public_env_read,
                    acceptance_lock.lifecycle_descriptor_sha256(lock.descriptor),
                )
        except BaseException as primary:
            reserved = reserved_holder.get("value")
            if reserved is not None:
                with _defer_launcher_signals():
                    _close_candidate_failure(reserved, None, None, primary)
            raise
        reserved = reserved_holder["value"]
        candidate, runtime, managed = _prepare_candidate(reserved, profile, environ, loaded_sources)
        prepared_holder: dict[str, acceptance_receipt.PreparedReceiptAuthority] = {}
        try:
            with _defer_launcher_signals():
                prepared_holder["value"] = acceptance_receipt.transition_reserved_to_prepared(
                    reserved,
                    candidate=candidate,
                    managed_environment_sha256=managed[acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV],
                    cleanup_on_failure=lambda _identity: _cleanup_prepared_candidate(candidate, runtime),
                )
        except AcceptanceLauncherInterrupted as primary:
            prepared = prepared_holder.get("value")
            if prepared is not None:
                with _defer_launcher_signals():
                    _close_prepared_failure(prepared, candidate, runtime, primary)
            raise
        prepared = prepared_holder["value"]
        try:
            acceptance_candidate.verify_candidate_snapshot(
                candidate,
                parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
                projection_validator=acceptance_toolchain.validate_frontend_dependency_projection,
            )
            acceptance_toolchain.validate_toolchain_authority(managed)
            prepared.verify_current()
            reexec = acceptance_contract.build_prepared_reexec_environment(
                managed,
                transport_values=_prepared_transport_env(lock, candidate, prepared),
            )
            with _defer_launcher_signals():
                acceptance_toolchain.exec_authoritative_python(
                    candidate.snapshot_repository_root / "scripts/container_acceptance_bootstrap.py",
                    (
                        "resume",
                        str(candidate.snapshot_runner_path),
                        "--env-file",
                        str(candidate.env_file),
                        "--profile",
                        profile.name,
                        "--",
                        *verifier.invocation_argv,
                    ),
                    reexec,
                )
        except BaseException as primary:
            with _defer_launcher_signals():
                _close_prepared_failure(prepared, candidate, runtime, primary)
            raise


def launch_from_toolchain(arguments: list[str], environ: Mapping[str, str]) -> NoReturn:
    if (
        not arguments
        or arguments[0] != "--profile"
        or "--" not in arguments
        or _SHA256.fullmatch(environ.get(acceptance_toolchain.BOOTSTRAP_STAGE_ENV, "")) is None
        or not sys.flags.isolated
        or not sys.flags.safe_path
        or not sys.flags.no_site
    ):
        raise AcceptanceLauncherError("container acceptance bootstrap stage is invalid")
    with controlled_launcher_signals():
        acceptance_toolchain.active_toolchain_authority()
        launch(arguments, environ)
