"""为运行中容器物化并清理 root-owned deployment source CAS。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from scripts.selected_env_operation_contract import SelectedEnvError

RunCommand: TypeAlias = Callable[[list[str], dict[str, str]], int]
RunOutput: TypeAlias = Callable[[list[str], dict[str, str]], str]
SourceHasher: TypeAlias = Callable[[Path], str]
DaemonVerifier: TypeAlias = Callable[[], None]


class DaemonSupport(Protocol):
    def capture(self, child_env: Mapping[str, str]) -> Mapping[str, object]: ...

    def verify(self, child_env: Mapping[str, str], expected: Mapping[str, object]) -> None: ...


_PERSISTENT_CAS_ROOT = Path("/var/lib/agentgov/deployment-sources-v1")
_ROOT_OWNER_UID = 0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_LOCK_NAME = "agentgov-selected-env-mutation-lock-v1"
_LOCK_NONCE_ENV = "AGENTGOV_SELECTED_ENV_LOCK_NONCE"
_LOCK_LABEL = "io.agentgov.selected-env-mutation-lock"
_LOCK_NONCE_LABEL = "io.agentgov.selected-env-lock-nonce"

_MATERIALIZE_SCRIPT = r"""
digest="$1"
nonce="$2"
case "$digest" in
  *[!0-9a-f]*|'') exit 64 ;;
esac
[ "${#digest}" -eq 64 ] || exit 64
case "$nonce" in
  *[!0-9a-f]*|'') exit 64 ;;
esac
[ "${#nonce}" -eq 32 ] || exit 64
root=/agentgov-cas
final="$root/$digest"
chmod 0755 "$root"
if [ -e "$final" ]; then
  chmod 0555 "$root"
  exit 0
fi
staging="$root/.${digest}.${nonce}"
cleanup() { rm -rf --one-file-system -- "$staging"; chmod 0555 "$root"; }
trap cleanup EXIT HUP INT TERM
mkdir -m 0700 "$staging"
cp -a /agentgov-input "$staging/source"
chown -R 0:0 "$staging"
find "$staging" -type d -exec chmod a-w,a+rx {} +
find "$staging" -type f -exec chmod a-w,a+r {} +
mv -T -- "$staging" "$final"
chmod 0555 "$root"
trap - EXIT HUP INT TERM
""".strip()

_CLEAN_SCRIPT = r"""
keep="$1"
root=/agentgov-cas
chmod 0755 "$root"
for entry in "$root"/*; do
  [ -e "$entry" ] || continue
  name=${entry##*/}
  case "$name" in
    *[!0-9a-f]*|'') exit 64 ;;
  esac
  [ "${#name}" -eq 64 ] || exit 64
  [ -n "$keep" ] && [ "$name" = "$keep" ] && continue
  rm -rf --one-file-system -- "$entry"
