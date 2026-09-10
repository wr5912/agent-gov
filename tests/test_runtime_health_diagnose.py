from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.diagnose_runtime_health as diagnose_runtime_health
from agentscope_runtime import healthcheck
from agentscope_runtime.settings import RUNTIME_USER_ID
from agentscope_runtime.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify_runtime_gateway_request


def test_diagnose_prints_agentscope_not_ready_reason(monkeypatch, capsys) -> None:
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (
                503,
                {
                    "status": "not_ready",
                    "runtime_service": {
                        "status": "not_ready",
                        "reason": "connection refused",
                        "route": "http://agentscope-runtime:8090",
                        "retryable": True,
                    },
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    result = diagnose_runtime_health.diagnose(api_base="http://agent-gov", wait_seconds=0, require_ready=False)

    assert result == 0
    output = capsys.readouterr().out
    assert "API: healthy" in output
    assert "AgentScope Runtime: not_ready" in output
    assert "reason=connection refused" in output
    assert "AgentGov API 已存活，但 AgentScope Runtime 尚未就绪" in output


def test_diagnose_can_require_agentscope_readiness(monkeypatch) -> None:
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (503, {"status": "not_ready", "runtime_service": {"status": "not_ready"}}, None),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    assert diagnose_runtime_health.diagnose(api_base="http://agent-gov", wait_seconds=0, require_ready=True) == 2


def test_diagnose_reports_api_liveness_failure_without_secondary_attribution(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        diagnose_runtime_health,
        "_get_json",
        lambda *_args, **_kwargs: (None, None, "ConnectionRefusedError"),
    )

    assert diagnose_runtime_health.diagnose(api_base="http://agent-gov", wait_seconds=0, require_ready=False) == 1
    output = capsys.readouterr().out
    assert "AgentGov API liveness 不可达" in output
    assert "AgentScope Runtime 尚未就绪" not in output


@pytest.mark.parametrize("host_port", [None, "50499"])
def test_main_reads_api_defaults_from_selected_compose_env(monkeypatch, tmp_path, host_port) -> None:
    selected_env = tmp_path / "selected.env"
    selected_env.write_text(f"HOST_PORT={host_port}\n" if host_port else "", encoding="utf-8")
    monkeypatch.setenv("COMPOSE_ENV_FILE", str(selected_env))
    monkeypatch.delenv("HOST_PORT", raising=False)
    monkeypatch.delenv("API_BASE", raising=False)
    monkeypatch.setattr(sys, "argv", ["diagnose_runtime_health.py"])

    def fake_diagnose(*, api_base: str, wait_seconds: float, require_ready: bool) -> int:
        assert api_base == f"http://localhost:{host_port or '50400'}"
        assert wait_seconds == 0
        assert require_ready is False
        return 0

    monkeypatch.setattr(diagnose_runtime_health, "diagnose", fake_diagnose)
    assert diagnose_runtime_health.main() == 0


def test_runtime_healthcheck_import_does_not_load_service_or_model_stack() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import agentscope_runtime.healthcheck; "
            "assert not {'agentscope_runtime.service', 'agentscope', 'litellm', 'uvicorn', 'fastapi'} & sys.modules.keys()",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", [200, 503])
def test_runtime_healthcheck_preserves_bwrap_and_signed_private_probe(monkeypatch, status) -> None:
    calls = []
    secret = "health-probe-test-secret"
    monkeypatch.setenv("AGENTGOV_RUNTIME_SHARED_SECRET", secret)
    monkeypatch.setenv("AGENTSCOPE_RUNTIME_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENTSCOPE_RUNTIME_PORT", "8090")

    def probe_bwrap(argv, **kwargs):
        calls.append("bwrap")
        assert argv == [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/true",
        ]
        assert kwargs["check"] is True and kwargs["timeout"] == 5

    def probe_http(request, *, timeout):
        calls.append("http")
        assert calls == ["bwrap", "http"]
        assert request.full_url == "http://127.0.0.1:8090/health" and timeout == 2
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-user-id"] == RUNTIME_USER_ID
        assert verify_runtime_gateway_request(
            secret, headers[TIMESTAMP_HEADER.lower()], headers[SIGNATURE_HEADER.lower()], RUNTIME_USER_ID, "GET", "/health", b""
        )
        return nullcontext(SimpleNamespace(status=status))

    monkeypatch.setattr(healthcheck.subprocess, "run", probe_bwrap)
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", probe_http)
    if status == 200:
        healthcheck.main()
    else:
        with pytest.raises(SystemExit) as exc:
            healthcheck.main()
        assert exc.value.code == 1
    assert calls == ["bwrap", "http"]


def test_runtime_healthcheck_does_not_bypass_failed_bwrap(monkeypatch) -> None:
    def failed_bwrap(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    def forbidden_http(*args, **kwargs):
        pytest.fail("bwrap 失败后不得继续请求 Runtime 并报告健康")

    monkeypatch.setattr(healthcheck.subprocess, "run", failed_bwrap)
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", forbidden_http)

    with pytest.raises(subprocess.CalledProcessError):
        healthcheck.main()
