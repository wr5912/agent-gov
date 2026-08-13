from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from app.runtime.json_types import JsonObject

from .docker_volume_contracts import (
    normalized_mount_path,
    validate_docker_volume_name,
    validated_local_volume_mountpoint,
)

_JSON_OBJECT = TypeAdapter(JsonObject)
_JSON_OBJECT_LIST = TypeAdapter(list[JsonObject])
_IMAGE_ID_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_REFERENCE_PATTERN: Final = re.compile(r"^[0-9a-f]{12,64}$")
_MULTIPLEXED_LOG_CONTENT_TYPES: Final = frozenset(
    {
        "application/vnd.docker.multiplexed-stream",
        "application/vnd.docker.raw-stream",
    }
)


class DockerEngineError(RuntimeError):
    """Docker Engine 请求失败，但不向上游泄露 daemon response body。"""

    def __init__(self, *, code: str, operation: str, status_code: int | None = None) -> None:
        self.code = code
        self.operation = operation
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"Docker Engine {operation} failed{status}")


class DockerEngineProtocolError(DockerEngineError):
    """Docker Engine 返回了不满足预期的响应。"""


@dataclass(frozen=True, slots=True)
class DockerLogs:
    stdout: str
    stderr: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkerMountAuthority:
    data_source: Path
    runs_volume_name: str


@dataclass(frozen=True, slots=True)
class _WorkerMountEvidence:
    data_source: Path
    runs_volume_name: str
    runs_mountpoint: Path


@dataclass(frozen=True, slots=True)
class _KernelMountIdentity:
    device: str
    root: Path


class _ContainerInspector(Protocol):
    def inspect_container(self, container_id: str) -> JsonObject: ...

    def inspect_volume(self, volume_name: str) -> JsonObject: ...


def inspect_worker_mount_authority(
    engine: _ContainerInspector,
    *,
    container_id: str,
    data_dir: Path,
    runs_dir: Path,
    mountinfo_path: Path,
) -> WorkerMountAuthority:
    inspect = engine.inspect_container(container_id)
    inspected_id = inspect.get("Id")
    if not isinstance(inspected_id, str) or _CONTAINER_ID_PATTERN.fullmatch(inspected_id) is None or not inspected_id.startswith(container_id):
        raise ValueError("inspect identity mismatch")
    state = inspect.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise ValueError("worker inspect does not describe its running container")
    _require_real_mount_directory(data_dir, field="data_dir")
    _require_real_mount_directory(runs_dir, field="runs_dir")
    evidence = _worker_mount_evidence(
        inspect,
        data_destination=data_dir,
        runs_destination=runs_dir,
    )
    _verify_local_volume(
        engine.inspect_volume(evidence.runs_volume_name),
        evidence=evidence,
    )
    identities = _kernel_mount_identities(mountinfo_path, destinations=(data_dir, runs_dir))
    _require_separate_kernel_mounts(identities=identities, data_dir=data_dir, runs_dir=runs_dir)
    return WorkerMountAuthority(
        data_source=evidence.data_source,
        runs_volume_name=evidence.runs_volume_name,
    )


def _worker_mount_evidence(
    inspect: JsonObject,
    *,
    data_destination: Path,
    runs_destination: Path,
) -> _WorkerMountEvidence:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise ValueError("worker inspect mounts are unavailable")
    expected = {str(data_destination), str(runs_destination)}
    found: set[str] = set()
    data_source: Path | None = None
    runs_volume_name: str | None = None
    runs_mountpoint: Path | None = None
    for raw_mount in mounts:
        if not isinstance(raw_mount, dict):
            raise ValueError("worker inspect mount is invalid")
        destination = normalized_mount_path(raw_mount.get("Destination"), field="mount destination")
        overlaps_expected = any(
            destination != root and (destination.is_relative_to(root) or root.is_relative_to(destination)) for root in (data_destination, runs_destination)
        )
        if overlaps_expected:
            raise ValueError("nested worker mount changes the observed source tree")
        if str(destination) not in expected:
            continue
        if str(destination) in found:
            raise ValueError("worker inspect contains duplicate data mounts")
        found.add(str(destination))
        if destination == data_destination:
            if raw_mount.get("Type") != "bind" or raw_mount.get("RW") is not True or raw_mount.get("Propagation") != "rprivate":
                raise ValueError("worker API data mount must be a private writable bind")
            data_source = normalized_mount_path(raw_mount.get("Source"), field="data mount source")
            continue
        if raw_mount.get("Type") != "volume" or raw_mount.get("RW") is not True:
            raise ValueError("worker runs mount must be a writable named volume")
        runs_volume_name = validate_docker_volume_name(raw_mount.get("Name"))
        runs_mountpoint = normalized_mount_path(raw_mount.get("Source"), field="runs volume mountpoint")
    if found != expected or data_source is None or runs_volume_name is None or runs_mountpoint is None:
        raise ValueError("worker data mounts are incomplete")
    return _WorkerMountEvidence(
        data_source=data_source,
        runs_volume_name=runs_volume_name,
        runs_mountpoint=runs_mountpoint,
    )


