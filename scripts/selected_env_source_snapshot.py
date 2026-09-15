"""selected-env operation 的只读源码快照、持久 CAS 与 Docker 工具链绑定。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from scripts import selected_env_persistent_source as persistent_source
from scripts import selected_env_python_toolchain as python_toolchain
from scripts.agentscope_atomic_cutover_images import selected_env_child_env
from scripts.selected_env_operation_contract import OperationEnvironment, SelectedEnvError

SOURCE_ROOT_ENV = "AGENTGOV_OPERATION_SOURCE_ROOT"
SOURCE_DIGEST_ENV = "AGENTGOV_OPERATION_SOURCE_SHA256"
SOURCE_TREE_DIGEST_ENV = "AGENTGOV_OPERATION_SOURCE_TREE_SHA256"
SOURCE_READONLY_ENV = "AGENTGOV_OPERATION_SOURCE_READONLY"
DOCKER_CLI_ENV = "AGENTGOV_OPERATION_DOCKER_CLI"
DOCKER_CLI_DIGEST_ENV = "AGENTGOV_OPERATION_DOCKER_CLI_SHA256"
COMPOSE_PLUGIN_ENV = "AGENTGOV_OPERATION_COMPOSE_PLUGIN"
COMPOSE_PLUGIN_DIGEST_ENV = "AGENTGOV_OPERATION_COMPOSE_PLUGIN_SHA256"
BUILDX_PLUGIN_ENV = "AGENTGOV_OPERATION_BUILDX_PLUGIN"
BUILDX_PLUGIN_DIGEST_ENV = "AGENTGOV_OPERATION_BUILDX_PLUGIN_SHA256"
BUILDX_CONFIG_ENV = "BUILDX_CONFIG"
BUILDX_BUILDER_ENV = "BUILDX_BUILDER"
INPUT_FILE_ENV = "AGENTGOV_OPERATION_INPUT_FILE"
INPUT_DIGEST_ENV = "AGENTGOV_OPERATION_INPUT_SHA256"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_DOCKER_CANDIDATES = (Path("/usr/bin/docker"), Path("/usr/local/bin/docker"))
_COMPOSE_PLUGIN_CANDIDATES = (
    Path("/usr/local/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/local/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/libexec/docker/cli-plugins/docker-compose"),
)
_BUILDX_PLUGIN_CANDIDATES = tuple(path.with_name("docker-buildx") for path in _COMPOSE_PLUGIN_CANDIDATES)
_INOTIFY_CHANGE_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800

SourceFreezer = Callable[[Path, Path], str]
SourceHasher = Callable[[Path], str]


@dataclass(frozen=True)
class FrozenDeploymentSource:
    root: Path
    digest: str
    execution_digest: str | None = None
    content_digest: str | None = None
    tree_digest: str | None = None
    readonly: bool = False
    persistent_binds: bool = False
    persistent_root: Path | None = None


def write_operation_input(operation_root: Path, payload: bytes) -> Path:
    """在独立私有目录原子写入 selected.env；调用方随后必须 seal。"""
    input_root = operation_root / "deployment-input"
    input_root.mkdir(mode=0o700)
    snapshot = input_root / "selected.env"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(snapshot, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(input_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return snapshot


def freeze_operation_source(
    repo_root: Path,
    operation_root: Path,
    _env_snapshot: Path,
    *,
    persistent_binds: bool,
    freeze_source: SourceFreezer,
    hash_source: SourceHasher,
) -> FrozenDeploymentSource:
    temporary_source = operation_root / "source-snapshot"
    canonical_digest = freeze_source(repo_root, temporary_source)
    if _SHA256.fullmatch(canonical_digest) is None:
        raise SelectedEnvError("冻结 deployable source 返回无效摘要")
    if hash_source(temporary_source) != canonical_digest:
        raise SelectedEnvError("冻结 deployable source bytes 与 canonical 摘要不一致")
    _remove_write_bits(temporary_source)
    execution_digest = hash_source(temporary_source)
    content_digest = _tree_snapshot_sha256(temporary_source, require_readonly=True)
    tree_digest = _tree_snapshot_sha256(temporary_source, require_readonly=True, include_generation=True)
    frozen = FrozenDeploymentSource(
        temporary_source,
        canonical_digest,
        execution_digest,
        content_digest,
        tree_digest,
        readonly=True,
        persistent_binds=persistent_binds,
    )
    if not persistent_binds:
        return frozen
    return replace(frozen, persistent_root=persistent_source.bind_source_root(canonical_digest))


def source_environment(source: FrozenDeploymentSource) -> OperationEnvironment:
    values = {
        SOURCE_ROOT_ENV: source.root.as_posix(),
        SOURCE_DIGEST_ENV: source.execution_digest or source.digest,
    }
    if source.readonly:
        if source.tree_digest is None or source.content_digest is None:
            raise SelectedEnvError("只读 deployment source 缺少完整 tree 摘要")
        values.update({SOURCE_READONLY_ENV: "1", SOURCE_TREE_DIGEST_ENV: source.tree_digest})
    if source.persistent_binds:
        if source.persistent_root is None:
            raise SelectedEnvError("持久 deployment source 缺少 root-owned bind root")
        values.update(
            {
                "RUNTIME_BOOTSTRAP_HOST_DIR": (source.persistent_root / "docker/runtime-bootstrap").as_posix(),
                "AGENTGOV_API_GATE_STATE_DIR_HOST": (source.persistent_root / "docker/api-gate").as_posix(),
            },
        )
    return values


def operation_environment(
    operation_root: Path,
    snapshot: Path,
    source: FrozenDeploymentSource,
    version: str,
    *,
    compose_files: tuple[Path, Path],
    include_buildx: bool,
    include_python: bool,
    input_environment: Mapping[str, str],
) -> OperationEnvironment:
    child_env = selected_env_child_env(
        snapshot,
        explicit={
            "AGENTGOV_SOURCE_ARTIFACT_SHA256": source.digest,
            "APP_VERSION": version,
            "AGENTGOV_RUNTIME_VERSION": version,
        },
        compose_files=compose_files,
    )
    child_env.update(source_environment(source))
    child_env.update(input_environment)
    python_environment = python_toolchain.prepare_python_toolchain(operation_root) if include_python else {}
    child_env.update(python_environment)
    child_env.update(prepare_docker_toolchain(operation_root, include_buildx=include_buildx))
    if python_environment:
        python_bin = Path(python_environment[python_toolchain.PYTHON_EXECUTABLE_ENV]).parent
        child_env["PATH"] = f"{python_bin}:{child_env['PATH']}"
    child_env.update(
        {
            "AGENT_GOV_COMPOSE_ENV_FILE": snapshot.as_posix(),
            "COMPOSE_ENV_FILE": snapshot.as_posix(),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    child_env.pop("DOCKER_CONTEXT", None)
    return child_env


def seal_operation_input(snapshot: Path) -> OperationEnvironment:
    """封闭 selected.env 的独立目录，并记录包含 generation 的完整边界。"""
    parent = snapshot.parent
    try:
        metadata = snapshot.lstat()
        parent_metadata = parent.lstat()
        snapshot.chmod(0o400)
        parent.chmod(0o500)
    except OSError as exc:
        raise SelectedEnvError("无法封闭 selected.env deployment input") from exc
    valid = (
        not snapshot.is_symlink()
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and not parent.is_symlink()
        and stat.S_ISDIR(parent_metadata.st_mode)
        and parent_metadata.st_uid == os.geteuid()
    )
    if not valid:
        raise SelectedEnvError("selected.env deployment input owner/type 无效")
    digest = _binary_boundary_sha256(snapshot)
    return {INPUT_FILE_ENV: snapshot.as_posix(), INPUT_DIGEST_ENV: digest}


def verify_operation_input(child_env: Mapping[str, str]) -> Path | None:
    raw_snapshot = child_env.get(INPUT_FILE_ENV)
    if raw_snapshot is None:
        return None
    snapshot = Path(raw_snapshot)
    expected = child_env.get(INPUT_DIGEST_ENV, "")
    if not snapshot.is_absolute() or _SHA256.fullmatch(expected) is None:
        raise SelectedEnvError("selected.env deployment input boundary 无效")
    try:
        metadata = snapshot.lstat()
        parent = snapshot.parent.lstat()
    except OSError as exc:
        raise SelectedEnvError("无法复验 selected.env deployment input") from exc
    valid = (
        not snapshot.is_symlink()
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o400
        and not snapshot.parent.is_symlink()
        and stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == os.geteuid()
        and stat.S_IMODE(parent.st_mode) == 0o500
    )
    if not valid or _binary_boundary_sha256(snapshot) != expected:
        raise SelectedEnvError("selected.env deployment input bytes/mode/generation 已漂移")
    return snapshot


def verify_command_source(child_env: Mapping[str, str], *, hash_source: SourceHasher) -> Path:
    verify_operation_input(child_env)
    raw_root = child_env.get(SOURCE_ROOT_ENV, "")
    expected = child_env.get(SOURCE_DIGEST_ENV, "")
    source_root = Path(raw_root)
    if not source_root.is_absolute() or source_root.is_symlink() or _SHA256.fullmatch(expected) is None:
        raise SelectedEnvError("冻结的 deployable source command boundary 无效")
    try:
        actual = hash_source(source_root)
    except (OSError, ValueError) as exc:
        raise SelectedEnvError("无法复验冻结的 deployable source") from exc
    if actual != expected:
        raise SelectedEnvError("冻结的 deployable source 在 command 同步屏障前发生变化")
    if child_env.get(SOURCE_READONLY_ENV) == "1":
        expected_tree = child_env.get(SOURCE_TREE_DIGEST_ENV, "")
        if _SHA256.fullmatch(expected_tree) is None:
            raise SelectedEnvError("冻结的 deployable source tree boundary 无效")
        observed_tree = _tree_snapshot_sha256(source_root, require_readonly=True, include_generation=True)
        if observed_tree != expected_tree:
            raise SelectedEnvError("冻结的 deployable source tree 在 command 同步屏障前发生变化")
    return source_root


def prepare_docker_toolchain(operation_root: Path, *, include_buildx: bool) -> OperationEnvironment:
    toolchain_root = operation_root / "docker-toolchain"
    docker_config = toolchain_root / "docker-config"
    plugin_directory = docker_config / "cli-plugins"
    plugin_directory.mkdir(mode=0o700, parents=True)
    docker_config.chmod(0o700)
    docker_cli = toolchain_root / "docker"
    compose_plugin = plugin_directory / "docker-compose"
    docker_digest = _copy_verified_binary(_resolve_trusted_binary(_DOCKER_CANDIDATES, "Docker CLI"), docker_cli)
    plugin_digest = _copy_verified_binary(
        _resolve_trusted_binary(_COMPOSE_PLUGIN_CANDIDATES, "Docker Compose plugin"),
        compose_plugin,
    )
    plugin_directory.chmod(0o500)
    toolchain_root.chmod(0o500)
    environment = {
        "DOCKER_CONFIG": docker_config.as_posix(),
        "PATH": f"{toolchain_root}:{_TRUSTED_PATH}",
        DOCKER_CLI_ENV: docker_cli.as_posix(),
        DOCKER_CLI_DIGEST_ENV: docker_digest,
        COMPOSE_PLUGIN_ENV: compose_plugin.as_posix(),
        COMPOSE_PLUGIN_DIGEST_ENV: plugin_digest,
    }
    if include_buildx:
        buildx_state = operation_root / "buildx-state"
        buildx_state.mkdir(mode=0o700)
        buildx_plugin = plugin_directory / "docker-buildx"
        plugin_directory.chmod(0o700)
        buildx_digest = _copy_verified_binary(
            _resolve_trusted_binary(_BUILDX_PLUGIN_CANDIDATES, "Docker Buildx plugin"),
            buildx_plugin,
        )
        plugin_directory.chmod(0o500)
        environment.update(
            {
                BUILDX_PLUGIN_ENV: buildx_plugin.as_posix(),
                BUILDX_PLUGIN_DIGEST_ENV: buildx_digest,
                BUILDX_CONFIG_ENV: buildx_state.as_posix(),
                BUILDX_BUILDER_ENV: "default",
            },
        )
    # BuildKit 构建时可能在 DOCKER_CONFIG 写 .token_seed/.token_seed.lock。
    # 其认证提供者容忍权限拒绝并采用本次会话随机值；保持配置目录不可变，
    # 不把认证缓存文件列入可变目录白名单。
    docker_config.chmod(0o500)
    verify_docker_toolchain(environment)
    return environment


def bind_docker_command(command: list[str], child_env: Mapping[str, str]) -> list[str]:
    docker_cli = verify_docker_toolchain(child_env)
    if command and command[0] == "docker":
        if docker_cli is None:
            raise SelectedEnvError("Docker command 缺少冻结 CLI boundary")
        return [docker_cli.as_posix(), *command[1:]]
    return command


@contextmanager
def mutation_monitor(child_env: Mapping[str, str]) -> Iterator[None]:
    watch_directories = _mutation_watch_directories(child_env)
    if not watch_directories:
        yield
        return
    descriptor = _open_inotify()
    try:
        for directory in watch_directories:
            _add_inotify_watch(descriptor, directory)
        yield
    finally:
        changed = _inotify_changed(descriptor)
        os.close(descriptor)
        if changed:
            raise SelectedEnvError("冻结 source/Docker toolchain 在 command 执行期间发生瞬时变化")


def verify_docker_toolchain(child_env: Mapping[str, str]) -> Path | None:
    raw_cli = child_env.get(DOCKER_CLI_ENV)
    if raw_cli is None:
        return None
    docker_cli = Path(raw_cli)
    compose_plugin = Path(child_env.get(COMPOSE_PLUGIN_ENV, ""))
    docker_config = Path(child_env.get("DOCKER_CONFIG", ""))
    expected_cli = child_env.get(DOCKER_CLI_DIGEST_ENV, "")
    expected_plugin = child_env.get(COMPOSE_PLUGIN_DIGEST_ENV, "")
    valid_layout = docker_cli.is_absolute() and compose_plugin == docker_config / "cli-plugins/docker-compose" and docker_cli.parent == docker_config.parent
    if not valid_layout or _SHA256.fullmatch(expected_cli) is None or _SHA256.fullmatch(expected_plugin) is None:
        raise SelectedEnvError("冻结 Docker CLI/plugin boundary 无效")
    _require_private_directory(docker_cli.parent, mode=0o500, label="Docker toolchain")
    _require_private_directory(docker_config, mode=0o500, label="Docker config")
    _require_private_directory(docker_config / "cli-plugins", mode=0o500, label="Docker plugin")
    if set(docker_config.iterdir()) != {docker_config / "cli-plugins"}:
        raise SelectedEnvError("Docker config 含未绑定的运行配置")
    _require_readonly_binary(docker_cli, expected_cli, "Docker CLI")
    _require_readonly_binary(compose_plugin, expected_plugin, "Docker Compose plugin")
    expected_plugins = {compose_plugin}
    raw_buildx = child_env.get(BUILDX_PLUGIN_ENV)
    raw_buildx_digest = child_env.get(BUILDX_PLUGIN_DIGEST_ENV)
    if raw_buildx is not None or raw_buildx_digest is not None:
        buildx_plugin = Path(raw_buildx or "")
        if buildx_plugin != docker_config / "cli-plugins/docker-buildx" or _SHA256.fullmatch(raw_buildx_digest or "") is None:
            raise SelectedEnvError("冻结 Docker Buildx plugin boundary 无效")
        _require_readonly_binary(buildx_plugin, raw_buildx_digest or "", "Docker Buildx plugin")
        buildx_state = docker_cli.parent.parent / "buildx-state"
        if child_env.get(BUILDX_CONFIG_ENV) != buildx_state.as_posix() or child_env.get(BUILDX_BUILDER_ENV) != "default":
            raise SelectedEnvError("冻结 Docker Buildx state/builder 未绑定到私有本机 default")
        _require_private_directory(buildx_state, mode=0o700, label="Docker Buildx state")
        expected_plugins.add(buildx_plugin)
    elif child_env.get(BUILDX_CONFIG_ENV) or child_env.get(BUILDX_BUILDER_ENV):
        raise SelectedEnvError("非构建操作不得注入 Docker Buildx state/builder")
    plugin_entries = set((docker_config / "cli-plugins").iterdir())
    if plugin_entries != expected_plugins:
        raise SelectedEnvError("Docker config 含未绑定的 CLI plugin")
    return docker_cli


def _mutation_watch_directories(child_env: Mapping[str, str]) -> tuple[Path, ...]:
    directories: set[Path] = set()
    if child_env.get(SOURCE_READONLY_ENV) == "1":
        source_root = Path(child_env.get(SOURCE_ROOT_ENV, ""))
        directories.update(path for path in (source_root, *source_root.rglob("*")) if path.is_dir())
    raw_cli = child_env.get(DOCKER_CLI_ENV)
    raw_plugin = child_env.get(COMPOSE_PLUGIN_ENV)
    if raw_cli is not None:
        directories.add(Path(raw_cli).parent)
    if raw_plugin is not None:
        directories.add(Path(raw_plugin).parent)
        directories.add(Path(raw_plugin).parents[1])
    raw_input = child_env.get(INPUT_FILE_ENV)
    if raw_input is not None:
        directories.add(Path(raw_input).parent)
    return tuple(sorted(directories))


def _open_inotify() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    initializer = libc.inotify_init1
    initializer.argtypes = [ctypes.c_int]
    initializer.restype = ctypes.c_int
    descriptor = int(initializer(os.O_NONBLOCK | os.O_CLOEXEC))
    if descriptor < 0:
        error = ctypes.get_errno()
        raise SelectedEnvError("当前平台无法建立冻结 command mutation monitor") from OSError(error, os.strerror(error))
    return descriptor


def _add_inotify_watch(descriptor: int, directory: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    add_watch = libc.inotify_add_watch
    add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add_watch.restype = ctypes.c_int
    if int(add_watch(descriptor, os.fsencode(directory), _INOTIFY_CHANGE_MASK)) < 0:
        error = ctypes.get_errno()
        raise SelectedEnvError("无法覆盖冻结 command source 的 mutation watch") from OSError(error, os.strerror(error))


def _inotify_changed(descriptor: int) -> bool:
    try:
        return bool(os.read(descriptor, 1024 * 1024))
    except BlockingIOError:
        return False


def _remove_write_bits(root: Path) -> None:
    paths = sorted((root, *root.rglob("*")), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise SelectedEnvError("deployment source snapshot 含 symlink/special entry")
        normalized = 0o555 if stat.S_ISDIR(metadata.st_mode) else stat.S_IMODE(metadata.st_mode) & ~0o222
        path.chmod(normalized)


def _tree_snapshot_sha256(
    root: Path,
    *,
    require_readonly: bool,
    include_generation: bool = False,
) -> str:
    metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise SelectedEnvError("deployment source root 必须是真实目录")
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*"))):
        relative = path.relative_to(root).as_posix().encode()
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not (stat.S_ISDIR(current.st_mode) or stat.S_ISREG(current.st_mode)):
            raise SelectedEnvError("deployment source 含 symlink/special entry")
        if require_readonly and stat.S_IMODE(current.st_mode) & 0o222:
            raise SelectedEnvError("deployment source 不再只读")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(current.st_mode).to_bytes(4, "big"))
        digest.update(b"d" if stat.S_ISDIR(current.st_mode) else b"f")
        if include_generation:
            digest.update(current.st_mtime_ns.to_bytes(8, "big", signed=True))
            digest.update(current.st_ctime_ns.to_bytes(8, "big", signed=True))
        if stat.S_ISREG(current.st_mode):
            _hash_regular_file(digest, path, current)
    return digest.hexdigest()


def _hash_regular_file(digest: Any, path: Path, initial: os.stat_result) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        expected = _file_identity(initial)
        if _file_identity(opened) != expected:
            raise SelectedEnvError("deployment source 在摘要读取前发生变化")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != expected:
            raise SelectedEnvError("deployment source 在摘要读取期间发生变化")
    finally:
        os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _resolve_trusted_binary(candidates: Sequence[Path], label: str) -> Path:
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.lstat()
        except OSError as exc:
            raise SelectedEnvError(f"无法解析可信 {label}") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()} or metadata.st_mode & 0o022:
            raise SelectedEnvError(f"可信 {label} owner/type/mode 无效")
        return resolved
    raise SelectedEnvError(f"缺少可信 {label}")


def _copy_verified_binary(source: Path, destination: Path) -> str:
    initial = source.lstat()
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o500)
    digest = hashlib.sha256()
    try:
        if _file_identity(os.fstat(source_fd)) != _file_identity(initial):
            raise SelectedEnvError("Docker toolchain binary 在复制前发生变化")
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target_fd, view) :]
        os.fchmod(target_fd, 0o500)
        os.fsync(target_fd)
        if _file_identity(os.fstat(source_fd)) != _file_identity(initial):
            raise SelectedEnvError("Docker toolchain binary 在复制期间发生变化")
    finally:
        os.close(target_fd)
        os.close(source_fd)
    if _regular_file_content_sha256(destination) != digest.hexdigest():
        raise SelectedEnvError("Docker toolchain binary copy bytes 不一致")
    return _binary_boundary_sha256(destination)


def _require_private_directory(path: Path, *, mode: int, label: str) -> None:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or path.resolve(strict=True) != path
    ):
        raise SelectedEnvError(f"冻结 {label} 目录边界无效")


def _require_readonly_binary(path: Path, expected_digest: str, label: str) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise SelectedEnvError(f"冻结 {label} owner/type 无效")
    if metadata.st_mode & 0o222 or _binary_boundary_sha256(path) != expected_digest:
        raise SelectedEnvError(f"冻结 {label} bytes/mode 已漂移")


def _binary_boundary_sha256(path: Path) -> str:
    initial = path.lstat()
    digest = hashlib.sha256()
    _hash_regular_file(digest, path, initial)
    for value in _file_identity(initial):
        digest.update(value.to_bytes(16, "big", signed=True))
    return digest.hexdigest()


def _regular_file_content_sha256(path: Path) -> str:
    initial = path.lstat()
    digest = hashlib.sha256()
    _hash_regular_file(digest, path, initial)
    return digest.hexdigest()
