from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Literal, TypedDict, cast

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    governed_git_command,
    governed_git_environment,
    require_governed_repository,
)
from app.runtime.agent_git_store import GitAgentVersionStore


class GitCommandError(RuntimeError):
    pass


WorkspaceOperationRefName = Literal["original", "base", "original-index-tree", "candidate", "target"]
WorkspaceOperationRefs = TypedDict(
    "WorkspaceOperationRefs",
    {
        "original": str,
        "base": str,
        "candidate": str,
        "target": str,
        "original-index-tree": str,
    },
    total=False,
)


def anchor_workspace_operation_snapshot(
    store: GitAgentVersionStore,
    *,
    operation_id: str,
    original_commit: str,
    base_commit: str,
    original_index_tree: str,
) -> None:
    expected: WorkspaceOperationRefs = {
        "original": original_commit,
        "base": base_commit,
        "original-index-tree": original_index_tree,
    }
    for name, object_sha in expected.items():
        object_type = "tree" if name == "original-index-tree" else "commit"
        _update_operation_ref(store.repository_dir, operation_id, name, object_sha, object_type=object_type)
    verify_workspace_operation_refs(store, operation_id, expected=expected)


def anchor_workspace_operation_candidate(
    store: GitAgentVersionStore,
    operation_id: str,
    candidate_commit: str,
) -> None:
    _update_operation_ref(store.repository_dir, operation_id, "candidate", candidate_commit, object_type="commit")
    verify_workspace_operation_refs(store, operation_id, expected={"candidate": candidate_commit})


def anchor_workspace_operation_target(
    store: GitAgentVersionStore,
    operation_id: str,
    target_commit: str,
) -> None:
    _update_operation_ref(store.repository_dir, operation_id, "target", target_commit, object_type="commit")
    verify_workspace_operation_refs(store, operation_id, expected={"target": target_commit})


def verify_workspace_operation_refs(
    store: GitAgentVersionStore,
    operation_id: str,
    *,
    expected: WorkspaceOperationRefs,
    exact: bool = False,
) -> None:
    values = _operation_ref_values(store.repository_dir, operation_id)
    for name, object_sha in expected.items():
        actual = values.get(name)
        if actual != object_sha:
            raise GitCommandError(f"Workspace activation durable ref {name} does not match its journal")
        expected_type = "tree" if name == "original-index-tree" else "commit"
        if _git_text(store.repository_dir, ["cat-file", "-t", actual]).strip() != expected_type:
            raise GitCommandError(f"Workspace activation durable ref {name} has the wrong object type")
    if exact and values != expected:
        raise GitCommandError("Workspace activation durable ref namespace does not exactly match its journal")


def delete_workspace_operation_refs(
    repository: Path,
    operation_id: str,
    *,
    expected: WorkspaceOperationRefs | None = None,
) -> None:
    values = _operation_ref_values(repository, operation_id)
    if not values:
        return
    if set(values) - _KNOWN_REF_KINDS:
        raise GitCommandError(f"Workspace activation durable ref namespace is invalid: {operation_id}")
    if expected is not None and values != expected:
        raise GitCommandError(f"Workspace activation durable refs do not match terminal outcome: {operation_id}")
    head_before = _head_identity(repository, operation_id)
    prefix = _operation_ref(operation_id)
    commands = ["start", *(f"delete {prefix}{name} {oid}" for name, oid in sorted(values.items())), "prepare", "commit"]
    _run_git(
        repository,
        ["update-ref", "--no-deref", "--stdin"],
        input_data=("\n".join(commands) + "\n").encode(),
    )
    if _operation_ref_values(repository, operation_id):
        raise GitCommandError(f"Workspace activation durable refs were not removed: {operation_id}")
    if _head_identity(repository, operation_id) != head_before:
        raise GitCommandError("Workspace HEAD changed while durable refs were removed")


