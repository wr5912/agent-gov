from app.runtime.agent_git_store import GitAgentVersionStore


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
    assert calls[2][0] == ["config", "user.name", "Claude Agent Runtime"]
