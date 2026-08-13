"""使用 root-owned Python 首跳并以 fd 执行仓库 venv 的最小 bootstrap。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypedDict, cast

_INJECTED_REPOSITORY_ROOT = globals().get("_ACTUAL_REPOSITORY_ROOT")
_INJECTED_REPOSITORY_FD = globals().get("_ACTUAL_REPOSITORY_FD")
_INJECTED_REPOSITORY_IDENTITY = globals().get("_ACTUAL_REPOSITORY_IDENTITY")
REPO_ROOT: Final = (
    Path(_INJECTED_REPOSITORY_ROOT)
    if isinstance(_INJECTED_REPOSITORY_ROOT, str) and Path(_INJECTED_REPOSITORY_ROOT).is_absolute()
    else Path(__file__).resolve().parents[1]
)
VENV_PYTHON: Final = REPO_ROOT / ".venv/bin/python"
TOOLCHAIN_PATH: Final = REPO_ROOT / "scripts/container_acceptance_toolchain.py"
IMPORT_AUTHORITY_PATH: Final = REPO_ROOT / "scripts/container_acceptance_import_authority.py"
BOOTSTRAP_PYTHON_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_PYTHON_SHA256"
BOOTSTRAP_TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_TOOLCHAIN_SHA256"
BOOTSTRAP_IMPORT_AUTHORITY_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_IMPORT_AUTHORITY_SHA256"
BOOTSTRAP_STAGE_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_STAGE"
LOADED_BOOTSTRAP_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOADED_BOOTSTRAP_SHA256"
SNAPSHOT_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT"
PREPARED_CANDIDATE_ENV: Final = "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY"
PREPARED_RECEIPT_ENV: Final = "AGENT_GOV_PREPARED_RECEIPT_AUTHORITY"
TOOLCHAIN_EVIDENCE_ENV: Final = "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE"
TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_SHA256"
PYTHON_AUTHORITY_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY"
PYTHON_AUTHORITY_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY_SHA256"
PYTHON_TOOLCHAIN_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_TOOLCHAIN_SHA256"
LOCK_FD_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOCK_FD"
LOCK_COOKIE_ENV: Final = "AGENT_GOV_ACCEPTANCE_LOCK_COOKIE"
REEXEC_STAGE_ENV: Final = "AGENT_GOV_ACCEPTANCE_REEXEC_STAGE"
REEXEC_STAGE_VALUE: Final = "locked-snapshot-v1"
SIGNAL_HANDOFF_ENV: Final = "AGENT_GOV_ACCEPTANCE_SIGNAL_HANDOFF"
SIGNAL_HANDOFF_VALUE: Final = "blocked-v1"
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_INTERNAL_AUTHORITY_BYTES: Final = 64 * 1024
_MAX_TOOLCHAIN_BYTES: Final = 1024 * 1024
_PASSTHROUGH_KEYS: Final = frozenset(
    {
        "ALL_PROXY",
        "COMPOSE_ENV_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LC_ALL",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class BootstrapAuthorityError(RuntimeError):
    """仓库 venv 首跳 authority 无效。"""


class ManagedPythonAuthorityRecord(TypedDict):
    command: str
    invocation_path: str
    resolved_path: str
    sha256: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    invocation_device: int
    invocation_inode: int
    invocation_mode: int
    invocation_uid: int
    invocation_gid: int
    invocation_mtime_ns: int
    invocation_ctime_ns: int
    leaf_link: str | None
    ancestor_authority_sha256: str


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


def _open_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            identity = os.fstat(child)
            linked = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not _same_identity(identity, linked) or identity.st_uid not in {0, os.geteuid()} or identity.st_mode & stat.S_IWOTH:
                os.close(child)
                raise BootstrapAuthorityError("venv Python ancestor authority is invalid")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _initial_repository_descriptor() -> int:
    if (
        type(_INJECTED_REPOSITORY_FD) is not int
        or _INJECTED_REPOSITORY_FD < 3
        or not isinstance(_INJECTED_REPOSITORY_IDENTITY, tuple)
        or len(_INJECTED_REPOSITORY_IDENTITY) != 8
    ):
        raise BootstrapAuthorityError("initial repository root authority is missing")
    reopened = _open_directory(REPO_ROOT)
    try:
        current = os.fstat(_INJECTED_REPOSITORY_FD)
        if not stat.S_ISDIR(current.st_mode) or not _same_identity(current, os.fstat(reopened)):
            raise BootstrapAuthorityError("initial repository root authority drifted")
        if _stat_identity(current) != _INJECTED_REPOSITORY_IDENTITY:
            raise BootstrapAuthorityError("initial repository root identity is invalid")
        return _INJECTED_REPOSITORY_FD
    finally:
        os.close(reopened)


def _open_child_directory(parent_fd: int, leaf: str) -> int:
    descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    identity = os.fstat(descriptor)
    linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(identity.st_mode) or not _same_identity(identity, linked) or identity.st_uid != os.geteuid() or identity.st_mode & stat.S_IWOTH:
        os.close(descriptor)
        raise BootstrapAuthorityError("snapshot directory authority is invalid")
    return descriptor


def _linked_identity(directory_fd: int, leaf: str, expected: os.stat_result) -> None:
    linked = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_identity(expected, linked):
        raise BootstrapAuthorityError("venv Python path drifted")


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


def _open_venv_python(repository_fd: int | None = None) -> tuple[int, str]:
    invocation_parent = _open_relative_directory(repository_fd, (".venv", "bin")) if repository_fd is not None else _open_directory(VENV_PYTHON.parent)
    target_parent: int | None = None
    descriptor: int | None = None
    try:
        link = os.stat(VENV_PYTHON.name, dir_fd=invocation_parent, follow_symlinks=False)
        if not stat.S_ISLNK(link.st_mode) or link.st_uid not in {0, os.geteuid()}:
            raise BootstrapAuthorityError("venv Python invocation link is invalid")
        target = os.readlink(VENV_PYTHON.name, dir_fd=invocation_parent)
        _linked_identity(invocation_parent, VENV_PYTHON.name, link)
        resolved = Path(os.path.abspath(VENV_PYTHON.parent / target))
        target_parent = _open_directory(resolved.parent)
        descriptor = os.open(resolved.name, _FILE_FLAGS, dir_fd=target_parent)
        identity = os.fstat(descriptor)
        linked = os.stat(resolved.name, dir_fd=target_parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(identity.st_mode)
            or not _same_identity(identity, linked)
            or identity.st_uid not in {0, os.geteuid()}
            or identity.st_mode & stat.S_IWOTH
            or not identity.st_mode & 0o111
        ):
            raise BootstrapAuthorityError("venv Python resolved ELF authority is invalid")
        digest = hashlib.sha256()
        prefix = b""
        while chunk := os.read(descriptor, 1024 * 1024):
            if not prefix:
                prefix = chunk[:4]
            digest.update(chunk)
        if prefix != b"\x7fELF" or not _same_identity(identity, os.fstat(descriptor)):
            raise BootstrapAuthorityError("venv Python ELF changed while hashing")
        _linked_identity(target_parent, resolved.name, identity)
        _linked_identity(invocation_parent, VENV_PYTHON.name, link)
        return descriptor, digest.hexdigest()
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        if target_parent is not None:
            os.close(target_parent)
        os.close(invocation_parent)


def _open_bootstrap_source(path: Path, repository_fd: int | None = None) -> tuple[int, str]:
    if repository_fd is None:
        parent = _open_directory(path.parent)
    else:
        try:
            relative = path.relative_to(REPO_ROOT)
        except ValueError as exc:
            raise BootstrapAuthorityError("bootstrap source escaped its repository root") from exc
        parent = _open_relative_directory(repository_fd, tuple(relative.parts[:-1]))
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, _FILE_FLAGS, dir_fd=parent)
        identity = os.fstat(descriptor)
        linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(identity.st_mode)
            or not _same_identity(identity, linked)
            or identity.st_uid not in {0, os.geteuid()}
            or identity.st_mode & stat.S_IWOTH
            or identity.st_size > _MAX_TOOLCHAIN_BYTES
        ):
            raise BootstrapAuthorityError("bootstrap source authority is invalid")
        digest = hashlib.sha256()
        remaining = _MAX_TOOLCHAIN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0 or not _same_identity(identity, os.fstat(descriptor)):
            raise BootstrapAuthorityError("bootstrap source changed while hashing")
        _linked_identity(parent, path.name, identity)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, digest.hexdigest()
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(parent)


def _open_toolchain_source(repository_fd: int | None = None) -> tuple[int, str]:
    return _open_bootstrap_source(TOOLCHAIN_PATH, repository_fd)


def _minimal_env(
    environ: Mapping[str, str],
    python_sha256: str,
    toolchain_sha256: str,
    import_authority_sha256: str,
) -> dict[str, str]:
    loaded = (globals().get("__agentgov_loaded_sha256__"), globals().get("__agentgov_system_python_sha256__"))
    if any(not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef") for value in loaded):
        raise BootstrapAuthorityError("bootstrap actual-loaded authority is invalid")
    child = {
        key: value
        for key, value in environ.items()
        if key in _PASSTHROUGH_KEYS
        and isinstance(value, str)
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
        and len(value.encode()) <= 32 * 1024
    }
    child["LC_ALL"] = "C.UTF-8"
    child[BOOTSTRAP_PYTHON_SHA256_ENV] = python_sha256
    child[BOOTSTRAP_TOOLCHAIN_SHA256_ENV] = toolchain_sha256
    child[BOOTSTRAP_IMPORT_AUTHORITY_SHA256_ENV] = import_authority_sha256
    child[BOOTSTRAP_STAGE_ENV] = str(loaded[1])
    child[LOADED_BOOTSTRAP_SHA256_ENV] = str(loaded[0])
    return child


def launch(environ: Mapping[str, str]) -> None:
    repository_descriptor = _initial_repository_descriptor()
    python_descriptor, python_sha256 = _open_venv_python(repository_descriptor)
    toolchain_descriptor: int | None = None
    import_descriptor: int | None = None
    try:
        toolchain_descriptor, toolchain_sha256 = _open_toolchain_source(repository_descriptor)
        import_descriptor, import_sha256 = _open_bootstrap_source(IMPORT_AUTHORITY_PATH, repository_descriptor)
        os.set_inheritable(repository_descriptor, True)
        os.set_inheritable(python_descriptor, True)
        os.set_inheritable(toolchain_descriptor, True)
        os.set_inheritable(import_descriptor, True)
        os.execve(
            python_descriptor,
            (
                str(VENV_PYTHON),
                "-I",
                "-P",
                "-S",
                "-X",
                "pycache_prefix=/dev/null",
                f"/proc/self/fd/{import_descriptor}",
                str(repository_descriptor),
                str(import_descriptor),
                str(toolchain_descriptor),
                str(IMPORT_AUTHORITY_PATH),
                str(TOOLCHAIN_PATH),
                str(REPO_ROOT),
                "launch",
                *sys.argv[2:],
            ),
            _minimal_env(environ, python_sha256, toolchain_sha256, import_sha256),
        )
    finally:
        if import_descriptor is not None:
            os.close(import_descriptor)
        if toolchain_descriptor is not None:
            os.close(toolchain_descriptor)
        os.close(python_descriptor)
        os.close(repository_descriptor)


def _managed_python_record(environ: Mapping[str, str]) -> ManagedPythonAuthorityRecord:
    raw = environ.get(PYTHON_AUTHORITY_ENV, "")
    digest = environ.get(PYTHON_AUTHORITY_SHA256_ENV, "")
    if not raw or len(raw.encode()) > 32 * 1024:
        raise BootstrapAuthorityError("managed Python authority is invalid")
    try:
        record = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BootstrapAuthorityError("managed Python authority is invalid") from exc
    canonical = json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    required = {
        "command",
        "invocation_path",
        "resolved_path",
        "sha256",
        "device",
        "inode",
        "mode",
        "uid",
        "gid",
        "size",
        "mtime_ns",
        "ctime_ns",
        "invocation_device",
        "invocation_inode",
        "invocation_mode",
        "invocation_uid",
        "invocation_gid",
        "invocation_mtime_ns",
        "invocation_ctime_ns",
        "leaf_link",
        "ancestor_authority_sha256",
    }
    if not isinstance(record, dict) or set(record) != required or hashlib.sha256(canonical).hexdigest() != digest:
        raise BootstrapAuthorityError("managed Python authority is invalid")
    return cast(ManagedPythonAuthorityRecord, record)


def _record_identity(record: Mapping[str, object], prefix: str = "") -> tuple[object, ...]:
    return tuple(record.get(f"{prefix}{key}") for key in ("device", "inode", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"))


def _invocation_record_identity(record: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(record.get(f"invocation_{key}") for key in ("device", "inode", "mode", "uid", "gid", "mtime_ns", "ctime_ns"))


def _stat_identity(identity: os.stat_result) -> tuple[int, ...]:
    return (
        identity.st_dev,
        identity.st_ino,
        stat.S_IMODE(identity.st_mode),
        identity.st_uid,
        identity.st_gid,
        identity.st_size,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    )


def _open_managed_python(record: Mapping[str, object]) -> int:
    invocation = Path(str(record.get("invocation_path")))
    resolved = Path(str(record.get("resolved_path")))
    if record.get("command") != "python" or not invocation.is_absolute() or not resolved.is_absolute():
        raise BootstrapAuthorityError("managed Python authority is invalid")
    invocation_parent = _open_directory(invocation.parent)
    resolved_parent = _open_directory(resolved.parent)
    descriptor: int | None = None
    try:
        leaf = os.stat(invocation.name, dir_fd=invocation_parent, follow_symlinks=False)
        link = os.readlink(invocation.name, dir_fd=invocation_parent) if stat.S_ISLNK(leaf.st_mode) else None
        expected_resolved = Path(os.path.abspath(invocation.parent / link)) if link is not None else invocation
        descriptor = os.open(resolved.name, _FILE_FLAGS, dir_fd=resolved_parent)
        identity = os.fstat(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        linked = os.stat(resolved.name, dir_fd=resolved_parent, follow_symlinks=False)
        valid = (
            _stat_identity(leaf)[:5] + _stat_identity(leaf)[6:] == _invocation_record_identity(record)
            and _stat_identity(identity) == _record_identity(record)
            and _same_identity(identity, linked)
            and link == record.get("leaf_link")
            and resolved == expected_resolved
            and digest.hexdigest() == record.get("sha256")
            and identity.st_mode & 0o111
        )
        if not valid or not _same_identity(identity, os.fstat(descriptor)):
            raise BootstrapAuthorityError("managed Python authority drifted")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(resolved_parent)
        os.close(invocation_parent)


def run_managed_python(arguments: list[str], environ: Mapping[str, str]) -> None:
    if not arguments or globals().get("__agentgov_system_python_sha256__") != environ.get("AGENT_GOV_ACCEPTANCE_SYSTEM_PYTHON_SHA256"):
        raise BootstrapAuthorityError("managed Python target is invalid")
    record = _managed_python_record(environ)
    descriptor = _open_managed_python(record)
    repository_descriptor = _initial_repository_descriptor()
    toolchain_descriptor: int | None = None
    import_descriptor: int | None = None
    try:
        toolchain_descriptor, toolchain_sha256 = _open_toolchain_source(repository_descriptor)
        if toolchain_sha256 != environ.get(PYTHON_TOOLCHAIN_SHA256_ENV):
            raise BootstrapAuthorityError("managed Python toolchain authority drifted")
        import_descriptor, import_sha256 = _open_bootstrap_source(IMPORT_AUTHORITY_PATH, repository_descriptor)
        bootstrap_sha256 = globals().get("__agentgov_loaded_sha256__")
        if not isinstance(bootstrap_sha256, str) or len(bootstrap_sha256) != 64:
            raise BootstrapAuthorityError("managed Python bootstrap authority is invalid")
        child = dict(environ)
        child[BOOTSTRAP_IMPORT_AUTHORITY_SHA256_ENV] = import_sha256
        child[BOOTSTRAP_TOOLCHAIN_SHA256_ENV] = toolchain_sha256
        child[LOADED_BOOTSTRAP_SHA256_ENV] = bootstrap_sha256
        os.set_inheritable(repository_descriptor, True)
        os.set_inheritable(descriptor, True)
        os.set_inheritable(toolchain_descriptor, True)
        os.set_inheritable(import_descriptor, True)
        os.execve(
            descriptor,
            (
                str(record["invocation_path"]),
                "-I",
                "-P",
                "-S",
                "-X",
                "pycache_prefix=/dev/null",
                f"/proc/self/fd/{import_descriptor}",
                str(repository_descriptor),
                str(import_descriptor),
                str(toolchain_descriptor),
                str(IMPORT_AUTHORITY_PATH),
                str(TOOLCHAIN_PATH),
                str(REPO_ROOT),
                "python",
                *arguments,
            ),
            child,
        )
    finally:
        if import_descriptor is not None:
            os.close(import_descriptor)
        if toolchain_descriptor is not None:
            os.close(toolchain_descriptor)
        os.close(repository_descriptor)
        os.close(descriptor)


def _snapshot_payload(environ: Mapping[str, str], snapshot_root: Path) -> tuple[list[object], dict[str, str]]:
    raw = environ.get(PREPARED_CANDIDATE_ENV, "")
    if not raw or len(raw.encode()) > _MAX_INTERNAL_AUTHORITY_BYTES:
        raise BootstrapAuthorityError("snapshot reexec authority is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BootstrapAuthorityError("snapshot reexec authority is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract", "source", "snapshot"}
        or payload.get("contract") != "agentgov.container-acceptance-candidate.v5"
        or not isinstance(payload.get("snapshot"), list)
        or len(payload["snapshot"]) != 32
        or payload["snapshot"][5] != str(snapshot_root)
        or payload["snapshot"][7] != str(snapshot_root / "repository")
    ):
        raise BootstrapAuthorityError("snapshot reexec authority is invalid")
    snapshot = payload["snapshot"]
    loaded = snapshot[24]
    if not isinstance(loaded, list) or not 0 < len(loaded) <= 64:
        raise BootstrapAuthorityError("snapshot loaded source authority is invalid")
    expected: dict[str, str] = {}
    for item in loaded:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or len(item[1]) != 64
            or set(item[1]) - set("0123456789abcdef")
            or item[0] in expected
        ):
            raise BootstrapAuthorityError("snapshot loaded source authority is invalid")
        expected[item[0]] = item[1]
    canonical = json.dumps(loaded, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != snapshot[25]:
        raise BootstrapAuthorityError("snapshot loaded source digest is invalid")
    return snapshot, expected


def _read_inherited_bootstrap(expected_sha256: str) -> str:
    parts = Path(__file__).parts
    if len(parts) != 5 or parts[:4] != ("/", "proc", "self", "fd") or not parts[4].isdecimal():
        raise BootstrapAuthorityError("snapshot bootstrap descriptor is invalid")
    descriptor = os.dup(int(parts[4]))
    try:
        before = os.fstat(descriptor)
        encoded = os.pread(descriptor, _MAX_TOOLCHAIN_BYTES + 1, 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or len(encoded) != before.st_size
            or len(encoded) > _MAX_TOOLCHAIN_BYTES
            or not _same_identity(before, os.fstat(descriptor))
            or hashlib.sha256(encoded).hexdigest() != expected_sha256
        ):
            raise BootstrapAuthorityError("snapshot bootstrap actual bytes drifted")
        return expected_sha256
    finally:
        os.close(descriptor)


def _load_snapshot_authority(
    repository_fd: int,
    expected: Mapping[str, str],
    bootstrap_digest: str,
) -> tuple[types.ModuleType, str]:
    relative = "scripts/container_acceptance_snapshot_authority.py"
    expected_sha256 = expected.get(relative, "")
    descriptor, digest = _open_bootstrap_source(REPO_ROOT / relative, repository_fd)
    try:
        if digest != expected_sha256:
            raise BootstrapAuthorityError("snapshot authority source drifted")
        encoded = os.pread(descriptor, _MAX_TOOLCHAIN_BYTES + 1, 0)
        if len(encoded) > _MAX_TOOLCHAIN_BYTES or hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise BootstrapAuthorityError("snapshot authority actual bytes drifted")
    finally:
        os.close(descriptor)
    module = types.ModuleType("scripts.container_acceptance_snapshot_authority")
    module.__file__ = f"/proc/self/fd/{repository_fd}/{relative}"
    module.__package__ = "scripts"
    module.__dict__["__agentgov_bootstrap_sha256__"] = bootstrap_digest
    missing_module = object()
    previous = sys.modules.get(module.__name__, missing_module)
    sys.modules[module.__name__] = module
    try:
        exec(compile(encoded, str(module.__file__), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        if previous is missing_module:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = cast(types.ModuleType, previous)
        raise
    return module, digest


def resume(arguments: list[str], environ: Mapping[str, str]) -> None:
    raw_root = environ.get(SNAPSHOT_ROOT_ENV)
    if not raw_root or not Path(raw_root).is_absolute() or not arguments:
        raise BootstrapAuthorityError("snapshot resume target is invalid")
    snapshot_root = Path(os.path.abspath(raw_root))
    snapshot, expected = _snapshot_payload(environ, snapshot_root)
    bootstrap_digest = _read_inherited_bootstrap(expected.get("scripts/container_acceptance_bootstrap.py", ""))
    root_fd = _open_directory(snapshot_root)
    repository_fd: int | None = None
    try:
        if _path_identity(os.fstat(root_fd)) != snapshot[6]:
            raise BootstrapAuthorityError("snapshot root authority drifted")
        repository_fd = _open_child_directory(root_fd, "repository")
        if _path_identity(os.fstat(repository_fd)) != snapshot[8]:
            raise BootstrapAuthorityError("snapshot repository authority drifted")
        module, module_digest = _load_snapshot_authority(repository_fd, expected, bootstrap_digest)
        entrypoint = getattr(module, "resume", None)
        if not callable(entrypoint):
            raise BootstrapAuthorityError("snapshot authority entrypoint is invalid")
        entrypoint(
            arguments,
            environ,
            snapshot_root=snapshot_root,
            root_fd=root_fd,
            repository_fd=repository_fd,
            module_digest=module_digest,
            bootstrap_digest=bootstrap_digest,
        )
    except RuntimeError as exc:
        raise BootstrapAuthorityError("snapshot authority validation failed") from exc
    finally:
        if repository_fd is not None:
            os.close(repository_fd)
        os.close(root_fd)


def main() -> int:
    try:
        if not sys.argv[1:]:
            raise BootstrapAuthorityError("container acceptance bootstrap mode is invalid")
        if sys.argv[1] == "launch":
            launch(os.environ)
        elif sys.argv[1] == "resume":
            resume(sys.argv[2:], os.environ)
        elif sys.argv[1] == "python":
            run_managed_python(sys.argv[2:], os.environ)
        else:
            raise BootstrapAuthorityError("container acceptance bootstrap mode is invalid")
    except (IndexError, OSError, UnicodeError, ValueError, BootstrapAuthorityError):
        print("CONTAINER_ACCEPTANCE_BOOTSTRAP_FAIL: fixed Python authority is invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
