from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from app.services import agent_workspace_fingerprint as fingerprint
from app.services import agent_workspace_git_evidence as evidence
from app.services.agent_workspace_activation_refs import GitCommandError


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "workspace"
    repository.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "AgentGov Test"),
        ("config", "user.email", "agentgov@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=repository, check=True)
    repository.joinpath("CLAUDE.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    return repository


def test_canonical_index_cleanup_failure_is_stable_and_swept_on_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    original_rmtree = evidence.shutil.rmtree
    attempts = 0

    def fail_once(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected cleanup failure")
        original_rmtree(path)

    monkeypatch.setattr(evidence.shutil, "rmtree", fail_once)
    with pytest.raises(GitCommandError, match="could not be removed"):
        evidence.canonical_index_fingerprint(repository, "HEAD")
    assert len(tuple(repository.joinpath(".git").glob("agentgov-terminal-index-*"))) == 1

    assert evidence.canonical_index_fingerprint(repository, "HEAD") == evidence.index_fingerprint(repository)
    assert not tuple(repository.joinpath(".git").glob("agentgov-terminal-index-*"))


def test_workspace_fingerprint_preserves_bytes_modes_and_does_not_follow_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "script"
    script.write_bytes(b"abc")
    script.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("ignored").write_bytes(b"outside bytes must not be read")
    workspace.joinpath("plain").write_bytes(b"\0\xff")
    link_target = "../outside"
    workspace.joinpath("linked-dir").symlink_to(link_target, target_is_directory=True)
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_ENTRIES", 3)
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_FILE_BYTES", len(link_target))
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_TOTAL_BYTES", len(link_target) + 5)

    expected = hashlib.sha256()
    expected.update(b"120000 linked-dir\0")
    expected.update(b"10\0../outside\0")
    expected.update(b"100644 plain\0")
    expected.update(b"2\0\0\xff\0")
    expected.update(b"100755 script\0")
    expected.update(b"3\0abc\0")
    original = fingerprint.workspace_fingerprint(workspace)

    outside.joinpath("ignored").write_bytes(b"changed outside bytes")
    assert original == expected.hexdigest()
    assert fingerprint.workspace_fingerprint(workspace) == original


def test_workspace_fingerprint_rejects_single_file_over_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("large").write_bytes(b"1234")
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_FILE_BYTES", 3)

    with pytest.raises(GitCommandError, match="single-file byte limit"):
        fingerprint.workspace_fingerprint(tmp_path)


def test_workspace_fingerprint_rejects_total_bytes_over_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("first").write_bytes(b"12")
    tmp_path.joinpath("second").write_bytes(b"34")
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_TOTAL_BYTES", 3)

    with pytest.raises(GitCommandError, match="total byte limit"):
        fingerprint.workspace_fingerprint(tmp_path)


def test_workspace_fingerprint_counts_empty_directories_toward_entry_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("first").mkdir()
    tmp_path.joinpath("second").mkdir()
    monkeypatch.setattr(fingerprint, "MAX_WORKSPACE_FINGERPRINT_ENTRIES", 1)

    with pytest.raises(GitCommandError, match="entry count limit"):
        fingerprint.workspace_fingerprint(tmp_path)


def test_workspace_fingerprint_reads_regular_files_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("content").write_bytes(b"12345678")
    original_read = os.read
    requested_sizes: list[int] = []

    def tracked_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(fingerprint, "WORKSPACE_FINGERPRINT_CHUNK_BYTES", 3)
    monkeypatch.setattr(fingerprint.os, "read", tracked_read)

    fingerprint.workspace_fingerprint(tmp_path)

    assert requested_sizes == [3, 3, 2, 1]


def test_workspace_fingerprint_rejects_replaced_directory_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    ancestor = workspace / "ancestor"
    ancestor.mkdir(parents=True)
    ancestor.joinpath("content").write_bytes(b"original")
    moved = tmp_path / "moved-ancestor"
    original_open = os.open
    replaced = False

    def replace_before_open(
        path: os.PathLike[str] | str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "ancestor" and dir_fd is not None and flags & os.O_DIRECTORY and not replaced:
            replaced = True
            ancestor.rename(moved)
            ancestor.mkdir()
            ancestor.joinpath("content").write_bytes(b"replacement")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(fingerprint.os, "open", replace_before_open)

    with pytest.raises(GitCommandError, match="directory was replaced"):
        fingerprint.workspace_fingerprint(workspace)


def test_workspace_fingerprint_fails_closed_when_directory_scan_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("content").write_bytes(b"content")

    def fail_scandir(_directory_fd: int) -> object:
        raise PermissionError("injected scan failure")

    monkeypatch.setattr(fingerprint.os, "scandir", fail_scandir)

    with pytest.raises(GitCommandError, match="could not be fingerprinted safely"):
        fingerprint.workspace_fingerprint(tmp_path)


def test_workspace_fingerprint_rejects_file_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = workspace / "content"
    content.write_bytes(b"original")
    moved = tmp_path / "moved-content"
    original_open = os.open
    replaced = False

    def replace_before_open(
        path: os.PathLike[str] | str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "content" and dir_fd is not None and not replaced:
            replaced = True
            content.rename(moved)
            content.write_bytes(b"replacement")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(fingerprint.os, "open", replace_before_open)

    with pytest.raises(GitCommandError, match="file was replaced"):
        fingerprint.workspace_fingerprint(workspace)


def test_workspace_fingerprint_fails_closed_when_file_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.joinpath("content").write_bytes(b"content")

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise OSError("injected read failure")

    monkeypatch.setattr(fingerprint.os, "read", fail_read)

    with pytest.raises(GitCommandError, match="could not be fingerprinted safely"):
        fingerprint.workspace_fingerprint(tmp_path)
