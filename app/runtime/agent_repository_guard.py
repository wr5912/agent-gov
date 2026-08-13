from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from app.runtime.advisory_lock import advisory_lock


class AgentRepositoryGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> _DirectoryIdentity:
        return cls(device=observed.st_dev, inode=observed.st_ino)


class AgentRepositoryTemporaryEntryAuthority:
    """Pin one operation directory below a stable repository-owned temporary root."""

    def __init__(
        self,
        *,
        owner: AgentRepositoryTemporaryDirectoryAuthority,
        name: str,
        descriptor: int,
        identity: _DirectoryIdentity,
    ) -> None:
        self.owner = owner
        self.name = name
        self.descriptor = descriptor
        self.identity = identity
        self.removed = False

    @property
    def path(self) -> Path:
        return self.owner.path / self.name

    @property
    def stable_path(self) -> Path:
        return Path("/proc/self/fd") / str(self.descriptor)

    def verify(self) -> None:
        if self.removed:
            raise AgentRepositoryGuardError("Repository temporary entry was already removed")
        self.owner.verify()
        root_fd = self.owner.require_descriptor()
        _verify_child_directory(root_fd, self.name, self.descriptor, self.identity, label="Repository temporary entry")

    def clear(self, *, preserve_names: frozenset[str] = frozenset()) -> None:
        self.verify()
        _clear_directory(self.descriptor, preserve_names=preserve_names)
        self.verify()

    def close(self) -> None:
        os.close(self.descriptor)


class AgentRepositoryTemporaryDirectoryAuthority:
    """Pin a temporary root below one real parent and mutate entries via dir fds."""

    def __init__(
        self,
        *,
        parent_path: Path,
        name: str,
        parent_descriptor: int,
        parent_identity: _DirectoryIdentity,
        descriptor: int | None,
        identity: _DirectoryIdentity | None,
    ) -> None:
        self.parent_path = parent_path
        self.name = name
        self.parent_descriptor = parent_descriptor
        self.parent_identity = parent_identity
        self.descriptor = descriptor
        self.identity = identity

    @property
    def path(self) -> Path:
        return self.parent_path / self.name

    @property
    def stable_path(self) -> Path:
        return Path("/proc/self/fd") / str(self.require_descriptor())

    @property
    def exists(self) -> bool:
        return self.descriptor is not None

    def require_descriptor(self) -> int:
        if self.descriptor is None:
            raise AgentRepositoryGuardError(f"Repository temporary authority is missing: {self.path}")
        return self.descriptor

    def verify(self) -> None:
        _verify_directory_path(
            self.parent_path,
            self.parent_descriptor,
            self.parent_identity,
            label="Repository temporary parent authority",
        )
        if self.descriptor is None or self.identity is None:
            if _entry_exists(self.parent_descriptor, self.name):
                raise AgentRepositoryGuardError(f"Repository temporary authority appeared unexpectedly: {self.path}")
            return
        _verify_child_directory(
            self.parent_descriptor,
            self.name,
            self.descriptor,
            self.identity,
            label="Repository temporary root authority",
        )

    def require_absent(self, name: str) -> None:
        _validate_authority_name(name)
        self.verify()
        if _entry_exists(self.require_descriptor(), name):
            raise AgentRepositoryGuardError(f"Repository temporary entry already exists: {self.path / name}")

    def pin_entry(self, name: str) -> AgentRepositoryTemporaryEntryAuthority | None:
        _validate_authority_name(name)
        self.verify()
        pinned = _pin_child_directory(self.require_descriptor(), name, missing_ok=True)
        if pinned is None:
            return None
        descriptor, identity = pinned
        try:
            self.verify()
        except BaseException:
            os.close(descriptor)
            raise
        return AgentRepositoryTemporaryEntryAuthority(owner=self, name=name, descriptor=descriptor, identity=identity)

    def create_entry(self, name: str) -> AgentRepositoryTemporaryEntryAuthority:
        _validate_authority_name(name)
        self.verify()
        try:
            os.mkdir(name, mode=0o770, dir_fd=self.require_descriptor())
        except OSError as exc:
            raise AgentRepositoryGuardError(f"Repository temporary entry cannot be created safely: {self.path / name}") from exc
        pinned = self.pin_entry(name)
        if pinned is None:
            raise AgentRepositoryGuardError(f"Repository temporary entry disappeared after creation: {self.path / name}")
        return pinned

    def remove_entry(self, entry: AgentRepositoryTemporaryEntryAuthority) -> None:
        if entry.owner is not self:
            raise AgentRepositoryGuardError("Repository temporary entry belongs to another authority")
        entry.verify()
        _clear_directory(entry.descriptor)
        entry.verify()
        try:
            os.rmdir(entry.name, dir_fd=self.require_descriptor())
        except OSError as exc:
            raise AgentRepositoryGuardError(f"Repository temporary entry cannot be removed safely: {entry.path}") from exc
        self.require_removed(entry)

    def require_removed(self, entry: AgentRepositoryTemporaryEntryAuthority) -> None:
        if entry.owner is not self:
            raise AgentRepositoryGuardError("Repository temporary entry belongs to another authority")
        self.verify()
        observed = _child_stat(self.require_descriptor(), entry.name)
        if observed is not None:
            suffix = "was replaced" if _DirectoryIdentity.from_stat(observed) != entry.identity else "was not removed"
            raise AgentRepositoryGuardError(f"Repository temporary entry {suffix}: {entry.path}")
        entry.removed = True

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
        os.close(self.parent_descriptor)


