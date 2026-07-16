"""发布控制器的人工出口命令。

与 agent_gov_release_controller 的自动路径（poll / 血缘校验 / 部署编排）分开：
这里只放**需要人为裁决**的操作，它们共同的契约是「必须带 --approved-by 且记入事件审计」。

之所以独立成模块，一是职责不同（自动收敛 vs 人工干预），二是控制器主文件已逼近
架构卫生的 800 行阈值——把人工出口继续堆进去会越过它。
"""

from __future__ import annotations

import json
import re

from agent_gov_release_state import (
    ControllerConfig,
    ControllerError,
    ReleaseStatus,
    StateStore,
    controller_lock,
)

_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _validated_manual_request(commit_sha: str, approved_by: str, action: str) -> None:
    if not _COMMIT_SHA_RE.fullmatch(commit_sha):
        raise ControllerError(f"invalid commit sha: {commit_sha}")
    if not approved_by.strip():
        raise ControllerError(f"manual {action} requires --approved-by")


def unquarantine(config: ControllerConfig, commit_sha: str, approved_by: str) -> int:
    """人工解封一个被隔离的提交，使其回到 CI 门等待。

    存在理由：隔离是终局判定，但判定依据可能已经变了（例如 PR 元数据在门禁通过后
    被编辑、或早期版本的控制器把一次传输故障错记成了血缘非法）。在此之前解封的
    唯一手段是手改 sqlite——那既无审计也易出错。

    QUARANTINED -> WAITING_CI 这条边已建模进 ALLOWED_TRANSITIONS，自动路径永不走它。
    """
    _validated_manual_request(commit_sha, approved_by, "unquarantine")
    with controller_lock(config.state_dir):
        store = StateStore(config.state_dir / "state.db")
        try:
            row = store.get_release(commit_sha)
            if row is None:
                raise ControllerError(f"unknown release commit: {commit_sha}")
            status = ReleaseStatus(row["status"])
            if status != ReleaseStatus.QUARANTINED:
                raise ControllerError(
                    f"commit {commit_sha} is not quarantined (status={status.value})"
                )
            reason = f"manually unquarantined by {approved_by}"
            store.transition(commit_sha, ReleaseStatus.WAITING_CI, reason=reason)
            store.add_event("manual_unquarantine", reason, commit_sha)
            print(json.dumps({"commit_sha": commit_sha, "status": "waiting_ci"}))
            return 0
        finally:
            store.close()


def set_cursor(config: ControllerConfig, commit_sha: str, approved_by: str) -> int:
    """人工把发布游标移到某个提交（人工审计后使用）。

    存在理由：compare_lineage 在 master 一次前进超过一页（250 提交）时会要求
    "manual audit required"，但此前没有任何工具能在审计之后把游标推过去——
    只能手改 sqlite。游标只接受已被本控制器登记过的提交，避免指向未知历史。
    """
    _validated_manual_request(commit_sha, approved_by, "cursor move")
    with controller_lock(config.state_dir):
        store = StateStore(config.state_dir / "state.db")
        try:
            if store.get_release(commit_sha) is None:
                raise ControllerError(
                    f"refusing to move the cursor to an untracked commit: {commit_sha}"
                )
            previous = store.get_metadata("cursor")
            store.set_metadata("cursor", commit_sha)
            reason = f"cursor moved from {previous or '<unset>'} by {approved_by}"
            store.add_event("manual_set_cursor", reason, commit_sha)
            print(json.dumps({"cursor": commit_sha, "previous": previous}))
            return 0
        finally:
            store.close()
