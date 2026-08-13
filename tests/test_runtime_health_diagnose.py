from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import scripts.diagnose_runtime_health as diagnose_runtime_health


def test_diagnose_prints_specific_degraded_provider_reason(monkeypatch, capsys) -> None:
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (
                503,
                {
                    "status": "not_ready",
                    "model_provider": {
                        "status": "degraded",
                        "error_code": "VLLM_VERSION_PROBE_FAILED",
                        "reason": "timeout",
                        "probe": "vllm_version",
                        "duration_ms": 30001,
                        "retryable": True,
                        "action": "verify external vLLM",
                    },
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    result = diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=False)

    assert result == 0
    output = capsys.readouterr().out
    assert "API: healthy" in output
    assert "Model provider: degraded" in output
    assert "error_code=VLLM_VERSION_PROBE_FAILED" in output
    assert "reason=timeout" in output
    assert "probe=vllm_version" in output
    assert "action=verify_external_vllm" in output
    assert "根因: API 容器已存活；外部模型 provider 就绪探测失败" in output
    assert "code=VLLM_VERSION_PROBE_FAILED, reason=timeout" in output
    assert "这不是镜像启动失败，Compose dependency 报错只是次级症状" in output


def test_diagnose_never_projects_untrusted_readiness_fields(monkeypatch, capsys) -> None:
    sentinel = "PRIVATE_SENTINEL_DO_NOT_PRINT"
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (
                503,
                {
                    "model_provider": {
                        "status": "degraded",
                        "error_code": sentinel,
                        "reason": f"timeout\n\x1b[31m{sentinel}",
                        "probe": [sentinel],
                        "duration_ms": 10**20,
                        "retryable": sentinel,
                        "action": {"secret": sentinel},
                        "checked_at": f"PROVIDER_HEALTH_CONTAINER_E2E_OK\n{sentinel}",
                    },
                },
                None,
            ),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    assert diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=False) == 0

    lines = capsys.readouterr().out.splitlines()
    assert sentinel not in "\n".join(lines)
    assert "\x1b" not in "\n".join(lines)
    assert "PROVIDER_HEALTH_CONTAINER_E2E_OK" not in lines
    assert "Model provider: degraded" in lines
    assert all(len(line) <= 200 for line in lines)


def test_diagnose_maps_transport_exceptions_without_echoing_messages(monkeypatch, capsys) -> None:
    sentinel = "PRIVATE_TRANSPORT_SENTINEL"

    def fail_request(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(diagnose_runtime_health, "urlopen", fail_request)

    assert diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=False) == 1

    output = capsys.readouterr().out
    assert sentinel not in output
    assert "error=request_failed" in output


def test_diagnose_can_require_model_readiness(monkeypatch) -> None:
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (503, {"status": "not_ready", "model_provider": {"status": "checking"}}, None),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    assert diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=True) == 2


def test_diagnose_does_not_blame_provider_when_api_liveness_is_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        diagnose_runtime_health,
        "_get_json",
        lambda *_args, **_kwargs: (None, None, "ConnectionRefusedError"),
    )

    assert diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=False) == 1
    output = capsys.readouterr().out
    assert "API: unhealthy" in output
    assert "当前不能归因于外部模型 provider" in output
    assert "这不是镜像启动失败" not in output


def test_main_reads_api_defaults_from_the_selected_compose_env(
    monkeypatch,
    tmp_path,
) -> None:
    selected_env = tmp_path / "selected.env"
    selected_env.write_text("HOST_PORT=61234\n", encoding="utf-8")
    monkeypatch.setenv("COMPOSE_ENV_FILE", str(selected_env))
    monkeypatch.delenv("HOST_PORT", raising=False)
    monkeypatch.delenv("API_BASE", raising=False)
    monkeypatch.setattr(sys, "argv", ["diagnose_runtime_health.py"])

    def fake_diagnose(*, api_base: str, wait_seconds: float, require_ready: bool) -> int:
        assert api_base == "http://localhost:61234"
        assert wait_seconds == 0
        assert require_ready is False
        return 0

    monkeypatch.setattr(diagnose_runtime_health, "diagnose", fake_diagnose)

    assert diagnose_runtime_health.main() == 0


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o700)


