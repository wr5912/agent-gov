from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_container_acceptance_test_support import candidate_authority, image_evidence, load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = load_module("agentgov_container_acceptance_signal_tests", REPO_ROOT / "scripts/run_container_acceptance.py")
BLOCKING_CHILD = (
    "import os,signal,sys,time; from pathlib import Path; "
    "signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
)


def _wait_for_pid(path: Path) -> int:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    return int(path.read_text(encoding="utf-8"))


def _assert_pid_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_signal_controller_escalates_and_reaps_only_the_active_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child.pid"
    monkeypatch.setattr(RUNNER, "CANCEL_FORCE_KILL_SECONDS", 0.05)
    monkeypatch.setattr(RUNNER, "PROCESS_WAIT_POLL_SECONDS", 0.01)
    observed: list[int] = []

    with RUNNER._controlled_signals() as controller:

        def interrupt() -> None:
            pid = _wait_for_pid(marker)
            observed.append(pid)
            controller._handle(signal.SIGTERM, None)

        sender = threading.Thread(target=interrupt)
        sender.start()
        with pytest.raises(RUNNER.AcceptanceCancelled) as cancelled:
            RUNNER._run_process(
                [sys.executable, "-c", BLOCKING_CHILD, str(marker)],
                cwd=tmp_path,
                env={"PATH": os.defpath},
                capture=True,
            )
        sender.join(timeout=2)

    assert cancelled.value.signum == signal.SIGTERM
    assert len(observed) == 1
    _assert_pid_gone(observed[0])


def test_cleanup_timeout_reaps_local_fake_process_without_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cleanup.pid"
    monkeypatch.setattr(RUNNER, "CANCEL_FORCE_KILL_SECONDS", 0.05)
    monkeypatch.setattr(RUNNER, "CLEANUP_PROCESS_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(RUNNER, "PROCESS_WAIT_POLL_SECONDS", 0.01)
    controller = RUNNER._SignalController()
    controller._begin_cleanup()
    token = RUNNER._ACTIVE_SIGNAL_CONTROLLER.set(controller)
    try:
        with pytest.raises(RUNNER.AcceptanceError, match="cleanup subprocess 超时"):
            RUNNER._run_process(
                [sys.executable, "-c", BLOCKING_CHILD, str(marker)],
                cwd=tmp_path,
                env={"PATH": os.defpath},
                capture=True,
            )
    finally:
        RUNNER._ACTIVE_SIGNAL_CONTROLLER.reset(token)

    _assert_pid_gone(_wait_for_pid(marker))


def test_cleanup_deadline_exceeds_compose_stop_grace_period() -> None:
    assert int(RUNNER.acceptance_environment._COMPOSE_STOP_TIMEOUT_SECONDS) < RUNNER.CLEANUP_PROCESS_TIMEOUT_SECONDS


def _context() -> object:
    candidate = candidate_authority(RUNNER, profile="core")
    return SimpleNamespace(
        profile=RUNNER.PROFILES["core"],
        verifier=RUNNER.acceptance_contract.VERIFIER_REGISTRY["core"][0],
        candidate=candidate,
        receipt=object(),
        managed={
            RUNNER.RUN_ID_ENV: candidate.snapshot.run_id,
            RUNNER.acceptance_toolchain.DOCKER_EXECUTABLE_ENV: "/usr/bin/docker",
        },
        lock=SimpleNamespace(assert_current=lambda: None),
        runtime=object(),
    )


def _successful_fake_execution(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> object:
    context = _context()
    images = image_evidence(RUNNER, context.profile)
    monkeypatch.setattr(RUNNER, "_require_source_current", lambda _candidate: None)
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: images)
    monkeypatch.setattr(RUNNER.acceptance_verifier_process, "run_verifier_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(RUNNER.acceptance_contract, "verifier_environment", lambda env: env)
    monkeypatch.setattr(RUNNER, "_require_images_current", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(RUNNER, "_cleanup_runtime", lambda *_args: events.append("runtime-cleanup"))
    monkeypatch.setattr(
        RUNNER.acceptance_terminal,
        "capture_cleanup_witness",
        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(RUNNER.acceptance_terminal, "require_terminal_freshness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        RUNNER.acceptance_candidate,
        "cleanup_candidate_snapshot",
        lambda *_args, **_kwargs: events.append("candidate-cleanup"),
    )
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "receipt_root_requirement", lambda: object())
    monkeypatch.setattr(RUNNER.acceptance_toolchain, "validate_receipt_root", lambda _requirement: None)
    monkeypatch.setattr(
        RUNNER.acceptance_receipt,
        "transition_receipt",
        lambda *_args, **kwargs: (events.append(f"terminal:{kwargs['status']}"), (Path("receipt"), "d" * 64))[1],
    )
    return context


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_interruption_terminalizes_only_after_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    signum: signal.Signals,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)
    controller = RUNNER._SignalController()
    controller.signum = signum

    with pytest.raises(RUNNER.AcceptanceCancelled) as cancelled:
        RUNNER._execute_with_cleanup(context, controller)

    assert cancelled.value.signum == signum
    assert controller.sealed
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal:failed"]


def test_interruption_cleanup_failure_retains_prepared_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)
    controller = RUNNER._SignalController()
    controller.signum = signal.SIGTERM

    def fail_cleanup(*_args: object) -> None:
        events.append("runtime-cleanup-failed")
        raise RUNNER.AcceptanceError("cleanup failed")

    monkeypatch.setattr(RUNNER, "_cleanup_runtime", fail_cleanup)

    with pytest.raises(RUNNER.AcceptanceError, match="cleanup failed"):
        RUNNER._execute_with_cleanup(context, controller)

    assert not controller.sealed
    assert events == ["runtime-cleanup-failed"]


def test_signal_during_terminal_transition_is_after_the_commit_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)

    def transition(*_args: object, **kwargs: object) -> tuple[Path, str]:
        events.append(f"terminal:{kwargs['status']}")
        signal.raise_signal(signal.SIGTERM)
        assert signal.SIGTERM in signal.sigpending()
        return Path("receipt"), "d" * 64

    monkeypatch.setattr(RUNNER.acceptance_receipt, "transition_receipt", transition)
    with RUNNER._controlled_signals() as controller:
        assert RUNNER._execute_with_cleanup(context, controller) == 0

    assert controller.sealed
    assert controller.signum is None
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal:succeeded"]


