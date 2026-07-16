"""发布控制器的人工路径：出口命令，及其状态语义。

与 agent_gov_release_controller 的自动路径（poll / 血缘校验 / 部署编排）分开：
这里只放**需要人为裁决**的操作，它们共同的契约是「必须带 --approved-by 且记入事件审计」。

边界划在**编排 vs 状态语义**：跑部署脚本、决定动作顺序留在控制器（与 complete_release
等自动部署路径共用 run_logged）；人工动作**落到 store 的那一半**在这里，且一律是纯 store
写入——不导入控制器（否则成环），outbox 条目由调用方算好传入。

之所以独立成模块，一是职责不同（自动收敛 vs 人工干预），二是控制器主文件已逼近
架构卫生的 800 行阈值——把人工出口继续堆进去会越过它。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence

from agent_gov_release_state import (
    ALLOWED_TRANSITIONS,
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


def currently_active_release(
    config: ControllerConfig,
    store: StateStore,
    *,
    exclude_release_id: str,
) -> sqlite3.Row | None:
    """取当前在线的 release 行；它就是人工回滚将要换下的那一个。

    必须在跑部署脚本**之前**取：脚本一旦成功，机器上的 current 已经变了。
    """
    active_id = store.get_metadata(f"active:{config.environment}")
    if not active_id or active_id == exclude_release_id:
        return None
    return store.get_release_by_id(active_id)


def record_manual_rollback(
    config: ControllerConfig,
    store: StateStore,
    *,
    replaced: sqlite3.Row | None,
    activated_id: str,
    reason: str,
    outbox_items: Sequence[tuple[str, str, Mapping[str, object]]],
) -> None:
    """把人工回滚记成一次有状态语义的动作，而不是裸写 active metadata。

    此前只写 metadata、不动被换下 release 的状态，于是它仍是 SUCCEEDED；30 秒后
    poll 的 `cursor == head_sha` 分支看到「head 仍成功」就把 active 覆写回去——
    机器上跑 A、`releasectl status` 说 B，且永不自愈。

    现在把被换下的 release 原子地置为 ROLLED_BACK，并在同一事务里落 active 指针与
    outbox 通知（复用 finalize_release，与自动回滚同一条路径）。
    """
    active_key = f"active:{config.environment}"
    if replaced is None:
        # 没有被换下的对象（首次激活、或目标本就在线）：只落 active 指针。
        _activate_without_transition(store, active_key, activated_id, outbox_items)
        return
    replaced_status = ReleaseStatus(replaced["status"])
    if ReleaseStatus.ROLLED_BACK not in ALLOWED_TRANSITIONS[replaced_status]:
        # 被换下的 release 不处在可回滚状态（例如已是 ROLLED_BACK）：不强推状态机，
        # 只落 active 指针并如实记账，避免用一次非法转移把审计写脏。
        _activate_without_transition(store, active_key, activated_id, outbox_items)
        store.add_event(
            "manual_rollback_replaced_non_successful",
            f"replaced={replaced['release_id']} status={replaced_status.value}; {reason}",
            str(replaced["commit_sha"]),
        )
        return
    store.finalize_release(
        str(replaced["commit_sha"]),
        ReleaseStatus.ROLLED_BACK,
        reason=reason,
        metadata={active_key: activated_id},
        outbox=outbox_items,
    )


def _activate_without_transition(
    store: StateStore,
    active_key: str,
    activated_id: str,
    outbox_items: Sequence[tuple[str, str, Mapping[str, object]]],
) -> None:
    store.set_metadata(active_key, activated_id)
    for dedupe_key, kind, payload in outbox_items:
        store.enqueue_outbox(dedupe_key, kind, payload)
