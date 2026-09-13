from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentReleaseSourceClaimModel,
    AgentReleaseTagClaimModel,
)
from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.runtime.state_machines import validate_transition
from app.services.agent_publication_provenance import (
    PublicationSourceRevision,
    finalize_source_improvement,
    validate_publication_provenance,
)


class PublicationReservationLost(RuntimeError):
    """Another caller changed the publication state before this reservation committed."""


class PublicationFinalizationLost(RuntimeError):
    """Another caller changed the publication state before finalization committed."""


class PublicationTagConflict(ValueError):
    """A release tag is already owned by another change set in the same Agent repository."""


class PublicationSourceConflict(ValueError):
    """A source improvement is already owned by another publication intent."""


def _is_lower_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(character in "0123456789abcdef" for character in value)


_PUBLICATION_INTENT_FIELDS = frozenset(
    {
        "release_id",
        "change_set_id",
        "agent_id",
        "commit_sha",
        "diff_digest",
        "test_run_id",
        "suite_digest",
        "tag_name",
        "operator",
        "note",
        "force",
        "force_publication_blocker",
        "previous_status",
        "started_at",
        "previous_commit_sha",
        "source_improvement_id",
        "source_improvement_updated_at",
    }
)
_PUBLICATION_INTENT_REQUIRED_STRINGS = (
    "release_id",
    "change_set_id",
    "agent_id",
    "commit_sha",
    "diff_digest",
    "tag_name",
    "operator",
    "previous_status",
    "started_at",
)
_PUBLICATION_INTENT_OPTIONAL_STRINGS = (
    "test_run_id",
    "suite_digest",
    "note",
    "force_publication_blocker",
    "previous_commit_sha",
    "source_improvement_id",
    "source_improvement_updated_at",
)


@dataclass(frozen=True)
class PublicationIntent:
    release_id: str
    change_set_id: str
    agent_id: str
    commit_sha: str
    diff_digest: str
    test_run_id: str | None
    suite_digest: str | None
    tag_name: str
    operator: str
    note: str | None
    force: bool
    force_publication_blocker: str | None
    previous_status: str
    started_at: str
    previous_commit_sha: str | None = None
    source_improvement_id: str | None = None
    source_improvement_updated_at: str | None = None

    def to_payload(self) -> JsonObject:
        return {
            "release_id": self.release_id,
            "change_set_id": self.change_set_id,
            "agent_id": self.agent_id,
            "commit_sha": self.commit_sha,
            "diff_digest": self.diff_digest,
            "test_run_id": self.test_run_id,
            "suite_digest": self.suite_digest,
            "tag_name": self.tag_name,
            "operator": self.operator,
            "note": self.note,
            "force": self.force,
            "force_publication_blocker": self.force_publication_blocker,
            "previous_status": self.previous_status,
            "started_at": self.started_at,
            "previous_commit_sha": self.previous_commit_sha,
            "source_improvement_id": self.source_improvement_id,
            "source_improvement_updated_at": self.source_improvement_updated_at,
        }

    @classmethod
    def from_payload(cls, value: object) -> PublicationIntent:
        record = _publication_intent_record(value)
        force = _publication_intent_force(record)
        _validate_publication_intent_record(record, force)
        return cls(
            release_id=str(record["release_id"]),
            change_set_id=str(record["change_set_id"]),
            agent_id=str(record["agent_id"]),
            commit_sha=str(record["commit_sha"]),
            diff_digest=str(record["diff_digest"]),
            test_run_id=cast(str | None, record["test_run_id"]),
            suite_digest=cast(str | None, record["suite_digest"]),
            tag_name=str(record["tag_name"]),
            operator=str(record["operator"]),
            note=cast(str | None, record["note"]),
            force=force,
            force_publication_blocker=cast(str | None, record["force_publication_blocker"]),
            previous_status=str(record["previous_status"]),
            started_at=str(record["started_at"]),
            previous_commit_sha=cast(str | None, record["previous_commit_sha"]),
            source_improvement_id=cast(str | None, record["source_improvement_id"]),
            source_improvement_updated_at=cast(str | None, record["source_improvement_updated_at"]),
        )