@contextmanager
def agent_repository_temporary_authority(
    parent_path: Path,
    name: str,
    *,
    create: bool,
) -> Iterator[AgentRepositoryTemporaryDirectoryAuthority]:
    """Open a stable parent/root pair; an absent read-only root must stay absent."""

    _validate_authority_name(name)
    parent_descriptor, parent_identity = _pin_directory_path(parent_path)
    root_descriptor: int | None = None
    try:
        if create:
            try:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o770, dir_fd=parent_descriptor)
            except OSError as exc:
                raise AgentRepositoryGuardError(f"Repository temporary authority cannot be created safely: {parent_path / name}") from exc
        pinned = _pin_child_directory(parent_descriptor, name, missing_ok=not create)
        root_identity: _DirectoryIdentity | None = None
        if pinned is not None:
            root_descriptor, root_identity = pinned
        authority = AgentRepositoryTemporaryDirectoryAuthority(
            parent_path=parent_path,
            name=name,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            descriptor=root_descriptor,
            identity=root_identity,
        )
        try:
            authority.verify()
            try:
                yield authority
            finally:
                authority.verify()
        finally:
            authority.close()
            parent_descriptor = -1
            root_descriptor = None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


class AgentRepositoryMutationGuard:
    """同一 stable lock 上显式区分 existing mutation 与 authority-owned 初始化。"""

    def __init__(
        self,
        *,
        lock_path: Path,
        repository_dir: Path,
        worktrees_dir: Path,
        releases_dir: Path,
        thread_lock: threading.RLock,
        precondition: Callable[[], bool] | None,
    ) -> None:
        self.lock_path = lock_path
        self.repository_dir = repository_dir
        self.worktrees_dir = worktrees_dir
        self.releases_dir = releases_dir
        self.thread_lock = thread_lock
        self.precondition = precondition
        self._thread_state = threading.local()

    @contextmanager
    def existing(self) -> Iterator[None]:
        with self._serialized(precondition=self.precondition):
            root, version_base = self._layout_authorities()
            for path in (root, self.repository_dir, self.repository_dir / ".git", version_base, self.worktrees_dir, self.releases_dir):
                _require_real_directory(path)
            yield

    @contextmanager
    def activation(self, *, precondition: Callable[[], bool] | None) -> Iterator[None]:
        """Use the same lease with an authority that may cross its own activation fence."""

        with self._serialized(precondition=precondition):
            root, version_base = self._layout_authorities()
            for path in (root, self.repository_dir, self.repository_dir / ".git", version_base, self.worktrees_dir, self.releases_dir):
                _require_real_directory(path)
            yield

    @contextmanager
    def initialization(self, *, require_new_repository: bool = False) -> Iterator[None]:
        with self._serialized(precondition=self.precondition):
            root, version_base = self._layout_authorities()
            _require_real_directory(root)
            with _open_real_directory(root) as root_fd:
                if require_new_repository:
                    repository_fd = _open_child_directory(root_fd, self.repository_dir.name)
                    _require_git_directory_state(self.repository_dir / ".git", require_absent=True)
                    if version_base == root or _entry_exists(root_fd, version_base.name):
                        os.close(repository_fd)
                        raise AgentRepositoryGuardError(f"New repository version authority already exists: {version_base}")
                else:
                    repository_fd = _ensure_child_directory(root_fd, self.repository_dir.name)
                os.close(repository_fd)
                if version_base == root:
                    version_fd = os.dup(root_fd)
                else:
                    version_fd = _ensure_child_directory(root_fd, version_base.name)
                try:
                    os.close(_ensure_child_directory(version_fd, self.worktrees_dir.name))
                    os.close(_ensure_child_directory(version_fd, self.releases_dir.name))
                finally:
                    os.close(version_fd)
            if not require_new_repository:
                _require_git_directory_state(self.repository_dir / ".git", require_absent=False)
            yield

    def validate_existing(self) -> None:
        """Validate a read target without creating lock or repository directories."""

        root, version_base = self._layout_authorities()
        for path in (root, self.repository_dir, self.repository_dir / ".git", version_base, self.worktrees_dir, self.releases_dir):
            _require_real_directory(path)

    def _layout_authorities(self) -> tuple[Path, Path]:
        version_base = self.worktrees_dir.parent
        if self.releases_dir.parent != version_base or self.repository_dir.name in {"", ".", ".."}:
            raise AgentRepositoryGuardError("Repository directories do not share one stable authority")
        root = self.repository_dir.parent
        if version_base not in {root, root / version_base.name} or (version_base != root and version_base.parent != root):
            raise AgentRepositoryGuardError("Repository version storage escapes its Agent authority root")
        return root, version_base

    @contextmanager
    def _serialized(self, *, precondition: Callable[[], bool] | None) -> Iterator[None]:
        with self.thread_lock:
            with advisory_lock(self.lock_path, mode="exclusive"):
                depth = int(getattr(self._thread_state, "depth", 0))
                if depth == 0 and precondition is not None and not precondition():
                    raise AgentRepositoryGuardError("Business Agent repository is no longer mutable")
                self._thread_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._thread_state.depth = depth


