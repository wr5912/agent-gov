from __future__ import annotations

import pytest
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseTagClaimModel
from app.services.agent_governance import AgentGovernanceError
from sqlalchemy.exc import OperationalError

from agent_governance_publish_test_support import _candidate_change_set, _governance, _trusted_test_run


def test_legacy_publishing_intent_without_complete_test_evidence_is_cancelled_before_git_side_effect(
    tmp_path,
    monkeypatch,
):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    head_before = str(agent_store.current_commit_sha())
    intent = governance._reserve_publication_intent(
        change_set_id,
        operator="legacy-publisher",
        tag_name=None,
        note=None,
        force=False,
    )
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        payload = dict(row.payload_json or {})
        legacy_intent = dict(payload["publication_intent"])
        for field in ("test_run_id", "test_receipt_digest", "test_suite_digest", "test_source_digest"):
            legacy_intent.pop(field, None)
        payload["publication_intent"] = legacy_intent
        row.payload_json = payload

    assert agent_store.publication_side_effects_present(intent.commit_sha, intent.tag_name) is False

    def reject_unexpected_git_publish(*_args, **_kwargs):
        raise AssertionError("legacy intent without trusted test evidence must not reach Git publication")

    monkeypatch.setattr(agent_store, "publish_commit", reject_unexpected_git_publish)
    with pytest.raises(AgentGovernanceError, match="intent 未固化 test run/receipt/suite/source") as exc:
        governance.publish_change_set(change_set_id, operator="retrying-publisher", force=True, note="不得绕过门证")

    assert exc.value.status_code == 409
    persisted = governance.get_change_set(change_set_id)
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert "intent 未固化 test run/receipt/suite/source" in persisted["publication_error"]["detail"]
    assert governance.list_releases() == []
    assert str(agent_store.current_commit_sha()) == head_before
    assert (
        agent_store._git(
            ["rev-parse", "--verify", f"refs/tags/{intent.tag_name}^{{commit}}"],
            cwd=agent_store.repository_dir,
            check=False,
        ).strip()
        == ""
    )
    actions = [event["action"] for event in governance.list_change_set_events(change_set_id)]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 1
    with governance.feedback_store.Session() as db:
        assert db.get(AgentReleaseTagClaimModel, (intent.agent_id, intent.tag_name)) is None


@pytest.mark.parametrize(
    "drift_field",
    ["test_run_id", "test_receipt_digest", "test_suite_digest", "test_source_digest"],
)
def test_publishing_intent_rejects_any_drift_from_current_release_eligible_test_evidence_even_with_force(
    tmp_path,
    monkeypatch,
    drift_field,
):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    current_run = _trusted_test_run(agent_id, commit_sha, test_run_id="atr-frozen")
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: current_run
    head_before = str(agent_store.current_commit_sha())
    intent = governance._reserve_publication_intent(
        change_set_id,
        operator="publisher-before-drift",
        tag_name=None,
        note=None,
        force=False,
    )

    if drift_field == "test_run_id":
        current_run["test_run_id"] = "atr-replaced"
    elif drift_field == "test_receipt_digest":
        current_run["receipt"] = {"receipt_digest": "9" * 64}
    elif drift_field == "test_suite_digest":
        current_run["suite_digest"] = "8" * 64
    else:
        current_run["source_digest"] = "7" * 64

    def reject_unexpected_git_publish(*_args, **_kwargs):
        raise AssertionError("drifted test evidence must be rejected before Git publication")

    monkeypatch.setattr(agent_store, "publish_commit", reject_unexpected_git_publish)
    with pytest.raises(AgentGovernanceError, match="当前 release-eligible 测试门证与 intent 不一致") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="force-retry",
            force=True,
            note="force 不能替换已固化门证",
        )

    assert exc.value.status_code == 409
    persisted = governance.get_change_set(change_set_id)
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert governance.list_releases() == []
    assert str(agent_store.current_commit_sha()) == head_before
    assert agent_store.publication_side_effects_present(intent.commit_sha, intent.tag_name) is False
    actions = [event["action"] for event in governance.list_change_set_events(change_set_id)]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 1


def test_test_evidence_drift_after_git_side_effect_only_reconciles_metadata_from_frozen_intent(
    tmp_path,
    monkeypatch,
):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    current_run = _trusted_test_run(agent_id, commit_sha, test_run_id="atr-frozen-before-git")
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: current_run
    real_add_event = governance._add_event_row

    def fail_published_event(db, target_change_set_id, action, operator, *, before, after):
        if action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected metadata failure"))
        return real_add_event(db, target_change_set_id, action, operator, before=before, after=after)

    monkeypatch.setattr(governance, "_add_event_row", fail_published_event)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="first-publisher")

    pending = governance.get_change_set(change_set_id)
    frozen_intent = dict(pending["publication_intent"])
    assert pending["status"] == "publishing"
    assert agent_store.publication_side_effects_present(commit_sha, str(frozen_intent["tag_name"])) is True
    assert governance.list_releases() == []

    current_run["test_run_id"] = "atr-newer-after-git"
    current_run["receipt"] = {"receipt_digest": "9" * 64}
    current_run["suite_digest"] = "8" * 64
    current_run["source_digest"] = "7" * 64
    monkeypatch.setattr(governance, "_add_event_row", real_add_event)

    release = governance.publish_change_set(change_set_id, operator="metadata-reconciler")

    assert release["release_id"] == frozen_intent["release_id"]
    assert release["commit_sha"] == commit_sha
    assert release["test_run_id"] == frozen_intent["test_run_id"] == "atr-frozen-before-git"
    assert release["test_receipt_digest"] == frozen_intent["test_receipt_digest"] == "1" * 64
    assert release["test_suite_digest"] == frozen_intent["test_suite_digest"] == "2" * 64
    assert release["test_source_digest"] == frozen_intent["test_source_digest"] == "3" * 64
    assert agent_store.publication_side_effects_present(commit_sha, str(frozen_intent["tag_name"])) is True
    assert len(governance.list_releases()) == 1
    persisted = governance.get_change_set(change_set_id)
    assert persisted["status"] == "published"
    assert persisted["publication_error"] is None
    actions = [event["action"] for event in governance.list_change_set_events(change_set_id)]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 0
    assert actions.count("published") == 1
