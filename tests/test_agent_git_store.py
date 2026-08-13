import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from app.runtime import agent_git_raw_storage as raw_storage
from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True).stdout


def test_git_store_uses_scoped_safe_directory_without_global_config(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git_bytes(repo, "init", "-q")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )

    store._configure_repo(repo)

    assert _git_bytes(repo, "config", "user.name").decode().strip() == "AgentGov"
    local_keys = _git_bytes(repo, "config", "--local", "--name-only", "--list").decode().splitlines()
    assert "safe.directory" not in local_keys


def test_git_store_file_diff_returns_unified_diff(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("one\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    first = store.ensure_bootstrap()
    repo.joinpath("CLAUDE.md").write_text("one\ntwo\n", encoding="utf-8")
    second = store.create_snapshot(reason="diff-test")

    diff = store.diff_version_file(
        str(first["agent_version_id"]),
        str(second["agent_version_id"]),
        "CLAUDE.md",
    )

    assert diff is not None
    assert diff["status"] == "modified"
    assert diff["is_text"] is True
    assert "+two" in str(diff["unified_diff"])


def test_git_store_snapshots_raw_bytes_and_exec_bit_despite_repository_attributes(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath(".gitignore").write_bytes(b".env\n*.secret\n")
    repo.joinpath(".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    repo.joinpath(".env").write_bytes(b"WORKSPACE_OWNED=true\n")
    repo.joinpath("crlf.txt").write_bytes(b"first\r\nsecond\r\n")
    tool = repo / "hooks" / "tool"
    tool.parent.mkdir()
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    tool.chmod(0o644)
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )

    store.ensure_bootstrap()
    assert _git_bytes(repo, "show", "HEAD:.env") == b"WORKSPACE_OWNED=true\n"
    assert _git_bytes(repo, "show", "HEAD:crlf.txt") == b"first\r\nsecond\r\n"
    subprocess.run(["git", "config", "core.fileMode", "false"], cwd=repo, check=True)
    tool.chmod(0o755)
    repo.joinpath("ignored.secret").write_bytes(b"workspace-owned\n")
    store.create_snapshot(reason="raw-mode")

    assert _git_bytes(repo, "show", "HEAD:crlf.txt") == b"first\r\nsecond\r\n"
    assert _git_bytes(repo, "show", "HEAD:ignored.secret") == b"workspace-owned\n"
    assert _git_bytes(repo, "ls-tree", "HEAD", "hooks/tool").split(maxsplit=1)[0] == b"100755"
    assert stat.S_IMODE(tool.stat().st_mode) & 0o111
    tool.unlink()
    store.create_snapshot(reason="tracked-delete")
    assert _git_bytes(repo, "ls-tree", "HEAD", "hooks/tool") == b""


def test_git_store_status_tracks_ignored_files_that_snapshots_preserve(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath(".gitignore").write_bytes(b"*.secret\n")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )

    store.ensure_bootstrap()
    ignored_file = repo / "ignored.secret"
    ignored_file.write_bytes(b"workspace-owned\n")

    changes = store.workspace_changes()
    assert changes == [
        {
            "path": "ignored.secret",
            "status": "untracked",
            "index_status": "!",
            "worktree_status": "!",
            "staged": False,
            "unstaged": False,
            "untracked": True,
            "ignored": True,
            "discardable": True,
        }
    ]
    status = store.repository_status()
    assert status["dirty"] is True
    assert status["changed_file_count"] == 1
    assert status["changed_files"] == changes
    assert status["file_diffs"][0]["status"] == "untracked"
    assert "+workspace-owned" in str(status["file_diffs"][0]["unified_diff"])

    discarded = store.discard_workspace_changes(["ignored.secret"])
    assert discarded["dirty"] is False
    assert not ignored_file.exists()

    ignored_file.write_bytes(b"snapshot-owned\n")
    store.create_snapshot(reason="ignored-raw-mode")

    assert _git_bytes(repo, "show", "HEAD:ignored.secret") == b"snapshot-owned\n"
    assert store.workspace_changes() == []
    assert store.repository_status()["dirty"] is False


def test_repository_status_disables_optional_git_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("# Agent\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    store.ensure_bootstrap()
    original_run = subprocess.run
    status_environments: list[dict[str, str]] = []

    def capture_run(*args: Any, **kwargs: Any):
        command = args[0] if args else kwargs.get("args", [])
        if "status" in command:
            status_environments.append(dict(kwargs.get("env") or {}))
        return original_run(*args, **kwargs)

    monkeypatch.setattr("app.runtime.agent_git_command_mixin.subprocess.run", capture_run)

    status = store.repository_status()

    assert status["status"] == "active"
    assert status_environments
    assert all(environment.get("GIT_OPTIONAL_LOCKS") == "0" for environment in status_environments)


def test_repository_status_does_not_refresh_index_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    tracked = repo / "CLAUDE.md"
    tracked.write_text("# Agent\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    store.ensure_bootstrap()
    index = repo / ".git" / "index"
    index_bytes = index.read_bytes()
    index_mtime_ns = index.stat().st_mtime_ns
    tracked_stat = tracked.stat()
    os.utime(tracked, ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 2_000_000_000))

    status = store.repository_status()

    assert status["status"] == "active"
    assert status["dirty"] is False
    assert index.read_bytes() == index_bytes
    assert index.stat().st_mtime_ns == index_mtime_ns


def test_git_store_snapshot_commits_deletion_when_no_worktree_files_remain(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    only_file = repo / "only.txt"
    only_file.write_bytes(b"tracked\n")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )

    store.ensure_bootstrap()
    only_file.unlink()
    snapshot = store.create_snapshot(reason="delete-last-file")

    assert snapshot["agent_version_id"]
    assert _git_bytes(repo, "ls-tree", "-r", "HEAD") == b""


def test_raw_git_storage_uses_verified_common_directory_without_git_path_output(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git_bytes(repo, "init", "-q")
    calls: list[list[str]] = []

    def fake_git(args: list[str], _repository: Path) -> str:
        calls.append(args)
        return ""

    configure_raw_git_storage(repo, run_git=fake_git)

    assert calls == [
        ["config", "core.autocrlf", "false"],
        ["config", "core.safecrlf", "false"],
        ["config", "core.fileMode", "true"],
    ]
    assert (repo / ".git" / "info" / "attributes").is_file()
    assert not (tmp_path / "outside").exists()


def _initialized_store(tmp_path: Path) -> tuple[Path, GitAgentVersionStore]:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git_bytes(repo, "init", "-q")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    return repo, store


def test_raw_git_storage_rejects_linked_info_directory_without_external_write(tmp_path: Path) -> None:
    repo, store = _initialized_store(tmp_path)
    info = repo / ".git" / "info"
    info.rename(repo / ".git" / "original-info")
    external = tmp_path / "external-info"
    external.mkdir()
    sentinel = external / "attributes"
    sentinel.write_text("preserve\n", encoding="utf-8")
    info.symlink_to(external, target_is_directory=True)

    with pytest.raises(AgentGitError, match="common metadata authority rejected"):
        store._configure_repo(repo)

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert info.is_symlink()


def test_git_metadata_writers_reject_linked_leaves_without_external_write(tmp_path: Path) -> None:
    repo, store = _initialized_store(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    attributes_sentinel = external / "attributes"
    attributes_sentinel.write_text("attributes-preserved\n", encoding="utf-8")
    attributes = repo / ".git" / "info" / "attributes"
    attributes.symlink_to(attributes_sentinel)

    with pytest.raises(AgentGitError, match="not a regular file"):
        store._configure_repo(repo)

    assert attributes_sentinel.read_text(encoding="utf-8") == "attributes-preserved\n"
    assert attributes.is_symlink()

    attributes.unlink()
    store._configure_repo(repo)
    exclude_sentinel = external / "exclude"
    exclude_sentinel.write_text("exclude-preserved\n", encoding="utf-8")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.symlink_to(exclude_sentinel)

    with pytest.raises(AgentGitError, match="not a regular file"):
        store._write_info_exclude(repo)

    assert exclude_sentinel.read_text(encoding="utf-8") == "exclude-preserved\n"
    assert exclude.is_symlink()


def test_linked_worktree_metadata_writers_share_verified_common_info_directory(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    head = str(store.ensure_bootstrap()["agent_version_id"])

    worktree = store.create_worktree("common-info", base_ref=head)

    linked_git_dir = Path(worktree.worktree_path.joinpath(".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    common_info = repo / ".git" / "info"
    assert common_info.joinpath("attributes").read_text(encoding="utf-8").startswith("# AgentGov raw workspace storage")
    assert "Agent runtime managed excludes" in common_info.joinpath("exclude").read_text(encoding="utf-8")
    assert not linked_git_dir.joinpath("info", "attributes").exists()
    assert not linked_git_dir.joinpath("info", "exclude").exists()


def test_raw_git_storage_rejects_leaf_identity_change_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, store = _initialized_store(tmp_path)
    store._configure_repo(repo)
    attributes = repo / ".git" / "info" / "attributes"
    attributes.write_text("outdated\n", encoding="utf-8")
    original_fsync = raw_storage.os.fsync
    displaced = attributes.with_name("attributes.displaced-before")
    raced = False

    def race_before_replace(fd: int) -> None:
        nonlocal raced
        original_fsync(fd)
        if not raced:
            raced = True
            attributes.rename(displaced)
            attributes.write_text("concurrent-owner\n", encoding="utf-8")

    monkeypatch.setattr(raw_storage.os, "fsync", race_before_replace)

    with pytest.raises(RawGitStorageError, match="lost its authority"):
        configure_raw_git_storage(
            repo,
            run_git=lambda args, repository: store._git(args, cwd=repository),
        )

    assert attributes.read_text(encoding="utf-8") == "concurrent-owner\n"
    assert not list(attributes.parent.glob(".attributes.*.tmp"))


def test_raw_git_storage_rejects_parent_identity_change_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, store = _initialized_store(tmp_path)
    store._configure_repo(repo)
    info = repo / ".git" / "info"
    info.joinpath("attributes").write_text("outdated\n", encoding="utf-8")
    displaced = repo / ".git" / "info.displaced"
    external = tmp_path / "external-info"
    external.mkdir()
    sentinel = external / "attributes"
    sentinel.write_text("preserve\n", encoding="utf-8")
    original_fsync = raw_storage.os.fsync
    raced = False

    def race_parent(fd: int) -> None:
        nonlocal raced
        original_fsync(fd)
        if not raced:
            raced = True
            info.rename(displaced)
            info.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(raw_storage.os, "fsync", race_parent)

    with pytest.raises(RawGitStorageError, match="lost its authority"):
        configure_raw_git_storage(
            repo,
            run_git=lambda args, repository: store._git(args, cwd=repository),
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert info.is_symlink()
    assert not list(displaced.glob(".attributes.*.tmp"))


def test_raw_git_storage_detects_leaf_identity_change_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, store = _initialized_store(tmp_path)
    store._configure_repo(repo)
    attributes = repo / ".git" / "info" / "attributes"
    attributes.write_text("outdated\n", encoding="utf-8")
    original_replace = raw_storage.os.replace
    displaced = attributes.with_name("attributes.displaced-after")

    def race_after_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        attributes.rename(displaced)
        attributes.write_text("concurrent-owner\n", encoding="utf-8")

    monkeypatch.setattr(raw_storage.os, "replace", race_after_replace)

    with pytest.raises(RawGitStorageError, match="lost its authority"):
        configure_raw_git_storage(
            repo,
            run_git=lambda args, repository: store._git(args, cwd=repository),
        )

    assert attributes.read_text(encoding="utf-8") == "concurrent-owner\n"
    assert not list(attributes.parent.glob(".attributes.*.tmp"))


def test_git_store_resets_and_removes_abandoned_worktree(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("agc-cleanup-test", base_ref=base)
    worktree.worktree_path.joinpath("CLAUDE.md").write_text("interrupted\n", encoding="utf-8")

    store.reset_worktree(worktree.worktree_path, base_ref=base)
    assert worktree.worktree_path.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "base\n"
    assert store.worktree_commit_sha(worktree.worktree_path) == base

    store.remove_worktree("agc-cleanup-test")
    assert not worktree.worktree_path.exists()
    assert not store._git(["show-ref", "--verify", "refs/heads/change-set/agc-cleanup-test"], cwd=repo, check=False).strip()


def _authority_worktree(tmp_path: Path, change_set_id: str = "agc-authority"):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    head = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree(change_set_id, base_ref=head)
    return store, worktree, head


def test_existing_worktree_authority_requires_registered_branch_and_head(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)

    authority = store._require_existing_worktree_authority(
        worktree.change_set_id,
        worktree.worktree_path,
        expected_head=head,
    )

    assert authority == worktree


def test_commit_worktree_rejects_foreign_agent_worktree_without_side_effects(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    foreign_root = tmp_path / "foreign"
    owner_root.mkdir()
    foreign_root.mkdir()
    owner_store, owner_worktree, owner_head = _authority_worktree(owner_root, "owner-change")
    foreign_store, foreign_worktree, foreign_head = _authority_worktree(foreign_root, "foreign-change")
    foreign_file = foreign_worktree.worktree_path / "CLAUDE.md"
    foreign_file.write_text("foreign pending change\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="escapes the governed worktree root"):
        owner_store.commit_worktree(foreign_worktree.worktree_path, message="cross-agent commit")

    assert owner_store.worktree_commit_sha(owner_worktree.worktree_path) == owner_head
    assert foreign_store.worktree_commit_sha(foreign_worktree.worktree_path) == foreign_head
    assert foreign_file.read_text(encoding="utf-8") == "foreign pending change\n"


def test_worktree_writers_reject_same_store_symlink_alias_without_side_effects(tmp_path: Path) -> None:
    store, first, head = _authority_worktree(tmp_path, "first-change")
    second = store.create_worktree("second-change", base_ref=head)
    store.remove_worktree(first.change_set_id)
    first.worktree_path.symlink_to(second.worktree_path, target_is_directory=True)
    second_file = second.worktree_path / "CLAUDE.md"
    second_file.write_text("second pending change\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="escapes the governed worktree root"):
        store.commit_worktree(first.worktree_path, message="aliased commit")
    with pytest.raises(AgentGitError, match="escapes the governed worktree root"):
        store.reset_worktree(first.worktree_path, base_ref=head)
    with pytest.raises(AgentGitError, match="escapes the governed worktree root"):
        store.commit_squashed_worktree(first.worktree_path, base_ref=head, message="aliased squash")
    with pytest.raises(AgentGitError, match="escapes the governed worktree root"):
        store.remove_worktree(first.change_set_id, delete_branch=False)

    assert first.worktree_path.is_symlink()
    assert store.worktree_commit_sha(second.worktree_path) == head
    assert second_file.read_text(encoding="utf-8") == "second pending change\n"


def test_create_worktree_rejects_path_escape_without_deleting_existing_data(tmp_path: Path) -> None:
    store, _, head = _authority_worktree(tmp_path, "existing-change")
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "marker.txt"
    marker.write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="Invalid change set id"):
        store.create_worktree("../../victim", base_ref=head)

    assert marker.read_text(encoding="utf-8") == "preserve me\n"
    residue = store.worktrees_dir / "residue-change"
    residue.mkdir()
    residue_marker = residue / "marker.txt"
    residue_marker.write_text("preserve residue\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="already exists without linked Git authority"):
        store.create_worktree("residue-change", base_ref=head)

    assert residue_marker.read_text(encoding="utf-8") == "preserve residue\n"


def test_nested_mutation_guard_exit_keeps_outer_guard_exclusive(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    competitor_entered = threading.Event()

    def compete() -> None:
        with store.mutation_guard():
            competitor_entered.set()

    with store.mutation_guard():
        authority = store._require_existing_worktree_authority(
            worktree.change_set_id,
            worktree.worktree_path,
            expected_head=head,
        )
        contender = threading.Thread(target=compete)
        contender.start()
        assert not competitor_entered.wait(0.1)
        assert authority == worktree
    contender.join(2)

    assert not contender.is_alive()
    assert competitor_entered.is_set()


def test_existing_worktree_authority_rejects_standalone_git_directory(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    metadata = worktree.worktree_path / ".git"
    metadata.unlink()
    shutil.copytree(store.repository_dir / ".git", metadata)
    metadata.joinpath("HEAD").write_text(f"ref: refs/heads/{worktree.branch_name}\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="worktree authority"):
        store._require_existing_worktree_authority(worktree.change_set_id, worktree.worktree_path, expected_head=head)
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.create_worktree(worktree.change_set_id, base_ref=head)
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.worktree_commit_sha(worktree.worktree_path)
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.reset_worktree(worktree.worktree_path, base_ref=head)
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.commit_worktree(worktree.worktree_path, message="standalone commit")
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.commit_squashed_worktree(worktree.worktree_path, base_ref=head, message="standalone squash")
    with pytest.raises(AgentGitError, match="worktree authority"):
        store.remove_worktree(worktree.change_set_id)

    assert not worktree.worktree_path.joinpath("tests").exists()


def test_existing_worktree_authority_rejects_wrong_linked_branch(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    _git_bytes(worktree.worktree_path, "branch", "wrong-authority", head)
    _git_bytes(worktree.worktree_path, "symbolic-ref", "HEAD", "refs/heads/wrong-authority")

    with pytest.raises(AgentGitError, match="worktree authority"):
        store._require_existing_worktree_authority(worktree.change_set_id, worktree.worktree_path, expected_head=head)

    assert not worktree.worktree_path.joinpath("tests").exists()


def test_existing_worktree_authority_rejects_gitdir_pointer_swap(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    other = store.create_worktree("agc-other-authority", base_ref=head)
    worktree.worktree_path.joinpath(".git").write_bytes(other.worktree_path.joinpath(".git").read_bytes())

    with pytest.raises(AgentGitError, match="worktree authority"):
        store._require_existing_worktree_authority(worktree.change_set_id, worktree.worktree_path, expected_head=head)

    assert not worktree.worktree_path.joinpath("tests").exists()


def test_existing_worktree_authority_rejects_symlinked_gitdir_target(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    metadata = worktree.worktree_path / ".git"
    real_git_dir = Path(metadata.read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    alias = real_git_dir.parent / "authority-alias"
    alias.symlink_to(real_git_dir, target_is_directory=True)
    metadata.write_text(f"gitdir: {alias}\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="worktree authority"):
        store._require_existing_worktree_authority(worktree.change_set_id, worktree.worktree_path, expected_head=head)


def test_existing_worktree_authority_rejects_symlinked_common_dir_target(tmp_path: Path) -> None:
    store, worktree, head = _authority_worktree(tmp_path)
    metadata = worktree.worktree_path / ".git"
    linked_git_dir = Path(metadata.read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    common_alias = store.repository_dir / ".git-common-alias"
    common_alias.symlink_to(store.repository_dir / ".git", target_is_directory=True)
    linked_git_dir.joinpath("commondir").write_text(str(common_alias), encoding="utf-8")

    with pytest.raises(AgentGitError, match="worktree authority"):
        store._require_existing_worktree_authority(worktree.change_set_id, worktree.worktree_path, expected_head=head)


def test_git_store_squashes_configuration_and_tests_into_one_commit_over_base(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("agc-squash-test", base_ref=base)
    worktree.worktree_path.joinpath("CLAUDE.md").write_text("optimized\n", encoding="utf-8")
    intermediate = store.commit_worktree(worktree.worktree_path, message="configuration candidate")
    tests_dir = worktree.worktree_path / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("test_feedback.py").write_text("def test_feedback():\n    assert 2 == 2\n", encoding="utf-8")

    candidate = store.commit_squashed_worktree(
        worktree.worktree_path,
        base_ref=base,
        message="configuration and regression tests",
    )

    assert candidate != intermediate
    assert store._git(["rev-parse", f"{candidate}^"], cwd=repo).strip() == base
    assert store._git(["rev-list", "--count", f"{base}..{candidate}"], cwd=repo).strip() == "1"
    assert _git_bytes(repo, "show", f"{candidate}:CLAUDE.md") == b"optimized\n"
    assert _git_bytes(repo, "show", f"{candidate}:tests/test_feedback.py") == b"def test_feedback():\n    assert 2 == 2\n"


def test_existing_tag_does_not_bypass_clean_workspace_or_fast_forward(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("agc-existing-tag", base_ref=base)
    worktree.worktree_path.joinpath("CLAUDE.md").write_text("candidate\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree.worktree_path, message="candidate")
    tag_name = "agent-release-existing"
    store._git(["tag", "-a", tag_name, "-m", "external tag", candidate], cwd=repo)
    repo.joinpath("CLAUDE.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(AgentGitError, match="uncommitted changes"):
        store.publish_commit(candidate, tag_name=tag_name, message="publish")

    assert store.current_commit_sha() == base
    store._git(["restore", "--", "CLAUDE.md"], cwd=repo)
    result = store.publish_commit(candidate, tag_name=tag_name, message="publish")
    assert result["published_commit_sha"] == candidate
    assert store.current_commit_sha() == candidate


def test_archive_names_do_not_collide_for_slash_and_dash_tags(tmp_path):
    repo = tmp_path / "workspace"
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    commit_sha = str(store.ensure_bootstrap()["agent_version_id"])
    store._git(["tag", "-a", "release/a", "-m", "slash", commit_sha], cwd=repo)
    store._git(["tag", "-a", "release-a", "-m", "dash", commit_sha], cwd=repo)

    slash = store.archive_ref("release/a")
    dash = store.archive_ref("release-a")

    assert slash["archive_path"] != dash["archive_path"]
    assert Path(str(slash["archive_path"])).is_file()
    assert Path(str(dash["archive_path"])).is_file()


def test_direct_repository_writers_recheck_precondition_before_side_effect(tmp_path: Path) -> None:
    repo = tmp_path / "agent" / "workspace"
    repo.mkdir(parents=True)
    repo.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    mutable = True
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=repo.parent / "version" / "worktrees",
        releases_dir=repo.parent / "version" / "releases",
        process_lock_path=tmp_path / "locks" / "agent.lock",
        mutation_precondition=lambda: mutable,
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("direct-writer", base_ref=base)
    worktree.worktree_path.joinpath("CLAUDE.md").write_text("interrupted\n", encoding="utf-8")
    branch_ref = "refs/heads/change-set/direct-writer"
    branch_before = store._git(["rev-parse", branch_ref], cwd=repo).strip()
    mutable = False

    with pytest.raises(AgentGitError, match="no longer mutable"):
        store.reset_worktree(worktree.worktree_path, base_ref=base)
    with pytest.raises(AgentGitError, match="no longer mutable"):
        store.remove_worktree("direct-writer")
    with pytest.raises(AgentGitError, match="no longer mutable"):
        store.archive_ref("HEAD")

    assert worktree.worktree_path.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "interrupted\n"
    assert store._git(["rev-parse", branch_ref], cwd=repo).strip() == branch_before
    assert list(store.releases_dir.iterdir()) == []


def test_read_only_repository_status_does_not_create_missing_authority(tmp_path: Path) -> None:
    agent_root = tmp_path / "data" / "business-agents" / "missing"
    lock_root = tmp_path / "data" / ".agent-repository-locks"
    store = GitAgentVersionStore(
        repository_dir=agent_root / "workspace",
        worktrees_dir=agent_root / "version" / "worktrees",
        releases_dir=agent_root / "version" / "releases",
        process_lock_path=lock_root / "missing.lock",
        mutation_precondition=lambda: False,
    )

    status = store.repository_status()

    assert status["status"] == "degraded"
    assert not agent_root.exists()
    assert not lock_root.exists()
