"""以 fd 读取、编译并登记首阶段实际加载的仓库 Python 源码。"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import stat
import sys
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Final, cast

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_SOURCE_BYTES: Final = 8 * 1024 * 1024
_MAX_LOADED_SOURCES: Final = 512
_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_IMPORT_AUTHORITY_SHA256"
_TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_TOOLCHAIN_SHA256"
_BOOTSTRAP_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOADED_BOOTSTRAP_SHA256"
_REPOSITORY_NAMESPACES: Final = frozenset({"agentgov_testkit", "app", "scripts"})


class ImportAuthorityError(RuntimeError):
    """首阶段仓库源码未由 actual-byte authority 约束。"""


class LoadedSourceDigests(dict[str, str]):
    """由 actual-byte registry 持有的仓库源码摘要。"""


def invalidate_import_root_cache(import_root: Path) -> None:
    """丢弃复用 proc-fd 导入根留下的旧 PathFinder 结果。"""

    prefix = f"{import_root}/"
    for path in tuple(sys.path_importer_cache):
        if isinstance(path, str) and (path == str(import_root) or path.startswith(prefix)):
            sys.path_importer_cache.pop(path, None)


def repository_roots(namespace: Mapping[str, object], module_file: str) -> tuple[Path, Path]:
    injected_root = namespace.get("_ACTUAL_REPOSITORY_ROOT")
    injected_import = namespace.get("_ACTUAL_REPOSITORY_IMPORT_ROOT")
    if isinstance(injected_root, str) and isinstance(injected_import, str):
        root, import_root = Path(injected_root), Path(injected_import)
        if root.is_absolute() and import_root.is_absolute() and re.fullmatch(r"/proc/self/fd/[0-9]+", str(import_root)):
            return root, import_root
        raise ImportAuthorityError("injected repository import authority is invalid")
    root = Path(module_file).resolve().parents[1]
    return root, root


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _source_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or len(relative.parts) > 64
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".py"
    ):
        raise ImportAuthorityError("repository import source path is invalid")
    return relative


def _read_descriptor(descriptor: int) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_BYTES or before.st_uid not in {0, os.geteuid()} or before.st_mode & stat.S_IWOTH:
        raise ImportAuthorityError("repository import source identity is invalid")
    chunks: list[bytes] = []
    offset = 0
    remaining = _MAX_SOURCE_BYTES + 1
    while remaining:
        chunk = os.pread(descriptor, min(64 * 1024, remaining), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    encoded = b"".join(chunks)
    if len(encoded) != before.st_size or not _same_identity(before, os.fstat(descriptor)):
        raise ImportAuthorityError("repository import source changed while reading")
    return encoded


def _trusted_directory(identity: os.stat_result) -> bool:
    writable = bool(identity.st_mode & stat.S_IWOTH)
    sticky_root = bool(identity.st_mode & stat.S_ISVTX) and identity.st_uid == 0
    return identity.st_uid in {0, os.geteuid()} and (not writable or sticky_root)


def _open_root(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            identity = os.fstat(child)
            linked = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not _same_identity(identity, linked) or not _trusted_directory(identity):
                os.close(child)
                raise ImportAuthorityError("repository import root authority is invalid")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _root_descriptor(path: Path, descriptor: int | None) -> int:
    if descriptor is None:
        return _open_root(path)
    reopened = _open_root(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode) or not _same_identity(before, os.fstat(reopened)) or not _trusted_directory(before):
            raise ImportAuthorityError("inherited repository root authority is invalid")
        return os.dup(descriptor)
    finally:
        os.close(reopened)


def _read_relative(root_fd: int, relative: PurePosixPath) -> bytes:
    directory_fd = os.dup(root_fd)
    descriptor: int | None = None
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            identity = os.fstat(child)
            linked = os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_identity(identity, linked) or identity.st_uid not in {0, os.geteuid()} or identity.st_mode & stat.S_IWOTH:
                os.close(child)
                raise ImportAuthorityError("repository import ancestor authority is invalid")
            os.close(directory_fd)
            directory_fd = child
        descriptor = os.open(relative.name, _FILE_FLAGS, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        linked = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_identity(before, linked):
            raise ImportAuthorityError("repository import source linkage drifted")
        encoded = _read_descriptor(descriptor)
        current = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_identity(before, current):
            raise ImportAuthorityError("repository import source linkage drifted")
        return encoded
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


@dataclass(frozen=True, slots=True)
class _CapturedSource:
    relative: PurePosixPath
    path: Path
    encoded: bytes
    sha256: str
    package: bool


def _expected_sources(value: Mapping[str, str] | None) -> LoadedSourceDigests | None:
    if value is None:
        return None
    expected = dict(value)
    if (
        not expected
        or len(expected) > _MAX_LOADED_SOURCES
        or any(_source_relative(path).as_posix() != path for path in expected)
        or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in expected.values())
    ):
        raise ImportAuthorityError("frozen repository source authority is invalid")
    return LoadedSourceDigests(expected)


class ActualLoadedSourceRegistry:
    def __init__(
        self,
        repository: Path,
        *,
        root_fd: int | None = None,
        expected: Mapping[str, str] | None = None,
    ) -> None:
        self.repository = Path(os.path.abspath(repository))
        self.root_fd = _root_descriptor(self.repository, root_fd)
        try:
            self.import_root = Path(f"/proc/self/fd/{self.root_fd}")
            invalidate_import_root_cache(self.import_root)
            self._digests: dict[str, str] = {}
            self._expected = _expected_sources(expected)
            self._frozen = expected is not None
        except BaseException:
            os.close(self.root_fd)
            raise

    def register_expected(self, relative_value: str, expected_sha256: str) -> None:
        relative = _source_relative(relative_value)
        encoded = _read_relative(self.root_fd, relative)
        if hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise ImportAuthorityError("actual-loaded repository source drifted")
        self._record(relative, expected_sha256)

    def capture(self, path: Path, *, package: bool) -> _CapturedSource:
        absolute = Path(os.path.abspath(path))
        relative = _source_relative(self._contained_relative(absolute).as_posix())
        encoded = _read_relative(self.root_fd, relative)
        digest = hashlib.sha256(encoded).hexdigest()
        self._record(relative, digest)
        return _CapturedSource(relative, absolute, encoded, digest, package)

    def _contained_relative(self, absolute: Path) -> PurePosixPath:
        for root in (self.import_root, self.repository):
            try:
                relative = PurePosixPath(absolute.relative_to(root).as_posix())
            except ValueError:
                continue
            if not relative.parts or len(relative.parts) > 64 or any(part in {"", ".", ".."} for part in relative.parts):
                raise ImportAuthorityError("repository import source path is invalid")
            return relative
        raise ImportAuthorityError("repository import escaped its source root")

    def _record(self, relative: PurePosixPath, digest: str) -> None:
        key = relative.as_posix()
        existing = self._digests.get(key)
        expected = self._expected.get(key) if self._expected is not None else None
        if self._expected is not None and expected is None:
            raise ImportAuthorityError("repository import set changed after authority freeze")
        if expected is not None and expected != digest:
            raise ImportAuthorityError("actual-loaded repository source drifted")
        if self._frozen and self._expected is None and existing is None:
            raise ImportAuthorityError("repository import set changed after authority freeze")
        if existing is not None and existing != digest:
            raise ImportAuthorityError("actual-loaded repository source drifted")
        self._digests[key] = digest
        if len(self._digests) > _MAX_LOADED_SOURCES:
            raise ImportAuthorityError("repository import source set is oversized")

    def freeze(self) -> LoadedSourceDigests:
        self._expected = LoadedSourceDigests(self._digests)
        self._frozen = True
        return LoadedSourceDigests(sorted(self._digests.items()))

    def digest(self, relative_value: str) -> str:
        relative = _source_relative(relative_value).as_posix()
        digest = self._digests.get(relative)
        if digest is None:
            raise ImportAuthorityError("repository source was not actually loaded")
        return digest


class _RepositorySourceLoader(importlib.abc.Loader):
    def __init__(self, source: _CapturedSource) -> None:
        self.source = source

    def create_module(self, _spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        code = compile(self.source.encoded, str(self.source.path), "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _RepositorySourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, registry: ActualLoadedSourceRegistry) -> None:
        self.registry = registry

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del target
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        namespace = fullname.partition(".")[0]
        if spec is None:
            if namespace in _REPOSITORY_NAMESPACES:
                raise ImportAuthorityError("repository namespace import authority is unavailable")
            return None
        if spec.origin in {None, "built-in", "frozen"}:
            if namespace in _REPOSITORY_NAMESPACES and spec.origin is None:
                raise ImportAuthorityError("repository namespace import is not source-only")
            return None
        origin = Path(os.path.abspath(spec.origin))
        try:
            self.registry._contained_relative(origin)
        except ImportAuthorityError:
            if namespace in _REPOSITORY_NAMESPACES:
                raise
            return None
        if origin.suffix != ".py":
            raise ImportAuthorityError("repository import is not source-only")
        package = spec.submodule_search_locations is not None
        source = self.registry.capture(origin, package=package)
        locations = [str(origin.parent)] if package else None
        return importlib.util.spec_from_file_location(fullname, origin, loader=_RepositorySourceLoader(source), submodule_search_locations=locations)


def _scripts_package(registry: ActualLoadedSourceRegistry) -> None:
    package = types.ModuleType("scripts")
    package.__package__ = "scripts"
    package.__path__ = [str(registry.import_root / "scripts")]
    package.__spec__ = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
    package.__spec__.submodule_search_locations = package.__path__
    sys.modules[package.__name__] = package


def install_frozen_repository_imports(
    repository: Path,
    root_fd: int,
    expected: Mapping[str, str],
    *,
    preloaded: Mapping[str, str],
) -> ActualLoadedSourceRegistry:
    python_sources = {path: digest for path, digest in expected.items() if path.endswith(".py")}
    if set(preloaded) - set(python_sources):
        raise ImportAuthorityError("preloaded repository source authority is invalid")
    registry = ActualLoadedSourceRegistry(repository, root_fd=root_fd, expected=python_sources)
    try:
        for path, digest in preloaded.items():
            registry.register_expected(path, digest)
    except BaseException:
        os.close(registry.root_fd)
        raise
    sys.meta_path.insert(0, _RepositorySourceFinder(registry))
    sys.path.insert(0, str(registry.import_root))
    _scripts_package(registry)
    return registry


def _required_digest(environ: dict[str, str], key: str) -> str:
    value = environ.get(key, "")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ImportAuthorityError("bootstrap source digest is invalid")
    return value


def _consume_source_descriptors(helper_fd: int, toolchain_fd: int) -> tuple[bytes, bytes]:
    try:
        return _read_descriptor(helper_fd), _read_descriptor(toolchain_fd)
    finally:
        for descriptor in {helper_fd, toolchain_fd}:
            os.close(descriptor)


def _execute_toolchain(arguments: list[str], environ: dict[str, str]) -> int:
    if len(arguments) < 6:
        raise ImportAuthorityError("bootstrap import authority arguments are invalid")
    repository_fd = int(arguments[0])
    helper_fd = int(arguments[1])
    toolchain_fd = int(arguments[2])
    helper_path = Path(os.path.abspath(arguments[3]))
    toolchain_path = Path(os.path.abspath(arguments[4]))
    repository = Path(os.path.abspath(arguments[5]))
    helper_encoded, toolchain_encoded = _consume_source_descriptors(helper_fd, toolchain_fd)
    helper_digest = _required_digest(environ, _SHA256_ENV)
    toolchain_digest = _required_digest(environ, _TOOLCHAIN_SHA256_ENV)
    if hashlib.sha256(helper_encoded).hexdigest() != helper_digest or hashlib.sha256(toolchain_encoded).hexdigest() != toolchain_digest:
        raise ImportAuthorityError("bootstrap captured source digest drifted")
    try:
        registry = ActualLoadedSourceRegistry(repository, root_fd=repository_fd)
    finally:
        os.close(repository_fd)
    registry.register_expected(helper_path.relative_to(repository).as_posix(), helper_digest)
    registry.register_expected(toolchain_path.relative_to(repository).as_posix(), toolchain_digest)
    registry.register_expected("scripts/container_acceptance_bootstrap.py", _required_digest(environ, _BOOTSTRAP_SHA256_ENV))
    if arguments[6:7] == ["python"]:
        for key in (_SHA256_ENV, _TOOLCHAIN_SHA256_ENV, _BOOTSTRAP_SHA256_ENV):
            environ.pop(key, None)
            os.environ.pop(key, None)
    sys.meta_path.insert(0, _RepositorySourceFinder(registry))
    sys.path.insert(0, str(registry.import_root))
    _scripts_package(registry)
    module = types.ModuleType("scripts.container_acceptance_toolchain")
    module.__file__ = str(registry.import_root / toolchain_path.relative_to(repository))
    module.__package__ = "scripts"
    module.__spec__ = None
    module.__dict__["_ACTUAL_LOADED_SOURCE_REGISTRY"] = registry
    module.__dict__["_ACTUAL_REPOSITORY_ROOT"] = str(repository)
    module.__dict__["_ACTUAL_REPOSITORY_IMPORT_ROOT"] = str(registry.import_root)
    sys.modules[module.__name__] = module
    sys.prefix = str(Path(sys.executable).parent.parent)
    sys.exec_prefix = sys.prefix
    sys.argv = [str(toolchain_path), *arguments[6:]]
    exec(compile(toolchain_encoded, str(module.__file__), "exec", dont_inherit=True), module.__dict__)
    entrypoint = module.__dict__.get("main")
    if not callable(entrypoint):
        raise ImportAuthorityError("container acceptance toolchain entrypoint is invalid")
    result = cast(object, entrypoint())
    return result if isinstance(result, int) else 1


def main() -> int:
    try:
        return _execute_toolchain(sys.argv[1:], dict(os.environ))
    except (ImportAuthorityError, OSError, UnicodeError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
