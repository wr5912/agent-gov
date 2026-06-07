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
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_DISALLOWED_TOOLS",
    "MODEL_PROVIDER_API_KEY",
    "MODEL_PROVIDER_API_URL",
)


def test_project_root_env_file_is_forbidden() -> None:
    root_env = REPO_ROOT / ".env"

    assert not root_env.exists(), "Project root .env is forbidden; use docker/.env, docker/.env.local-debug, or frontend/.env.local."


def test_docker_env_local_example_is_not_an_official_entrypoint() -> None:
    assert not (REPO_ROOT / "docker/.env.local.example").exists()


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
            "DEFAULT_ALLOWED_TOOLS",
            "DEFAULT_DISALLOWED_TOOLS",
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
