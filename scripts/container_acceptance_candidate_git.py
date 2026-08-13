"""候选树的固定 Git authority、临时 index 与 source freshness 捕获。"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from app.agent_testing.source_limits import (
    MAX_SOURCE_COMPONENT_BYTES,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_PATH_BYTES,
    MAX_SOURCE_PATH_DEPTH,
)
from app.runtime.agent_git_environment import GovernedGitEnvironmentError, require_governed_repository
from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_candidate_storage as candidate_storage
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts.container_acceptance_candidate_authority import (
    CandidatePathIdentity,
    CandidateSnapshotError,
    CandidateSnapshotIdentity,
    CandidateSnapshotReservation,
    CandidateSourceIdentity,
)

_GIT_TIMEOUT_SECONDS: Final = 30.0
_MFD_CLOEXEC: Final = 1
_MFD_ALLOW_SEALING: Final = 2
_F_ADD_SEALS: Final = 1033
_F_GET_SEALS: Final = 1034
_MEMFD_SEALS: Final = 0x0001 | 0x0002 | 0x0004 | 0x0008
_MAX_INDEX_BYTES: Final = 64 * 1024 * 1024
_MAX_PATH_SET_BYTES: Final = MAX_SOURCE_FILES * (MAX_SOURCE_PATH_BYTES + 1)
_FORBIDDEN_TRACKED_PATHS: Final = (
    ".obsidian",
    ":(glob)**/.obsidian/**",
    ".venv",
    ":(glob)**/.venv/**",
    "node_modules",
    ":(glob)**/node_modules/**",
)


class CandidateGitAuthority(candidate_storage.GitObjectReader, Protocol):
    def validate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FrozenCandidateIndex:
    descriptor: int
    identity: CandidatePathIdentity
    paths: tuple[str, ...]
    generations: tuple[tuple[str, CandidatePathIdentity], ...]
    directories: tuple[tuple[str, CandidatePathIdentity], ...]
    git_control_sha256: str

    def close(self) -> None:
        os.close(self.descriptor)


def _open_git_output_memfd() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    if create is None:
        raise OSError(errno.ENOSYS, "memfd_create is unavailable")
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = int(create(b"agentgov-candidate-git-output", _MFD_CLOEXEC))
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 0 or identity.st_uid != os.geteuid():
            raise OSError(errno.EPERM, "memfd authority is invalid")
        if fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC == 0:
            raise OSError(errno.EPERM, "memfd close-on-exec authority is invalid")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _open_sealable_index_memfd() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    if create is None:
        raise OSError(errno.ENOSYS, "memfd_create is unavailable")
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = int(create(b"agentgov-candidate-terminal-index", _MFD_CLOEXEC | _MFD_ALLOW_SEALING))
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return descriptor


class FixedCandidateGitAuthority:
    def validate(self) -> None:
        try:
            acceptance_toolchain.validate_execution_tool_authority(commands=("git",))
        except acceptance_toolchain.ToolchainAuthorityError as exc:
            raise CandidateSnapshotError("candidate Git toolchain authority is invalid") from exc

    def run(
        self,
        repository: Path,
        arguments: Sequence[str],
        *,
        index_root: candidate_storage.OpenSnapshotRoot | None = None,
        index_descriptor: int | None = None,
        input_bytes: bytes | None = None,
        max_output_bytes: int = 4096,
    ) -> bytes:
        output_descriptor: int | None = None
        try:
            if index_root is not None and index_descriptor is not None:
                raise CandidateSnapshotError("candidate Git index authority is ambiguous")
            command = acceptance_toolchain.git_argv(repository, *arguments)
            if index_root is not None:
                index_file = _verified_index_file(index_root)
                pass_fds = (index_root.descriptor,)
            elif index_descriptor is not None:
                _verified_frozen_index(index_descriptor)
                index_file = Path(f"/proc/self/fd/{index_descriptor}")
                pass_fds = (index_descriptor,)
            else:
                index_file, pass_fds = None, ()
            environment = acceptance_toolchain.git_environment(index_file=index_file)
            output_descriptor = _open_git_output_memfd()
            result = subprocess.run(
                command,
                cwd=repository,
                env=environment,
                input=input_bytes,
                stdout=output_descriptor,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
                pass_fds=pass_fds,
            )
            if index_root is not None:
                _verified_index_file(index_root)
            if index_descriptor is not None:
                _verified_frozen_index(index_descriptor)
            if result.returncode or os.fstat(output_descriptor).st_size > max_output_bytes:
                raise CandidateSnapshotError("candidate Git query did not satisfy its bounded contract")
            os.lseek(output_descriptor, 0, os.SEEK_SET)
            return os.read(output_descriptor, max_output_bytes + 1)
        except CandidateSnapshotError:
            raise
        except (OSError, subprocess.TimeoutExpired, acceptance_toolchain.ToolchainAuthorityError) as exc:
            raise CandidateSnapshotError("candidate Git query could not be completed") from exc
        finally:
            if output_descriptor is not None:
                os.close(output_descriptor)


def validated_git_authority(authority: CandidateGitAuthority | None) -> CandidateGitAuthority:
    selected = authority or FixedCandidateGitAuthority()
    selected.validate()
    return selected


def capture_candidate_source(
    reservation: CandidateSnapshotReservation,
    index_root: candidate_storage.OpenSnapshotRoot,
    authority: CandidateGitAuthority,
) -> CandidateSourceIdentity:
    first = _capture_candidate_source_once(reservation, index_root, authority)
    second = _capture_candidate_source_once(reservation, index_root, authority)
    if second != first:
        raise CandidateSnapshotError("candidate source changed while its identity was captured")
    return first


def freeze_candidate_index(
    repository: Path,
    index_root: candidate_storage.OpenSnapshotRoot,
    authority: CandidateGitAuthority,
) -> FrozenCandidateIndex:
    safe_repository = _validated_repository(repository, authority)
    first_paths = _indexed_paths(safe_repository, authority, index_root=index_root)
    first_generations, first_directories = _source_generations(safe_repository, first_paths)
    first_control = _git_control_sha256(safe_repository, authority)
    descriptor = _sealed_index_copy(index_root)
    try:
        frozen_paths = _indexed_paths(safe_repository, authority, index_descriptor=descriptor)
        generations, directories = _source_generations(safe_repository, frozen_paths)
        control = _git_control_sha256(safe_repository, authority)
        if (frozen_paths, generations, directories, control) != (
            first_paths,
            first_generations,
            first_directories,
            first_control,
        ):
            raise CandidateSnapshotError("candidate source changed while terminal freshness was frozen")
        return FrozenCandidateIndex(
            descriptor,
            _verified_frozen_index(descriptor),
            frozen_paths,
            generations,
            directories,
            control,
        )
    except BaseException:
        os.close(descriptor)
        raise


def require_frozen_candidate_current(
    repository: Path,
    witness: FrozenCandidateIndex,
    authority: CandidateGitAuthority,
) -> None:
    if _verified_frozen_index(witness.descriptor) != witness.identity:
        raise CandidateSnapshotError("candidate terminal index authority changed")
    safe_repository = _validated_repository(repository, authority)
    paths = _indexed_paths(safe_repository, authority, index_descriptor=witness.descriptor)
    generations, directories = _source_generations(safe_repository, paths)
    control = _git_control_sha256(safe_repository, authority)
    if (paths, generations, directories, control) != (
        witness.paths,
        witness.generations,
        witness.directories,
        witness.git_control_sha256,
    ):
        raise CandidateSnapshotError("candidate source generation changed during cleanup")
    changed = authority.run(
        safe_repository,
        ("diff-files", "--name-only", "-z", "--", ".", ":(exclude).obsidian", ":(exclude).obsidian/**"),
        index_descriptor=witness.descriptor,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    untracked = authority.run(
        safe_repository,
        ("ls-files", "--others", "--exclude-standard", "-z", "--", ".", ":(exclude).obsidian", ":(exclude).obsidian/**"),
        index_descriptor=witness.descriptor,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    if _validate_path_set(changed) or _validate_path_set(untracked):
        raise CandidateSnapshotError("candidate Git tree changed during cleanup")


def source_loaded_file_sha256(repository: Path, relative_path: str) -> str:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CandidateSnapshotError("candidate loaded source path is invalid")
    absolute = repository.joinpath(*relative.parts)
    parent = candidate_storage._open_real_directory(absolute.parent)
    descriptor: int | None = None
    try:
        identity = candidate_storage.PathIdentity.from_stat(os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False))
        if (
            not stat.S_ISREG(identity.mode)
            or identity.uid != os.geteuid()
            or identity.links != 1
            or identity.size > MAX_SOURCE_FILE_BYTES
            or identity.mode & stat.S_IWOTH
        ):
            raise CandidateSnapshotError("candidate loaded source authority is invalid")
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent.descriptor,
        )
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise CandidateSnapshotError("candidate loaded source was replaced")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if candidate_storage.PathIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise CandidateSnapshotError("candidate loaded source changed while verified")
        linked = candidate_storage.PathIdentity.from_stat(os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False))
        if linked != identity:
            raise CandidateSnapshotError("candidate loaded source path changed")
        return digest.hexdigest()
    except OSError as exc:
        raise CandidateSnapshotError("candidate loaded source is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent.descriptor)


def snapshot_loaded_relative(snapshot: CandidateSnapshotIdentity, loaded_file: Path) -> str:
    repository = snapshot.repository_root.absolute()
    candidate = loaded_file.absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CandidateSnapshotError("acceptance loaded source path is unavailable") from exc
    try:
        relative = candidate.relative_to(repository)
    except ValueError:
        relative = _proc_fd_loaded_relative(snapshot, candidate, resolved)
    else:
        if resolved != candidate:
            raise CandidateSnapshotError("acceptance loaded source used an unauthorized symlink")
    return relative.as_posix()


def _proc_fd_loaded_relative(snapshot: CandidateSnapshotIdentity, candidate: Path, resolved: Path) -> PurePosixPath:
    parts = candidate.parts
    valid_prefix = len(parts) >= 6 and parts[1:4] == ("proc", "self", "fd")
    descriptor_text = parts[4] if valid_prefix else ""
    if not descriptor_text.isascii() or not descriptor_text.isdigit() or str(int(descriptor_text)) != descriptor_text:
        raise CandidateSnapshotError("acceptance source was not loaded from the candidate snapshot")
    relative = PurePosixPath(*parts[5:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CandidateSnapshotError("acceptance loaded source fd path is invalid")
    try:
        before = CandidatePathIdentity.from_stat(os.fstat(int(descriptor_text)))
        expected = snapshot.repository_root.joinpath(*relative.parts)
        after = CandidatePathIdentity.from_stat(os.fstat(int(descriptor_text)))
    except OSError as exc:
        raise CandidateSnapshotError("acceptance loaded source fd authority is unavailable") from exc
    if before != snapshot.repository_identity or after != before or resolved != expected:
        raise CandidateSnapshotError("acceptance loaded source fd is not the frozen candidate repository")
    return relative


@contextmanager
def prepared_candidate_index(snapshot: CandidateSnapshotIdentity) -> Iterator[candidate_storage.OpenSnapshotRoot]:
    parent = candidate_storage._open_real_directory(snapshot.parent)
    root_fd: int | None = None
    runtime_fd: int | None = None
    try:
        if not candidate_storage.same_node(parent.chain[-1], snapshot.parent_identity):
            raise CandidateSnapshotError("candidate snapshot parent changed before source freshness capture")
        root_fd = candidate_storage._open_child_directory(parent.descriptor, snapshot.root.name, snapshot.root_identity)
        runtime = candidate_storage.PathIdentity.from_stat(os.stat(snapshot.runtime_root.name, dir_fd=root_fd, follow_symlinks=False))
        if not candidate_storage.same_node(runtime, snapshot.runtime_identity) or runtime.uid != os.geteuid() or stat.S_IMODE(runtime.mode) != 0o700:
            raise CandidateSnapshotError("candidate runtime authority changed before source freshness capture")
        runtime_fd = os.open(
            snapshot.runtime_root.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        if not candidate_storage.same_node(candidate_storage.PathIdentity.from_stat(os.fstat(runtime_fd)), runtime):
            raise CandidateSnapshotError("candidate runtime was replaced before source freshness capture")
        opened = candidate_storage.OpenSnapshotRoot(
            candidate_storage.SnapshotRoot(snapshot.root, snapshot.root_identity, snapshot.runtime_root, runtime),
            runtime_fd,
        )
        try:
            yield opened
        finally:
            _cleanup_temporary_index(opened)
    except OSError as exc:
        raise CandidateSnapshotError("candidate runtime index authority is unavailable") from exc
    finally:
        if runtime_fd is not None:
            os.close(runtime_fd)
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent.descriptor)


def staged_tree_sha(repository: Path, *, git_authority: CandidateGitAuthority | None = None) -> str:
    authority = validated_git_authority(git_authority)
    safe_repository = _validated_repository(repository, authority)
    return _validated_object_id(
        _git_text(authority.run(safe_repository, ("write-tree",), max_output_bytes=128)),
        "staged Git tree identity is invalid",
    )


def revision_tree_sha(
    repository: Path,
    revision: str = "HEAD",
    *,
    git_authority: CandidateGitAuthority | None = None,
) -> str:
    if not revision or any(ord(character) < 32 for character in revision):
        raise CandidateSnapshotError("commit Git tree revision is invalid")
    authority = validated_git_authority(git_authority)
    safe_repository = _validated_repository(repository, authority)
    raw = authority.run(safe_repository, ("rev-parse", "--verify", f"{revision}^{{tree}}"), max_output_bytes=128)
    return _validated_object_id(_git_text(raw), "commit Git tree identity is invalid")


def _capture_candidate_source_once(
    reservation: CandidateSnapshotReservation,
    index_root: candidate_storage.OpenSnapshotRoot,
    authority: CandidateGitAuthority,
) -> CandidateSourceIdentity:
    repository = _validated_repository(reservation.repository_root, authority)
    try:
        repository_before = candidate_storage.real_directory_identity(repository)
        env = candidate_storage.capture_regular_file(
            reservation.selected_env_file,
            allow_public_read=reservation.allow_public_env_read,
        )
        tree_sha = _candidate_tree(repository, authority, index_root)
        repository_after = candidate_storage.real_directory_identity(repository)
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate source identity could not be captured safely") from exc
    if repository_after != repository_before:
        raise CandidateSnapshotError("candidate repository root changed while its tree was captured")
    return CandidateSourceIdentity(
        repository,
        repository_after,
        reservation.selected_env_file,
        env.identity,
        tree_sha,
        env.sha256,
        reservation.allow_public_env_read,
    )


def _candidate_tree(
    repository: Path,
    authority: CandidateGitAuthority,
    index_root: candidate_storage.OpenSnapshotRoot,
) -> str:
    authority.run(repository, ("rev-parse", "--verify", "HEAD^{commit}"), index_root=index_root, max_output_bytes=128)
    authority.run(repository, ("read-tree", "HEAD"), index_root=index_root)
    _reject_candidate_filters(repository, index_root, authority)
    authority.run(
        repository,
        ("add", "-A", "--", ".", ":(exclude).obsidian", ":(exclude).obsidian/**"),
        index_root=index_root,
    )
    _reject_tracked_private_paths(repository, index_root, authority)
    tree = _git_text(authority.run(repository, ("write-tree",), index_root=index_root, max_output_bytes=128))
    kind = _git_text(authority.run(repository, ("cat-file", "-t", tree), index_root=index_root, max_output_bytes=32))
    if kind != "tree":
        raise CandidateSnapshotError("candidate Git tree identity is invalid")
    return _validated_object_id(tree, "candidate Git tree identity is invalid")


def _reject_candidate_filters(
    repository: Path,
    index_root: candidate_storage.OpenSnapshotRoot,
    authority: CandidateGitAuthority,
) -> None:
    paths = authority.run(
        repository,
        ("ls-files", "-co", "--exclude-standard", "-z"),
        index_root=index_root,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    _validate_path_set(paths)
    if not paths:
        return
    attributes = authority.run(
        repository,
        ("check-attr", "-z", "--stdin", "filter"),
        index_root=index_root,
        input_bytes=paths,
        max_output_bytes=_MAX_PATH_SET_BYTES * 2,
    )
    fields = attributes.split(b"\0")
    if fields[-1:] == [b""]:
        fields.pop()
    if len(fields) % 3 or any(fields[index + 2] not in {b"unspecified", b"unset"} for index in range(0, len(fields), 3)):
        raise CandidateSnapshotError("candidate Git clean filter is forbidden")


def _reject_tracked_private_paths(
    repository: Path,
    index_root: candidate_storage.OpenSnapshotRoot,
    authority: CandidateGitAuthority,
) -> None:
    ignored = authority.run(
        repository,
        ("ls-files", "-ci", "--exclude-standard", "-z"),
        index_root=index_root,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    explicit = authority.run(
        repository,
        ("ls-files", "-z", "--", *_FORBIDDEN_TRACKED_PATHS),
        index_root=index_root,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    if _validate_path_set(ignored) or _validate_path_set(explicit):
        raise CandidateSnapshotError("candidate tree contains tracked private runtime assets")


def _validate_path_set(raw: bytes) -> tuple[bytes, ...]:
    paths = tuple(path for path in raw.split(b"\0") if path)
    if len(paths) > MAX_SOURCE_FILES or len(set(paths)) != len(paths) or any(len(path) > MAX_SOURCE_PATH_BYTES for path in paths):
        raise CandidateSnapshotError("candidate Git path set exceeds its bounded contract")
    return paths


def _indexed_paths(
    repository: Path,
    authority: CandidateGitAuthority,
    *,
    index_root: candidate_storage.OpenSnapshotRoot | None = None,
    index_descriptor: int | None = None,
) -> tuple[str, ...]:
    raw = authority.run(
        repository,
        ("ls-files", "-z", "--", ".", ":(exclude).obsidian", ":(exclude).obsidian/**"),
        index_root=index_root,
        index_descriptor=index_descriptor,
        max_output_bytes=_MAX_PATH_SET_BYTES,
    )
    paths = tuple(_validated_candidate_path(path) for path in _validate_path_set(raw))
    if tuple(sorted(paths, key=lambda item: item.encode())) != paths:
        raise CandidateSnapshotError("candidate terminal index path set is invalid")
    return paths


def _validated_candidate_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateSnapshotError("candidate terminal index path is invalid") from exc
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or len(path.parts) > MAX_SOURCE_PATH_DEPTH
        or any(part in {"", ".", ".."} or len(part.encode()) > MAX_SOURCE_COMPONENT_BYTES for part in path.parts)
    ):
        raise CandidateSnapshotError("candidate terminal index path is invalid")
    return value


def _source_generations(
    repository: Path,
    paths: tuple[str, ...],
) -> tuple[tuple[tuple[str, CandidatePathIdentity], ...], tuple[tuple[str, CandidatePathIdentity], ...]]:
    files: list[tuple[str, CandidatePathIdentity]] = []
    directory_paths = {PurePosixPath(".")}
    for value in paths:
        relative = PurePosixPath(value)
        try:
            identity = _source_file_identity(repository.joinpath(*relative.parts))
        except candidate_storage.CandidateStorageError as exc:
            raise CandidateSnapshotError("candidate source generation is unavailable") from exc
        files.append((value, identity))
        directory_paths.update(PurePosixPath(*relative.parts[:depth]) for depth in range(1, len(relative.parts)))
    directories: list[tuple[str, CandidatePathIdentity]] = []
    try:
        for relative in sorted(directory_paths, key=lambda item: item.as_posix().encode()):
            path = repository if relative == PurePosixPath(".") else repository.joinpath(*relative.parts)
            directories.append((relative.as_posix(), candidate_storage.real_directory_identity(path)))
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate source directory generation is unavailable") from exc
    return tuple(files), tuple(directories)


def _source_file_identity(path: Path) -> CandidatePathIdentity:
    parent = candidate_storage._open_real_directory(path.absolute().parent)
    descriptor: int | None = None
    try:
        linked = CandidatePathIdentity.from_stat(os.stat(path.name, dir_fd=parent.descriptor, follow_symlinks=False))
        if (
            not stat.S_ISREG(linked.mode)
            or linked.links != 1
            or linked.uid != os.geteuid()
            or linked.size > MAX_SOURCE_FILE_BYTES
            or linked.mode & stat.S_IWOTH
        ):
            raise candidate_storage.CandidateStorageError("candidate source file generation is invalid")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent.descriptor)
        if CandidatePathIdentity.from_stat(os.fstat(descriptor)) != linked:
            raise candidate_storage.CandidateStorageError("candidate source file generation changed")
        return linked
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate source file generation is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent.descriptor)


def _git_control_sha256(repository: Path, authority: CandidateGitAuthority) -> str:
    try:
        scope = require_governed_repository(repository)
        head_commit = _git_text(authority.run(repository, ("rev-parse", "--verify", "HEAD^{commit}"), max_output_bytes=128))
        head_ref = _git_text(authority.run(repository, ("rev-parse", "--symbolic-full-name", "HEAD"), max_output_bytes=MAX_SOURCE_PATH_BYTES))
        control_files = {
            repository / ".git",
            scope.git_dir / "HEAD",
            scope.git_dir / "commondir",
            scope.git_dir / "config.worktree",
            scope.git_dir / "gitdir",
            scope.git_dir / "info/sparse-checkout",
            scope.common_git_dir / "config",
            scope.common_git_dir / "packed-refs",
            scope.common_git_dir / "info/attributes",
            scope.common_git_dir / "info/exclude",
        }
        if head_ref.startswith("refs/") and ".." not in PurePosixPath(head_ref).parts:
            control_files.add(scope.common_git_dir.joinpath(*PurePosixPath(head_ref).parts))
        records = [_control_file_record(path) for path in sorted(control_files, key=lambda item: str(item).encode())]
        directories = tuple(_identity_payload(candidate_storage.real_directory_identity(path)) for path in (repository, scope.git_dir, scope.common_git_dir))
    except (GovernedGitEnvironmentError, candidate_storage.CandidateStorageError) as exc:
        raise CandidateSnapshotError("candidate Git control authority is unavailable") from exc
    payload = {"head_commit": head_commit, "head_ref": head_ref, "directories": directories, "files": records}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _control_file_record(path: Path) -> tuple[object, ...]:
    try:
        identity = candidate_storage.lstat_identity(path)
    except candidate_storage.CandidateStorageError:
        return (str(path), "absent")
    if stat.S_ISDIR(identity.mode):
        return (str(path), "directory", *_identity_payload(candidate_storage.real_directory_identity(path)))
    file_identity = _source_file_identity(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if CandidatePathIdentity.from_stat(os.fstat(descriptor)) != file_identity:
            raise candidate_storage.CandidateStorageError("candidate Git control file changed")
    finally:
        os.close(descriptor)
    return (str(path), "file", *_identity_payload(file_identity), digest.hexdigest())


def _identity_payload(identity: CandidatePathIdentity) -> tuple[int, ...]:
    return (
        identity.device,
        identity.inode,
        identity.mode,
        identity.links,
        identity.size,
        identity.uid,
        identity.gid,
        identity.modified_ns,
        identity.changed_ns,
    )


def _sealed_index_copy(root: candidate_storage.OpenSnapshotRoot) -> int:
    _verified_index_file(root)
    source: int | None = None
    descriptor: int | None = None
    completed = False
    try:
        linked = CandidatePathIdentity.from_stat(os.stat("index", dir_fd=root.descriptor, follow_symlinks=False))
        if not stat.S_ISREG(linked.mode) or linked.uid != os.geteuid() or linked.links != 1 or linked.size > _MAX_INDEX_BYTES:
            raise CandidateSnapshotError("candidate terminal index authority is invalid")
        source = os.open("index", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root.descriptor)
        if CandidatePathIdentity.from_stat(os.fstat(source)) != linked:
            raise CandidateSnapshotError("candidate terminal index was replaced")
        descriptor = _open_sealable_index_memfd()
        while chunk := os.read(source, 1024 * 1024):
            offset = 0
            while offset < len(chunk):
                offset += os.write(descriptor, chunk[offset:])
        if CandidatePathIdentity.from_stat(os.fstat(source)) != linked:
            raise CandidateSnapshotError("candidate terminal index changed while copied")
        os.fchmod(descriptor, 0o400)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _MEMFD_SEALS)
        _verified_frozen_index(descriptor)
        completed = True
        return descriptor
    except OSError as exc:
        raise CandidateSnapshotError("candidate terminal index could not be frozen") from exc
    finally:
        if source is not None:
            os.close(source)
        if descriptor is not None and not completed:
            os.close(descriptor)


def _verified_frozen_index(descriptor: int) -> CandidatePathIdentity:
    try:
        identity = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        seals = fcntl.fcntl(descriptor, _F_GET_SEALS)
    except OSError as exc:
        raise CandidateSnapshotError("candidate terminal index authority is unavailable") from exc
    if (
        not stat.S_ISREG(identity.mode)
        or stat.S_IMODE(identity.mode) != 0o400
        or identity.links != 0
        or identity.uid != os.geteuid()
        or identity.size > _MAX_INDEX_BYTES
        or seals != _MEMFD_SEALS
    ):
        raise CandidateSnapshotError("candidate terminal index authority is invalid")
    return identity


def _verified_index_file(root: candidate_storage.OpenSnapshotRoot) -> Path:
    opened = CandidatePathIdentity.from_stat(os.fstat(root.descriptor))
    try:
        linked = candidate_storage.lstat_identity(root.authority.root)
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate temporary index root is unavailable") from exc
    if not candidate_storage.same_node(opened, root.authority.root_identity) or not candidate_storage.same_node(linked, opened):
        raise CandidateSnapshotError("candidate temporary index root was replaced")
    return Path(f"/proc/self/fd/{root.descriptor}/index")


def _cleanup_temporary_index(root: candidate_storage.OpenSnapshotRoot) -> None:
    for name in ("index.lock", "index"):
        try:
            identity = candidate_storage.PathIdentity.from_stat(os.stat(name, dir_fd=root.descriptor, follow_symlinks=False))
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(identity.mode) or identity.uid != os.geteuid() or identity.links != 1:
            raise CandidateSnapshotError("candidate temporary index cleanup authority is invalid")
        try:
            os.unlink(name, dir_fd=root.descriptor)
        except OSError as exc:
            raise CandidateSnapshotError("candidate temporary index could not be cleaned") from exc
    _verified_index_file(root)


def _validated_repository(repository: Path, authority: CandidateGitAuthority) -> Path:
    candidate = repository.absolute()
    try:
        scope = require_governed_repository(candidate)
        identity = candidate_storage.real_directory_identity(candidate)
    except (GovernedGitEnvironmentError, candidate_storage.CandidateStorageError) as exc:
        raise CandidateSnapshotError("candidate Git repository authority is invalid") from exc
    if scope.work_tree != candidate or identity.uid != os.geteuid():
        raise CandidateSnapshotError("candidate Git worktree authority is invalid")
    root = _git_text(authority.run(candidate, ("rev-parse", "--show-toplevel"), max_output_bytes=MAX_SOURCE_PATH_BYTES))
    object_format = _git_text(authority.run(candidate, ("rev-parse", "--show-object-format"), max_output_bytes=32))
    if Path(root) != candidate or object_format != "sha1":
        raise CandidateSnapshotError("candidate Git worktree authority is invalid")
    return candidate


def _git_text(raw: bytes) -> str:
    try:
        value = raw.decode().rstrip("\n")
    except UnicodeDecodeError as exc:
        raise CandidateSnapshotError("candidate Git query returned invalid text") from exc
    if not value or "\n" in value or "\r" in value:
        raise CandidateSnapshotError("candidate Git query returned an ambiguous value")
    return value


def _validated_object_id(value: str, message: str) -> str:
    if candidate_authority.OBJECT_ID.fullmatch(value) is None:
        raise CandidateSnapshotError(message)
    return value