def _health_verifier_environment(tmp_path: Path, diagnosis: str) -> dict[str, str]:
    fake_make = tmp_path / "make"
    fake_pnpm = tmp_path / "pnpm"
    fake_docker = tmp_path / "docker"
    _write_executable(fake_make, "printf '%s\\n' \"$FAKE_DIAGNOSIS\"\n")
    _write_executable(fake_pnpm, "exit 0\n")
    _write_executable(fake_docker, "printf '%s\\n' 'bounded container log'\n")
    screenshot_dir = tmp_path / "artifacts"
    screenshot_dir.mkdir()
    return {
        "AGENT_GOV_ACCEPTANCE_DOCKER": str(fake_docker),
        "AGENT_GOV_ACCEPTANCE_MAKE": str(fake_make),
        "AGENT_GOV_ACCEPTANCE_PNPM": str(fake_pnpm),
        "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY": "fixture",
        "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY_SHA256": "a" * 64,
        "AGENT_GOV_ACCEPTANCE_RUN_ID": "health-output-test",
        "AGENT_GOV_COMPOSE_ENV_FILE": str(tmp_path / "selected.env"),
        "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
        "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE": "isolated-health",
        "API_BASE": "http://127.0.0.1:1",
        "API_KEY": "test-only-private-key",
        "COMPOSE_PROJECT_NAME": "health-output-test",
        "FAKE_DIAGNOSIS": diagnosis,
        "FRONTEND_HOST_PORT": "1",
        "PATH": os.environ["PATH"],
        "VERIFY_SCREENSHOT_DIR": str(screenshot_dir),
    }


def test_isolated_health_verifier_suppresses_diagnosis_payload(tmp_path) -> None:
    sentinel = "PRIVATE_DIAGNOSIS_SENTINEL"
    diagnosis = "\n".join(
        (
            "API: healthy",
            "Model provider: degraded",
            "error_code=VLLM_VERSION_PROBE_FAILED",
            "reason=timeout",
            "根因: API 容器已存活；外部模型 provider 就绪探测失败",
            "这不是镜像启动失败，Compose dependency 报错只是次级症状",
            sentinel,
        )
    )
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        ("bash", "scripts/run_healthcheck_container_e2e.sh"),
        cwd=root,
        env=_health_verifier_environment(tmp_path, diagnosis),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "PROVIDER_HEALTH_CONTAINER_E2E_OK\n"
    assert result.stderr == ""
    assert sentinel not in result.stdout + result.stderr


def test_isolated_health_verifier_reports_only_bounded_contract_mismatch(tmp_path) -> None:
    sentinel = "PRIVATE_DIAGNOSIS_SENTINEL"
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        ("bash", "scripts/run_healthcheck_container_e2e.sh"),
        cwd=root,
        env=_health_verifier_environment(tmp_path, sentinel),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "failure_phase=provider_health_diagnose failure_code=ContractMismatch\n"
    assert sentinel not in result.stdout + result.stderr


def test_isolated_health_verifier_suppresses_diagnosis_child_errors(tmp_path) -> None:
    sentinel = "PRIVATE_DIAGNOSIS_SENTINEL"
    root = Path(__file__).resolve().parents[1]
    environment = _health_verifier_environment(tmp_path, "unused")
    _write_executable(
        Path(environment["AGENT_GOV_ACCEPTANCE_MAKE"]),
        f"printf '%s\\n' '{sentinel}' >&2\nexit 9\n",
    )

    result = subprocess.run(
        ("bash", "scripts/run_healthcheck_container_e2e.sh"),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "failure_phase=provider_health_diagnose failure_code=ChildExit\n"
    assert sentinel not in result.stdout + result.stderr
