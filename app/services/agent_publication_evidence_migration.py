from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import (
    AgentChangeSetEventModel,
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentReleaseSourceClaimModel,
    AgentReleaseTagClaimModel,
    utc_now,
)
from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.services.agent_candidate_approval import CANDIDATE_EVIDENCE_EPOCH
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import candidate_diff_digest, manual_approval_paths
from app.services.agent_publication import PublicationIntent, release_matches_intent
from app.services.agent_publication_validation import require_publication_intent_evidence


class PublicationEvidenceMigrationHost(Protocol):
    feedback_store: Any

    def _store_for_read_only(self, agent_id: str | None) -> GitAgentVersionStore: ...


@dataclass(frozen=True)
class PublicationEvidenceMigrationReport:
    reset_for_review: int = 0
    reset_for_test: int = 0
    reset_unpublished_intent: int = 0
    archived_published_identity: int = 0
    quarantined: int = 0

    def to_payload(self) -> JsonObject:
        return {
            "reset_for_review": self.reset_for_review,
            "reset_for_test": self.reset_for_test,
            "reset_unpublished_intent": self.reset_unpublished_intent,
            "archived_published_identity": self.archived_published_identity,
            "quarantined": self.quarantined,
        }


@dataclass(frozen=True)
class _ChangeSetSnapshot:
    change_set_id: str
    agent_id: str
    status: str
    updated_at: str
    base_commit_sha: str
    candidate_commit_sha: str
    record: JsonObject


@dataclass(frozen=True)
class _CandidateInspection:
    diff_digest: str
    sensitive_paths: tuple[str, ...]
    store: GitAgentVersionStore


_ACTIVE_EVIDENCE_STATES = frozenset({"candidate_committed", "pending_approval", "approved"})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def reconcile_legacy_publication_evidence(
    host: PublicationEvidenceMigrationHost,
) -> PublicationEvidenceMigrationReport:
    """把旧发布状态迁入精确候选证据契约，且绝不合成测试或审批事实。"""

    report = PublicationEvidenceMigrationReport()
    for snapshot in _legacy_snapshots(host.feedback_store.Session):
        if snapshot.status in _ACTIVE_EVIDENCE_STATES:
            if _release_exists(host.feedback_store.Session, snapshot.change_set_id):
                result = _commit_quarantine(
                    host.feedback_store.Session,
                    snapshot,
                    None,
                    "活动候选已存在无不可变 publication intent 归属的 release，禁止继续发布",
                )
            else:
                result = _reset_legacy_candidate(host, snapshot)
        elif snapshot.status == "publishing":
            current_intent = _current_intent(snapshot)
            if current_intent is not None:
                result = _verify_current_publishing(host, snapshot, current_intent)
            elif _declares_current_intent(snapshot):
                result = _quarantine_invalid_current_intent(host, snapshot)
            else:
                result = _reconcile_legacy_publishing(host, snapshot)
        else:
            current_intent = _current_intent(snapshot)
            if current_intent is not None:
                result = _verify_current_published(host, snapshot, current_intent)
            elif _declares_current_intent(snapshot):
                result = _quarantine_invalid_current_intent(host, snapshot)
            else:
                result = _archive_legacy_published(host, snapshot)
        if result is not None:
            report = PublicationEvidenceMigrationReport(
                reset_for_review=report.reset_for_review + (result == "reset_for_review"),
                reset_for_test=report.reset_for_test + (result == "reset_for_test"),
                reset_unpublished_intent=report.reset_unpublished_intent + (result == "reset_unpublished_intent"),
                archived_published_identity=report.archived_published_identity + (result == "archived_published_identity"),
                quarantined=report.quarantined + (result == "quarantined"),
            )
    return report


def _legacy_snapshots(session_factory: sessionmaker) -> tuple[_ChangeSetSnapshot, ...]:
    with session_factory() as db:
        rows = list(
            db.scalars(
                select(AgentChangeSetModel).where(
                    AgentChangeSetModel.status.in_((*_ACTIVE_EVIDENCE_STATES, "publishing", "published")),
                ),
            ).all(),
        )
        release_change_set_ids = {str(change_set_id) for change_set_id in db.scalars(select(AgentReleaseModel.change_set_id)).all() if change_set_id}
    snapshots: list[_ChangeSetSnapshot] = []
    for row in rows:
        payload = dict(row.payload_json or {})
        if _uses_current_contract(row.status, payload, has_release=row.change_set_id in release_change_set_ids):
            continue
        snapshots.append(
            _ChangeSetSnapshot(
                change_set_id=str(row.change_set_id),
                agent_id=str(row.agent_id),
                status=str(row.status),
                updated_at=str(row.updated_at),
                base_commit_sha=str(row.base_commit_sha),
                candidate_commit_sha=str(row.candidate_commit_sha or ""),
                record=payload,
            ),
        )
    return tuple(snapshots)


