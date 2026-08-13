from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
authority = load_module(
    "agentgov_container_acceptance_receipt_authority_tests",
    REPO_ROOT / "scripts/container_acceptance_receipt_authority.py",
)


def _reference(tmp_path: Path, *, phase: str = "prepared") -> object:
    anchor = tmp_path / "private"
    receipt_root = anchor / "receipts"
    run_id = "1700000000-a1b2c3d4e5f6"
    chain = tuple((index + 1, index + 10) for index in range(len(receipt_root.parts)))
    return authority.ReceiptFileAuthority(
        phase,
        receipt_root / f"{run_id}.json",
        "a" * 64,
        anchor,
        receipt_root,
        chain,
        (101, 202),
    )


@pytest.mark.parametrize("phase", ("reserved", "prepared"))
def test_receipt_file_authority_round_trips_canonically(tmp_path: Path, phase: str) -> None:
    expected = _reference(tmp_path, phase=phase)
    raw = expected.to_json()

    assert authority.ReceiptFileAuthority.from_json(raw, expected_phase=phase) == expected
    assert json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")) == raw


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.__setitem__("authority_sha256", "0" * 64), "digest"),
        (lambda payload: payload.__setitem__("phase", "reserved"), "digest|phase"),
        (lambda payload: payload.__setitem__("root_chain", []), "digest|chain"),
        (lambda payload: payload.__setitem__("unexpected", True), "invalid"),
    ),
)
def test_receipt_file_authority_rejects_mutation(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    payload = json.loads(_reference(tmp_path).to_json())
    mutation(payload)

    with pytest.raises(authority.ReceiptAuthorityError, match=message):
        authority.ReceiptFileAuthority.from_json(json.dumps(payload), expected_phase="prepared")


def test_receipt_file_authority_rejects_wrong_expected_phase(tmp_path: Path) -> None:
    raw = _reference(tmp_path, phase="reserved").to_json()

    with pytest.raises(authority.ReceiptAuthorityError, match="digest"):
        authority.ReceiptFileAuthority.from_json(raw, expected_phase="prepared")
