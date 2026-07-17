from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_DEBUG_PORT_FAMILY_RE = re.compile(r"(?<![#A-Za-z0-9_])4\d{4}(?![A-Za-z0-9_])|" + "4" + r"[xX]{4}")
_PORT_POLICY_VENDOR_PREFIXES = ("app/static/docs/",)
RUNTIME_ENV_KEYS = (
    "CLAUDE_HOME",
    "DATA_DIR",
    "DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS",
    "GOVERNANCE_AGENT_TIMEOUT_SECONDS",
    "HITL_TIMEOUT_SECONDS",
    "LOG_LEVEL",
    "MODEL_PROVIDER_API_KEY",
    "MODEL_PROVIDER_API_URL",
    "MODEL_PROVIDER_BACKEND",
    "MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS",
    "MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD",
    "MODEL_PROVIDER_WARNING_TTL_SECONDS",
)
CONTAINER_ONLY_ENV_KEYS = {
    "COMPOSE_PROJECT_NAME",
    "CONTAINER_NAME_PREFIX",
    "FRONTEND_BIND_IP",
    "FRONTEND_HOST_PORT",
    "FRONTEND_PORT",
    "LANGFUSE_BIND_IP",
    "LANGFUSE_CLICKHOUSE_CLUSTER_ENABLED",
    "LANGFUSE_CLICKHOUSE_DATA_MOUNT",
    "LANGFUSE_CLICKHOUSE_DB",
    "LANGFUSE_CLICKHOUSE_IMAGE",
    "LANGFUSE_CLICKHOUSE_LOGS_MOUNT",
    "LANGFUSE_CLICKHOUSE_PASSWORD",
    "LANGFUSE_CLICKHOUSE_USER",
    "LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_HOST_PORT",
    "LANGFUSE_INIT_ORG_ID",
    "LANGFUSE_INIT_ORG_NAME",
    "LANGFUSE_INIT_PROJECT_ID",
    "LANGFUSE_INIT_PROJECT_NAME",
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    "LANGFUSE_INIT_USER_EMAIL",
    "LANGFUSE_INIT_USER_NAME",
    "LANGFUSE_INIT_USER_PASSWORD",
    "LANGFUSE_MINIO_CONSOLE_HOST_PORT",
    "LANGFUSE_MINIO_DATA_MOUNT",
    "LANGFUSE_MINIO_HOME",
    "LANGFUSE_MINIO_HOST_PORT",
    "LANGFUSE_MINIO_IMAGE",
    "LANGFUSE_MINIO_ROOT_PASSWORD",
    "LANGFUSE_MINIO_ROOT_USER",
    "LANGFUSE_NEXTAUTH_SECRET",
    "LANGFUSE_NEXTAUTH_URL",
    "LANGFUSE_POSTGRES_DATA_MOUNT",
    "LANGFUSE_POSTGRES_DB",
    "LANGFUSE_POSTGRES_IMAGE",
    "LANGFUSE_POSTGRES_PASSWORD",
    "LANGFUSE_POSTGRES_USER",
    "LANGFUSE_REDIS_AUTH",
    "LANGFUSE_REDIS_CONTAINER",
    "LANGFUSE_REDIS_DATA_MOUNT",
    "LANGFUSE_REDIS_IMAGE",
    "LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT",
    "LANGFUSE_S3_BUCKET",
    "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT",
    "LANGFUSE_S3_REGION",
    "LANGFUSE_SALT",
    "LANGFUSE_TELEMETRY_ENABLED",
    "LANGFUSE_WEB_IMAGE",
    "LANGFUSE_WORKER_IMAGE",
    "RUNTIME_VOLUME_SEEDS_HOST_DIR",
}


def _env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def _dockerfile_apt_packages(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    pattern = (
        r"apt-get update && apt-get install -y --no-install-recommends\s+(.+?)\s+"
        r"&& rm -rf /var/lib/apt/lists/\*"
    )
    match = re.search(
        pattern,
        text,
        re.S,
    )
    assert match is not None, "Dockerfile must keep apt package installation in the expected layer."

    packages: set[str] = set()
    for line in match.group(1).splitlines():
        package = line.strip().rstrip("\\").strip()
        if package:
            packages.add(package)
    return packages


def _tracked_text_files(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=repo_root, check=True, stdout=subprocess.PIPE)
    files: list[tuple[str, str]] = []
    for rel_path in result.stdout.decode("utf-8").split("\0"):
        if not rel_path:
            continue
        path = repo_root / rel_path
        if not path.is_file():
            continue  # A tracked file deleted by the current change cannot contribute text to the resulting tree.
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        files.append((rel_path, raw.decode("utf-8", errors="ignore")))
    return files


def test_tracked_text_files_skip_paths_deleted_from_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "present.txt").write_text("present\n", encoding="utf-8")
    deleted = tmp_path / "deleted.txt"
    deleted.write_text("deleted\n", encoding="utf-8")
    subprocess.run(["git", "add", "present.txt", "deleted.txt"], cwd=tmp_path, check=True)
    deleted.unlink()

    assert _tracked_text_files(tmp_path) == [("present.txt", "present\n")]