def _publication_intent_record(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("publication intent must be an object")
    if set(value) != _PUBLICATION_INTENT_FIELDS:
        raise ValueError("publication intent schema is invalid")
    return cast(JsonObject, value)


def _publication_intent_force(record: JsonObject) -> bool:
    value = record["force"]
    if type(value) is not bool:
        raise ValueError("publication intent force flag is invalid")
    return value


def _validate_publication_intent_record(record: JsonObject, force: bool) -> None:
    if any(not isinstance(record.get(field), str) or not record[field] for field in _PUBLICATION_INTENT_REQUIRED_STRINGS):
        raise ValueError("publication intent is incomplete")
    if any(record[field] is not None and not isinstance(record[field], str) for field in _PUBLICATION_INTENT_OPTIONAL_STRINGS):
        raise ValueError("publication intent optional field type is invalid")
    if record["previous_status"] not in {"candidate_committed", "approved"}:
        raise ValueError("publication intent previous status is invalid")
    test_run_id = record["test_run_id"]
    suite_digest = record["suite_digest"]
    if (test_run_id is None) != (suite_digest is None) or force == (test_run_id is not None):
        raise ValueError("publication intent test evidence is incomplete")
    if not _is_lower_hex(record["commit_sha"], 40) or not _is_lower_hex(record["diff_digest"], 64):
        raise ValueError("publication intent commit or diff identity is invalid")
    if suite_digest is not None and not _is_lower_hex(suite_digest, 64):
        raise ValueError("publication intent suite digest is invalid")
    previous_commit = record["previous_commit_sha"]
    if previous_commit is not None and not _is_lower_hex(previous_commit, 40):
        raise ValueError("publication intent previous commit is invalid")
    if test_run_id is not None and not test_run_id:
        raise ValueError("publication intent test run id is invalid")
    source_id = record["source_improvement_id"]
    source_updated_at = record["source_improvement_updated_at"]
    if (source_id is None) != (source_updated_at is None):
        raise ValueError("publication intent source identity is incomplete")
    if source_id == "" or source_updated_at == "":
        raise ValueError("publication intent source identity is invalid")
    force_blocker = record["force_publication_blocker"]
    if force != (isinstance(force_blocker, str) and bool(force_blocker)):
        raise ValueError("publication intent force evidence is invalid")
    note = record["note"]
    if force and (not isinstance(note, str) or not note.strip()):
        raise ValueError("publication intent force reason is invalid")
    if force and record["previous_status"] != "candidate_committed":
        raise ValueError("force publication intent cannot replace an approved evidence path")


def capture_publication_source(db: Session, change_set_id: str) -> PublicationSourceRevision | None:
    return validate_publication_provenance(db, change_set_id)


def validate_intent_provenance(db: Session, intent: PublicationIntent) -> None:
    validate_publication_provenance(
        db,
        intent.change_set_id,
        expected_improvement_id=intent.source_improvement_id,
        expected_updated_at=intent.source_improvement_updated_at,
        require_revision_match=True,
    )


def finalize_intent_source(db: Session, intent: PublicationIntent, *, completed_at: str) -> None:
    validate_intent_provenance(db, intent)
    finalize_source_improvement(
        db,
        improvement_id=intent.source_improvement_id,
        expected_updated_at=intent.source_improvement_updated_at,
        completed_at=completed_at,
    )


def commit_publication_intent(
    session_factory: sessionmaker,
    *,
    intent: PublicationIntent,
    previous_status: str,
    previous_updated_at: str,
    before: JsonObject,
    after: JsonObject,
    add_event: Callable[..., None],
    validate_candidate_test: Callable[[Session], None],
) -> None:
    try:
        with session_factory.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            validate_candidate_test(db)
            validate_intent_provenance(db, intent)
            _assert_release_tag_available(db, intent)
            _assert_release_source_available(db, intent)
            _ensure_tag_claim(db, intent)
            _ensure_source_claim(db, intent)
            reserved = db.execute(
                update(AgentChangeSetModel)
                .where(
                    AgentChangeSetModel.change_set_id == intent.change_set_id,
                    AgentChangeSetModel.status == previous_status,
                    AgentChangeSetModel.updated_at == previous_updated_at,
                    AgentChangeSetModel.candidate_commit_sha == intent.commit_sha,
                )
                .values(status="publishing", updated_at=after["updated_at"], payload_json=after)
            ).rowcount
            if reserved != 1:
                raise PublicationReservationLost
            add_event(
                db,
                intent.change_set_id,
                "publication_started",
                intent.operator,
                before=before,
                after=after,
            )
    except IntegrityError as exc:
        with session_factory() as db:
            source_claim = _source_claim(db, intent)
            if source_claim is not None and (source_claim.change_set_id, source_claim.release_id) != (
                intent.change_set_id,
                intent.release_id,
            ):
                raise _source_conflict(intent, source_claim.change_set_id) from exc
            claim = db.get(AgentReleaseTagClaimModel, (intent.agent_id, intent.tag_name))
            if claim is not None and (claim.change_set_id, claim.release_id) == (intent.change_set_id, intent.release_id):
                raise PublicationReservationLost from exc
            if claim is not None:
                raise PublicationTagConflict(f"Release tag {intent.tag_name!r} is already owned by change set {claim.change_set_id}") from exc
            claim = db.scalar(
                select(AgentReleaseTagClaimModel).where(
                    (AgentReleaseTagClaimModel.change_set_id == intent.change_set_id) | (AgentReleaseTagClaimModel.release_id == intent.release_id)
                )
            )
            if claim is not None:
                raise PublicationTagConflict(f"Change set {intent.change_set_id} already owns release tag {claim.tag_name!r}") from exc
        raise


def validate_tag_claim(db: Session, intent: PublicationIntent) -> None:
    claim = db.get(AgentReleaseTagClaimModel, (intent.agent_id, intent.tag_name))
    expected = (intent.change_set_id, intent.release_id)
    actual = (claim.change_set_id, claim.release_id) if claim else None
    if actual != expected:
        raise PublicationTagConflict(f"Release tag {intent.tag_name!r} is not owned by this publication intent")


def validate_source_claim(db: Session, intent: PublicationIntent) -> None:
    if not intent.source_improvement_id:
        return
    claim = _source_claim(db, intent)
    expected = (intent.change_set_id, intent.release_id)
    actual = (claim.change_set_id, claim.release_id) if claim else None
    if actual != expected:
        raise PublicationSourceConflict(f"来源改进事项 {intent.source_improvement_id} 的发布预留不属于当前变更集")


def record_publication_error(
    session_factory: sessionmaker,
    *,
    change_set_id: str,
    detail: str,
    updated_at: str,
) -> None:
    with session_factory() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        if not row or row.status != "publishing":
            return
        previous_updated_at = row.updated_at
        payload = {
            **dict(row.payload_json or {}),
            "publication_error": {"detail": detail, "updated_at": updated_at},
        }
    with session_factory.begin() as db:
        db.execute(
            update(AgentChangeSetModel)
            .where(
                AgentChangeSetModel.change_set_id == change_set_id,
                AgentChangeSetModel.status == "publishing",
                AgentChangeSetModel.updated_at == previous_updated_at,
            )
            .values(updated_at=updated_at, payload_json=payload)
        )


def reconcile_publication_failure(
    session_factory: sessionmaker,
    store: GitAgentVersionStore,
    *,
    intent: PublicationIntent,
    detail: str,
    updated_at: str,
    add_event: Callable[..., None],
) -> bool:
    try:
        side_effects_present = store.publication_side_effects_present(
            intent.commit_sha,
            intent.tag_name,
            previous_commit_sha=intent.previous_commit_sha,
        )
    except AgentGitError:
        side_effects_present = True
    if side_effects_present:
        record_publication_error(
            session_factory,
            change_set_id=intent.change_set_id,
            detail=detail,
            updated_at=updated_at,
        )
        return False
    return _cancel_publication_intent(
        session_factory,
        intent=intent,
        detail=detail,
        updated_at=updated_at,
        add_event=add_event,
    )


def _cancel_publication_intent(
    session_factory: sessionmaker,
    *,
    intent: PublicationIntent,
    detail: str,
    updated_at: str,
    add_event: Callable[..., None],
) -> bool:
    with session_factory() as db:
        row = db.get(AgentChangeSetModel, intent.change_set_id)
        if not row or row.status != "publishing":
            return False
        current_intent = (row.payload_json or {}).get("publication_intent")
        if not isinstance(current_intent, dict) or current_intent.get("release_id") != intent.release_id:
            return False
        validate_transition("agent_change_set", "publishing", intent.previous_status)
        previous_updated_at = row.updated_at
        before = {**dict(row.payload_json or {}), "status": row.status, "updated_at": row.updated_at}
        after = dict(row.payload_json or {})
        after.pop("publication_intent", None)
        after.update(
            {
                "status": intent.previous_status,
                "updated_at": updated_at,
                "publication_error": {"detail": detail, "updated_at": updated_at},
            }
        )
    with session_factory.begin() as db:
        cancelled = db.execute(
            update(AgentChangeSetModel)
            .where(
                AgentChangeSetModel.change_set_id == intent.change_set_id,
                AgentChangeSetModel.status == "publishing",
                AgentChangeSetModel.updated_at == previous_updated_at,
                AgentChangeSetModel.candidate_commit_sha == intent.commit_sha,
            )
            .values(status=intent.previous_status, updated_at=updated_at, payload_json=after)
        ).rowcount
        if cancelled != 1:
            return False
        claim = db.get(AgentReleaseTagClaimModel, (intent.agent_id, intent.tag_name))
        if claim and (claim.change_set_id, claim.release_id) == (intent.change_set_id, intent.release_id):
            db.delete(claim)
        source_claim = _source_claim(db, intent)
        if source_claim and (source_claim.change_set_id, source_claim.release_id) == (intent.change_set_id, intent.release_id):
            db.delete(source_claim)
        add_event(
            db,
            intent.change_set_id,
            "publication_cancelled",
            intent.operator,
            before=before,
            after=after,
        )
    return True


def release_projection_matches_intent(value: JsonObject, intent: PublicationIntent) -> bool:
    expected: JsonObject = {
        "schema_version": "agent-release/v1",
        "release_id": intent.release_id,
        "agent_id": intent.agent_id,
        "created_at": intent.started_at,
        "status": "published",
        "tag_name": intent.tag_name,
        "commit_sha": intent.commit_sha,
        "previous_commit_sha": intent.previous_commit_sha,
        "source_improvement_id": intent.source_improvement_id,
        "change_set_id": intent.change_set_id,
        "rollback_of_release_id": None,
        "note": intent.note,
        "operator": intent.operator,
        "force_published": intent.force,
        "force_publication_blocker": intent.force_publication_blocker if intent.force else None,
        "force_publish_reason": intent.note if intent.force else None,
    }
    return all(value.get(field) == expected_value for field, expected_value in expected.items())


def release_matches_intent(row: AgentReleaseModel, intent: PublicationIntent) -> bool:
    actual = (row.release_id, row.change_set_id, row.agent_id, row.created_at, row.commit_sha, row.tag_name, row.status)
    expected = (
        intent.release_id,
        intent.change_set_id,
        intent.agent_id,
        intent.started_at,
        intent.commit_sha,
        intent.tag_name,
        "published",
    )
    return actual == expected and release_projection_matches_intent(dict(row.payload_json or {}), intent)


def _assert_release_tag_available(db: Session, intent: PublicationIntent) -> None:
    rows = db.scalars(
        select(AgentReleaseModel).where(AgentReleaseModel.agent_id == intent.agent_id, AgentReleaseModel.tag_name == intent.tag_name).limit(2)
    ).all()
    if any(row.change_set_id != intent.change_set_id or row.release_id != intent.release_id for row in rows):
        raise PublicationTagConflict(f"Release tag {intent.tag_name!r} is already assigned to another release in Agent {intent.agent_id}")


def _assert_release_source_available(db: Session, intent: PublicationIntent) -> None:
    if not intent.source_improvement_id:
        return
    releases = db.scalars(select(AgentReleaseModel).where(AgentReleaseModel.agent_id == intent.agent_id)).all()
    for release in releases:
        source_improvement_id = str((release.payload_json or {}).get("source_improvement_id") or "")
        if source_improvement_id != intent.source_improvement_id:
            continue
        if (release.change_set_id, release.release_id) != (intent.change_set_id, intent.release_id):
            raise _source_conflict(intent, str(release.change_set_id or "unknown"))


def _ensure_tag_claim(db: Session, intent: PublicationIntent) -> None:
    claim = db.get(AgentReleaseTagClaimModel, (intent.agent_id, intent.tag_name))
    if claim is None:
        db.add(
            AgentReleaseTagClaimModel(
                agent_id=intent.agent_id,
                tag_name=intent.tag_name,
                change_set_id=intent.change_set_id,
                release_id=intent.release_id,
                created_at=intent.started_at,
            )
        )
        db.flush()
        return
    if (claim.change_set_id, claim.release_id) != (intent.change_set_id, intent.release_id):
        raise PublicationTagConflict(f"Release tag {intent.tag_name!r} is already owned by change set {claim.change_set_id}")


def _ensure_source_claim(db: Session, intent: PublicationIntent) -> None:
    if not intent.source_improvement_id:
        return
    claim = _source_claim(db, intent)
    if claim is None:
        db.add(
            AgentReleaseSourceClaimModel(
                agent_id=intent.agent_id,
                source_improvement_id=intent.source_improvement_id,
                change_set_id=intent.change_set_id,
                release_id=intent.release_id,
                created_at=intent.started_at,
            )
        )
        db.flush()
        return
    if (claim.change_set_id, claim.release_id) != (intent.change_set_id, intent.release_id):
        raise _source_conflict(intent, claim.change_set_id)


def _source_claim(db: Session, intent: PublicationIntent) -> AgentReleaseSourceClaimModel | None:
    if not intent.source_improvement_id:
        return None
    return db.get(AgentReleaseSourceClaimModel, (intent.agent_id, intent.source_improvement_id))


def _source_conflict(intent: PublicationIntent, owner_change_set_id: str) -> PublicationSourceConflict:
    return PublicationSourceConflict(f"来源改进事项 {intent.source_improvement_id} 已由变更集 {owner_change_set_id} 持有发布预留，不能重复发布")


def release_payload(
    intent: PublicationIntent,
    *,
    archive: JsonObject,
    created_at: str,
    updated_at: str,
) -> JsonObject:
    return {
        "schema_version": "agent-release/v1",
        "release_id": intent.release_id,
        "agent_id": intent.agent_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": "published",
        "tag_name": intent.tag_name,
        "commit_sha": intent.commit_sha,
        "previous_commit_sha": intent.previous_commit_sha,
        "source_improvement_id": intent.source_improvement_id,
        "change_set_id": intent.change_set_id,
        "rollback_of_release_id": None,
        "archive_path": archive.get("archive_path"),
        "archive_sha256": archive.get("sha256"),
        "runtime_agent_id": archive.get("runtime_agent_id"),
        "harness_digest": archive.get("harness_digest"),
        "workspace_id": archive.get("workspace_id"),
        "note": intent.note,
        "operator": intent.operator,
        "force_published": intent.force,
        "force_publication_blocker": intent.force_publication_blocker if intent.force else None,
        "force_publish_reason": intent.note if intent.force else None,
    }
