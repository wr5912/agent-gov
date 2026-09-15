import stat
import subprocess
from pathlib import Path

import pytest
from app.runtime.agent_git_raw_storage import RawGitStorageError, configure_raw_git_storage
from app.runtime.agent_git_read_helpers import parse_name_status_z
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True).stdout


def test_git_store_configures_real_repository_without_overwriting_global_safe_directories(
    process_environment,
    tmp_path,
):
    global_config = tmp_path / "global.gitconfig"
    process_environment.set("GIT_CONFIG_GLOBAL", str(global_config))
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "/operator-owned/repository"],
        check=True,
    )
    repo = tmp_path / "workspace"
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    store.ensure_bootstrap()

    safe_directories = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert safe_directories == ["/operator-owned/repository"]
    assert store._git(["config", "user.name"], cwd=repo).strip() == "AgentGov"
    assert store._git(["config", "user.email"], cwd=repo).strip() == "agent-runtime@example.local"
    assert store._git(["config", "core.autocrlf"], cwd=repo).strip() == "false"
    assert store._git(["config", "core.safecrlf"], cwd=repo).strip() == "false"
    assert store._git(["config", "core.fileMode"], cwd=repo).strip() == "true"


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
    worktree = store.create_worktree("diff-test", base_ref=str(first["agent_version_id"]))
    worktree.worktree_path.joinpath("CLAUDE.md").write_text("one\ntwo\n", encoding="utf-8")
    second = store.commit_worktree(worktree.worktree_path, message="diff-test")

    diff = store.diff_version_file(
        str(first["agent_version_id"]),
        second,
        "CLAUDE.md",
    )

    assert diff is not None
    assert diff["status"] == "modified"
    assert diff["is_text"] is True
    assert "+two" in str(diff["unified_diff"])


