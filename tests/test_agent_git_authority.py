from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from app.runtime.agent_git_errors import AgentGitError
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_repository_guard import (
    AgentRepositoryGuardError,
    AgentRepositoryTemporaryEntryAuthority,
    agent_repository_temporary_authority,
)
from app.runtime.recovery_read_only_git import (
    HardenedReadOnlyGitRepository,
    RecoveryReadOnlyGitError,
)
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_activation_refs import (
    GitCommandError,
    anchor_workspace_operation_candidate,
    delete_workspace_operation_refs,
    verify_workspace_operation_refs,
)
from app.services.agent_workspace_git_operations import (
    activate_candidate,
    commit_parent_shas,
    observe_live_workspace,
    prepare_workspace_snapshot,
    run_git,
    workspace_status,
)


def test_repository_filter_driver_never_executes_during_core_or_operator_status(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    marker = tmp_path / "filter-ran"
    driver = _marker_program(tmp_path / "filter-driver", marker, passthrough=True)
    repository.joinpath(".gitattributes").write_text(
        "tracked.txt filter=hostile\n",
        encoding="utf-8",
    )
    _git(repository, "config", "filter.hostile.clean", str(driver))

    with pytest.raises(GitCommandError, match="authority rejected"):
        workspace_status(repository)
    with pytest.raises(RecoveryReadOnlyGitError):
        HardenedReadOnlyGitRepository(repository).status()

    assert not marker.exists()


def test_repository_signing_program_never_executes_during_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    marker = tmp_path / "signer-ran"
    signer = _marker_program(tmp_path / "signer", marker)
    repository.joinpath("tracked.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "config", "commit.gpgSign", "true")
    _git(repository, "config", "gpg.program", str(signer))

    with pytest.raises(GitCommandError, match="authority rejected"):
        run_git(repository, ["commit", "-m", "must not sign"])

    assert not marker.exists()


def test_repository_core_worktree_cannot_redirect_governed_commands(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "tracked.txt"
    sentinel.write_text("external\n", encoding="utf-8")
    _git(repository, "config", "core.worktree", str(external))

    with pytest.raises(GitCommandError, match="authority rejected"):
        workspace_status(repository)
    with pytest.raises(RecoveryReadOnlyGitError):
        HardenedReadOnlyGitRepository(repository).status()

    assert sentinel.read_text(encoding="utf-8") == "external\n"


def test_repository_alternates_cannot_import_another_agent_object_graph(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "agent-a")
    external = _repository(tmp_path / "agent-b")
    external_head = _git(external, "rev-parse", "HEAD")
    alternates = repository / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(external / ".git" / "objects") + "\n", encoding="utf-8")
    store = _store(repository, tmp_path)

    with pytest.raises(AgentGitError, match="authority rejected"):
        store.resolve_commit_sha(external_head)


def test_repository_grafts_cannot_rewrite_commit_parent_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    repository.joinpath("tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "second")
    head = _git(repository, "rev-parse", "HEAD")
    repository.joinpath(".git", "info", "grafts").write_text(head + "\n", encoding="utf-8")

    with pytest.raises(GitCommandError, match="authority rejected"):
        commit_parent_shas(repository, head)
    with pytest.raises(RecoveryReadOnlyGitError):
        HardenedReadOnlyGitRepository(repository).commit_parent_shas(head)


def test_repository_include_fifo_is_rejected_without_following_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    include_fifo = tmp_path / "config-fifo"
    os.mkfifo(include_fifo)
    _git(repository, "config", "include.path", str(include_fifo))

    started = time.monotonic()
    with pytest.raises(GitCommandError, match="authority rejected"):
        workspace_status(repository)

    assert time.monotonic() - started < 2.0


def test_repository_archive_command_never_executes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    marker = tmp_path / "archive-ran"
    command = _marker_program(tmp_path / "archive-command", marker, passthrough=True)
    _git(repository, "config", "tar.tar.gz.command", str(command))
    store = _store(repository, tmp_path)

    with pytest.raises(AgentGitError, match="authority rejected"):
        store.archive_ref("HEAD")

    assert not marker.exists()


def test_commit_blob_reader_ignores_repository_replace_refs(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    original_blob = _git(repository, "rev-parse", "HEAD:tracked.txt")
    replacement_blob = _git(
        repository,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"altered\n",
    )
    _git(repository, "replace", original_blob, replacement_blob)

    entries = package_codec.read_commit_entries(
        repository,
        commit_sha,
        run_git=run_git,
    )

    assert {entry.relative_path.as_posix(): entry.content for entry in entries}["tracked.txt"] == b"initial\n"


def test_symbolic_operation_ref_is_rejected_without_deleting_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    store = _store(repository, tmp_path)
    operation_id = f"wao-{uuid4()}"
    candidate = _git(repository, "rev-parse", "HEAD")
    branch = _git(repository, "symbolic-ref", "HEAD")
    operation_ref = _operation_ref(operation_id, "candidate")
    _git(repository, "symbolic-ref", operation_ref, branch)

    with pytest.raises(GitCommandError, match="symbolic ref"):
        verify_workspace_operation_refs(
            store,
            operation_id,
            expected={"candidate": candidate},
            exact=True,
        )
    with pytest.raises(GitCommandError, match="symbolic ref"):
        delete_workspace_operation_refs(
            repository,
            operation_id,
            expected={"candidate": candidate},
        )

    assert _git(repository, "rev-parse", branch) == candidate


def test_head_cannot_reference_operation_namespace_during_ref_cleanup(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    operation_id = f"wao-{uuid4()}"
    candidate = _git(repository, "rev-parse", "HEAD")
    operation_ref = _operation_ref(operation_id, "candidate")
    _git(repository, "update-ref", operation_ref, candidate)
    _git(repository, "symbolic-ref", "HEAD", operation_ref)

    with pytest.raises(GitCommandError, match="authority rejected"):
        delete_workspace_operation_refs(
            repository,
            operation_id,
            expected={"candidate": candidate},
        )

    assert _git(repository, "rev-parse", "HEAD") == candidate
    assert _git(repository, "rev-parse", operation_ref) == candidate


def test_head_cannot_reference_operation_namespace_during_activation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    store = _store(repository, tmp_path)
    operation_id = f"wao-{uuid4()}"
    observation = observe_live_workspace(store)
    snapshot = prepare_workspace_snapshot(
        store,
        observation=observation,
        operation_id=operation_id,
    )
    anchor_workspace_operation_candidate(store, operation_id, snapshot.current_head)
    original_ref = _operation_ref(operation_id, "original")
    _git(repository, "symbolic-ref", "HEAD", original_ref)

    with pytest.raises(GitCommandError, match="authority rejected"):
        activate_candidate(
            store,
            snapshot=snapshot,
            candidate_commit=snapshot.current_head,
            operation_id=operation_id,
            before_activate=lambda: None,
        )

    assert _git(repository, "rev-parse", original_ref) == snapshot.original_head


def test_repository_commondir_cannot_redirect_agent_authority(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "agent-a")
    external = _repository(tmp_path / "agent-b")
    repository.joinpath(".git", "commondir").write_text(
        str(external / ".git") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GitCommandError, match="authority rejected"):
        workspace_status(repository)
    with pytest.raises(RecoveryReadOnlyGitError):
        HardenedReadOnlyGitRepository(repository).head_sha()


@pytest.mark.parametrize("root_name", ["workspace-package-worktrees", "workspace-package-indexes"])
def test_temporary_authority_rejects_replaced_parent_without_cleaning_new_target(
    tmp_path: Path,
    root_name: str,
) -> None:
    parent = tmp_path / "version"
    parent.mkdir()
    entry: AgentRepositoryTemporaryEntryAuthority | None = None
    marker = parent / root_name / "operation" / "marker"
    try:
        with pytest.raises(AgentRepositoryGuardError, match="parent authority.*replaced"):
            with agent_repository_temporary_authority(parent, root_name, create=True) as authority:
                entry = authority.create_entry("operation")
                parent.rename(tmp_path / "displaced-version")
                marker.parent.mkdir(parents=True)
                marker.write_bytes(b"replacement-owned")
                authority.remove_entry(entry)
    finally:
        if entry is not None:
            entry.close()

    assert marker.read_bytes() == b"replacement-owned"


@pytest.mark.parametrize("root_name", ["workspace-package-worktrees", "workspace-package-indexes"])
def test_temporary_authority_rejects_replaced_entry_without_cleaning_new_target(
    tmp_path: Path,
    root_name: str,
) -> None:
    parent = tmp_path / "version"
    parent.mkdir()
    with agent_repository_temporary_authority(parent, root_name, create=True) as authority:
        entry = authority.create_entry("operation")
        try:
            entry.path.rename(authority.path / "displaced-operation")
            entry.path.mkdir()
            marker = entry.path / "marker"
            marker.write_bytes(b"replacement-owned")

            with pytest.raises(AgentRepositoryGuardError, match="temporary entry.*replaced"):
                authority.remove_entry(entry)

            assert marker.read_bytes() == b"replacement-owned"
        finally:
            entry.close()


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Authority Test")
    _git(path, "config", "user.email", "authority@example.local")
    path.joinpath("tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")
    return path


def _store(repository: Path, root: Path) -> GitAgentVersionStore:
    worktrees = root / "version" / "worktrees"
    releases = root / "version" / "releases"
    worktrees.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)
    return GitAgentVersionStore(
        repository_dir=repository,
        worktrees_dir=worktrees,
        releases_dir=releases,
    )


def _marker_program(
    path: Path,
    marker: Path,
    *,
    passthrough: bool = False,
) -> Path:
    body = ["#!/bin/sh", f"touch {shlex.quote(str(marker))}"]
    if passthrough:
        body.append("cat")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _operation_ref(operation_id: str, name: str) -> str:
    return f"refs/agentgov/workspace-activations/{operation_id}/{name}"


def _git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> str:
    return (
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            input=input_bytes,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )
