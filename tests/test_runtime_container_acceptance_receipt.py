from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest

from runtime_container_acceptance_test_support import candidate_authority, candidate_reservation, load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
receipt = load_module("agentgov_container_acceptance_receipt_tests", REPO_ROOT / "scripts/container_acceptance_receipt.py")
receipt.acceptance_contract.acceptance_toolchain.activate_toolchain_authority(receipt.acceptance_contract.acceptance_toolchain.capture_toolchain_authority())

VERIFIER = receipt.acceptance_contract.VERIFIER_REGISTRY["agent-test"][0]
MANAGED_DIGEST = "c" * 64
LOCK_DIGEST = "1" * 64
RUN_ID = "1700000000-a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _isolated_abstract_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    address = f"\0agentgov.receipt.test.{os.getpid()}.{secrets.token_hex(6)}".encode()
    monkeypatch.setattr(receipt.acceptance_lock, "_LOCK_ADDRESS", address)
    monkeypatch.setattr(receipt.acceptance_candidate, "verify_candidate_snapshot", lambda _candidate: None)


def _reserved(root: Path, *, run_id: str = RUN_ID, lock_digest: str = LOCK_DIGEST) -> object:
    reservation = candidate_reservation(receipt, run_id=run_id, parent=root.parent / "candidates")
    return receipt.write_reserved_receipt(
        receipt_root=root,
        reservation=reservation,
        verifier=VERIFIER,
        lifecycle_lock_sha256=lock_digest,
        trusted_anchor=root.parent,
    )


def _prepared(root: Path, *, run_id: str = RUN_ID, lock_digest: str = LOCK_DIGEST) -> object:
    reserved = _reserved(root, run_id=run_id, lock_digest=lock_digest)
    candidate = candidate_authority(
        receipt,
        run_id=run_id,
        reservation=reserved.identity.candidate_reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
    )
    return receipt.transition_reserved_to_prepared(
        reserved,
        candidate=candidate,
        managed_environment_sha256=MANAGED_DIGEST,
        cleanup_on_failure=lambda _identity: None,
    )


def _payload(authority: object) -> dict[str, object]:
    return json.loads(authority.path.read_text(encoding="utf-8"))


def _lock() -> object:
    return receipt.acceptance_lock.acquire_lifecycle_lock({}, timeout_seconds=0)


def _drift_current_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    original_payload = receipt.acceptance_contract.verifier_receipt_payload

    def drifted_toolchain(profile: str, verifier: object) -> dict[str, object]:
        payload = cast(dict[str, object], json.loads(json.dumps(original_payload(profile, verifier))))
        toolchain = cast(dict[str, object], payload["toolchain"])
        frontend = cast(dict[str, object], toolchain["frontend_dependencies"])
        frontend["projection_sha256"] = "f" * 64
        unsigned = {key: value for key, value in payload.items() if key != "contract_sha256"}
        payload["contract_sha256"] = hashlib.sha256(receipt.acceptance_contract.canonical_json(unsigned)).hexdigest()
        return payload

    monkeypatch.setattr(receipt.acceptance_contract, "verifier_receipt_payload", drifted_toolchain)


