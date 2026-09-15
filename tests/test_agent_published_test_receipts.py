"""历史回执投影/身份契约；宿主 pytest 不证明 Agent 成功，不写 passed Agent 测试记录。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel, AgentReleaseModel, AgentReleaseTagClaimModel, make_session_factory
from app.services.agent_candidate_approval import CandidateApprovalFailure, require_recorded_publication_test
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import candidate_diff_digest, matching_passed_test_run
from app.services.agent_publication import PublicationIntent, release_payload
from app.services.agent_publication_projection import project_change_set_publication_state
from app.services.agent_publication_validation import require_publication_intent_evidence
from sqlalchemy import select

from business_agent_test_utils import create_test_business_agent_workspace
from test_agent_test_report_validation import real_report as real_report


def _intent() -> PublicationIntent:
    return PublicationIntent(
        release_id="agr-receipt-contract",
        change_set_id="agc-receipt-contract",
        agent_id="receipt-contract-agent",
        commit_sha="a" * 40,
        previous_commit_sha="b" * 40,
        diff_digest="c" * 64,
        test_run_id="atr-receipt-contract",
        suite_digest="d" * 64,
        tag_name="receipt-contract-tag",
        operator="contract",
        note=None,
        force=False,
        force_publication_blocker=None,
        previous_status="approved",
        started_at="2026-09-12T00:00:00+00:00",
    )


def _row(intent: PublicationIntent, *, status: str = "published") -> AgentChangeSetModel:
    return AgentChangeSetModel(
        change_set_id=intent.change_set_id,
        agent_id=intent.agent_id,
        status=status,
        base_commit_sha=intent.previous_commit_sha,
        candidate_commit_sha=intent.commit_sha,
        branch_name="receipt-contract",
        worktree_path="",
        payload_json={"publication_intent": intent.to_payload()},
    )


@pytest.fixture
def historical_projection_input(real_report: dict[str, object]) -> JsonObject:
    # 仅构造边界输入，绝不把这个无 Agent 调用的宿主报告持久化为 release_check passed。
    report = deepcopy(real_report)
    report.pop("collected_nodeids")
    for item in report["items"]:
        item.pop("phase_outcomes")
    intent = _intent()
    return {
        "test_run_id": intent.test_run_id,
        "agent_id": intent.agent_id,
        "commit_sha": intent.commit_sha,
        "change_set_id": intent.change_set_id,
        "suite_digest": intent.suite_digest,
        "source": "release_check",
        "status": "passed",
        "items": deepcopy(report["items"]),
        "report": report,
    }


def _project(receipt: JsonObject, *, status: str = "published", extra: JsonObject | None = None) -> JsonObject:
    intent = _intent()
    row = _row(intent, status=status)
    payload = {
        **row.payload_json,
        "agent_id": intent.agent_id,
        "status": status,
        "approval_evidence": {"test_run_id": intent.test_run_id},
        **(extra or {}),
    }
    return project_change_set_publication_state(
        row,
        payload,
        test_run_by_id={intent.test_run_id: receipt}.get,
        latest_candidate_test_run=None,
    )


def test_published_projection_preserves_original_receipt_without_requalifying_it(historical_projection_input: JsonObject) -> None:
    receipt = historical_projection_input
    original = deepcopy(receipt)
    projected = _project(receipt)
    assert projected["latest_test_run_id"] == receipt["test_run_id"]
    assert projected["latest_test_run"] == receipt
    assert projected["publication_blocker"] is None
    assert (
        matching_passed_test_run(
            receipt,
            agent_id=_intent().agent_id,
            commit_sha=_intent().commit_sha,
            change_set_id=_intent().change_set_id,
        )
        is None
    )
    assert receipt == original


@pytest.mark.parametrize("status", ["candidate_committed", "pending_approval", "approved", "publishing"])
def test_unfinished_candidate_never_uses_historical_receipt_gate(historical_projection_input: JsonObject, status: str) -> None:
    projected = _project(historical_projection_input, status=status)
    assert projected["latest_test_run_id"] is None
    assert projected["publication_blocker"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", "another-agent"),
        ("commit_sha", "e" * 40),
        ("change_set_id", "agc-other"),
        ("test_run_id", "atr-other"),
        ("suite_digest", "f" * 64),
        ("status", "error"),
        ("source", "manual"),
        ("items", []),
    ],
)
def test_published_projection_requires_exact_original_receipt(historical_projection_input: JsonObject, field: str, value: object) -> None:
    projected = _project({**historical_projection_input, field: value})
    assert projected["latest_test_run"] is None


@pytest.mark.parametrize("conflict", ["exit", "failed_item", "duplicate_item", "failed_phase", "invocation_error", "invocation_owner"])
def test_historical_report_cannot_contradict_recorded_success(historical_projection_input: JsonObject, conflict: str) -> None:
    receipt = deepcopy(historical_projection_input)
    report = receipt["report"]
    if conflict == "exit":
        report["exit_code"] = 1
    elif conflict == "failed_item":
        report["items"][0]["outcome"] = "failed"
    elif conflict == "duplicate_item":
        report["items"].append(deepcopy(report["items"][0]))
    elif conflict == "failed_phase":
        report["items"][0]["phase_outcomes"] = {"teardown": "failed"}
    elif conflict == "invocation_error":
        report["invocations"] = [{"errors": ["known failure"]}]
    else:
        report["invocations"] = [{"test_run_id": "atr-foreign", "errors": []}]
    assert _project(receipt)["latest_test_run"] is None


def test_history_projection_does_not_clear_quarantine_or_guess_unbound_test(historical_projection_input: JsonObject) -> None:
    quarantine = {"detail": "Git identity requires review"}
    projected = _project(historical_projection_input, extra={"legacy_publication_quarantine": quarantine})
    assert projected["legacy_publication_quarantine"] == quarantine
    assert projected["publication_blocker"] == quarantine["detail"]
    assert _project(historical_projection_input, extra={"publication_intent": None})["latest_test_run"] is None


@pytest.fixture
def completed_git_identity(tmp_path: Path):
    workspace = tmp_path / "workspace"
    create_test_business_agent_workspace(workspace, agent_id=_intent().agent_id, name="Receipt identity")
    git_store = GitAgentVersionStore(repository_dir=workspace, worktrees_dir=tmp_path / "worktrees", releases_dir=tmp_path / "releases")
    git_store.ensure_bootstrap()
    worktree = git_store.create_worktree(_intent().change_set_id)
    (worktree.worktree_path / "receipt-note.md").write_text("真实临时 Git 发布身份，不执行 Agent。\n", encoding="utf-8")
    candidate = git_store.commit_worktree(worktree.worktree_path, message="提交临时身份契约")
    diff = git_store.diff_versions(worktree.base_commit_sha, candidate)
    assert diff is not None
    intent = replace(
        _intent(),
        commit_sha=candidate,
        previous_commit_sha=worktree.base_commit_sha,
        diff_digest=candidate_diff_digest(diff),
        force=True,
        test_run_id=None,
        suite_digest=None,
        previous_status="candidate_committed",
        note="仅测试 Git 发布身份",
        force_publication_blocker="No Agent test executed",
    )
    published = git_store.publish_commit(candidate, tag_name=intent.tag_name, message="发布临时 Git 身份")
    factory = make_session_factory(tmp_path / "records.sqlite3")
    with factory.begin() as db:
        row = _row(intent)
        row.payload_json = {**row.payload_json, "publication_blocker": intent.force_publication_blocker}
        db.add(row)
        payload = release_payload(intent, archive=published["archive"], created_at=intent.started_at, updated_at=intent.started_at)
        db.add(
            AgentReleaseModel(
                release_id=intent.release_id,
                agent_id=intent.agent_id,
                change_set_id=intent.change_set_id,
                status="published",
                tag_name=intent.tag_name,
                commit_sha=intent.commit_sha,
                created_at=intent.started_at,
                payload_json=payload,
            )
        )
        db.add(
            AgentReleaseTagClaimModel(
                agent_id=intent.agent_id,
                tag_name=intent.tag_name,
                change_set_id=intent.change_set_id,
                release_id=intent.release_id,
            )
        )
    yield factory, git_store, intent
    factory.kw["bind"].dispose()


def test_completed_git_identity_is_readable_repeatedly_without_creating_test_receipts(completed_git_identity) -> None:
    factory, git_store, intent = completed_git_identity
    with factory() as db:
        row = db.get(AgentChangeSetModel, intent.change_set_id)
        original = deepcopy(row.payload_json)
        for _ in range(2):
            require_publication_intent_evidence(db, row=row, intent=intent, store=git_store)
        assert row.payload_json == original
        assert len(db.scalars(select(AgentReleaseModel)).all()) == 1
        assert git_store.current_commit_sha() == intent.commit_sha
        with pytest.raises(CandidateApprovalFailure, match="no exact identity"):
            require_recorded_publication_test(
                db,
                agent_id=intent.agent_id,
                commit_sha=intent.commit_sha,
                change_set_id=intent.change_set_id,
                test_run_id="",
                suite_digest="",
            )


@pytest.mark.parametrize("conflict", ["missing_release", "duplicate_release", "release_owner", "rollback", "git_tag", "git_unavailable"])
def test_published_state_alone_cannot_bypass_completed_git_identity(completed_git_identity, conflict: str) -> None:
    factory, git_store, intent = completed_git_identity
    with factory.begin() as db:
        row = db.get(AgentChangeSetModel, intent.change_set_id)
        release = db.get(AgentReleaseModel, intent.release_id)
        if conflict == "missing_release":
            db.delete(release)
        elif conflict == "duplicate_release":
            db.add(
                AgentReleaseModel(
                    release_id="agr-duplicate",
                    agent_id=intent.agent_id,
                    change_set_id=intent.change_set_id,
                    status="published",
                    tag_name="another-tag",
                    commit_sha=intent.commit_sha,
                    payload_json={},
                )
            )
        elif conflict == "release_owner":
            release.agent_id = "another-agent"
        elif conflict == "rollback":
            release.rollback_of_release_id = "agr-original"
        elif conflict == "git_tag":
            intent = replace(intent, tag_name="missing-tag")
            release.tag_name = intent.tag_name
            release.payload_json = {**release.payload_json, "tag_name": intent.tag_name}
        else:
            (git_store.repository_dir / ".git").rename(git_store.repository_dir / ".git-unavailable")
        db.flush()
        expected = "live Git/tag identity" if conflict.startswith("git_") else "Completed publication release identity"
        with pytest.raises(AgentGovernanceError, match=expected) as exc:
            require_publication_intent_evidence(db, row=row, intent=intent, store=git_store)
        assert exc.value.status_code == 409


@pytest.mark.parametrize("status", ["published", "publishing"])
def test_force_approval_still_requires_its_own_exact_test_receipt(completed_git_identity, status: str) -> None:
    factory, git_store, intent = completed_git_identity
    intent = replace(intent, previous_status="approved")
    with factory() as db:
        row = db.get(AgentChangeSetModel, intent.change_set_id)
        row.status = status
        row.payload_json = {**row.payload_json, "approval_evidence": {"test_run_id": "atr-missing", "suite_digest": "d" * 64}}
        with pytest.raises(AgentGovernanceError, match="Force publication approval test evidence is stale"):
            require_publication_intent_evidence(db, row=row, intent=intent, store=git_store)
