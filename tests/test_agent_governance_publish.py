from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from app.agent_testing.runner import FIXED_PYTEST_COMMAND, AgentTestRunner
from app.agent_testing.store import AgentTestingStore
from app.agent_testing.suite import inspect_agent_test_suite
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
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
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_candidate_approval import inspect_candidate_review
from app.services.agent_change_set_provisioner import ChangeSetSource
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService
from app.services.agent_governance_projections import candidate_diff_digest
from app.services.agent_publication_evidence_migration import reconcile_legacy_publication_evidence
from sqlalchemy import text

from agent_release_test_utils import install_release_activation_boundary
from business_agent_test_utils import LEGACY_MAIN_AGENT_ID, ORDINARY_TEST_AGENT_ID, create_test_business_agent_workspace
from feedback_store_test_utils import _run_payload, _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    _write_real_workspace_suite(settings.default_workspace_dir)
    agent_store = GitAgentVersionStore(
        repository_dir=settings.default_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    agent_store.ensure_bootstrap()
    store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.default_workspace_dir)
    governance = AgentGovernanceService(
        feedback_store=store,
        agent_version_store=agent_store,
        runtime_mode=settings.runtime_volume_mode,
        runtime_env={"SEC_OPS_MCP_URL": "http://localhost:58001/mcp"},
    )
    testing_store = AgentTestingStore(store.Session)

    governance.latest_passed_test_run = testing_store.latest_passed_for_commit
    governance.latest_candidate_test_run = testing_store.latest_for_candidate
    governance.test_run_by_id = testing_store.get_run
    install_release_activation_boundary(governance)
    store.agent_version_provider = governance.current_agent_version_id
    return governance, governance._store_for(DEFAULT_BUSINESS_AGENT_ID)


def _write_real_workspace_suite(workspace: Path) -> None:
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("README.md").write_text("# 发布候选测试\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(
        "from pathlib import Path\n\n"
        "def test_harness_contains_required_sources():\n"
        "    root = Path(__file__).parents[1]\n"
        "    assert (root / 'AGENT.md').is_file()\n"
        "    assert (root / 'agent.yaml').is_file()\n",
        encoding="utf-8",
    )


def _candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    content: str = "# Test Agent\n\n发布候选变更。\n",
    agent_id: str | None = None,
    record_passed_test: bool = True,
):
    if agent_id and agent_id != DEFAULT_BUSINESS_AGENT_ID:
        workspace = business_agent_layout(governance.feedback_store.data_dir, agent_id).workspace
        if not workspace.exists():
            create_test_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
        _write_real_workspace_suite(workspace)
    change_set = governance.create_change_set(title="候选发布测试", operator="tester", agent_id=agent_id)
    worktree_path = Path(str(change_set["worktree_path"]))
    worktree_path.joinpath("candidate-note.md").write_text(content, encoding="utf-8")
    # 候选提交必须落在该 change set 归属 Agent 自己的版本 store（per-agent 隔离）。
    commit_store = governance._store_for(change_set.get("agent_id"))
    candidate_commit = commit_store.commit_worktree(worktree_path, message="Commit candidate change")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate_commit,
        execution_job_id="job-publish-test",
        operator="tester",
    )
    if record_passed_test:
        _record_passed_test_run(
            governance,
            agent_id=str(committed["agent_id"]),
            commit_sha=candidate_commit,
            change_set_id=str(committed["change_set_id"]),
        )
        refreshed = governance.get_change_set(str(committed["change_set_id"]))
        assert refreshed is not None
        return refreshed
    return committed