def test_tracked_text_files_do_not_commit_private_debug_port_family() -> None:
    offenders: list[str] = []
    for rel_path, text in _tracked_text_files():
        # 自托管 API 文档资源是逐字节 vendor 的压缩产物，其中五位数值是 Unicode 码点而非端口。
        if rel_path.startswith(_PORT_POLICY_VENDOR_PREFIXES):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LOCAL_DEBUG_PORT_FAMILY_RE.search(line):
                offenders.append(f"{rel_path}:{lineno}:{line.strip()}")

    assert offenders == []


def test_dockerfile_installs_claude_code_sandbox_dependencies_when_seed_sandbox_enabled() -> None:
    business_agents_dir = REPO_ROOT / "docker/runtime-volume-seeds/data/business-agents"
    settings_files = sorted(business_agents_dir.glob("*/workspace/.claude/settings.json"))
    sandbox_enabled = False
    for path in settings_files:
        text = path.read_text(encoding="utf-8")
        sandbox_enabled = sandbox_enabled or ('"sandbox"' in text and '"enabled": true' in text)

    assert sandbox_enabled, "The runtime workspace seeds are expected to keep Claude Code sandbox enabled."

    packages = _dockerfile_apt_packages(REPO_ROOT / "docker/Dockerfile")

    assert "bubblewrap" in packages
    assert "socat" in packages


def test_compose_does_not_grant_unconfined_or_namespace_capabilities() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for service_name in services:
        service = services[service_name]
        assert "SYS_ADMIN" not in service.get("cap_add", [])
        assert "NET_ADMIN" not in service.get("cap_add", [])
        assert "seccomp=unconfined" not in service.get("security_opt", [])
        assert "apparmor=unconfined" not in service.get("security_opt", [])


def test_project_root_env_file_is_forbidden() -> None:
    root_env = REPO_ROOT / ".env"

    assert not root_env.exists(), "Project root .env is forbidden; use docker/.env, docker/.env.local-debug, or frontend/.env.local."


def test_docker_env_local_example_is_not_an_official_entrypoint() -> None:
    assert not (REPO_ROOT / "docker/.env.local.example").exists()


