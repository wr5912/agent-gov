"""候选清理到单一终态发布之间的末端 freshness witness。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts import agent_test_acceptance_support as acceptance_support
from scripts import container_acceptance_candidate as acceptance_candidate
from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_candidate_git as candidate_git
from scripts import container_acceptance_image_authority as image_authority
from scripts import container_acceptance_receipt as acceptance_receipt
from scripts import container_acceptance_signals as acceptance_signals
from scripts import container_acceptance_toolchain as acceptance_toolchain


class TerminalFreshnessError(RuntimeError):
    """清理期间 live source、toolchain、browser、daemon 或 image 已漂移。"""


def safe_docker_runner(action: Callable[[list[str]], str]) -> image_authority.DockerRunner:
    def run(command: list[str]) -> str:
        try:
            return action(command)
        except BaseException as exc:
            raise TerminalFreshnessError("terminal Docker authority query failed") from exc

    return run


def validate_post_cleanup_freshness(
    failure: BaseException | None,
    *,
    require_images: Callable[[], None] | None,
    require_source: Callable[[], None],
    expected_errors: tuple[type[BaseException], ...],
) -> BaseException | None:
    try:
        if require_images is not None:
            require_images()
        require_source()
    except expected_errors as observed:
        if failure is None:
            return observed
        failure.add_note(f"additional freshness failure: {type(observed).__name__}")
    return failure


@dataclass(frozen=True, slots=True)
class TerminalFreshnessWitness:
    candidate: candidate_authority.PreparedCandidateAuthority
    source: candidate_git.FrozenCandidateIndex
    images: tuple[image_authority.TerminalImageEvidence, ...]
    docker: str
    run_id: str

    def close(self) -> None:
        self.source.close()


@dataclass(frozen=True, slots=True)
class AcceptanceOutcome:
    result: int | None
    images: tuple[image_authority.LocalImageEvidence, ...]
    failure: BaseException | None


def execute_acceptance(
    controller: acceptance_signals.SignalController,
    *,
    require_source: Callable[[], None],
    refresh: Callable[[], tuple[image_authority.LocalImageEvidence, ...]],
    verifier: Callable[[], int],
    require_images: Callable[[tuple[image_authority.LocalImageEvidence, ...]], None],
    mark_failure: Callable[[BaseException, str], BaseException],
) -> AcceptanceOutcome:
    result: int | None = None
    images: tuple[image_authority.LocalImageEvidence, ...] = ()
    phase = "source_preflight"
    try:
        controller._raise_if_cancelled()
        controller._run_interruptible(require_source)
        phase = "profile_refresh"
        images = refresh()
        phase = "verifier"
        result = verifier()
        phase = "source_postflight"
        controller._run_interruptible(require_source)
        phase = "image_postflight"
        require_images(images)
        return AcceptanceOutcome(result, images, None)
    except BaseException as exc:
        return AcceptanceOutcome(result, images, mark_failure(exc, phase))


def cleanup_acceptance(
    outcome: AcceptanceOutcome,
    *,
    cleanup_runtime: Callable[[], None],
    validate_freshness: Callable[[BaseException | None], BaseException | None],
    capture_witness: Callable[[], TerminalFreshnessWitness],
    cleanup_candidate: Callable[[], None],
    mark_failure: Callable[[BaseException, str], BaseException],
) -> tuple[BaseException | None, TerminalFreshnessWitness | None]:
    failure = outcome.failure
    witness: TerminalFreshnessWitness | None = None
    phase = "runtime_cleanup"
    try:
        cleanup_runtime()
        phase = "cleanup_freshness"
        previous = failure
        failure = validate_freshness(failure)
        if failure is not None and failure is not previous:
            failure = mark_failure(failure, phase)
        if failure is None:
            try:
                witness = capture_witness()
            except BaseException as exc:
                wrapped = exc
                if not isinstance(exc, TerminalFreshnessError):
                    wrapped = TerminalFreshnessError("terminal freshness witness could not be captured")
                    wrapped.__cause__ = exc
                failure = mark_failure(wrapped, "terminal_freshness")
        phase = "candidate_cleanup"
        cleanup_candidate()
        return failure, witness
    except BaseException as exc:
        if witness is not None:
            witness.close()
        cleanup_failure = mark_failure(exc, phase)
        if failure is not None:
            cleanup_failure.add_note(f"primary acceptance failure: {type(failure).__name__}")
        raise cleanup_failure from exc


def capture_terminal_freshness(
    candidate: candidate_authority.PreparedCandidateAuthority,
    *,
    compose_base: list[str],
    images: tuple[image_authority.LocalImageEvidence, ...],
    running_services: tuple[str, ...],
    run_id: str,
    identity: candidate_authority.AcceptanceCandidateIdentity,
    docker_runner: image_authority.DockerRunner,
) -> TerminalFreshnessWitness:
    source: candidate_git.FrozenCandidateIndex | None = None
    try:
        source = acceptance_candidate.freeze_candidate_source_current(
            candidate,
            parent_validator=acceptance_toolchain.validate_candidate_snapshot_parent,
        )
        terminal_images = image_authority.capture_terminal_image_evidence(
            compose_base=compose_base,
            images=images,
            running_services=running_services,
            run_id=run_id,
            candidate=identity,
            docker_runner=docker_runner,
        )
        docker = compose_base[0]
        return TerminalFreshnessWitness(candidate, source, terminal_images, docker, run_id)
    except (
        acceptance_candidate.CandidateSnapshotError,
        acceptance_support.AcceptanceSupportError,
        acceptance_toolchain.ToolchainAuthorityError,
        TerminalFreshnessError,
    ) as exc:
        if source is not None:
            source.close()
        raise TerminalFreshnessError("terminal freshness witness could not be captured") from exc


def require_terminal_freshness(
    witness: TerminalFreshnessWitness,
    *,
    identity: candidate_authority.AcceptanceCandidateIdentity,
    docker_runner: image_authority.DockerRunner,
) -> None:
    try:
        acceptance_toolchain.validate_execution_tool_authority()
        acceptance_toolchain.validate_browser_runtime_authority()
        image_authority.verify_terminal_image_evidence(
            witness.images,
            docker=witness.docker,
            run_id=witness.run_id,
            candidate=identity,
            docker_runner=docker_runner,
        )
        acceptance_candidate.require_frozen_candidate_source_current(witness.candidate, witness.source)
        acceptance_toolchain.validate_dependency_tree_generations()
        acceptance_candidate.require_frozen_candidate_generation_current(witness.candidate, witness.source)
    except (
        acceptance_candidate.CandidateSnapshotError,
        acceptance_support.AcceptanceSupportError,
        acceptance_toolchain.ToolchainAuthorityError,
        TerminalFreshnessError,
    ) as exc:
        raise TerminalFreshnessError("terminal freshness witness drifted during candidate cleanup") from exc


def commit_terminal_receipt(
    controller: acceptance_signals.SignalController,
    receipt: acceptance_receipt.PreparedReceiptAuthority,
    *,
    result: int | None,
    images: tuple[image_authority.LocalImageEvidence, ...],
    failure: BaseException | None,
    witness: TerminalFreshnessWitness | None,
    identity: candidate_authority.AcceptanceCandidateIdentity,
    docker_runner: image_authority.DockerRunner,
    mark_failure: Callable[[BaseException, str], BaseException],
) -> tuple[str, int | None, BaseException | None]:
    def publish(cancellation: int | None) -> tuple[str, BaseException | None]:
        terminal_failure = failure
        if terminal_failure is None:
            try:
                if witness is None:
                    raise TerminalFreshnessError("terminal freshness witness is unavailable")
                require_terminal_freshness(witness, identity=identity, docker_runner=docker_runner)
            except TerminalFreshnessError as exc:
                terminal_failure = mark_failure(exc, "terminal_freshness")
        status = "failed" if terminal_failure is not None or cancellation is not None else "succeeded" if result == 0 else "child_failed"
        _path, digest = acceptance_receipt.transition_receipt(receipt, status=status, images=images, child_returncode=result)
        return digest, terminal_failure

    (digest, terminal_failure), cancellation = controller._commit_terminal(publish)
    return digest, cancellation, terminal_failure


def capture_cleanup_witness(
    candidate: candidate_authority.PreparedCandidateAuthority,
    *,
    compose_base: list[str],
    images: tuple[image_authority.LocalImageEvidence, ...],
    running_services: tuple[str, ...],
    run_id: str,
    identity: candidate_authority.AcceptanceCandidateIdentity,
    docker_action: Callable[[list[str]], str],
) -> TerminalFreshnessWitness:
    return capture_terminal_freshness(
        candidate,
        compose_base=compose_base,
        images=images,
        running_services=running_services,
        run_id=run_id,
        identity=identity,
        docker_runner=safe_docker_runner(docker_action),
    )
