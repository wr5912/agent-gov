from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from app.agent_testing import source_snapshot as snapshot_module
from app.agent_testing.materializer import GIT_BINARY, MAX_SOURCE_FILE_BYTES, materialize_git_commit
from app.agent_testing.source_snapshot import SourceSnapshotError, snapshot_source_tree


def _expected_digest(files: list[tuple[str, int, bytes]]) -> str:
    digest = hashlib.sha256(b"agentgov-source-v1\0")
    for path, mode, content in sorted(files):
        for value in (f"{mode:o}".encode("ascii"), path.encode("utf-8"), str(len(content)).encode("ascii"), content):
            digest.update(value)
            digest.update(b"\0")
    return digest.hexdigest()


def test_snapshot_matches_materializer_digest_before_and_after_read_only_lockdown(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    plain = root / "CLAUDE.md"
    executable = nested / "check.py"
    plain_content = b"# test Agent\n"
    executable_content = b"#!/usr/bin/env python\n"
    plain.write_bytes(plain_content)
    executable.write_bytes(executable_content)
    plain.chmod(0o644)
    executable.chmod(0o755)
    expected = _expected_digest(
        [
            ("CLAUDE.md", 0o644, plain_content),
            ("nested/check.py", 0o755, executable_content),
        ]
    )

    writable = snapshot_source_tree(root)
    plain.chmod(0o444)
    executable.chmod(0o555)
    nested.chmod(0o555)
    root.chmod(0o555)
    readonly = snapshot_source_tree(root)

    assert writable == readonly
    assert readonly.source_digest == expected
    assert readonly.file_count == 2
    assert readonly.total_bytes == len(plain_content) + len(executable_content)


def test_snapshot_matches_real_exact_commit_materialization(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run([str(GIT_BINARY), "init", "-q", str(repository)], check=True)
    repository.joinpath("CLAUDE.md").write_text("# exact commit\n", encoding="utf-8")
    script = repository / "check.py"
    script.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    script.chmod(0o755)
    repository.joinpath(".env").write_text("EXCLUDED=sensitive\n", encoding="utf-8")
    subprocess.run([str(GIT_BINARY), "-C", str(repository), "add", "--all"], check=True)
    subprocess.run(
        [
            str(GIT_BINARY),
            "-C",
            str(repository),
            "-c",
            "user.name=AgentGov Test",
            "-c",
            "user.email=agentgov@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    commit_sha = subprocess.check_output([str(GIT_BINARY), "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    workspace = tmp_path / "materialized"

    fingerprint = materialize_git_commit(repository, commit_sha, workspace)
    snapshot = snapshot_source_tree(workspace)

    assert snapshot.source_digest == fingerprint.source_digest
    assert snapshot.file_count == fingerprint.file_count
    assert snapshot.total_bytes == fingerprint.total_bytes
    assert not workspace.joinpath(".env").exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "hardlink"])
def test_snapshot_rejects_link_and_special_file_aliases(tmp_path: Path, unsafe_kind: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("safe", encoding="utf-8")
    source.chmod(0o644)
    candidate = root / "unsafe"
    if unsafe_kind == "symlink":
        candidate.symlink_to(source)
    elif unsafe_kind == "fifo":
        os.mkfifo(candidate)
    else:
        os.link(source, candidate)

    with pytest.raises(SourceSnapshotError) as exc_info:
        snapshot_source_tree(root)

    assert exc_info.value.code in {
        "AGENT_SOURCE_SNAPSHOT_SYMLINK_FORBIDDEN",
        "AGENT_SOURCE_SNAPSHOT_SPECIAL_FILE_FORBIDDEN",
    }


def test_snapshot_rejects_unsafe_path_empty_directory_and_oversized_file(tmp_path: Path) -> None:
    unsafe_path_root = tmp_path / "unsafe-path"
    unsafe_path_root.mkdir()
    unsafe_path_root.joinpath("bad\nname").write_text("bad", encoding="utf-8")
    with pytest.raises(SourceSnapshotError) as unsafe_path:
        snapshot_source_tree(unsafe_path_root)
    assert unsafe_path.value.code == "AGENT_SOURCE_SNAPSHOT_PATH_INVALID"

    empty_directory_root = tmp_path / "empty-directory"
    empty_directory_root.joinpath("empty").mkdir(parents=True)
    with pytest.raises(SourceSnapshotError) as empty_directory:
        snapshot_source_tree(empty_directory_root)
    assert empty_directory.value.code == "AGENT_SOURCE_SNAPSHOT_TREE_INVALID"

    oversized_root = tmp_path / "oversized"
    oversized_root.mkdir()
    oversized = oversized_root / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_SOURCE_FILE_BYTES + 1)
    oversized.chmod(0o644)
    with pytest.raises(SourceSnapshotError) as oversized_file:
        snapshot_source_tree(oversized_root)
    assert oversized_file.value.code == "AGENT_SOURCE_SNAPSHOT_TOO_LARGE"


def test_snapshot_detects_path_replacement_during_file_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    source.chmod(0o644)
    original_read = snapshot_module.os.read
    replaced = False

    def replacing_read(file_descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(file_descriptor, size)
        if chunk and not replaced:
            replaced = True
            source.unlink()
            source.write_bytes(b"b" * (2 * 1024 * 1024))
        return chunk

    monkeypatch.setattr(snapshot_module.os, "read", replacing_read)

    with pytest.raises(SourceSnapshotError) as exc_info:
        snapshot_source_tree(root)

    assert replaced is True
    assert exc_info.value.code == "AGENT_SOURCE_SNAPSHOT_CHANGED"