def test_clean_checkout_compose_config_uses_the_one_selected_env_file(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    version = subprocess.run(
        [docker, "compose", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        pytest.skip("docker compose is unavailable")

    compose_dir = tmp_path / "docker"
    compose_dir.mkdir()
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        (REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    selected_env = tmp_path / "selected-compose.env"
    selected_env.write_text(
        (REPO_ROOT / "docker/.env.example").read_text(encoding="utf-8") + "\nAGENT_GOV_CLEAN_CHECKOUT_SENTINEL=selected\n",
        encoding="utf-8",
    )
    assert not (compose_dir / ".env").exists()

    environment = {
        **os.environ,
        "AGENT_GOV_COMPOSE_ENV_FILE": str(selected_env),
        "RUNTIME_VOLUME_SEEDS_HOST_DIR": str(REPO_ROOT / "docker/runtime-volume-seeds"),
    }
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(selected_env),
            "-f",
            str(compose_path),
            "config",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "AGENT_GOV_CLEAN_CHECKOUT_SENTINEL: selected" in result.stdout


def test_make_container_helpers_use_the_selected_compose_env_file() -> None:
    selected = "/tmp/agent-gov-selected-compose.env"
    result = subprocess.run(
        [
            "make",
            "-n",
            "ui-smoke",
            "langfuse-dirs",
            "langfuse-smoke",
            "chat",
            "container-openapi-check",
            "up",
            "smoke",
            "runtime-clean",
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
    assert "docker/.env" not in result.stdout
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/smoke.sh" not in makefile
    assert "scripts/chat.sh" not in makefile


def test_official_docker_env_examples_do_not_define_runtime_volume_mode() -> None:
    container_example = (REPO_ROOT / "docker/.env.example").read_text(encoding="utf-8")
    local_debug_example = (REPO_ROOT / "docker/.env.local-debug.example").read_text(encoding="utf-8")

    assert "RUNTIME_VOLUME_MODE=" not in container_example
    assert "RUNTIME_VOLUME_MODE=" not in local_debug_example


def test_official_env_examples_expose_only_the_general_debug_api_key() -> None:
    for env_file in ("docker/.env.example", "docker/.env.local-debug.example"):
        lines = set((REPO_ROOT / env_file).read_text(encoding="utf-8").splitlines())

        assert "API_KEY=change-me" in lines
        assert "RESPONSE_ORCHESTRATOR_API_KEY=" not in lines


def test_local_debug_env_example_keeps_runtime_keys_in_sync() -> None:
    container_keys = _env_keys(REPO_ROOT / "docker/.env.example")
    local_debug_keys = _env_keys(REPO_ROOT / "docker/.env.local-debug.example")

    assert local_debug_keys - container_keys == set()
    assert container_keys - local_debug_keys == CONTAINER_ONLY_ENV_KEYS


def test_official_env_examples_define_mode_specific_log_level_defaults() -> None:
    container_example = (REPO_ROOT / "docker/.env.example").read_text(encoding="utf-8")
    local_debug_example = (REPO_ROOT / "docker/.env.local-debug.example").read_text(encoding="utf-8")

    assert "LOG_LEVEL=info" in container_example
    assert "LOG_LEVEL=debug" in local_debug_example
    assert "AGENT_JOB_WORKER_LOG_LEVEL" not in container_example
    assert "AGENT_JOB_WORKER_LOG_LEVEL" not in local_debug_example


def test_official_env_examples_do_not_define_claude_config_takeover_keys() -> None:
    forbidden = {
        "CLAUDE_MCP_CONFIG_PATH=",
        "CLAUDE_SETTINGS_PATH=",
        "CLAUDE_TOOLS=",
        "DEFAULT_ALLOWED_TOOLS=",
        "DEFAULT_DISALLOWED_TOOLS=",
        "PERMISSION_MODE=",
        "STRICT_MCP_CONFIG=",
        "ENABLE_POLICY_HOOKS=",
        "ENABLE_PROGRAMMATIC_AGENTS=",
        "CLAUDE_SETTING_SOURCES=",
        "DEFAULT_AGENT=",
        "DEFAULT_SKILLS=",
        "DEFAULT_SKILLS_MODE=",
        "CLAUDE_ADD_DIRS=",
        "PERMISSION_PROMPT_TOOL_NAME=",
        "CLAUDE_EXTRA_ARGS_JSON=",
    }
    for env_file in ("docker/.env.example", "docker/.env.local-debug.example"):
        text = (REPO_ROOT / env_file).read_text(encoding="utf-8")
        for item in forbidden:
            assert item not in text


def test_official_env_examples_do_not_ship_configured_model_provider_key() -> None:
    for env_file in ("docker/.env.example", "docker/.env.local-debug.example"):
        text = (REPO_ROOT / env_file).read_text(encoding="utf-8")

        assert "MODEL_PROVIDER_API_KEY=sk-" not in text
        assert "ANTHROPIC_API_KEY=sk-" not in text


def test_official_env_and_settings_do_not_allow_manual_vllm_version_or_second_upstream_url() -> None:
    checked_paths = [
        REPO_ROOT / "docker/.env.example",
        REPO_ROOT / "docker/.env.local-debug.example",
        REPO_ROOT / "app/runtime/settings.py",
    ]
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert "MODEL_PROVIDER_VLLM_VERSION=" not in text
        assert "MODEL_PROVIDER_UPSTREAM_URL=" not in text


def test_runtime_env_governance_skill_keeps_required_boundary_terms() -> None:
    skill = (REPO_ROOT / ".codex/skills/runtime-env-governance/SKILL.md").read_text(encoding="utf-8")

    required_terms = [
        "Consumer x Mode x Boundary",
        "`RUNTIME_VOLUME_MODE` 不应出现在官方 env 示例中",
        "本机后台 Agent job 不复用交互式 Claude `/login`",
        "原 Docker Compose 容器服务中生效",
        "make ui-build && make ui-up && make ui-smoke",
        "不另起临时 Vite",
        "`MODEL_PROVIDER_API_KEY` required privately",
        "测试模式选择矩阵",
        "make container-live-test",
        "不使用 `docker/.env.local-debug`",
        "docker/.env.local-debug",
        "/tmp/local-debug-volume-agent-gov",
        "${HOME}/volume-agent-gov",
    ]
    for term in required_terms:
        assert term in skill

    assert "不要写“覆盖文件”“私有覆盖”“覆盖配置”" in skill


def test_bare_dspy_import_does_not_load_project_root_runtime_env() -> None:
    env = {key: value for key, value in os.environ.items() if key not in RUNTIME_ENV_KEYS}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    script = textwrap.dedent(
        """
        import os
        import dspy

        keys = (
            "CLAUDE_HOME",
            "DATA_DIR",
            "MODEL_PROVIDER_API_KEY",
            "MODEL_PROVIDER_API_URL",
        )
        polluted = {key: os.environ[key] for key in keys if key in os.environ}
        if polluted:
            raise SystemExit(f"DSPy import loaded forbidden runtime env keys: {sorted(polluted)}")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_project_dspy_entrypoint_suppresses_known_litellm_import_warnings() -> None:
    env = {key: value for key, value in os.environ.items() if key not in {*RUNTIME_ENV_KEYS, "LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_LOG"}}
    script = textwrap.dedent(
        """
        import os

        import app.runtime.agent_job_types

        if os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True":
            raise SystemExit("LITELLM_LOCAL_MODEL_COST_MAP was not defaulted")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "Failed to fetch remote model cost map" not in output
    assert "could not pre-load bedrock-runtime response stream shape" not in output
    assert "could not pre-load sagemaker-runtime response stream shape" not in output


def test_container_live_test_includes_litellm_sidecar_dependency() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("container-live-test:", 1)[1].split("\n\n", 1)[0]

    assert "--no-deps" not in target
    assert "tests/test_live_runtime_acceptance.py" in target


def test_litellm_sidecar_does_not_receive_full_runtime_env_file() -> None:
    compose = (REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")
    sidecar = compose.split("agent-gov-litellm-sidecar:", 1)[1].split("\n  claude-agent-api:", 1)[0]

    assert "env_file:" not in sidecar
    assert "MODEL_PROVIDER_API_URL" in sidecar
    assert "MODEL_PROVIDER_API_KEY" in sidecar
    assert "LANGFUSE_SECRET_KEY" not in sidecar
    assert "\n      API_KEY:" not in sidecar


def test_runtime_volume_seeds_are_bound_read_only_for_api() -> None:
    compose = (REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")
    api = compose.split("  claude-agent-api:", 1)[1].split("\n  claude-agent-ui:", 1)[0]

    assert "source: ${RUNTIME_VOLUME_SEEDS_HOST_DIR:-./runtime-volume-seeds}" in compose
    assert "target: /app/docker/runtime-volume-seeds" in compose
    assert "read_only: true" in compose
    assert "create_host_path: false" in compose
    assert "- *runtime-volume-seeds" in api


def test_compose_healthcheck_uses_local_liveness_without_provider_dependency() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api_health = services["claude-agent-api"]["healthcheck"]

    assert api_health["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "2",
        "http://127.0.0.1:${API_PORT:-8080}/health/live",
    ]
    assert api_health["timeout"] == "3s"
    assert api_health["start_period"] == "20s"
    assert api_health["retries"] == 12
    assert "MODEL_PROVIDER" not in str(api_health)
    assert services["claude-agent-ui"]["depends_on"]["claude-agent-api"]["condition"] == "service_healthy"
    assert "claude-agent-worker" not in services


def test_make_up_waits_removes_orphans_and_prints_sanitized_diagnostics() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    up_target = makefile.split("\nup:", 1)[1].split("\n\ndown:", 1)[0]
    diagnose_script = (REPO_ROOT / "scripts/compose_diagnose.sh").read_text(encoding="utf-8")

    assert "up -d --wait --remove-orphans" in up_target
    assert "compose-diagnose" in up_target
    assert "diagnose_runtime_health.py" in up_target
    assert "compose config" not in diagnose_script
    assert "os.path.abspath(sys.argv[1])" in diagnose_script
    assert 'export COMPOSE_ENV_FILE="$compose_env_file"' in diagnose_script
    assert 'export AGENT_GOV_COMPOSE_ENV_FILE="$compose_env_file"' in diagnose_script
    assert '--env-file "$compose_env_file"' in diagnose_script
    assert "docker inspect" in diagnose_script
    assert "logs --no-color --tail=80" in diagnose_script


def test_governance_ci_uses_repository_pnpm_version_without_corepack_download() -> None:
    workflow = (REPO_ROOT / ".github/workflows/governance.yml").read_text(encoding="utf-8")

    assert "uses: pnpm/action-setup@v6" in workflow
    assert "package_json_file: frontend/package.json" in workflow
    assert "cache-dependency-path: frontend/pnpm-lock.yaml" in workflow
    assert "corepack enable pnpm" not in workflow


def test_compose_uses_api_coordinator_without_runtime_init_container() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    dockerfile = (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")

    assert "agent-gov-runtime-init" not in services
    assert "claude-agent-worker" not in services
    assert services["claude-agent-ui"]["depends_on"]["claude-agent-api"]["condition"] == "service_healthy"
    assert "healthcheck" in services["claude-agent-api"]
    assert services["claude-agent-api"]["healthcheck"]["test"][-1].endswith("/health/live")
    assert 'ENTRYPOINT ["python", "-m", "app.runtime.service_launcher"]' in dockerfile
    assert not (REPO_ROOT / "docker/entrypoint.sh").exists()
