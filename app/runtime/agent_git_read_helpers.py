"""Pure/read-only helpers shared by the Git-backed Agent version store."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from app.runtime.agent_git_errors import AgentGitError
from app.runtime.json_types import JsonObject


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