def test_historical_terminal_receipt_is_self_contained_across_live_toolchain_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    reserved = _reserved(root)

    assert root.stat().st_mode & 0o777 == 0o700
    assert reserved.path.stat().st_mode & 0o777 == 0o600
    assert _payload(reserved)["status"] == "reserved"
    assert _payload(reserved)["lifecycle_lock_sha256"] == LOCK_DIGEST
    assert receipt.ReservedReceiptAuthority.from_json(reserved.to_json()) == reserved
    assert reserved.verify_current() == reserved.reserved_sha256
    _drift_current_verifier(monkeypatch)
    assert reserved.verify_current() == reserved.reserved_sha256
    assert receipt.ReservedReceiptAuthority.from_json(reserved.to_json()) == reserved
    receipt.transition_reserved_failure(reserved)
    current = _lock()
    try:
        assert (
            receipt.recover_stale_receipts(
                root,
                lock=current,
                cleanup_reserved=lambda _authority: pytest.fail("terminal receipt must not be cleaned"),
                cleanup_prepared=lambda _identity: pytest.fail("terminal receipt must not be cleaned"),
                trusted_anchor=root.parent,
            )
            == ()
        )
    finally:
        current.close()
    tampered = _payload(reserved)
    verifier_payload = cast(dict[str, object], tampered["verifier"])
    toolchain = cast(dict[str, object], verifier_payload["toolchain"])
    frontend = cast(dict[str, object], toolchain["frontend_dependencies"])
    frontend["projection_sha256"] = "0" * 64
    with pytest.raises(receipt.receipt_authority.ReceiptAuthorityError, match="verifier"):
        receipt.receipt_authority.reserved_identity(tampered)
    tampered = _payload(reserved)
    verifier_payload = cast(dict[str, object], tampered["verifier"])
    verifier_payload["identity"] = "different-verifier"
    verifier_payload["invocation_argv"] = ["make", "--no-print-directory", "_different-verifier"]
    verifier_payload["execution_template"] = {
        "executable": "/usr/bin/make",
        "arguments": ["--no-print-directory", "-f", "Makefile", "_different-verifier"],
    }
    unsigned = {key: value for key, value in verifier_payload.items() if key != "contract_sha256"}
    verifier_payload["contract_sha256"] = hashlib.sha256(receipt.acceptance_contract.canonical_json(unsigned)).hexdigest()
    with pytest.raises(receipt.receipt_authority.ReceiptAuthorityError, match="verifier"):
        receipt.receipt_authority.reserved_identity(tampered)
    tampered = _payload(reserved)
    verifier_payload = cast(dict[str, object], tampered["verifier"])
    toolchain = cast(dict[str, object], verifier_payload["toolchain"])
    tools = cast(list[dict[str, object]], toolchain["tools"])
    tools[-1]["command"] = "bogus"
    unsigned = {key: value for key, value in verifier_payload.items() if key != "contract_sha256"}
    verifier_payload["contract_sha256"] = hashlib.sha256(receipt.acceptance_contract.canonical_json(unsigned)).hexdigest()
    with pytest.raises(receipt.receipt_authority.ReceiptAuthorityError, match="verifier"):
        receipt.receipt_authority.reserved_identity(tampered)


def test_default_root_uses_fixed_private_state_requirement() -> None:
    requirement = receipt.acceptance_toolchain.receipt_root_requirement()

    assert receipt.default_receipt_root() == requirement.path
    assert requirement.path.stat().st_mode & 0o777 == 0o700
    receipt.acceptance_toolchain.validate_receipt_root(requirement)


def test_prepared_cas_freezes_identity_and_terminal_accepts_only_results(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "receipts")

    assert receipt.PreparedReceiptAuthority.from_json(prepared.to_json()) == prepared
    assert prepared.verify_current() == prepared.prepared_sha256
    assert prepared.lifecycle_lock_sha256 == LOCK_DIGEST
    assert set(inspect.signature(receipt.transition_receipt).parameters) == {
        "prepared",
        "status",
        "images",
        "child_returncode",
    }
    with pytest.raises(FrozenInstanceError):
        prepared.identity.reserved.profile = "core"


def test_prepared_receipt_accepts_only_its_original_lifecycle_lock(tmp_path: Path) -> None:
    current = _lock()
    try:
        digest = receipt.acceptance_lock.lifecycle_descriptor_sha256(current.descriptor)
        prepared = _prepared(tmp_path / "receipts", lock_digest=digest)
        prepared.verify_lifecycle_lock(current.descriptor)
    finally:
        current.close()

    replacement = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="lifecycle lock"):
            prepared.verify_lifecycle_lock(replacement.descriptor)
    finally:
        replacement.close()


def test_terminal_is_single_atomic_transition(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "receipts")
    path, digest = receipt.transition_receipt(prepared, status="failed", images=())

    assert len(digest) == 64
    assert _payload(prepared)["status"] == "failed"
    terminal = path.read_bytes()
    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="replaced|authoritative"):
        receipt.transition_receipt(prepared, status="failed", images=())
    assert path.read_bytes() == terminal


