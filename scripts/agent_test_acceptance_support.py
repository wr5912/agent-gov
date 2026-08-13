"""Agent test 容器验收的 Docker authority 与 named-volume canary 辅助。"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, ParamSpec, TypedDict, TypeVar, cast

from scripts import container_acceptance_candidate as acceptance_candidate
from scripts.container_acceptance_candidate_authority import (
    PREPARED_CANDIDATE_AUTHORITY_ENV,
    AcceptanceCandidateIdentity,
    CandidateDependencySnapshotIdentity,
    CandidateExecutableSnapshotIdentity,
    CandidatePathIdentity,
    CandidateRecoveryAuthority,
    CandidateSnapshotIdentity,
    CandidateSnapshotReservation,
    CandidateSourceIdentity,
    LoadedSourceIdentity,
    PreparedCandidateAuthority,
)
from scripts.container_acceptance_candidate_git import CandidateGitAuthority, FixedCandidateGitAuthority
from scripts.container_acceptance_image_authority import (
    ACCEPTANCE_CANDIDATE_TREE_LABEL as ACCEPTANCE_CANDIDATE_TREE_LABEL,
)
from scripts.container_acceptance_image_authority import ACCEPTANCE_ENV_DIGEST_LABEL as ACCEPTANCE_ENV_DIGEST_LABEL
from scripts.container_acceptance_image_authority import ACCEPTANCE_IMAGE_LABEL as ACCEPTANCE_IMAGE_LABEL
from scripts.container_acceptance_image_authority import AcceptanceSupportError as AcceptanceSupportError
from scripts.container_acceptance_image_authority import DockerRunner as DockerRunner
from scripts.container_acceptance_image_authority import ImageAuthorityKind as ImageAuthorityKind
from scripts.container_acceptance_image_authority import LocalImageEvidence as LocalImageEvidence
from scripts.container_acceptance_image_authority import capture_external_images as capture_external_images
from scripts.container_acceptance_image_authority import capture_local_images as capture_local_images
from scripts.container_acceptance_image_authority import verify_running_container as verify_running_container

__all__ = (
    "AcceptanceCandidateIdentity",
    "CandidateDependencySnapshotIdentity",
    "CandidateExecutableSnapshotIdentity",
    "CandidateGitAuthority",
    "CandidatePathIdentity",
    "CandidateRecoveryAuthority",
    "CandidateSnapshotIdentity",
    "CandidateSnapshotReservation",
    "CandidateSourceIdentity",
    "FixedCandidateGitAuthority",
    "LoadedSourceIdentity",
    "PREPARED_CANDIDATE_AUTHORITY_ENV",
    "PreparedCandidateAuthority",
)

ACCEPTANCE_CANDIDATE_TREE_ENV: Final = "AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE"
ACCEPTANCE_ENV_DIGEST_ENV: Final = "AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256"
COMPOSE_NETWORK_LABEL: Final = "com.docker.compose.network"
COMPOSE_PROJECT_LABEL: Final = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL: Final = "com.docker.compose.service"
COMPOSE_VOLUME_LABEL: Final = "com.docker.compose.volume"
VOLUME_ROOT_CANARY: Final = ".agentgov-volume-root-canary"
VOLUME_SIBLING_RUN: Final = "agentgov-volume-sibling-canary"
VOLUME_SIBLING_CANARY: Final = ".agentgov-sibling-workspace-canary"
RUNTIME_BOOTSTRAP_TARGET: Final = "/app/docker/runtime-bootstrap"
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_ISOLATED_PROFILES: Final = frozenset({"agent-test", "isolated-health"})

CleanupNotice = Callable[[], None]
_P = ParamSpec("_P")
_R = TypeVar("_R")


class _DockerObject(TypedDict, total=False):
    Id: object
    Name: object
    State: object
    Config: object
    Mounts: object
    Driver: object
    Scope: object
    Options: object
    Mountpoint: object
    Labels: object
    Containers: object


class _DockerMount(TypedDict, total=False):
    Type: object
    Name: object
    Source: object
    Destination: object
    Driver: object
    RW: object
    Propagation: object


@dataclass(frozen=True, slots=True)
class WorkerRuntimeAuthority:
    container_id: str
    runs_volume_name: str
    runs_volume_mountpoint: Path


def capture_failure(action: Callable[[], None]) -> BaseException | None:
    try:
        action()
    except BaseException as exc:
        return exc
    return None


def prefer_primary_failure(
    primary: BaseException | None,
    secondary: BaseException | None,
    *,
    on_secondary: CleanupNotice | None = None,
) -> BaseException | None:
    if secondary is None:
        return primary
    if primary is not None and on_secondary is not None:
        on_secondary()
    return primary or secondary


@dataclass(frozen=True, slots=True)
class _CleanupResourceNames:
    volume: str | None
    network: str


@dataclass(frozen=True, slots=True)
class _NetworkEvidence:
    network_id: str
    container_ids: tuple[str, ...]


def _candidate_call(action: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs) -> _R:
    try:
        return action(*args, **kwargs)
    except acceptance_candidate.CandidateSnapshotError as exc:
        raise AcceptanceSupportError(str(exc)) from exc


def reserve_candidate_snapshot(
    repository: Path,
    selected_env: Path,
    *,
    run_id: str,
    profile: str,
    allow_public_env_read: bool = False,
) -> CandidateSnapshotReservation:
    return _candidate_call(
        acceptance_candidate.reserve_candidate_snapshot,
        repository,
        selected_env,
        run_id=run_id,
        profile=profile,
        allow_public_env_read=allow_public_env_read,
    )


def prepare_candidate_snapshot(
    reservation: CandidateSnapshotReservation,
    *,
    reserved_receipt_sha256: str,
    loaded_sources: tuple[LoadedSourceIdentity, ...],
    git_authority: CandidateGitAuthority | None = None,
) -> PreparedCandidateAuthority:
    return _candidate_call(
        acceptance_candidate.prepare_candidate_snapshot,
        reservation,
        reserved_receipt_sha256=reserved_receipt_sha256,
        loaded_sources=loaded_sources,
        git_authority=git_authority,
    )


def verify_candidate_snapshot(authority: PreparedCandidateAuthority) -> None:
    _candidate_call(acceptance_candidate.verify_candidate_snapshot, authority)


def require_candidate_source_current(
    authority: PreparedCandidateAuthority,
    *,
    git_authority: CandidateGitAuthority | None = None,
) -> None:
    _candidate_call(acceptance_candidate.require_candidate_source_current, authority, git_authority=git_authority)


def cleanup_reserved_candidate(reservation: CandidateSnapshotReservation, reserved_receipt_sha256: str) -> None:
    _candidate_call(acceptance_candidate.cleanup_reserved_candidate, reservation, reserved_receipt_sha256)


def require_snapshot_loaded_file(
    authority: PreparedCandidateAuthority,
    loaded_file: Path,
    relative_path: Path,
) -> None:
    _candidate_call(
        acceptance_candidate.require_snapshot_loaded_file,
        authority,
        loaded_file,
        PurePosixPath(relative_path.as_posix()),
    )


def require_snapshot_loaded_sources(
    authority: PreparedCandidateAuthority,
    loaded_files: tuple[Path, ...],
    *,
    required_relative_paths: tuple[Path, ...] = (),
) -> None:
    required = tuple(PurePosixPath(path.as_posix()) for path in required_relative_paths)
    _candidate_call(
        acceptance_candidate.require_snapshot_loaded_sources,
        authority,
        loaded_files,
        required_relative_paths=required,
    )


def cleanup_candidate_snapshot(authority: PreparedCandidateAuthority) -> None:
    _candidate_call(acceptance_candidate.cleanup_candidate_snapshot, authority)


def recover_and_cleanup_candidate_snapshot(authority: CandidateRecoveryAuthority) -> None:
    _candidate_call(acceptance_candidate.recover_and_cleanup_candidate_snapshot, authority)


def staged_tree_sha(repository: Path, *, git_authority: CandidateGitAuthority | None = None) -> str:
    return _candidate_call(acceptance_candidate.staged_tree_sha, repository, git_authority=git_authority)


def revision_tree_sha(
    repository: Path,
    revision: str = "HEAD",
    *,
    git_authority: CandidateGitAuthority | None = None,
) -> str:
    return _candidate_call(acceptance_candidate.revision_tree_sha, repository, revision, git_authority=git_authority)


def available_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def sandbox_scope_id(volume_name: str) -> str:
    encoded = json.dumps(volume_name, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prove_runtime_bootstrap_not_mounted(
    *,
    api_container: str,
    acceptance_run_id: str,
    docker_runner: DockerRunner,
) -> None:
    inspect = _json_array_object(docker_runner(["docker", "inspect", api_container]), operation="API inspect")
    _require_current_container(
        inspect,
        container_name=api_container,
        acceptance_run_id=acceptance_run_id,
        operation="API inspect",
    )
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
        raise AcceptanceSupportError("API mount evidence is invalid")
    if any(mount.get("Destination") == RUNTIME_BOOTSTRAP_TARGET for mount in mounts):
        raise AcceptanceSupportError("API container unexpectedly mounts the runtime bootstrap target")


def json_value(raw: str, *, operation: str) -> object:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AcceptanceSupportError(f"{operation} returned invalid JSON") from exc


def _json_array_object(raw: str, *, operation: str) -> _DockerObject:
    payload = json_value(raw, operation=operation)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise AcceptanceSupportError(f"{operation} returned an ambiguous identity")
    return cast(_DockerObject, payload[0])


def _absolute_mount_path(value: object, *, operation: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        raise AcceptanceSupportError(f"{operation} returned an invalid mount path")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise AcceptanceSupportError(f"{operation} returned an invalid mount path")
    return path


def prove_worker_runtime_authority(
    *,
    worker_container: str,
    acceptance_run_id: str,
    runtime_data_dir: Path,
    docker_runner: DockerRunner,
) -> WorkerRuntimeAuthority:
    inspect = _json_array_object(docker_runner(["docker", "inspect", worker_container]), operation="worker inspect")
    container_id = _require_current_container(
        inspect,
        container_name=worker_container,
        acceptance_run_id=acceptance_run_id,
        operation="worker inspect",
    )
    _data_mount, runs_mount = _worker_data_mounts(inspect, runtime_data_dir=runtime_data_dir)
    volume_name = runs_mount.get("Name")
    if not isinstance(volume_name, str) or _VOLUME_NAME.fullmatch(volume_name) is None:
        raise AcceptanceSupportError("worker inspect returned an invalid runs volume identity")
    volume = _json_array_object(docker_runner(["docker", "volume", "inspect", volume_name]), operation="runs volume inspect")
    mountpoint = _validated_volume_mountpoint(volume, expected_name=volume_name)
    if runs_mount.get("Source") != str(mountpoint):
        raise AcceptanceSupportError("worker and volume inspect disagree on the runs volume Mountpoint")
    return WorkerRuntimeAuthority(container_id=container_id, runs_volume_name=volume_name, runs_volume_mountpoint=mountpoint)


def _require_current_container(
    inspect: _DockerObject,
    *,
    container_name: str,
    acceptance_run_id: str,
    operation: str,
) -> str:
    container_id = inspect.get("Id")
    state = inspect.get("State")
    config = inspect.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(container_id, str)
        or _CONTAINER_ID.fullmatch(container_id) is None
        or inspect.get("Name") != f"/{container_name}"
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(labels, dict)
        or labels.get(ACCEPTANCE_IMAGE_LABEL) != acceptance_run_id
    ):
        raise AcceptanceSupportError(f"{operation} does not prove the current running acceptance container")
    return container_id


def _worker_data_mounts(inspect: _DockerObject, *, runtime_data_dir: Path) -> tuple[_DockerMount, _DockerMount]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
        raise AcceptanceSupportError("worker inspect mounts are invalid")
    typed_mounts = [cast(_DockerMount, mount) for mount in mounts if isinstance(mount, dict)]
    data = [mount for mount in typed_mounts if mount.get("Destination") == "/data"]
    runs = [mount for mount in typed_mounts if mount.get("Destination") == "/agent-test-runs"]
    named_volumes = [mount for mount in typed_mounts if mount.get("Type") == "volume"]
    if len(data) != 1 or len(runs) != 1 or named_volumes != runs:
        raise AcceptanceSupportError("worker inspect does not prove one isolated runs named volume")
    data_mount, runs_mount = data[0], runs[0]
    if (
        data_mount.get("Type") != "bind"
        or data_mount.get("Source") != str(runtime_data_dir)
        or data_mount.get("RW") is not True
        or data_mount.get("Propagation") != "rprivate"
        or runs_mount.get("Driver") != "local"
        or runs_mount.get("RW") is not True
    ):
        raise AcceptanceSupportError("worker inspect mounts do not match the isolated acceptance runtime")
    return data_mount, runs_mount


def _validated_volume_mountpoint(volume: _DockerObject, *, expected_name: str) -> Path:
    if volume.get("Name") != expected_name or volume.get("Driver") != "local" or volume.get("Scope") != "local" or volume.get("Options") not in (None, {}):
        raise AcceptanceSupportError("runs volume inspect does not prove an option-free local named volume")
    return _absolute_mount_path(volume.get("Mountpoint"), operation="runs volume inspect")


def _cleanup_resource_names(
    raw: str,
    *,
    project_name: str,
    expect_runs_volume: bool,
) -> _CleanupResourceNames:
    try:
        config = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AcceptanceSupportError("cleanup Compose config returned invalid JSON") from exc
    volumes = config.get("volumes") if isinstance(config, dict) else None
    networks = config.get("networks") if isinstance(config, dict) else None
    volume = volumes.get("agent-test-runs") if isinstance(volumes, dict) else None
    network = networks.get("default") if isinstance(networks, dict) else None
    volume_name = volume.get("name") if isinstance(volume, dict) else None
    network_name = network.get("name") if isinstance(network, dict) else None
    volume_valid = (
        isinstance(volume_name, str) and _VOLUME_NAME.fullmatch(volume_name) is not None and volume.get("driver") == "local"
        if expect_runs_volume
        else volumes in (None, {})
    )
    valid = (
        isinstance(config, dict)
        and config.get("name") == project_name
        and volume_valid
        and isinstance(network_name, str)
        and _VOLUME_NAME.fullmatch(network_name) is not None
        and network.get("driver") in (None, "bridge")
        and network.get("external") is not True
    )
    if not valid:
        raise AcceptanceSupportError("cleanup Compose resources do not match the isolated project")
    return _CleanupResourceNames(volume=volume_name if expect_runs_volume else None, network=network_name)


def _listed_resource_names(kind: str, docker_runner: DockerRunner) -> tuple[str, ...]:
    output = docker_runner(["docker", kind, "ls", "--format", "{{.Name}}"])
    names = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(names) > 256 or len(names) != len(set(names)) or any(_VOLUME_NAME.fullmatch(name) is None for name in names):
        raise AcceptanceSupportError(f"Docker {kind} list returned invalid identities")
    return names


def _prove_cleanup_network(
    name: str,
    *,
    project_name: str,
    docker_runner: DockerRunner,
) -> _NetworkEvidence:
    network = _json_array_object(docker_runner(["docker", "network", "inspect", name]), operation="cleanup network inspect")
    labels = network.get("Labels")
    containers = network.get("Containers")
    network_id = network.get("Id")
    container_ids = tuple(sorted(containers)) if isinstance(containers, dict) else ()
    if (
        network.get("Name") != name
        or not isinstance(network_id, str)
        or _CONTAINER_ID.fullmatch(network_id) is None
        or network.get("Driver") != "bridge"
        or network.get("Scope") != "local"
        or not isinstance(labels, dict)
        or labels.get(COMPOSE_PROJECT_LABEL) != project_name
        or labels.get(COMPOSE_NETWORK_LABEL) != "default"
        or not isinstance(containers, dict)
        or any(_CONTAINER_ID.fullmatch(container_id) is None for container_id in container_ids)
    ):
        raise AcceptanceSupportError("cleanup network inspect does not prove the isolated project network")
    return _NetworkEvidence(network_id=network_id, container_ids=container_ids)


def _prove_cleanup_container(
    container_id: str,
    *,
    project_name: str,
    acceptance_run_id: str,
    expected_services: tuple[str, ...],
    docker_runner: DockerRunner,
) -> None:
    container = _json_array_object(docker_runner(["docker", "inspect", container_id]), operation="cleanup container inspect")
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        container.get("Id") != container_id
        or not isinstance(labels, dict)
        or labels.get(COMPOSE_PROJECT_LABEL) != project_name
        or labels.get(ACCEPTANCE_IMAGE_LABEL) != acceptance_run_id
        or labels.get(COMPOSE_SERVICE_LABEL) not in expected_services
    ):
        raise AcceptanceSupportError("cleanup container inspect does not prove a current project container")


def _fallback_volume(name: str, *, project_name: str, docker_runner: DockerRunner) -> None:
    if name not in _listed_resource_names("volume", docker_runner):
        return
    volume = _json_array_object(docker_runner(["docker", "volume", "inspect", name]), operation="cleanup volume inspect")
    labels = volume.get("Labels")
    _validated_volume_mountpoint(volume, expected_name=name)
    if not isinstance(labels, dict) or labels.get(COMPOSE_PROJECT_LABEL) != project_name or labels.get(COMPOSE_VOLUME_LABEL) != "agent-test-runs":
        raise AcceptanceSupportError("cleanup volume inspect does not prove the isolated project volume")
    users = docker_runner(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"volume={name}"])
    if users.strip():
        raise AcceptanceSupportError("cleanup volume still has attached containers")
    docker_runner(["docker", "volume", "rm", name])


def _fallback_network(
    name: str,
    *,
    project_name: str,
    acceptance_run_id: str,
    expected_services: tuple[str, ...],
    docker_runner: DockerRunner,
) -> None:
    if name not in _listed_resource_names("network", docker_runner):
        return
    evidence = _prove_cleanup_network(name, project_name=project_name, docker_runner=docker_runner)
    for container_id in evidence.container_ids:
        _prove_cleanup_container(
            container_id,
            project_name=project_name,
            acceptance_run_id=acceptance_run_id,
            expected_services=expected_services,
            docker_runner=docker_runner,
        )
    for container_id in evidence.container_ids:
        docker_runner(["docker", "rm", "--force", container_id])
    current = _prove_cleanup_network(name, project_name=project_name, docker_runner=docker_runner)
    if current.network_id != evidence.network_id or current.container_ids:
        raise AcceptanceSupportError("cleanup network identity changed or still has attached containers")
    docker_runner(["docker", "network", "rm", evidence.network_id])


def cleanup_isolated_compose_residue(
    *,
    compose_config: str,
    project_name: str,
    acceptance_run_id: str,
    expected_services: tuple[str, ...],
    expect_runs_volume: bool,
    docker_runner: DockerRunner,
) -> None:
    names = _cleanup_resource_names(
        compose_config,
        project_name=project_name,
        expect_runs_volume=expect_runs_volume,
    )
    errors: list[Exception] = []
    actions: list[Callable[[], None]] = [
        lambda: _fallback_network(
            names.network,
            project_name=project_name,
            acceptance_run_id=acceptance_run_id,
            expected_services=expected_services,
            docker_runner=docker_runner,
        ),
    ]
    if names.volume is not None:
        volume_name = names.volume
        actions.append(lambda: _fallback_volume(volume_name, project_name=project_name, docker_runner=docker_runner))
    for action in actions:
        try:
            action()
        except Exception as exc:
            errors.append(exc)
    try:
        network_remains = names.network in _listed_resource_names("network", docker_runner)
        volume_remains = names.volume is not None and names.volume in _listed_resource_names("volume", docker_runner)
        if network_remains or volume_remains:
            raise AcceptanceSupportError("isolated Compose network or volume remains after fallback cleanup")
    except Exception as exc:
        errors.append(exc)
    if errors:
        raise AcceptanceSupportError("isolated Compose residue fallback failed") from errors[0]


def cleanup_stale_acceptance_scope(
    *,
    profile: str,
    run_id: str,
    project_name: str,
    expected_services: tuple[str, ...],
    docker_runner: DockerRunner,
) -> None:
    token = "".join(character for character in run_id.lower() if character.isalnum())[-20:]
    if profile not in _ISOLATED_PROFILES or len(token) < 12 or project_name != f"agentgov-acceptance-{token}":
        raise AcceptanceSupportError("stale acceptance scope is not an isolated run authority")
    containers = _labeled_resources("container", project_name, docker_runner)
    for container_id in containers:
        _prove_cleanup_container(
            container_id,
            project_name=project_name,
            acceptance_run_id=run_id,
            expected_services=expected_services,
            docker_runner=docker_runner,
        )
    for container_id in containers:
        docker_runner(["docker", "rm", "--force", container_id])
    for name in _labeled_resources("network", project_name, docker_runner):
        evidence = _prove_cleanup_network(name, project_name=project_name, docker_runner=docker_runner)
        if evidence.container_ids:
            raise AcceptanceSupportError("stale acceptance network still has unproven containers")
        docker_runner(["docker", "network", "rm", evidence.network_id])
    for name in _labeled_resources("volume", project_name, docker_runner):
        _fallback_volume(name, project_name=project_name, docker_runner=docker_runner)
    if any(_labeled_resources(kind, project_name, docker_runner) for kind in ("container", "network", "volume")):
        raise AcceptanceSupportError("stale isolated acceptance scope remains after exact cleanup")


def _labeled_resources(kind: str, project_name: str, docker_runner: DockerRunner) -> tuple[str, ...]:
    if kind == "container":
        command = ["docker", "ps", "--all", "--quiet", "--no-trunc"]
        pattern = _CONTAINER_ID
    elif kind in {"network", "volume"}:
        command = ["docker", kind, "ls", "--format", "{{.Name}}"]
        pattern = _VOLUME_NAME
    else:
        raise AcceptanceSupportError("stale acceptance resource kind is invalid")
    output = docker_runner([*command, "--filter", f"label={COMPOSE_PROJECT_LABEL}={project_name}"])
    identities = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(identities) > 64 or len(set(identities)) != len(identities) or any(pattern.fullmatch(item) is None for item in identities):
        raise AcceptanceSupportError("stale acceptance resource enumeration is invalid")
    return identities


def hostile_test_source(*, host_marker: Path, private_asset_paths: tuple[str, ...]) -> str:
    marker_literal = repr(str(host_marker))
    private_paths = repr(tuple(f"/workspace/{path}" for path in private_asset_paths))
    return f"""from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path


