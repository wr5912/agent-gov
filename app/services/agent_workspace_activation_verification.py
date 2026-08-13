from __future__ import annotations

from collections.abc import Callable

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.workspace_activation_graph import (
    workspace_activation_early_graph_shape_is_valid,
    workspace_activation_graph_is_valid,
    workspace_activation_graph_shape_is_valid,
)
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from app.services.agent_workspace_activation_journal import durable_ref_values, required_sha
from app.services.agent_workspace_activation_refs import (
    GitCommandError,
    WorkspaceOperationRefs,
    verify_workspace_operation_refs,
)
from app.services.agent_workspace_git_evidence import (
    canonical_index_fingerprint,
    index_fingerprint,
    object_type_or_none,
)
from app.services.agent_workspace_git_operations import (
    commit_parent_shas,
    commit_tree_sha,
    git_text,
    require_clean_activation_workspace,
    run_git,
    workspace_fingerprint,
    workspace_status,
)
from app.services.agent_workspace_index_state import index_snapshot


def verify_candidate(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
    *,
    allow_missing_historical_objects: bool = False,
) -> None:
    repository = store.repository_dir
    candidate_commit = required_sha(operation.candidate_commit_sha, "candidate")
    candidate_tree = required_sha(operation.candidate_tree_sha, "candidate tree")
    head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if head != candidate_commit:
        raise WorkspaceActivationVerificationError("Workspace candidate is not the current HEAD")
    verify_operation_graph(
        operation,
        store,
        allow_missing_historical_objects=allow_missing_historical_objects,
    )
    if commit_tree_sha(repository, head) != candidate_tree:
        raise WorkspaceActivationVerificationError("Workspace candidate tree no longer matches its journal")
    if operation.action in {"import_overwrite", "import_unchanged"}:
        entries = package_codec.read_commit_entries(repository, head, run_git=run_git)
        if package_codec.tree_sha256(entries) != operation.tree_sha256:
            raise WorkspaceActivationVerificationError("Workspace candidate package tree failed verification")
    if index_fingerprint(repository) != canonical_index_fingerprint(repository, candidate_commit):
        raise WorkspaceActivationVerificationError("Workspace candidate index contains non-canonical stage or flags")
    require_clean_activation_workspace(repository)


def verify_durable_refs(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
) -> None:
    verify_workspace_operation_refs(
        store,
        operation.operation_id,
        expected=durable_ref_values(operation),
        exact=True,
    )


def verify_staged_completion_evidence(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
) -> WorkspaceOperationRefs:
    observed_refs = _verify_present_or_cleaned_refs(
        operation,
        store,
        expected=durable_ref_values(operation),
    )
    verify_candidate(
        operation,
        store,
        allow_missing_historical_objects=not observed_refs,
    )
    return observed_refs


def verify_final_workspace(
    operation: AgentWorkspaceActivationOperationModel,
    *,
    store_for: Callable[[str], GitAgentVersionStore],
) -> None:
    repository = store_for(operation.agent_id).repository_dir
    head = git_text(repository, ["rev-parse", "HEAD"]).strip()
    if head != operation.candidate_commit_sha:
        raise WorkspaceActivationVerificationError("Workspace HEAD changed during activation finalization")
    try:
        require_clean_activation_workspace(repository)
    except package_codec.WorkspacePackageError as exc:
        raise WorkspaceActivationVerificationError("Workspace changed during activation finalization") from exc


def verify_rejected_workspace(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
) -> None:
    repository = store.repository_dir
    graph_fields = (
        operation.base_commit_sha,
        operation.candidate_commit_sha,
        operation.candidate_tree_sha,
        operation.original_index_tree_sha,
    )
    if all(graph_fields):
        verify_operation_graph_shape(operation, store=store)
    elif any(graph_fields):
        raise WorkspaceActivationVerificationError("Rejected Workspace activation graph journal is partial")
    elif not workspace_activation_early_graph_shape_is_valid(
        action=operation.action,
        snapshot_created=operation.snapshot_created,
        original_commit=operation.original_head_sha,
        base_commit=operation.base_commit_sha,
        candidate_commit=operation.candidate_commit_sha,
        candidate_tree=operation.candidate_tree_sha,
        target_commit=operation.target_commit_sha,
        original_index_tree=operation.original_index_tree_sha,
    ):
        raise WorkspaceActivationVerificationError("Rejected Workspace activation graph journal is invalid")
    if git_text(repository, ["rev-parse", "HEAD"]).strip() != operation.original_head_sha:
        raise WorkspaceActivationVerificationError("Rejected Workspace HEAD no longer matches its original observation")
    if workspace_status(repository) != operation.original_status_text:
        raise WorkspaceActivationVerificationError("Rejected Workspace status no longer matches its original observation")
    if index_fingerprint(repository) != operation.original_index_fingerprint:
        raise WorkspaceActivationVerificationError("Rejected Workspace index no longer matches its original observation")
    if operation.original_index_snapshot is None or index_snapshot(repository) != operation.original_index_snapshot:
        raise WorkspaceActivationVerificationError("Rejected Workspace index bytes no longer match its original observation")
    if workspace_fingerprint(repository) != operation.original_workspace_fingerprint:
        raise WorkspaceActivationVerificationError("Rejected Workspace bytes no longer match its original observation")


