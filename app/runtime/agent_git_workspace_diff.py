from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from app.runtime.feedback_privacy import SENSITIVE_KEY_PARTS
from app.runtime.json_types import JsonObject

MAX_FILE_DIFF_BYTES = 200_000


def parse_workspace_changes(
    raw_status: str,
    *,
    normalize_path: Callable[[str], str | None],
) -> list[JsonObject]:
    changes: list[JsonObject] = []
    for line in raw_status.splitlines():
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        safe_path = normalize_path(raw_path)
        if not safe_path:
            continue
        ignored = index_status == "!" and worktree_status == "!"
        untracked = (index_status == "?" and worktree_status == "?") or ignored
        changes.append(
            {
                "path": safe_path,
                "status": workspace_change_status(index_status, worktree_status),
                "index_status": index_status,
                "worktree_status": worktree_status,
                "staged": index_status not in {" ", "?", "!"},
                "unstaged": worktree_status not in {" ", "?", "!"},
                "untracked": untracked,
                "ignored": ignored,
                "discardable": True,
            }
        )
    return changes


def workspace_change_status(index_status: str, worktree_status: str) -> str:
    if (index_status, worktree_status) in {("?", "?"), ("!", "!")}:
        return "untracked"
    if "D" in {index_status, worktree_status}:
        return "deleted"
    if "A" in {index_status, worktree_status}:
        return "added"
    if "R" in {index_status, worktree_status}:
        return "renamed"
    if "M" in {index_status, worktree_status}:
        return "modified"
    return "changed"


def untracked_workspace_file_diff(repository_dir: Path, safe_path: str, status: str) -> JsonObject:
    result: JsonObject = {
        "path": safe_path,
        "status": status,
        "unified_diff": "",
        "is_text": False,
        "truncated": False,
        "reason": None,
    }
    path = repository_dir / safe_path
    if path.is_dir():
        result["reason"] = "未跟踪目录未展开内容。"
        return result
    try:
        data = path.read_bytes()
    except OSError as exc:
        result["reason"] = f"{exc.__class__.__name__}: {exc}"
        return result
    if len(data) > MAX_FILE_DIFF_BYTES:
        result.update(
            {
                "status": "binary_or_too_large",
                "truncated": True,
                "reason": f"文件超过 {MAX_FILE_DIFF_BYTES} bytes，未展开内容。",
            }
        )
        return result
    if b"\x00" in data:
        result["reason"] = "文件包含二进制内容，未展开内容。"
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result["reason"] = "文件不是 UTF-8 文本，未展开内容。"
        return result
    result["is_text"] = True
    result["unified_diff"] = redact_sensitive_diff(
        "".join(
            difflib.unified_diff(
                [],
                text.splitlines(keepends=True),
                fromfile=f"HEAD:{safe_path}",
                tofile=f"workspace:{safe_path}",
                lineterm="\n",
            )
        )
    )
    return result


def workspace_diff_error(path: str, status: str, reason: str) -> JsonObject:
    return {
        "path": path,
        "status": status,
        "unified_diff": "",
        "is_text": False,
        "truncated": False,
        "reason": reason,
    }


def redact_sensitive_diff(diff: str) -> str:
    lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        lowered = line.lower()
        if line.startswith(("+++", "---", "@@")) or not any(part in lowered for part in SENSITIVE_KEY_PARTS):
            lines.append(line)
            continue
        marker = line[:1] if line[:1] in {"+", "-", " "} else ""
        newline = "\n" if line.endswith("\n") else ""
        lines.append(f"{marker}[redacted sensitive line]{newline}")
    return "".join(lines)
