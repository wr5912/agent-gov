"""为 selected-env 子命令物化并绑定最小、只读的 Python 依赖闭包。"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.selected_env_operation_contract import OperationEnvironment, SelectedEnvError

PYTHON_EXECUTABLE_ENV = "AGENTGOV_OPERATION_PYTHON"
PYTHON_ROOT_ENV = "AGENTGOV_OPERATION_PYTHON_ROOT"
PYTHON_DIGEST_ENV = "AGENTGOV_OPERATION_PYTHON_SHA256"
PYTHON_DEPENDENCY_DIGEST_ENV = "AGENTGOV_OPERATION_PYTHON_DEPS_SHA256"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_INOTIFY_CHANGE_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
_REQUIRED_DISTRIBUTIONS = (
    "annotated-types",
    "greenlet",
    "pydantic",
    "pydantic-core",
    "python-dotenv",
    "PyYAML",
    "SQLAlchemy",
    "typing-extensions",
    "typing-inspection",
)


@dataclass(frozen=True)
class _SourceFile:
    source: Path
    relative: Path
    identity: tuple[int, ...]
    content_digest: str


def prepare_python_toolchain(operation_root: Path) -> OperationEnvironment:
    """复制解释器、stdlib 与所需 distribution，返回不可被 host env 替换的边界。"""
    toolchain_root = operation_root / "python-toolchain"
    runtime_root = toolchain_root / "runtime"
    dependencies = toolchain_root / "dependencies"
    runtime_root.mkdir(mode=0o700, parents=True)
    dependencies.mkdir(mode=0o700)
    source_executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    base_root = Path(sys.base_prefix).resolve(strict=True)
    _require_source_executable(source_executable, base_root)
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    source_library = base_root / "lib"
    stdlib = source_library / python_version
    if not stdlib.is_dir() or stdlib.is_symlink():
        raise SelectedEnvError("Python stdlib source boundary 无效")
    runtime_sources = _runtime_source_files(source_executable, source_library)
    dependency_sources = _dependency_source_files(_REQUIRED_DISTRIBUTIONS)
    _materialize_files(runtime_sources, runtime_root)
    _materialize_files(dependency_sources, dependencies)
    copied_python = runtime_root / "bin/python"
    _make_readonly(toolchain_root, executable=copied_python)
    environment = {
        PYTHON_EXECUTABLE_ENV: copied_python.as_posix(),
        PYTHON_ROOT_ENV: toolchain_root.as_posix(),
        PYTHON_DIGEST_ENV: _tree_boundary_digest(runtime_root),
        PYTHON_DEPENDENCY_DIGEST_ENV: _tree_boundary_digest(dependencies),
        "PYTHONHOME": runtime_root.as_posix(),
        "PYTHONPATH": dependencies.as_posix(),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    verify_python_toolchain(environment)
    return environment


def bind_python_command(command: list[str], child_env: Mapping[str, str]) -> list[str]:
    frozen = verify_python_toolchain(child_env)
    if not command or not _is_python_command(command[0]):
        return command
    if frozen is None:
        raise SelectedEnvError("Python command 缺少冻结解释器/dependency boundary")
    return [frozen.as_posix(), *command[1:]]


def verify_python_toolchain(child_env: Mapping[str, str]) -> Path | None:
    raw_python = child_env.get(PYTHON_EXECUTABLE_ENV)
    if raw_python is None:
        return None
    root = Path(child_env.get(PYTHON_ROOT_ENV, ""))
    python = Path(raw_python)
    runtime = root / "runtime"
    dependencies = root / "dependencies"
    expected_runtime = child_env.get(PYTHON_DIGEST_ENV, "")
    expected_dependencies = child_env.get(PYTHON_DEPENDENCY_DIGEST_ENV, "")
    valid = (
        root.is_absolute()
        and python == runtime / "bin/python"
        and child_env.get("PYTHONHOME") == runtime.as_posix()
        and child_env.get("PYTHONPATH") == dependencies.as_posix()
        and _SHA256.fullmatch(expected_runtime) is not None
        and _SHA256.fullmatch(expected_dependencies) is not None
    )
    if not valid:
        raise SelectedEnvError("冻结 Python interpreter/dependency boundary 无效")
    _require_readonly_tree(root)
    if _tree_boundary_digest(runtime) != expected_runtime:
        raise SelectedEnvError("冻结 Python interpreter/stdlib 已漂移")
    if _tree_boundary_digest(dependencies) != expected_dependencies:
        raise SelectedEnvError("冻结 Python dependency closure 已漂移")
    return python


@contextmanager
def mutation_monitor(child_env: Mapping[str, str]) -> Iterator[None]:
    raw_root = child_env.get(PYTHON_ROOT_ENV)
    if raw_root is None:
        yield
        return
    root = Path(raw_root)
    descriptor = _open_inotify()
    try:
        for directory in (root, *(path for path in root.rglob("*") if path.is_dir())):
            _add_inotify_watch(descriptor, directory)
        yield
    finally:
        changed = _inotify_changed(descriptor)
        os.close(descriptor)
        if changed:
            raise SelectedEnvError("冻结 Python interpreter/dependency 在 command 执行期间发生瞬时变化")


def _runtime_source_files(executable: Path, library: Path) -> tuple[_SourceFile, ...]:
    files = [_snapshot_source(executable, Path("bin/python"))]
    python_library = library / f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (*python_library.rglob("*"), *library.glob("libpython*"))
    for path in sorted(set(candidates)):
        relative = Path("lib") / path.relative_to(library)
        if "site-packages" in path.parts or path.is_dir():
            continue
        files.append(_snapshot_source(path, relative))
    return tuple(files)


def _dependency_source_files(distributions: Sequence[str]) -> tuple[_SourceFile, ...]:
    files: dict[Path, _SourceFile] = {}
    for name in distributions:
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise SelectedEnvError(f"Python dependency closure 缺少 distribution: {name}") from exc
        root = Path(distribution.locate_file("")).resolve(strict=True)
        for item in distribution.files or ():
            source = Path(distribution.locate_file(item))
            try:
                relative = source.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                continue
            if source.is_dir():
                continue
            snapshot = _snapshot_source(source, relative)
            existing = files.get(relative)
            if existing is not None and existing.content_digest != snapshot.content_digest:
                raise SelectedEnvError(f"Python distributions 对同一路径给出不同 bytes: {relative}")
            files[relative] = snapshot
    if not files:
        raise SelectedEnvError("Python dependency closure 为空")
    return tuple(files[path] for path in sorted(files))


def _snapshot_source(source: Path, relative: Path) -> _SourceFile:
    try:
        resolved = source.resolve(strict=True)
        initial = resolved.lstat()
    except OSError as exc:
        raise SelectedEnvError(f"无法读取 Python dependency source: {relative}") from exc
    if not stat.S_ISREG(initial.st_mode):
        raise SelectedEnvError(f"Python dependency source 不是普通文件: {relative}")
    digest = _regular_file_digest(resolved, initial)
    return _SourceFile(resolved, relative, _file_identity(initial), digest)


def _materialize_files(files: Sequence[_SourceFile], destination: Path) -> None:
    for item in files:
        target = destination / item.relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_source_file(item, target)


def _copy_source_file(item: _SourceFile, target: Path) -> None:
    source_fd = os.open(item.source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    digest = hashlib.sha256()
    try:
        if _file_identity(os.fstat(source_fd)) != item.identity:
            raise SelectedEnvError(f"Python dependency 在物化前发生变化: {item.relative}")
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target_fd, view) :]
        os.fsync(target_fd)
        if _file_identity(os.fstat(source_fd)) != item.identity:
            raise SelectedEnvError(f"Python dependency 在物化期间发生变化: {item.relative}")
    finally:
        os.close(target_fd)
        os.close(source_fd)
    if digest.hexdigest() != item.content_digest:
        raise SelectedEnvError(f"Python dependency snapshot bytes 不一致: {item.relative}")


def _make_readonly(root: Path, *, executable: Path) -> None:
    for path in sorted((root, *root.rglob("*")), key=lambda item: len(item.parts), reverse=True):
        metadata = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise SelectedEnvError("Python toolchain snapshot 含 symlink/special entry")
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o500)
        else:
            path.chmod(0o500 if path == executable else 0o400)


def _require_readonly_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        valid_type = stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        if path.is_symlink() or not valid_type or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o222:
            raise SelectedEnvError("冻结 Python toolchain owner/type/mode 无效")


def _tree_boundary_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*"))):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        for value in _file_identity(metadata):
            digest.update(value.to_bytes(16, "big", signed=True))
        if stat.S_ISREG(metadata.st_mode):
            digest.update(bytes.fromhex(_regular_file_digest(path, metadata)))
    return digest.hexdigest()


def _regular_file_digest(path: Path, initial: os.stat_result) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        if _file_identity(os.fstat(descriptor)) != _file_identity(initial):
            raise SelectedEnvError("Python dependency source identity 已漂移")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(initial):
            raise SelectedEnvError("Python dependency source 在读取期间发生变化")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_source_executable(executable: Path, base_root: Path) -> None:
    metadata = executable.lstat()
    try:
        executable.relative_to(base_root)
    except ValueError as exc:
        raise SelectedEnvError("Python executable 不属于当前 base runtime") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
        raise SelectedEnvError("Python executable source owner/type 无效")


def _is_python_command(command: str) -> bool:
    return command in {"python", "python3", sys.executable, getattr(sys, "_base_executable", "")}


def _open_inotify() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    initializer = libc.inotify_init1
    initializer.argtypes = [ctypes.c_int]
    initializer.restype = ctypes.c_int
    descriptor = int(initializer(os.O_NONBLOCK | os.O_CLOEXEC))
    if descriptor < 0:
        error = ctypes.get_errno()
        raise SelectedEnvError("当前平台无法建立 Python toolchain mutation monitor") from OSError(error, os.strerror(error))
    return descriptor


def _add_inotify_watch(descriptor: int, directory: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    add_watch = libc.inotify_add_watch
    add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add_watch.restype = ctypes.c_int
    if int(add_watch(descriptor, os.fsencode(directory), _INOTIFY_CHANGE_MASK)) < 0:
        error = ctypes.get_errno()
        raise SelectedEnvError("无法覆盖冻结 Python toolchain mutation watch") from OSError(error, os.strerror(error))


def _inotify_changed(descriptor: int) -> bool:
    try:
        return bool(os.read(descriptor, 1024 * 1024))
    except BlockingIOError:
        return False
