from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "restart_agent_gov_on_host"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_restart_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_restart_script_defaults_and_preserves_remote_env_boundary() -> None:
    text = _script_text()

    assert 'DEFAULT_HOST="172.16.112.232"' in text
    assert 'DEFAULT_REMOTE_DIR="~/work/agent-gov"' in text
    assert 'EXPECTED_GIT_REMOTE_PROJECT="agent-gov"' in text
    assert 'DEPLOY_USER="${DEPLOY_USER:-root}"' in text
    assert "missing docker/.env" in text
    assert "cp -n docker/.env.example docker/.env" not in text
    assert "docker/.env.local-debug" not in text
    assert "frontend/.env.local" not in text


def test_restart_script_uses_full_compose_stack_without_rebuilding_or_pulling() -> None:
    text = _script_text()

    assert "--profile langfuse down --remove-orphans" in text
    assert "--profile langfuse up -d --force-recreate --no-build --pull never" in text
    assert "container_name_prefix=$(read_env CONTAINER_NAME_PREFIX agent-gov)" in text
    assert 'docker ps -aq --filter "name=${container_name_prefix}-"' in text
    assert 'docker ps -aq --filter "name=agent-gov"' not in text
    assert "docker build" not in text
    assert "docker pull" not in text
    assert "git fetch" not in text
    assert "rsync" not in text


def test_restart_script_validates_git_origin_project_and_supports_localhost() -> None:
    text = _script_text()

    assert 'git -C "$ROOT_DIR" remote get-url origin' in text
    assert "git origin project must be" in text
    assert "Pass localhost, 127.0.0.1, or ::1 to restart on this machine without SSH." in text
    assert "localhost|127.0.0.1|::1)" in text
    assert "LOCAL_TARGET=1" in text
    assert 'bash -c "$1"' in text
    assert "REQUIRED_COMMANDS+=(ssh)" in text


def test_restart_script_reuses_container_runtime_root_and_python_health_checks() -> None:
    text = _script_text()

    assert "runtime_root=$(expand_remote_value" in text
    assert "HOST_RUNTIME_VOLUME_ROOT" in text
    assert "from urllib.request import Request, urlopen" in text
    assert '("API health", "http://127.0.0.1:${host_port}/health", 60, True)' in text
    assert '("UI", "http://127.0.0.1:${frontend_port}", 60, False)' in text
    assert '("Langfuse", "http://127.0.0.1:${langfuse_port}", 90, False)' in text
    assert "curl " not in text