def verify_staged_rejection_evidence(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
) -> WorkspaceOperationRefs:
    if operation.recovery_phase != "rejection_outcome" or operation.state not in {
        "rejecting",
        "recovery_required",
    }:
        raise WorkspaceActivationVerificationError("Workspace activation has no staged rejected outcome")
    verify_rejected_workspace(operation, store)
    fields = (
        operation.base_commit_sha,
        operation.candidate_commit_sha,
        operation.original_index_tree_sha,
    )
    if all(fields):
        expected = durable_ref_values(operation)
    elif any(fields):
        raise WorkspaceActivationVerificationError("Workspace rejected outcome has an incomplete durable ref journal")
    else:
        expected = {}
    observed_refs = _verify_present_or_cleaned_refs(operation, store, expected=expected)
    if observed_refs:
        verify_operation_graph(operation, store)
    return observed_refs


def _verify_present_or_cleaned_refs(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
    *,
    expected: WorkspaceOperationRefs,
) -> WorkspaceOperationRefs:
    try:
        verify_workspace_operation_refs(
            store,
            operation.operation_id,
            expected=expected,
            exact=True,
        )
        return expected
    except GitCommandError as present_error:
        if not expected:
            raise WorkspaceActivationVerificationError("Workspace activation durable refs are not exactly cleaned") from present_error
        try:
            verify_workspace_operation_refs(
                store,
                operation.operation_id,
                expected={},
                exact=True,
            )
        except GitCommandError as cleaned_error:
            raise WorkspaceActivationVerificationError("Workspace activation durable refs are partial or conflict with its journal") from cleaned_error
        return {}


def verify_candidate_parent(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
) -> None:
    verify_operation_graph(operation, store)


def verify_operation_graph(
    operation: AgentWorkspaceActivationOperationModel,
    store: GitAgentVersionStore,
    *,
    allow_missing_historical_objects: bool = False,
) -> None:
    repository = store.repository_dir
    if not workspace_activation_graph_is_valid(
        action=operation.action,
        snapshot_created=operation.snapshot_created,
        original_commit=operation.original_head_sha,
        base_commit=operation.base_commit_sha or "",
        candidate_commit=operation.candidate_commit_sha or "",
        candidate_tree=operation.candidate_tree_sha or "",
        target_commit=operation.target_commit_sha,
        original_index_tree=operation.original_index_tree_sha or "",
        object_type=lambda object_id: object_type_or_none(repository, object_id),
        commit_parent_shas=lambda commit: commit_parent_shas(repository, commit),
        commit_tree_sha=lambda commit: commit_tree_sha(repository, commit),
        allow_missing_historical_objects=allow_missing_historical_objects,
    ):
        raise WorkspaceActivationVerificationError("Workspace activation commit graph conflicts with its journal")


def verify_operation_graph_shape(
    operation: AgentWorkspaceActivationOperationModel,
    *,
    store: GitAgentVersionStore,
) -> None:
    if not workspace_activation_graph_shape_is_valid(
        action=operation.action,
        snapshot_created=operation.snapshot_created,
        original_commit=operation.original_head_sha,
        base_commit=operation.base_commit_sha or "",
        candidate_commit=operation.candidate_commit_sha or "",
        candidate_tree=operation.candidate_tree_sha or "",
        target_commit=operation.target_commit_sha,
        original_index_tree=operation.original_index_tree_sha or "",
    ):
        raise WorkspaceActivationVerificationError("Workspace activation commit graph conflicts with its journal")