def test_signal_after_terminal_commit_does_not_change_receipt_or_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)

    with RUNNER._controlled_signals() as controller:
        result = RUNNER._execute_with_cleanup(context, controller)
        controller._handle(signal.SIGINT, None)

    assert result == 0
    assert controller.sealed
    assert controller.signum is None
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal:succeeded"]


def test_failed_terminal_transition_keeps_prepared_and_cannot_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)
    prepared = SimpleNamespace(status="prepared")
    context.receipt = prepared

    def fail_transition(receipt: object, **kwargs: object) -> tuple[Path, str]:
        assert receipt is prepared
        events.append(f"terminal-attempt:{kwargs['status']}")
        signal.raise_signal(signal.SIGTERM)
        raise RuntimeError("terminal commit failed")

    monkeypatch.setattr(RUNNER.acceptance_receipt, "transition_receipt", fail_transition)
    with RUNNER._controlled_signals() as controller, pytest.raises(RuntimeError, match="terminal commit failed"):
        RUNNER._execute_with_cleanup(context, controller)

    assert prepared.status == "prepared"
    assert controller.signum == signal.SIGTERM
    assert not controller.sealed
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal-attempt:succeeded"]


def test_signal_interrupts_python_freshness_barrier_then_exactly_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)
    started = threading.Event()
    calls = 0

    def freshness(_candidate: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            return
        started.set()
        while True:
            time.sleep(0.01)

    monkeypatch.setattr(RUNNER, "_require_source_current", freshness)

    def interrupt() -> None:
        assert started.wait(timeout=2)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=interrupt)
    sender.start()
    before = time.monotonic()
    with RUNNER._controlled_signals() as controller, pytest.raises(RUNNER.AcceptanceCancelled) as cancelled:
        RUNNER._execute_with_cleanup(context, controller)
    sender.join(timeout=2)

    assert cancelled.value.signum == signal.SIGTERM
    assert time.monotonic() - before < 2
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal:failed"]


def test_pending_handoff_signal_is_unblocked_only_after_handler_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = _successful_fake_execution(monkeypatch, events)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        signal.raise_signal(signal.SIGTERM)
        with RUNNER._controlled_signals() as controller, pytest.raises(RUNNER.AcceptanceCancelled) as cancelled:
            RUNNER._execute_with_cleanup(context, controller)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    assert cancelled.value.signum == signal.SIGTERM
    assert events == ["runtime-cleanup", "candidate-cleanup", "terminal:failed"]