def _require_real_directory(path: Path) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError as exc:
        raise AgentRepositoryGuardError(f"Business Agent repository authority is missing: {path}") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise AgentRepositoryGuardError(f"Business Agent repository authority is not a real directory: {path}")


@contextmanager
def _open_real_directory(path: Path) -> Iterator[int]:
    descriptor, _identity = _pin_directory_path(path)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _ensure_child_directory(parent_fd: int, name: str) -> int:
    _validate_authority_name(name)
    with suppress(FileExistsError):
        os.mkdir(name, mode=0o770, dir_fd=parent_fd)
    pinned = _pin_child_directory(parent_fd, name, missing_ok=False)
    assert pinned is not None
    return pinned[0]


def _open_child_directory(parent_fd: int, name: str) -> int:
    _validate_authority_name(name)
    pinned = _pin_child_directory(parent_fd, name, missing_ok=False)
    assert pinned is not None
    return pinned[0]


def _entry_exists(parent_fd: int, name: str) -> bool:
    return _child_stat(parent_fd, name) is not None


def _require_git_directory_state(path: Path, *, require_absent: bool) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if require_absent or not stat.S_ISDIR(observed.st_mode):
        raise AgentRepositoryGuardError(f"Repository Git authority is not safely initializable: {path}")


def _pin_directory_path(path: Path) -> tuple[int, _DirectoryIdentity]:
    try:
        before = os.lstat(path)
        if not stat.S_ISDIR(before.st_mode):
            raise AgentRepositoryGuardError(f"Repository authority is not a real directory: {path}")
        descriptor = os.open(path, _directory_open_flags())
    except AgentRepositoryGuardError:
        raise
    except OSError as exc:
        raise AgentRepositoryGuardError(f"Repository authority cannot be opened safely: {path}") from exc
    identity = _DirectoryIdentity.from_stat(before)
    try:
        _verify_directory_path(path, descriptor, identity, label="Repository authority")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _pin_child_directory(
    parent_fd: int,
    name: str,
    *,
    missing_ok: bool,
) -> tuple[int, _DirectoryIdentity] | None:
    _validate_authority_name(name)
    before = _child_stat(parent_fd, name)
    if before is None:
        if missing_ok:
            return None
        raise AgentRepositoryGuardError(f"Repository authority is missing: {name}")
    if not stat.S_ISDIR(before.st_mode):
        raise AgentRepositoryGuardError(f"Repository authority is not a real directory: {name}")
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise AgentRepositoryGuardError(f"Repository authority cannot be opened safely: {name}") from exc
    identity = _DirectoryIdentity.from_stat(before)
    try:
        _verify_child_directory(parent_fd, name, descriptor, identity, label="Repository authority")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _verify_directory_path(
    path: Path,
    descriptor: int,
    identity: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    try:
        path_stat = os.lstat(path)
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise AgentRepositoryGuardError(f"{label} is no longer available: {path}") from exc
    _require_directory_identity(path_stat, descriptor_stat, identity, label=label)


def _verify_child_directory(
    parent_fd: int,
    name: str,
    descriptor: int,
    identity: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    path_stat = _child_stat(parent_fd, name)
    if path_stat is None:
        raise AgentRepositoryGuardError(f"{label} disappeared: {name}")
    try:
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise AgentRepositoryGuardError(f"{label} descriptor is no longer available: {name}") from exc
    _require_directory_identity(path_stat, descriptor_stat, identity, label=label)


def _require_directory_identity(
    path_stat: os.stat_result,
    descriptor_stat: os.stat_result,
    identity: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    if not stat.S_ISDIR(path_stat.st_mode) or not stat.S_ISDIR(descriptor_stat.st_mode):
        raise AgentRepositoryGuardError(f"{label} is not a real directory")
    if _DirectoryIdentity.from_stat(path_stat) != identity or _DirectoryIdentity.from_stat(descriptor_stat) != identity:
        raise AgentRepositoryGuardError(f"{label} was replaced")


def _child_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentRepositoryGuardError(f"Repository authority entry cannot be inspected safely: {name}") from exc


def _clear_directory(descriptor: int, *, preserve_names: frozenset[str] = frozenset()) -> None:
    try:
        names = tuple(os.listdir(descriptor))
    except OSError as exc:
        raise AgentRepositoryGuardError("Repository temporary entry cannot be inspected safely") from exc
    for name in names:
        if name in preserve_names:
            continue
        observed = _child_stat(descriptor, name)
        if observed is None:
            continue
        try:
            if stat.S_ISDIR(observed.st_mode):
                pinned = _pin_child_directory(descriptor, name, missing_ok=False)
                assert pinned is not None
                child_descriptor, child_identity = pinned
                try:
                    _clear_directory(child_descriptor)
                    _verify_child_directory(descriptor, name, child_descriptor, child_identity, label="Repository temporary child")
                finally:
                    os.close(child_descriptor)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except AgentRepositoryGuardError:
            raise
        except OSError as exc:
            raise AgentRepositoryGuardError(f"Repository temporary child cannot be removed safely: {name}") from exc


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


def _validate_authority_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or (os.altsep and os.altsep in name):
        raise AgentRepositoryGuardError(f"Invalid repository authority name: {name!r}")
