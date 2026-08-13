from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError
from app.runtime.errors import ConflictError
from app.runtime.improvement_db import AttributionModel, ExecutionRecordModel, ImprovementItemModel, OptimizationPlanModel
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetCreateRequest,
    AgentChangeSetPublishRequest,
)
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    AgentReleaseModel,
    AgentReleaseSourceClaimModel,
    AgentReleaseTagClaimModel,
    utc_now,
)
from app.services.agent_governance import AgentGovernanceError
from sqlalchemy.exc import OperationalError

from agent_governance_publish_test_support import (
    _assert_improvement_release_completed,
    _bind_candidate_to_source_claim,
    _candidate_change_set,
    _feedback_candidate_change_set,
    _governance,
    _trusted_test_run,
)


def test_stable_change_set_intent_is_idempotent_and_candidate_can_advance_before_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    stable_id = "agc-11111111-2222-3333-4444-555555555555"
    base = str(agent_store.current_commit_sha())
    first = governance.create_change_set(
        change_set_id=stable_id,
        base_commit_sha=base,
        execution_job_id="exec-stable",
        title="stable execution intent",
    )
    repeated = governance.create_change_set(
        change_set_id=stable_id,
        base_commit_sha=base,
        execution_job_id="exec-stable",
        title="stable execution intent",
    )
    assert repeated["change_set_id"] == first["change_set_id"]
    assert [event["action"] for event in governance.list_change_set_events(stable_id)] == ["created"]

    worktree = Path(str(first["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text("first candidate\n", encoding="utf-8")
    first_candidate = agent_store.commit_worktree(worktree, message="first candidate")
    governance.mark_candidate_committed(stable_id, candidate_commit_sha=first_candidate, execution_job_id="exec-stable")
    worktree.joinpath("CLAUDE.md").write_text("stale second candidate\n", encoding="utf-8")
    stale_candidate = agent_store.commit_worktree(worktree, message="stale candidate")

    updated = governance.mark_candidate_committed(
        stable_id,
        candidate_commit_sha=stale_candidate,
        execution_job_id="exec-stable",
    )
    assert updated["candidate_commit_sha"] == stale_candidate
    assert updated["candidate_commit_sha"] != first_candidate
    with pytest.raises(AgentGovernanceError, match="different execution"):
        governance.create_change_set(
            change_set_id=stable_id,
            base_commit_sha=base,
            execution_job_id="exec-other",
        )


def test_publish_cleans_candidate_worktree_and_retry_remains_idempotent(tmp_path):
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    change_set_id = str(candidate["change_set_id"])
    worktree = Path(str(candidate["worktree_path"]))
    branch = str(candidate["branch_name"])
    assert worktree.exists()

    release = governance.publish_change_set(change_set_id, operator="tester")
    repeated = governance.publish_change_set(change_set_id, operator="tester")

    assert repeated["release_id"] == release["release_id"]
    assert not worktree.exists()
    assert not agent_store._git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=agent_store.repository_dir, check=False).strip()


def test_abandon_cleans_worktree_but_keeps_unpublished_candidate_branch_for_audit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    change_set_id = str(candidate["change_set_id"])
    worktree = Path(str(candidate["worktree_path"]))
    branch_ref = f"refs/heads/{candidate['branch_name']}"

    abandoned = governance.abandon_change_set(change_set_id, operator="tester")
    repeated = governance.abandon_change_set(change_set_id, operator="tester")

    assert abandoned["status"] == "abandoned" and repeated["status"] == "abandoned"
    assert not worktree.exists()
    assert agent_store._git(["show-ref", "--verify", branch_ref], cwd=agent_store.repository_dir, check=False).strip()
    assert [event["action"] for event in governance.list_change_set_events(change_set_id)].count("abandoned") == 1
    with pytest.raises(AgentGovernanceError, match="cannot be published from status abandoned"):
        governance.publish_change_set(change_set_id)


def test_manual_change_set_has_no_fabricated_improvement_attribution(tmp_path):
    governance, _agent_store = _governance(tmp_path)

    change_set = governance.create_change_set(title="手工候选", operator="tester")

    assert change_set.get("source_improvement_id") is None
    assert change_set.get("source_attribution_id") is None
    assert change_set.get("source_attribution_status") is None
    with pytest.raises(ValueError, match="source_improvement_id"):
        AgentChangeSetCreateRequest.model_validate({"title": "伪造来源", "source_improvement_id": "imp-hostile", "source_attribution_status": "confirmed"})


def test_change_set_and_release_carry_agent_id_and_filter(tmp_path):
    """B3.1（AGV-017 版本维度基础）：change set/release 带默认业务 Agent ID 且可按 Agent 过滤。"""
    governance, agent_store = _governance(tmp_path)
    candidate = _candidate_change_set(governance, agent_store)
    assert candidate["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    assert all(cs["agent_id"] == DEFAULT_BUSINESS_AGENT_ID for cs in governance.list_change_sets())
    # 按 Agent 维度过滤 change set：默认 Agent 命中、其他 Agent 为空（不串扰）。
    assert governance.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID)
    assert governance.list_change_sets(agent_id="biz-other") == []
    # 发布后 release 同样带 agent_id 且可按 Agent 过滤。
    published = governance.publish_change_set(str(candidate["change_set_id"]), operator="tester")
    assert published is not None
    assert all(rel["agent_id"] == DEFAULT_BUSINESS_AGENT_ID for rel in governance.list_releases())
    assert governance.list_releases(agent_id=DEFAULT_BUSINESS_AGENT_ID)
    assert governance.list_releases(agent_id="biz-other") == []


def test_candidate_committed_change_set_can_publish_directly(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert release["commit_sha"] == change_set["candidate_commit_sha"]
    assert release["status"] == "published"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "published"


def test_publish_requires_passed_platform_test_for_exact_candidate_commit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert projected is not None
    assert projected["latest_test_run"] is None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: _trusted_test_run(
        agent_id,
        "0" * 40,
        test_run_id="atr-wrong",
    )
    assert governance.get_change_set(str(change_set["change_set_id"]))["latest_test_run"] is None

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: _trusted_test_run(
        agent_id,
        commit_sha,
        test_run_id="atr-exact",
    )
    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
    assert release["commit_sha"] == commit_sha


def test_force_publish_cannot_bypass_platform_test_and_requires_reason_for_audited_force(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配") as exc:
        governance.publish_change_set(change_set_id, operator="tester", force=True)
    assert exc.value.status_code == 409
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: _trusted_test_run(
        agent_id,
        commit_sha,
        test_run_id="atr-trusted",
    )
    with pytest.raises(AgentGovernanceError, match="explicit reason") as exc:
        governance.publish_change_set(change_set_id, operator="tester", force=True)
    assert exc.value.status_code == 422

    reason = "平台测试回执已满足，值班负责人要求保留强制发布审计"
    release = governance.publish_change_set(
        change_set_id,
        operator="tester",
        note=reason,
        force=True,
    )
    assert release["force_published"] is True
    assert release["operator"] == "tester"
    assert release["force_publish_reason"] == reason
    assert release["force_publication_blocker"] is None
    events = governance.list_change_set_events(change_set_id)
    assert [event for event in events if event["action"] == "force_published"]

    assert AgentChangeSetPublishRequest(force=True, force_reason=reason).force_reason == reason
    with pytest.raises(ValueError, match="force_reason"):
        AgentChangeSetPublishRequest(force=True)


def test_feedback_publication_cannot_force_bypass_complete_agent_test_suite(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set, _bound_at = _feedback_candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    agent_id = str(change_set["agent_id"])
    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: None

    projected = governance.get_change_set(change_set_id)
    assert projected is not None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="tester",
            note="请求跳过失败测试",
            force=True,
        )
    assert exc.value.status_code == 409
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"

    governance.latest_passed_test_run = lambda _agent_id, _commit_sha: _trusted_test_run(
        agent_id,
        commit_sha,
        test_run_id="atr-feedback-exact",
    )
    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["commit_sha"] == commit_sha
    assert release["force_published"] is False


def test_publish_retries_after_archive_failure_without_duplicate_release(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    real_archive_ref = agent_store.archive_ref

    def fail_archive(_ref: str):
        raise AgentGitError("injected archive failure")

    monkeypatch.setattr(agent_store, "archive_ref", fail_archive)
    with pytest.raises(AgentGovernanceError, match="injected archive failure"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert pending["publication_error"]["detail"] == "injected archive failure"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.list_releases() == []
    intent = pending["publication_intent"]
    assert (
        agent_store._git(
            ["rev-parse", "--verify", f"refs/tags/{intent['tag_name']}^{{commit}}"],
            cwd=agent_store.repository_dir,
        ).strip()
        == change_set["candidate_commit_sha"]
    )

    monkeypatch.setattr(agent_store, "archive_ref", real_archive_ref)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")

    assert release["release_id"] == intent["release_id"]
    assert Path(str(release["archive_path"])).is_file()
    assert len(governance.list_releases()) == 1
    assert governance.get_change_set(change_set_id)["status"] == "published"


def test_publish_db_finalize_failure_rolls_back_metadata_and_retry_reconciles(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    real_add_event = governance._add_event_row

    def fail_published_event(db, target_change_set_id, action, operator, *, before, after):
        if action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected DB failure"))
        return real_add_event(
            db,
            target_change_set_id,
            action,
            operator,
            before=before,
            after=after,
        )

    monkeypatch.setattr(governance, "_add_event_row", fail_published_event)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert governance.list_releases() == []
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]

    monkeypatch.setattr(governance, "_add_event_row", real_add_event)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    events = governance.list_change_set_events(change_set_id)

    assert release["release_id"] == pending["publication_intent"]["release_id"]
    assert len(governance.list_releases()) == 1
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_publish_finishes_metadata_without_overwriting_newer_source_after_git_side_effect(tmp_path, monkeypatch):
    import app.services.agent_publication_finalization as finalization_module

    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    def source_changed(*_args, **_kwargs):
        raise ConflictError("Source improvement changed during publication finalization")

    monkeypatch.setattr(finalization_module, "finalize_intent_source", source_changed)
    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert release["status"] == "published"
    assert projected["status"] == "published"
    assert release["source_finalization_conflict"]["detail"] == ("Source improvement changed during publication finalization")
    assert projected["source_finalization_conflict"] == release["source_finalization_conflict"]
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]


def test_source_claim_blocks_second_publication_before_git_side_effect(tmp_path, monkeypatch):
    import app.services.agent_publication_finalization as finalization_module

    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# first source publication\n")
    source_improvement_id = "imp-source-claim"
    bound_at = utc_now()
    _bind_candidate_to_source_claim(
        governance,
        first,
        source_improvement_id=source_improvement_id,
        bound_at=bound_at,
    )

    def source_changed(*_args, **_kwargs):
        raise ConflictError("Source improvement changed during publication finalization")

    monkeypatch.setattr(finalization_module, "finalize_intent_source", source_changed)
    first_release = governance.publish_change_set(str(first["change_set_id"]), operator="tester")
    published_head = agent_store.current_commit_sha()
    assert first_release["source_improvement_id"] == source_improvement_id

    second = _candidate_change_set(governance, agent_store, content="# second source publication\n")
    with governance.feedback_store.Session.begin() as db:
        first_row = db.get(AgentChangeSetModel, str(first["change_set_id"]))
        second_row = db.get(AgentChangeSetModel, str(second["change_set_id"]))
        execution = db.query(ExecutionRecordModel).filter_by(improvement_id=source_improvement_id).one()
        assert first_row is not None and second_row is not None
        second_payload = dict(second_row.payload_json or {})
        second_payload.update(
            {
                "source_improvement_id": source_improvement_id,
                "source_attribution_id": (first_row.payload_json or {})["source_attribution_id"],
            }
        )
        second_row.payload_json = second_payload
        execution.change_set_id = str(second["change_set_id"])
        execution.applied_agent_version_id = str(second["candidate_commit_sha"])

    with pytest.raises(AgentGovernanceError, match="持有发布预留，不能重复发布"):
        governance.publish_change_set(str(second["change_set_id"]), operator="tester")

    assert agent_store.current_commit_sha() == published_head
    assert governance.get_change_set(str(second["change_set_id"]))["status"] == "candidate_committed"
    with governance.feedback_store.Session() as db:
        claim = db.get(AgentReleaseSourceClaimModel, (DEFAULT_BUSINESS_AGENT_ID, source_improvement_id))
        assert claim is not None and claim.change_set_id == first["change_set_id"]


def test_improvement_publication_rejects_unconfirmed_or_revised_provenance_even_with_force(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set, bound_at = _feedback_candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    candidate = str(change_set["candidate_commit_sha"])

    with governance.feedback_store.Session.begin() as db:
        db.get(ExecutionRecordModel, "exec-publish").status = "draft"

    projected = governance.get_change_set(change_set_id)
    assert projected["publication_provenance_blocker"] == "改进执行尚未确认或执行来源不完整，请先确认执行结果"
    assert projected["publication_blocker"] == projected["publication_provenance_blocker"]
    with pytest.raises(ConflictError, match="执行尚未确认"):
        governance.publish_change_set(change_set_id, operator="tester")

    with governance.feedback_store.Session.begin() as db:
        db.get(ExecutionRecordModel, "exec-publish").status = "confirmed"
        attribution = db.get(AttributionModel, "attr-publish")
        attribution.status = "draft"
        attribution.updated_at = "2026-07-10T00:01:00+00:00"

    assert governance.get_change_set(change_set_id)["source_attribution_status"] == "draft"
    for force in (False, True):
        with pytest.raises(ConflictError, match="归因未确认"):
            governance.publish_change_set(change_set_id, operator="tester", force=force)
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"
    assert agent_store.current_commit_sha() != candidate

    with governance.feedback_store.Session.begin() as db:
        attribution = db.get(AttributionModel, "attr-publish")
        execution = db.get(ExecutionRecordModel, "exec-publish")
        plan = db.get(OptimizationPlanModel, "opt-publish")
        attribution.status = "confirmed"
        execution.source_attribution_updated_at = attribution.updated_at
        plan.status = "draft"
        plan.updated_at = "2026-07-10T00:02:00+00:00"

    with pytest.raises(ConflictError, match="优化方案未确认"):
        governance.publish_change_set(
            change_set_id,
            operator="tester",
            note="来源链已人工核验",
            force=True,
        )

    with governance.feedback_store.Session.begin() as db:
        execution = db.get(ExecutionRecordModel, "exec-publish")
        plan = db.get(OptimizationPlanModel, "opt-publish")
        plan.status = "confirmed"
        execution.source_optimization_plan_updated_at = plan.updated_at

    real_add_event = governance._add_event_row

    def fail_published_event(db, target_change_set_id, action, operator, *, before, after):
        if action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected source finalize failure"))
        return real_add_event(db, target_change_set_id, action, operator, before=before, after=after)

    monkeypatch.setattr(governance, "_add_event_row", fail_published_event)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="tester")

    with governance.feedback_store.Session() as db:
        rolled_back_item = db.get(ImprovementItemModel, "imp-publish")
        assert (rolled_back_item.improvement_stage, rolled_back_item.improvement_status, rolled_back_item.updated_at) == (
            "regression",
            "active",
            bound_at,
        )
    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert pending["publication_intent"]["source_improvement_updated_at"] == bound_at

    monkeypatch.setattr(governance, "_add_event_row", real_add_event)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    _assert_improvement_release_completed(governance, release)


def test_publish_retry_finalizes_older_tag_after_newer_release_advances_head(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv1\n")
    first_id = str(first["change_set_id"])
    real_add_event = governance._add_event_row

    def fail_first_finalize(db, change_set_id, action, operator, *, before, after):
        if change_set_id == first_id and action == "published":
            raise OperationalError("INSERT agent_change_set_events", {}, RuntimeError("injected DB failure"))
        return real_add_event(db, change_set_id, action, operator, before=before, after=after)

    monkeypatch.setattr(governance, "_add_event_row", fail_first_finalize)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(first_id, operator="tester")
    monkeypatch.setattr(governance, "_add_event_row", real_add_event)

    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv2\n")
    second_release = governance.publish_change_set(str(second["change_set_id"]), operator="tester")
    first_release = governance.publish_change_set(first_id, operator="reconciler")

    assert first_release["commit_sha"] == first["candidate_commit_sha"]
    assert agent_store.current_commit_sha() == second_release["commit_sha"]
    assert governance.get_change_set(first_id)["status"] == "published"
    assert len(governance.list_releases()) == 2


def test_divergent_candidate_publish_failure_cancels_intent_and_tag_claim(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-a\n")
    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-b\n")
    governance.publish_change_set(str(first["change_set_id"]), tag_name="release-branch-a")

    with pytest.raises(AgentGovernanceError, match="intent was cancelled before side effects"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name="release-branch-b")

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert persisted["publication_error"]["detail"]
    actions = [event["action"] for event in governance.list_change_set_events(str(second["change_set_id"]))]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 1
    with governance.feedback_store.Session() as db:
        assert db.get(AgentReleaseTagClaimModel, (DEFAULT_BUSINESS_AGENT_ID, "release-branch-b")) is None


def test_repeated_publish_returns_same_release_and_rejects_conflicting_tag(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    first = governance.publish_change_set(change_set_id, operator="tester")
    repeated = governance.publish_change_set(
        change_set_id,
        operator="retrying-operator",
        tag_name=str(first["tag_name"]),
    )

    assert repeated["release_id"] == first["release_id"]
    assert len(governance.list_releases()) == 1
    actions = [event["action"] for event in governance.list_change_set_events(change_set_id)]
    assert actions.count("publication_started") == 1
    assert actions.count("published") == 1
    with pytest.raises(AgentGovernanceError, match="already published with a different tag"):
        governance.publish_change_set(change_set_id, tag_name="agent-release-conflict")


def test_release_tag_is_owned_by_one_change_set_per_agent(tmp_path):
    governance, agent_store = _governance(tmp_path)
    shared_tag = "agent-release-shared-candidate"
    first = _candidate_change_set(governance, agent_store)
    first_release = governance.publish_change_set(
        str(first["change_set_id"]),
        operator="tester",
        tag_name=shared_tag,
    )
    second = governance.create_change_set(
        base_commit_sha=str(first["candidate_commit_sha"]),
        title="same candidate, different change set",
        operator="tester",
    )
    second = governance.mark_candidate_committed(
        str(second["change_set_id"]),
        candidate_commit_sha=str(first["candidate_commit_sha"]),
        execution_job_id="job-same-candidate",
        operator="tester",
    )

    with pytest.raises(AgentGovernanceError, match="already assigned to another release"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name=shared_tag)

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert "publication_started" not in {str(event["action"]) for event in governance.list_change_set_events(str(second["change_set_id"]))}
    business = _candidate_change_set(
        governance,
        agent_store,
        content="# Business Agent\n\nsame tag, isolated repository\n",
        agent_id="biz-shared-tag",
    )
    business_release = governance.publish_change_set(
        str(business["change_set_id"]),
        tag_name=shared_tag,
    )
    assert first_release["tag_name"] == business_release["tag_name"] == shared_tag
    assert first_release["agent_id"] != business_release["agent_id"]


def test_concurrent_publish_reserves_one_intent_and_one_audit_event(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    real_publish_commit = agent_store.publish_commit

    def synchronized_publish(commit_sha: str, *, tag_name: str, message: str, validate_ref=None):
        publish_entered.set()
        assert allow_publish.wait(timeout=10)
        return real_publish_commit(commit_sha, tag_name=tag_name, message=message, validate_ref=validate_ref)

    monkeypatch.setattr(agent_store, "publish_commit", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(governance.publish_change_set, change_set_id, operator="publisher-0")
        assert publish_entered.wait(timeout=10)
        second = executor.submit(governance.publish_change_set, change_set_id, operator="publisher-1")
        with pytest.raises(AgentGovernanceError, match="maintenance"):
            second.result(timeout=10)
        allow_publish.set()
        release = first.result(timeout=30)

    assert release["release_id"]
    assert len(governance.list_releases()) == 1
    events = governance.list_change_set_events(change_set_id)
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_concurrent_publish_with_different_tags_is_fenced_before_db_reservation(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    publish_entered = threading.Event()
    allow_publish = threading.Event()
    real_publish_commit = agent_store.publish_commit

    def synchronized_publish(commit_sha: str, *, tag_name: str, message: str, validate_ref=None):
        publish_entered.set()
        assert allow_publish.wait(timeout=10)
        return real_publish_commit(commit_sha, tag_name=tag_name, message=message, validate_ref=validate_ref)

    monkeypatch.setattr(agent_store, "publish_commit", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            governance.publish_change_set,
            change_set_id,
            operator="publisher-0",
            tag_name="agent-release-competing-0",
        )
        assert publish_entered.wait(timeout=10)
        second = executor.submit(
            governance.publish_change_set,
            change_set_id,
            operator="publisher-1",
            tag_name="agent-release-competing-1",
        )
        with pytest.raises(AgentGovernanceError, match="maintenance") as exc:
            second.result(timeout=10)
        allow_publish.set()
        release = first.result(timeout=30)

    assert exc.value.status_code == 409
    assert release["tag_name"] == "agent-release-competing-0"
    assert len(governance.list_releases()) == 1
    events = governance.list_change_set_events(change_set_id)
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_invalid_explicit_tag_is_rejected_before_intent_is_reserved(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    with pytest.raises(AgentGovernanceError, match="Invalid release tag name"):
        governance.publish_change_set(change_set_id, tag_name="--hostile-option")

    persisted = governance.get_change_set(change_set_id)
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted


def test_publish_reconciles_legacy_release_row_without_duplicate(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    tag_name = "agent-release-legacy-partial"
    git_result = agent_store.publish_commit(
        str(change_set["candidate_commit_sha"]),
        tag_name=tag_name,
        message="legacy partial publication",
    )
    archive = git_result["archive"]
    now = utc_now()
    legacy_release_id = "agr-legacy-partial"
    legacy_payload = {
        "schema_version": "agent-release/v1",
        "release_id": legacy_release_id,
        "agent_id": DEFAULT_BUSINESS_AGENT_ID,
        "created_at": now,
        "updated_at": now,
        "status": "published",
        "tag_name": tag_name,
        "commit_sha": change_set["candidate_commit_sha"],
        "change_set_id": change_set_id,
        "archive_path": archive["archive_path"],
        "archive_sha256": archive["sha256"],
    }
    with governance.feedback_store.Session.begin() as db:
        db.add(
            AgentReleaseModel(
                release_id=legacy_release_id,
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
                created_at=now,
                updated_at=now,
                status="published",
                tag_name=tag_name,
                commit_sha=str(change_set["candidate_commit_sha"]),
                change_set_id=change_set_id,
                archive_path=str(archive["archive_path"]),
                payload_json=legacy_payload,
            )
        )

    release = governance.publish_change_set(change_set_id, operator="reconciler")

    assert release["release_id"] == legacy_release_id
    assert len(governance.list_releases()) == 1
    assert governance.get_change_set(change_set_id)["latest_release_id"] == legacy_release_id
