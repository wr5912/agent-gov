"""Snapshot runner 到 private Make capability gate 的 typed 进程适配。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from scripts import agent_test_acceptance_support as acceptance_support
from scripts import container_acceptance_contract as acceptance_contract
from scripts import container_acceptance_make_gate as acceptance_make_gate

if TYPE_CHECKING:
    from scripts.container_acceptance_candidate_authority import PreparedCandidateAuthority
    from scripts.container_acceptance_lock import AcceptanceLifecycleLock
    from scripts.container_acceptance_profiles import AcceptanceProfile
    from scripts.container_acceptance_receipt import PreparedReceiptAuthority


class VerifierProcessRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> int: ...


class VerifierRunContext(Protocol):
    @property
    def profile(self) -> AcceptanceProfile: ...

    @property
    def verifier(self) -> acceptance_contract.AcceptanceVerifierIdentity: ...

    @property
    def candidate(self) -> PreparedCandidateAuthority: ...

    @property
    def receipt(self) -> PreparedReceiptAuthority: ...

    @property
    def managed(self) -> Mapping[str, str]: ...

    @property
    def lock(self) -> AcceptanceLifecycleLock: ...


def run_verifier_process(
    context: VerifierRunContext,
    command: Sequence[str],
    *,
    process_runner: VerifierProcessRunner,
) -> int:
    """Validate the managed verifier environment and enter the one-shot gate."""

    try:
        managed = acceptance_contract.verifier_environment(context.managed)
        return acceptance_make_gate.run_make_verifier(
            command,
            managed,
            profile=context.profile.name,
            verifier=context.verifier,
            candidate=context.candidate,
            receipt=context.receipt,
            lock=context.lock,
            process_runner=process_runner,
        )
    except (OSError, acceptance_contract.AcceptanceContractError, acceptance_make_gate.MakeGateError) as exc:
        raise acceptance_support.AcceptanceSupportError("private Make verifier authority is invalid") from exc
