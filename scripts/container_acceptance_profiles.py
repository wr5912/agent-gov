"""容器验收公开 profile 与受控生命周期配置。"""

from __future__ import annotations

from dataclasses import dataclass

CORE_SERVICES = ("agent-gov-litellm-sidecar", "claude-agent-api", "agent-test-worker", "claude-agent-ui")
CORE_BUILD_SERVICES = (*CORE_SERVICES, "agent-test-sandbox-image")
LANGFUSE_SERVICES = (
    "langfuse-postgres",
    "langfuse-clickhouse",
    "langfuse-redis",
    "langfuse-minio",
    "langfuse-web",
    "langfuse-worker",
)
ISOLATED_HEALTH_SERVICES = (
    "slow-vllm",
    "agent-gov-litellm-sidecar",
    "claude-agent-api",
    "claude-agent-ui",
)


@dataclass(frozen=True, slots=True)
class AcceptanceProfile:
    name: str
    compose_profiles: tuple[str, ...]
    build_services: tuple[str, ...]
    expected_services: tuple[str, ...]
    active_services: tuple[str, ...] | None = None
    isolated_runtime: bool = False
    volume_init_service: str | None = None
    compose_overlays: tuple[str, ...] = ()
    external_image_services: tuple[str, ...] = ()

    @property
    def services_to_run(self) -> tuple[str, ...]:
        return self.active_services or self.expected_services


PROFILES = {
    "core": AcceptanceProfile("core", (), CORE_BUILD_SERVICES, CORE_SERVICES),
    "langfuse": AcceptanceProfile(
        "langfuse",
        ("langfuse",),
        CORE_BUILD_SERVICES,
        (*CORE_SERVICES, *LANGFUSE_SERVICES),
        volume_init_service="langfuse-volume-init",
        external_image_services=LANGFUSE_SERVICES,
    ),
    "agent-test": AcceptanceProfile(
        "agent-test",
        (),
        (*CORE_SERVICES[:3], "agent-test-sandbox-image"),
        CORE_SERVICES,
        active_services=CORE_SERVICES[:3],
        isolated_runtime=True,
    ),
    "isolated-health": AcceptanceProfile(
        "isolated-health",
        (),
        ISOLATED_HEALTH_SERVICES,
        ISOLATED_HEALTH_SERVICES,
        isolated_runtime=True,
        compose_overlays=("docker/e2e/docker-compose.provider-health.yml",),
    ),
}
