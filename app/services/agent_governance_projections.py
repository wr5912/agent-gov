"""版本治理的 ORM row -> JSON 边界投影。

从 `agent_governance.py` 拆出：这些是持久化行到 HTTP 契约的纯投影，不参与治理编排，也不碰
session。放在一起使编排服务只保留决策与事务，且不再超出单文件行数阈值。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

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
        "digest": candidate_diff_digest(diff),
    }


def candidate_diff_digest(diff: JsonObject) -> str:
    """为精确 base/candidate 文件清单生成稳定审批指纹。"""

    encoded = json.dumps(
        diff,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approval_evidence(diff: JsonObject, candidate_commit_sha: str, passed_run: JsonObject) -> JsonObject:
    return {
        "candidate_commit_sha": candidate_commit_sha,
        "diff_digest": candidate_diff_digest(diff),
        "test_run_id": str(passed_run["test_run_id"]),
        "suite_digest": str(passed_run["suite_digest"]),
    }


def require_approval_evidence(
    change_set: JsonObject,
    diff: JsonObject | None,
    passed_run: JsonObject | None,
) -> JsonObject:
    if change_set.get("status") != "pending_approval":
        raise ValueError("Agent change set must have a recorded approval request before approval")
    if not all(str(change_set.get(field) or "").strip() for field in ("approval_reason", "impact_scope", "rollback_plan")):
        raise ValueError("Agent change set approval request is incomplete")
    candidate = str(change_set.get("candidate_commit_sha") or "")
    if diff is None:
        raise ValueError("Unable to inspect candidate diff for approval")
    if passed_run is None:
        raise ValueError("候选审批前必须完成且通过精确 candidate commit 的平台测试。")
    return approval_evidence(diff, candidate, passed_run)


def approval_evidence_matches(
    value: object,
    *,
    diff: JsonObject,
    candidate_commit_sha: str,
    passed_run: JsonObject,
) -> bool:
    evidence = dict(value) if isinstance(value, dict) else {}
    expected = approval_evidence(diff, candidate_commit_sha, passed_run)
    return all(evidence.get(key) == expected_value for key, expected_value in expected.items())


def manual_approval_paths(diff: JsonObject) -> tuple[str, ...]:
    sensitive: set[str] = set()
    for bucket in ("added", "modified", "deleted"):
        entries = diff.get(bucket)
        if not isinstance(entries, list):
            raise ValueError("Candidate diff is invalid for mandatory approval")
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and (path in {"AGENT.md", "agent.yaml"} or path.startswith(("skills/", "mcp/", "subagents/"))):
                sensitive.add(path)
    return tuple(sorted(sensitive))


def matching_passed_test_run(
    value: object,
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str | None,
    not_before: str | None = None,
) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    if (
        str(value.get("agent_id") or "") != agent_id
        or str(value.get("commit_sha") or "") != commit_sha
        or str(value.get("status") or "") != "passed"
        or (change_set_id is not None and str(value.get("change_set_id") or "") != change_set_id)
        or not str(value.get("test_run_id") or "")
        or not str(value.get("suite_digest") or "")
        or (not_before is not None and str(value.get("created_at") or "") <= not_before)
    ):
        return None
    items = value.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) or item.get("outcome") != "passed" for item in items):
        return None
    return value


def load_matching_passed_test_run(
    latest_passed_test_run: Callable[..., JsonObject | None] | None,
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str | None,
    not_before: str | None = None,
) -> JsonObject | None:
    if not commit_sha or latest_passed_test_run is None:
        return None
    return matching_passed_test_run(
        latest_passed_test_run(agent_id=agent_id, commit_sha=commit_sha),
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
        not_before=not_before,
    )


def load_bound_approval_test_run(
    test_run_by_id: Callable[[str], JsonObject | None] | None,
    approval: object,
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str,
) -> JsonObject | None:
    evidence = dict(approval) if isinstance(approval, dict) else {}
    test_run_id = str(evidence.get("test_run_id") or "")
    if not test_run_id or test_run_by_id is None:
        return None
    return matching_passed_test_run(
        test_run_by_id(test_run_id),
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
    )


def load_publication_test_run(
    latest_passed_test_run: Callable[..., JsonObject | None] | None,
    test_run_by_id: Callable[[str], JsonObject | None] | None,
    approval: object,
    *,
    status: str,
    agent_id: str,
    commit_sha: str,
    change_set_id: str,
) -> JsonObject | None:
    if status == "approved":
        return load_bound_approval_test_run(
            test_run_by_id,
            approval,
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
        )
    return load_matching_passed_test_run(
        latest_passed_test_run,
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=None,
    )


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def publication_blocker_for_change_set(change_set: JsonObject) -> str | None:
    blocker = change_set.get("publication_blocker")
    return str(blocker) if blocker else None


def projected_publication_blocker(change_set: JsonObject, passed_run: JsonObject | None) -> str | None:
    quarantine = change_set.get("legacy_publication_quarantine")
    if isinstance(quarantine, dict) and quarantine.get("detail"):
        return str(quarantine["detail"])
    provenance = change_set.get("publication_provenance_blocker")
    if provenance:
        return str(provenance)
    return None if passed_run else "待发布版本缺少 commit_sha 完全匹配且通过的平台测试运行记录。"
