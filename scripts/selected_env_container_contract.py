"""冻结 Compose 与实际 Docker container inspect 的安全配置投影。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.selected_env_container_security import actual_systempaths_unconfined
from scripts.selected_env_operation_contract import SelectedEnvError

_SERVICE_KEYS = frozenset(
    {
        "build",
        "cap_add",
        "cap_drop",
        "command",
        "container_name",
        "depends_on",
        "deploy",
        "devices",
        "entrypoint",
        "environment",
        "expose",
        "extra_hosts",
        "healthcheck",
        "image",
        "init",
        "ipc",
        "labels",
        "logging",
        "network_mode",
        "networks",
        "pid",
        "ports",
        "privileged",
        "profiles",
        "read_only",
        "restart",
        "security_opt",
        "tmpfs",
        "user",
        "volumes",
    }
)
_DURATION = re.compile(r"(?P<value>\d+)(?P<unit>ns|us|ms|s|m|h)")
_DURATION_SCALE = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
_HEALTHCHECK_FIELDS = {
    "test": ("test", "Test", ()),
    "interval": ("interval", "Interval", 0),
    "timeout": ("timeout", "Timeout", 0),
    "start_period": ("start_period", "StartPeriod", 0),
    "start_interval": ("start_interval", "StartInterval", 0),
    "retries": ("retries", "Retries", 0),
}
_HEALTHCHECK_KEYS = frozenset({"disable", *(key for names in _HEALTHCHECK_FIELDS.values() for key in names[:2])})
_COMPOSE_RESERVED_LABEL_PREFIX = "com.docker.compose."
_SYSTEMPATHS_UNCONFINED = "systempaths=unconfined"


@dataclass(frozen=True)
class ImageConfig:
    environment: tuple[tuple[str, str], ...]
    labels: tuple[tuple[str, str], ...]
    user: str
    entrypoint: tuple[str, ...] | None
    command: tuple[str, ...] | None
    healthcheck: tuple[tuple[str, object], ...] = ()
    exposed_ports: tuple[str, ...] = ()


@dataclass(frozen=True, order=True)
class MountContract:
    mount_type: str
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True, order=True)
class PortContract:
    host_ip: str
    published: str
    target: int
    protocol: str


@dataclass(frozen=True, order=True)
class DeviceContract:
    source: str
    target: str
    permissions: str


@dataclass(frozen=True)
class ContainerConfigContract:
    environment: tuple[tuple[str, str], ...]
    labels: tuple[tuple[str, str], ...]
    mounts: tuple[MountContract, ...]
    ports: tuple[PortContract, ...]
    privileged: bool
    cap_add: tuple[str, ...]
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    systempaths_unconfined: bool
    devices: tuple[DeviceContract, ...]
    read_only_rootfs: bool
    network_mode: str
    pid_mode: str
    ipc_mode: str
    user: str
    entrypoint: tuple[str, ...] | None
    command: tuple[str, ...] | None
    container_name: str
    init: bool
    extra_hosts: tuple[tuple[str, str], ...]
    healthcheck: tuple[tuple[str, object], ...]
    restart: tuple[str, int]
    logging: tuple[str, tuple[tuple[str, str], ...]]
    exposed_ports: tuple[str, ...]
    tmpfs: tuple[tuple[str, str], ...]
    networks: tuple[str, ...]
    running: bool
    health_status: str | None


def decode_image_config(value: object, service: str) -> ImageConfig:
    if not isinstance(value, Mapping):
        raise SelectedEnvError(f"本地镜像缺少 Config: {service}")
    return ImageConfig(
        environment=_environment_pairs(value.get("Env"), boundary=f"镜像环境: {service}"),
        labels=_label_pairs(value.get("Labels"), boundary=f"镜像标签: {service}"),
        user=_string(value.get("User"), default="", boundary=f"镜像 user: {service}"),
        entrypoint=_command(value.get("Entrypoint"), boundary=f"镜像 entrypoint: {service}"),
        command=_command(value.get("Cmd"), boundary=f"镜像 command: {service}"),
        healthcheck=_healthcheck(value.get("Healthcheck"), boundary=f"镜像 healthcheck: {service}"),
        exposed_ports=tuple(sorted(_mapping_keys(value.get("ExposedPorts"), boundary=f"镜像 exposed ports: {service}"))),
    )


def verify_container_config(
    rendered: str,
    service_config: object,
    image: ImageConfig,
    *,
    expected_image_id: str,
    project_name: Callable[[], str],
    service: str,
) -> None:
    container = _decode_container(rendered, service)
    if container.get("Image") != expected_image_id:
        raise SelectedEnvError(f"运行容器未使用已核验镜像: {service}")
    expected = _expected_container_config(service_config, image, service, project_name())
    actual = _actual_container_config(container, service)
    if actual != expected:
        raise SelectedEnvError(f"运行容器配置不匹配冻结 Compose: {service}")


def _decode_container(rendered: str, service: str) -> Mapping[str, object]:
    try:
        inspected = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError(f"运行容器配置不是 JSON: {service}") from exc
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise SelectedEnvError(f"运行容器配置无效: {service}")
    return inspected[0]


def _expected_container_config(
    service_config: object,
    image: ImageConfig,
    service: str,
    project_name: str,
) -> ContainerConfigContract:
    if not isinstance(service_config, Mapping):
        raise SelectedEnvError(f"冻结 Compose service 配置无效: {service}")
    _validate_service_config(service_config, service)
    service_labels = _label_pairs(service_config.get("labels"), boundary=f"Compose 标签: {service}")
    if any(key.startswith(_COMPOSE_RESERVED_LABEL_PREFIX) for key, _value in service_labels):
        raise SelectedEnvError(f"冻结 Compose 不得覆盖内部标签: {service}")
    healthcheck = _expected_healthcheck(service_config.get("healthcheck"), image.healthcheck, service)
    security_opt = _string_sequence(service_config.get("security_opt"), boundary=f"Compose security_opt: {service}")
    if security_opt.count(_SYSTEMPATHS_UNCONFINED) > 1:
        raise SelectedEnvError(f"冻结 Compose 重复声明 systempaths: {service}")
    return ContainerConfigContract(
        environment=_merge_pairs(
            image.environment,
            _environment_pairs(service_config.get("environment"), boundary=f"Compose 环境: {service}"),
        ),
        labels=tuple((key, value) for key, value in _merge_pairs(image.labels, service_labels) if not key.startswith(_COMPOSE_RESERVED_LABEL_PREFIX)),
        mounts=_expected_mounts(service_config, service),
        ports=_expected_ports(service_config, service),
        privileged=_boolean(service_config.get("privileged"), default=False, boundary=f"Compose privileged: {service}"),
        cap_add=_capabilities(service_config.get("cap_add"), boundary=f"Compose cap_add: {service}"),
        cap_drop=_capabilities(service_config.get("cap_drop"), boundary=f"Compose cap_drop: {service}"),
        security_opt=tuple(option for option in security_opt if option != _SYSTEMPATHS_UNCONFINED),
        systempaths_unconfined=_SYSTEMPATHS_UNCONFINED in security_opt,
        devices=_expected_devices(service_config.get("devices"), service),
        read_only_rootfs=_boolean(service_config.get("read_only"), default=False, boundary=f"Compose read_only: {service}"),
        network_mode=_expected_network_mode(service_config, project_name, service),
        pid_mode=_string(service_config.get("pid"), default="", boundary=f"Compose pid: {service}"),
        ipc_mode=_string(service_config.get("ipc"), default="private", boundary=f"Compose ipc: {service}"),
        user=_expected_user(service_config, image, service),
        entrypoint=_command_override(service_config, "entrypoint", image.entrypoint, service),
        command=_command_override(service_config, "command", image.command, service),
        container_name=_container_name(service_config.get("container_name"), service),
        init=_boolean(service_config.get("init"), default=False, boundary=f"Compose init: {service}"),
        extra_hosts=_extra_hosts(service_config.get("extra_hosts"), boundary=f"Compose extra_hosts: {service}"),
        healthcheck=healthcheck,
        restart=_expected_restart(service_config.get("restart"), service),
        logging=_logging(service_config.get("logging"), docker=False, service=service),
        exposed_ports=_expected_exposed_ports(service_config, image.exposed_ports, service),
        tmpfs=_expected_tmpfs(service_config.get("tmpfs"), service),
        networks=_expected_networks(service_config, project_name, service),
        running=True,
        health_status="healthy" if healthcheck else None,
    )


def _actual_container_config(
    container: Mapping[str, object],
    service: str,
) -> ContainerConfigContract:
    config = container.get("Config")
    host_config = container.get("HostConfig")
    network_settings = container.get("NetworkSettings")
    state = container.get("State")
    if not isinstance(config, Mapping) or not isinstance(host_config, Mapping) or not isinstance(network_settings, Mapping) or not isinstance(state, Mapping):
        raise SelectedEnvError(f"运行容器缺少 Config/HostConfig/NetworkSettings/State: {service}")
    labels = tuple(
        (key, value)
        for key, value in _label_pairs(config.get("Labels"), boundary=f"运行容器标签: {service}")
        if not key.startswith(_COMPOSE_RESERVED_LABEL_PREFIX)
    )
    return ContainerConfigContract(
        environment=_environment_pairs(config.get("Env"), boundary=f"运行容器环境: {service}"),
        labels=labels,
        mounts=_actual_mounts(container, host_config, service),
        ports=_actual_ports(host_config, service),
        privileged=_boolean(host_config.get("Privileged"), default=False, boundary=f"运行容器 privileged: {service}"),
        cap_add=_capabilities(host_config.get("CapAdd"), boundary=f"运行容器 CapAdd: {service}"),
        cap_drop=_capabilities(host_config.get("CapDrop"), boundary=f"运行容器 CapDrop: {service}"),
        security_opt=_string_sequence(host_config.get("SecurityOpt"), boundary=f"运行容器 SecurityOpt: {service}"),
        systempaths_unconfined=actual_systempaths_unconfined(host_config, service),
        devices=_actual_devices(host_config.get("Devices"), service),
        read_only_rootfs=_boolean(host_config.get("ReadonlyRootfs"), default=False, boundary=f"运行容器 rootfs: {service}"),
        network_mode=_string(host_config.get("NetworkMode"), default="", boundary=f"运行容器 network: {service}"),
        pid_mode=_string(host_config.get("PidMode"), default="", boundary=f"运行容器 pid: {service}"),
        ipc_mode=_string(host_config.get("IpcMode"), default="private", boundary=f"运行容器 ipc: {service}"),
        user=_string(config.get("User"), default="", boundary=f"运行容器 user: {service}"),
        entrypoint=_command(config.get("Entrypoint"), boundary=f"运行容器 entrypoint: {service}"),
        command=_command(config.get("Cmd"), boundary=f"运行容器 command: {service}"),
        container_name=_actual_container_name(container.get("Name"), service),
        init=_boolean(host_config.get("Init"), default=False, boundary=f"运行容器 init: {service}"),
        extra_hosts=_extra_hosts(host_config.get("ExtraHosts"), boundary=f"运行容器 ExtraHosts: {service}"),
        healthcheck=_complete_healthcheck(_healthcheck(config.get("Healthcheck"), boundary=f"运行容器 healthcheck: {service}")),
        restart=_actual_restart(host_config.get("RestartPolicy"), service),
        logging=_logging(host_config.get("LogConfig"), docker=True, service=service),
        exposed_ports=tuple(sorted(_mapping_keys(config.get("ExposedPorts"), boundary=f"运行容器 exposed ports: {service}"))),
        tmpfs=_actual_tmpfs(host_config.get("Tmpfs"), service),
        networks=tuple(sorted(_mapping_keys(network_settings.get("Networks"), boundary=f"运行容器 networks: {service}"))),
        running=_boolean(state.get("Running"), default=False, boundary=f"运行容器 state: {service}"),
        health_status=_actual_health_status(state.get("Health"), service),
    )


def _validate_service_config(service_config: Mapping[str, object], service: str) -> None:
    unknown = set(service_config) - _SERVICE_KEYS
    if unknown:
        names = ",".join(sorted(str(item) for item in unknown))
        raise SelectedEnvError(f"冻结 Compose service {service} 含未建模字段: {names}")
    if not _supported_deploy(service_config.get("deploy")):
        raise SelectedEnvError(f"冻结 Compose service {service} 含未建模 deploy 约束")


def _supported_deploy(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping) or set(value) - {"replicas", "resources", "placement"}:
        return False
    replicas = value.get("replicas", 1)
    return type(replicas) is int and replicas == 1 and value.get("resources") in (None, {}) and value.get("placement") in (None, {})


def _container_name(value: object, service: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise SelectedEnvError(f"Compose container_name 结构无效: {service}")
    return value


def _actual_container_name(value: object, service: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or len(value) == 1:
        raise SelectedEnvError(f"运行容器 name 结构无效: {service}")
    return value[1:]


def _actual_health_status(value: object, service: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("Status"), str):
        raise SelectedEnvError(f"运行容器 health state 结构无效: {service}")
    status = value["Status"]
    if status not in {"starting", "healthy", "unhealthy"}:
        raise SelectedEnvError(f"运行容器 health status 无效: {service}")
    return status


def _extra_hosts(value: object, *, boundary: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SelectedEnvError(f"{boundary}结构无效")
    result: list[tuple[str, str]] = []
    for item in value:
        separator = "=" if "=" in item else ":"
        host, found, address = item.partition(separator)
        if not found or not host or not address:
            raise SelectedEnvError(f"{boundary}结构无效")
        result.append((host, address))
    if len(set(result)) != len(result):
        raise SelectedEnvError(f"{boundary}含重复项")
    return tuple(sorted(result))


def _duration(value: object, *, boundary: str) -> int:
    if value in (None, 0, "0s"):
        return 0
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, str):
        raise SelectedEnvError(f"{boundary} duration 结构无效")
    position = 0
    total = 0
    for match in _DURATION.finditer(value):
        if match.start() != position:
            raise SelectedEnvError(f"{boundary} duration 无法解析")
        total += int(match.group("value")) * _DURATION_SCALE[match.group("unit")]
        position = match.end()
    if position != len(value):
        raise SelectedEnvError(f"{boundary} duration 无法解析")
    return total


def _healthcheck(value: object, *, boundary: str) -> tuple[tuple[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise SelectedEnvError(f"{boundary}结构无效")
    unknown = set(value) - _HEALTHCHECK_KEYS
    if unknown:
        raise SelectedEnvError(f"{boundary}含未建模字段: {','.join(sorted(str(item) for item in unknown))}")
    result: dict[str, object] = {}
    for canonical, (compose_key, docker_key, _default) in _HEALTHCHECK_FIELDS.items():
        present = [key for key in (compose_key, docker_key) if key in value]
        if len(present) > 1:
            raise SelectedEnvError(f"{boundary}{canonical} 重复声明")
        if not present:
            continue
        raw = value[present[0]]
        if canonical == "test":
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise SelectedEnvError(f"{boundary}test 结构无效")
            result[canonical] = tuple(raw)
        elif canonical == "retries":
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise SelectedEnvError(f"{boundary}retries 结构无效")
            result[canonical] = raw
        else:
            result[canonical] = _duration(raw, boundary=boundary)
    if "disable" in value:
        disabled = value["disable"]
        if not isinstance(disabled, bool):
            raise SelectedEnvError(f"{boundary}disable 结构无效")
        if disabled:
            if result.get("test", ("NONE",)) != ("NONE",):
                raise SelectedEnvError(f"{boundary}disable 与 test 冲突")
            result["test"] = ("NONE",)
    return tuple(sorted(result.items()))


def _complete_healthcheck(value: tuple[tuple[str, object], ...]) -> tuple[tuple[str, object], ...]:
    if not value:
        return ()
    supplied = dict(value)
    return tuple((name, supplied.get(name, default)) for name, (*_keys, default) in _HEALTHCHECK_FIELDS.items())


def _expected_healthcheck(
    service_value: object,
    image_value: tuple[tuple[str, object], ...],
    service: str,
) -> tuple[tuple[str, object], ...]:
    combined = dict(image_value)
    if service_value is not None:
        service_healthcheck = dict(_healthcheck(service_value, boundary=f"Compose healthcheck: {service}"))
        test = service_healthcheck.get("test")
        if isinstance(test, tuple):
            service_healthcheck["test"] = tuple(part.replace("$$", "$") for part in test)
        combined.update(service_healthcheck)
    return _complete_healthcheck(tuple(combined.items()))


def _expected_restart(value: object, service: str) -> tuple[str, int]:
    if value is None:
        return "no", 0
    if not isinstance(value, str):
        raise SelectedEnvError(f"Compose restart 结构无效: {service}")
    name, separator, maximum = value.partition(":")
    if name not in {"no", "always", "on-failure", "unless-stopped"}:
        raise SelectedEnvError(f"Compose restart 结构无效: {service}")
    try:
        count = int(maximum) if separator else 0
    except ValueError as exc:
        raise SelectedEnvError(f"Compose restart 结构无效: {service}") from exc
    if count < 0 or separator and name != "on-failure":
        raise SelectedEnvError(f"Compose restart 结构无效: {service}")
    return name, count


def _actual_restart(value: object, service: str) -> tuple[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"Name", "MaximumRetryCount"}:
        raise SelectedEnvError(f"运行容器 RestartPolicy 结构无效: {service}")
    name = value.get("Name")
    count = value.get("MaximumRetryCount")
    if not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise SelectedEnvError(f"运行容器 RestartPolicy 结构无效: {service}")
    return name, count


def _logging(
    value: object,
    *,
    docker: bool,
    service: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not isinstance(value, Mapping):
        raise SelectedEnvError(f"{'运行容器' if docker else 'Compose'} logging 结构无效: {service}")
    driver_key, options_key = ("Type", "Config") if docker else ("driver", "options")
    if set(value) != {driver_key, options_key}:
        raise SelectedEnvError(f"{'运行容器' if docker else 'Compose'} logging 结构无效: {service}")
    driver = value.get(driver_key)
    options = value.get(options_key)
    if not isinstance(driver, str) or not driver or not isinstance(options, Mapping):
        raise SelectedEnvError(f"{'运行容器' if docker else 'Compose'} logging 结构无效: {service}")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in options.items()):
        raise SelectedEnvError(f"{'运行容器' if docker else 'Compose'} logging options 无效: {service}")
    return driver, tuple(sorted(options.items()))


def _mapping_keys(value: object, *, boundary: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SelectedEnvError(f"{boundary}结构无效")
    return tuple(value)


def _expected_exposed_ports(
    service_config: Mapping[str, object],
    image_ports: tuple[str, ...],
    service: str,
) -> tuple[str, ...]:
    ports = set(image_ports)
    expose = service_config.get("expose") or []
    if not isinstance(expose, list) or not all(isinstance(item, str) and item for item in expose):
        raise SelectedEnvError(f"Compose expose 结构无效: {service}")
    ports.update(item if "/" in item else f"{item}/tcp" for item in expose)
    published = service_config.get("ports") or []
    if not isinstance(published, list):
        raise SelectedEnvError(f"Compose ports 结构无效: {service}")
    for item in published:
        if not isinstance(item, Mapping) or not isinstance(item.get("target"), int):
            raise SelectedEnvError(f"Compose ports 结构无效: {service}")
        ports.add(f"{item['target']}/{item.get('protocol', 'tcp')}")
    return tuple(sorted(ports))


def _expected_tmpfs(value: object, service: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SelectedEnvError(f"Compose tmpfs 结构无效: {service}")
    result: list[tuple[str, str]] = []
    for item in value:
        path, separator, options = item.partition(":")
        if not path:
            raise SelectedEnvError(f"Compose tmpfs 结构无效: {service}")
        result.append((path, options if separator else ""))
    return tuple(sorted(result))


def _actual_tmpfs(value: object, service: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise SelectedEnvError(f"运行容器 Tmpfs 结构无效: {service}")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise SelectedEnvError(f"运行容器 Tmpfs 结构无效: {service}")
    return tuple(sorted(value.items()))


def _expected_networks(
    service_config: Mapping[str, object],
    project_name: str,
    service: str,
) -> tuple[str, ...]:
    if service_config.get("network_mode") is not None:
        return ()
    value = service_config.get("networks")
    if value is None:
        return (f"{project_name}_default",)
    if not isinstance(value, Mapping) or tuple(value) != ("default",):
        raise SelectedEnvError(f"Compose networks 仅支持单 default network: {service}")
    return (f"{project_name}_default",)


def _expected_network_mode(
    service_config: Mapping[str, object],
    project_name: str,
    service: str,
) -> str:
    explicit = service_config.get("network_mode")
    if explicit is not None:
        return _string(explicit, default="", boundary=f"Compose network_mode: {service}")
    raw_networks = service_config.get("networks")
    if raw_networks is None:
        return f"{project_name}_default"
    if isinstance(raw_networks, Mapping) or isinstance(raw_networks, list) and all(isinstance(item, str) for item in raw_networks):
        network_names = tuple(raw_networks)
    else:
        raise SelectedEnvError(f"Compose networks 结构无效: {service}")
    if network_names == ("default",):
        return f"{project_name}_default"
    raise SelectedEnvError(f"Compose multi/custom network 尚不能精确投影: {service}")


def _expected_user(service_config: Mapping[str, object], image: ImageConfig, service: str) -> str:
    configured = service_config.get("user")
    if configured in (None, ""):
        return image.user
    return _string(configured, default="", boundary=f"Compose user: {service}")


def _command_override(
    service_config: Mapping[str, object],
    field: str,
    image_value: tuple[str, ...] | None,
    service: str,
) -> tuple[str, ...] | None:
    configured = service_config.get(field)
    if configured is None:
        return image_value
    return _command(configured, boundary=f"Compose {field}: {service}")


def _command(value: object, *, boundary: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SelectedEnvError(f"{boundary}结构无效")
    return tuple(value)


def _string(value: object, *, default: str, boundary: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise SelectedEnvError(f"{boundary}结构无效")
    return value


def _boolean(value: object, *, default: bool, boundary: str) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise SelectedEnvError(f"{boundary}结构无效")
    return value


def _string_sequence(value: object, *, boundary: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SelectedEnvError(f"{boundary}结构无效")
    return tuple(sorted(value))


def _capabilities(value: object, *, boundary: str) -> tuple[str, ...]:
    capabilities = _string_sequence(value, boundary=boundary)
    return tuple(sorted(item.removeprefix("CAP_").upper() for item in capabilities))


def _environment_pairs(value: object, *, boundary: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, list):
        pairs = []
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                raise SelectedEnvError(f"{boundary}结构无效")
            pairs.append(tuple(item.split("=", 1)))
    else:
        raise SelectedEnvError(f"{boundary}结构无效")
    normalized: dict[str, str] = {}
    for key, item_value in pairs:
        if not isinstance(key, str) or not key or not isinstance(item_value, str) or key in normalized:
            raise SelectedEnvError(f"{boundary}结构无效")
        normalized[key] = item_value
    return tuple(sorted(normalized.items()))


def _label_pairs(value: object, *, boundary: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise SelectedEnvError(f"{boundary}结构无效")
    labels: list[tuple[str, str]] = []
    for key, item_value in value.items():
        if not isinstance(key, str) or not key or not isinstance(item_value, str):
            raise SelectedEnvError(f"{boundary}结构无效")
        labels.append((key, item_value))
    return tuple(sorted(labels))


def _merge_pairs(
    base: tuple[tuple[str, str], ...],
    override: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    merged = dict(base)
    merged.update(override)
    return tuple(sorted(merged.items()))


def _expected_devices(value: object, service: str) -> tuple[DeviceContract, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SelectedEnvError(f"Compose devices 结构无效: {service}")
    devices: list[DeviceContract] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise SelectedEnvError(f"Compose device 结构无效: {service}")
        source = item.get("source")
        target = item.get("target")
        permissions = item.get("permissions", "rwm")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(permissions, str):
            raise SelectedEnvError(f"Compose device 结构无效: {service}")
        devices.append(DeviceContract(source, target, permissions))
    return tuple(sorted(devices))


def _actual_devices(value: object, service: str) -> tuple[DeviceContract, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SelectedEnvError(f"运行容器 devices 结构无效: {service}")
    devices: list[DeviceContract] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise SelectedEnvError(f"运行容器 device 结构无效: {service}")
        source = item.get("PathOnHost")
        target = item.get("PathInContainer")
        permissions = item.get("CgroupPermissions")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(permissions, str):
            raise SelectedEnvError(f"运行容器 device 结构无效: {service}")
        devices.append(DeviceContract(source, target, permissions))
    return tuple(sorted(devices))


def _expected_mounts(service_config: Mapping[str, object], service: str) -> tuple[MountContract, ...]:
    mounts: list[MountContract] = []
    raw_volumes = service_config.get("volumes") or []
    if not isinstance(raw_volumes, list):
        raise SelectedEnvError(f"Compose mounts 结构无效: {service}")
    for raw_mount in raw_volumes:
        if not isinstance(raw_mount, Mapping):
            raise SelectedEnvError(f"Compose mount 结构无效: {service}")
        mount_type = raw_mount.get("type")
        source = raw_mount.get("source")
        target = raw_mount.get("target")
        read_only = raw_mount.get("read_only", False)
        if mount_type not in {"bind", "volume"} or not isinstance(source, str) or not isinstance(target, str):
            raise SelectedEnvError(f"Compose mount 结构无效: {service}")
        if type(read_only) is not bool:
            raise SelectedEnvError(f"Compose mount 结构无效: {service}")
        mounts.append(MountContract(mount_type, _mount_source(mount_type, source), target, read_only))
    raw_tmpfs = service_config.get("tmpfs") or []
    if not isinstance(raw_tmpfs, list) or not all(isinstance(target, str) and target for target in raw_tmpfs):
        raise SelectedEnvError(f"Compose tmpfs 结构无效: {service}")
    mounts.extend(MountContract("tmpfs", "", target, False) for target in raw_tmpfs)
    return _unique_mounts(mounts, service)


def _actual_mounts(
    container: Mapping[str, object],
    host_config: Mapping[str, object],
    service: str,
) -> tuple[MountContract, ...]:
    raw_mounts = container.get("Mounts") or []
    if not isinstance(raw_mounts, list):
        raise SelectedEnvError(f"运行容器 mounts 结构无效: {service}")
    mounts: list[MountContract] = []
    for raw_mount in raw_mounts:
        if not isinstance(raw_mount, Mapping):
            raise SelectedEnvError(f"运行容器 mount 结构无效: {service}")
        mount_type = raw_mount.get("Type")
        source = raw_mount.get("Name") if mount_type == "volume" else raw_mount.get("Source")
        target = raw_mount.get("Destination")
        writable = raw_mount.get("RW")
        if mount_type == "tmpfs":
            continue
        if mount_type not in {"bind", "volume"} or not isinstance(source, str) or not isinstance(target, str):
            raise SelectedEnvError(f"运行容器 mount 结构无效: {service}")
        if type(writable) is not bool:
            raise SelectedEnvError(f"运行容器 mount 结构无效: {service}")
        mounts.append(MountContract(mount_type, _mount_source(mount_type, source), target, not writable))
    raw_tmpfs = host_config.get("Tmpfs") or {}
    if not isinstance(raw_tmpfs, Mapping) or not all(isinstance(target, str) and target for target in raw_tmpfs):
        raise SelectedEnvError(f"运行容器 tmpfs 结构无效: {service}")
    mounts.extend(MountContract("tmpfs", "", target, False) for target in raw_tmpfs)
    return _unique_mounts(mounts, service)


def _unique_mounts(mounts: list[MountContract], service: str) -> tuple[MountContract, ...]:
    by_target = {mount.target: mount for mount in mounts}
    if len(by_target) != len(mounts):
        raise SelectedEnvError(f"容器存在重复 mount target: {service}")
    return tuple(sorted(mounts))


def _mount_source(mount_type: object, source: str) -> str:
    if mount_type == "bind":
        if not source.startswith("/"):
            raise SelectedEnvError("Docker bind mount source 必须是绝对路径")
        return Path(source).resolve().as_posix()
    return source


def _expected_ports(service_config: Mapping[str, object], service: str) -> tuple[PortContract, ...]:
    raw_ports = service_config.get("ports") or []
    if not isinstance(raw_ports, list):
        raise SelectedEnvError(f"Compose ports 结构无效: {service}")
    ports: list[PortContract] = []
    for raw_port in raw_ports:
        if not isinstance(raw_port, Mapping):
            raise SelectedEnvError(f"Compose port 结构无效: {service}")
        target = raw_port.get("target")
        published = raw_port.get("published")
        host_ip = raw_port.get("host_ip", "0.0.0.0")
        protocol = raw_port.get("protocol", "tcp")
        valid = type(target) is int and isinstance(published, (str, int)) and isinstance(host_ip, str)
        if not valid or protocol not in {"tcp", "udp", "sctp"}:
            raise SelectedEnvError(f"Compose port 结构无效: {service}")
        ports.append(PortContract(host_ip or "0.0.0.0", str(published), target, protocol))
    return tuple(sorted(ports))


def _actual_ports(host_config: Mapping[str, object], service: str) -> tuple[PortContract, ...]:
    raw_bindings = host_config.get("PortBindings") or {}
    if not isinstance(raw_bindings, Mapping):
        raise SelectedEnvError(f"运行容器 ports 结构无效: {service}")
    ports: list[PortContract] = []
    for target_protocol, bindings in raw_bindings.items():
        if bindings is None:
            continue
        if not isinstance(target_protocol, str) or "/" not in target_protocol or not isinstance(bindings, list):
            raise SelectedEnvError(f"运行容器 port 结构无效: {service}")
        target_text, protocol = target_protocol.rsplit("/", 1)
        if not target_text.isdigit() or protocol not in {"tcp", "udp", "sctp"}:
            raise SelectedEnvError(f"运行容器 port 结构无效: {service}")
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise SelectedEnvError(f"运行容器 port binding 结构无效: {service}")
            host_ip = binding.get("HostIp")
            published = binding.get("HostPort")
            if not isinstance(host_ip, str) or not isinstance(published, str) or not published:
                raise SelectedEnvError(f"运行容器 port binding 结构无效: {service}")
            ports.append(PortContract(host_ip or "0.0.0.0", published, int(target_text), protocol))
    return tuple(sorted(ports))