def _require_write_denied(path: Path) -> None:
    try:
        path.write_text("probe", encoding="utf-8")
    except OSError:
        return
    raise AssertionError("write unexpectedly succeeded")


def test_secret_environment_is_not_inherited() -> None:
    forbidden = {{"API_KEY", "MODEL_PROVIDER_API_KEY", "CLAUDE_ENV_JSON", "AGENTGOV_API_KEY"}}
    assert forbidden.isdisjoint(os.environ)
    process_environment = Path("/proc/1/environ").read_bytes()
    assert all((name + "=").encode() not in process_environment for name in forbidden)
    assert all(not Path(path).exists() for path in {private_paths})


def test_live_runtime_and_host_paths_are_not_mounted() -> None:
    assert not Path("/data").exists()
    assert not Path("/governor-workspace").exists()
    assert not Path("/claude-roots").exists()
    assert not Path({marker_literal}).exists()


def test_docker_socket_and_host_tools_are_absent() -> None:
    assert not Path("/var/run/docker.sock").exists()
    assert all(shutil.which(name) is None for name in ("docker", "git", "curl", "ssh"))


def test_network_has_no_default_route() -> None:
    routes = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]
    assert not any(len(fields := line.split()) > 1 and fields[1] == "00000000" for line in routes)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.1)
        assert probe.connect_ex(("198.51.100.1", 9)) != 0
    finally:
        probe.close()


