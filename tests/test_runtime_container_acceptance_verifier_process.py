from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
verifier_process = load_module(
    "agentgov_container_acceptance_verifier_process_tests",
    REPO_ROOT / "scripts/container_acceptance_verifier_process.py",
)


def _context() -> object:
    return SimpleNamespace(
        profile=SimpleNamespace(name="core"),
        verifier=object(),
        candidate=object(),
        receipt=object(),
        managed={"managed": "value"},
        lock=object(),
    )


def test_verifier_process_maps_only_typed_context_into_make_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    observed: dict[str, object] = {}

    def process_runner(_command: list[str], _environment: dict[str, str], _pass_fds: tuple[int, ...]) -> int:
        return 0

    monkeypatch.setattr(
        verifier_process.acceptance_contract,
        "verifier_environment",
        lambda managed: {"validated": managed["managed"]},
    )

    def run_gate(command: object, managed: object, **kwargs: object) -> int:
        observed.update(command=command, managed=managed, **kwargs)
        return 7

    monkeypatch.setattr(verifier_process.acceptance_make_gate, "run_make_verifier", run_gate)

    assert verifier_process.run_verifier_process(context, ("make", "_smoke"), process_runner=process_runner) == 7
    assert observed == {
        "command": ("make", "_smoke"),
        "managed": {"validated": "value"},
        "profile": "core",
        "verifier": context.verifier,
        "candidate": context.candidate,
        "receipt": context.receipt,
        "lock": context.lock,
        "process_runner": process_runner,
    }


def test_verifier_process_normalizes_gate_failure_without_exposing_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier_process.acceptance_contract, "verifier_environment", lambda managed: managed)
    monkeypatch.setattr(
        verifier_process.acceptance_make_gate,
        "run_make_verifier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(verifier_process.acceptance_make_gate.MakeGateError("sentinel-details")),
    )

    with pytest.raises(verifier_process.acceptance_support.AcceptanceSupportError, match="authority") as error:
        verifier_process.run_verifier_process(
            _context(),
            ("make", "_smoke"),
            process_runner=lambda _command, _environment, _pass_fds: 0,
        )

    assert "sentinel-details" not in str(error.value)
