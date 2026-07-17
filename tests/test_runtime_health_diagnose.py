from __future__ import annotations

import sys

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
    assert "action=verify external vLLM" in output
    assert "根因: API 容器已存活；外部模型 provider 就绪探测失败" in output
    assert "code=VLLM_VERSION_PROBE_FAILED, reason=timeout" in output
    assert "这不是镜像启动失败，Compose dependency 报错只是次级症状" in output


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
