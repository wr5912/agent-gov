from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_deploy_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_script_defaults_and_preserves_private_remote_env() -> None:
    text = _script_text()

    assert 'DEFAULT_HOST="172.16.112.232"' in text
    assert 'DEFAULT_REMOTE_DIR="~/work/agent-gov"' in text
    assert 'DEPLOY_USER="${DEPLOY_USER:-root}"' in text
    assert 'REMOTE_DIR="${REMOTE_DIR:-$DEFAULT_REMOTE_DIR}"' in text
    assert 'EXPECTED_GIT_REMOTE_PROJECT="agent-gov"' in text
    assert "cp -n docker/.env.example docker/.env" in text

    for excluded in (
        "--exclude='/images/'",
        "--exclude='/docker/.env'",
        "--exclude='/docker/.env.local-debug'",
        "--exclude='/frontend/.env.local'",
    ):
        assert excluded in text


def test_deploy_script_packages_project_and_langfuse_dependency_images() -> None:
    text = _script_text()

    for image in (
        "agent-gov-api:${VERSION}",
        "agent-gov-ui:${VERSION}",
        "agent-gov-litellm-sidecar:${VERSION}",
    ):
        assert image in text

    for env_key in (
        "LANGFUSE_WORKER_IMAGE",
        "LANGFUSE_WEB_IMAGE",
        "LANGFUSE_POSTGRES_IMAGE",
        "LANGFUSE_CLICKHOUSE_IMAGE",
        "LANGFUSE_REDIS_IMAGE",
        "LANGFUSE_MINIO_IMAGE",
    ):
        assert env_key in text

    assert "docker save" in text
    assert "docker load" in text
    assert "sha256sum" in text
    assert "agent-gov-${VERSION}-images.tar.gz" in text
    assert "agent-gov-${VERSION}-langfuse-deps-images.tar.gz" in text


def test_deploy_script_uses_loaded_images_for_full_compose_stack() -> None:
    text = _script_text()

    assert "git fetch origin master" in text
    assert 'git -C "$ROOT_DIR" remote get-url origin' in text
    assert "git origin project must be" in text
    assert "origin/master" in text
    assert "git show origin/master:VERSION" in text
    assert "git archive origin/master" in text
    assert "working tree must be clean" not in text
    assert "--profile langfuse down --remove-orphans" in text
    assert "--profile langfuse up -d --wait --wait-timeout 180 --remove-orphans --force-recreate --no-build --pull never" in text
    assert "container_name_prefix=$(read_env CONTAINER_NAME_PREFIX agent-gov)" in text
    assert 'docker ps -aq --filter "name=${container_name_prefix}-"' in text
    assert 'docker ps -aq --filter "name=agent-gov"' not in text
    assert "runtime_root=$(expand_remote_value" in text
    assert "rm -rf '${HOME}'" in text


def test_deploy_script_supports_localhost_without_ssh_transport() -> None:
    text = _script_text()

    assert "Pass localhost, 127.0.0.1, or ::1 to deploy on this machine without SSH." in text
    assert "localhost|127.0.0.1|::1)" in text
    assert "LOCAL_TARGET=1" in text
    assert 'bash -c "$1"' in text
    assert "REQUIRED_COMMANDS+=(ssh)" in text
    assert '"$TMP_DIR"/ "$REMOTE_PATH/"' in text
    assert '"$TMP_DIR"/ "${REMOTE}:${REMOTE_PATH}/"' in text
    assert text.count("--chown=0:0") == 1
    assert "local target dir must not be the current repository" in text


def test_deploy_script_uses_python_health_checks_without_remote_curl_dependency() -> None:
    text = _script_text()

    assert "from urllib.request import Request, urlopen" in text
    assert '("API liveness", "http://127.0.0.1:${host_port}/health/live", 60, True)' in text
    assert '("UI", "http://127.0.0.1:${frontend_port}", 60, False)' in text
    assert '("Langfuse", "http://127.0.0.1:${langfuse_port}", 90, False)' in text
    assert "curl " not in text
    assert "except HTTPError as exc:" in text
    assert 'last_error = f"HTTP {exc.code}"' in text
    assert "diagnose_runtime_health.py" in text
    assert "bash scripts/compose_diagnose.sh" in text
