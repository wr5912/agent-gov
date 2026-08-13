"""在候选快照内验证并消费 Prepared 绑定的 Python 源码与依赖。"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import stat
import sys
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Final, NoReturn

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_AUTHORITY_BYTES: Final = 64 * 1024
_MAX_SOURCE_BYTES: Final = 8 * 1024 * 1024
_MAX_DEPENDENCY_ENTRIES: Final = 50_000
_MAX_DEPENDENCY_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_DEPENDENCY_DEPTH: Final = 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_NAMESPACES: Final = frozenset({"agentgov_testkit", "app", "scripts"})


class SnapshotAuthorityError(RuntimeError):
    """候选快照与 Prepared authority 不一致。"""


class FrozenSourceDigests(dict[str, str]):
    """Prepared candidate 冻结的仓库源码摘要。"""


def _invalidate_import_root_cache(import_root: Path) -> None:
    """丢弃复用 proc-fd 导入根留下的旧 PathFinder 结果。"""

    prefix = f"{import_root}/"
    for path in tuple(sys.path_importer_cache):
        if isinstance(path, str) and (path == str(import_root) or path.startswith(prefix)):
            sys.path_importer_cache.pop(path, None)


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


def _path_identity(identity: os.stat_result) -> list[int]:
    return [
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_nlink,
        identity.st_size,
        identity.st_uid,
        identity.st_gid,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    ]


def _open_child_directory(parent_fd: int, leaf: str) -> int:
    descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    identity = os.fstat(descriptor)
    linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    valid = stat.S_ISDIR(identity.st_mode) and _same_identity(identity, linked) and identity.st_uid == os.geteuid() and not identity.st_mode & stat.S_IWOTH
    if not valid:
        os.close(descriptor)
        raise SnapshotAuthorityError("snapshot directory authority is invalid")
    return descriptor


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = _open_child_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _source_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or len(relative.parts) > 64
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".py"
    ):
        raise SnapshotAuthorityError("snapshot source path is invalid")
    return relative


def _read_descriptor(descriptor: int, *, limit: int = _MAX_SOURCE_BYTES) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit or before.st_uid != os.geteuid() or before.st_mode & stat.S_IWOTH:
        raise SnapshotAuthorityError("snapshot source identity is invalid")
    chunks: list[bytes] = []
    offset = 0
    remaining = limit + 1
    while remaining:
        chunk = os.pread(descriptor, min(64 * 1024, remaining), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    encoded = b"".join(chunks)
    if len(encoded) != before.st_size or not _same_identity(before, os.fstat(descriptor)):
        raise SnapshotAuthorityError("snapshot source changed while reading")
    return encoded


def _read_relative(root_fd: int, relative: PurePosixPath) -> bytes:
    directory_fd = os.dup(root_fd)
    descriptor: int | None = None
    try:
        for part in relative.parts[:-1]:
            child = _open_child_directory(directory_fd, part)
            os.close(directory_fd)
            directory_fd = child
        descriptor = os.open(relative.name, _FILE_FLAGS, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        linked = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_identity(before, linked):
            raise SnapshotAuthorityError("snapshot source linkage drifted")
        encoded = _read_descriptor(descriptor)
        if not _same_identity(before, os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)):
            raise SnapshotAuthorityError("snapshot source linkage drifted")
        return encoded
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _loaded_sources(snapshot: list[object]) -> FrozenSourceDigests:
    value = snapshot[24]
    if not isinstance(value, list) or not 0 < len(value) <= 64:
        raise SnapshotAuthorityError("snapshot loaded source authority is invalid")
    expected: dict[str, str] = {}
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], str):
            raise SnapshotAuthorityError("snapshot loaded source authority is invalid")
        relative, digest = item
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or (relative != "VERSION" and path.suffix != ".py")
            or _SHA256.fullmatch(digest) is None
            or relative in expected
        ):
            raise SnapshotAuthorityError("snapshot loaded source authority is invalid")
        expected[relative] = digest
    canonical = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    if list(expected) != sorted(expected, key=str.encode) or hashlib.sha256(canonical).hexdigest() != snapshot[25]:
        raise SnapshotAuthorityError("snapshot loaded source digest is invalid")
    return FrozenSourceDigests(expected)


@dataclass(frozen=True, slots=True)
class _CapturedSource:
    relative: PurePosixPath
    encoded: bytes
    digest: str
    package: bool


class _FrozenSourceRegistry:
    def __init__(self, repository: Path, repository_fd: int, expected: Mapping[str, str], preloaded: Mapping[str, str]) -> None:
        self.repository = repository
        self.root_fd = os.dup(repository_fd)
        try:
            self.import_root = Path(f"/proc/self/fd/{self.root_fd}")
            _invalidate_import_root_cache(self.import_root)
            self.expected = {key: value for key, value in expected.items() if key.endswith(".py")}
            if not self.expected or set(preloaded) - set(self.expected):
                raise SnapshotAuthorityError("snapshot preloaded source authority is invalid")
            if any(self.expected[path] != digest for path, digest in preloaded.items()):
                raise SnapshotAuthorityError("snapshot preloaded source digest is invalid")
            self.observed = dict(preloaded)
        except BaseException:
            os.close(self.root_fd)
            raise

    def close(self) -> None:
        os.close(self.root_fd)

    def contained_relative(self, absolute: Path) -> PurePosixPath:
        for root in (self.import_root, self.repository):
            try:
                relative = PurePosixPath(absolute.relative_to(root).as_posix())
            except ValueError:
                continue
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise SnapshotAuthorityError("snapshot import path is invalid")
            return relative
        raise SnapshotAuthorityError("snapshot import escaped its repository")

    def capture(self, origin: Path, *, package: bool) -> _CapturedSource:
        relative = _source_relative(self.contained_relative(origin).as_posix())
        key = relative.as_posix()
        expected = self.expected.get(key)
        if expected is None:
            raise SnapshotAuthorityError("snapshot import was not frozen in Prepared authority")
        encoded = _read_relative(self.root_fd, relative)
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != expected:
            raise SnapshotAuthorityError("snapshot import source drifted")
        self.observed[key] = digest
        return _CapturedSource(relative, encoded, digest, package)


class _FrozenSourceLoader(importlib.abc.Loader):
    def __init__(self, source: _CapturedSource, registry: _FrozenSourceRegistry) -> None:
        self.source = source
        self.registry = registry

    def create_module(self, _spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        path = self.registry.import_root / self.source.relative
        exec(compile(self.source.encoded, str(path), "exec", dont_inherit=True), module.__dict__)


class _FrozenSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, registry: _FrozenSourceRegistry) -> None:
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
                raise SnapshotAuthorityError("snapshot namespace import authority is unavailable")
            return None
        if spec.origin in {None, "built-in", "frozen"}:
            if namespace in _REPOSITORY_NAMESPACES and spec.origin is None:
                raise SnapshotAuthorityError("snapshot namespace import is not source-only")
            return None
        origin = Path(os.path.abspath(spec.origin))
        try:
            self.registry.contained_relative(origin)
        except SnapshotAuthorityError:
            if namespace in _REPOSITORY_NAMESPACES:
                raise
            return None
        if origin.suffix != ".py":
            raise SnapshotAuthorityError("snapshot import is not source-only")
        source = self.registry.capture(origin, package=spec.submodule_search_locations is not None)
        locations = [str(self.registry.import_root / source.relative.parent)] if source.package else None
        return importlib.util.spec_from_file_location(
            fullname,
            self.registry.import_root / source.relative,
            loader=_FrozenSourceLoader(source, self.registry),
            submodule_search_locations=locations,
        )


def _install_scripts_package(registry: _FrozenSourceRegistry) -> None:
    package = types.ModuleType("scripts")
    package.__package__ = "scripts"
    package.__path__ = [str(registry.import_root / "scripts")]
    package.__spec__ = importlib.machinery.ModuleSpec("scripts", loader=None, is_package=True)
    package.__spec__.submodule_search_locations = package.__path__
    sys.modules[package.__name__] = package


class _DependencyState:
    def __init__(self) -> None:
        self.digest = hashlib.sha256(b"agentgov-dependency-snapshot-v1\0")
        self.entries = 0
        self.regular_bytes = 0

    def record(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.digest.update(len(encoded).to_bytes(8, "big"))
        self.digest.update(encoded)

    def names(self, descriptor: int) -> tuple[str, ...]:
        names: list[str] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > _MAX_DEPENDENCY_ENTRIES:
                    raise SnapshotAuthorityError("snapshot dependency entry limit exceeded")
                names.append(entry.name)
        return tuple(sorted(names))


def _dependency_target(parts: tuple[str, ...], target: str) -> None:
    if not target or "\x00" in target or Path(target).is_absolute():
        raise SnapshotAuthorityError("snapshot dependency symlink authority is invalid")
    resolved = list(parts[:-1])
    for part in Path(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise SnapshotAuthorityError("snapshot dependency symlink escapes its root")
            resolved.pop()
        else:
            resolved.append(part)
        if len(resolved) > _MAX_DEPENDENCY_DEPTH * 4:
            raise SnapshotAuthorityError("snapshot dependency symlink path limit exceeded")


def _hash_dependency_file(parent_fd: int, name: str, expected: os.stat_result, state: _DependencyState) -> str:
    if expected.st_size < 0 or state.regular_bytes + expected.st_size > _MAX_DEPENDENCY_BYTES:
        raise SnapshotAuthorityError("snapshot dependency byte limit exceeded")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(expected, opened):
            raise SnapshotAuthorityError("snapshot dependency file authority drifted")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if not _same_identity(opened, os.fstat(descriptor)):
            raise SnapshotAuthorityError("snapshot dependency file authority drifted")
        if not _same_identity(opened, os.stat(name, dir_fd=parent_fd, follow_symlinks=False)):
            raise SnapshotAuthorityError("snapshot dependency file linkage drifted")
    finally:
        os.close(descriptor)
    state.regular_bytes += expected.st_size
    return digest.hexdigest()


def _scan_dependency_entry(
    descriptor: int,
    name: str,
    identity: os.stat_result,
    parts: tuple[str, ...],
    state: _DependencyState,
    depth: int,
) -> None:
    relative = "/".join(parts)
    mode = stat.S_IMODE(identity.st_mode)
    if identity.st_uid != os.geteuid():
        raise SnapshotAuthorityError("snapshot dependency contains foreign authority")
    if stat.S_ISDIR(identity.st_mode):
        if mode != 0o500:
            raise SnapshotAuthorityError("snapshot dependency directory is writable")
        state.record(["directory", relative, 0o500])
        child = _open_child_directory(descriptor, name)
        try:
            _scan_dependency(child, parts, state, depth + 1)
        finally:
            os.close(child)
        return
    if stat.S_ISREG(identity.st_mode):
        if mode not in {0o400, 0o500} or identity.st_nlink != 1:
            raise SnapshotAuthorityError("snapshot dependency file authority is invalid")
        digest = _hash_dependency_file(descriptor, name, identity, state)
        state.record(["file", relative, mode, identity.st_size, digest])
        return
    if stat.S_ISLNK(identity.st_mode):
        target = os.readlink(name, dir_fd=descriptor)
        _dependency_target(parts, target)
        if not _same_identity(identity, os.stat(name, dir_fd=descriptor, follow_symlinks=False)):
            raise SnapshotAuthorityError("snapshot dependency symlink drifted")
        state.record(["symlink", relative, target])
        return
    raise SnapshotAuthorityError("snapshot dependency contains an unsupported object")


def _scan_dependency(descriptor: int, prefix: tuple[str, ...], state: _DependencyState, depth: int = 0) -> None:
    if depth > _MAX_DEPENDENCY_DEPTH:
        raise SnapshotAuthorityError("snapshot dependency depth limit exceeded")
    before = os.fstat(descriptor)
    for name in state.names(descriptor):
        identity = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        _scan_dependency_entry(descriptor, name, identity, (*prefix, name), state, depth)
    if not _same_identity(before, os.fstat(descriptor)):
        raise SnapshotAuthorityError("snapshot dependency directory drifted")


def _verify_dependency(descriptor: int, value: object) -> None:
    if not isinstance(value, list) or len(value) != 7 or not isinstance(value[1], list):
        raise SnapshotAuthorityError("snapshot dependency authority is invalid")
    identity = os.fstat(descriptor)
    if _path_identity(identity) != value[1] or stat.S_IMODE(identity.st_mode) != 0o500:
        raise SnapshotAuthorityError("snapshot dependency root authority drifted")
    state = _DependencyState()
    _scan_dependency(descriptor, (), state)
    if (state.digest.hexdigest(), state.entries, state.regular_bytes) != (value[4], value[5], value[6]):
        raise SnapshotAuthorityError("snapshot dependency digest authority drifted")


def _snapshot_dependency_root(snapshot: list[object], index: int, expected: Path, prefix: str, environ: Mapping[str, str]) -> Path:
    value = snapshot[index]
    valid = (
        isinstance(value, list)
        and len(value) == 7
        and value[0] == str(expected)
        and value[4] == environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_SHA256", "")
        and str(value[5]) == environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_ENTRIES", "")
        and str(value[6]) == environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_BYTES", "")
    )
    if not valid:
        raise SnapshotAuthorityError("snapshot dependency environment is invalid")
    return expected


def _verify_node(dependencies_fd: int, snapshot: list[object], environ: Mapping[str, str]) -> None:
    expected = Path(environ.get("AGENT_GOV_ACCEPTANCE_NODE", ""))
    value = snapshot[23]
    if not isinstance(value, list) or len(value) != 6 or value[0] != str(expected):
        raise SnapshotAuthorityError("snapshot Node executable authority is invalid")
    parent = _open_relative_directory(dependencies_fd, ("node", "bin"))
    descriptor: int | None = None
    try:
        descriptor = os.open("node", _FILE_FLAGS, dir_fd=parent)
        before = os.fstat(descriptor)
        encoded = _read_descriptor(descriptor, limit=512 * 1024 * 1024)
        linked = os.stat("node", dir_fd=parent, follow_symlinks=False)
        valid = (
            stat.S_ISREG(before.st_mode)
            and stat.S_IMODE(before.st_mode) == 0o500
            and _path_identity(before) == value[1]
            and _same_identity(before, linked)
            and hashlib.sha256(encoded).hexdigest() == value[5]
        )
        if not valid:
            raise SnapshotAuthorityError("snapshot Node executable authority drifted")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _validate_dependencies(snapshot_root: Path, root_fd: int, repository_fd: int, snapshot: list[object], environ: Mapping[str, str]) -> int:
    repository = snapshot_root / "repository"
    frontend = _snapshot_dependency_root(snapshot, 20, repository / "frontend/node_modules", "FRONTEND", environ)
    python = _snapshot_dependency_root(snapshot, 21, snapshot_root / "dependencies/python-site-packages", "PYTHON", environ)
    pnpm = _snapshot_dependency_root(snapshot, 22, snapshot_root / "dependencies/pnpm", "PNPM", environ)
    if (
        environ.get("AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT"),
        environ.get("AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES"),
        environ.get("AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT"),
    ) != (str(frontend), str(python), str(pnpm)):
        raise SnapshotAuthorityError("snapshot dependency paths are invalid")
    dependencies_fd: int | None = None
    frontend_fd: int | None = None
    python_fd: int | None = None
    pnpm_fd: int | None = None
    try:
        dependencies_fd = _open_child_directory(root_fd, "dependencies")
        frontend_fd = _open_relative_directory(repository_fd, ("frontend", "node_modules"))
        python_fd = _open_child_directory(dependencies_fd, "python-site-packages")
        pnpm_fd = _open_child_directory(dependencies_fd, "pnpm")
        if _path_identity(os.fstat(dependencies_fd)) != snapshot[31]:
            raise SnapshotAuthorityError("snapshot dependency parent authority drifted")
        _verify_dependency(frontend_fd, snapshot[20])
        _verify_dependency(python_fd, snapshot[21])
        _verify_dependency(pnpm_fd, snapshot[22])
        _verify_node(dependencies_fd, snapshot, environ)
        return python_fd
    except BaseException:
        if python_fd is not None:
            os.close(python_fd)
        raise
    finally:
        for descriptor in (pnpm_fd, frontend_fd, dependencies_fd):
            if descriptor is not None:
                os.close(descriptor)


def _candidate(environ: Mapping[str, str], snapshot_root: Path) -> tuple[list[object], FrozenSourceDigests]:
    raw = environ.get("AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY", "")
    if not raw or len(raw.encode()) > _MAX_AUTHORITY_BYTES:
        raise SnapshotAuthorityError("snapshot reexec authority is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise SnapshotAuthorityError("snapshot reexec authority is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract", "source", "snapshot"}
        or payload.get("contract") != "agentgov.container-acceptance-candidate.v5"
        or not isinstance(payload.get("snapshot"), list)
        or len(payload["snapshot"]) != 32
        or payload["snapshot"][5] != str(snapshot_root)
        or payload["snapshot"][7] != str(snapshot_root / "repository")
    ):
        raise SnapshotAuthorityError("snapshot reexec authority is invalid")
    snapshot = payload["snapshot"]
    return snapshot, _loaded_sources(snapshot)


def _validate_transport(environ: Mapping[str, str], snapshot: list[object], runner: Path) -> None:
    evidence = environ.get("AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE", "")
    digest = environ.get("AGENT_GOV_ACCEPTANCE_TOOLCHAIN_SHA256", "")
    lock_fd = environ.get("AGENT_GOV_ACCEPTANCE_LOCK_FD", "")
    cookie = environ.get("AGENT_GOV_ACCEPTANCE_LOCK_COOKIE", "")
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        toolchain = json.loads(evidence)
    except (UnicodeError, ValueError) as exc:
        raise SnapshotAuthorityError("snapshot toolchain authority is invalid") from exc
    encoded = json.dumps(toolchain, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    valid = (
        runner == Path(snapshot[7]) / "scripts/run_container_acceptance.py"
        and bool(environ.get("AGENT_GOV_PREPARED_RECEIPT_AUTHORITY"))
        and lock_fd.isdecimal()
        and int(lock_fd) >= 3
        and len(cookie) == 16
        and environ.get("AGENT_GOV_ACCEPTANCE_REEXEC_STAGE") == "locked-snapshot-v1"
        and environ.get("AGENT_GOV_ACCEPTANCE_SIGNAL_HANDOFF") == "blocked-v1"
        and {signal.SIGINT, signal.SIGTERM}.issubset(blocked)
        and hashlib.sha256(encoded).hexdigest() == digest
    )
    if not valid:
        raise SnapshotAuthorityError("snapshot reexec transport is invalid")


def resume(
    arguments: list[str],
    environ: Mapping[str, str],
    *,
    snapshot_root: Path,
    root_fd: int,
    repository_fd: int,
    module_digest: str,
    bootstrap_digest: str,
) -> NoReturn:
    if not arguments:
        raise SnapshotAuthorityError("snapshot runner target is invalid")
    runner = Path(os.path.abspath(arguments[0]))
    snapshot, expected = _candidate(environ, snapshot_root)
    _validate_transport(environ, snapshot, runner)
    if _path_identity(os.fstat(root_fd)) != snapshot[6] or _path_identity(os.fstat(repository_fd)) != snapshot[8]:
        raise SnapshotAuthorityError("snapshot root authority drifted")
    python_fd = _validate_dependencies(snapshot_root, root_fd, repository_fd, snapshot, environ)
    registry: _FrozenSourceRegistry | None = None
    finder: _FrozenSourceFinder | None = None
    previous_scripts = sys.modules.get("scripts")
    previous_path = list(sys.path)
    try:
        module_path = "scripts/container_acceptance_snapshot_authority.py"
        bootstrap_path = "scripts/container_acceptance_bootstrap.py"
        runner_path = "scripts/run_container_acceptance.py"
        preloaded = {module_path: module_digest, bootstrap_path: bootstrap_digest}
        registry = _FrozenSourceRegistry(snapshot_root / "repository", repository_fd, expected, preloaded)
        runner_source = registry.capture(registry.import_root / runner_path, package=False)
        registry.observed[runner_path] = runner_source.digest
        finder = _FrozenSourceFinder(registry)
        sys.meta_path.insert(0, finder)
        _install_scripts_package(registry)
        stdlib = [path for path in sys.path if path and "site-packages" not in Path(path).parts]
        sys.path[:] = [
            str(registry.import_root),
            str(registry.import_root / "packages/agentgov-testkit/src"),
            f"/proc/self/fd/{python_fd}",
            *stdlib,
        ]
        sys.argv = [str(registry.import_root / runner_path), *arguments[1:]]
        namespace = {
            "__name__": "__main__",
            "__file__": str(registry.import_root / runner_path),
            "__package__": None,
            "__spec__": None,
        }
        exec(compile(runner_source.encoded, str(registry.import_root / runner_path), "exec", dont_inherit=True), namespace)
        raise SnapshotAuthorityError("snapshot runner returned unexpectedly")
    finally:
        if finder is not None and finder in sys.meta_path:
            sys.meta_path.remove(finder)
        if previous_scripts is None:
            sys.modules.pop("scripts", None)
        else:
            sys.modules["scripts"] = previous_scripts
        sys.path[:] = previous_path
        if registry is not None:
            registry.close()
        os.close(python_fd)