def validate_operation_id(operation_id: str) -> None:
    suffix = operation_id.removeprefix("wao-")
    try:
        parsed = uuid.UUID(suffix)
    except (ValueError, AttributeError) as exc:
        raise GitCommandError("Workspace activation operation id is unsafe for Git state") from exc
    if not operation_id.startswith("wao-") or str(parsed) != suffix:
        raise GitCommandError("Workspace activation operation id is unsafe for Git state")


def _operation_ref(operation_id: str, name: str | None = None) -> str:
    validate_operation_id(operation_id)
    prefix = f"refs/agentgov/workspace-activations/{operation_id}"
    return f"{prefix}/{name}" if name else f"{prefix}/"


_KNOWN_REF_KINDS = {"original", "base", "original-index-tree", "candidate", "target"}


def _operation_ref_values(repository: Path, operation_id: str) -> WorkspaceOperationRefs:
    prefix = _operation_ref(operation_id)
    raw = _git_text(
        repository,
        [
            "for-each-ref",
            "--format=%(refname)\t%(objectname)\t%(symref)",
            prefix,
        ],
    )
    values: WorkspaceOperationRefs = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise GitCommandError("Workspace activation durable ref namespace is malformed")
        ref_name, object_sha, symbolic_target = fields
        if symbolic_target:
            raise GitCommandError("Workspace activation durable ref namespace contains a symbolic ref")
        if not ref_name.startswith(prefix):
            raise GitCommandError("Workspace activation durable ref namespace escaped its prefix")
        name = ref_name.removeprefix(prefix)
        if name not in _KNOWN_REF_KINDS or name in values:
            raise GitCommandError("Workspace activation durable ref namespace is invalid")
        values[cast(WorkspaceOperationRefName, name)] = object_sha
    return values


def _update_operation_ref(
    repository: Path,
    operation_id: str,
    name: str,
    object_sha: str,
    *,
    object_type: str,
) -> None:
    if _git_text(repository, ["cat-file", "-t", object_sha]).strip() != object_type:
        raise GitCommandError(f"Workspace activation cannot anchor non-{object_type} object {name}")
    ref_name = _operation_ref(operation_id, name)
    existing = _operation_ref_values(repository, operation_id).get(cast(WorkspaceOperationRefName, name))
    if existing:
        if existing != object_sha:
            raise GitCommandError(f"Workspace activation durable ref collision: {name}")
        return
    object_format = _git_text(repository, ["rev-parse", "--show-object-format"]).strip()
    zero_oid = "0" * (64 if object_format == "sha256" else 40)
    _run_git(repository, ["update-ref", "--no-deref", ref_name, object_sha, zero_oid])


def _head_identity(repository: Path, operation_id: str) -> tuple[str, str]:
    symbolic_name = _git_text(
        repository,
        ["rev-parse", "--symbolic-full-name", "HEAD"],
    ).strip()
    if symbolic_name != "HEAD" and not symbolic_name.startswith("refs/heads/"):
        raise GitCommandError(f"Workspace HEAD cannot reference activation operation {operation_id} state")
    head_sha = _git_text(repository, ["rev-parse", "--verify", "HEAD"]).strip()
    if not head_sha:
        raise GitCommandError("Workspace HEAD is unavailable during durable ref cleanup")
    return symbolic_name, head_sha


def _run_git(
    repository: Path,
    args: list[str],
    *,
    check: bool = True,
    input_data: bytes | None = None,
) -> bytes:
    try:
        require_governed_repository(repository)
        command = governed_git_command(repository, args)
    except GovernedGitEnvironmentError as exc:
        raise GitCommandError("Workspace durable-ref authority rejected the repository") from exc
    process = subprocess.run(
        command,
        cwd=str(repository),
        env=governed_git_environment(repository=repository, optional_locks=False),
        capture_output=True,
        check=False,
        input=input_data,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
        raise GitCommandError(detail or f"git {' '.join(args)} failed")
    return process.stdout


def _git_text(repository: Path, args: list[str], *, check: bool = True) -> str:
    return _run_git(repository, args, check=check).decode("utf-8", errors="replace")