def _feedback_candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    record_passed_test: bool = True,
) -> tuple[dict, str]:
    change_set_id = "agc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bound_at = "2026-07-10T00:00:00+00:00"
    with governance.feedback_store.Session.begin() as db:
        db.add(
            ImprovementItemModel(
                improvement_id="imp-publish",
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
                title="来源治理",
                improvement_stage="regression",
                improvement_status="active",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            AttributionModel(
                attribution_id="attr-publish",
                improvement_id="imp-publish",
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            OptimizationPlanModel(
                optimization_plan_id="opt-publish",
                improvement_id="imp-publish",
                status="confirmed",
                created_at=bound_at,
                updated_at=bound_at,
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id="exec-publish",
                improvement_id="imp-publish",
                change_set_id=change_set_id,
                status="confirmed",
                source_optimization_plan_id="opt-publish",
                source_optimization_plan_updated_at=bound_at,
                source_attribution_id="attr-publish",
                source_attribution_updated_at=bound_at,
            )
        )
    change_set = governance.create_change_set(
        change_set_id=change_set_id,
        execution_job_id="exec-publish",
        source=ChangeSetSource("imp-publish", "attr-publish", "confirmed"),
    )
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("candidate-note.md").write_text("provenance candidate\n", encoding="utf-8")
    candidate = agent_store.commit_worktree(worktree, message="provenance candidate")
    committed = governance.mark_candidate_committed(
        change_set_id,
        candidate_commit_sha=candidate,
        execution_job_id="exec-publish",
    )
    if record_passed_test:
        _record_passed_test_run(
            governance,
            agent_id=str(committed["agent_id"]),
            commit_sha=candidate,
            change_set_id=change_set_id,
        )
        refreshed = governance.get_change_set(change_set_id)
        assert refreshed is not None
        return refreshed, bound_at
    return committed, bound_at


def _record_passed_test_run(
    governance: AgentGovernanceService,
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str,
) -> dict:
    testing_store = AgentTestingStore(governance.feedback_store.Session)
    change_set = governance.get_change_set(change_set_id)
    assert change_set is not None
    suite = inspect_agent_test_suite(
        Path(str(change_set["worktree_path"])),
        agent_id=agent_id,
        commit_sha=commit_sha,
    )
    assert suite.runnable and suite.suite_digest
    run = testing_store.create_run(
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite=suite.model_dump(mode="json"),
        suite_digest=suite.suite_digest,
    )
    runner = AgentTestRunner(
        store=testing_store,
        store_for=governance._store_for,
        artifacts_dir=governance.feedback_store.data_dir / ".publish-test-runs",
        api_base_url="http://127.0.0.1:1",
        api_key=None,
        timeout_seconds=30,
    )
    try:
        runner.enqueue(str(run["test_run_id"]))
        deadline = time.monotonic() + 30
        persisted = testing_store.get_run(str(run["test_run_id"]))
        while persisted and persisted["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            persisted = testing_store.get_run(str(run["test_run_id"]))
        assert persisted is not None and persisted["status"] == "passed", persisted
        return persisted
    finally:
        runner.close()


def _record_completed_test_run(
    governance: AgentGovernanceService,
    change_set: dict,
    *,
    status: str,
    outcome: str,
) -> dict:
    testing_store = AgentTestingStore(governance.feedback_store.Session)
    candidate = str(change_set["candidate_commit_sha"])
    suite = inspect_agent_test_suite(
        Path(str(change_set["worktree_path"])),
        agent_id=str(change_set["agent_id"]),
        commit_sha=candidate,
    )
    assert suite.runnable and suite.suite_digest
    run = testing_store.create_run(
        agent_id=str(change_set["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(change_set["change_set_id"]),
        source="release_check",
        command=FIXED_PYTEST_COMMAND,
        suite=suite.model_dump(mode="json"),
        suite_digest=suite.suite_digest,
    )
    assert testing_store.claim_run(str(run["test_run_id"])) is not None
    return testing_store.finish_run(
        str(run["test_run_id"]),
        status=status,
        report={"exit_code": 0 if status == "passed" else 1},
        items=[{"nodeid": "tests/test_agent.py::test_agent", "outcome": outcome}],
        stdout="",
        stderr="",
    )


def _approval_kwargs(governance: AgentGovernanceService, change_set: dict) -> dict:
    current = governance.get_change_set(str(change_set["change_set_id"]))
    assert current is not None
    candidate = str(current["candidate_commit_sha"])
    diff = governance.change_set_diff(current, candidate)
    run = current.get("latest_test_run")
    assert diff is not None and isinstance(run, dict)
    review = inspect_candidate_review(
        diff,
        lambda path: governance.change_set_file_diff(current, candidate, path),
    )
    return {
        "candidate_commit_sha": candidate,
        "diff_digest": candidate_diff_digest(diff),
        "test_run_id": run["test_run_id"],
        "suite_digest": run["suite_digest"],
        "reviewed_files": [item.to_payload() for item in review.files],
    }


def _missing_approval_kwargs(governance: AgentGovernanceService, change_set: dict) -> dict:
    candidate = str(change_set["candidate_commit_sha"])
    diff = governance.change_set_diff(change_set, candidate)
    assert diff is not None
    review = inspect_candidate_review(
        diff,
        lambda path: governance.change_set_file_diff(change_set, candidate, path),
    )
    return {
        "candidate_commit_sha": candidate,
        "diff_digest": candidate_diff_digest(diff),
        "test_run_id": "agtr-missing",
        "suite_digest": "0" * 64,
        "reviewed_files": [item.to_payload() for item in review.files],
    }


def _install_published_event_rejection(governance: AgentGovernanceService) -> None:
    with governance.feedback_store.Session.begin() as db:
        db.execute(
            text(
                "CREATE TRIGGER reject_published_event BEFORE INSERT ON agent_change_set_events "
                "WHEN NEW.action IN ('published', 'force_published') "
                "BEGIN SELECT RAISE(ABORT, 'reject real published event insert'); END"
            )
        )


def _drop_published_event_rejection(governance: AgentGovernanceService) -> None:
    with governance.feedback_store.Session.begin() as db:
        db.execute(text("DROP TRIGGER reject_published_event"))


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
    worktree.joinpath("AGENT.md").write_text("first candidate\n", encoding="utf-8")
    first_candidate = agent_store.commit_worktree(worktree, message="first candidate")
    governance.mark_candidate_committed(stable_id, candidate_commit_sha=first_candidate, execution_job_id="exec-stable")
    worktree.joinpath("AGENT.md").write_text("stale second candidate\n", encoding="utf-8")
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


def test_publish_rejects_literal_mcp_endpoint_even_after_manual_approval(tmp_path):
    governance, store = _governance(tmp_path)
    original_head = store.current_commit_sha()
    change_set = governance.create_change_set(title="real MCP endpoint", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    mcp_path = worktree / "mcp" / "sec-ops.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcp_config"]["url"] = "http://unapproved.example/mcp"
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="drift managed MCP")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-invalid-policy",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    assert committed["status"] == "pending_approval"
    assert "mcp/sec-ops.json" in committed["approval_reason"]

    with pytest.raises(AgentGovernanceError, match="manual approval"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")
    governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )
    with pytest.raises(AgentGovernanceError, match="Managed Agent policy rejected"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert store.current_commit_sha() == original_head


def test_publish_requires_manual_approval_for_valid_mcp_capability_change(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="approved MCP capability", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    mcp_path = worktree / "mcp" / "sec-ops.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["enable_tools"] = ["soc_api__list_alerts"]
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="approve exact MCP capability")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-approved-mcp",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    assert committed["status"] == "pending_approval"

    with pytest.raises(AgentGovernanceError, match="manual approval"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester", force=True, note="cannot bypass")
    governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )

    published = governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert published["commit_sha"] == candidate


def test_prompt_only_candidate_requires_passed_test_before_approval_and_binds_evidence(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="prompt-only approval", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Prompt only\n\n必须先测试再审批。\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="prompt-only candidate")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-prompt-only",
    )

    assert committed["status"] == "pending_approval"
    assert "AGENT.md" in committed["approval_reason"]
    with pytest.raises(AgentGovernanceError, match="审批前必须完成") as before_test:
        governance.approve_change_set(
            str(committed["change_set_id"]),
            operator="reviewer",
            **_missing_approval_kwargs(governance, committed),
        )
    assert before_test.value.status_code == 409

    passed_run = _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    with pytest.raises(AgentGovernanceError, match="manual approval"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")
    approved = governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )

    assert approved["status"] == "approved"
    evidence = approved["approval_evidence"]
    assert evidence["candidate_commit_sha"] == candidate
    assert evidence["diff_digest"] == approved["diff_summary"]["digest"]
    assert evidence["test_run_id"] == passed_run["test_run_id"]
    assert evidence["suite_digest"] == passed_run["suite_digest"]
    assert evidence["reviewed_file_count"] >= 1
    assert len(evidence["review_digest"]) == 64
    assert len(approved["approval_evidence"]["diff_digest"]) == 64
    approved_event = [event for event in governance.list_change_set_events(str(committed["change_set_id"])) if event["action"] == "approved"]
    assert approved_event[0]["after"]["approval_evidence"] == approved["approval_evidence"]
    newer_run = _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    assert newer_run["test_run_id"] != approved["approval_evidence"]["test_run_id"]
    assert governance.publish_change_set(str(committed["change_set_id"]), operator="tester")["commit_sha"] == candidate


def test_approval_rejects_candidate_with_binary_or_truncated_file_diff(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="unreviewable candidate", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Sensitive prompt change\n", encoding="utf-8")
    worktree.joinpath("opaque.bin").write_bytes(b"\x00\x01\x02")
    candidate = store.commit_worktree(worktree, message="unreviewable binary candidate")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-unreviewable",
    )
    passed_run = _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    diff = governance.change_set_diff(committed, candidate)
    assert diff is not None

    with pytest.raises(AgentGovernanceError, match="not completely reviewable") as exc:
        governance.approve_change_set(
            str(committed["change_set_id"]),
            operator="reviewer",
            candidate_commit_sha=candidate,
            diff_digest=candidate_diff_digest(diff),
            test_run_id=str(passed_run["test_run_id"]),
            suite_digest=str(passed_run["suite_digest"]),
            reviewed_files=[{"path": "AGENT.md", "detail_sha256": "0" * 64}],
        )

    assert exc.value.status_code == 409
    assert governance.get_change_set(str(committed["change_set_id"]))["status"] == "pending_approval"


def test_approval_rejects_each_stale_candidate_diff_test_suite_and_file_claim(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="approval claim CAS", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Exact review claims\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="exact review claims")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-review-claims",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    valid = _approval_kwargs(governance, committed)
    mutations = (
        {"candidate_commit_sha": "f" * 40},
        {"diff_digest": "e" * 64},
        {"test_run_id": "atr-stale"},
        {"suite_digest": "d" * 64},
        {"reviewed_files": [{**item, "detail_sha256": "c" * 64} for item in valid["reviewed_files"]]},
    )
    for mutation in mutations:
        with pytest.raises(AgentGovernanceError) as exc:
            governance.approve_change_set(
                str(committed["change_set_id"]),
                operator="reviewer",
                **{**valid, **mutation},
            )
        assert exc.value.status_code == 409
        assert governance.get_change_set(str(committed["change_set_id"]))["status"] == "pending_approval"

    approved = governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **valid,
    )
    assert approved["status"] == "approved"


def test_publish_rejects_tampered_approval_evidence(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="tampered approval evidence", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Protected prompt\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="protected prompt")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-tampered-evidence",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    approved = governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, str(committed["change_set_id"]))
        assert row is not None
        payload = dict(row.payload_json or {})
        payload["approval_evidence"] = {**dict(payload["approval_evidence"]), "diff_digest": "0" * 64}
        row.payload_json = payload

    with pytest.raises(AgentGovernanceError, match="approval evidence no longer matches") as exc:
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")
    assert exc.value.status_code == 409

    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, str(committed["change_set_id"]))
        assert row is not None
        payload = dict(row.payload_json or {})
        payload["approval_evidence"] = {
            **dict(approved["approval_evidence"]),
            "test_run_id": "agtr-does-not-exist",
        }
        row.payload_json = payload
    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配") as wrong_run:
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")
    assert wrong_run.value.status_code == 409


def test_publish_rejects_candidate_missing_required_agentscope_prompt(tmp_path):
    governance, store = _governance(tmp_path)
    original_head = store.current_commit_sha()
    change_set = governance.create_change_set(title="missing AgentScope prompt", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    (worktree / "AGENT.md").unlink()
    worktree.joinpath("tests", "test_agent.py").write_text(
        "from pathlib import Path\n\n"
        "def test_harness_keeps_agent_config():\n"
        "    root = Path(__file__).parents[1]\n"
        "    assert (root / 'agent.yaml').is_file()\n",
        encoding="utf-8",
    )
    candidate = store.commit_worktree(worktree, message="remove required AgentScope prompt")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-invalid-agentscope-policy",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )

    with pytest.raises(AgentGovernanceError, match="Managed Agent policy rejected"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert store.current_commit_sha() == original_head


def test_skill_only_candidate_requires_approval_before_publish(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="custom AgentScope skill", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    custom_skill = worktree / "skills" / "custom-audit" / "SKILL.md"
    custom_skill.parent.mkdir(parents=True, exist_ok=True)
    custom_skill.write_text(
        "---\nname: custom-audit\ndescription: 审计智能体输出。\n---\n\n# Custom Audit\n",
        encoding="utf-8",
    )
    candidate = store.commit_worktree(worktree, message="add custom AgentScope skill")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-custom-skill-policy",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    assert committed["status"] == "pending_approval"
    assert "skills/custom-audit/SKILL.md" in committed["approval_reason"]
    with pytest.raises(AgentGovernanceError, match="manual approval"):
        governance.publish_change_set(str(committed["change_set_id"]), operator="tester")
    approved = governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="reviewer",
        **_approval_kwargs(governance, committed),
    )
    assert approved["approval_evidence"]["candidate_commit_sha"] == candidate

    published = governance.publish_change_set(str(committed["change_set_id"]), operator="tester")

    assert published is not None
    assert (store.repository_dir / "skills" / "custom-audit" / "SKILL.md").is_file()


def test_utf8_skill_path_is_visible_and_requires_manual_approval(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="UTF-8 skill path", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    skill = worktree / "skills" / "审计" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# 审计\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="add UTF-8 skill")

    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-utf8-skill-policy",
    )

    assert committed["status"] == "pending_approval"
    assert "skills/审计/SKILL.md" in committed["approval_reason"]


def test_business_agent_version_chain_is_isolated_from_platform_default(tmp_path):
    """B3.2/B3.3：普通业务 Agent 的版本链与平台默认业务 Agent 相互隔离。"""
    governance, default_store = _governance(tmp_path)
    default_head_before = default_store.current_commit_sha()

    # 为业务 Agent 创建 → 提交 → 发布一条独立版本记录。
    biz_change_set = _candidate_change_set(
        governance,
        default_store,
        content="# Biz Agent\n\n业务 Agent 候选。\n",
        agent_id="biz-agent-001",
    )
    assert biz_change_set["agent_id"] == "biz-agent-001"
    biz_release = governance.publish_change_set(str(biz_change_set["change_set_id"]), operator="tester")
    assert biz_release["agent_id"] == "biz-agent-001"

    # 隔离性：发布普通业务 Agent 版本不改动默认 Agent 的版本链。
    assert default_store.current_commit_sha() == default_head_before
    biz_store = governance._store_for("biz-agent-001")
    assert biz_store.repository_dir != default_store.repository_dir
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]
    assert biz_store.repository_dir != default_store.repository_dir

    # 按 Agent 过滤互不串扰：各自只看到自己的 change set/release。
    assert [cs["change_set_id"] for cs in governance.list_change_sets(agent_id="biz-agent-001")] == [biz_change_set["change_set_id"]]
    assert governance.list_change_sets(agent_id=DEFAULT_BUSINESS_AGENT_ID) == []
    assert [rel["release_id"] for rel in governance.list_releases(agent_id="biz-agent-001")] == [biz_release["release_id"]]
    assert governance.list_releases(agent_id=DEFAULT_BUSINESS_AGENT_ID) == []

    # 默认 Agent 路径不受影响，仍可独立创建并发布版本。
    default_change_set = _candidate_change_set(governance, default_store, content="# Default Agent\n\n默认候选。\n")
    assert default_change_set["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    default_release = governance.publish_change_set(str(default_change_set["change_set_id"]), operator="tester")
    assert default_release["agent_id"] == DEFAULT_BUSINESS_AGENT_ID
    assert default_store.current_commit_sha() == default_release["commit_sha"]
    # 普通业务 Agent 链未被默认 Agent 发布污染。
    assert biz_store.current_commit_sha() == biz_release["commit_sha"]


def test_governance_serves_multiple_business_agents_with_isolated_closed_loops(tmp_path):
    """AGV-017：多个业务 Agent 的运行、反馈、测试门和版本记录互不混淆。"""
    governance, default_store = _governance(tmp_path)
    store = governance.feedback_store
    agents = ("agent-alpha", "agent-beta")

    records: dict[str, dict] = {}
    for agent_id in agents:
        # 每个业务 Agent 一条独立闭环记录：run -> signal -> case + change set/release。
        store.record_run(
            _run_payload(
                run_id=f"run-{agent_id}",
                agent_id=agent_id,
                created_at="2026-06-12T00:00:00Z",
                started_at="2026-06-12T00:00:00Z",
                updated_at="2026-06-12T00:00:00Z",
                completed_at="2026-06-12T00:00:00Z",
            )
        )
        signal = store.create_signal(FeedbackSignalCreateRequest(run_id=f"run-{agent_id}", labels=["tool_data_incomplete"]))
        case = store.create_case(source_refs=[("signal", signal["signal_id"])], title=f"{agent_id} 反馈")
        change_set = _candidate_change_set(governance, default_store, content=f"# {agent_id}\n\n候选\n", agent_id=agent_id)
        release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
        records[agent_id] = {
            "signal": signal,
            "case": case,
            "change_set": change_set,
            "release": release,
        }

    # 治理 Agent（单一 governance 实例）为不同业务 Agent 各自管理独立版本 store（物理隔离）。
    assert governance._store_for("agent-alpha") is not governance._store_for("agent-beta")

    # 每个维度按 Agent 过滤只见自身记录，不被另一个 Agent 串扰。
    for agent_id in agents:
        assert {r["agent_id"] for r in store.list_runs(agent_id=agent_id)} == {agent_id}
        assert {s["agent_id"] for s in store.list_signals(agent_id=agent_id)} == {agent_id}
        assert records[agent_id]["case"]["agent_id"] == agent_id
        assert records[agent_id]["change_set"]["latest_test_run"]["agent_id"] == agent_id
        assert {c["agent_id"] for c in governance.list_change_sets(agent_id=agent_id)} == {agent_id}
        assert {rel["agent_id"] for rel in governance.list_releases(agent_id=agent_id)} == {agent_id}

    # 跨 Agent 隔离：alpha 的版本记录不出现在 beta 的过滤视图。
    alpha_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-alpha")}
    beta_cs = {c["change_set_id"] for c in governance.list_change_sets(agent_id="agent-beta")}
    assert alpha_cs and beta_cs and alpha_cs.isdisjoint(beta_cs)
    # 各 Agent 版本链落在各自 store，互不污染。
    assert governance._store_for("agent-alpha").current_commit_sha() == records["agent-alpha"]["release"]["commit_sha"]
    assert governance._store_for("agent-beta").current_commit_sha() == records["agent-beta"]["release"]["commit_sha"]


def test_create_change_set_rejects_path_traversal_agent_id(tmp_path):
    """B3.2 越权输入：恶意 agent_id（路径穿越）不得用于版本 store 落地路径。"""
    governance, _ = _governance(tmp_path)
    for hostile in ["../evil", "biz/../../etc", ".", "..", "a/b", "with space"]:
        with pytest.raises(AgentGovernanceError) as exc:
            governance.create_change_set(title="恶意归属", operator="attacker", agent_id=hostile)
        assert exc.value.status_code == 400


def test_candidate_committed_change_set_can_publish_directly(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert release["commit_sha"] == change_set["candidate_commit_sha"]
    assert release["status"] == "published"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "published"


def test_direct_publish_rejects_newer_failed_exact_candidate_run_without_mutation(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    failed = _record_completed_test_run(
        governance,
        change_set,
        status="failed",
        outcome="failed",
    )
    events_before = governance.list_change_set_events(change_set_id)

    projected = governance.get_change_set(change_set_id)
    assert projected is not None
    assert projected["latest_test_run"] is None
    assert projected["publication_blocker"]
    with pytest.raises(AgentGovernanceError, match="当前精确候选") as exc:
        governance.publish_change_set(change_set_id, operator="tester")

    assert exc.value.status_code == 409
    current = governance.get_change_set(change_set_id)
    assert current is not None and current["status"] == "candidate_committed"
    assert "publication_intent" not in current
    assert governance.list_change_set_events(change_set_id) == events_before
    assert governance.list_releases(agent_id=str(change_set["agent_id"])) == []
    assert failed["status"] == "failed"


def test_publish_request_rejects_stale_review_fence_before_and_after_idempotent_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    stale = {
        "expected_candidate_commit_sha": "f" * 40,
        "expected_diff_digest": "e" * 64,
        "expected_test_run_id": "atr-stale-review",
        "expected_suite_digest": "d" * 64,
    }

    with pytest.raises(AgentGovernanceError, match="stale") as before:
        governance.publish_change_set(change_set_id, operator="tester", **stale)
    assert before.value.status_code == 409
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"

    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["status"] == "published"
    with pytest.raises(AgentGovernanceError, match="immutable intent") as after:
        governance.publish_change_set(change_set_id, operator="tester", **stale)
    assert after.value.status_code == 409


def test_approval_rejects_when_newer_exact_candidate_test_failed_after_review(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="approval test CAS", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Review candidate\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="approval test CAS")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-approval-cas",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    reviewed_claims = _approval_kwargs(governance, committed)
    _record_completed_test_run(governance, committed, status="failed", outcome="failed")

    with pytest.raises(AgentGovernanceError, match="stale or not passed") as exc:
        governance.approve_change_set(
            str(committed["change_set_id"]),
            operator="reviewer",
            **reviewed_claims,
        )

    assert exc.value.status_code == 409
    current = governance.get_change_set(str(committed["change_set_id"]))
    assert current is not None and current["status"] == "pending_approval"
    assert not any(event["action"] == "approved" for event in governance.list_change_set_events(str(committed["change_set_id"])))


def test_approval_rejects_newer_passed_status_without_structured_items(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="approval report completeness", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Complete report required\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="approval report completeness")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-approval-report",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    reviewed_claims = _approval_kwargs(governance, committed)
    incomplete = _record_completed_test_run(governance, committed, status="passed", outcome="passed")
    with governance.feedback_store.Session.begin() as db:
        db.execute(
            text("DELETE FROM agent_test_run_items WHERE test_run_id = :test_run_id"),
            {"test_run_id": incomplete["test_run_id"]},
        )

    with pytest.raises(AgentGovernanceError, match="stale or not passed") as exc:
        governance.approve_change_set(
            str(committed["change_set_id"]),
            operator="reviewer",
            **reviewed_claims,
        )

    assert exc.value.status_code == 409
    assert governance.get_change_set(str(committed["change_set_id"]))["status"] == "pending_approval"


def test_publish_requires_passed_platform_test_for_exact_candidate_commit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store, record_passed_test=False)
    agent_id = str(change_set["agent_id"])
    commit_sha = str(change_set["candidate_commit_sha"])

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert projected is not None
    assert projected["latest_test_run"] is None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="commit_sha 完全匹配"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    _record_passed_test_run(
        governance,
        agent_id=agent_id,
        commit_sha=str(change_set["base_commit_sha"]),
        change_set_id=str(change_set["change_set_id"]),
    )
    assert governance.get_change_set(str(change_set["change_set_id"]))["latest_test_run"] is None

    _record_passed_test_run(
        governance,
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=str(change_set["change_set_id"]),
    )
    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
    assert release["commit_sha"] == commit_sha


def test_force_publish_manual_candidate_bypasses_only_test_gate_and_persists_warning_audit(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store, record_passed_test=False)
    change_set_id = str(change_set["change_set_id"])

    reason = "紧急修复，已由值班负责人确认发布风险"
    with pytest.raises(AgentGovernanceError, match="explicit reason") as missing_reason:
        governance.publish_change_set(change_set_id, operator="tester", force=True)
    assert missing_reason.value.status_code == 422

    release = governance.publish_change_set(
        change_set_id,
        operator="tester",
        note=reason,
        force=True,
    )
    assert release["force_published"] is True
    assert release["operator"] == "tester"
    assert release["force_publish_reason"] == reason
    assert "平台测试运行记录" in release["force_publication_blocker"]
    events = governance.list_change_set_events(change_set_id)
    assert [event for event in events if event["action"] == "force_published"]

    request = AgentChangeSetPublishRequest(
        force=True,
        force_reason=reason,
        expected_candidate_commit_sha=str(change_set["candidate_commit_sha"]),
        expected_diff_digest=str(change_set["diff_summary"]["digest"]),
    )
    assert request.force_reason == reason and request.expected_test_run_id is None
    with pytest.raises(ValueError, match="force_reason"):
        AgentChangeSetPublishRequest(
            force=True,
            expected_candidate_commit_sha=str(change_set["candidate_commit_sha"]),
            expected_diff_digest=str(change_set["diff_summary"]["digest"]),
        )
    with pytest.raises(ValueError, match="explicitly omit test evidence"):
        AgentChangeSetPublishRequest(
            force=True,
            force_reason=reason,
            expected_candidate_commit_sha=str(change_set["candidate_commit_sha"]),
            expected_diff_digest=str(change_set["diff_summary"]["digest"]),
            expected_test_run_id="atr-not-applicable",
            expected_suite_digest="e" * 64,
        )
    with pytest.raises(ValueError, match="normal publication requires"):
        AgentChangeSetPublishRequest(
            expected_candidate_commit_sha=str(change_set["candidate_commit_sha"]),
            expected_diff_digest=str(change_set["diff_summary"]["digest"]),
        )


def test_force_publish_is_rejected_when_no_platform_test_blocker_exists(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    with pytest.raises(AgentGovernanceError, match="bypassable platform test blocker") as exc:
        governance.publish_change_set(
            str(change_set["change_set_id"]),
            operator="tester",
            note="不应绕过已经通过的测试",
            force=True,
        )

    assert exc.value.status_code == 409


def test_feedback_publication_cannot_force_bypass_complete_agent_test_suite(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set, _bound_at = _feedback_candidate_change_set(
        governance,
        agent_store,
        record_passed_test=False,
    )
    change_set_id = str(change_set["change_set_id"])
    commit_sha = str(change_set["candidate_commit_sha"])
    agent_id = str(change_set["agent_id"])
    projected = governance.get_change_set(change_set_id)
    assert projected is not None
    assert "commit_sha 完全匹配" in str(projected["publication_blocker"])
    with pytest.raises(AgentGovernanceError, match="不能强制绕过") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="tester",
            note="请求跳过失败测试",
            force=True,
        )
    assert exc.value.status_code == 409
    assert governance.get_change_set(change_set_id)["status"] == "candidate_committed"

    _record_passed_test_run(
        governance,
        agent_id=agent_id,
        commit_sha=commit_sha,
        change_set_id=change_set_id,
    )
    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["commit_sha"] == commit_sha
    assert release["force_published"] is False


def test_publish_retries_after_real_archive_filesystem_failure_without_duplicate_release(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    writable_releases_dir = agent_store.releases_dir
    agent_store.releases_dir = Path("/proc")

    with pytest.raises(AgentGovernanceError, match="Agent publish failed"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert pending["publication_error"]["detail"]
    # HEAD 是 Git 发布的不可回退分界；archive 失败只保留 intent 供原命令重放。
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.list_releases() == []
    intent = pending["publication_intent"]
    public_evidence = pending["publication_evidence"]
    assert public_evidence == {
        "candidate_commit_sha": intent["commit_sha"],
        "diff_digest": intent["diff_digest"],
        "test_run_id": intent["test_run_id"],
        "suite_digest": intent["suite_digest"],
        "tag_name": intent["tag_name"],
        "force": False,
    }
    assert (
        agent_store._git(
            ["rev-parse", "--verify", f"refs/tags/{intent['tag_name']}^{{commit}}"],
            cwd=agent_store.repository_dir,
        ).strip()
        == change_set["candidate_commit_sha"]
    )

    for _index in range(21):
        _record_completed_test_run(governance, pending, status="passed", outcome="passed")
    reopened = governance.get_change_set(change_set_id)
    assert reopened is not None
    assert reopened["publication_evidence"] == public_evidence
    assert reopened["latest_test_run_id"] == intent["test_run_id"]

    agent_store.releases_dir = writable_releases_dir
    release = governance.publish_change_set(
        change_set_id,
        operator="retrying-operator",
        tag_name=str(public_evidence["tag_name"]),
        expected_candidate_commit_sha=str(public_evidence["candidate_commit_sha"]),
        expected_diff_digest=str(public_evidence["diff_digest"]),
        expected_test_run_id=str(public_evidence["test_run_id"]),
        expected_suite_digest=str(public_evidence["suite_digest"]),
    )

    assert release["release_id"] == intent["release_id"]
    assert Path(str(release["archive_path"])).is_file()
    assert len(governance.list_releases()) == 1
    assert governance.get_change_set(change_set_id)["status"] == "published"


def test_publish_retry_fails_before_external_activation_when_tag_claim_is_missing(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    writable_releases_dir = agent_store.releases_dir
    agent_store.releases_dir = Path("/proc")
    with pytest.raises(AgentGovernanceError, match="Agent publish failed"):
        governance.publish_change_set(change_set_id, operator="tester")
    pending = governance.get_change_set(change_set_id)
    assert pending is not None
    intent = pending["publication_intent"]
    with governance.feedback_store.Session.begin() as db:
        claim = db.get(AgentReleaseTagClaimModel, (intent["agent_id"], intent["tag_name"]))
        assert claim is not None
        db.delete(claim)

    async def unexpected_activation(*_args, **_kwargs):
        raise AssertionError("release activation must not run after claim integrity fails")

    governance.release_activator = unexpected_activation
    agent_store.releases_dir = writable_releases_dir
    with pytest.raises(AgentGovernanceError, match="not owned") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="retrying-operator",
            tag_name=str(intent["tag_name"]),
            expected_candidate_commit_sha=str(intent["commit_sha"]),
            expected_diff_digest=str(intent["diff_digest"]),
            expected_test_run_id=str(intent["test_run_id"]),
            expected_suite_digest=str(intent["suite_digest"]),
        )
    assert exc.value.status_code == 409


def test_publishing_retry_rejects_repointed_tag_before_runtime_or_git_side_effect(tmp_path, monkeypatch):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    writable_releases_dir = agent_store.releases_dir
    agent_store.releases_dir = Path("/proc")
    with pytest.raises(AgentGovernanceError, match="Agent publish failed"):
        governance.publish_change_set(change_set_id, operator="tester")
    pending = governance.get_change_set(change_set_id)
    assert pending is not None
    intent = pending["publication_intent"]
    agent_store._git(
        ["tag", "-f", str(intent["tag_name"]), str(change_set["base_commit_sha"])],
        cwd=agent_store.repository_dir,
    )

    async def unexpected_activation(*_args, **_kwargs):
        raise AssertionError("Runtime activation must not run after persisted tag identity diverges")

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("Git publish must not run after persisted tag identity diverges")

    governance.release_activator = unexpected_activation
    monkeypatch.setattr(agent_store, "publish_commit", unexpected_publish)
    agent_store.releases_dir = writable_releases_dir
    with pytest.raises(AgentGovernanceError, match="publish preflight") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="retrying-operator",
            tag_name=str(intent["tag_name"]),
            expected_candidate_commit_sha=str(intent["commit_sha"]),
            expected_diff_digest=str(intent["diff_digest"]),
            expected_test_run_id=str(intent["test_run_id"]),
            expected_suite_digest=str(intent["suite_digest"]),
        )
    assert exc.value.status_code == 409


def test_tag_race_after_runtime_prepare_compensates_inactive_binding(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    original_activator = governance.release_activator
    assert original_activator is not None
    compensated: list[object] = []

    async def racing_activation(**kwargs):
        binding = await original_activator(**kwargs)
        agent_store._git(
            ["tag", "-f", f"agent-release-{change_set_id}", str(change_set["base_commit_sha"])],
            cwd=agent_store.repository_dir,
        )
        return binding

    async def compensate(binding):
        compensated.append(binding)

    governance.release_activator = racing_activation
    governance.release_activation_compensator = compensate

    with pytest.raises(AgentGovernanceError, match="previous active version was retained") as exc:
        governance.publish_change_set(change_set_id, operator="tester")

    assert exc.value.status_code == 409
    assert len(compensated) == 1
    persisted = governance.get_change_set(change_set_id)
    assert persisted is not None and persisted["status"] == "publishing"


def test_publish_real_db_finalize_failure_rolls_back_metadata_and_retry_reconciles(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    _install_published_event_rejection(governance)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(change_set_id, operator="tester")

    pending = governance.get_change_set(change_set_id)
    assert pending["status"] == "publishing"
    assert governance.list_releases() == []
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]

    _drop_published_event_rejection(governance)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    events = governance.list_change_set_events(change_set_id)

    assert release["release_id"] == pending["publication_intent"]["release_id"]
    assert len(governance.list_releases()) == 1
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_publish_retry_does_not_overwrite_revised_source_after_git_side_effect(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set, _bound_at = _feedback_candidate_change_set(governance, agent_store)
    writable_releases_dir = agent_store.releases_dir
    agent_store.releases_dir = Path("/proc")
    with pytest.raises(AgentGovernanceError, match="Agent publish failed"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")
    agent_store.releases_dir = writable_releases_dir
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]

    with governance.feedback_store.Session.begin() as db:
        improvement = db.get(ImprovementItemModel, "imp-publish")
        assert improvement is not None
        improvement.updated_at = "2026-07-10T00:05:00+00:00"

    with pytest.raises(ConflictError, match="发生变化"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    projected = governance.get_change_set(str(change_set["change_set_id"]))
    assert projected["status"] == "publishing"
    assert projected["publication_intent"]["source_improvement_updated_at"] == _bound_at
    assert projected["publication_error"]["detail"]
    assert governance.list_releases() == []


def test_source_claim_blocks_second_publication_before_git_side_effect(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first, _bound_at = _feedback_candidate_change_set(governance, agent_store)
    source_improvement_id = "imp-publish"
    first_release = governance.publish_change_set(str(first["change_set_id"]), operator="tester")
    published_head = agent_store.current_commit_sha()

    second = _candidate_change_set(governance, agent_store, content="# second source publication\n")
    rebound_at = "2026-07-10T01:00:00+00:00"
    with governance.feedback_store.Session.begin() as db:
        second_row = db.get(AgentChangeSetModel, str(second["change_set_id"]))
        execution = db.query(ExecutionRecordModel).filter_by(improvement_id=source_improvement_id).one()
        improvement = db.get(ImprovementItemModel, source_improvement_id)
        attribution = db.get(AttributionModel, "attr-publish")
        plan = db.get(OptimizationPlanModel, "opt-publish")
        assert second_row is not None and improvement is not None and attribution is not None and plan is not None
        improvement.improvement_stage = "regression"
        improvement.improvement_status = "active"
        improvement.updated_at = rebound_at
        attribution.status = "confirmed"
        attribution.updated_at = rebound_at
        plan.status = "confirmed"
        plan.updated_at = rebound_at
        second_payload = dict(second_row.payload_json or {})
        second_payload.update(
            {
                "source_improvement_id": source_improvement_id,
                "source_attribution_id": "attr-publish",
            }
        )
        second_row.payload_json = second_payload
        second_row.execution_job_id = execution.execution_id
        execution.change_set_id = str(second["change_set_id"])
        execution.status = "confirmed"
        execution.applied_agent_version_id = str(second["candidate_commit_sha"])
        execution.source_optimization_plan_id = "opt-publish"
        execution.source_optimization_plan_updated_at = rebound_at
        execution.source_attribution_id = "attr-publish"
        execution.source_attribution_updated_at = rebound_at

    with pytest.raises(AgentGovernanceError, match="持有发布预留，不能重复发布"):
        governance.publish_change_set(str(second["change_set_id"]), operator="tester")

    assert agent_store.current_commit_sha() == published_head
    assert governance.get_change_set(str(second["change_set_id"]))["status"] == "candidate_committed"
    with governance.feedback_store.Session() as db:
        claim = db.get(AgentReleaseSourceClaimModel, (DEFAULT_BUSINESS_AGENT_ID, source_improvement_id))
        assert claim is not None and claim.change_set_id == first["change_set_id"]
    assert first_release["source_improvement_id"] == source_improvement_id


def test_improvement_publication_rejects_unconfirmed_or_revised_provenance_even_with_force(tmp_path):
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

    _install_published_event_rejection(governance)
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

    _drop_published_event_rejection(governance)
    release = governance.publish_change_set(change_set_id, operator="retrying-operator")
    with governance.feedback_store.Session() as db:
        completed_item = db.get(ImprovementItemModel, "imp-publish")

    assert release["source_improvement_id"] == "imp-publish"
    assert completed_item.improvement_stage == "release"
    assert completed_item.improvement_status == "done"
    assert completed_item.updated_at == release["updated_at"]


def test_publish_retry_finalizes_older_tag_after_newer_release_advances_head(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv1\n")
    first_id = str(first["change_set_id"])
    _install_published_event_rejection(governance)
    with pytest.raises(AgentGovernanceError, match="metadata is pending reconciliation"):
        governance.publish_change_set(first_id, operator="tester")
    _drop_published_event_rejection(governance)

    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv2\n")
    second_release = governance.publish_change_set(str(second["change_set_id"]), operator="tester")
    first_release = governance.publish_change_set(first_id, operator="reconciler")

    assert first_release["commit_sha"] == first["candidate_commit_sha"]
    assert agent_store.current_commit_sha() == second_release["commit_sha"]
    assert governance.get_change_set(first_id)["status"] == "published"
    assert len(governance.list_releases()) == 2


def test_divergent_candidate_publish_failure_retains_intent_when_zero_side_effects_cannot_be_proven(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-a\n")
    second = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nbranch-b\n")
    governance.publish_change_set(str(first["change_set_id"]), tag_name="release-branch-a")

    with pytest.raises(AgentGovernanceError, match="previous active version was retained"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name="release-branch-b")

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "publishing"
    assert persisted["publication_intent"]["tag_name"] == "release-branch-b"
    assert persisted["publication_error"]["detail"]
    actions = [event["action"] for event in governance.list_change_set_events(str(second["change_set_id"]))]
    assert actions.count("publication_started") == 1
    assert actions.count("publication_cancelled") == 0
    with governance.feedback_store.Session() as db:
        assert db.get(AgentReleaseTagClaimModel, (DEFAULT_BUSINESS_AGENT_ID, "release-branch-b")) is not None


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
    _record_passed_test_run(
        governance,
        agent_id=str(second["agent_id"]),
        commit_sha=str(second["candidate_commit_sha"]),
        change_set_id=str(second["change_set_id"]),
    )

    with pytest.raises(AgentGovernanceError, match="already assigned to another release"):
        governance.publish_change_set(str(second["change_set_id"]), tag_name=shared_tag)

    persisted = governance.get_change_set(str(second["change_set_id"]))
    assert persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted
    assert "publication_started" not in {event["action"] for event in governance.list_change_set_events(str(second["change_set_id"]))}
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


def test_concurrent_publish_reserves_one_intent_and_one_audit_event(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    ready = Barrier(4)

    def publish_after_barrier(index: int):
        ready.wait(timeout=10)
        try:
            return governance.publish_change_set(change_set_id, operator=f"publisher-{index}")
        except AgentGovernanceError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = [future.result(timeout=30) for future in [executor.submit(publish_after_barrier, index) for index in range(4)]]

    successful = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, AgentGovernanceError)]
    assert successful
    assert all(item["release_id"] == successful[0]["release_id"] for item in successful)
    assert all(item.status_code == 409 for item in conflicts)
    assert len(governance.list_releases()) == 1
    events = governance.list_change_set_events(change_set_id)
    assert [event["action"] for event in events].count("publication_started") == 1
    assert [event["action"] for event in events].count("published") == 1


def test_concurrent_publish_with_different_tags_is_fenced_before_db_reservation(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])
    ready = Barrier(2)

    def publish_tag_after_barrier(index: int):
        ready.wait(timeout=10)
        try:
            return governance.publish_change_set(
                change_set_id,
                operator=f"publisher-{index}",
                tag_name=f"agent-release-competing-{index}",
            )
        except AgentGovernanceError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=30) for future in [executor.submit(publish_tag_after_barrier, index) for index in range(2)]]

    successful = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, AgentGovernanceError)]
    assert len(successful) == len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert successful[0]["tag_name"] in {
        "agent-release-competing-0",
        "agent-release-competing-1",
    }
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


def test_legacy_partial_release_is_blocked_before_new_publication_side_effects(tmp_path, monkeypatch):
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

    async def unexpected_activation(*_args, **_kwargs):
        raise AssertionError("Runtime activation must not run for an orphan release row")

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("Git publication must not run for an orphan release row")

    governance.release_activator = unexpected_activation
    monkeypatch.setattr(agent_store, "publish_commit", unexpected_publish)
    with pytest.raises(AgentGovernanceError, match="without a current immutable publication intent") as exc:
        governance.publish_change_set(change_set_id, operator="reconciler")
    assert exc.value.status_code == 409
    persisted = governance.get_change_set(change_set_id)
    assert persisted is not None and persisted["status"] == "candidate_committed"
    assert "publication_intent" not in persisted

    migration = reconcile_legacy_publication_evidence(governance)
    assert migration.quarantined == 1
    quarantined = governance.get_change_set(change_set_id)
    assert quarantined is not None and quarantined["legacy_publication_quarantine"]
    assert len(governance.list_releases()) == 1
    assert governance.list_releases()[0]["release_id"] == legacy_release_id


def test_terminal_change_set_cannot_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    governance.reject_change_set(str(change_set["change_set_id"]), operator="tester")

    with pytest.raises(AgentGovernanceError, match="cannot be published from status rejected") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409


def test_high_risk_change_set_requires_approval_before_publish(tmp_path):
    """AGV-041：标记为待审批的高风险变更不经审批不得发布；审批后可发布。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    with pytest.raises(AgentGovernanceError, match="recorded approval request"):
        governance.approve_change_set(
            change_set_id,
            operator="reviewer",
            **_approval_kwargs(governance, change_set),
        )

    pending = governance.request_change_set_approval(
        change_set_id,
        operator="reviewer",
        reason="改动生产策略 prompt",
        impact_scope="默认业务 Agent 全量输出",
        rollback_plan="回滚到上一个 release",
    )
    assert pending["status"] == "pending_approval"
    assert pending["impact_scope"] == "默认业务 Agent 全量输出"
    assert pending["rollback_plan"] == "回滚到上一个 release"

    with pytest.raises(AgentGovernanceError) as exc:
        governance.publish_change_set(change_set_id, operator="tester")
    assert exc.value.status_code == 409

    governance.approve_change_set(
        change_set_id,
        operator="reviewer",
        note="审批通过",
        **_approval_kwargs(governance, pending),
    )
    release = governance.publish_change_set(change_set_id, operator="tester")
    assert release["status"] == "published"


def test_rejected_change_set_records_audit_event(tmp_path):
    """AGV-041：拒绝高风险变更产生审计事件，且变更不发布。"""
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    change_set_id = str(change_set["change_set_id"])

    governance.request_change_set_approval(change_set_id, operator="reviewer", reason="风险过高", impact_scope="工具配置", rollback_plan="撤销变更")
    rejected = governance.reject_change_set(change_set_id, operator="reviewer", note="不通过")

    assert rejected["status"] == "rejected"
    actions = {str(event.get("action")) for event in governance.list_change_set_events(change_set_id)}
    assert {"approval_requested", "rejected"} <= actions


def test_repository_ops_route_per_agent_not_always_platform_default(tmp_path):
    """缺陷②回归：repository_status/current_ref 按 agent_id 路由到对应 per-agent 版本库，
    不再恒走平台默认业务 Agent 的版本库。"""
    governance, default_store = _governance(tmp_path)
    assert governance._store_for(None) is default_store
    ordinary_store = governance._store_for(ORDINARY_TEST_AGENT_ID)
    assert ordinary_store.repository_dir != default_store.repository_dir
    # 其他业务 Agent 也走独立 per-Agent 库。
    biz_store = governance._store_for("biz-x")
    assert biz_store.repository_dir != default_store.repository_dir
    assert default_store.repository_dir != biz_store.repository_dir
    assert "business-agents/biz-x/workspace" in str(biz_store.repository_dir)
    # repository_status 按 agent_id 路由：业务 Agent 的状态来自其自己的库，不是默认库。
    biz_status = governance.repository_status("biz-x")
    default_status = governance.repository_status(DEFAULT_BUSINESS_AGENT_ID)
    assert str(biz_store.repository_dir) == str(biz_status["repository_dir"])
    assert biz_status["repository_dir"] != default_status["repository_dir"]


def test_version_governance_rejects_unregistered_ghost_agent(tmp_path):
    """缺陷④：装配 agent_exists 后，未注册 agent_id 的版本治理操作被拒（404），不懒建幽灵版本库。

    main-agent 不再豁免这条校验：它是可删除的普通业务 Agent，删除后对它的版本治理请求应当
    404，而不是就地重建一个版本库把它复活。
    """
    governance, _ = _governance(tmp_path)
    registry = AgentRegistryStore(governance.feedback_store.Session)
    for agent_id in ("real-biz", LEGACY_MAIN_AGENT_ID):
        workspace = business_agent_layout(governance.feedback_store.data_dir, agent_id).workspace
        create_test_business_agent_workspace(workspace, agent_id=agent_id, name=agent_id)
        registry.create_business_agent(name=agent_id, agent_id=agent_id, workspace_dir=str(workspace))
    governance.agent_exists = registry.has_agent
    with pytest.raises(AgentGovernanceError) as exc:
        governance.repository_status("ghost-agent")
    assert exc.value.status_code == 404
    # 已注册的放行（main-agent 与其他业务 Agent 同等对待）。
    assert governance.repository_status(LEGACY_MAIN_AGENT_ID)
    assert governance.repository_status("real-biz")

    # main-agent 未注册（已删除）时同样 404——没有「恒有效」豁免。
    governance.evict_agent_store(LEGACY_MAIN_AGENT_ID)
    registry.delete_business_agent(LEGACY_MAIN_AGENT_ID)
    with pytest.raises(AgentGovernanceError) as deleted_main:
        governance.repository_status(LEGACY_MAIN_AGENT_ID)
    assert deleted_main.value.status_code == 404


def test_legacy_approved_candidate_requires_new_test_and_file_review_after_upgrade(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = governance.create_change_set(title="legacy approved", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("AGENT.md").write_text("# Legacy protected prompt\n", encoding="utf-8")
    candidate = store.commit_worktree(worktree, message="legacy protected prompt")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-legacy-approved",
    )
    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    old_claims = _approval_kwargs(governance, committed)
    approved = governance.approve_change_set(
        str(committed["change_set_id"]),
        operator="legacy-reviewer",
        **old_claims,
    )
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, str(approved["change_set_id"]))
        assert row is not None
        payload = dict(row.payload_json or {})
        payload.pop("candidate_evidence_epoch", None)
        payload.pop("approval_evidence", None)
        row.payload_json = payload

    report = reconcile_legacy_publication_evidence(governance)

    assert report.reset_for_review == 1
    migrated = governance.get_change_set(str(approved["change_set_id"]))
    assert migrated is not None
    assert migrated["status"] == "pending_approval"
    assert migrated["latest_test_run"] is None
    assert migrated["approval_evidence"] is None
    with pytest.raises(AgentGovernanceError, match="候选审批前必须完成"):
        governance.approve_change_set(
            str(approved["change_set_id"]),
            operator="new-reviewer",
            **old_claims,
        )

    _record_passed_test_run(
        governance,
        agent_id=str(committed["agent_id"]),
        commit_sha=candidate,
        change_set_id=str(committed["change_set_id"]),
    )
    refreshed = governance.get_change_set(str(approved["change_set_id"]))
    assert refreshed is not None and refreshed["latest_test_run"] is not None
    reapproved = governance.approve_change_set(
        str(approved["change_set_id"]),
        operator="new-reviewer",
        **_approval_kwargs(governance, refreshed),
    )
    assert reapproved["status"] == "approved"


def test_legacy_publishing_without_git_side_effect_is_atomically_reset_and_claim_released(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store, record_passed_test=False)
    change_set_id = str(change_set["change_set_id"])
    release_id = "agr-legacy-no-side-effect"
    tag_name = "legacy-no-side-effect"
    now = utc_now()
    legacy_intent = {
        "release_id": release_id,
        "change_set_id": change_set_id,
        "agent_id": change_set["agent_id"],
        "commit_sha": change_set["candidate_commit_sha"],
        "tag_name": tag_name,
        "operator": "legacy",
        "note": None,
        "force": False,
        "force_publication_blocker": None,
        "previous_status": "candidate_committed",
        "previous_commit_sha": change_set["base_commit_sha"],
        "started_at": now,
    }
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.status = "publishing"
        row.updated_at = now
        row.payload_json = {**dict(row.payload_json or {}), "publication_intent": legacy_intent}
        db.add(
            AgentReleaseTagClaimModel(
                agent_id=str(change_set["agent_id"]),
                tag_name=tag_name,
                change_set_id=change_set_id,
                release_id=release_id,
                created_at=now,
            ),
        )

    report = reconcile_legacy_publication_evidence(governance)

    assert report.reset_unpublished_intent == 1
    migrated = governance.get_change_set(change_set_id)
    assert migrated is not None and migrated["status"] == "candidate_committed"
    assert "publication_intent" not in migrated
    assert migrated["latest_test_run"] is None
    with governance.feedback_store.Session() as db:
        assert db.get(AgentReleaseTagClaimModel, (str(change_set["agent_id"]), tag_name)) is None


def test_legacy_publishing_with_git_side_effect_is_quarantined_without_fabricated_evidence(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store, record_passed_test=False)
    change_set_id = str(change_set["change_set_id"])
    release_id = "agr-legacy-side-effect"
    tag_name = "legacy-side-effect"
    store.publish_commit(
        str(change_set["candidate_commit_sha"]),
        tag_name=tag_name,
        message="legacy side effect",
    )
    # 即使 HEAD 后续退回 base，已创建的 tag 仍是不可忽略的发布副作用。
    store.reset_to_ref_for_managed_migration(str(change_set["base_commit_sha"]))
    now = utc_now()
    legacy_intent = {
        "release_id": release_id,
        "change_set_id": change_set_id,
        "agent_id": change_set["agent_id"],
        "commit_sha": change_set["candidate_commit_sha"],
        "tag_name": tag_name,
        "operator": "legacy",
        "note": None,
        "force": False,
        "force_publication_blocker": None,
        "previous_status": "candidate_committed",
        "previous_commit_sha": change_set["base_commit_sha"],
        "started_at": now,
    }
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.status = "publishing"
        row.updated_at = now
        row.payload_json = {**dict(row.payload_json or {}), "publication_intent": legacy_intent}

    report = reconcile_legacy_publication_evidence(governance)

    assert report.quarantined == 1
    migrated = governance.get_change_set(change_set_id)
    assert migrated is not None and migrated["status"] == "publishing"
    assert migrated["approval_evidence"] is None
    assert migrated["legacy_publication_quarantine"]["identity"]["release_id"] == release_id
    diff = governance.change_set_diff(migrated, str(migrated["candidate_commit_sha"]))
    assert diff is not None
    with pytest.raises(AgentGovernanceError, match="quarantined"):
        governance.publish_change_set(
            change_set_id,
            operator="reconciler",
            force=True,
            note="do not invent evidence",
            expected_candidate_commit_sha=str(migrated["candidate_commit_sha"]),
            expected_diff_digest=candidate_diff_digest(diff),
        )


def test_legacy_published_identity_is_read_only_after_release_and_git_verification(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store)
    change_set_id = str(change_set["change_set_id"])
    release = governance.publish_change_set(change_set_id, operator="publisher")
    published = governance.get_change_set(change_set_id)
    assert published is not None
    strict_intent = dict(published["publication_intent"])
    legacy_intent = {key: value for key, value in strict_intent.items() if key not in {"schema_version", "diff_digest", "test_run_id", "suite_digest"}}
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.payload_json = {**dict(row.payload_json or {}), "publication_intent": legacy_intent}

    report = reconcile_legacy_publication_evidence(governance)

    assert report.archived_published_identity == 1
    migrated = governance.get_change_set(change_set_id)
    assert migrated is not None and migrated["status"] == "published"
    assert migrated["latest_release_id"] == release["release_id"]
    assert "publication_intent" not in migrated
    assert migrated["legacy_publication_identity"]["read_only"] is True
    with pytest.raises(AgentGovernanceError, match="read-only"):
        governance.publish_change_set(
            change_set_id,
            operator="publisher",
            expected_candidate_commit_sha=str(strict_intent["commit_sha"]),
            expected_diff_digest=str(strict_intent["diff_digest"]),
            expected_test_run_id=str(strict_intent["test_run_id"]),
            expected_suite_digest=str(strict_intent["suite_digest"]),
        )


def test_legacy_published_identity_is_quarantined_when_tag_points_to_another_commit(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store)
    change_set_id = str(change_set["change_set_id"])
    governance.publish_change_set(change_set_id, operator="publisher")
    published = governance.get_change_set(change_set_id)
    assert published is not None
    strict_intent = dict(published["publication_intent"])
    legacy_intent = {key: value for key, value in strict_intent.items() if key not in {"schema_version", "diff_digest", "test_run_id", "suite_digest"}}
    marker = store.repository_dir / "post-release-marker.txt"
    marker.write_text("new head\n", encoding="utf-8")
    store._git(["add", marker.name], cwd=store.repository_dir)
    store._git(["commit", "-m", "Advance after release"], cwd=store.repository_dir)
    newer_commit = store.current_commit_sha()
    assert newer_commit and newer_commit != strict_intent["commit_sha"]
    store._git(["tag", "-f", str(strict_intent["tag_name"]), newer_commit], cwd=store.repository_dir)
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.payload_json = {**dict(row.payload_json or {}), "publication_intent": legacy_intent}

    report = reconcile_legacy_publication_evidence(governance)

    assert report.quarantined == 1
    migrated = governance.get_change_set(change_set_id)
    assert migrated is not None and migrated["legacy_publication_quarantine"]


def test_published_retry_rejects_live_tag_repoint_before_cleanup_without_restart(tmp_path):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store)
    change_set_id = str(change_set["change_set_id"])
    governance.publish_change_set(change_set_id, operator="publisher")
    published = governance.get_change_set(change_set_id)
    assert published is not None
    intent = published["publication_intent"]
    store._git(
        ["tag", "-f", str(intent["tag_name"]), str(change_set["base_commit_sha"])],
        cwd=store.repository_dir,
    )

    with pytest.raises(AgentGovernanceError, match="live Git/tag identity") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="publisher",
            tag_name=str(intent["tag_name"]),
            expected_candidate_commit_sha=str(intent["commit_sha"]),
            expected_diff_digest=str(intent["diff_digest"]),
            expected_test_run_id=str(intent["test_run_id"]),
            expected_suite_digest=str(intent["suite_digest"]),
        )

    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("commit_sha", "base"),
        ("agent_id", "different-agent"),
        ("release_id", "agr-different-release"),
        ("tag_name", "different-release-tag"),
        ("force", "false"),
        ("test_run_id", 123),
        ("suite_digest", 456),
        ("previous_commit_sha", 789),
        ("diff_digest", "0" * 64),
        ("test_run_id", "agtr-nonexistent"),
        ("suite_digest", "0" * 64),
        ("previous_status", "draft"),
        ("operator", "different-operator"),
        ("note", "tampered-note"),
        ("started_at", "2099-01-01T00:00:00Z"),
    ),
)
def test_published_retry_rejects_structurally_valid_but_misbound_intent(tmp_path, field, replacement):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store)
    change_set_id = str(change_set["change_set_id"])
    release = governance.publish_change_set(change_set_id, operator="publisher")
    published = governance.get_change_set(change_set_id)
    assert published is not None
    intent = dict(published["publication_intent"])
    intent[field] = change_set["base_commit_sha"] if replacement == "base" else replacement
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.payload_json = {**dict(row.payload_json or {}), "publication_intent": intent}

    migration = reconcile_legacy_publication_evidence(governance)
    assert migration.quarantined == 1
    with pytest.raises(AgentGovernanceError, match="quarantined") as exc:
        governance.publish_change_set(
            change_set_id,
            operator="publisher",
            expected_candidate_commit_sha=str(intent["commit_sha"]),
            expected_diff_digest=str(intent["diff_digest"]),
            expected_test_run_id=str(intent["test_run_id"]),
            expected_suite_digest=str(intent["suite_digest"]),
        )
    assert exc.value.status_code == 409
    assert len(governance.list_releases()) == 1
    assert governance.list_releases()[0]["release_id"] == release["release_id"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("previous_commit_sha", "0" * 40),
        ("operator", "tampered-operator"),
        ("note", "tampered-note"),
        ("force_published", True),
        ("force_publication_blocker", "tampered blocker"),
    ),
)
def test_current_published_release_payload_tamper_is_quarantined(tmp_path, field, replacement):
    governance, store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, store)
    change_set_id = str(change_set["change_set_id"])
    release = governance.publish_change_set(change_set_id, operator="publisher", note="reviewed")
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentReleaseModel, str(release["release_id"]))
        assert row is not None
        row.payload_json = {**dict(row.payload_json or {}), field: replacement}

    migration = reconcile_legacy_publication_evidence(governance)

    assert migration.quarantined == 1
    quarantined = governance.get_change_set(change_set_id)
    assert quarantined is not None and quarantined["legacy_publication_quarantine"]
