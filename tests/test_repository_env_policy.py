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

    assert not root_env.exists(), "Project root .env is forbidden; use docker/.env.local or frontend/.env.local."


def test_bare_dspy_import_does_not_load_project_root_runtime_env() -> None:
    env = {key: value for key, value in os.environ.items() if key not in RUNTIME_ENV_KEYS}
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