def test_reserved_can_only_close_as_failed_before_candidate_exists(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    receipt.transition_reserved_failure(reserved)

    payload = _payload(reserved)
    assert payload["status"] == "failed"
    assert "candidate_snapshot" not in payload
    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="replaced|authoritative"):
        receipt.transition_reserved_failure(reserved)


def test_prepared_rejects_candidate_bound_to_another_reserved_digest(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    candidate = candidate_authority(
        receipt,
        reservation=reserved.identity.candidate_reservation,
        reserved_receipt_sha256="f" * 64,
    )

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="inconsistent"):
        receipt.transition_reserved_to_prepared(
            reserved,
            candidate=candidate,
            managed_environment_sha256=MANAGED_DIGEST,
            cleanup_on_failure=lambda _identity: None,
        )
    assert _payload(reserved)["status"] == "reserved"


def test_prepared_rejects_source_path_mismatch_before_redacting_source(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    candidate = candidate_authority(
        receipt,
        reservation=reserved.identity.candidate_reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
    )
    candidate = replace(candidate, source=replace(candidate.source, repository_root=Path("/redirected-source")))

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="source does not match"):
        receipt.transition_reserved_to_prepared(
            reserved,
            candidate=candidate,
            managed_environment_sha256=MANAGED_DIGEST,
            cleanup_on_failure=lambda _identity: None,
        )
    assert _payload(reserved)["status"] == "reserved"


def test_prepared_post_publish_failure_cleans_candidate_then_closes_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reserved = _reserved(tmp_path / "receipts")
    candidate = candidate_authority(
        receipt,
        reservation=reserved.identity.candidate_reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
    )
    original = receipt.PreparedReceiptAuthority.verify_current
    calls = 0
    cleaned: list[object] = []

    def fail_once(authority: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise receipt.acceptance_support.AcceptanceSupportError("post-publish failure")
        return original(authority)

    monkeypatch.setattr(receipt.PreparedReceiptAuthority, "verify_current", fail_once)
    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="post-publish"):
        receipt.transition_reserved_to_prepared(
            reserved,
            candidate=candidate,
            managed_environment_sha256=MANAGED_DIGEST,
            cleanup_on_failure=cleaned.append,
        )

    assert len(cleaned) == 1
    assert _payload(reserved)["status"] == "failed"


def test_prepared_publication_cleanup_failure_retains_prepared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reserved = _reserved(tmp_path / "receipts")
    candidate = candidate_authority(
        receipt,
        reservation=reserved.identity.candidate_reservation,
        reserved_receipt_sha256=reserved.reserved_sha256,
    )
    monkeypatch.setattr(
        receipt.PreparedReceiptAuthority,
        "verify_current",
        lambda _authority: (_ for _ in ()).throw(receipt.acceptance_support.AcceptanceSupportError("verify failed")),
    )

    def fail_cleanup(_identity: object) -> None:
        raise RuntimeError("cleanup failed")

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="cleanup failed"):
        receipt.transition_reserved_to_prepared(
            reserved,
            candidate=candidate,
            managed_environment_sha256=MANAGED_DIGEST,
            cleanup_on_failure=fail_cleanup,
        )
    assert _payload(reserved)["status"] == "prepared"


def test_reserved_publish_failure_before_link_leaves_absent_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(receipt.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("link failed")))
    root = tmp_path / "receipts"

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="persisted"):
        _reserved(root)
    assert not (root / f"{RUN_ID}.json").exists()
    assert not tuple(root.glob("*.tmp"))


