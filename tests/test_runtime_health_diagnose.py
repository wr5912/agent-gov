from __future__ import annotations

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


def test_diagnose_can_require_model_readiness(monkeypatch) -> None:
    responses = iter(
        [
            (200, {"status": "ok"}, None),
            (503, {"status": "not_ready", "model_provider": {"status": "checking"}}, None),
        ]
    )
    monkeypatch.setattr(diagnose_runtime_health, "_get_json", lambda *_args, **_kwargs: next(responses))

    assert diagnose_runtime_health.diagnose(api_base="http://runtime", wait_seconds=0, require_ready=True) == 2
