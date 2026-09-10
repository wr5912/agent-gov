from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker/docker-compose.yml"
LANGFUSE_COMPOSE_PATH = REPO_ROOT / "docker/docker-compose.langfuse.yml"
ENV_EXAMPLE = REPO_ROOT / "docker/.env.example"
LOCAL_DEBUG_EXAMPLE = REPO_ROOT / "docker/.env.local-debug.example"
CORE_SERVICES = {"agent-gov-api", "agent-gov-ui", "agentscope-runtime"}


def _load_container_acceptance() -> ModuleType:
    module_name = "_agentgov_container_acceptance_test"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts/run_container_acceptance.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_public_bind_guard() -> ModuleType:
    module_name = "_agentgov_public_bind_guard_test"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts/check_public_bind.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _compose() -> dict[str, object]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _volume_targets(service: dict[str, object]) -> set[str]:
    targets: set[str] = set()
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            targets.add(volume.rsplit(":", 1)[-1])
        elif isinstance(volume, dict) and isinstance(volume.get("target"), str):
            targets.add(volume["target"])
    return targets


def test_project_root_env_file_is_forbidden() -> None:
    assert not (REPO_ROOT / ".env").exists()
    assert not (REPO_ROOT / "docker/.env.local.example").exists()


def test_clean_checkout_compose_config_resolves_exact_agentscope_core_services(tmp_path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    version = subprocess.run([docker, "compose", "version"], check=False, capture_output=True, text=True)
    if version.returncode != 0:
        pytest.skip("docker compose is unavailable")

    core_env = tmp_path / "core.env"
    core_env.write_text(
        "\n".join(line for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if not line.startswith(("LANGFUSE_", "OTEL_"))),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(core_env),
            "-f",
            str(COMPOSE_PATH),
            "config",
            "--services",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == CORE_SERVICES


def test_compose_has_one_runtime_and_no_protocol_sidecar() -> None:
    services = _compose()["services"]

    assert CORE_SERVICES.issubset(services)
    assert [name for name in CORE_SERVICES if "runtime" in name] == ["agentscope-runtime"]
    assert all("sidecar" not in name for name in services)
    assert not (REPO_ROOT / "docker/litellm-sidecar.Dockerfile").exists()
    assert not (REPO_ROOT / "docker/litellm_sidecar_entrypoint.py").exists()
    assert not (REPO_ROOT / "docker/e2e/docker-compose.provider-health.yml").exists()


@pytest.mark.parametrize("custom_frontend_url", [None, "https://traces.example.test"])
def test_langfuse_config_derives_identity_and_browser_port(tmp_path, custom_frontend_url) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    env_file = tmp_path / "langfuse.env"
    secrets = (
        "LANGFUSE_POSTGRES_PASSWORD",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_ROOT_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
    )
    values = {key: "a" * 64 for key in secrets}
    values.update(
        {
            "LANGFUSE_ENABLED": "true",
            "LANGFUSE_HOST_PORT": "50499",
            "LANGFUSE_PUBLIC_KEY": "pk-test-only",
            "LANGFUSE_SECRET_KEY": "sk-test-only",
            "LANGFUSE_INIT_PROJECT_ID": "custom-project",
            "LANGFUSE_BASE_URL": "http://collector.test:3000",
        }
    )
    if custom_frontend_url:
        values["FRONTEND_LANGFUSE_URL"] = custom_frontend_url
    source = "\n".join(
        line for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if not line.startswith(("LANGFUSE_", "OTEL_", "FRONTEND_LANGFUSE_"))
    )
    env_file.write_text(source + "\n" + "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(COMPOSE_PATH),
            "-f",
            str(LANGFUSE_COMPOSE_PATH),
            "--profile",
            "langfuse",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    web = services["langfuse-web"]["environment"]
    runtime = services["agentscope-runtime"]["environment"]
    ui = services["agent-gov-ui"]["environment"]
    assert web["NEXTAUTH_URL"] == "http://localhost:50499"
    assert web["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"] == runtime["AGENTGOV_OTEL_PUBLIC_KEY"] == "pk-test-only"
    assert web["LANGFUSE_INIT_PROJECT_SECRET_KEY"] == runtime["AGENTGOV_OTEL_SECRET_KEY"] == "sk-test-only"
    assert web["LANGFUSE_INIT_ORG_ID"] == "agent-gov"
    assert web["LANGFUSE_INIT_USER_NAME"] == "admin"
    assert services["langfuse-minio"]["environment"]["MINIO_ROOT_USER"] == "minio"
    assert ui["VITE_LANGFUSE_URL"] == (custom_frontend_url or "http://localhost:50499")
    assert ui["VITE_LANGFUSE_PROJECT_ID"] == web["LANGFUSE_INIT_PROJECT_ID"] == "custom-project"
    assert runtime["AGENTGOV_OTEL_BASE_URL"] == "http://collector.test:3000"
    assert runtime["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    assert runtime["OTEL_EXPORTER_OTLP_HEADERS"] == ""


def test_container_acceptance_loads_langfuse_compose_only_for_langfuse_profile(tmp_path) -> None:
    acceptance = _load_container_acceptance()
    for profile_name in ("core", "langfuse"):
        profile = acceptance.PROFILES[profile_name]
        env_file = tmp_path / "selected.env"
        command = acceptance.compose_command(profile, env_file)
        assert (str(LANGFUSE_COMPOSE_PATH) in command) == (profile_name == "langfuse")
        isolation = acceptance.IsolatedEnvironment(env_file, tmp_path, "isolated", "isolated", {})
        child_env = acceptance.build_acceptance_env(profile, isolation, "test-run", {"LANGFUSE_ENABLED": "host-setting"})
        assert child_env["LANGFUSE_ENABLED"] == ("true" if profile_name == "langfuse" else "false")


def test_all_default_published_ports_stay_in_project_range_without_changing_internal_ports() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "-f",
            str(COMPOSE_PATH),
            "-f",
            str(LANGFUSE_COMPOSE_PATH),
            "--profile",
            "langfuse",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    published = {(name, int(port["published"]), int(port["target"])) for name, service in services.items() for port in service.get("ports", [])}
    assert published == {
        ("agent-gov-api", 50400, 8080),
        ("agent-gov-ui", 50401, 5173),
        ("langfuse-web", 50402, 3000),
        ("langfuse-minio", 50403, 9000),
        ("langfuse-minio", 50404, 9001),
    }
    assert all(50400 <= host <= 50499 for _, host, _ in published)
    assert all(port["host_ip"] == "127.0.0.1" for service in services.values() for port in service.get("ports", []))
    assert services["agent-gov-ui"]["environment"]["VITE_RUNTIME_API_BASE"] == "http://localhost:50400"
    assert services["agent-gov-ui"]["environment"]["VITE_LANGFUSE_URL"] == "http://localhost:50402"
    web_env = services["langfuse-web"]["environment"]
    assert web_env["NEXTAUTH_URL"] == "http://localhost:50402"
    assert web_env["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"] == "http://localhost:50403"
    assert web_env["LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT"] == "http://localhost:50403"


def test_cutover_loads_current_langfuse_file_without_changing_historical_rollback(tmp_path) -> None:
    from scripts import agentscope_atomic_cutover as cutover

    env_file = tmp_path / "selected.env"
    assert str(LANGFUSE_COMPOSE_PATH) in cutover._compose_base(env_file)
    rollback = tmp_path / "rollback-compose.resolved.yml"
    assert cutover._compose_base(env_file, rollback) == ["docker", "compose", "--env-file", str(env_file), "-f", str(rollback)]


@pytest.mark.parametrize(
    ("langfuse_config", "image_count"),
    [
        ("LANGFUSE_ENABLED=false\n", 0),
        ("LANGFUSE_ENABLED=true\nLANGFUSE_BASE_URL=https://traces.example.test\n", 0),
        ("LANGFUSE_ENABLED=true\n", 6),
        ("LANGFUSE_ENABLED='True' # enabled\nLANGFUSE_BASE_URL='http://langfuse-web:3000/'\n", 6),
        ('LANGFUSE_ENABLED= "TRUE" \nLANGFUSE_BASE_URL="http://langfuse-web:3000"\n', 6),
        ("export LANGFUSE_ENABLED = 'yes'\n", 6),
        *((f"LANGFUSE_ENABLED={value}\n", 6) for value in ("1", "yes", "ON", "t", "Y")),
        *((f"LANGFUSE_ENABLED='{value}'\n", 0) for value in ("0", "NO", "Off", "f", "N")),
        ("LANGFUSE_ENABLED=invalid\n", None),
    ],
)
def test_deployment_selects_infra_images_only_for_local_langfuse(tmp_path, langfuse_config, image_count) -> None:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/.env").write_text(langfuse_config, encoding="utf-8")
    source = (REPO_ROOT / "scripts/deploy_agent_gov_to_host").read_text(encoding="utf-8")
    image_selection = source.split("<<'REMOTE_IMAGES'\n", 1)[1].split("\nREMOTE_IMAGES", 1)[0]
    result = subprocess.run(
        ["bash", "-s", "--", str(tmp_path)],
        input=image_selection,
        capture_output=True,
        text=True,
        check=False,
    )
    if image_count is None:
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip() == "LANGFUSE_ENABLED must be a boolean"
    else:
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.splitlines()) == image_count


@pytest.mark.parametrize(("self_hosted", "with_storage", "expected_success"), [(False, False, True), (True, False, False), (True, True, True)])
def test_deployment_requires_storage_secrets_only_for_self_hosted_langfuse(tmp_path, self_hosted, with_storage, expected_success) -> None:
    values = {key: "test-only-credential-value" for key in ("API_KEY", "AGENTGOV_RUNTIME_SHARED_SECRET", "MODEL_PROVIDER_API_KEY")}
    if with_storage:
        values.update(
            {
                key: "test-only-credential-value"
                for key in (
                    "LANGFUSE_PUBLIC_KEY",
                    "LANGFUSE_SECRET_KEY",
                    "LANGFUSE_SALT",
                    "LANGFUSE_NEXTAUTH_SECRET",
                    "LANGFUSE_POSTGRES_PASSWORD",
                    "LANGFUSE_CLICKHOUSE_PASSWORD",
                    "LANGFUSE_REDIS_AUTH",
                    "LANGFUSE_MINIO_ROOT_PASSWORD",
                )
            }
        )
        values["LANGFUSE_ENCRYPTION_KEY"] = "b" * 64
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/.env").write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    source = (REPO_ROOT / "scripts/deploy_agent_gov_to_host").read_text(encoding="utf-8")
    private_preflight = "read_env() {" + source.split("\nread_env() {", 1)[1].split("\nrequire_public_bind_opt_in()", 1)[0]
    result = subprocess.run(
        ["bash", "-s"],
        input=f"set -euo pipefail\nversion=test\nwith_langfuse={int(self_hosted)}\n" + private_preflight,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is expected_success, result.stderr


def test_provider_and_mcp_secrets_are_injected_only_into_runtime() -> None:
    services = _compose()["services"]
    runtime_env = services["agentscope-runtime"]["environment"]
    api_env = services["agent-gov-api"]["environment"]
    ui_env = services["agent-gov-ui"]["environment"]

    assert "MODEL_PROVIDER_API_KEY" in runtime_env
    assert "SEC_OPS_MCP_TOKEN" in runtime_env
    assert runtime_env["SEC_OPS_MCP_TOKEN"] == "${SEC_OPS_MCP_TOKEN:-}"
    assert "OTEL_EXPORTER_OTLP_HEADERS" in runtime_env
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert name not in runtime_env
    for environment in (api_env, ui_env):
        assert "MODEL_PROVIDER_API_KEY" not in environment
        assert "SEC_OPS_MCP_TOKEN" not in environment
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in environment


def test_api_key_is_injected_only_into_the_public_api_and_ui_client_config() -> None:
    services = _compose()["services"]
    api_env = services["agent-gov-api"]["environment"]

    assert api_env["API_KEY"] == "${API_KEY:?API key required}"
    assert api_env["AGENTGOV_API_MODE"] == "${AGENTGOV_API_MODE:-open}"
    assert api_env["AGENTGOV_ACCEPTANCE_IDENTITY"] == "${AGENTGOV_ACCEPTANCE_IDENTITY:-}"
    assert api_env["AGENTGOV_ACCEPTANCE_API_KEY"] == "${AGENTGOV_ACCEPTANCE_API_KEY:-}"
    assert api_env["AGENTGOV_API_GATE_STATE_FILE"] == "${AGENTGOV_API_GATE_STATE_FILE:-}"
    assert "API_KEY" not in services["agentscope-runtime"]["environment"]
    assert "AGENTGOV_API_MODE" not in services["agentscope-runtime"]["environment"]
    assert "AGENTGOV_ACCEPTANCE_API_KEY" not in services["agentscope-runtime"]["environment"]
    assert "/run/agentgov/api-gate" in _volume_targets(services["agent-gov-api"])
    assert "/run/agentgov/api-gate" not in _volume_targets(services["agentscope-runtime"])
    gate_volume = next(item for item in services["agent-gov-api"]["volumes"] if isinstance(item, dict) and item.get("target") == "/run/agentgov/api-gate")
    assert gate_volume["read_only"] is True
    assert gate_volume["bind"]["create_host_path"] is False
    assert "API_KEY" not in services["agent-gov-ui"]["environment"]
    assert services["agent-gov-ui"]["environment"]["VITE_RUNTIME_API_KEY"] == ("${FRONTEND_RUNTIME_API_KEY:-${API_KEY:-}}")


def test_api_gets_langfuse_read_key_but_runtime_does_not_get_langfuse_management_env() -> None:
    services = _compose()["services"]
    runtime_env = services["agentscope-runtime"]["environment"]
    api_env = services["agent-gov-api"]["environment"]

    assert api_env["LANGFUSE_SECRET_KEY"] == "${LANGFUSE_SECRET_KEY:-}"
    assert api_env["LANGFUSE_PUBLIC_KEY"] == "${LANGFUSE_PUBLIC_KEY:-}"
    assert "LANGFUSE_SECRET_KEY" not in runtime_env
    assert "LANGFUSE_PUBLIC_KEY" not in runtime_env
    assert runtime_env["LANGFUSE_ENABLED"] == api_env["LANGFUSE_ENABLED"]
    assert runtime_env["AGENTGOV_OTEL_PUBLIC_KEY"] == "${LANGFUSE_PUBLIC_KEY:-}"
    assert runtime_env["AGENTGOV_OTEL_SECRET_KEY"] == "${LANGFUSE_SECRET_KEY:-}"
    assert "AGENTGOV_OTEL_SECRET_KEY" not in services["agent-gov-ui"]["environment"]


def test_public_operator_ports_default_to_configurable_loopback_bindings() -> None:
    source = COMPOSE_PATH.read_text(encoding="utf-8") + LANGFUSE_COMPOSE_PATH.read_text(encoding="utf-8")
    required = (
        "LANGFUSE_POSTGRES_PASSWORD",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_ROOT_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
    )
    for name in required:
        assert f"${{{name}:?" in source
        assert f"${{{name}:-" not in source
    assert '"${API_BIND_IP:-127.0.0.1}:${HOST_PORT:-50400}:${API_PORT:-8080}"' in source
    assert '"${FRONTEND_BIND_IP:-127.0.0.1}:${FRONTEND_HOST_PORT:-50401}:${FRONTEND_PORT:-5173}"' in source
    assert '"${LANGFUSE_BIND_IP:-127.0.0.1}:${LANGFUSE_HOST_PORT:-50402}:3000"' in source
    assert '"${LANGFUSE_BIND_IP:-127.0.0.1}:${LANGFUSE_MINIO_HOST_PORT:-50403}:9000"' in source
    assert '"${LANGFUSE_BIND_IP:-127.0.0.1}:${LANGFUSE_MINIO_CONSOLE_HOST_PORT:-50404}:9001"' in source
    container = _env_values(ENV_EXAMPLE)
    local = _env_values(LOCAL_DEBUG_EXAMPLE)
    for name in ("API_BIND_IP", "FRONTEND_BIND_IP", "LANGFUSE_BIND_IP"):
        assert container.get(name, "127.0.0.1") == "127.0.0.1"
    for name in ("API_BIND_IP", "FRONTEND_BIND_IP"):
        assert local[name] == "127.0.0.1"
    for name in ("API_ALLOW_PUBLIC_BIND", "FRONTEND_ALLOW_PUBLIC_BIND", "LANGFUSE_ALLOW_PUBLIC_BIND"):
        assert container.get(name, "0") == "0"
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert 'scripts/check_public_bind.py --env-file "$(COMPOSE_ENV_FILE)"' in makefile
    assert "up: public-bind-check cutover-inspect" in makefile
    assert "all-up: public-bind-check cutover-inspect" in makefile
    assert "ui-up: public-bind-check" in makefile
    assert "langfuse-up: public-bind-check langfuse-prepare" in makefile


def test_public_bind_guard_requires_explicit_single_tenant_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _load_public_bind_guard()
    for name in (
        "API_BIND_IP",
        "API_ALLOW_PUBLIC_BIND",
        "FRONTEND_BIND_IP",
        "FRONTEND_ALLOW_PUBLIC_BIND",
        "LANGFUSE_BIND_IP",
        "LANGFUSE_ALLOW_PUBLIC_BIND",
    ):
        monkeypatch.delenv(name, raising=False)
    guard.require_public_bind_opt_in({"API_BIND_IP": "127.0.0.1"})
    with pytest.raises(ValueError, match="API_ALLOW_PUBLIC_BIND=1"):
        guard.require_public_bind_opt_in({"API_BIND_IP": "0.0.0.0"})
    guard.require_public_bind_opt_in({"API_BIND_IP": "0.0.0.0", "API_ALLOW_PUBLIC_BIND": "1"})


def test_runtime_mount_and_process_boundary_is_fail_closed() -> None:
    runtime = _compose()["services"]["agentscope-runtime"]
    targets = _volume_targets(runtime)

    assert runtime["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in runtime["security_opt"]
    assert runtime["deploy"]["replicas"] == 1
    assert "ports" not in runtime
    assert {"/business-agents", "/candidate-workspaces", "/runtime-data", "/runtime-workspaces"}.issubset(targets)
    assert "/data" not in targets
    assert runtime["environment"]["AGENTSCOPE_RUNTIME_DATABASE_URL"].startswith("sqlite+aiosqlite:////runtime-data/")


def test_agentscope_runtime_uses_container_init_to_reap_adopted_sandbox_processes() -> None:
    runtime = _compose()["services"]["agentscope-runtime"]
    assert runtime.get("init") is True


def test_only_non_root_agentscope_runtime_gets_exact_bubblewrap_exceptions() -> None:
    services = _compose()["services"]
    runtime = services["agentscope-runtime"]
    assert runtime.get("privileged") is not True
    assert runtime["user"] == "${AGENT_GOV_RUNTIME_UID:-1000}:${AGENT_GOV_RUNTIME_GID:-1000}"
    assert runtime["cap_drop"] == ["ALL"]
    assert set(runtime["security_opt"]) == {
        "no-new-privileges:true",
        "seccomp=unconfined",
        "apparmor=unconfined",
        "systempaths=unconfined",
    }

    for name, service in services.items():
        assert "SYS_ADMIN" not in service.get("cap_add", [])
        assert "NET_ADMIN" not in service.get("cap_add", [])
        if name != "agentscope-runtime":
            assert not {"seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined"}.intersection(service.get("security_opt", []))


def test_runtime_bootstrap_source_is_bound_read_only_for_api() -> None:
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    api = _compose()["services"]["agent-gov-api"]

    assert "source: ${RUNTIME_BOOTSTRAP_HOST_DIR:-./runtime-bootstrap}" in compose_text
    assert "target: /app/docker/runtime-bootstrap" in compose_text
    assert "read_only: true" in compose_text
    assert "/app/docker/runtime-bootstrap" in _volume_targets(api)


def test_compose_healthchecks_and_dependencies_use_agentscope_service_names() -> None:
    services = _compose()["services"]
    api_health = services["agent-gov-api"]["healthcheck"]
    runtime_health = services["agentscope-runtime"]["healthcheck"]

    assert api_health["test"][-1].endswith("/health/live")
    assert runtime_health["test"] == ["CMD", "python", "-m", "agentscope_runtime.healthcheck"]
    assert services["agent-gov-api"]["depends_on"]["agentscope-runtime"]["condition"] == "service_healthy"
    assert services["agent-gov-ui"]["depends_on"]["agent-gov-api"]["condition"] == "service_healthy"


def test_official_env_examples_keep_secrets_and_runtime_ownership_explicit() -> None:
    container = _env_values(ENV_EXAMPLE)
    local = _env_values(LOCAL_DEBUG_EXAMPLE)

    assert container["AGENTGOV_RUNTIME_SHARED_SECRET"].startswith("replace-with-")
    assert container["MODEL_PROVIDER_API_KEY"] == "replace-with-private-provider-key"
    assert "SEC_OPS_MCP_TOKEN" not in container
    assert container["API_KEY"] == "replace-with-private-api-key"
    assert container["FRONTEND_RUNTIME_API_KEY"] == container["API_KEY"]
    assert local["API_KEY"] == "replace-with-local-debug-api-key"
    assert container["AGENTGOV_API_MODE"] == "open"
    assert local["AGENTGOV_API_MODE"] == "open"
    assert "MODEL_PROVIDER_API_KEY" not in local
    assert "SEC_OPS_MCP_TOKEN" not in local
    assert container["LOG_LEVEL"] == "info"
    assert local["LOG_LEVEL"] == "debug"
    assert container["AGENTSCOPE_MODEL_PARAMETERS_JSON"] == "{}"
    assert local["AGENTSCOPE_MODEL_PARAMETERS_JSON"] == "{}"
    assert "RUNTIME_VOLUME_MODE" not in container
    assert "RUNTIME_VOLUME_MODE" not in local
    assert not re.search(r"MODEL_PROVIDER_API_KEY=sk-", ENV_EXAMPLE.read_text(encoding="utf-8"))


def test_make_operational_targets_use_selected_env_and_agentscope_services() -> None:
    selected = "/tmp/agent-gov-selected-compose.env"
    result = subprocess.run(
        [
            "make",
            "-n",
            "up",
            "all-up",
            "smoke",
            "container-openapi-check",
            "langfuse-smoke",
            "runtime-validate",
            "cutover-check",
            f"COMPOSE_ENV_FILE={selected}",
            "COMPOSE=:",
            "PYTHON_RUN=:",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert selected in result.stdout
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "logs -f agent-gov-api agentscope-runtime" in makefile
    assert "build agent-gov-ui" in makefile
    assert "scripts/check_agentscope_cutover.py" in makefile


def test_container_acceptance_uses_exact_agentscope_core_services() -> None:
    acceptance = _load_container_acceptance()

    assert set(acceptance.CORE_SERVICES) == CORE_SERVICES
    assert acceptance.PROFILES["core"].build_services == acceptance.CORE_SERVICES
    assert "isolated-health" not in acceptance.PROFILES


@pytest.mark.parametrize("target", ["up", "all-up"])
@pytest.mark.parametrize("failure", ["epoch", "readiness"])
def test_deployment_fails_before_writes_on_old_epoch_and_requires_readiness(tmp_path, target, failure) -> None:
    calls = tmp_path / "calls.txt"
    runner = tmp_path / "python-probe"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(calls)!r})\n"
        "args = ' '.join(sys.argv[1:])\n"
        "with log.open('a') as stream: stream.write(args + '\\n')\n"
        f"failure = {failure!r}\n"
        "if failure == 'epoch' and 'agentscope_atomic_cutover.py inspect' in args: sys.exit(2)\n"
        "if 'diagnose_runtime_health.py' in args:\n"
        "    assert '--require-ready' in sys.argv\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    result = subprocess.run(
        ["make", "--no-print-directory", target, f"PYTHON={runner}", f"PYTHON_RUN={runner}", "COMPOSE=true", f"COMPOSE_ENV_FILE={tmp_path / 'unused.env'}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    invocations = calls.read_text(encoding="utf-8")
    assert "agentscope_atomic_cutover.py inspect" in invocations
    assert ("bootstrap_runtime_volume.py" in invocations) is (failure == "readiness")
    assert ("diagnose_runtime_health.py" in invocations) is (failure == "readiness")


@pytest.mark.parametrize("target", ["container-live-test", "ui-feedback-smoke"])
def test_real_feedback_acceptance_enables_langfuse_profile(target) -> None:
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", target, "REQUIRE_LIVE_RUNTIME=1", "PYTHON_RUN=:"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--profile langfuse" in result.stdout


def test_container_acceptance_generates_isolated_project_ports_and_mounts(tmp_path, monkeypatch) -> None:
    acceptance = _load_container_acceptance()

    source_env = tmp_path / "source.env"
    source_env.write_text(
        "COMPOSE_PROJECT_NAME=live-project\n"
        "API_BIND_IP=0.0.0.0\n"
        "API_ALLOW_PUBLIC_BIND=1\n"
        "FRONTEND_BIND_IP=0.0.0.0\n"
        "FRONTEND_ALLOW_PUBLIC_BIND=1\n"
        "LANGFUSE_BIND_IP=0.0.0.0\n"
        "LANGFUSE_ALLOW_PUBLIC_BIND=1\n"
        "HOST_RUNTIME_VOLUME_ROOT=/home/operator/volume-agent-gov\n"
        "HOST_DATA_MOUNT=/home/operator/volume-agent-gov/data/business-agents\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(acceptance, "_allocate_loopback_ports", lambda _count: (50451, 50452, 50453, 50454, 50455))

    isolation = acceptance.prepare_isolated_environment(source_env, "1234-deadbeef", tmp_path)
    child_env = acceptance.build_acceptance_env(acceptance.PROFILES["core"], isolation, "1234-deadbeef", {})

    assert isolation.project_name == f"agv-acceptance-{acceptance.os.getuid()}-deadbeef"
    assert isolation.container_prefix == isolation.project_name
    assert isolation.runtime_root == (tmp_path / "runtime-root").resolve()
    assert isolation.env_file.stat().st_mode & 0o777 == 0o600
    assert child_env["COMPOSE_PROJECT_NAME"] == isolation.project_name
    assert child_env["CONTAINER_NAME_PREFIX"] == isolation.container_prefix
    assert child_env["API_BIND_IP"] == "127.0.0.1"
    assert child_env["API_ALLOW_PUBLIC_BIND"] == "0"
    assert child_env["FRONTEND_BIND_IP"] == "127.0.0.1"
    assert child_env["FRONTEND_ALLOW_PUBLIC_BIND"] == "0"
    assert child_env["LANGFUSE_BIND_IP"] == "127.0.0.1"
    assert child_env["LANGFUSE_ALLOW_PUBLIC_BIND"] == "0"
    assert child_env["HOST_PORT"] == "50451"
    assert child_env["FRONTEND_HOST_PORT"] == "50452"
    assert child_env["LANGFUSE_HOST_PORT"] == "50453"
    assert child_env["COMPOSE_ENV_FILE"] == str(isolation.env_file)
    assert acceptance._compose_mount_variables() == set(acceptance.ISOLATED_MOUNT_PATHS)
    for variable in acceptance.ISOLATED_MOUNT_PATHS:
        mount = Path(child_env[variable]).resolve()
        assert mount.is_relative_to(isolation.runtime_root)

    generated = isolation.env_file.read_text(encoding="utf-8")
    assert generated.rfind(f"HOST_DATA_MOUNT={child_env['HOST_DATA_MOUNT']}") > generated.rfind(
        "HOST_DATA_MOUNT=/home/operator/volume-agent-gov/data/business-agents"
    )
    for name in ("API_BIND_IP", "FRONTEND_BIND_IP", "LANGFUSE_BIND_IP"):
        assert generated.rfind(f"{name}=127.0.0.1") > generated.rfind(f"{name}=0.0.0.0")
    for name in ("API_ALLOW_PUBLIC_BIND", "FRONTEND_ALLOW_PUBLIC_BIND", "LANGFUSE_ALLOW_PUBLIC_BIND"):
        assert generated.rfind(f"{name}=0") > generated.rfind(f"{name}=1")


def test_container_acceptance_compose_config_resolves_only_isolated_bind_mounts(tmp_path) -> None:
    acceptance = _load_container_acceptance()

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    version = subprocess.run([docker, "compose", "version"], check=False, capture_output=True, text=True)
    if version.returncode != 0:
        pytest.skip("docker compose is unavailable")

    source_env = tmp_path / "source.env"
    required_secrets = (
        "LANGFUSE_POSTGRES_PASSWORD",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_ROOT_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
    )
    source_env.write_text(
        ENV_EXAMPLE.read_text(encoding="utf-8") + "\n" + "\n".join(f"{name}={'a' * 64}" for name in required_secrets),
        encoding="utf-8",
    )
    isolation = acceptance.prepare_isolated_environment(source_env, "1234-configcheck", tmp_path)
    child_env = acceptance.build_acceptance_env(
        acceptance.PROFILES["langfuse"],
        isolation,
        "1234-configcheck",
        {},
    )
    base = acceptance.compose_command(acceptance.PROFILES["langfuse"], isolation.env_file)

    acceptance._validate_isolated_mounts(base, isolation, child_env)


def test_container_acceptance_always_cleans_failed_isolated_refresh(tmp_path, monkeypatch) -> None:
    acceptance = _load_container_acceptance()

    source_env = tmp_path / "source.env"
    source_env.write_text("API_KEY=test-only\n", encoding="utf-8")
    monkeypatch.setattr(acceptance, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(acceptance, "source_fingerprint", lambda _path: "stable")
    monkeypatch.setattr(acceptance, "_allocate_loopback_ports", lambda _count: (50461, 50462, 50463, 50464, 50465))
    monkeypatch.setattr(acceptance, "_bootstrap_isolated_runtime", lambda _isolation, _env: None)

    cleaned: list[Path] = []

    def fail_refresh(_profile, _isolation, _env) -> None:
        raise acceptance.AcceptanceError("expected refresh failure")

    def record_cleanup(_profile, isolation, _env) -> None:
        assert isolation.runtime_root.is_dir()
        cleaned.append(isolation.runtime_root)

    monkeypatch.setattr(acceptance, "refresh_profile", fail_refresh)
    monkeypatch.setattr(acceptance, "cleanup_profile", record_cleanup)

    with pytest.raises(acceptance.AcceptanceError, match="expected refresh failure"):
        acceptance.run_acceptance(acceptance.PROFILES["core"], source_env, ["true"], {})

    assert len(cleaned) == 1
    assert not cleaned[0].parent.exists()


def test_langfuse_smoke_does_not_initialize_the_selected_live_volume() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert re.search(r"^langfuse-smoke:\s*$", makefile, flags=re.MULTILINE)


def test_langfuse_permission_init_reuses_all_stateful_mount_sources() -> None:
    services = yaml.safe_load(LANGFUSE_COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
    init_service = services["langfuse-volume-init"]

    def volume_map(service_name: str) -> dict[str, str]:
        return {target: source for source, target in (volume.rsplit(":", 1) for volume in services[service_name]["volumes"])}

    expected_sources = {
        "/langfuse/postgres": volume_map("langfuse-postgres")["/var/lib/postgresql/data"],
        "/langfuse/clickhouse-data": volume_map("langfuse-clickhouse")["/var/lib/clickhouse"],
        "/langfuse/clickhouse-logs": volume_map("langfuse-clickhouse")["/var/log/clickhouse-server"],
        "/langfuse/redis": volume_map("langfuse-redis")["/data"],
        "/langfuse/minio": volume_map("langfuse-minio")["/data"],
    }

    assert init_service["profiles"] == ["langfuse-maintenance"]
    assert volume_map("langfuse-volume-init") == expected_sources
    assert "chmod -R a+rwX" in "\n".join(init_service["command"])


def test_api_image_hosts_only_control_plane_entrypoint() -> None:
    dockerfile = (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["python", "-m", "app.runtime.service_launcher"]' in dockerfile
    assert 'CMD ["api"]' in dockerfile
    assert not (REPO_ROOT / "docker/entrypoint.sh").exists()