def test_reserved_post_publish_verification_failure_is_terminalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = receipt.ReservedReceiptAuthority.verify_current
    calls = 0

    def fail_once(authority: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise receipt.acceptance_support.AcceptanceSupportError("post-publish failure")
        return original(authority)

    monkeypatch.setattr(receipt.ReservedReceiptAuthority, "verify_current", fail_once)
    root = tmp_path / "receipts"
    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="post-publish"):
        _reserved(root)
    assert json.loads((root / f"{RUN_ID}.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_serialized_authority_rejects_identity_mutation(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    serialized = json.loads(reserved.to_json())
    serialized["file_identity"][1] += 1

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="digest"):
        receipt.ReservedReceiptAuthority.from_json(json.dumps(serialized))
    payload = _payload(reserved)
    verifier_payload = cast(dict[str, object], payload["verifier"])
    toolchain = cast(dict[str, object], verifier_payload["toolchain"])
    frontend = cast(dict[str, object], toolchain["frontend_dependencies"])
    frontend["projection_sha256"] = "e" * 64
    unsigned = {key: value for key, value in verifier_payload.items() if key != "contract_sha256"}
    verifier_payload["contract_sha256"] = hashlib.sha256(receipt.acceptance_contract.canonical_json(unsigned)).hexdigest()
    reserved.path.write_bytes(receipt.acceptance_contract.canonical_json(payload) + b"\n")
    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="contract"):
        reserved.verify_current()


def test_transition_rejects_private_parent_chain_replacement(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    reserved = _reserved(anchor / "receipts")
    displaced = tmp_path / "displaced"
    anchor.rename(displaced)
    anchor.mkdir(mode=0o700)
    (anchor / "receipts").mkdir(mode=0o700)

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="parent chain"):
        receipt.transition_reserved_failure(reserved)
    assert json.loads((displaced / "receipts" / reserved.path.name).read_text(encoding="utf-8"))["status"] == "reserved"


def test_existing_non_private_receipt_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o755)

    with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="private chain"):
        _reserved(root)
    assert root.stat().st_mode & 0o777 == 0o755


def test_stale_reserved_cleanup_then_terminal_under_lock(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    lifecycle_lock = _lock()
    cleaned: list[object] = []
    try:
        recovered = receipt.recover_stale_receipts(
            reserved.path.parent,
            lock=lifecycle_lock,
            cleanup_reserved=cleaned.append,
            cleanup_prepared=lambda _identity: None,
            trusted_anchor=reserved.path.parent.parent,
        )
    finally:
        lifecycle_lock.close()

    assert cleaned == [reserved]
    assert recovered[0].phase == "reserved"
    assert _payload(reserved)["status"] == "failed"


def test_stale_reserved_cleanup_failure_retains_reserved(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    lifecycle_lock = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="receipt retained"):
            receipt.recover_stale_receipts(
                reserved.path.parent,
                lock=lifecycle_lock,
                cleanup_reserved=lambda _authority: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
                cleanup_prepared=lambda _identity: None,
                trusted_anchor=reserved.path.parent.parent,
            )
    finally:
        lifecycle_lock.close()
    assert _payload(reserved)["status"] == "reserved"


def test_stale_prepared_cleanup_then_terminal_under_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "receipts")
    frozen_verifier = _payload(prepared)["verifier"]
    _drift_current_verifier(monkeypatch)
    lifecycle_lock = _lock()
    cleaned: list[object] = []
    try:
        recovered = receipt.recover_stale_receipts(
            prepared.path.parent,
            lock=lifecycle_lock,
            cleanup_reserved=lambda _authority: None,
            cleanup_prepared=cleaned.append,
            trusted_anchor=prepared.path.parent.parent,
        )
    finally:
        lifecycle_lock.close()

    assert cleaned == [prepared.identity]
    assert recovered[0].phase == "prepared"
    assert _payload(prepared)["status"] == "failed"
    assert _payload(prepared)["verifier"] == frozen_verifier


def test_stale_prepared_cleanup_failure_retains_prepared(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "receipts")
    lifecycle_lock = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="receipt retained"):
            receipt.recover_stale_receipts(
                prepared.path.parent,
                lock=lifecycle_lock,
                cleanup_reserved=lambda _authority: None,
                cleanup_prepared=lambda _identity: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
                trusted_anchor=prepared.path.parent.parent,
            )
    finally:
        lifecycle_lock.close()
    assert _payload(prepared)["status"] == "prepared"


