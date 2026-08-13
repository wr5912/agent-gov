from __future__ import annotations

import re
from collections.abc import Callable

_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def workspace_activation_graph_is_valid(
    *,
    action: str,
    snapshot_created: bool,
    original_commit: str,
    base_commit: str,
    candidate_commit: str,
    candidate_tree: str,
    target_commit: str | None,
    original_index_tree: str,
    object_type: Callable[[str], str | None],
    commit_parent_shas: Callable[[str], tuple[str, ...]],
    commit_tree_sha: Callable[[str], str],
    allow_missing_historical_objects: bool = False,
) -> bool:
    """验证持久化激活 journal 描述的是一条完整、无歧义的提交图。"""

    if not workspace_activation_graph_shape_is_valid(
        action=action,
        snapshot_created=snapshot_created,
        original_commit=original_commit,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        target_commit=target_commit,
        original_index_tree=original_index_tree,
    ):
        return False
    try:
        commit_objects = (original_commit, base_commit, candidate_commit)
        if any(object_type(object_id) != "commit" for object_id in commit_objects):
            return False
        if object_type(candidate_tree) != "tree":
            return False
        original_index_type = object_type(original_index_tree)
        if original_index_type != "tree" and not (allow_missing_historical_objects and original_index_type is None):
            return False
        if snapshot_created:
            if commit_parent_shas(base_commit) != (original_commit,):
                return False
        if action == "import_unchanged":
            if candidate_commit != base_commit:
                return False
        elif action in {"import_overwrite", "restore"}:
            if commit_parent_shas(candidate_commit) != (base_commit,):
                return False
        else:
            return False
        if commit_tree_sha(candidate_commit) != candidate_tree:
            return False
        return _target_is_valid(
            target_commit,
            candidate_tree=candidate_tree,
            object_type=object_type,
            commit_tree_sha=commit_tree_sha,
            allow_missing=allow_missing_historical_objects,
        )
    except Exception:  # noqa: BLE001 - Git object lookup failure is invalid graph evidence.
        return False


def workspace_activation_graph_shape_is_valid(
    *,
    action: str,
    snapshot_created: bool,
    original_commit: str,
    base_commit: str,
    candidate_commit: str,
    candidate_tree: str,
    target_commit: str | None,
    original_index_tree: str,
) -> bool:
    object_ids = (
        original_commit,
        base_commit,
        candidate_commit,
        candidate_tree,
        original_index_tree,
    )
    if not all(_OBJECT_ID.fullmatch(object_id) for object_id in object_ids):
        return False
    if target_commit is not None and not _OBJECT_ID.fullmatch(target_commit):
        return False
    if snapshot_created != (base_commit != original_commit):
        return False
    if action == "import_unchanged":
        return candidate_commit == base_commit and target_commit is None
    if action == "import_overwrite":
        return candidate_commit != base_commit and target_commit is None
    if action == "restore":
        return candidate_commit != base_commit and bool(target_commit)
    return False


def workspace_activation_early_graph_shape_is_valid(
    *,
    action: str,
    snapshot_created: bool,
    original_commit: str,
    base_commit: str | None,
    candidate_commit: str | None,
    candidate_tree: str | None,
    target_commit: str | None,
    original_index_tree: str | None,
) -> bool:
    return (
        action in {"import_overwrite", "import_unchanged", "restore"}
        and bool(_OBJECT_ID.fullmatch(original_commit))
        and not snapshot_created
        and not any((base_commit, candidate_commit, candidate_tree, target_commit, original_index_tree))
    )


def _target_is_valid(
    target_commit: str | None,
    *,
    candidate_tree: str,
    object_type: Callable[[str], str],
    commit_tree_sha: Callable[[str], str],
    allow_missing: bool,
) -> bool:
    if target_commit is None:
        return True
    target_type = object_type(target_commit)
    if target_type is None:
        return allow_missing
    return target_type == "commit" and commit_tree_sha(target_commit) == candidate_tree