done
chmod 0555 "$root"
""".strip()


def bind_source_root(digest: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise SelectedEnvError("deployment source CAS digest 无效")
    return _PERSISTENT_CAS_ROOT / digest / "source"


@contextmanager
def daemon_mutation_lock(
    enabled: bool,
    child_env: dict[str, str],
    daemon_factory: Callable[[], DaemonSupport],
    *,
    run_command: RunCommand,
    run_output: RunOutput,
) -> Iterator[Mapping[str, object] | None]:
    """用 daemon 内原子 network name 锁串行化完整 selected-env mutation。"""
    if not enabled:
        yield None
        return
    daemon = daemon_factory()
    expected = daemon.capture(child_env)
    nonce = os.urandom(32).hex()
    command = [
        "docker",
        "network",
        "create",
        "--internal",
        "--label",
        f"{_LOCK_LABEL}=v1",
        "--label",
        f"{_LOCK_NONCE_LABEL}={nonce}",
        _LOCK_NAME,
    ]
    if run_command(command, child_env) != 0:
        raise SelectedEnvError("已有同 Docker daemon 的 selected-env mutating operation 正在执行")
    child_env[_LOCK_NONCE_ENV] = nonce
    try:
        daemon.verify(child_env, expected)
        _verify_daemon_lock(child_env, nonce, run_output=run_output)
        yield expected
        daemon.verify(child_env, expected)
        _verify_daemon_lock(child_env, nonce, run_output=run_output)
    finally:
        try:
            if run_command(["docker", "network", "rm", _LOCK_NAME], child_env) != 0:
                raise SelectedEnvError("无法释放 selected-env daemon mutation lock")
            _verify_daemon_lock_removed(child_env, run_output=run_output)
            daemon.verify(child_env, expected)
        finally:
            child_env.pop(_LOCK_NONCE_ENV, None)


def materialize(
    source_root: Path,
    canonical_digest: str,
    execution_digest: str,
    child_env: dict[str, str],
    *,
    helper_image: str,
    run_command: RunCommand,
    hash_source: SourceHasher,
    verify_daemon: DaemonVerifier,
) -> Path:
    """通过已绑定 daemon 创建 root-owned CAS，并逐字节复验。"""
    lock_nonce = _require_lock_nonce(child_env)
    if _SHA256.fullmatch(execution_digest) is None:
        raise SelectedEnvError("deployment source execution digest 无效")
    target = bind_source_root(canonical_digest)
    source_tree = _content_tree_sha256(source_root, expected_uid=None)
    if target.exists() or target.is_symlink():
        _verify_materialized(
            target,
            canonical_digest,
            execution_digest,
            source_tree,
            hash_source=hash_source,
        )
        return target
    verify_daemon()
    command = _helper_command(
        source_root,
        helper_image,
        script=_MATERIALIZE_SCRIPT,
        argument=canonical_digest,
        nonce=lock_nonce[:32],
        source_mount=True,
    )
    if run_command(command, child_env) != 0:
        raise SelectedEnvError("root-owned deployment source CAS 物化失败")
    verify_daemon()
    _verify_materialized(
        target,
        canonical_digest,
        execution_digest,
        source_tree,
        hash_source=hash_source,
    )
    return target


def cleanup_obsolete(
    child_env: dict[str, str],
    *,
    keep_digest: str | None,
    helper_image: str,
    run_command: RunCommand,
    run_output: RunOutput,
    verify_daemon: DaemonVerifier,
) -> None:
    """清理未被任何容器引用的旧 CAS；发现引用则拒绝删除。"""
    _require_lock_nonce(child_env)
    if keep_digest is not None and _SHA256.fullmatch(keep_digest) is None:
        raise SelectedEnvError("保留的 deployment source CAS digest 无效")
    if not _PERSISTENT_CAS_ROOT.exists():
        return
    entries = _cas_entries()
    obsolete = entries - ({keep_digest} if keep_digest is not None else set())
    if not obsolete:
        return
    mounted = _mounted_digests(child_env, run_output=run_output)
    blocked = sorted(obsolete & mounted)
    if blocked:
        raise SelectedEnvError(f"旧 deployment source CAS 仍被容器引用: {blocked}")
    verify_daemon()
    command = _helper_command(
        _PERSISTENT_CAS_ROOT,
        helper_image,
        script=_CLEAN_SCRIPT,
        argument=keep_digest or "",
        nonce="",
        source_mount=False,
    )
    if run_command(command, child_env) != 0:
        raise SelectedEnvError("旧 deployment source CAS 清理失败")
    verify_daemon()
    remaining = _cas_entries()
    expected = {keep_digest} if keep_digest is not None and keep_digest in entries else set()
    if remaining != expected:
        raise SelectedEnvError("deployment source CAS 清理结果不完整")


def _helper_command(
    source_root: Path,
    helper_image: str,
    *,
    script: str,
    argument: str,
    nonce: str,
    source_mount: bool,
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
    ]
    if source_mount:
        command.extend(("--mount", f"type=bind,source={source_root},target=/agentgov-input,readonly"))
    command.extend(
        (
            "--volume",
            f"{_PERSISTENT_CAS_ROOT}:/agentgov-cas",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--entrypoint",
            "/bin/sh",
            helper_image,
            "-ceu",
            script,
            "agentgov-persistent-source",
            argument,
            nonce,
        )
    )
    return command


def _require_lock_nonce(child_env: Mapping[str, str]) -> str:
    nonce = child_env.get(_LOCK_NONCE_ENV, "")
    if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise SelectedEnvError("deployment source CAS mutation 未持有 daemon-global lock")
    return nonce


def _verify_daemon_lock(
    child_env: dict[str, str],
    nonce: str,
    *,
    run_output: RunOutput,
) -> None:
    rendered = run_output(["docker", "network", "inspect", _LOCK_NAME], child_env)
    try:
        values = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError("selected-env daemon mutation lock inventory 不是 JSON") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], Mapping):
        raise SelectedEnvError("selected-env daemon mutation lock inventory 无效")
    network = values[0]
    labels = network.get("Labels")
    valid = (
        network.get("Name") == _LOCK_NAME
        and network.get("Internal") is True
        and isinstance(network.get("Id"), str)
        and _CONTAINER_ID.fullmatch(network["Id"]) is not None
        and labels == {_LOCK_LABEL: "v1", _LOCK_NONCE_LABEL: nonce}
    )
    if not valid:
        raise SelectedEnvError("selected-env daemon mutation lock identity 已漂移")


def _verify_daemon_lock_removed(child_env: dict[str, str], *, run_output: RunOutput) -> None:
    rendered = run_output(
        ["docker", "network", "ls", "--filter", f"name=^{_LOCK_NAME}$", "--format", "{{.ID}}"],
        child_env,
    )
    if rendered.strip():
        raise SelectedEnvError("selected-env daemon mutation lock 未完全释放")


def _verify_materialized(
    target: Path,
    canonical_digest: str,
    execution_digest: str,
    expected_tree: str,
    *,
    hash_source: SourceHasher,
) -> None:
    _require_root_directory(_PERSISTENT_CAS_ROOT.parent, readonly=False)
    _require_root_directory(_PERSISTENT_CAS_ROOT, readonly=True)
    _require_root_directory(target.parent, readonly=True)
    if target != bind_source_root(canonical_digest):
        raise SelectedEnvError("deployment source CAS bind path 不匹配")
    observed_tree = _content_tree_sha256(target, expected_uid=_ROOT_OWNER_UID)
    if observed_tree != expected_tree or hash_source(target) != execution_digest:
        raise SelectedEnvError("root-owned deployment source CAS 与冻结 source 不一致")


def _require_root_directory(path: Path, *, readonly: bool) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SelectedEnvError("无法复验 root-owned deployment source CAS") from exc
    invalid = (
        path.is_symlink()
        or resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != _ROOT_OWNER_UID
        or bool(metadata.st_mode & 0o022)
        or (readonly and bool(metadata.st_mode & 0o200))
    )
    if invalid:
        raise SelectedEnvError("deployment source CAS root owner/mode 无效")


def _cas_entries() -> set[str]:
    _require_root_directory(_PERSISTENT_CAS_ROOT, readonly=True)
    entries: set[str] = set()
    try:
        children = tuple(_PERSISTENT_CAS_ROOT.iterdir())
    except OSError as exc:
        raise SelectedEnvError("无法枚举 deployment source CAS") from exc
    for child in children:
        if _SHA256.fullmatch(child.name) is None or child.is_symlink() or not child.is_dir():
            raise SelectedEnvError("deployment source CAS 含非摘要目录")
        entries.add(child.name)
    return entries


def _mounted_digests(child_env: dict[str, str], *, run_output: RunOutput) -> set[str]:
    rendered_ids = run_output(
        ["docker", "container", "ls", "--all", "--quiet", "--no-trunc"],
        child_env,
    )
    container_ids = [item.strip() for item in rendered_ids.splitlines() if item.strip()]
    if any(_CONTAINER_ID.fullmatch(item) is None for item in container_ids):
        raise SelectedEnvError("Docker container identity 无效，拒绝清理 source CAS")
    if not container_ids:
        return set()
    rendered = run_output(["docker", "container", "inspect", *container_ids], child_env)
    try:
        containers = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError("Docker container mount inventory 不是 JSON") from exc
    if not isinstance(containers, list) or len(containers) != len(container_ids):
        raise SelectedEnvError("Docker container mount inventory 结构无效")
    return _extract_mounted_digests(containers)


def _extract_mounted_digests(containers: list[object]) -> set[str]:
    mounted: set[str] = set()
    for container in containers:
        if not isinstance(container, Mapping) or not isinstance(container.get("Mounts"), list):
            raise SelectedEnvError("Docker container mounts 结构无效")
        for mount in container["Mounts"]:
            if not isinstance(mount, Mapping) or not isinstance(mount.get("Source"), str):
                raise SelectedEnvError("Docker container mount 结构无效")
            source = Path(mount["Source"])
            try:
                relative = source.relative_to(_PERSISTENT_CAS_ROOT)
            except ValueError:
                continue
            if not relative.parts or _SHA256.fullmatch(relative.parts[0]) is None:
                raise SelectedEnvError("容器引用了未归档的 deployment source CAS 路径")
            mounted.add(relative.parts[0])
    return mounted


def _content_tree_sha256(root: Path, *, expected_uid: int | None) -> str:
    try:
        paths = sorted((root, *root.rglob("*")))
    except OSError as exc:
        raise SelectedEnvError("无法遍历 deployment source CAS") from exc
    digest = hashlib.sha256()
    for path in paths:
        metadata = path.lstat()
        valid_type = stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        if path.is_symlink() or not valid_type or metadata.st_mode & 0o222:
            raise SelectedEnvError("deployment source CAS 含可写或特殊 entry")
        if expected_uid is not None and metadata.st_uid != expected_uid:
            raise SelectedEnvError("deployment source CAS entry 并非 root-owned")
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        digest.update(b"d" if stat.S_ISDIR(metadata.st_mode) else b"f")
        if stat.S_ISREG(metadata.st_mode):
            _hash_regular_file(digest, path, metadata)
    return digest.hexdigest()


def _hash_regular_file(digest: Any, path: Path, initial: os.stat_result) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(initial):
            raise SelectedEnvError("deployment source CAS entry 在读取前漂移")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(initial):
            raise SelectedEnvError("deployment source CAS entry 在读取期间漂移")
    finally:
        os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
