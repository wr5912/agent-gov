import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True).stdout


def test_git_store_marks_repository_as_safe_before_local_config(tmp_path, monkeypatch):
    repo = tmp_path / "workspace"
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    calls: list[tuple[list[str], object, bool]] = []

    def fake_git(args: list[str], *, cwd, check: bool = True) -> str:
        calls.append((args, cwd, check))
        if args == ["rev-parse", "--git-path", "info/attributes"]:
            return str(repo / ".git" / "info" / "attributes")
        if args == ["rev-parse", "--git-common-dir"]:
            return str(repo / ".git")
        return ""

    monkeypatch.setattr(store, "_git", fake_git)
    monkeypatch.setattr(
        "app.runtime.agent_git_store.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: detected dubious ownership; add safe.directory",
        ),
    )

    store._configure_repo(repo)

    assert calls[0] == (
        ["config", "--global", "--get-all", "safe.directory"],
        repo,
        False,
    )
    assert calls[1] == (
        ["config", "--global", "--add", "safe.directory", str(repo.resolve())],
        repo,
        False,
    )
    assert calls[2][0] == ["config", "user.name", "AgentGov"]


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


@pytest.mark.parametrize("attributes_path", ["", "outside"])
def test_raw_git_storage_rejects_empty_or_out_of_git_metadata_path(tmp_path, attributes_path):
    repo = tmp_path / "workspace"
    git_dir = repo / ".git"
    resolved_attributes = "" if not attributes_path else str(tmp_path / attributes_path / "attributes")

    def fake_git(args: list[str], _repository: Path) -> str:
        if args[:2] == ["config", "core.autocrlf"] or args[:2] == ["config", "core.safecrlf"] or args[:2] == ["config", "core.fileMode"]:
            return ""
        if args == ["rev-parse", "--git-path", "info/attributes"]:
            return resolved_attributes
        if args == ["rev-parse", "--git-common-dir"]:
            return str(git_dir)
        raise AssertionError(args)

    with pytest.raises(RawGitStorageError) as exc_info:
        configure_raw_git_storage(repo, run_git=fake_git)

    assert str(tmp_path) not in str(exc_info.value)


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
