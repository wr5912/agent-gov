from __future__ import annotations

import re
from dataclasses import dataclass

_OBJECT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OBJECT_ID_TEXT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class RawGitCommitError(RuntimeError):
    pass


@dataclass(frozen=True)
class RawGitCommitEvidence:
    tree_sha: str
    parent_shas: tuple[str, ...]


def require_git_object_id(value: str) -> str:
    if not _OBJECT_ID_TEXT.fullmatch(value):
        raise RawGitCommitError("Git object id is invalid")
    return value


def parse_raw_git_commit(
    content: bytes,
    *,
    expected_object_id: str,
) -> RawGitCommitEvidence:
    expected = require_git_object_id(expected_object_id).encode("ascii")
    raw_headers, separator, _message = content.partition(b"\n\n")
    if not separator or b"\0" in raw_headers:
        raise RawGitCommitError("Git commit object headers are invalid")
    tree: bytes | None = None
    parents: list[bytes] = []
    for line in raw_headers.splitlines():
        if line.startswith(b" "):
            continue
        name, field_separator, value = line.partition(b" ")
        if not field_separator:
            raise RawGitCommitError("Git commit object header is malformed")
        if name == b"tree":
            if tree is not None:
                raise RawGitCommitError("Git commit object has duplicate tree headers")
            tree = value
        elif name == b"parent":
            parents.append(value)
    object_ids = [tree, *parents]
    if tree is None or any(value is None or len(value) != len(expected) or not _OBJECT_ID.fullmatch(value) for value in object_ids):
        raise RawGitCommitError("Git commit object graph headers are invalid")
    return RawGitCommitEvidence(
        tree_sha=tree.decode("ascii"),
        parent_shas=tuple(parent.decode("ascii") for parent in parents),
    )
