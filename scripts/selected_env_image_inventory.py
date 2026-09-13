"""selected-env 部署的 Compose 镜像引用与 identity 核验。"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from scripts import selected_env_container_contract as container_contract
from scripts.agentscope_atomic_cutover_bootstrap import SOURCE_ARTIFACT_LABEL
from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings
from scripts.selected_env_operation_contract import (
    DIGEST_REFERENCE,
    IMAGE_ID,
    LOCAL_IMAGES,
    THIRD_PARTY_SERVICES,
    ComposeServices,
    ImageReferences,
    MissingImageError,
    SelectedEnvError,
    StackImageIds,
)

ComposeBuilder: TypeAlias = Callable[..., list[str]]
OutputRunner: TypeAlias = Callable[[list[str], dict[str, str]], str]
CommandRunner: TypeAlias = Callable[..., int]
ServicesLoader: TypeAlias = Callable[..., ComposeServices]
ReferencesLoader: TypeAlias = Callable[[Path, Path, dict[str, str]], ImageReferences]
ImageInspector: TypeAlias = Callable[..., str]
InventoryVerifier: TypeAlias = Callable[[Path, Path, dict[str, str]], StackImageIds]
ProbeVerifier: TypeAlias = Callable[[str], None]
BoundaryVerifier: TypeAlias = Callable[[], None]

BUILD_DOCKERFILES = (
    "docker/Dockerfile",
    "docker/agentscope-runtime.Dockerfile",
    "docker/frontend.Dockerfile",
)
_FROM_INSTRUCTION = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(?P<reference>\S+)(?:\s+AS\s+\S+)?$",
    re.IGNORECASE,
)
_COMPOSE_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class ImageMetadata:
    image_id: str
    source_digest: str | None
    config: container_contract.ImageConfig


def rendered_services(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    langfuse: bool,
    compose_builder: ComposeBuilder,
    run_output: OutputRunner,
) -> ComposeServices:
    compose = compose_builder(snapshot, source_root, langfuse=langfuse)
    if langfuse:
        compose.extend(("--profile", "*"))
    rendered = run_output([*compose, "config", "--format", "json"], child_env)
    try:
        config = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError("Compose image contract 不是 JSON") from exc
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        raise SelectedEnvError("Compose image contract 缺少 services")
    return services


def third_party_image_references(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    load_services: ServicesLoader,
) -> ImageReferences:
    services = load_services(snapshot, source_root, child_env, langfuse=True)
    references: ImageReferences = {}
    for service in THIRD_PARTY_SERVICES:
        service_config = services.get(service)
        reference = service_config.get("image") if isinstance(service_config, dict) else None
        if not isinstance(reference, str) or DIGEST_REFERENCE.fullmatch(reference) is None:
            raise SelectedEnvError(f"第三方镜像必须使用 digest pin: {service}")
        references[service] = reference
    return references


def build_base_image_references(source_root: Path) -> ImageReferences:
    """读取受源码摘要保护的 Dockerfile，并要求每个 build base 固定 digest。"""

    references: ImageReferences = {}
    for relative in BUILD_DOCKERFILES:
        dockerfile = source_root / relative
        try:
            metadata = dockerfile.lstat()
            source = dockerfile.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SelectedEnvError(f"无法读取构建基础镜像契约: {relative}") from exc
        if dockerfile.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise SelectedEnvError(f"构建 Dockerfile 必须是普通非符号链接文件: {relative}")
        found = 0
        for line_number, raw_line in enumerate(source.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.upper().startswith("FROM"):
                continue
            match = _FROM_INSTRUCTION.fullmatch(line)
            reference = match.group("reference") if match else ""
            if not reference or DIGEST_REFERENCE.fullmatch(reference) is None:
                raise SelectedEnvError(f"构建基础镜像必须使用 digest pin: {relative}:{line_number}")
            references[f"build-base:{relative}:{found}"] = reference
            found += 1
        if found == 0:
            raise SelectedEnvError(f"构建 Dockerfile 缺少 FROM: {relative}")
    return references


def required_external_image_references(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    load_services: ServicesLoader,
) -> ImageReferences:
    references = third_party_image_references(
        snapshot,
        source_root,
        child_env,
        load_services=load_services,
    )
    references.update(build_base_image_references(source_root))
    return references


def inspect_image_id(
    reference: str,
    child_env: dict[str, str],
    *,
    service: str,
    run_output: OutputRunner,
) -> str:
    listed_id = _listed_digest_image_id(
        reference,
        run_output(
            ["docker", "image", "ls", "--digests", "--no-trunc", "--format", "{{json .}}"],
            child_env,
        ),
        service=service,
    )
    if listed_id is None:
        raise MissingImageError(f"本地缺少 digest-pinned 镜像: {service}")
    rendered = run_output(["docker", "image", "inspect", reference], child_env)
    try:
        inspected = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError(f"本地镜像 identity 不是 JSON: {service}") from exc
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise SelectedEnvError(f"本地镜像 identity 无效: {service}")
    image_id = inspected[0].get("Id")
    if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
        raise SelectedEnvError(f"本地镜像 metadata 无效: {service}")
    if image_id != listed_id:
        raise SelectedEnvError(f"本地镜像 availability/inspect identity 漂移: {service}")
    return image_id


def _listed_digest_image_id(reference: str, rendered: str, *, service: str) -> str | None:
    repository, digest = _reference_repository_digest(reference, service=service)
    matching_ids: set[str] = set()
    for line in rendered.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SelectedEnvError(f"本地镜像 availability inventory 不是 JSON: {service}") from exc
        if not isinstance(record, dict):
            raise SelectedEnvError(f"本地镜像 availability inventory 无效: {service}")
        if not all(isinstance(record.get(field), str) for field in ("Repository", "Digest", "ID")):
            raise SelectedEnvError(f"本地镜像 availability inventory 字段无效: {service}")
        if _normalize_repository(record.get("Repository")) != repository or record.get("Digest") != digest:
            continue
        image_id = record.get("ID")
        if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
            raise SelectedEnvError(f"本地镜像 availability identity 无效: {service}")
        matching_ids.add(image_id)
    if len(matching_ids) > 1:
        raise SelectedEnvError(f"本地镜像 availability identity 不唯一: {service}")
    return next(iter(matching_ids), None)


def _reference_repository_digest(reference: str, *, service: str) -> tuple[str, str]:
    image_name, separator, digest_value = reference.rpartition("@")
    if not separator or not digest_value.startswith("sha256:"):
        raise SelectedEnvError(f"本地镜像 reference identity 无效: {service}")
    last_slash = image_name.rfind("/")
    last_colon = image_name.rfind(":")
    repository = image_name[:last_colon] if last_colon > last_slash else image_name
    return _normalize_repository(repository), digest_value


def _normalize_repository(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.removeprefix("docker.io/").removeprefix("index.docker.io/")
    return normalized.removeprefix("library/")


def verify_required_external_images(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    load_references: ReferencesLoader,
    inspect_image: ImageInspector,
) -> StackImageIds:
    references = load_references(snapshot, source_root, child_env)
    image_ids = _inspect_reference_inventory(references, child_env, inspect_image=inspect_image)
    if set(image_ids) != set(references):
        raise SelectedEnvError("部署所需外部镜像 inventory 不完整")
    return image_ids


def prepare_required_external_images(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    *,
    load_references: ReferencesLoader,
    inspect_image: ImageInspector,
    run_command: CommandRunner,
    verify_inventory: InventoryVerifier,
    verify_bootstrap_probe: ProbeVerifier | None = None,
    verify_before_bootstrap_pull: BoundaryVerifier | None = None,
) -> StackImageIds:
    references = load_references(snapshot, source_root, child_env)
    bootstrap_reference = references.get("langfuse-postgres")
    if bootstrap_reference is None:
        raise SelectedEnvError("部署镜像 inventory 缺少 Docker host probe 镜像")
    inspected_by_reference: dict[str, str] = {}
    bootstrap_id = _prepare_reference(
        "langfuse-postgres",
        bootstrap_reference,
        child_env,
        inspected_by_reference=inspected_by_reference,
        inspect_image=inspect_image,
        run_command=run_command,
        before_pull=verify_before_bootstrap_pull,
    )
    if verify_bootstrap_probe is not None:
        verify_bootstrap_probe(bootstrap_id)
    for service, reference in references.items():
        if reference in inspected_by_reference:
            continue
        _prepare_reference(
            service,
            reference,
            child_env,
            inspected_by_reference=inspected_by_reference,
            inspect_image=inspect_image,
            run_command=run_command,
        )
    verified = verify_inventory(snapshot, source_root, child_env)
    if any(verified.get(service) != inspected_by_reference[reference] for service, reference in references.items()):
        raise SelectedEnvError("部署镜像 identity 在 prepare 最终复验时发生漂移")
    return verified


def _prepare_reference(
    service: str,
    reference: str,
    child_env: dict[str, str],
    *,
    inspected_by_reference: dict[str, str],
    inspect_image: ImageInspector,
    run_command: CommandRunner,
    before_pull: BoundaryVerifier | None = None,
) -> str:
    try:
        image_id = inspect_image(reference, child_env, service=service)
    except MissingImageError:
        if before_pull is not None:
            before_pull()
        run_command(["docker", "pull", reference], child_env)
        image_id = inspect_image(reference, child_env, service=service)
    inspected_by_reference[reference] = image_id
    return image_id


def _inspect_reference_inventory(
    references: ImageReferences,
    child_env: dict[str, str],
    *,
    inspect_image: ImageInspector,
) -> StackImageIds:
    image_by_reference: dict[str, str] = {}
    image_ids: StackImageIds = {}
    for service, reference in references.items():
        image_id = image_by_reference.get(reference)
        if image_id is None:
            image_id = inspect_image(reference, child_env, service=service)
            image_by_reference[reference] = image_id
        image_ids[service] = image_id
    return image_ids


def verify_stack_images(
    child_env: dict[str, str],
    snapshot: Path,
    version: str,
    source_digest: str,
    *,
    source_root: Path,
    langfuse: bool,
    running: bool,
    expected_ids: StackImageIds | None,
    compose_builder: ComposeBuilder,
    load_services: ServicesLoader,
    run_output: OutputRunner,
    services: tuple[str, ...] | None = None,
) -> StackImageIds:
    del version
    compose = compose_builder(snapshot, source_root, langfuse=langfuse)
    rendered_services = load_services(snapshot, source_root, child_env, langfuse=langfuse)
    allowed = (*LOCAL_IMAGES, *THIRD_PARTY_SERVICES) if langfuse else tuple(LOCAL_IMAGES)
    names = allowed if services is None else services
    if not names or len(set(names)) != len(names) or set(names) - set(allowed):
        raise SelectedEnvError("Compose 镜像验收 service scope 无效")
    image_ids: StackImageIds = {}
    for service in names:
        service_config = rendered_services.get(service)
        reference = service_config.get("image") if isinstance(service_config, dict) else None
        if not isinstance(reference, str) or not reference:
            raise SelectedEnvError(f"Compose 缺少镜像引用: {service}")
        if service in THIRD_PARTY_SERVICES and DIGEST_REFERENCE.fullmatch(reference) is None:
            raise SelectedEnvError(f"第三方镜像必须使用 digest pin: {service}")
        inspected = _decode_image_metadata(run_output(["docker", "image", "inspect", reference], child_env), service)
        image_id = inspected.image_id
        if service in LOCAL_IMAGES and inspected.source_digest != source_digest:
            raise SelectedEnvError(f"本地镜像不属于当前源码摘要: {service}")
        image_ids[service] = image_id
        if expected_ids is not None and expected_ids.get(service) != image_id:
            raise SelectedEnvError(f"部署期间镜像 identity 漂移: {service}")
        if running:
            _verify_running_container(
                compose,
                service,
                service_config,
                inspected,
                child_env,
                project_name=lambda: _compose_project_name(snapshot, source_root),
                langfuse=langfuse,
                run_output=run_output,
            )
    if set(image_ids) != set(names):
        raise SelectedEnvError("Compose 镜像 inventory 不完整")
    return image_ids


def _decode_image_metadata(rendered: str, service: str) -> ImageMetadata:
    try:
        inspected = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SelectedEnvError(f"本地镜像 identity 不是 JSON: {service}") from exc
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise SelectedEnvError(f"本地镜像 identity 无效: {service}")
    image_id = inspected[0].get("Id")
    config = inspected[0].get("Config")
    if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
        raise SelectedEnvError(f"本地镜像 metadata 无效: {service}")
    image_config = container_contract.decode_image_config(config, service)
    source_digest = dict(image_config.labels).get(SOURCE_ARTIFACT_LABEL)
    return ImageMetadata(
        image_id=image_id,
        source_digest=source_digest if isinstance(source_digest, str) else None,
        config=image_config,
    )


def _verify_running_container(
    compose: list[str],
    service: str,
    service_config: object,
    image: ImageMetadata,
    child_env: dict[str, str],
    *,
    project_name: Callable[[], str],
    langfuse: bool,
    run_output: OutputRunner,
) -> None:
    container_command = [*compose]
    if langfuse:
        container_command.extend(("--profile", "*"))
    container_command.extend(("ps", "-q", service))
    container_id = run_output(container_command, child_env)
    if not container_id:
        raise SelectedEnvError(f"本地部署容器未启动: {service}")
    rendered = run_output(["docker", "container", "inspect", container_id], child_env)
    container_contract.verify_container_config(
        rendered,
        service_config,
        image.config,
        expected_image_id=image.image_id,
        project_name=project_name,
        service=service,
    )


def _compose_project_name(snapshot: Path, source_root: Path) -> str:
    try:
        values = {binding.key: binding.value or "" for binding in parse_selected_env_bindings(snapshot) if binding.key}
    except (OSError, UnicodeError, ValueError) as exc:
        raise SelectedEnvError("无法固定 Compose project name") from exc
    project_name = values.get("COMPOSE_PROJECT_NAME") or (source_root / "docker").name
    if _COMPOSE_PROJECT_NAME.fullmatch(project_name) is None:
        raise SelectedEnvError("Compose project name 无法安全归一化")
    return project_name