def test_process_is_non_root_and_source_and_rootfs_are_read_only() -> None:
    assert os.getuid() != 0
    _require_write_denied(Path("/workspace/.agentgov-write-probe"))
    _require_write_denied(Path("/agentgov-rootfs-probe"))


def test_named_volume_subpath_hides_root_and_sibling_runs() -> None:
    assert not Path("/workspace/{VOLUME_ROOT_CANARY}").exists()
    assert not (Path("/workspace/{VOLUME_SIBLING_RUN}") / "workspace" / "{VOLUME_SIBLING_CANARY}").exists()
"""


def create_volume_canaries(*, worker_container: str, docker_runner: DockerRunner) -> None:
    program = (
        "import os,sys; from pathlib import Path; b=Path('/agent-test-runs'); r=b/sys.argv[1]; "
        f"s=b/sys.argv[2]/'workspace'/'{VOLUME_SIBLING_CANARY}'; assert not r.exists() and not s.parent.parent.exists(); "
        "s.parent.mkdir(parents=True); "
        "[(lambda f:(os.write(f,b'canary'),os.close(f)))(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)) for p in (r,s)]; "
        "print('ready')"
    )
    state = docker_runner(["docker", "exec", worker_container, "/usr/local/bin/python", "-c", program, VOLUME_ROOT_CANARY, VOLUME_SIBLING_RUN])
    if state != "ready":
        raise AcceptanceSupportError("named-volume isolation canaries could not be created")


def remove_volume_canaries(*, worker_container: str, docker_runner: DockerRunner) -> None:
    program = (
        "import shutil,sys; from pathlib import Path; b=Path('/agent-test-runs'); "
        "r=b/sys.argv[1]; s=b/sys.argv[2]; r.unlink(missing_ok=True); shutil.rmtree(s); "
        "print('removed' if not r.exists() and not s.exists() else 'residue')"
    )
    state = docker_runner(["docker", "exec", worker_container, "/usr/local/bin/python", "-c", program, VOLUME_ROOT_CANARY, VOLUME_SIBLING_RUN])
    if state != "removed":
        raise AcceptanceSupportError("named-volume isolation canaries remain")