def test_stale_scan_recovers_interrupted_reserved_hardlink(tmp_path: Path) -> None:
    reserved = _reserved(tmp_path / "receipts")
    stale_temp = reserved.path.parent / f".{RUN_ID}.reserved-{'e' * 16}.tmp"
    os.link(reserved.path, stale_temp)
    lifecycle_lock = _lock()
    try:
        recovered = receipt.recover_stale_receipts(
            reserved.path.parent,
            lock=lifecycle_lock,
            cleanup_reserved=lambda _authority: None,
            cleanup_prepared=lambda _identity: None,
            trusted_anchor=reserved.path.parent.parent,
        )
    finally:
        lifecycle_lock.close()

    assert len(recovered) == 1
    assert not stale_temp.exists()
    assert _payload(reserved)["status"] == "failed"


def test_stale_scan_budget_and_unmanaged_residue_fail_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(receipt, "_MAX_RECEIPT_ENTRIES", 1)
    for index in range(3):
        leaf = root / f".{RUN_ID}.terminal-{index:016x}.tmp"
        leaf.write_text("partial", encoding="utf-8")
        leaf.chmod(0o600)
    lifecycle_lock = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="entry limit"):
            receipt.recover_stale_receipts(
                root,
                lock=lifecycle_lock,
                cleanup_reserved=lambda _authority: None,
                cleanup_prepared=lambda _identity: None,
                trusted_anchor=root.parent,
            )
    finally:
        lifecycle_lock.close()
    assert len(tuple(root.iterdir())) == 3


def test_terminal_retention_prunes_oldest_receipt_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    monkeypatch.setattr(receipt, "_MAX_RECEIPT_ENTRIES", 2)
    run_ids = tuple(f"17000000{index}-a1b2c3d4e5f{index}" for index in range(1, 3))
    for run_id in run_ids:
        receipt.transition_reserved_failure(_reserved(root, run_id=run_id))
    lifecycle_lock = _lock()
    try:
        receipt.recover_stale_receipts(
            root,
            lock=lifecycle_lock,
            cleanup_reserved=lambda _authority: pytest.fail("terminal receipt must not be cleaned"),
            cleanup_prepared=lambda _identity: pytest.fail("terminal receipt must not be cleaned"),
            trusted_anchor=root.parent,
        )
    finally:
        lifecycle_lock.close()

    assert not root.joinpath(f"{run_ids[0]}.json").exists()
    assert tuple(path.name for path in sorted(root.iterdir())) == (f"{run_ids[1]}.json",)


def test_terminal_retention_never_prunes_nonterminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    monkeypatch.setattr(receipt, "_MAX_RECEIPT_ENTRIES", 2)
    receipt.transition_reserved_failure(_reserved(root, run_id="170000011-a1b2c3d4e5f1"))
    reserved = _reserved(root, run_id="170000012-a1b2c3d4e5f2")
    lifecycle_lock = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="receipt retained"):
            receipt.recover_stale_receipts(
                root,
                lock=lifecycle_lock,
                cleanup_reserved=lambda _authority: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
                cleanup_prepared=lambda _identity: None,
                trusted_anchor=root.parent,
            )
    finally:
        lifecycle_lock.close()

    assert _payload(reserved)["status"] == "reserved"
    assert len(tuple(root.iterdir())) == 2


def test_terminal_retention_delete_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "receipts"
    monkeypatch.setattr(receipt, "_MAX_RECEIPT_ENTRIES", 2)
    run_ids = tuple(f"17000002{index}-a1b2c3d4e5f{index}" for index in range(1, 3))
    for run_id in run_ids:
        receipt.transition_reserved_failure(_reserved(root, run_id=run_id))
    original_unlink = receipt.receipt_retention.os.unlink

    def deny_oldest(path: str, *, dir_fd: int) -> None:
        if path == f"{run_ids[0]}.json":
            raise OSError("denied")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(receipt.receipt_retention.os, "unlink", deny_oldest)
    lifecycle_lock = _lock()
    try:
        with pytest.raises(receipt.acceptance_support.AcceptanceSupportError, match="pruned safely"):
            receipt.recover_stale_receipts(
                root,
                lock=lifecycle_lock,
                cleanup_reserved=lambda _authority: None,
                cleanup_prepared=lambda _identity: None,
                trusted_anchor=root.parent,
            )
    finally:
        lifecycle_lock.close()

    assert tuple(path.name for path in sorted(root.iterdir())) == tuple(f"{run_id}.json" for run_id in run_ids)
