from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import scripts.container_acceptance_candidate as acceptance_candidate
import scripts.container_acceptance_candidate_cleanup as candidate_cleanup
import scripts.container_acceptance_lock as acceptance_lock
import scripts.container_acceptance_receipt as acceptance_receipt
import scripts.container_acceptance_toolchain as acceptance_toolchain

from test_container_acceptance_candidate_authority import (
    LOCAL_GIT,
    _candidate_repository,
    _candidate_requirements,
    _loaded_sources,
)
from test_container_acceptance_toolchain_authority import _daemon, _dependency

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
RUN_ID = "1700000000-a1b2c3d4e5f6"


class _InterruptedCleanup(BaseException):
    pass


@pytest.fixture(autouse=True)
def _isolated_toolchain_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceptance_toolchain, "_ACTIVE_AUTHORITY", None)


def test_real_candidate_reservation_prepares_and_cas_publishes_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    acceptance_toolchain.activate_toolchain_authority(
        acceptance_toolchain.capture_toolchain_authority(
            dependency_capturer=_dependency,
            daemon_capturer=_daemon,
        )
    )
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    reservation = acceptance_candidate.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=RUN_ID,
        profile="agent-test",
        parent_requirement=acceptance_toolchain.candidate_snapshot_parent_requirement(),
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )
    reserved = acceptance_receipt.write_reserved_receipt(
        receipt_root=tmp_path / "receipts",
        reservation=reservation,
        verifier=acceptance_receipt.acceptance_contract.VERIFIER_REGISTRY["agent-test"][0],
        lifecycle_lock_sha256="1" * 64,
        trusted_anchor=tmp_path,
    )
    candidate = acceptance_candidate.prepare_candidate_snapshot(
        reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
        loaded_sources=_loaded_sources(repository),
        git_authority=LOCAL_GIT,
        dependency_projection=acceptance_toolchain.frontend_dependency_projection_requirement(),
        python_dependencies=acceptance_toolchain.python_dependency_snapshot_requirement(),
        pnpm_dependencies=acceptance_toolchain.pnpm_dependency_snapshot_requirement(),
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        projection_validator=acceptance_toolchain.validate_frontend_dependency_projection,
        python_validator=acceptance_toolchain.validate_python_dependency_snapshot,
        pnpm_validator=acceptance_toolchain.validate_pnpm_dependency_snapshot,
    )
    prepared = acceptance_receipt.transition_reserved_to_prepared(
        reserved,
        candidate=candidate,
        managed_environment_sha256="c" * 64,
        cleanup_on_failure=lambda _identity: acceptance_candidate.cleanup_candidate_snapshot(candidate),
    )

    assert prepared.identity.candidate_snapshot == candidate.recovery
    assert prepared.identity.reserved_sha256 == reserved.reserved_sha256
    assert prepared.verify_current() == prepared.prepared_sha256

    acceptance_candidate.cleanup_candidate_snapshot(
        candidate,
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )
    acceptance_receipt.transition_receipt(prepared, status="failed", images=())


@pytest.mark.parametrize("phase", ["root-renamed", "root-rmdir-before-fsync"])
def test_stale_prepared_receipt_retries_interrupted_candidate_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    acceptance_toolchain.activate_toolchain_authority(
        acceptance_toolchain.capture_toolchain_authority(
            dependency_capturer=_dependency,
            daemon_capturer=_daemon,
        )
    )
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    reservation = acceptance_candidate.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=RUN_ID,
        profile="agent-test",
        parent_requirement=acceptance_toolchain.candidate_snapshot_parent_requirement(),
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
    )
    receipt_root = tmp_path / "receipts"
    reserved = acceptance_receipt.write_reserved_receipt(
        receipt_root=receipt_root,
        reservation=reservation,
        verifier=acceptance_receipt.acceptance_contract.VERIFIER_REGISTRY["agent-test"][0],
        lifecycle_lock_sha256="1" * 64,
        trusted_anchor=tmp_path,
    )
    candidate = acceptance_candidate.prepare_candidate_snapshot(
        reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
        loaded_sources=_loaded_sources(repository),
        git_authority=LOCAL_GIT,
        dependency_projection=acceptance_toolchain.frontend_dependency_projection_requirement(),
        python_dependencies=acceptance_toolchain.python_dependency_snapshot_requirement(),
        pnpm_dependencies=acceptance_toolchain.pnpm_dependency_snapshot_requirement(),
        parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        projection_validator=acceptance_toolchain.validate_frontend_dependency_projection,
        python_validator=acceptance_toolchain.validate_python_dependency_snapshot,
        pnpm_validator=acceptance_toolchain.validate_pnpm_dependency_snapshot,
    )
    prepared = acceptance_receipt.transition_reserved_to_prepared(
        reserved,
        candidate=candidate,
        managed_environment_sha256="c" * 64,
        cleanup_on_failure=lambda _identity: acceptance_candidate.cleanup_candidate_snapshot(candidate),
    )
    interrupted = False

    def interrupt_once(observed: str) -> None:
        nonlocal interrupted
        if observed == phase and not interrupted:
            interrupted = True
            raise _InterruptedCleanup

    def cleanup(identity: acceptance_receipt.PreparedReceiptIdentity) -> None:
        acceptance_candidate.recover_and_cleanup_candidate_snapshot(
            identity.candidate_snapshot,
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        )

    monkeypatch.setattr(candidate_cleanup, "_checkpoint", interrupt_once)
    with acceptance_lock.lifecycle_lock({}) as lock:
        assert prepared.lifecycle_lock_sha256 != acceptance_lock.lifecycle_descriptor_sha256(lock.descriptor)
        with pytest.raises(_InterruptedCleanup):
            acceptance_receipt.recover_stale_receipts(
                receipt_root,
                lock=lock,
                cleanup_reserved=lambda _authority: pytest.fail("unexpected reserved receipt"),
                cleanup_prepared=cleanup,
                trusted_anchor=tmp_path,
            )
        assert json.loads(prepared.path.read_text(encoding="utf-8"))["status"] == "prepared"
        monkeypatch.setattr(candidate_cleanup, "_checkpoint", lambda _phase: None)
        recovered = acceptance_receipt.recover_stale_receipts(
            receipt_root,
            lock=lock,
            cleanup_reserved=lambda _authority: pytest.fail("unexpected reserved receipt"),
            cleanup_prepared=cleanup,
            trusted_anchor=tmp_path,
        )

    assert interrupted
    assert tuple(item.phase for item in recovered) == ("prepared",)
    assert json.loads(prepared.path.read_text(encoding="utf-8"))["status"] == "failed"
    assert not any(candidate.snapshot.parent.iterdir())
