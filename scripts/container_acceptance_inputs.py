"""隔离容器验收的输入快照与完整性指纹。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeAlias, TypedDict, cast
from urllib.parse import urlparse

from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.container_acceptance_identity import PinnedFileIdentity, write_exclusive_file
from scripts.container_acceptance_toolchain import (
    FORMAL_SOURCE_ROOT_ENV,
    SYSTEM_TOOL_PATHS,
    TOOL_PATH_ENV_KEYS,
    AcceptanceToolchain,
    toolchain_from_environment,
    verify_acceptance_toolchain,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCENARIO_FILE_ENV_KEYS: Final = ("REAL_SCENARIO_FILE", "TECHNICAL_SCENARIO_FILE")
ACCEPTANCE_CONTEXT_ENV: Final = "AGENT_GOV_ACCEPTANCE_CONTEXT_FILE"
ACCEPTANCE_ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
ACCEPTANCE_RUN_ID_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUN_ID"
ACCEPTANCE_PROFILE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE"
ACCEPTANCE_TARGET_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_TARGET"
ACCEPTANCE_COMMAND_SHA256_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_COMMAND_SHA256"
LIVE_SOURCE_ROOT_ENV: Final = "AGENTGOV_ACCEPTANCE_LIVE_SOURCE_ROOT"
DEPLOYABLE_SOURCE_ROOT_ENV: Final = "AGENTGOV_ACCEPTANCE_DEPLOYABLE_SOURCE_ROOT"
ACCEPTANCE_LABEL_KEY: Final = "io.agentgov.acceptance-run-id"
CORE_ACCEPTANCE_SERVICES: Final = frozenset({"agent-gov-api", "agent-gov-ui", "agentscope-runtime"})
LANGFUSE_ACCEPTANCE_SERVICES: Final = frozenset(
    {
        *CORE_ACCEPTANCE_SERVICES,
        "langfuse-postgres",
        "langfuse-clickhouse",
        "langfuse-redis",
        "langfuse-minio",
        "langfuse-web",
        "langfuse-worker",
    }
)
SCENARIO_SNAPSHOT_NAMES: Final = {
    "REAL_SCENARIO_FILE": "real-scenarios.json",
    "TECHNICAL_SCENARIO_FILE": "technical-scenarios.json",
}
MAX_SCENARIO_FILE_BYTES: Final = 16 * 1024 * 1024
READ_CHUNK_BYTES: Final = 1024 * 1024
TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
PLACEHOLDER_MARKERS: Final = ("replace-with", "change-me", "example", "dummy", "test-only")


class AcceptanceError(RuntimeError):
    """隔离容器验收输入不满足安全或一致性约束。"""


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


@dataclass(frozen=True)
class ScenarioFileSnapshot:
    environment_key: str
    path: Path
    size_bytes: int
    sha256: str


class ScenarioSnapshotEnvironment(TypedDict, total=False):
    REAL_SCENARIO_FILE: str
    TECHNICAL_SCENARIO_FILE: str


class ContainerIdentity(TypedDict):
    service: str
    container_id: str
    image_id: str


VerifiedContainerIdentities: TypeAlias = dict[str, tuple[str, str]]


class SnapshotIdentity(TypedDict):
    environment_key: str
    size_bytes: int
    sha256: str


class AcceptanceContextEnvironment(TypedDict):
    AGENT_GOV_ACCEPTANCE_RUN_ID: str
    AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE: str
    AGENT_GOV_CONTAINER_ACCEPTANCE_TARGET: str
    AGENT_GOV_CONTAINER_ACCEPTANCE_COMMAND_SHA256: str
    COMPOSE_PROJECT_NAME: str
    COMPOSE_ENV_FILE: str
    API_BASE: str
    FRONTEND_URL: str
    AGENTGOV_ACCEPTANCE_LIVE_SOURCE_ROOT: str
    AGENTGOV_ACCEPTANCE_DEPLOYABLE_SOURCE_ROOT: str
    AGENTGOV_ACCEPTANCE_FORMAL_SOURCE_ROOT: str


class AcceptanceContext(TypedDict):
    schema_version: int
    source_env: str
    source_fingerprint_sha256: str
    frozen_source_sha256: str
    source_env_sha256: str
    effective_env_sha256: str
    environment: AcceptanceContextEnvironment
    scenario_snapshots: list[SnapshotIdentity]
    containers: list[ContainerIdentity]
    toolchain: AcceptanceToolchain


def read_compose_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise AcceptanceError("所选隔离 Compose env 文件不存在")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_live_authorization(env: dict[str, str], *, require_trace_complete: bool = False) -> None:
    if os.environ.get("REQUIRE_LIVE_RUNTIME", "").strip().lower() not in TRUTHY:
        raise AcceptanceError("必须显式设置 REQUIRE_LIVE_RUNTIME=1 才能调用真实模型")
    if os.environ.get(ACCEPTANCE_ACTIVE_ENV) != "1":
        raise AcceptanceError("必须通过公共隔离容器验收 Make 入口执行")
    if not os.environ.get("AGENT_GOV_ACCEPTANCE_RUN_ID", "").strip():
        raise AcceptanceError("缺少隔离容器验收 run id")
    profile = os.environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE")
    if profile not in {"core", "langfuse"}:
        raise AcceptanceError("AgentScope live 验收必须使用 core 或 langfuse 隔离 profile")
    if require_trace_complete and profile != "langfuse":
        raise AcceptanceError("完整 Trace 验收必须使用 langfuse 隔离 profile")
    for key in ("API_KEY", "MODEL_PROVIDER_API_KEY", "AGENTSCOPE_MODEL_NAME"):
        value = env.get(key, "").strip()
        if not value or any(marker in value.lower() for marker in PLACEHOLDER_MARKERS):
            raise AcceptanceError(f"真实验收要求所选 env 提供非占位 {key}")
    if env.get("AGENTGOV_API_MODE") == "acceptance":
        if not env.get("AGENTGOV_ACCEPTANCE_IDENTITY", "").strip():
            raise AcceptanceError("cutover acceptance 模式缺少一次性 identity")
        if env.get("AGENTGOV_ACCEPTANCE_API_KEY") != env.get("API_KEY"):
            raise AcceptanceError("cutover acceptance Bearer key 与 API_KEY 未绑定")
    verify_acceptance_context(dict(os.environ), require_trace_complete=require_trace_complete)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _absolute_path_without_links(raw_path: str, *, base: Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _open_pinned_regular_file(path: Path, *, label: str) -> tuple[int, int]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if no_follow is None or directory is None or close_on_exec is None:
        raise AcceptanceError("当前平台不支持验收输入的无符号链接读取")
    parts = path.parts
    if not path.is_absolute() or len(parts) < 2:
        raise AcceptanceError(f"{label} 必须指向普通文件")
    directory_fd = os.open(parts[0], os.O_RDONLY | directory | close_on_exec | no_follow)
    try:
        for component in parts[1:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory | close_on_exec | no_follow,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | close_on_exec | no_follow, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise AcceptanceError(f"{label} 必须是无符号链接且可稳定读取的普通文件") from exc
    metadata = os.fstat(file_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(file_fd)
        os.close(directory_fd)
        raise AcceptanceError(f"{label} 必须指向普通文件")
    return directory_fd, file_fd


def _read_stable_regular_file(path: Path, *, label: str) -> tuple[bytes, os.stat_result]:
    directory_fd, file_fd = _open_pinned_regular_file(path, label=label)
    try:
        before = os.fstat(file_fd)
        if before.st_size > MAX_SCENARIO_FILE_BYTES:
            raise AcceptanceError(f"{label} 超过 {MAX_SCENARIO_FILE_BYTES} 字节上限")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(file_fd, READ_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_SCENARIO_FILE_BYTES:
                raise AcceptanceError(f"{label} 超过 {MAX_SCENARIO_FILE_BYTES} 字节上限")
            chunks.append(chunk)
        after = os.fstat(file_fd)
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _metadata_identity(before) != _metadata_identity(after) or _metadata_identity(after) != _metadata_identity(current):
            raise AcceptanceError(f"{label} 在读取期间发生变化")
        return b"".join(chunks), after
    except OSError as exc:
        raise AcceptanceError(f"{label} 无法稳定读取") from exc
    finally:
        os.close(file_fd)
        os.close(directory_fd)


def _validate_private_directory(path: Path, *, label: str, mode: int = 0o700) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AcceptanceError(f"{label} 不可用") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != mode:
        raise AcceptanceError(f"{label} 必须是当前用户持有的 {mode:04o} 真实目录")


def _write_private_snapshot(path: Path, payload: bytes) -> PinnedFileIdentity:
    return write_exclusive_file(path, payload, error_type=AcceptanceError, label="私有验收快照")


def snapshot_scenario_files(
    temp_root: Path,
    environ: dict[str, str],
    *,
    source_base: Path = REPO_ROOT,
) -> tuple[ScenarioFileSnapshot, ...]:
    """在锁内复制场景文件；之后子进程只接收这些固定快照。"""

    _validate_private_directory(temp_root, label="验收临时根")
    selected = tuple((key, environ.get(key, "").strip()) for key in SCENARIO_FILE_ENV_KEYS)
    selected = tuple((key, raw_path) for key, raw_path in selected if raw_path)
    if not selected:
        return ()
    snapshot_root = temp_root / "acceptance-inputs"
    try:
        snapshot_root.mkdir(mode=0o700)
    except OSError as exc:
        raise AcceptanceError("无法创建私有验收输入目录") from exc
    _validate_private_directory(snapshot_root, label="验收输入目录")

    snapshots: list[ScenarioFileSnapshot] = []
    for environment_key, raw_path in selected:
        source = _absolute_path_without_links(raw_path, base=source_base)
        payload, _ = _read_stable_regular_file(source, label=environment_key)
        target = snapshot_root / SCENARIO_SNAPSHOT_NAMES[environment_key]
        _write_private_snapshot(target, payload)
        copied, metadata = _read_stable_regular_file(target, label=f"{environment_key} 快照")
        if copied != payload or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AcceptanceError(f"{environment_key} 快照权限或内容校验失败")
        snapshots.append(
            ScenarioFileSnapshot(
                environment_key=environment_key,
                path=target,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(snapshots)


def snapshot_environment(snapshots: tuple[ScenarioFileSnapshot, ...]) -> ScenarioSnapshotEnvironment:
    if len({item.environment_key for item in snapshots}) != len(snapshots):
        raise AcceptanceError("验收场景快照环境键重复")
    if any(item.environment_key not in SCENARIO_FILE_ENV_KEYS for item in snapshots):
        raise AcceptanceError("验收场景快照包含未知环境键")
    result = ScenarioSnapshotEnvironment()
    for snapshot in snapshots:
        if snapshot.environment_key == "REAL_SCENARIO_FILE":
            result["REAL_SCENARIO_FILE"] = str(snapshot.path)
        else:
            result["TECHNICAL_SCENARIO_FILE"] = str(snapshot.path)
    return result


def _tracked_and_untracked_paths(
    repo_root: Path = REPO_ROOT,
    git_path: Path = SYSTEM_TOOL_PATHS["git"],
) -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            [str(git_path), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AcceptanceError("无法枚举当前工作树") from exc
    if result.returncode:
        raise AcceptanceError("无法枚举当前工作树")
    decoded = (os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
    return tuple(sorted((repo_root / item for item in decoded), key=lambda path: path.as_posix()))


def _update_path_digest(digest: _Digest, path: Path, *, repo_root: Path = REPO_ROOT) -> None:
    relative = path.relative_to(repo_root).as_posix().encode("utf-8", errors="surrogateescape")
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"MISSING\0")
        return
    digest.update(metadata.st_mode.to_bytes(8, "big"))
    if path.is_symlink():
        digest.update(b"SYMLINK\0")
        digest.update(os.fsencode(os.readlink(path)))
        return
    if not path.is_file():
        digest.update(b"NON_FILE\0")
        return
    digest.update(b"FILE\0")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)


def source_fingerprint(
    env_file: Path,
    *,
    repo_root: Path = REPO_ROOT,
    git_path: Path = SYSTEM_TOOL_PATHS["git"],
) -> str:
    digest = hashlib.sha256()
    for path in _tracked_and_untracked_paths(repo_root, git_path):
        _update_path_digest(digest, path, repo_root=repo_root)
    digest.update(b"\0SELECTED_ENV\0")
    with env_file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _update_effective_environment_digest(digest: _Digest, environ: dict[str, str]) -> None:
    for name, value in sorted(environ.items()):
        encoded_name = name.encode("utf-8", errors="surrogateescape")
        encoded_value = value.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)


def _update_snapshot_digest(digest: _Digest, snapshots: tuple[ScenarioFileSnapshot, ...]) -> None:
    for snapshot in sorted(snapshots, key=lambda item: item.environment_key):
        payload, metadata = _read_stable_regular_file(snapshot.path, label=f"{snapshot.environment_key} 快照")
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            len(payload) != snapshot.size_bytes
            or actual_sha256 != snapshot.sha256
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise AcceptanceError(f"{snapshot.environment_key} 快照在验收期间发生变化")
        encoded_key = snapshot.environment_key.encode("ascii")
        digest.update(len(encoded_key).to_bytes(8, "big"))
        digest.update(encoded_key)
        digest.update(snapshot.size_bytes.to_bytes(8, "big"))
        digest.update(payload)
        digest.update(bytes.fromhex(snapshot.sha256))


def acceptance_fingerprint(
    source_env: Path,
    effective_env: Path,
    environ: dict[str, str],
    snapshots: tuple[ScenarioFileSnapshot, ...] = (),
    *,
    repo_root: Path = REPO_ROOT,
    git_path: Path = SYSTEM_TOOL_PATHS["git"],
) -> str:
    """散列工作树、配置、子进程环境以及受审场景快照的字节和 SHA-256。"""

    digest = hashlib.sha256()
    digest.update(bytes.fromhex(source_fingerprint(source_env, repo_root=repo_root, git_path=git_path)))
    digest.update(b"\0EFFECTIVE_ENV_FILE\0")
    with effective_env.open("rb") as stream:
        for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    digest.update(b"\0EFFECTIVE_PROCESS_ENV\0")
    _update_effective_environment_digest(digest, environ)
    digest.update(b"\0SCENARIO_SNAPSHOTS\0")
    _update_snapshot_digest(digest, snapshots)
    return digest.hexdigest()


def _stable_sha256(path: Path, *, label: str) -> str:
    payload, _metadata = _read_stable_regular_file(path, label=label)
    return hashlib.sha256(payload).hexdigest()


def write_acceptance_context(
    path: Path,
    *,
    source_env: Path,
    effective_env: Path,
    environ: dict[str, str],
    source_sha256: str,
    frozen_source_sha256: str,
    snapshots: tuple[ScenarioFileSnapshot, ...],
    containers: tuple[ContainerIdentity, ...],
) -> PinnedFileIdentity:
    """写入只由公共 runner 在 recreate 后生成的容器身份回执。"""

    required_environment = AcceptanceContextEnvironment(
        AGENT_GOV_ACCEPTANCE_RUN_ID=environ.get(ACCEPTANCE_RUN_ID_ENV, ""),
        AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE=environ.get(ACCEPTANCE_PROFILE_ENV, ""),
        AGENT_GOV_CONTAINER_ACCEPTANCE_TARGET=environ.get(ACCEPTANCE_TARGET_ENV, ""),
        AGENT_GOV_CONTAINER_ACCEPTANCE_COMMAND_SHA256=environ.get(ACCEPTANCE_COMMAND_SHA256_ENV, ""),
        COMPOSE_PROJECT_NAME=environ.get("COMPOSE_PROJECT_NAME", ""),
        COMPOSE_ENV_FILE=environ.get("COMPOSE_ENV_FILE", ""),
        API_BASE=environ.get("API_BASE", ""),
        FRONTEND_URL=environ.get("FRONTEND_URL", ""),
        AGENTGOV_ACCEPTANCE_LIVE_SOURCE_ROOT=environ.get(LIVE_SOURCE_ROOT_ENV, ""),
        AGENTGOV_ACCEPTANCE_DEPLOYABLE_SOURCE_ROOT=environ.get(DEPLOYABLE_SOURCE_ROOT_ENV, ""),
        AGENTGOV_ACCEPTANCE_FORMAL_SOURCE_ROOT=environ.get(FORMAL_SOURCE_ROOT_ENV, ""),
    )
    if any(not value for value in required_environment.values()):
        raise AcceptanceError("无法绑定验收回执的运行环境")
    toolchain = toolchain_from_environment(environ, error_type=AcceptanceError)
    verify_acceptance_toolchain(environ, toolchain, error_type=AcceptanceError)
    payload = AcceptanceContext(
        schema_version=4,
        source_env=str(source_env),
        source_fingerprint_sha256=source_sha256,
        frozen_source_sha256=frozen_source_sha256,
        source_env_sha256=_stable_sha256(source_env, label="验收源 env"),
        effective_env_sha256=_stable_sha256(effective_env, label="验收有效 env"),
        environment=required_environment,
        scenario_snapshots=[
            SnapshotIdentity(
                environment_key=item.environment_key,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in snapshots
        ],
        containers=list(containers),
        toolchain=toolchain,
    )
    return _write_private_snapshot(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _context_file(environ: dict[str, str]) -> Path:
    raw = environ.get(ACCEPTANCE_CONTEXT_ENV, "").strip()
    if not raw:
        raise AcceptanceError("缺少公共 runner 生成的验收上下文回执")
    path = _absolute_path_without_links(raw, base=REPO_ROOT)
    _validate_private_directory(path.parent, label="验收上下文目录", mode=0o500)
    return path


def _context_object(environ: dict[str, str]) -> AcceptanceContext:
    path = _context_file(environ)
    raw, metadata = _read_stable_regular_file(path, label="验收上下文回执")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o400:
        raise AcceptanceError("验收上下文回执必须由当前用户持有且已封存为 0400")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("验收上下文回执不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise AcceptanceError("验收上下文回执必须是 JSON object")
    return cast(AcceptanceContext, payload)


def _docker_output(arguments: list[str], environ: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [environ[TOOL_PATH_ENV_KEYS["docker"]], *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environ,
        )
    except OSError as exc:
        raise AcceptanceError("无法调用 Docker 核对验收容器身份") from exc
    if result.returncode != 0:
        raise AcceptanceError("Docker 拒绝验收容器身份核对")
    return result.stdout.strip()


def _verify_container_identities(
    value: object,
    *,
    run_id: str,
    profile: str,
    project: str,
    environ: dict[str, str],
) -> None:
    expected_services = CORE_ACCEPTANCE_SERVICES if profile == "core" else LANGFUSE_ACCEPTANCE_SERVICES
    identities = _parse_container_identities(value, expected_services)
    _verify_current_container_set(identities, run_id=run_id, project=project, environ=environ)
    template = (
        '{{.Id}}\t{{.Image}}\t{{.State.Running}}\t{{index .Config.Labels "'
        + ACCEPTANCE_LABEL_KEY
        + '"}}\t{{index .Config.Labels "com.docker.compose.project"}}\t'
        '{{index .Config.Labels "com.docker.compose.service"}}'
    )
    published_ports: dict[str, frozenset[int]] = {}
    for service, (container_id, image_id) in identities.items():
        observed = _docker_output(["inspect", "--format", template, container_id], environ).split("\t")
        if observed != [container_id, image_id, "true", run_id, project, service]:
            raise AcceptanceError(f"服务 {service} 的实时 Docker 身份与验收回执不一致")
        published_ports[service] = _verify_host_port_bindings(service, container_id, environ)
    _verify_service_url_binding(
        environ["API_BASE"],
        service="agent-gov-api",
        published_ports=published_ports["agent-gov-api"],
    )
    _verify_service_url_binding(
        environ["FRONTEND_URL"],
        service="agent-gov-ui",
        published_ports=published_ports["agent-gov-ui"],
    )


def _parse_container_identities(
    value: object,
    expected_services: frozenset[str],
) -> VerifiedContainerIdentities:
    if not isinstance(value, list) or len(value) != len(expected_services):
        raise AcceptanceError("验收回执的容器数量与 profile 不一致")
    identities: VerifiedContainerIdentities = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"service", "container_id", "image_id"}:
            raise AcceptanceError("验收回执的容器身份 schema 不精确")
        service = item.get("service")
        container_id = item.get("container_id")
        image_id = item.get("image_id")
        if (
            not isinstance(service, str)
            or service in identities
            or not isinstance(container_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or not isinstance(image_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        ):
            raise AcceptanceError("验收回执的容器或镜像 ID 无效")
        identities[service] = (container_id, image_id)
    if set(identities) != expected_services:
        raise AcceptanceError("验收回执的服务集合与 profile 不一致")
    return identities


def _verify_current_container_set(
    identities: VerifiedContainerIdentities,
    *,
    run_id: str,
    project: str,
    environ: dict[str, str],
) -> None:
    actual_ids = set(
        _docker_output(
            [
                "ps",
                "--no-trunc",
                "-aq",
                "--filter",
                f"label={ACCEPTANCE_LABEL_KEY}={run_id}",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            environ,
        ).splitlines()
    )
    if actual_ids != {identity[0] for identity in identities.values()}:
        raise AcceptanceError("当前 Docker project 容器集合与验收回执不一致")


def _verify_host_port_bindings(service: str, container_id: str, environ: dict[str, str]) -> frozenset[int]:
    bindings_raw = _docker_output(
        ["inspect", "--format", "{{json .HostConfig.PortBindings}}", container_id],
        environ,
    )
    try:
        bindings = json.loads(bindings_raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"服务 {service} 的端口绑定无法审计") from exc
    if bindings is None:
        return frozenset()
    if not isinstance(bindings, dict):
        raise AcceptanceError(f"服务 {service} 的端口绑定 schema 无效")
    published_ports: set[int] = set()
    for destinations in bindings.values():
        if not isinstance(destinations, list):
            raise AcceptanceError(f"服务 {service} 的端口绑定 schema 无效")
        for destination in destinations:
            if not isinstance(destination, dict):
                raise AcceptanceError(f"服务 {service} 的端口绑定 schema 无效")
            host_ip = destination.get("HostIp")
            host_port = destination.get("HostPort")
            if host_ip not in {"127.0.0.1", "::1"} or not isinstance(host_port, str):
                raise AcceptanceError(f"服务 {service} 存在非 loopback 端口绑定")
            if not host_port.isdigit() or not 50400 <= int(host_port) <= 50499:
                raise AcceptanceError(f"服务 {service} 的宿主机端口不在 50400–50499")
            published_ports.add(int(host_port))
    return frozenset(published_ports)


def _verify_service_url_binding(value: str, *, service: str, published_ports: frozenset[int]) -> None:
    port = urlparse(value).port
    if port is None or port not in published_ports:
        raise AcceptanceError(f"{service} 的 URL 端口与实时 Docker 发布端口不一致")


def _verify_loopback_url(value: str, *, label: str) -> None:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise AcceptanceError(f"{label} 不是有效 URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or port is None
        or not 50400 <= port <= 50499
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AcceptanceError(f"{label} 必须使用 50400–50499 范围内的 loopback URL")


def verify_acceptance_context(
    environ: dict[str, str] | None = None,
    *,
    require_trace_complete: bool = False,
) -> None:
    """重新核对工作树、env、场景快照和正在运行的 Docker 身份。"""

    current = dict(os.environ if environ is None else environ)
    if current.get(ACCEPTANCE_ACTIVE_ENV) != "1":
        raise AcceptanceError("验收上下文只能在公共隔离容器入口中使用")
    payload = _context_object(current)
    expected_keys = set(AcceptanceContext.__required_keys__)
    if set(payload) != expected_keys or payload.get("schema_version") != 4:
        raise AcceptanceError("验收上下文回执 schema 不精确")
    profile = _verify_context_environment(current, payload["environment"], require_trace_complete)
    verify_acceptance_toolchain(current, payload["toolchain"], error_type=AcceptanceError)
    _verify_context_sources(current, payload)
    snapshots = _snapshots_from_context(current, payload["scenario_snapshots"])
    _update_snapshot_digest(hashlib.sha256(), snapshots)
    _verify_container_identities(
        payload["containers"],
        run_id=current[ACCEPTANCE_RUN_ID_ENV],
        profile=profile,
        project=current["COMPOSE_PROJECT_NAME"],
        environ=current,
    )


def _verify_context_environment(
    current: dict[str, str],
    expected_environment: object,
    require_trace_complete: bool,
) -> str:
    environment_keys = {
        ACCEPTANCE_RUN_ID_ENV,
        ACCEPTANCE_PROFILE_ENV,
        ACCEPTANCE_TARGET_ENV,
        ACCEPTANCE_COMMAND_SHA256_ENV,
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_ENV_FILE",
        "API_BASE",
        "FRONTEND_URL",
        LIVE_SOURCE_ROOT_ENV,
        DEPLOYABLE_SOURCE_ROOT_ENV,
        FORMAL_SOURCE_ROOT_ENV,
    }
    if not isinstance(expected_environment, dict) or set(expected_environment) != environment_keys:
        raise AcceptanceError("验收上下文回执的环境绑定不精确")
    if any(current.get(key) != value for key, value in expected_environment.items()):
        raise AcceptanceError("当前进程环境与公共 runner 回执不一致")
    if re.fullmatch(r"[0-9a-f]{64}", current[ACCEPTANCE_COMMAND_SHA256_ENV]) is None:
        raise AcceptanceError("验收命令摘要无效")
    _verify_loopback_url(current["API_BASE"], label="API_BASE")
    _verify_loopback_url(current["FRONTEND_URL"], label="FRONTEND_URL")
    profile = current.get(ACCEPTANCE_PROFILE_ENV, "")
    if profile not in {"core", "langfuse"} or (require_trace_complete and profile != "langfuse"):
        raise AcceptanceError("验收回执 profile 不满足当前验证要求")
    return profile


def _verify_context_sources(current: dict[str, str], payload: AcceptanceContext) -> None:
    source_env_value = payload.get("source_env")
    if not isinstance(source_env_value, str):
        raise AcceptanceError("验收回执缺少源 env 身份")
    source_env = _absolute_path_without_links(source_env_value, base=REPO_ROOT)
    effective_env = _absolute_path_without_links(current["COMPOSE_ENV_FILE"], base=REPO_ROOT)
    live_root = _absolute_path_without_links(current[LIVE_SOURCE_ROOT_ENV], base=REPO_ROOT)
    deployable_root = _absolute_path_without_links(current[DEPLOYABLE_SOURCE_ROOT_ENV], base=REPO_ROOT)
    formal_root = _absolute_path_without_links(current[FORMAL_SOURCE_ROOT_ENV], base=REPO_ROOT)
    if (
        live_root != Path(current[LIVE_SOURCE_ROOT_ENV])
        or deployable_root != Path(current[DEPLOYABLE_SOURCE_ROOT_ENV])
        or formal_root != Path(current[FORMAL_SOURCE_ROOT_ENV])
    ):
        raise AcceptanceError("验收源码根必须是绝对无别名路径")
    git_path = Path(current[TOOL_PATH_ENV_KEYS["git"]])
    if payload.get("source_fingerprint_sha256") != source_fingerprint(
        source_env,
        repo_root=live_root,
        git_path=git_path,
    ):
        raise AcceptanceError("当前工作树或源 env 与验收回执不一致")
    frozen_source_sha256 = payload.get("frozen_source_sha256")
    if not isinstance(frozen_source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", frozen_source_sha256) is None:
        raise AcceptanceError("验收回执缺少冻结源码摘要")
    if source_artifact_sha256(live_root) != frozen_source_sha256:
        raise AcceptanceError("当前 deployable source 与容器构建快照不一致")
    if source_artifact_sha256(deployable_root) != frozen_source_sha256:
        raise AcceptanceError("冻结 deployable source 与验收回执不一致")
    formal_identity = next(
        (item for item in payload["toolchain"]["artifacts"] if item["name"] == "formal-source"),
        None,
    )
    if formal_identity is None or formal_identity["path"] != str(formal_root):
        raise AcceptanceError("正式验收源码根与工具链回执不一致")
    if payload.get("source_env_sha256") != _stable_sha256(source_env, label="验收源 env"):
        raise AcceptanceError("验收源 env 已变化")
    if payload.get("effective_env_sha256") != _stable_sha256(effective_env, label="验收有效 env"):
        raise AcceptanceError("验收有效 env 已变化")


def _snapshots_from_context(
    current: dict[str, str],
    snapshot_values: object,
) -> tuple[ScenarioFileSnapshot, ...]:
    if not isinstance(snapshot_values, list):
        raise AcceptanceError("验收回执缺少场景快照身份")
    expected_snapshot_keys = {key for key in SCENARIO_FILE_ENV_KEYS if current.get(key, "").strip()}
    snapshots: list[ScenarioFileSnapshot] = []
    for item in snapshot_values:
        if not isinstance(item, dict) or set(item) != {"environment_key", "size_bytes", "sha256"}:
            raise AcceptanceError("验收回执的场景快照 schema 不精确")
        key, size_bytes, digest = item.get("environment_key"), item.get("size_bytes"), item.get("sha256")
        if (
            key not in SCENARIO_FILE_ENV_KEYS
            or type(size_bytes) is not int
            or size_bytes < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not current.get(str(key))
        ):
            raise AcceptanceError("验收回执的场景快照身份无效")
        snapshots.append(
            ScenarioFileSnapshot(
                environment_key=str(key),
                path=_absolute_path_without_links(current[str(key)], base=REPO_ROOT),
                size_bytes=size_bytes,
                sha256=digest,
            )
        )
    if {item.environment_key for item in snapshots} != expected_snapshot_keys or len(snapshots) != len(expected_snapshot_keys):
        raise AcceptanceError("当前场景快照集合与验收回执不一致")
    return tuple(snapshots)
