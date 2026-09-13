"""健康诊断脚本的真实文件、进程和网络失败边界。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.diagnose_runtime_health import _env_value

ROOT = Path(__file__).resolve().parents[1]


def test_selected_env_file_parses_host_port_without_process_override(tmp_path: Path) -> None:
    selected_env = tmp_path / "selected.env"
    selected_env.write_text(
        "# deployment\nHOST_PORT=50499 # public API\nAPI_BASE=\"http://127.0.0.1:50499\"\n",
        encoding="utf-8",
    )

    assert _env_value(selected_env, "HOST_PORT") == "50499"
    assert _env_value(selected_env, "API_BASE") == "http://127.0.0.1:50499"
    assert _env_value(selected_env, "MISSING") is None


def test_diagnose_reports_real_api_connection_failure() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/diagnose_runtime_health.py",
            "--api-base",
            "http://127.0.0.1:1",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1
    assert "AgentGov API liveness 不可达" in result.stdout
    assert "AgentScope Runtime 尚未就绪" not in result.stdout


def test_runtime_healthcheck_import_does_not_load_service_or_model_stack() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import agentscope_runtime.healthcheck; "
            "assert not {'agentscope_runtime.service', 'agentscope', 'litellm', 'uvicorn', 'fastapi'} & sys.modules.keys()",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_healthcheck_fails_closed_against_real_unreachable_endpoint() -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "AGENTGOV_RUNTIME_SHARED_SECRET": "health-probe-test-secret",
            "AGENTSCOPE_RUNTIME_HOST": "127.0.0.1",
            "AGENTSCOPE_RUNTIME_PORT": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "agentscope_runtime.healthcheck"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
