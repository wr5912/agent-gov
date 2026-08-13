"""固定本机 Docker socket 与 daemon 非敏感摘要 authority。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import stat
from pathlib import Path
from typing import Final, TypedDict

DOCKER_SOCKET: Final = Path("/run/docker.sock")
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


class DockerAuthorityError(RuntimeError):
    """固定 Docker socket 或 daemon identity 不可用。"""


class DockerDaemonAuthority(TypedDict):
    socket_authority_sha256: str
    daemon_identity_sha256: str


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=3.0)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(DOCKER_SOCKET))
        self.sock = connection


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _socket_identity() -> os.stat_result:
    try:
        directory_fd = os.open(DOCKER_SOCKET.parent, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise DockerAuthorityError("fixed Docker socket parent is unavailable") from exc
    try:
        parent = os.fstat(directory_fd)
        identity = os.stat(DOCKER_SOCKET.name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DockerAuthorityError("fixed Docker socket authority is unavailable") from exc
    finally:
        os.close(directory_fd)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & stat.S_IWOTH
        or not stat.S_ISSOCK(identity.st_mode)
        or identity.st_uid != 0
        or identity.st_mode & stat.S_IWOTH
    ):
        raise DockerAuthorityError("fixed Docker socket authority is invalid")
    return identity


def _read_daemon_payload(path: str) -> dict[str, object]:
    connection = _UnixHTTPConnection()
    try:
        connection.request("GET", path, headers={"Host": "localhost"})
        response = connection.getresponse()
        encoded = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(encoded) > 1024 * 1024:
            raise DockerAuthorityError("fixed Docker daemon identity is unavailable")
        payload = json.loads(encoded)
    except (OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
        raise DockerAuthorityError("fixed Docker daemon identity is unavailable") from exc
    finally:
        connection.close()
    if not isinstance(payload, dict):
        raise DockerAuthorityError("fixed Docker daemon identity is invalid")
    return payload


def capture_docker_daemon_authority() -> DockerDaemonAuthority:
    before = _socket_identity()
    version = _read_daemon_payload("/version")
    info = _read_daemon_payload("/info")
    after = _socket_identity()
    if not _same_identity(before, after):
        raise DockerAuthorityError("fixed Docker socket drifted while capturing daemon identity")
    daemon = {
        "id": info.get("ID"),
        "name": info.get("Name"),
        "server_version": info.get("ServerVersion"),
        "version": version.get("Version"),
        "api_version": version.get("ApiVersion"),
        "min_api_version": version.get("MinAPIVersion"),
    }
    if any(not isinstance(value, str) or not value for value in daemon.values()):
        raise DockerAuthorityError("fixed Docker daemon identity is incomplete")
    socket_payload = [before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid]
    return {
        "socket_authority_sha256": hashlib.sha256(_canonical_json(socket_payload)).hexdigest(),
        "daemon_identity_sha256": hashlib.sha256(_canonical_json(daemon)).hexdigest(),
    }
