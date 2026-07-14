from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import update

from app.runtime.errors import ConflictError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseModel, utc_now
from app.runtime.state_machines import validate_transition
from app.services.agent_change_set_worktree_lifecycle import (
    ensure_worktree_cleanup_task,
    pending_cleanup_projection,
)
from app.services.agent_publication import (
    PublicationFinalizationLost,
    PublicationIntent,
    PublicationSourceConflict,
    PublicationTagConflict,
    finalize_intent_source,
    release_matches_intent,
    release_payload,
    validate_source_claim,
    validate_tag_claim,
)


class _GovernanceService(Protocol):
    feedback_store: Any

    def _validate_publication_intent(
        self,
        row: AgentChangeSetModel,
        intent: PublicationIntent,
        *,
        requested_tag_name: str | None,
    ) -> None: ...

    def _published_release(
        self,
        change_set: JsonObject,
        *,
        requested_tag_name: str | None,
    ) -> JsonObject: ...

    def _change_set_to_payload(self, row: AgentChangeSetModel) -> JsonObject: ...

    def _add_event_row(self, *args: Any, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class _Finalization:
    previous_updated_at: str
    before: JsonObject
    release: JsonObject
    change_set_projection: JsonObject


def finalize_publication_once(
    service: _GovernanceService,
    intent: PublicationIntent,
    *,
    archive: JsonObject,
) -> JsonObject:
    now = utc_now()
    prepared = _prepare_finalization(service, intent, archive=archive, now=now)
    if isinstance(prepared, dict):
        return prepared
    _commit_finalization(service, intent, prepared=prepared, now=now)
    return prepared.release


def _prepare_finalization(
    service: _GovernanceService,
    intent: PublicationIntent,
    *,
    archive: JsonObject,
    now: str,
) -> _Finalization | JsonObject:
    with service.feedback_store.Session() as db:
        row = db.get(AgentChangeSetModel, intent.change_set_id)
        if row is None:
            raise _error(404, "Agent change set not found")
        service._validate_publication_intent(row, intent, requested_tag_name=intent.tag_name)
        if row.status == "published":
            return service._published_release(
                service._change_set_to_payload(row),
                requested_tag_name=intent.tag_name,
            )
        validate_transition("agent_change_set", row.status, "published")
        before = service._change_set_to_payload(row)
        release = release_payload(
            intent,
            archive=archive,
            created_at=intent.started_at,
            updated_at=now,
        )
        change_set_projection = {
            **dict(row.payload_json or {}),
            "status": "published",
            "updated_at": now,
            "latest_release_id": intent.release_id,
            "latest_release": release,
            "force_published": intent.force,
            "force_publication_blocker": intent.force_publication_blocker,
            "force_publish_note": intent.note if intent.force else None,
            "publication_error": None,
            **pending_cleanup_projection(),
        }
        return _Finalization(row.updated_at, before, release, change_set_projection)


def _commit_finalization(
    service: _GovernanceService,
    intent: PublicationIntent,
    *,
    prepared: _Finalization,
    now: str,
) -> None:
    with service.feedback_store.Session.begin() as db:
        source_conflict: JsonObject | None = None
        try:
            validate_source_claim(db, intent)
            finalize_intent_source(db, intent, completed_at=now)
        except PublicationSourceConflict as exc:
            raise _error(409, str(exc)) from exc
        except ConflictError as exc:
            # Git publish/tag/archive 已是不可逆外部副作用。来源事项 CAS 失败时不能让 intent
            # 永久卡在 publishing，也不能覆盖并发产生的新内容；完成发布元数据并显式保留冲突。
            source_conflict = {
                "detail": str(exc),
                "detected_at": now,
                "expected_improvement_id": intent.source_improvement_id,
                "expected_updated_at": intent.source_improvement_updated_at,
            }
            prepared.release["source_finalization_conflict"] = source_conflict
            prepared.change_set_projection["source_finalization_conflict"] = source_conflict
        finalized = db.execute(
            update(AgentChangeSetModel)
            .where(
                AgentChangeSetModel.change_set_id == intent.change_set_id,
                AgentChangeSetModel.status == "publishing",
                AgentChangeSetModel.updated_at == prepared.previous_updated_at,
                AgentChangeSetModel.candidate_commit_sha == intent.commit_sha,
            )
            .values(
                status="published",
                updated_at=now,
                payload_json=prepared.change_set_projection,
            )
        ).rowcount
        if finalized != 1:
            raise PublicationFinalizationLost
        ensure_worktree_cleanup_task(
            db,
            change_set_id=intent.change_set_id,
            agent_id=intent.agent_id,
            delete_branch=True,
            now=now,
        )
        try:
            validate_tag_claim(db, intent)
        except PublicationTagConflict as exc:
            raise _error(409, str(exc)) from exc
        _upsert_release_row(db, intent, release=prepared.release, now=now)
        service._add_event_row(
            db,
            intent.change_set_id,
            "force_published" if intent.force else "published",
            intent.operator,
            before=prepared.before,
            after=prepared.change_set_projection,
        )
        db.flush()


def _upsert_release_row(
    db: Any,
    intent: PublicationIntent,
    *,
    release: JsonObject,
    now: str,
) -> None:
    release_row = db.get(AgentReleaseModel, intent.release_id)
    if release_row is None:
        db.add(
            AgentReleaseModel(
                release_id=intent.release_id,
                agent_id=intent.agent_id,
                created_at=intent.started_at,
                updated_at=now,
                status="published",
                tag_name=intent.tag_name,
                commit_sha=intent.commit_sha,
                change_set_id=intent.change_set_id,
                archive_path=release.get("archive_path"),
                payload_json=release,
            )
        )
        return
    if not release_matches_intent(release_row, intent):
        raise _error(409, "Existing Agent release metadata conflicts with publication intent")
    release_row.updated_at = now
    release_row.archive_path = release.get("archive_path")
    release_row.payload_json = release


def _error(status_code: int, detail: str) -> Exception:
    from app.services.agent_governance import AgentGovernanceError

    return AgentGovernanceError(status_code, detail)
