from __future__ import annotations

from pathlib import PurePosixPath

MAX_WORKSPACE_COMMIT_PATH_BYTES = 4 * 1024
MAX_WORKSPACE_COMMIT_DEPTH = 32


class WorkspaceCommitPathError(ValueError):
    """Git tree path 不是可发布的 Workspace UTF-8 相对路径。"""


class WorkspaceCommitPathTooLarge(WorkspaceCommitPathError):
    """Git tree path 超出 Workspace 包与 Runtime 的共同边界。"""


def validate_workspace_commit_path(raw_path: bytes) -> PurePosixPath:
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceCommitPathError("Workspace Git path must be UTF-8") from exc
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise WorkspaceCommitPathError(f"Unsafe workspace Git path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ".git" in parts:
        raise WorkspaceCommitPathError(f"Unsafe workspace Git path: {path!r}")
    if len(path.encode("utf-8")) + len("workspace/") > MAX_WORKSPACE_COMMIT_PATH_BYTES or len(parts) > MAX_WORKSPACE_COMMIT_DEPTH:
        raise WorkspaceCommitPathTooLarge(f"Workspace Git path exceeds limits: {path!r}")
    return PurePosixPath(*parts)
