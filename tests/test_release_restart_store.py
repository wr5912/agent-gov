from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseSourceClaimModel, AgentReleaseTagClaimModel, make_session_factory
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime_gateway.release_activation import RuntimeActivationRestartRequired
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService
from app.services.agent_publication import PublicationIntent, validate_source_claim, validate_tag_claim
from app.services.agent_release_activation_workflow import _handle_activation_failure


def _start(store: RuntimeRunStore, *, digest: str = "b" * 64):
    return store.start_ephemeral_resource(
        cache_key="release-activation:contract",
        business_agent_id="release-agent",
        version_owner_id="release-agent",
        agent_version_id="a" * 40,
        digest=digest,
        source_id="published-release-contract",
        source_kind="release_activation",
        workspace_id=f"published-release-contract--v-{digest}",
    )


def test_awaiting_restart_repeated_claims_preserve_exact_locators_and_reject_rebinding(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    key = _start(store).cache_key
    store.record_ephemeral_agent(key, "runtime-agent")
    store.record_ephemeral_session(key, "undeleted-probe")
    waiting = store.mark_ephemeral_awaiting_restart(key, stage="release_activation", error_type="RuntimeUpstreamError")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: _start(store), range(6)))
    assert all(row.status == "awaiting_restart" and row.runtime_agent_id == "runtime-agent" and row.session_id == "undeleted-probe" for row in results)
    assert all(row.updated_at == waiting.updated_at for row in results)
    with pytest.raises(RuntimeStateConflict, match="another immutable resource"):
        _start(store, digest="c" * 64)
    with pytest.raises(RuntimeStateConflict, match="another Runtime Session"):
        store.clear_ephemeral_session(key, "another-probe")
    with pytest.raises(RuntimeStateConflict, match="not ready"):
        store.mark_release_activation_active(key)
    store.clear_ephemeral_session(key, "undeleted-probe")
    with pytest.raises(RuntimeStateConflict, match="no matching immutable"):
        store.mark_release_activation_bound(key)
    store.bind_agent_version(
        agent_id="release-agent",
        agent_version_id="a" * 40,
        digest="b" * 64,
        runtime_agent_id="runtime-agent",
        source_kind="published",
        source_id="published-release-contract",
    )
    assert store.mark_release_activation_bound(key).status == "ready"
    assert store.mark_release_activation_active(key).status == "active"
    with pytest.raises(RuntimeStateConflict, match="Invalid ephemeral resource transition"):
        store.mark_ephemeral_awaiting_restart(key, stage="release_activation", error_type="RuntimeUpstreamError")


def test_pending_compensation_cannot_be_resurrected_as_restart_or_ready(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    key = _start(store).cache_key
    store.record_ephemeral_agent(key, "cleanup-agent")
    store.record_ephemeral_session(key, "cleanup-session")
    store.mark_ephemeral_cleanup_pending(key, stage="release_activation", error_type="DeletionFailed")
    with pytest.raises(RuntimeStateConflict, match="Invalid ephemeral resource transition"):
        store.mark_ephemeral_awaiting_restart(key, stage="release_activation", error_type="RuntimeUpstreamError")
    with pytest.raises(RuntimeStateConflict, match="Invalid ephemeral resource transition"):
        store.mark_ephemeral_ready(key)
    durable = _start(store)
    assert (durable.status, durable.runtime_agent_id, durable.session_id) == ("cleanup_pending", "cleanup-agent", "cleanup-session")
    store.clear_ephemeral_session(key, "cleanup-session")
    assert store.complete_ephemeral_resource(key).status == "cleanup_complete"
    with pytest.raises(RuntimeStateConflict, match="already complete"):
        store.mark_ephemeral_awaiting_restart(key, stage="release_activation", error_type="RuntimeUpstreamError")
    assert _start(store).status == "provisioning"


def _publication(tmp_path: Path) -> tuple[AgentGovernanceService, PublicationIntent]:
    feedback = FeedbackStore(data_dir=tmp_path / "data")
    versions = GitAgentVersionStore(repository_dir=tmp_path / "repository", worktrees_dir=tmp_path / "worktrees", releases_dir=tmp_path / "releases")
    service = AgentGovernanceService(feedback_store=feedback, agent_version_store=versions)
    intent = PublicationIntent(
        release_id="release-contract",
        change_set_id="change-contract",
        agent_id="release-agent",
        commit_sha="a" * 40,
        diff_digest="b" * 64,
        test_run_id="test-contract",
        suite_digest="c" * 64,
        tag_name="release-contract",
        operator="contract",
        note=None,
        force=False,
        force_publication_blocker=None,
        previous_status="approved",
        started_at="2026-09-13T00:00:00Z",
        previous_commit_sha="d" * 40,
        source_improvement_id="improvement-contract",
        source_improvement_updated_at="2026-09-13T00:00:00Z",
    )
    with feedback.Session.begin() as db:
        db.add(
            AgentChangeSetModel(
                change_set_id=intent.change_set_id,
                agent_id=intent.agent_id,
                status="publishing",
                base_commit_sha=intent.previous_commit_sha,
                candidate_commit_sha=intent.commit_sha,
                branch_name="candidate",
                worktree_path=str(tmp_path / "candidate"),
                payload_json={"publication_intent": intent.to_payload()},
            )
        )
        db.add(AgentReleaseTagClaimModel(agent_id=intent.agent_id, tag_name=intent.tag_name, change_set_id=intent.change_set_id, release_id=intent.release_id))
        db.add(
            AgentReleaseSourceClaimModel(
                agent_id=intent.agent_id,
                source_improvement_id=intent.source_improvement_id,
                change_set_id=intent.change_set_id,
                release_id=intent.release_id,
            )
        )
    return service, intent


def test_restart_failure_retains_publication_intent_tag_and_source_claims(tmp_path: Path) -> None:
    """实际 SQLite 失败投影不能取消已审批候选的发布预留。"""
    service, intent = _publication(tmp_path)
    for _ in range(2):
        with pytest.raises(AgentGovernanceError, match="maintenance restart") as caught:
            asyncio.run(
                _handle_activation_failure(
                    service,
                    service.agent_version_store,
                    intent,
                    None,
                    RuntimeActivationRestartRequired("Runtime maintenance restart is required"),
                )
            )
        assert caught.value.status_code == 409
        with service.feedback_store.Session() as db:
            row = db.get(AgentChangeSetModel, intent.change_set_id)
            assert row is not None and row.status == "publishing"
            assert row.payload_json["publication_intent"] == intent.to_payload()
            assert "maintenance restart" in row.payload_json["publication_error"]["detail"]
            validate_tag_claim(db, intent)
            validate_source_claim(db, intent)