def _uses_current_contract(status: str, change_set: JsonObject, *, has_release: bool) -> bool:
    if status in _ACTIVE_EVIDENCE_STATES:
        if has_release:
            return False
        if change_set.get("candidate_evidence_epoch") != CANDIDATE_EVIDENCE_EPOCH:
            return False
        return status != "approved" or _strict_approval_evidence(change_set.get("approval_evidence"))
    return bool(change_set.get("legacy_publication_quarantine") or change_set.get("legacy_publication_identity"))


def _release_exists(session_factory: sessionmaker, change_set_id: str) -> bool:
    with session_factory() as db:
        return db.scalar(select(AgentReleaseModel.release_id).where(AgentReleaseModel.change_set_id == change_set_id).limit(1)) is not None


def _strict_approval_evidence(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _is_hex(value.get("candidate_commit_sha"), _HEX40)
        and _is_hex(value.get("diff_digest"), _HEX64)
        and isinstance(value.get("test_run_id"), str)
        and bool(value["test_run_id"])
        and _is_hex(value.get("suite_digest"), _HEX64)
        and _is_hex(value.get("review_digest"), _HEX64)
        and type(value.get("reviewed_file_count")) is int
        and int(value["reviewed_file_count"]) > 0
    )


def _is_hex(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _inspect_candidate(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
) -> _CandidateInspection | None:
    if not _is_hex(snapshot.candidate_commit_sha, _HEX40):
        return None
    try:
        store = host._store_for_read_only(snapshot.agent_id)
        diff = store.diff_versions(snapshot.base_commit_sha, snapshot.candidate_commit_sha)
        if diff is None:
            return None
        return _CandidateInspection(
            diff_digest=candidate_diff_digest(diff),
            sensitive_paths=manual_approval_paths(diff),
            store=store,
        )
    except (AgentGitError, OSError, RuntimeError, ValueError):
        return None


def _reset_legacy_candidate(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
) -> str | None:
    inspection = _inspect_candidate(host, snapshot)
    if inspection is None:
        return _commit_quarantine(
            host.feedback_store.Session,
            snapshot,
            None,
            "旧候选 Git diff 无法安全核验，未写入当前证据 epoch",
        )
    sensitive_paths = inspection.sensitive_paths
    target = "pending_approval" if sensitive_paths else "candidate_committed"
    reason = "旧审批/测试证据不满足精确候选契约，必须重新执行平台测试"
    if sensitive_paths:
        reason += "并逐文件复核"
    return _commit_reset(
        host.feedback_store.Session,
        snapshot,
        target=target,
        sensitive_paths=sensitive_paths,
        reason=reason,
        clear_claims=False,
        action="legacy_candidate_evidence_reset",
        result="reset_for_review" if sensitive_paths else "reset_for_test",
    )


def _reconcile_legacy_publishing(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
) -> str | None:
    identity = _legacy_intent_identity(snapshot)
    inspection = _inspect_candidate(host, snapshot)
    if identity is None or inspection is None:
        return _commit_quarantine(host.feedback_store.Session, snapshot, identity, "旧发布意图或候选 Git 身份无法安全核验")
    try:
        side_effects = inspection.store.publication_side_effects_present(
            snapshot.candidate_commit_sha,
            str(identity["tag_name"]),
            previous_commit_sha=str(identity.get("previous_commit_sha") or "") or None,
        )
    except (AgentGitError, OSError, RuntimeError):
        side_effects = True
    if side_effects:
        return _commit_quarantine(host.feedback_store.Session, snapshot, identity, "旧发布意图已有或无法排除 Git/tag 副作用")
    target = "pending_approval" if inspection.sensitive_paths else "candidate_committed"
    return _commit_reset(
        host.feedback_store.Session,
        snapshot,
        target=target,
        sensitive_paths=inspection.sensitive_paths,
        reason="旧发布意图未产生 Git/tag 副作用；已撤销并要求重新建立精确候选证据",
        clear_claims=True,
        action="legacy_publication_reset",
        result="reset_unpublished_intent",
    )


def _archive_legacy_published(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
) -> str | None:
    identity = _legacy_intent_identity(snapshot)
    inspection = _inspect_candidate(host, snapshot)
    if identity is None or inspection is None:
        return _commit_quarantine(host.feedback_store.Session, snapshot, identity, "旧已发布记录的 Git 身份无法安全核验")
    try:
        published_identity_matches = inspection.store.published_identity_matches(
            snapshot.candidate_commit_sha,
            str(identity["tag_name"]),
        )
    except (AgentGitError, OSError, RuntimeError):
        published_identity_matches = False
    with host.feedback_store.Session() as db:
        release = _matching_release(db, snapshot, identity)
    if not published_identity_matches or release is None:
        return _commit_quarantine(host.feedback_store.Session, snapshot, identity, "旧已发布记录缺少一致的 release 与 Git/tag 身份")
    archived_identity = {
        **identity,
        "schema_version": "legacy-publication-identity/v1",
        "diff_digest": inspection.diff_digest,
        "release_id": str(release.release_id),
        "read_only": True,
    }
    return _commit_published_archive(host.feedback_store.Session, snapshot, archived_identity)


def _legacy_intent_identity(snapshot: _ChangeSetSnapshot) -> JsonObject | None:
    value = snapshot.record.get("publication_intent")
    if not isinstance(value, dict):
        return None
    required = (
        "release_id",
        "change_set_id",
        "agent_id",
        "commit_sha",
        "tag_name",
        "operator",
        "previous_status",
        "started_at",
    )
    if any(not isinstance(value.get(field), str) or not value[field] for field in required):
        return None
    if type(value.get("force", False)) is not bool:
        return None
    force = value.get("force", False)
    note = value.get("note")
    force_blocker = value.get("force_publication_blocker")
    source_improvement_id = value.get("source_improvement_id")
    if (
        (note is not None and not isinstance(note, str))
        or (force_blocker is not None and not isinstance(force_blocker, str))
        or (source_improvement_id is not None and not isinstance(source_improvement_id, str))
        or value["previous_status"] not in {"candidate_committed", "approved"}
        or (force and (not isinstance(note, str) or not note.strip()))
        or force != (isinstance(force_blocker, str) and bool(force_blocker))
    ):
        return None
    if (
        value["change_set_id"] != snapshot.change_set_id
        or value["agent_id"] != snapshot.agent_id
        or value["commit_sha"] != snapshot.candidate_commit_sha
        or value.get("previous_commit_sha") != snapshot.base_commit_sha
        or not _is_hex(value["commit_sha"], _HEX40)
    ):
        return None
    return {
        "release_id": str(value["release_id"]),
        "change_set_id": snapshot.change_set_id,
        "agent_id": snapshot.agent_id,
        "commit_sha": snapshot.candidate_commit_sha,
        "tag_name": str(value["tag_name"]),
        "operator": str(value["operator"]),
        "note": note,
        "force": force,
        "force_publication_blocker": force_blocker,
        "previous_status": str(value["previous_status"]),
        "started_at": str(value["started_at"]),
        "source_improvement_id": source_improvement_id,
        "previous_commit_sha": snapshot.base_commit_sha,
    }


def _current_intent(snapshot: _ChangeSetSnapshot) -> PublicationIntent | None:
    try:
        intent = PublicationIntent.from_payload(snapshot.record.get("publication_intent"))
    except ValueError:
        return None
    expected = (
        snapshot.change_set_id,
        snapshot.agent_id,
        snapshot.candidate_commit_sha,
        snapshot.base_commit_sha,
    )
    actual = (intent.change_set_id, intent.agent_id, intent.commit_sha, intent.previous_commit_sha)
    return intent if actual == expected else None


def _declares_current_intent(snapshot: _ChangeSetSnapshot) -> bool:
    value = snapshot.record.get("publication_intent")
    return isinstance(value, dict) and {"diff_digest", "test_run_id", "suite_digest"} <= set(value)


def _quarantine_invalid_current_intent(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
) -> str | None:
    return _commit_quarantine(
        host.feedback_store.Session,
        snapshot,
        {"contract": "publication-intent", "valid": False},
        "当前 publication intent 的字段、类型、格式或 change set 绑定无效",
    )


def _verify_current_published(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
    intent: PublicationIntent,
) -> str | None:
    inspection = _inspect_candidate(host, snapshot)
    if inspection is None:
        return _commit_quarantine(host.feedback_store.Session, snapshot, intent.to_payload(), "当前已发布记录的候选 Git diff 无法安全核验")
    try:
        git_matches = inspection.store.published_identity_matches(intent.commit_sha, intent.tag_name)
    except (AgentGitError, OSError, RuntimeError):
        git_matches = False
    evidence_matches = _current_intent_evidence_matches(host, snapshot, intent, inspection.store)
    with host.feedback_store.Session() as db:
        release = _matching_release(db, snapshot, intent.to_payload())
    force_projection_matches = (
        snapshot.record.get("force_published") is True
        and snapshot.record.get("force_publish_reason") == intent.note
        and snapshot.record.get("force_publication_blocker") == intent.force_publication_blocker
        if intent.force
        else snapshot.record.get("force_published") is None or snapshot.record.get("force_published") is False
    )
    if git_matches and evidence_matches and force_projection_matches and release is not None and release_matches_intent(release, intent):
        return None
    return _commit_quarantine(
        host.feedback_store.Session,
        snapshot,
        intent.to_payload(),
        "当前已发布记录的 publication intent、release 与 Git/tag 身份不一致",
    )


def _verify_current_publishing(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
    intent: PublicationIntent,
) -> str | None:
    inspection = _inspect_candidate(host, snapshot)
    if inspection is not None and _current_intent_evidence_matches(host, snapshot, intent, inspection.store):
        return None
    return _commit_quarantine(
        host.feedback_store.Session,
        snapshot,
        intent.to_payload(),
        "当前 publishing 记录的 publication intent 候选、diff、测试或审批证据无效",
    )


def _current_intent_evidence_matches(
    host: PublicationEvidenceMigrationHost,
    snapshot: _ChangeSetSnapshot,
    intent: PublicationIntent,
    store: GitAgentVersionStore,
) -> bool:
    with host.feedback_store.Session() as db:
        row = db.get(AgentChangeSetModel, snapshot.change_set_id)
        if row is None:
            return False
        try:
            require_publication_intent_evidence(db, row=row, intent=intent, store=store)
        except (AgentGovernanceError, AgentGitError, OSError, RuntimeError, ValueError):
            return False
    return True


def _matching_release(
    db: Session,
    snapshot: _ChangeSetSnapshot,
    identity: JsonObject,
) -> AgentReleaseModel | None:
    rows = list(
        db.scalars(
            select(AgentReleaseModel).where(AgentReleaseModel.change_set_id == snapshot.change_set_id).limit(2),
        ).all(),
    )
    if len(rows) != 1:
        return None
    row = rows[0]
    expected = (
        identity["release_id"],
        snapshot.agent_id,
        identity["started_at"],
        snapshot.candidate_commit_sha,
        identity["tag_name"],
        "published",
    )
    actual = (row.release_id, row.agent_id, row.created_at, row.commit_sha, row.tag_name, row.status)
    if actual != expected:
        return None
    payload = dict(row.payload_json or {})
    immutable_payload = {
        "schema_version": "agent-release/v1",
        "release_id": identity["release_id"],
        "agent_id": snapshot.agent_id,
        "created_at": identity["started_at"],
        "status": "published",
        "tag_name": identity["tag_name"],
        "commit_sha": snapshot.candidate_commit_sha,
        "previous_commit_sha": snapshot.base_commit_sha,
        "source_improvement_id": identity.get("source_improvement_id"),
        "change_set_id": snapshot.change_set_id,
        "rollback_of_release_id": None,
        "note": identity.get("note"),
        "operator": identity["operator"],
        "force_published": identity["force"],
        "force_publication_blocker": identity.get("force_publication_blocker") if identity["force"] else None,
        "force_publish_reason": identity.get("note") if identity["force"] else None,
    }
    return row if all(payload.get(field) == expected_value for field, expected_value in immutable_payload.items()) else None


def _commit_reset(
    session_factory: sessionmaker,
    snapshot: _ChangeSetSnapshot,
    *,
    target: str,
    sensitive_paths: tuple[str, ...],
    reason: str,
    clear_claims: bool,
    action: str,
    result: str,
) -> str | None:
    now = utc_now()
    payload = dict(snapshot.record)
    payload.pop("publication_intent", None)
    payload.pop("legacy_publication_quarantine", None)
    payload.update(
        {
            "status": target,
            "updated_at": now,
            "approval_note": None,
            "approval_evidence": None,
            "latest_test_run_id": None,
            "latest_test_run": None,
            "candidate_evidence_epoch": CANDIDATE_EVIDENCE_EPOCH,
            "evidence_not_before": now,
            "legacy_evidence_migration": {"action": action, "reason": reason, "migrated_at": now},
            "publication_error": None,
        },
    )
    if sensitive_paths:
        payload.update(
            {
                "approval_reason": "Sensitive Harness paths changed: " + ", ".join(sensitive_paths),
                "impact_scope": "Agent instructions, skills, MCP, manifest, or subagent execution boundary",
                "rollback_plan": f"Restore base commit {snapshot.base_commit_sha}",
            },
        )
    with session_factory.begin() as db:
        begin_sqlite_write_transaction(db.connection())
        row = _unchanged_row(db, snapshot)
        if row is None:
            return None
        before = _row_payload(row)
        row.status = target
        row.updated_at = now
        row.payload_json = payload
        if clear_claims:
            _clear_owned_claims(db, snapshot)
        _add_event(db, snapshot.change_set_id, action, before, _row_payload(row))
    return result


def _commit_quarantine(
    session_factory: sessionmaker,
    snapshot: _ChangeSetSnapshot,
    identity: JsonObject | None,
    detail: str,
) -> str | None:
    existing = snapshot.record.get("legacy_publication_quarantine")
    if isinstance(existing, dict) and existing.get("detail") == detail and existing.get("identity") == identity:
        return None
    now = utc_now()
    payload = dict(snapshot.record)
    payload.update(
        {
            "legacy_publication_quarantine": {
                "schema_version": "legacy-publication-quarantine/v1",
                "detail": detail,
                "detected_at": now,
                "identity": identity,
            },
            "publication_error": {"detail": detail, "updated_at": now},
        },
    )
    with session_factory.begin() as db:
        begin_sqlite_write_transaction(db.connection())
        row = _unchanged_row(db, snapshot)
        if row is None:
            return None
        before = _row_payload(row)
        row.updated_at = now
        row.payload_json = payload
        _add_event(db, snapshot.change_set_id, "legacy_publication_quarantined", before, _row_payload(row))
    return "quarantined"


def _commit_published_archive(
    session_factory: sessionmaker,
    snapshot: _ChangeSetSnapshot,
    identity: JsonObject,
) -> str | None:
    now = utc_now()
    payload = dict(snapshot.record)
    payload.pop("publication_intent", None)
    payload.update(
        {
            "legacy_publication_identity": identity,
            "legacy_evidence_migration": {
                "action": "legacy_published_identity_archived",
                "migrated_at": now,
            },
        },
    )
    with session_factory.begin() as db:
        begin_sqlite_write_transaction(db.connection())
        row = _unchanged_row(db, snapshot)
        if row is None:
            return None
        before = _row_payload(row)
        row.updated_at = now
        row.payload_json = payload
        _add_event(db, snapshot.change_set_id, "legacy_published_identity_archived", before, _row_payload(row))
    return "archived_published_identity"


def _unchanged_row(db: Session, snapshot: _ChangeSetSnapshot) -> AgentChangeSetModel | None:
    row = db.get(AgentChangeSetModel, snapshot.change_set_id)
    if row is None:
        return None
    actual = (
        row.status,
        row.updated_at,
        row.base_commit_sha,
        row.candidate_commit_sha or "",
        dict(row.payload_json or {}),
    )
    expected = (
        snapshot.status,
        snapshot.updated_at,
        snapshot.base_commit_sha,
        snapshot.candidate_commit_sha,
        snapshot.record,
    )
    return row if actual == expected else None


def _clear_owned_claims(db: Session, snapshot: _ChangeSetSnapshot) -> None:
    identity = _legacy_intent_identity(snapshot)
    if identity is None:
        return
    tag_claim = db.get(AgentReleaseTagClaimModel, (snapshot.agent_id, str(identity["tag_name"])))
    if tag_claim and (tag_claim.change_set_id, tag_claim.release_id) == (snapshot.change_set_id, identity["release_id"]):
        db.delete(tag_claim)
    source_id = identity.get("source_improvement_id")
    if source_id:
        source_claim = db.get(AgentReleaseSourceClaimModel, (snapshot.agent_id, str(source_id)))
        if source_claim and (source_claim.change_set_id, source_claim.release_id) == (snapshot.change_set_id, identity["release_id"]):
            db.delete(source_claim)


def _row_payload(row: AgentChangeSetModel) -> JsonObject:
    return {
        **dict(row.payload_json or {}),
        "change_set_id": row.change_set_id,
        "agent_id": row.agent_id,
        "status": row.status,
        "updated_at": row.updated_at,
        "base_commit_sha": row.base_commit_sha,
        "candidate_commit_sha": row.candidate_commit_sha,
    }


def _add_event(
    db: Session,
    change_set_id: str,
    action: str,
    before: JsonObject,
    after: JsonObject,
) -> None:
    db.add(
        AgentChangeSetEventModel(
            event_id=f"age-{uuid.uuid4()}",
            change_set_id=change_set_id,
            action=action,
            operator="runtime-migration",
            created_at=utc_now(),
            before_json=before,
            after_json=after,
        ),
    )
