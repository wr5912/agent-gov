"""Pure/read-only helpers shared by the Git-backed Agent version store."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from app.runtime.agent_git_errors import AgentGitError
from app.runtime.json_types import JsonObject
from app.runtime.workspace_commit_path import WorkspaceCommitPathError, validate_workspace_commit_path


def run_git_read_only(args: list[str], *, cwd: Path) -> str:
    """Run a Git query without optional locks or index refreshes."""

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AgentGitError(detail or f"git {' '.join(args)} failed with {proc.returncode}")
    return proc.stdout


def run_git_read_only_bytes(args: list[str], *, cwd: Path) -> bytes:
    """运行不会刷新 index 的 Git 查询，并逐字节保留路径输出。"""

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise AgentGitError(detail or f"git {' '.join(args)} failed with {proc.returncode}")
    return proc.stdout


def parse_name_status_z(raw: bytes) -> tuple[tuple[str, str], ...]:
    """解析 `git diff --name-status -z --no-renames`，异常输入一律 fail closed。"""

    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        raise AgentGitError("Git name-status output is not NUL terminated")
    records = raw[:-1].split(b"\x00")
    if len(records) % 2:
        raise AgentGitError("Git name-status output contains an incomplete record")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index in range(0, len(records), 2):
        raw_status, raw_path = records[index : index + 2]
        if raw_status not in {b"A", b"M", b"D"}:
            raise AgentGitError("Git name-status output contains an unsupported record")
        try:
            path = validate_workspace_commit_path(raw_path).as_posix()
        except WorkspaceCommitPathError as exc:
            raise AgentGitError(str(exc)) from exc
        if path in seen:
            raise AgentGitError("Git name-status output contains a duplicate path")
        seen.add(path)
        entries.append((raw_status.decode("ascii"), path))
    return tuple(entries)


def safe_relative_path(path: str) -> str | None:
    raw = str(path or "").strip().replace("\\", "/")
    if raw.startswith("workspace/"):
        raw = raw.removeprefix("workspace/")
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        return None
    return relative.as_posix()


def read_file_at_ref(repository_dir: Path, ref: str, path: str) -> bytes | None:
    safe_path = safe_relative_path(path)
    if not safe_path:
        return None
    proc = subprocess.run(
        ["git", "show", f"{ref}:{safe_path}"],
        cwd=str(repository_dir),
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def file_entry(repository_dir: Path, ref: str, path: str) -> JsonObject | None:
    data = read_file_at_ref(repository_dir, ref, path)
    if data is None:
        return None
    return {
        "path": path,
        "type": "file",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def file_diff_status(before: bytes | None, after: bytes | None) -> str:
    if before is None and after is None:
        return "missing"
    if before is None:
        return "added"
    if after is None:
        return "deleted"
    return "unchanged" if before == after else "modified"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
