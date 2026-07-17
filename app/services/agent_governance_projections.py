"""版本治理的 ORM row -> JSON 边界投影。

从 `agent_governance.py` 拆出：这些是持久化行到 HTTP 契约的纯投影，不参与治理编排，也不碰
session。放在一起使编排服务只保留决策与事务，且不再超出单文件行数阈值。
"""

from __future__ import annotations

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetEventModel, AgentReleaseModel


def event_to_payload(row: AgentChangeSetEventModel) -> JsonObject:
    return {
        "event_id": row.event_id,
        "change_set_id": row.change_set_id,
        "action": row.action,
        "operator": row.operator,
        "created_at": row.created_at,
        "before": row.before_json or {},
        "after": row.after_json or {},
    }


def release_to_payload(row: AgentReleaseModel) -> JsonObject:
    """把 release 行投影为 API 载荷。

    `agent_id or "main-agent"` 是历史数据回填：早于多业务 Agent 模型的旧行没有 agent_id，
    它们当时就属于 main。这与「main 是可删除的普通业务 Agent」不冲突——这里读的是旧行事实，
    不是运行时默认。
    """

    payload = dict(row.payload_json or {})
    payload.update(
        {
            "release_id": row.release_id,
            "agent_id": row.agent_id or "main-agent",
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "status": row.status,
            "tag_name": row.tag_name,
            "commit_sha": row.commit_sha,
            "change_set_id": row.change_set_id,
            "rollback_of_release_id": row.rollback_of_release_id,
            "archive_path": row.archive_path,
        }
    )
    return payload


def diff_summary(diff: JsonObject) -> JsonObject:
    return {
        "added": len(diff.get("added") or []),
        "modified": len(diff.get("modified") or []),
        "deleted": len(diff.get("deleted") or []),
    }


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