def _verify_local_volume(volume: JsonObject, *, evidence: _WorkerMountEvidence) -> None:
    mountpoint = validated_local_volume_mountpoint(volume, expected_name=evidence.runs_volume_name)
    if mountpoint != evidence.runs_mountpoint:
        raise ValueError("volume inspect mountpoint mismatch")
    if mountpoint.is_relative_to(evidence.data_source) or evidence.data_source.is_relative_to(mountpoint):
        raise ValueError("worker runs volume and API data mount overlap")


def _require_real_mount_directory(path: Path, *, field: str) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{field} is unavailable") from exc
    if resolved != path or not resolved.is_dir():
        raise ValueError(f"{field} must be a real directory without symlinks")


def _require_separate_kernel_mounts(
    *,
    identities: Mapping[Path, _KernelMountIdentity],
    data_dir: Path,
    runs_dir: Path,
) -> None:
    data_stat = data_dir.stat(follow_symlinks=False)
    runs_stat = runs_dir.stat(follow_symlinks=False)
    if (data_stat.st_dev, data_stat.st_ino) == (runs_stat.st_dev, runs_stat.st_ino):
        raise ValueError("worker runs and API data mounts alias the same directory")
    data_identity = identities[data_dir]
    runs_identity = identities[runs_dir]
    if data_identity.device == runs_identity.device and (
        runs_identity.root.is_relative_to(data_identity.root) or data_identity.root.is_relative_to(runs_identity.root)
    ):
        raise ValueError("worker runs and API data mounts overlap on the backing filesystem")


def _kernel_mount_identities(
    mountinfo_path: Path,
    *,
    destinations: tuple[Path, Path],
) -> Mapping[Path, _KernelMountIdentity]:
    expected = set(destinations)
    found: dict[Path, _KernelMountIdentity] = {}
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("worker mountinfo is unavailable") from exc
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise ValueError("worker mountinfo is malformed")
        mount_point = Path(_decode_mountinfo_path(fields[4]))
        if mount_point not in expected:
            continue
        if mount_point in found or "rw" not in fields[5].split(","):
            raise ValueError("worker mountinfo data mount is ambiguous or read-only")
        root = Path(_decode_mountinfo_path(fields[3]))
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("worker mountinfo root is invalid")
        found[mount_point] = _KernelMountIdentity(device=fields[2], root=root)
    if set(found) != expected:
        raise ValueError("worker mountinfo does not prove both data mounts")
    return found


def _decode_mountinfo_path(value: str) -> str:
    decoded = value
    for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        decoded = decoded.replace(escaped, literal)
    if "\\" in decoded or any(ord(character) < 32 for character in decoded):
        raise ValueError("worker mountinfo path contains unsupported escapes")
    return decoded


