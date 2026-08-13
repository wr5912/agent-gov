"""Compose model 与 Docker image/container 的不可变验收 authority。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from scripts.container_acceptance_candidate_authority import AcceptanceCandidateIdentity

ACCEPTANCE_IMAGE_LABEL: Final = "io.agentgov.acceptance-run-id"
ACCEPTANCE_CANDIDATE_TREE_LABEL: Final = "io.agentgov.acceptance-candidate-tree"
ACCEPTANCE_ENV_DIGEST_LABEL: Final = "io.agentgov.acceptance-selected-env-sha256"

DockerRunner = Callable[[list[str]], str]
ImageAuthorityKind = Literal["candidate", "external-runtime"]

_IMAGE_ID: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID: Final = re.compile(r"^[0-9a-f]{64}$")
_SERVICE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_COMPOSE_MODEL_BYTES: Final = 8 * 1024 * 1024
_MAX_IMAGE_REFERENCE_BYTES: Final = 1024


class AcceptanceSupportError(RuntimeError):
    """Docker 证据不满足隔离验收契约；消息不包含 daemon response。"""


@dataclass(frozen=True, slots=True)
class LocalImageEvidence:
    service: str
    image_id: str
    kind: ImageAuthorityKind = "candidate"


@dataclass(frozen=True, slots=True)
class TerminalImageEvidence:
    service: str
    image_id: str
    image_reference: str
    kind: ImageAuthorityKind
    container_id: str | None


def capture_local_images(
    *,
    compose_base: list[str],
    services: tuple[str, ...],
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    docker_runner: DockerRunner,
) -> tuple[LocalImageEvidence, ...]:
    _validate_image_services(services)
    docker = _docker_executable(compose_base)
    evidence: list[LocalImageEvidence] = []
    for service in services:
        image_id = _compose_image_id(compose_base, service, docker_runner)
        _verify_acceptance_labels(image_id, run_id=run_id, candidate=candidate, docker=docker, docker_runner=docker_runner)
        evidence.append(LocalImageEvidence(service=service, image_id=image_id))
    return tuple(evidence)


def capture_external_images(
    *,
    compose_base: list[str],
    services: tuple[str, ...],
    docker_runner: DockerRunner,
) -> tuple[LocalImageEvidence, ...]:
    _validate_image_services(services)
    _docker_executable(compose_base)
    return tuple(LocalImageEvidence(service, _compose_image_id(compose_base, service, docker_runner), "external-runtime") for service in services)


def capture_terminal_image_evidence(
    *,
    compose_base: list[str],
    images: tuple[LocalImageEvidence, ...],
    running_services: tuple[str, ...],
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    docker_runner: DockerRunner,
) -> tuple[TerminalImageEvidence, ...]:
    services = tuple(item.service for item in images)
    _validate_image_services(services)
    _validate_image_services(running_services)
    if not set(running_services) <= set(services):
        raise AcceptanceSupportError("terminal running service evidence is incomplete")
    docker = _docker_executable(compose_base)
    captured: list[TerminalImageEvidence] = []
    for image in images:
        reference = _compose_image_reference(compose_base, image.service, docker_runner)
        if _image_reference_id(docker, reference, docker_runner) != image.image_id:
            raise AcceptanceSupportError("terminal image reference drifted before cleanup")
        if image.kind == "candidate":
            _verify_acceptance_labels(image.image_id, run_id=run_id, candidate=candidate, docker=docker, docker_runner=docker_runner)
        container_id = _compose_container_id(compose_base, image.service, docker_runner=docker_runner) if image.service in running_services else None
        if container_id is not None:
            _verify_terminal_container(container_id, image.image_id, run_id, candidate, docker, docker_runner)
        captured.append(TerminalImageEvidence(image.service, image.image_id, reference, image.kind, container_id))
    return tuple(captured)


def verify_terminal_image_evidence(
    evidence: tuple[TerminalImageEvidence, ...],
    *,
    docker: str,
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    docker_runner: DockerRunner,
) -> None:
    if not isinstance(docker, str) or not Path(docker).is_absolute() or str(Path(docker)) != docker:
        raise AcceptanceSupportError("terminal Docker executable authority is invalid")
    _validate_image_services(tuple(item.service for item in evidence))
    for item in evidence:
        if item.kind not in ("candidate", "external-runtime") or _IMAGE_ID.fullmatch(item.image_id) is None:
            raise AcceptanceSupportError("terminal image evidence is invalid")
        if _image_reference_id(docker, item.image_reference, docker_runner) != item.image_id:
            raise AcceptanceSupportError("terminal image reference drifted during cleanup")
        if item.kind == "candidate":
            _verify_acceptance_labels(item.image_id, run_id=run_id, candidate=candidate, docker=docker, docker_runner=docker_runner)
        if item.container_id is not None:
            _verify_terminal_container(item.container_id, item.image_id, run_id, candidate, docker, docker_runner)


def verify_running_container(
    *,
    compose_base: list[str],
    service: str,
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    started_at: datetime,
    expected_image_id: str | None,
    docker_runner: DockerRunner,
    image_kind: ImageAuthorityKind = "candidate",
) -> None:
    _validate_image_services((service,))
    if image_kind not in ("candidate", "external-runtime"):
        raise AcceptanceSupportError("running container image authority kind is invalid")
    if expected_image_id is not None and (not isinstance(expected_image_id, str) or _IMAGE_ID.fullmatch(expected_image_id) is None):
        raise AcceptanceSupportError("captured image digest is invalid")
    _require_aware_datetime(started_at, label="Acceptance start timestamp")
    docker = _docker_executable(compose_base)
    container_id = _compose_container_id(compose_base, service, docker_runner=docker_runner)
    running = _inspect_value(docker, ["{{.State.Running}}", container_id], docker_runner=docker_runner)
    created = _inspect_value(docker, ["{{.Created}}", container_id], docker_runner=docker_runner)
    if running != "true":
        raise AcceptanceSupportError("Compose service is not running")
    _verify_acceptance_labels(container_id, run_id=run_id, candidate=candidate, docker=docker, docker_runner=docker_runner)
    if _parse_created(created) < started_at - timedelta(seconds=2):
        raise AcceptanceSupportError("Compose service was not recreated for this acceptance")
    image_id = _inspect_value(docker, ["{{.Image}}", container_id], docker_runner=docker_runner)
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise AcceptanceSupportError("running container image digest is invalid")
    if expected_image_id is not None and image_id != expected_image_id:
        raise AcceptanceSupportError("Compose service does not use the captured image digest")
    _verify_running_container_stable(
        compose_base=compose_base,
        service=service,
        container_id=container_id,
        image_id=image_id,
        docker=docker,
        docker_runner=docker_runner,
    )


def _compose_image_id(compose_base: list[str], service: str, docker_runner: DockerRunner) -> str:
    docker = _docker_executable(compose_base)
    return _image_reference_id(docker, _compose_image_reference(compose_base, service, docker_runner), docker_runner)


def _compose_image_reference(compose_base: list[str], service: str, docker_runner: DockerRunner) -> str:
    _validate_image_services((service,))
    raw = docker_runner([*compose_base, "config", "--format", "json", service])
    try:
        encoded = raw.encode() if isinstance(raw, str) else b""
    except UnicodeError as exc:
        raise AcceptanceSupportError("Compose image model is invalid") from exc
    if not encoded or len(encoded) > _MAX_COMPOSE_MODEL_BYTES:
        raise AcceptanceSupportError("Compose image model is invalid")
    try:
        model = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AcceptanceSupportError("Compose image model is invalid") from exc
    services = model.get("services") if isinstance(model, dict) else None
    declared = services.get(service) if isinstance(services, dict) else None
    image_reference = declared.get("image") if isinstance(declared, dict) else None
    if not _valid_image_reference(image_reference):
        raise AcceptanceSupportError("Compose service image reference is invalid")
    return image_reference


def _image_reference_id(docker: str, image_reference: str, docker_runner: DockerRunner) -> str:
    if not _valid_image_reference(image_reference):
        raise AcceptanceSupportError("Docker image reference is invalid")
    output = docker_runner([docker, "image", "inspect", "--format", "{{.Id}}", image_reference])
    if not isinstance(output, str):
        raise AcceptanceSupportError("Compose image digest is ambiguous")
    image_ids = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(image_ids) != 1 or _IMAGE_ID.fullmatch(image_ids[0]) is None:
        raise AcceptanceSupportError("Compose image digest is ambiguous")
    return image_ids[0]


def _verify_terminal_container(
    container_id: str,
    image_id: str,
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    docker: str,
    docker_runner: DockerRunner,
) -> None:
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise AcceptanceSupportError("terminal container evidence is invalid")
    if _inspect_value(docker, ["{{.State.Running}}", container_id], docker_runner=docker_runner) != "true":
        raise AcceptanceSupportError("terminal container stopped during cleanup")
    if _inspect_value(docker, ["{{.Image}}", container_id], docker_runner=docker_runner) != image_id:
        raise AcceptanceSupportError("terminal container image drifted during cleanup")
    _verify_acceptance_labels(container_id, run_id=run_id, candidate=candidate, docker=docker, docker_runner=docker_runner)


def _docker_executable(compose_base: list[str]) -> str:
    if (
        not isinstance(compose_base, list)
        or len(compose_base) < 2
        or not all(isinstance(value, str) and value and not any(ord(character) < 32 for character in value) for value in compose_base)
        or compose_base[1] != "compose"
    ):
        raise AcceptanceSupportError("Compose command authority is invalid")
    executable = Path(compose_base[0])
    if not executable.is_absolute() or ".." in executable.parts or str(executable) != compose_base[0]:
        raise AcceptanceSupportError("Compose command authority is invalid")
    return compose_base[0]


def _compose_container_id(compose_base: list[str], service: str, *, docker_runner: DockerRunner) -> str:
    output = docker_runner([*compose_base, "ps", "-q", service])
    if not isinstance(output, str):
        raise AcceptanceSupportError("Compose service container identity is ambiguous")
    container_ids = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(container_ids) != 1 or _CONTAINER_ID.fullmatch(container_ids[0]) is None:
        raise AcceptanceSupportError("Compose service container identity is ambiguous")
    return container_ids[0]


def _verify_running_container_stable(
    *,
    compose_base: list[str],
    service: str,
    container_id: str,
    image_id: str,
    docker: str,
    docker_runner: DockerRunner,
) -> None:
    if _compose_container_id(compose_base, service, docker_runner=docker_runner) != container_id:
        raise AcceptanceSupportError("Compose service container was replaced during authority verification")
    final_running = _inspect_value(docker, ["{{.State.Running}}", container_id], docker_runner=docker_runner)
    final_image_id = _inspect_value(docker, ["{{.Image}}", container_id], docker_runner=docker_runner)
    if final_running != "true":
        raise AcceptanceSupportError("Compose service stopped during authority verification")
    if final_image_id != image_id:
        raise AcceptanceSupportError("Compose service image drifted during authority verification")
    if _compose_container_id(compose_base, service, docker_runner=docker_runner) != container_id:
        raise AcceptanceSupportError("Compose service container was replaced during authority verification")


def _valid_image_reference(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode()
    except UnicodeError:
        return False
    return (
        0 < len(encoded) <= _MAX_IMAGE_REFERENCE_BYTES
        and not value.startswith("-")
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _validate_image_services(services: tuple[str, ...]) -> None:
    if (
        len(services) > 32
        or len(set(services)) != len(services)
        or any(not isinstance(service, str) or _SERVICE.fullmatch(service) is None for service in services)
    ):
        raise AcceptanceSupportError("Compose image service set is invalid")


def _inspect_value(docker: str, arguments: list[str], *, docker_runner: DockerRunner) -> str:
    value = docker_runner([docker, "inspect", "--format", *arguments])
    if not isinstance(value, str):
        raise AcceptanceSupportError("Docker inspect output is invalid")
    return value


def _verify_acceptance_labels(
    object_id: str,
    *,
    run_id: str,
    candidate: AcceptanceCandidateIdentity,
    docker: str,
    docker_runner: DockerRunner,
) -> None:
    expected = (
        (ACCEPTANCE_IMAGE_LABEL, run_id),
        (ACCEPTANCE_CANDIDATE_TREE_LABEL, candidate.git_tree_sha),
        (ACCEPTANCE_ENV_DIGEST_LABEL, candidate.selected_env_sha256),
    )
    for key, value in expected:
        actual = _inspect_value(docker, [f'{{{{index .Config.Labels "{key}"}}}}', object_id], docker_runner=docker_runner)
        if actual != value:
            raise AcceptanceSupportError("Docker object does not bind the current acceptance candidate")


def _parse_created(value: str) -> datetime:
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        date_part, dot, remainder = normalized.partition(".")
        if not dot:
            parsed = datetime.fromisoformat(normalized)
        else:
            fraction, offset_sign, offset = remainder.partition("+")
            sign = "+"
            if not offset_sign:
                fraction, offset_sign, offset = remainder.partition("-")
                sign = "-"
            suffix = f"{sign}{offset}" if offset_sign else ""
            parsed = datetime.fromisoformat(f"{date_part}.{fraction[:6]}{suffix}")
    except (AttributeError, TypeError, ValueError) as exc:
        raise AcceptanceSupportError("Docker creation timestamp is invalid") from exc
    return _require_aware_datetime(parsed, label="Docker creation timestamp")


def _require_aware_datetime(value: datetime, *, label: str) -> datetime:
    try:
        offset = value.utcoffset()
    except (AttributeError, TypeError, ValueError) as exc:
        raise AcceptanceSupportError(f"{label} is invalid") from exc
    if value.tzinfo is None or offset is None:
        raise AcceptanceSupportError(f"{label} is invalid")
    return value