def test_git_store_mode_only_diff_is_modified_and_exposes_exact_tree_modes(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    tool = repo / "hooks" / "tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o644)
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("mode-only", base_ref=base)
    worktree.worktree_path.joinpath("hooks", "tool").chmod(0o755)
    candidate = store.commit_worktree(worktree.worktree_path, message="mode only")

    summary = store.diff_versions(base, candidate)
    detail = store.diff_version_file(base, candidate, "hooks/tool")

    assert summary is not None and detail is not None
    assert [entry["path"] for entry in summary["modified"]] == ["hooks/tool"]
    summary_entry = summary["modified"][0]
    assert summary_entry["before"]["mode"] == "100644"
    assert summary_entry["after"]["mode"] == "100755"
    assert summary_entry["before"]["sha256"] == summary_entry["after"]["sha256"]
    assert detail["status"] == "modified"
    assert detail["before"] == summary_entry["before"]
    assert detail["after"] == summary_entry["after"]
    assert detail["is_text"] is True and detail["truncated"] is False
    assert "(git mode)" in str(detail["unified_diff"])
    assert "-100644" in str(detail["unified_diff"])
    assert "+100755" in str(detail["unified_diff"])


def test_git_store_combined_content_and_mode_diff_displays_both_changes(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    tool = repo / "tool.sh"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o644)
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("content-and-mode", base_ref=base)
    candidate_tool = worktree.worktree_path / "tool.sh"
    candidate_tool.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    candidate_tool.chmod(0o755)
    candidate = store.commit_worktree(worktree.worktree_path, message="content and mode")

    detail = store.diff_version_file(base, candidate, "tool.sh")

    assert detail is not None
    assert detail["status"] == "modified"
    assert detail["before"]["mode"] == "100644"
    assert detail["after"]["mode"] == "100755"
    assert "-100644" in str(detail["unified_diff"])
    assert "+100755" in str(detail["unified_diff"])
    assert "+echo changed" in str(detail["unified_diff"])


def test_git_store_empty_file_add_and_delete_have_reviewable_mode_diff(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("deleted-empty.txt").write_bytes(b"")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("empty-files", base_ref=base)
    worktree.worktree_path.joinpath("deleted-empty.txt").unlink()
    worktree.worktree_path.joinpath("added-empty.txt").write_bytes(b"")
    candidate = store.commit_worktree(worktree.worktree_path, message="empty files")

    summary = store.diff_versions(base, candidate)
    added = store.diff_version_file(base, candidate, "added-empty.txt")
    deleted = store.diff_version_file(base, candidate, "deleted-empty.txt")

    assert summary is not None and added is not None and deleted is not None
    assert summary["added"][0]["mode"] == "100644"
    assert summary["deleted"][0]["mode"] == "100644"
    assert added["status"] == "added" and added["is_text"] is True
    assert added["before"] is None and added["after"] == summary["added"][0]
    assert "+100644" in str(added["unified_diff"])
    assert deleted["status"] == "deleted" and deleted["is_text"] is True
    assert deleted["before"] == summary["deleted"][0] and deleted["after"] is None
    assert "-100644" in str(deleted["unified_diff"])


def test_version_diff_preserves_utf8_and_pathological_git_paths_without_c_quoting(tmp_path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    repo.joinpath("AGENT.md").write_text("# base\n", encoding="utf-8")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("pathological-paths", base_ref=base)
    paths = {
        "skills/审计/SKILL.md",
        "skills/tab\tname/SKILL.md",
        "skills/line\nname/SKILL.md",
        'skills/"quoted"/SKILL.md',
    }
    for relative in paths:
        target = worktree.worktree_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# governed skill\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree.worktree_path, message="pathological paths")

    diff = store.diff_versions(base, candidate)

    assert diff is not None
    assert {str(entry["path"]) for entry in diff["added"]} == paths


@pytest.mark.parametrize(
    "raw",
    (
        b"A\x00unterminated",
        b"R100\x00old\x00new\x00",
        b"A\x00invalid-\xff\x00",
        b"A\x00../escape\x00",
        b"A\x00duplicate\x00M\x00duplicate\x00",
    ),
)
def test_name_status_z_parser_fails_closed_on_ambiguous_or_unsafe_records(raw: bytes) -> None:
    with pytest.raises(AgentGitError):
        parse_name_status_z(raw)


def test_git_store_candidate_commit_preserves_raw_bytes_and_exec_bit_despite_repository_attributes(tmp_path):
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

    base = str(store.ensure_bootstrap()["agent_version_id"])
    assert _git_bytes(repo, "show", "HEAD:.env") == b"WORKSPACE_OWNED=true\n"
    assert _git_bytes(repo, "show", "HEAD:crlf.txt") == b"first\r\nsecond\r\n"
    worktree = store.create_worktree("raw-mode", base_ref=base)
    subprocess.run(["git", "config", "core.fileMode", "false"], cwd=worktree.worktree_path, check=True)
    candidate_tool = worktree.worktree_path / "hooks" / "tool"
    candidate_tool.chmod(0o755)
    worktree.worktree_path.joinpath("ignored.secret").write_bytes(b"workspace-owned\n")
    candidate = store.commit_worktree(worktree.worktree_path, message="raw-mode")

    assert _git_bytes(repo, "show", f"{candidate}:crlf.txt") == b"first\r\nsecond\r\n"
    assert _git_bytes(repo, "show", f"{candidate}:ignored.secret") == b"workspace-owned\n"
    assert _git_bytes(repo, "ls-tree", candidate, "hooks/tool").split(maxsplit=1)[0] == b"100755"
    assert stat.S_IMODE(candidate_tool.stat().st_mode) & 0o111
    assert store.current_commit_sha() == base


def test_git_store_status_reports_ignored_live_workspace_drift_without_mutating_it(tmp_path):
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
        }
    ]
    status = store.repository_status()
    assert status["dirty"] is True
    assert status["changed_file_count"] == 1
    assert status["changed_files"] == changes
    assert status["file_diffs"][0]["status"] == "untracked"
    assert "+workspace-owned" in str(status["file_diffs"][0]["unified_diff"])

    assert ignored_file.read_bytes() == b"workspace-owned\n"
    assert _git_bytes(repo, "ls-tree", "HEAD", "ignored.secret") == b""


def test_git_store_candidate_commit_records_deletion_when_no_worktree_files_remain(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    only_file = repo / "only.txt"
    only_file.write_bytes(b"tracked\n")
    store = GitAgentVersionStore(
        repository_dir=repo,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )

    base = str(store.ensure_bootstrap()["agent_version_id"])
    worktree = store.create_worktree("delete-last-file", base_ref=base)
    worktree.worktree_path.joinpath("only.txt").unlink()
    candidate = store.commit_worktree(worktree.worktree_path, message="delete-last-file")

    assert candidate
    assert _git_bytes(repo, "ls-tree", "-r", candidate) == b""
    assert store.current_commit_sha() == base


@pytest.mark.parametrize("unsafe_kind", ["symlink_escape", "directory"])
def test_raw_git_storage_rejects_unsafe_real_git_attributes_path(tmp_path, unsafe_kind):
    repo = tmp_path / "workspace"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    attributes = repo / ".git" / "info" / "attributes"
    if unsafe_kind == "symlink_escape":
        outside = tmp_path / "outside" / "attributes"
        outside.parent.mkdir()
        outside.write_text("operator-owned\n", encoding="utf-8")
        attributes.symlink_to(outside)
    else:
        attributes.mkdir()

    def run_real_git(args: list[str], repository: Path) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    with pytest.raises(RawGitStorageError) as exc_info:
        configure_raw_git_storage(repo, run_git=run_real_git)

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