class DockerEngineClient:
    """仅封装 Agent 测试 sandbox 所需的最小 Docker Engine HTTP API。"""

    def __init__(
        self,
        *,
        socket_path: Path = Path("/var/run/docker.sock"),
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url="http://docker",
            transport=httpx.HTTPTransport(uds=str(socket_path)),
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DockerEngineClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def resolve_image_id(self, image_ref: str) -> str:
        payload = self._request_object("GET", f"/images/{quote(image_ref, safe='')}/json", operation="image inspect")
        image_id = payload.get("Id")
        if not isinstance(image_id, str) or _IMAGE_ID_PATTERN.fullmatch(image_id) is None:
            raise DockerEngineProtocolError(code="DOCKER_IMAGE_ID_INVALID", operation="image inspect")
        return image_id

    def create_container(self, *, name: str, config: JsonObject) -> str:
        payload = self._request_object(
            "POST",
            "/containers/create",
            operation="container create",
            params={"name": name},
            json_body=config,
        )
        container_id = payload.get("Id")
        if not isinstance(container_id, str) or _CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
            raise DockerEngineProtocolError(code="DOCKER_CONTAINER_ID_INVALID", operation="container create")
        return container_id

    def inspect_container(self, container_id: str) -> JsonObject:
        _require_container_reference(container_id)
        return self._request_object("GET", f"/containers/{container_id}/json", operation="container inspect")

    def inspect_volume(self, volume_name: str) -> JsonObject:
        validated_name = validate_docker_volume_name(volume_name)
        return self._request_object(
            "GET",
            f"/volumes/{quote(validated_name, safe='')}",
            operation="volume inspect",
        )

    def start_container(self, container_id: str) -> None:
        _require_container_id(container_id)
        self._request_empty("POST", f"/containers/{container_id}/start", operation="container start", allowed_statuses={204, 304})

    def kill_container(self, container_id: str) -> None:
        _require_container_id(container_id)
        self._request_empty(
            "POST",
            f"/containers/{container_id}/kill",
            operation="container kill",
            params={"signal": "KILL"},
            allowed_statuses={204, 304, 409},
        )

    def remove_container(self, container_id: str) -> None:
        _require_container_id(container_id)
        self._request_empty(
            "DELETE",
            f"/containers/{container_id}",
            operation="container remove",
            params={"force": "1", "v": "1"},
            allowed_statuses={204},
        )

    def container_ids_with_label(self, label: str) -> tuple[str, ...]:
        payload = self._request_object_list(
            "GET",
            "/containers/json",
            operation="container list",
            params={"all": "1", "filters": json.dumps({"label": [label]}, separators=(",", ":"))},
        )
        ids: list[str] = []
        for item in payload:
            container_id = item.get("Id")
            if not isinstance(container_id, str) or _CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
                raise DockerEngineProtocolError(code="DOCKER_CONTAINER_LIST_INVALID", operation="container list")
            ids.append(container_id)
        return tuple(ids)

    def read_container_logs(
        self,
        container_id: str,
        *,
        max_bytes_per_stream: int,
        tail_lines: int | None = None,
        include_stderr: bool = True,
    ) -> DockerLogs:
        _require_container_id(container_id)
        if max_bytes_per_stream <= 0:
            raise ValueError("max_bytes_per_stream must be positive")
        if tail_lines is not None and tail_lines <= 0:
            raise ValueError("tail_lines must be positive")
        wire_limit = (max_bytes_per_stream * 2) + 65_536
        payload = bytearray()
        wire_truncated = False
        params = {"stdout": "1", "stderr": "1" if include_stderr else "0"}
        if tail_lines is not None:
            params["tail"] = str(tail_lines)
        try:
            with self._client.stream(
                "GET",
                f"/containers/{container_id}/logs",
                params=params,
            ) as response:
                self._require_status(response, operation="container logs", allowed_statuses={200})
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                multiplexed = content_type in _MULTIPLEXED_LOG_CONTENT_TYPES
                for chunk in response.iter_bytes():
                    remaining = wire_limit - len(payload)
                    if remaining <= 0:
                        wire_truncated = True
                        break
                    payload.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        wire_truncated = True
                        break
        except httpx.HTTPError as exc:
            raise DockerEngineError(code="DOCKER_LOGS_UNAVAILABLE", operation="container logs") from exc
        if multiplexed:
            return _decode_multiplexed_logs(bytes(payload), max_bytes=max_bytes_per_stream, wire_truncated=wire_truncated)
        stdout, truncated = _decode_limited(bytes(payload), max_bytes=max_bytes_per_stream, already_truncated=wire_truncated)
        return DockerLogs(stdout=stdout, stderr="", truncated=truncated)

    def _request_object(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        json_body: JsonObject | None = None,
    ) -> JsonObject:
        response = self._request(method, path, operation=operation, params=params, json_body=json_body)
        try:
            return _JSON_OBJECT.validate_python(response.json())
        except (ValueError, ValidationError) as exc:
            raise DockerEngineProtocolError(code="DOCKER_RESPONSE_INVALID", operation=operation, status_code=response.status_code) from exc

    def _request_object_list(self, method: str, path: str, *, operation: str, params: dict[str, str]) -> list[JsonObject]:
        response = self._request(method, path, operation=operation, params=params)
        try:
            return _JSON_OBJECT_LIST.validate_python(response.json())
        except (ValueError, ValidationError) as exc:
            raise DockerEngineProtocolError(code="DOCKER_RESPONSE_INVALID", operation=operation, status_code=response.status_code) from exc

    def _request_empty(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        allowed_statuses: set[int],
        params: dict[str, str] | None = None,
    ) -> None:
        self._request(method, path, operation=operation, params=params, allowed_statuses=allowed_statuses)

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        json_body: JsonObject | None = None,
        allowed_statuses: set[int] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise DockerEngineError(code="DOCKER_ENGINE_UNAVAILABLE", operation=operation) from exc
        self._require_status(response, operation=operation, allowed_statuses=allowed_statuses or {200, 201})
        return response

    @staticmethod
    def _require_status(response: httpx.Response, *, operation: str, allowed_statuses: set[int]) -> None:
        if response.status_code not in allowed_statuses:
            raise DockerEngineError(code="DOCKER_ENGINE_REQUEST_FAILED", operation=operation, status_code=response.status_code)


def _require_container_id(container_id: str) -> None:
    if _CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
        raise ValueError("container_id must be a full Docker container ID")


def _require_container_reference(container_id: str) -> None:
    if _CONTAINER_REFERENCE_PATTERN.fullmatch(container_id) is None:
        raise ValueError("container_id must be a Docker container ID or unambiguous short ID")


def _decode_multiplexed_logs(payload: bytes, *, max_bytes: int, wire_truncated: bool) -> DockerLogs:
    stdout = bytearray()
    stderr = bytearray()
    offset = 0
    truncated = wire_truncated
    while offset < len(payload):
        if len(payload) - offset < 8:
            truncated = True
            break
        stream_type = payload[offset]
        if payload[offset + 1 : offset + 4] != b"\x00\x00\x00" or stream_type not in {1, 2}:
            raise DockerEngineProtocolError(code="DOCKER_LOG_STREAM_INVALID", operation="container logs")
        frame_size = int.from_bytes(payload[offset + 4 : offset + 8], byteorder="big")
        offset += 8
        frame_end = min(offset + frame_size, len(payload))
        target = stdout if stream_type == 1 else stderr
        remaining = max_bytes - len(target)
        if remaining > 0:
            target.extend(payload[offset : min(frame_end, offset + remaining)])
        incomplete_frame = frame_end - offset < frame_size
        if frame_size > remaining or incomplete_frame:
            truncated = True
        offset = frame_end
        if incomplete_frame:
            break
    stdout_text, stdout_truncated = _decode_limited(bytes(stdout), max_bytes=max_bytes, already_truncated=truncated and len(stdout) >= max_bytes)
    stderr_text, stderr_truncated = _decode_limited(bytes(stderr), max_bytes=max_bytes, already_truncated=truncated and len(stderr) >= max_bytes)
    return DockerLogs(stdout=stdout_text, stderr=stderr_text, truncated=truncated or stdout_truncated or stderr_truncated)


def _decode_limited(payload: bytes, *, max_bytes: int, already_truncated: bool) -> tuple[str, bool]:
    truncated = already_truncated or len(payload) > max_bytes
    text = payload[:max_bytes].decode("utf-8", errors="replace")
    if truncated:
        text += "\n[output truncated]"
    return text, truncated
