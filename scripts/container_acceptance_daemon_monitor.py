"""Continuous Docker mutation and sidecar boundary for formal acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, TypeVar

from scripts.container_acceptance_compose_contract import render_services, verify_running_service
from scripts.container_acceptance_inputs import ContainerIdentity

_E = TypeVar("_E", bound=Exception)
_RunOutput = Callable[[list[str], str], str]
_MAX_EVENTS = 10_000


def _fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


class DockerMutationMonitor:
    """Drain Docker events continuously and bind project/network/mount inventory."""

    def __init__(
        self,
        *,
        docker_path: str,
        project_name: str,
        runtime_root: Path,
        source_root: Path,
        environ: dict[str, str],
        run_output: _RunOutput,
        error_type: type[_E],
    ) -> None:
        self._docker = docker_path
        self._project = project_name
        self._runtime_root = runtime_root.resolve()
        self._source_root = source_root.resolve()
        self._environ = environ
        self._run_output = run_output
        self._error_type = error_type
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._events: list[str] = []
        self._overflow = False
        self._container_ids: frozenset[str] = frozenset()
        self._network_ids: frozenset[str] = frozenset()
        self._inventory: tuple[frozenset[str], tuple[tuple[str, tuple[str, ...]], ...]] | None = None

    def start(self) -> None:
        if self._process is not None:
            _fail(self._error_type, "Docker event monitor 重复启动")
        since = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            process = subprocess.Popen(
                [self._docker, "events", "--since", since, "--format", "{{json .}}"],
                env=self._environ,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            _fail(self._error_type, "无法启动连续 Docker event monitor", exc)
        self._process = process
        self._reader = threading.Thread(target=self._drain, name="agentgov-docker-events", daemon=True)
        self._reader.start()
        for _attempt in range(250):
            if process.poll() is not None:
                self.close()
                _fail(self._error_type, "Docker event monitor 在 ready 前退出")
            if _has_socket_descriptor(process.pid):
                break
            threading.Event().wait(0.02)
        else:
            self.close()
            _fail(self._error_type, "Docker event monitor 未建立 daemon stream")
        until = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        gap = self._run_output(
            [self._docker, "events", "--since", since, "--until", until, "--format", "{{json .}}"],
            "Docker event monitor ready gap",
        )
        self._events.extend(gap.splitlines())

    def bind(self, containers: tuple[ContainerIdentity, ...]) -> None:
        if self._process is None or self._inventory is not None:
            _fail(self._error_type, "Docker event monitor 未按顺序绑定")
        self._container_ids = frozenset(item["container_id"] for item in containers)
        self._inventory = self._capture_inventory()
        self._reject_events()

    def verify(self) -> None:
        if self._process is None or self._inventory is None:
            _fail(self._error_type, "Docker event monitor 缺少完整基线")
        if self._capture_inventory() != self._inventory:
            _fail(self._error_type, "正式验收期间 Docker project/network/mount inventory 发生变化")
        self._stop(require_live=True)
        self._reject_events()

    def close(self) -> None:
        self._stop(require_live=False)

    def _drain(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            if len(self._events) >= _MAX_EVENTS:
                self._overflow = True
            else:
                self._events.append(line.rstrip("\n"))

    def _stop(self, *, require_live: bool) -> None:
        process = self._process
        if process is None:
            return
        early_exit = process.poll() is not None
        if not early_exit:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        stderr = process.stderr.read().strip() if process.stderr is not None else ""
        self._process = None
        if require_live and (early_exit or self._reader is None or self._reader.is_alive() or stderr):
            _fail(self._error_type, "Docker event stream 提前结束或无法完整排空")

    def _reject_events(self) -> None:
        if self._overflow:
            _fail(self._error_type, "Docker event stream 超过可审计上限")
        for line in self._events:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                _fail(self._error_type, "Docker event stream 不是 JSONL", exc)
            if not isinstance(event, Mapping):
                _fail(self._error_type, "Docker event schema 无效")
            if self._relevant(event):
                action = event.get("Action", event.get("status", "unknown"))
                _fail(self._error_type, f"正式验收期间 Docker 状态发生变化: {action}")

    def _relevant(self, event: Mapping[str, object]) -> bool:
        event_type = event.get("Type", event.get("type"))
        action = event.get("Action", event.get("status"))
        if event_type == "container" and isinstance(action, str) and action.partition(":")[0] in {"exec_create", "exec_start", "exec_die", "exec_detach"}:
            # 健康检查和验收内只读命令不改变容器生命周期或镜像身份。
            return False
        if event_type in {"container", "network", "volume", "daemon"}:
            # 正式 child 不包含 Docker 变更；daemon 级拒绝也能闭合从未获得
            # project label/network attachment 的 create/mount/remove sidecar。
            return True
        actor = event.get("Actor")
        attributes = actor.get("Attributes") if isinstance(actor, Mapping) else None
        attrs = attributes if isinstance(attributes, Mapping) else {}
        if attrs.get("com.docker.compose.project") == self._project:
            return True
        identifiers = [event.get("id"), event.get("ID"), attrs.get("container"), attrs.get("containerID")]
        if isinstance(actor, Mapping):
            identifiers.append(actor.get("ID"))
        if any(_matches(value, self._container_ids) for value in identifiers):
            return True
        return event_type == "network" and any(_matches(value, self._network_ids) for value in identifiers)

    def _capture_inventory(self) -> tuple[frozenset[str], tuple[tuple[str, tuple[str, ...]], ...]]:
        project_ids = frozenset(
            self._run_output(
                [self._docker, "ps", "--no-trunc", "-aq", "--filter", f"label=com.docker.compose.project={self._project}"],
                "Docker project container inventory",
            ).splitlines()
        )
        if project_ids != self._container_ids:
            _fail(self._error_type, "Docker project 含非回执容器或缺少回执容器")
        network_ids = tuple(
            self._run_output(
                [self._docker, "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={self._project}"],
                "Docker project network inventory",
            ).splitlines()
        )
        networks: list[tuple[str, tuple[str, ...]]] = []
        for network_id in sorted(network_ids):
            raw = self._run_output(
                [self._docker, "network", "inspect", "--format", "{{json .Containers}}", network_id],
                "Docker project network members",
            )
            try:
                members = json.loads(raw)
            except json.JSONDecodeError as exc:
                _fail(self._error_type, "Docker network member inventory 不是 JSON", exc)
            if members is None:
                member_ids: tuple[str, ...] = ()
            elif isinstance(members, Mapping) and all(isinstance(key, str) for key in members):
                member_ids = tuple(sorted(members))
            else:
                _fail(self._error_type, "Docker network member inventory schema 无效")
            if any(not _matches(item, self._container_ids) for item in member_ids):
                _fail(self._error_type, "Docker project network 含非回执 sidecar")
            networks.append((network_id, member_ids))
        self._network_ids = frozenset(network_ids)
        self._reject_external_mounts()
        return project_ids, tuple(networks)

    def _reject_external_mounts(self) -> None:
        all_ids = self._run_output([self._docker, "ps", "--no-trunc", "-aq"], "Docker 全容器 mount inventory").splitlines()
        if not all_ids:
            return
        raw = self._run_output([self._docker, "container", "inspect", *all_ids], "Docker 全容器 mount inspect")
        try:
            containers = json.loads(raw)
        except json.JSONDecodeError as exc:
            _fail(self._error_type, "Docker mount inventory 不是 JSON", exc)
        if not isinstance(containers, list):
            _fail(self._error_type, "Docker mount inventory schema 无效")
        for container in containers:
            if not isinstance(container, Mapping) or not isinstance(container.get("Id"), str):
                _fail(self._error_type, "Docker mount container schema 无效")
            if _matches(container["Id"], self._container_ids):
                continue
            mounts = container.get("Mounts", [])
            if not isinstance(mounts, list):
                _fail(self._error_type, "Docker mount schema 无效")
            for mount in mounts:
                source = mount.get("Source") if isinstance(mount, Mapping) else None
                if isinstance(source, str) and _inside(Path(source), (self._runtime_root, self._source_root)):
                    _fail(self._error_type, "非回执容器引用本轮隔离 source/runtime root")


def _matches(value: object, candidates: frozenset[str]) -> bool:
    return isinstance(value, str) and bool(value) and any(item.startswith(value) or value.startswith(item) for item in candidates)


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _has_socket_descriptor(pid: int) -> bool:
    try:
        return any(os.readlink(path).startswith("socket:[") for path in Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False


def verify_frozen_running_contract(
    compose: list[str],
    containers: tuple[ContainerIdentity, ...],
    *,
    project_name: str,
    docker_path: str,
    run_output: _RunOutput,
    error_type: type[_E],
) -> None:
    services = render_services(compose, run_output=run_output, error_type=error_type)
    expected = {item["service"] for item in containers}
    if not expected.issubset(services):
        _fail(error_type, "运行容器服务集合逃离冻结 Compose")
    for item in containers:
        verify_running_service(
            service=item["service"],
            service_config=services[item["service"]],
            container_id=item["container_id"],
            expected_image_id=item["image_id"],
            project_name=project_name,
            docker_path=docker_path,
            run_output=run_output,
            error_type=error_type,
        )
