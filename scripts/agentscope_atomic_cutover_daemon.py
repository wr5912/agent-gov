"""Local Docker daemon identity contract for destructive atomic cutover."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn, TypedDict
from urllib.parse import unquote, urlsplit

try:
    from scripts.agentscope_atomic_cutover_types import DockerDaemonIdentity
except ModuleNotFoundError:
    from agentscope_atomic_cutover_types import DockerDaemonIdentity


_DAEMON_ID = re.compile(r"[^\x00-\x20\x7f]{1,256}")
HOST_FILESYSTEM_PROBE_IMAGE = "docker.io/postgres:17.9@sha256:347bc4e64006d47bb255b0e28652d08590260a5e97f6b55f6ba1c0b31aef58b3"


class _SocketIdentity(TypedDict):
    socket_device: int
    socket_inode: int
    socket_uid: int
    socket_mode: int


class CutoverDaemonSupport:
    """Require one local Unix daemon and bind its stable Engine identity."""

    def __init__(
        self,
        *,
        error_type: type[RuntimeError],
        run_command: Callable[..., str],
    ) -> None:
        self._error_type = error_type
        self._run_command = run_command

    def _fail(self, message: str) -> NoReturn:
        raise self._error_type(message)

    def capture(self, child_env: Mapping[str, str]) -> DockerDaemonIdentity:
        endpoint = self._effective_endpoint(child_env)
        before = self._socket_identity(endpoint)
        daemon_id = self._daemon_id(child_env)
        after = self._socket_identity(endpoint)
        if before != after:
            self._fail("Docker daemon Unix socket 在 identity capture 期间发生变化")
        return DockerDaemonIdentity(endpoint=endpoint, id=daemon_id, **before)

    def verify(
        self,
        child_env: Mapping[str, str],
        expected: Mapping[str, object],
    ) -> None:
        required = {"endpoint", "id", "socket_device", "socket_inode", "socket_uid", "socket_mode"}
        if set(expected) != required:
            self._fail("prepare Docker daemon identity 结构无效")
        current = self.capture(child_env)
        if current != expected:
            self._fail("Docker daemon endpoint/identity 在 prepare 后发生变化")

    def verify_host_filesystem(
        self,
        child_env: Mapping[str, str],
        probe_root: Path,
        image_id: str,
    ) -> None:
        root = probe_root.resolve()
        if probe_root.is_symlink() or not root.is_dir() or "," in root.as_posix():
            self._fail("Docker host-filesystem probe root 无效")
        nonce = os.urandom(32).hex().encode("ascii")
        descriptor, raw_path = tempfile.mkstemp(prefix=".agentgov-daemon-probe-", dir=root)
        probe = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(nonce)
                stream.flush()
                os.fsync(stream.fileno())
            expected = nonce.decode("ascii")
            actual = self._run_command(
                [
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
                    "--mount",
                    f"type=bind,source={probe},target=/agentgov-host-probe,readonly",
                    "--entrypoint",
                    "/bin/cat",
                    image_id,
                    "/agentgov-host-probe",
                ],
                "Docker host-filesystem nonce probe",
                capture=True,
                child_env=child_env,
            ).strip()
            if actual != expected:
                self._fail("Docker daemon 未绑定当前 host filesystem")
        finally:
            probe.unlink(missing_ok=True)

    def require_runtime_root_quiescent(
        self,
        child_env: Mapping[str, str],
        runtime_root: Path,
    ) -> None:
        rendered_ids = self._run_command(
            ["docker", "container", "ls", "--quiet", "--no-trunc"],
            "枚举 Docker 运行容器",
            capture=True,
            child_env=child_env,
        )
        container_ids = [item.strip() for item in rendered_ids.splitlines() if item.strip()]
        if not container_ids:
            return
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in container_ids):
            self._fail("Docker 运行容器 identity 无效")
        rendered = self._run_command(
            ["docker", "container", "inspect", *container_ids],
            "核验 Docker 运行容器 mounts",
            capture=True,
            child_env=child_env,
        )
        try:
            containers = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise self._error_type("Docker container mount inventory 不是 JSON") from exc
        if not isinstance(containers, list) or len(containers) != len(container_ids):
            self._fail("Docker container mount inventory 结构无效")
        root = runtime_root.resolve()
        for container in containers:
            if not isinstance(container, dict) or not isinstance(container.get("Mounts"), list):
                self._fail("Docker container mounts 结构无效")
            for mount in container["Mounts"]:
                if not isinstance(mount, dict):
                    self._fail("Docker container mount 结构无效")
                if type(mount.get("RW")) is not bool:
                    self._fail("Docker container mount RW 标记无效")
                if mount["RW"] is False:
                    continue
                for source in self._mount_host_paths(mount, child_env):
                    resolved = source.resolve()
                    if resolved == root or resolved.is_relative_to(root) or root.is_relative_to(resolved):
                        self._fail("运行容器仍持有覆盖 Runtime root 的可写 Docker mount")

    def _mount_host_paths(self, mount: Mapping[str, object], child_env: Mapping[str, str]) -> tuple[Path, ...]:
        mount_type = mount.get("Type")
        source = mount.get("Source")
        if mount_type == "bind":
            if not isinstance(source, str) or not source.startswith("/"):
                self._fail("Docker bind mount source 结构无效")
            return (Path(source),)
        if mount_type == "tmpfs":
            return ()
        if mount_type != "volume" or not isinstance(mount.get("Name"), str):
            self._fail("无法证明 Docker RW mount 不覆盖 Runtime root")
        rendered = self._run_command(
            ["docker", "volume", "inspect", str(mount["Name"])],
            "核验 Docker named volume host path",
            capture=True,
            child_env=child_env,
        )
        try:
            payload = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise self._error_type("Docker volume inventory 不是 JSON") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            self._fail("Docker volume inventory 结构无效")
        volume = payload[0]
        if volume.get("Driver") != "local":
            self._fail("无法证明非 local Docker RW volume 不覆盖 Runtime root")
        mountpoint = volume.get("Mountpoint")
        if not isinstance(mountpoint, str) or not mountpoint.startswith("/"):
            self._fail("Docker volume Mountpoint 无效")
        paths = [Path(mountpoint)]
        options = volume.get("Options")
        if options is not None:
            if not isinstance(options, dict):
                self._fail("Docker volume Options 无效")
            device = options.get("device")
            if device is not None:
                if not isinstance(device, str) or not device.startswith("/"):
                    self._fail("bind-backed Docker volume device 无效")
                paths.append(Path(device))
        return tuple(paths)

    def verify_destructive_boundary(
        self,
        child_env: Mapping[str, str],
        expected: Mapping[str, object],
        runtime_root: Path,
        image_id: str,
    ) -> None:
        self.verify(child_env, expected)
        self.verify_host_filesystem(child_env, runtime_root, image_id)
        self.require_runtime_root_quiescent(child_env, runtime_root)
        self.verify(child_env, expected)
        self.verify_host_filesystem(child_env, runtime_root, image_id)

    def verify_prepare_boundary(
        self,
        child_env: Mapping[str, str],
        expected: Mapping[str, object],
        probe_root: Path,
        runtime_root: Path,
        image_id: str,
    ) -> None:
        self.verify(child_env, expected)
        self.verify_host_filesystem(child_env, probe_root, image_id)
        self.require_runtime_root_quiescent(child_env, runtime_root)
        self.verify(child_env, expected)
        self.verify_host_filesystem(child_env, probe_root, image_id)

    def _effective_endpoint(self, child_env: Mapping[str, str]) -> str:
        context = child_env.get("DOCKER_CONTEXT", "").strip()
        host = child_env.get("DOCKER_HOST", "").strip()
        if context:
            endpoint = self._inspect_context_endpoint(context, child_env)
        elif host:
            endpoint = host
        else:
            selected = self._run_command(
                ["docker", "context", "show"],
                "读取 Docker context",
                capture=True,
                child_env=child_env,
            ).strip()
            if not selected or "\n" in selected:
                self._fail("Docker context identity 无效")
            endpoint = self._inspect_context_endpoint(selected, child_env)
        return self._canonical_local_unix_endpoint(endpoint)

    def _inspect_context_endpoint(
        self,
        context: str,
        child_env: Mapping[str, str],
    ) -> str:
        rendered = self._run_command(
            ["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"],
            "读取 Docker context endpoint",
            capture=True,
            child_env=child_env,
        ).strip()
        try:
            endpoint = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise self._error_type("Docker context endpoint 不是 JSON") from exc
        if not isinstance(endpoint, str) or not endpoint:
            self._fail("Docker context endpoint 无效")
        return endpoint

    def _canonical_local_unix_endpoint(self, endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "unix" or parsed.netloc or parsed.query or parsed.fragment:
            self._fail("atomic cutover 仅允许本机 unix Docker daemon")
        socket_path = Path(unquote(parsed.path))
        if not socket_path.is_absolute():
            self._fail("Docker unix socket 必须是绝对路径")
        try:
            resolved = socket_path.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as exc:
            raise self._error_type("Docker unix socket 不可访问") from exc
        if not stat.S_ISSOCK(metadata.st_mode):
            self._fail("Docker endpoint 不是本机 unix socket")
        return f"unix://{resolved.as_posix()}"

    def _socket_identity(self, endpoint: str) -> _SocketIdentity:
        parsed = urlsplit(endpoint)
        try:
            metadata = os.stat(Path(unquote(parsed.path)), follow_symlinks=False)
        except OSError as exc:
            raise self._error_type("Docker unix socket identity 不可访问") from exc
        if not stat.S_ISSOCK(metadata.st_mode):
            self._fail("Docker endpoint 不再是 Unix socket")
        return {
            "socket_device": metadata.st_dev,
            "socket_inode": metadata.st_ino,
            "socket_uid": metadata.st_uid,
            "socket_mode": metadata.st_mode,
        }

    def _daemon_id(self, child_env: Mapping[str, str]) -> str:
        rendered = self._run_command(
            ["docker", "info", "--format", "{{json .ID}}"],
            "读取 Docker daemon identity",
            capture=True,
            child_env=child_env,
        ).strip()
        try:
            daemon_id = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise self._error_type("Docker daemon identity 不是 JSON") from exc
        if not isinstance(daemon_id, str) or _DAEMON_ID.fullmatch(daemon_id) is None:
            self._fail("Docker daemon identity 无效")
        return daemon_id
