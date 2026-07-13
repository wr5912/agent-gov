from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore


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
        return ""

    monkeypatch.setattr(store, "_git", fake_git)

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
