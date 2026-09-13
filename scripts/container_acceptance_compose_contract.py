"""Formal acceptance image inventory and rendered-to-running Compose contract."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import NoReturn, TypeVar

from scripts import selected_env_container_contract as selected_contract
from scripts.selected_env_image_inventory import build_base_image_references
from scripts.selected_env_operation_contract import DIGEST_REFERENCE, IMAGE_ID, SelectedEnvError

_E = TypeVar("_E", bound=Exception)
_RunOutput = Callable[[list[str], str], str]
ComposeServices = dict[str, object]
ImageInventory = dict[str, str]
HealthcheckContract = dict[str, object]
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
_DURATION_SCALE = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000, "h": 3_600_000_000_000}
_HEALTHCHECK_FIELDS = {
    "test": ("test", "Test", ()),
    "interval": ("interval", "Interval", 0),
    "timeout": ("timeout", "Timeout", 0),
    "start_period": ("start_period", "StartPeriod", 0),
    "start_interval": ("start_interval", "StartInterval", 0),
    "retries": ("retries", "Retries", 0),
}
_HEALTHCHECK_KEYS = frozenset({"disable", *(key for names in _HEALTHCHECK_FIELDS.values() for key in names[:2])})


def _fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def parse_container_created(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    match = re.fullmatch(r"(?P<prefix>.+\.\d{6})\d*(?P<offset>[+-]\d\d:\d\d)", normalized)
    if match:
        normalized = match.group("prefix") + match.group("offset")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("容器创建时间不可解析") from exc
    if parsed.tzinfo is None:
        raise ValueError("容器创建时间缺少时区")
    return parsed


def render_services(
    compose: list[str],
    *,
    run_output: _RunOutput,
    error_type: type[_E],
) -> ComposeServices:
    raw = run_output([*compose, "config", "--format", "json"], "Compose 完整配置冻结")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(error_type, "Compose 完整配置不是 JSON", exc)
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict) or not services:
        _fail(error_type, "Compose 完整配置缺少 services")
    result: dict[str, object] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            _fail(error_type, "Compose service schema 无效")
        unknown = set(service) - _SERVICE_KEYS
        if unknown:
            _fail(error_type, f"Compose service {name} 含未建模字段: {','.join(sorted(unknown))}")
        deploy = service.get("deploy")
        if deploy not in (None, {}, {"replicas": 1, "resources": {}, "placement": {}}):
            _fail(error_type, f"Compose service {name} 含未建模 deploy 约束")
        result[name] = service
    return result


def required_external_images(
    services: Mapping[str, object],
    *,
    build_services: tuple[str, ...],
    source_root: Path,
    error_type: type[_E],
) -> ImageInventory:
    try:
        references = dict(build_base_image_references(source_root))
    except SelectedEnvError as exc:
        _fail(error_type, str(exc), exc)
    for name, raw_service in services.items():
        if name in build_services:
            continue
        if not isinstance(raw_service, Mapping):
            _fail(error_type, f"Compose service {name} schema 无效")
        reference = raw_service.get("image")
        if not isinstance(reference, str) or DIGEST_REFERENCE.fullmatch(reference) is None:
            _fail(error_type, f"第三方镜像必须使用 digest pin: {name}")
        references[f"service:{name}"] = reference
    return references


def capture_image_inventory(
    references: Mapping[str, str],
    *,
    docker_path: str,
    run_output: _RunOutput,
    error_type: type[_E],
) -> ImageInventory:
    inventory: dict[str, str] = {}
    by_reference: dict[str, str] = {}
    for name, reference in sorted(references.items()):
        image_id = by_reference.get(reference)
        if image_id is None:
            image_id = run_output(
                [docker_path, "image", "inspect", "--format", "{{.Id}}", reference],
                f"本地镜像 inventory {name}",
            )
            if IMAGE_ID.fullmatch(image_id) is None:
                _fail(error_type, f"本地镜像 identity 无效: {name}")
            by_reference[reference] = image_id
        inventory[name] = image_id
    if set(inventory) != set(references):
        _fail(error_type, "正式验收外部镜像 inventory 不完整")
    return inventory


def capture_service_image_ids(
    services: Mapping[str, object],
    *,
    docker_path: str,
    run_output: _RunOutput,
    error_type: type[_E],
) -> ImageInventory:
    result: dict[str, str] = {}
    for name, raw_service in sorted(services.items()):
        reference = raw_service.get("image") if isinstance(raw_service, Mapping) else None
        if not isinstance(reference, str) or not reference:
            _fail(error_type, f"Compose service {name} 缺少 image identity")
        image_id = run_output(
            [docker_path, "image", "inspect", "--format", "{{.Id}}", reference],
            f"Compose service 镜像 identity {name}",
        )
        if IMAGE_ID.fullmatch(image_id) is None:
            _fail(error_type, f"Compose service 镜像 identity 无效: {name}")
        result[name] = image_id
    return result


def verify_running_service(
    *,
    service: str,
    service_config: object,
    container_id: str,
    expected_image_id: str,
    project_name: str,
    docker_path: str,
    run_output: _RunOutput,
    error_type: type[_E],
) -> None:
    container_raw = run_output([docker_path, "container", "inspect", container_id], f"运行容器完整配置 {service}")
    image_raw = run_output([docker_path, "image", "inspect", expected_image_id], f"运行镜像完整配置 {service}")
    try:
        container_values = json.loads(container_raw)
        image_values = json.loads(image_raw)
        container = _single_mapping(container_values, f"运行容器 {service}")
        image = _single_mapping(image_values, f"运行镜像 {service}")
        image_config_value = image.get("Config")
        image_config = selected_contract.decode_image_config(image_config_value, service)
        selected_contract.verify_container_config(
            container_raw,
            service_config,
            image_config,
            expected_image_id=expected_image_id,
            project_name=lambda: project_name,
            service=service,
        )
        _verify_supplemental(service_config, image_config_value, container, project_name, service)
    except (json.JSONDecodeError, SelectedEnvError, ValueError, TypeError) as exc:
        _fail(error_type, f"运行容器完整配置不匹配冻结 Compose: {service}", exc)


def _single_mapping(value: object, boundary: str) -> Mapping[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        raise ValueError(f"{boundary} inspect schema 无效")
    return value[0]


def _verify_supplemental(
    service_value: object,
    image_config_value: object,
    container: Mapping[str, object],
    project_name: str,
    service: str,
) -> None:
    if not isinstance(service_value, Mapping) or not isinstance(image_config_value, Mapping):
        raise ValueError("Compose/image service schema 无效")
    config = container.get("Config")
    host = container.get("HostConfig")
    networks = container.get("NetworkSettings")
    if not isinstance(config, Mapping) or not isinstance(host, Mapping) or not isinstance(networks, Mapping):
        raise ValueError("container Config/HostConfig/NetworkSettings 缺失")
    expected_name = service_value.get("container_name")
    if not isinstance(expected_name, str) or container.get("Name") != f"/{expected_name}":
        raise ValueError("container_name 不一致")
    expected = {
        "init": bool(service_value.get("init", False)),
        "extra_hosts": _extra_hosts(service_value.get("extra_hosts")),
        "healthcheck": _expected_healthcheck(service_value.get("healthcheck"), image_config_value.get("Healthcheck")),
        "restart": _expected_restart(service_value.get("restart")),
        "logging": _expected_logging(service_value.get("logging")),
        "exposed_ports": _expected_exposed_ports(service_value, image_config_value.get("ExposedPorts")),
        "tmpfs": _expected_tmpfs(service_value.get("tmpfs")),
        "networks": _expected_networks(service_value.get("networks"), project_name),
    }
    actual = {
        "init": bool(host.get("Init", False)),
        "extra_hosts": _extra_hosts(host.get("ExtraHosts")),
        "healthcheck": _actual_healthcheck(config.get("Healthcheck")),
        "restart": _actual_restart(host.get("RestartPolicy")),
        "logging": _actual_logging(host.get("LogConfig")),
        "exposed_ports": tuple(sorted(_mapping_keys(config.get("ExposedPorts"), "ExposedPorts"))),
        "tmpfs": _actual_tmpfs(host.get("Tmpfs")),
        "networks": tuple(sorted(_mapping_keys(networks.get("Networks"), "Networks"))),
    }
    if actual != expected:
        raise ValueError(f"supplemental container config mismatch: {service}")


def _mapping_keys(value: object, boundary: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{boundary} schema 无效")
    return tuple(value)


def _extra_hosts(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("extra_hosts schema 无效")
    result = []
    for item in value:
        separator = "=" if "=" in item else ":"
        host, address = item.split(separator, 1)
        result.append((host, address))
    return tuple(sorted(result))


def _duration(value: object) -> int:
    if value in (None, 0, "0s"):
        return 0
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, str):
        raise ValueError("healthcheck duration schema 无效")
    position = 0
    total = 0
    for match in _DURATION.finditer(value):
        if match.start() != position:
            raise ValueError("healthcheck duration 无法解析")
        total += int(match.group("value")) * _DURATION_SCALE[match.group("unit")]
        position = match.end()
    if position != len(value):
        raise ValueError("healthcheck duration 无法解析")
    return total


def _healthcheck(value: object) -> HealthcheckContract:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("healthcheck schema 无效")
    unknown = set(value) - _HEALTHCHECK_KEYS
    if unknown:
        raise ValueError(f"healthcheck 含未建模字段: {','.join(sorted(str(item) for item in unknown))}")
    result: dict[str, object] = {}
    for canonical, (compose_key, docker_key, _default) in _HEALTHCHECK_FIELDS.items():
        present = [key for key in (compose_key, docker_key) if key in value]
        if len(present) > 1:
            raise ValueError(f"healthcheck {canonical} 重复声明")
        if not present:
            continue
        raw = value[present[0]]
        if canonical == "test":
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValueError("healthcheck test schema 无效")
            result[canonical] = tuple(raw)
        elif canonical == "retries":
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError("healthcheck retries schema 无效")
            result[canonical] = raw
        else:
            result[canonical] = _duration(raw)
    if "disable" in value:
        disabled = value["disable"]
        if not isinstance(disabled, bool):
            raise ValueError("healthcheck disable schema 无效")
        if disabled:
            if result.get("test", ("NONE",)) != ("NONE",):
                raise ValueError("healthcheck disable 与 test 冲突")
            result["test"] = ("NONE",)
    return result


def _complete_healthcheck(value: Mapping[str, object]) -> HealthcheckContract:
    if not value:
        return {}
    return {name: value.get(name, default) for name, (*_keys, default) in _HEALTHCHECK_FIELDS.items()}


def _expected_healthcheck(service: object, image: object) -> HealthcheckContract:
    base = _healthcheck(image)
    if service is None:
        return _complete_healthcheck(base)
    base.update(_healthcheck(service))
    return _complete_healthcheck(base)


def _actual_healthcheck(value: object) -> HealthcheckContract:
    return _complete_healthcheck(_healthcheck(value))


def _expected_restart(value: object) -> tuple[str, int]:
    if value is None:
        return ("no", 0)
    if not isinstance(value, str):
        raise ValueError("restart schema 无效")
    name, separator, maximum = value.partition(":")
    return (name, int(maximum) if separator else 0)


def _actual_restart(value: object) -> tuple[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("RestartPolicy schema 无效")
    return (str(value.get("Name", "no")), int(value.get("MaximumRetryCount", 0)))


def _expected_logging(value: object) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("driver"), str):
        raise ValueError("logging 必须显式声明")
    options = value.get("options", {})
    if not isinstance(options, Mapping) or not all(isinstance(key, str) and isinstance(item, str) for key, item in options.items()):
        raise ValueError("logging options schema 无效")
    return (value["driver"], tuple(sorted(options.items())))


def _actual_logging(value: object) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("LogConfig schema 无效")
    return _expected_logging({"driver": value.get("Type"), "options": value.get("Config", {})})


def _expected_exposed_ports(service: Mapping[str, object], image_value: object) -> tuple[str, ...]:
    ports = set(_mapping_keys(image_value, "image ExposedPorts"))
    expose = service.get("expose", [])
    if not isinstance(expose, list) or not all(isinstance(item, str) for item in expose):
        raise ValueError("expose schema 无效")
    ports.update(item if "/" in item else f"{item}/tcp" for item in expose)
    published = service.get("ports", [])
    if not isinstance(published, list):
        raise ValueError("ports schema 无效")
    for item in published:
        if not isinstance(item, Mapping) or not isinstance(item.get("target"), int):
            raise ValueError("ports schema 无效")
        ports.add(f"{item['target']}/{item.get('protocol', 'tcp')}")
    return tuple(sorted(ports))


def _expected_tmpfs(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("tmpfs schema 无效")
    result: list[tuple[str, str]] = []
    for item in value:
        path, separator, options = item.partition(":")
        result.append((path, options if separator else ""))
    return tuple(sorted(result))


def _actual_tmpfs(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError("Tmpfs schema 无效")
    return tuple(sorted(value.items()))


def _expected_networks(value: object, project_name: str) -> tuple[str, ...]:
    if value is None:
        return (f"{project_name}_default",)
    if not isinstance(value, Mapping) or tuple(value) != ("default",):
        raise ValueError("仅支持冻结的单 default network")
    return (f"{project_name}_default",)
