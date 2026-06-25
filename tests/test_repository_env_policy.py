from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ENV_KEYS = (
    "CLAUDE_HOME",
    "DATA_DIR",
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
}


def _env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_project_root_env_file_is_forbidden() -> None:
    root_env = REPO_ROOT / ".env"

    assert not root_env.exists(), "Project root .env is forbidden; use docker/.env, docker/.env.local-debug, or frontend/.env.local."


def test_docker_env_local_example_is_not_an_official_entrypoint() -> None:
    assert not (REPO_ROOT / "docker/.env.local.example").exists()


def test_official_docker_env_examples_do_not_define_runtime_volume_mode() -> None:
    container_example = (REPO_ROOT / "docker/.env.example").read_text(encoding="utf-8")
    local_debug_example = (REPO_ROOT / "docker/.env.local-debug.example").read_text(encoding="utf-8")

    assert "RUNTIME_VOLUME_MODE=" not in container_example
    assert "RUNTIME_VOLUME_MODE=" not in local_debug_example


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
