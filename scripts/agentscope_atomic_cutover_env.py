"""Strict selected-env parsing and immutable cutover env derivation."""

from __future__ import annotations

import io
import os
import pwd
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dotenv.parser import Binding, parse_stream

_ENV_READ_LIMIT = 1024 * 1024
_ErrorT = TypeVar("_ErrorT", bound=RuntimeError)


@dataclass(frozen=True)
class OperatorIdentity:
    uid: int
    gid: int
    home: Path


def trusted_operator_identity() -> OperatorIdentity:
    uid = os.geteuid()
    gid = os.getegid()
    if uid == 0 and os.environ.get("SUDO_UID"):
        raw_uid = os.environ["SUDO_UID"]
        raw_gid = os.environ.get("SUDO_GID", "")
        if not raw_uid.isdecimal() or not raw_gid.isdecimal() or int(raw_uid) <= 0:
            raise ValueError("SUDO_UID/SUDO_GID 无效")
        uid, gid = int(raw_uid), int(raw_gid)
    account = pwd.getpwuid(uid)
    if uid != 0 and gid != account.pw_gid:
        raise ValueError("cutover operator gid 与 passwd identity 不一致")
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and uid != 0 and sudo_user != account.pw_name:
        raise ValueError("SUDO_USER 与 SUDO_UID 不一致")
    home = Path(account.pw_dir).resolve(strict=True)
    if not home.is_dir() or home.is_symlink() or home.stat().st_uid != uid:
        raise ValueError("cutover operator HOME owner/type 不安全")
    return OperatorIdentity(uid=uid, gid=gid, home=home)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def read_stable_env_file(path: Path, *, error_type: type[_ErrorT]) -> tuple[bytes, tuple[int, ...]]:
    """Read one regular env through no-follow dirfds and bind it to its directory entry."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    absolute = Path(os.path.abspath(path))
    if nofollow is None or directory is None or cloexec is None or not absolute.is_absolute():
        raise error_type("当前平台无法安全固定所选 Compose env")
    directory_fd = os.open("/", os.O_RDONLY | directory | nofollow | cloexec)
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        file_fd = os.open(absolute.name, os.O_RDONLY | nofollow | cloexec, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise error_type("所选 Compose env 必须是不可经符号链接替换的普通文件") from exc
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _ENV_READ_LIMIT:
            raise error_type("所选 Compose env 必须是大小受限的普通文件")
        payload = bytearray()
        while chunk := os.read(file_fd, 1024 * 1024):
            payload.extend(chunk)
            if len(payload) > _ENV_READ_LIMIT:
                raise error_type("所选 Compose env 超过安全读取上限")
        after = os.fstat(file_fd)
        current = os.stat(absolute.name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            raise error_type("所选 Compose env 在读取期间发生变化")
        return bytes(payload), _file_identity(after)
    except OSError as exc:
        raise error_type("所选 Compose env 无法稳定读取") from exc
    finally:
        os.close(file_fd)
        os.close(directory_fd)


def verify_stable_env_file(
    path: Path,
    expected_payload: bytes,
    expected_identity: tuple[int, ...],
    *,
    error_type: type[_ErrorT],
) -> None:
    current_payload, current_identity = read_stable_env_file(path, error_type=error_type)
    if current_payload != expected_payload or current_identity != expected_identity:
        raise error_type("所选 Compose env 在事务开始前发生变化")


def parse_selected_env_payload(payload: bytes) -> list[Binding]:
    try:
        rendered = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("cutover env 必须是 UTF-8") from exc
    if "\x00" in rendered:
        raise ValueError("cutover env 含 NUL 字节")
    bindings: list[Binding] = []
    keys: set[str] = set()
    for binding in parse_stream(io.StringIO(rendered)):
        if binding.error:
            raise ValueError("cutover env 格式无法安全解析")
        if binding.key is not None:
            if binding.key in keys:
                raise ValueError(f"cutover env 含重复键: {binding.key}")
            keys.add(binding.key)
        bindings.append(binding)
    return bindings


def parse_selected_env_bindings(path: Path) -> list[Binding]:
    payload, _identity = read_stable_env_file(path, error_type=ValueError)
    return parse_selected_env_payload(payload)


def declared_env_keys(path: Path) -> set[str]:
    return {binding.key for binding in parse_selected_env_bindings(path) if binding.key is not None}


def write_env_overrides(
    source: Path,
    destination: Path,
    *,
    label: str,
    overrides: Mapping[str, str],
) -> None:
    if any(any(character in value for character in "\x00\r\n$") for value in overrides.values()):
        raise ValueError("cutover env override 含不安全字符")
    retained = [binding.original.string.rstrip("\r\n") for binding in parse_selected_env_bindings(source) if binding.key not in overrides]
    retained.extend(("", f"# AgentScope cutover {label} overrides"))
    retained.extend(f"{key}={value}" for key, value in overrides.items())
    destination.write_text("\n".join(retained).rstrip() + "\n", encoding="utf-8")
    destination.chmod(0o600)
