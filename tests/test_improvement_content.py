"""四阶段改进治理 P3：系统理解 NormalizedFeedback + 归因 Attribution 内容子资源（store + API）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.errors import BusinessRuleViolation, ConflictError
from app.runtime.improvement_db import ImprovementFeedbackCaseAssignmentModel, ImprovementFeedbackModel
from app.runtime.runtime_db import AgentChangeSetModel, make_session_factory
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.generated_agent_tests import build_generated_agent_test
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID
from feedback_store_test_utils import _seed_execution_record


def _store(tmp_path: Path) -> ImprovementContentStore:
    return ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def _regression_test_payload(improvement_id: str, prompt: str = "case") -> dict[str, str]:
    return build_generated_agent_test(
        improvement_id=improvement_id,
        index=1,
        test_code=(
            "def test_regression(agent):\n"
            f"    result = agent.run({prompt!r})\n"
            "    assert not result.errors\n"
            "    normalized_text = ''.join(result.text.split())\n"
            f"    assert {prompt!r} in normalized_text\n"
        ),
        test_intent=f"验证 {prompt}",
        assertion_rationale="回答必须包含反馈中的关键业务词",
    ).to_payload()


def _create_feedback_case(module, *, agent_id: str) -> dict:
    if module.agent_registry_store.get_agent(agent_id) is None:
        workspace_dir = module.settings.data_dir / "business-agents" / agent_id / "workspace"
        module.agent_registry_store.create_business_agent(
            name=agent_id,
            agent_id=agent_id,
            workspace_dir=str(workspace_dir),
        )
    run_id = f"run-{agent_id}"
    module.feedback_store.record_run({"run_id": run_id, "agent_id": agent_id, "created_at": "2026-07-10T00:00:00Z"})
    signal = module.feedback_store.create_signal(FeedbackSignalCreateRequest(run_id=run_id, labels=["tool_data_incomplete"]))
    feedback_case = module.feedback_store.create_case(
        source_refs=[("signal", signal["signal_id"])],
        title=f"{agent_id} feedback",
    )
    assert feedback_case is not None
    return feedback_case


def test_reassign_feedback_and_delete_improvement_cascade(tmp_path: Path) -> None:
    """Part B：跨事项调整（reassign）移动反馈；删除事项级联删反馈/内容，A 不受影响。"""
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    items = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    a = items.create_improvement(agent_id=ORDINARY_TEST_AGENT_ID, title="事项A")
    b = items.create_improvement(agent_id=ORDINARY_TEST_AGENT_ID, title="事项B")
    fb = content.create_feedback(a.improvement_id, agent_id=ORDINARY_TEST_AGENT_ID, summary="反馈一")

    # reassign：把 A 的反馈移到 B（跨事项调整）。
    moved = content.reassign_feedback(
        fb.feedback_id,
        source_improvement_id=a.improvement_id,
        target_improvement_id=b.improvement_id,
    )
    assert moved.improvement_id == b.improvement_id
    assert content.count_feedbacks(a.improvement_id) == 0  # A 被清空
    assert content.count_feedbacks(b.improvement_id) == 1
    # attachable：从 A 视角能看到 B 的反馈作为可调整来源。
    attachable = content.list_attachable_feedbacks(agent_id=ORDINARY_TEST_AGENT_ID, exclude_improvement_id=a.improvement_id)
    assert any(f.feedback_id == fb.feedback_id for f in attachable)

    # deletion_impact + 硬删除：删 B，其反馈随删；A 仍在。
    impact = items.deletion_impact(b.improvement_id)
    assert impact.feedbacks == 1
    items.delete_improvement(b.improvement_id)
    assert items.get_improvement(b.improvement_id) is None
    assert content.count_feedbacks(b.improvement_id) == 0
    assert items.get_improvement(a.improvement_id) is not None


def test_part_b_reassign_attachable_delete_endpoints(monkeypatch, tmp_path: Path) -> None:
    """Part B API：reassign / attachable / deletion-impact / DELETE 端到端。"""
    module = _load_app(monkeypatch, tmp_path, extra_agent_ids=(ORDINARY_TEST_AGENT_ID,))
    with TestClient(module.app) as client:
        a = client.post("/api/improvements", json={"agent_id": ORDINARY_TEST_AGENT_ID, "title": "事项A"}).json()
        b = client.post("/api/improvements", json={"agent_id": ORDINARY_TEST_AGENT_ID, "title": "事项B"}).json()
        fb = client.post(f"/api/improvements/{a['improvement_id']}/feedbacks", json={"summary": "反馈一"}).json()

        # 跨事项调整：A 的反馈 reassign 到 B。
        moved = client.post(
            f"/api/improvements/{a['improvement_id']}/feedbacks/{fb['feedback_id']}/reassign",
            json={"target_improvement_id": b["improvement_id"]},
        )
        assert moved.status_code == 200 and moved.json()["improvement_id"] == b["improvement_id"]
        # 从 A 视角 attachable 能看到 B 的反馈（其他事项源）。
        attach = client.get(f"/api/improvements/{a['improvement_id']}/attachable-feedbacks").json()
        assert any(f["feedback_id"] == fb["feedback_id"] for f in attach["other_improvement_feedbacks"])

        # deletion-impact + 硬删除 B：反馈随删；A 仍在。
        impact = client.get(f"/api/improvements/{b['improvement_id']}/deletion-impact").json()
        assert impact["feedbacks"] == 1
        assert client.delete(f"/api/improvements/{b['improvement_id']}").status_code == 204
        assert client.get(f"/api/improvements/{b['improvement_id']}").status_code == 404
        assert client.get(f"/api/improvements/{a['improvement_id']}").status_code == 200


def test_reassign_feedback_rejects_foreign_feedback_id_without_side_effects(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        alpha_source = client.post("/api/improvements", json={"agent_id": "agent-alpha", "title": "Alpha 来源"}).json()
        alpha_target = client.post("/api/improvements", json={"agent_id": "agent-alpha", "title": "Alpha 目标"}).json()
        alpha_other = client.post("/api/improvements", json={"agent_id": "agent-alpha", "title": "Alpha 其他来源"}).json()
        beta_source = client.post("/api/improvements", json={"agent_id": "agent-beta", "title": "Beta 来源"}).json()
        same_agent_foreign = client.post(
            f"/api/improvements/{alpha_other['improvement_id']}/feedbacks",
            json={"summary": "Other Alpha feedback"},
        ).json()
        foreign = client.post(
            f"/api/improvements/{beta_source['improvement_id']}/feedbacks",
            json={"summary": "Beta feedback"},
        ).json()

        wrong_source_response = client.post(
            f"/api/improvements/{alpha_source['improvement_id']}/feedbacks/{same_agent_foreign['feedback_id']}/reassign",
            json={"target_improvement_id": alpha_target["improvement_id"]},
        )
        cross_agent_response = client.post(
            f"/api/improvements/{alpha_source['improvement_id']}/feedbacks/{foreign['feedback_id']}/reassign",
            json={"target_improvement_id": alpha_target["improvement_id"]},
        )
        alpha_other_feedbacks = client.get(f"/api/improvements/{alpha_other['improvement_id']}/feedbacks").json()
        beta_feedbacks = client.get(f"/api/improvements/{beta_source['improvement_id']}/feedbacks").json()
        alpha_feedbacks = client.get(f"/api/improvements/{alpha_target['improvement_id']}/feedbacks").json()

    assert wrong_source_response.status_code == 409
    assert wrong_source_response.json()["error_code"] == "CONFLICT"
    assert cross_agent_response.status_code == 400
    assert cross_agent_response.json()["error_code"] == "BUSINESS_RULE_VIOLATION"
    assert [item["feedback_id"] for item in alpha_other_feedbacks] == [same_agent_foreign["feedback_id"]]
    assert [item["feedback_id"] for item in beta_feedbacks] == [foreign["feedback_id"]]
    assert alpha_feedbacks == []


def test_attach_feedback_case_accepts_same_business_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    feedback_case = _create_feedback_case(module, agent_id="soc-ops")

    with TestClient(module.app) as client:
        improvement = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "反馈归因"}).json()
        attachable = client.get(f"/api/improvements/{improvement['improvement_id']}/attachable-feedbacks").json()
        attached = client.post(
            f"/api/improvements/{improvement['improvement_id']}/attach-feedback-case",
            json={"feedback_case_id": feedback_case["feedback_case_id"]},
        )

    assert [item["feedback_case_id"] for item in attachable["feedback_cases"]] == [feedback_case["feedback_case_id"]]
    assert attached.status_code == 201
    assert attached.json()["agent_id"] == "soc-ops"
    assert attached.json()["case_id"] == feedback_case["feedback_case_id"]


def test_generic_feedback_api_rejects_feedback_case_semantics_without_side_effects(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    feedback_case = _create_feedback_case(module, agent_id="soc-ops")
    case_id = feedback_case["feedback_case_id"]

    with TestClient(module.app) as client:
        source = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "来源"}).json()
        target = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "目标"}).json()
        source_id = source["improvement_id"]
        target_id = target["improvement_id"]

        forged = client.post(
            f"/api/improvements/{source_id}/feedbacks",
            json={"summary": "伪造挂接", "source": "feedback_inbox", "case_id": case_id},
        )
        disguised = client.post(
            f"/api/improvements/{source_id}/feedbacks",
            json={"summary": "伪装来源", "source": "trace", "case_id": case_id},
        )

        assert forged.status_code == 422
        assert disguised.status_code == 422
        assert "attach-feedback-case" in str(forged.json())
        assert "attach-feedback-case" in str(disguised.json())
        assert client.get(f"/api/improvements/{source_id}/feedbacks").json() == []
        assert client.get(f"/api/improvements/{source_id}").json()["source_feedback_refs"] == []
        with module.runtime_db_session_factory.begin() as db:
            assert db.query(ImprovementFeedbackModel).count() == 0
            assert db.query(ImprovementFeedbackCaseAssignmentModel).count() == 0

        attached = client.post(
            f"/api/improvements/{source_id}/attach-feedback-case",
            json={"feedback_case_id": case_id},
        )
        duplicate = client.post(
            f"/api/improvements/{target_id}/attach-feedback-case",
            json={"feedback_case_id": case_id},
        )

    with module.runtime_db_session_factory.begin() as db:
        feedback_rows = db.query(ImprovementFeedbackModel).all()
        assignment_rows = db.query(ImprovementFeedbackCaseAssignmentModel).all()

    assert attached.status_code == 201
    assert duplicate.status_code == 409
    assert len(feedback_rows) == 1
    assert len(assignment_rows) == 1
    assert assignment_rows[0].feedback_case_id == case_id
    assert assignment_rows[0].feedback_id == feedback_rows[0].feedback_id
    assert module.improvement_store.get_improvement(source_id).source_feedback_refs == [case_id]
    assert module.improvement_store.get_improvement(target_id).source_feedback_refs == []


def test_generic_feedback_store_rejects_feedback_case_semantics_without_side_effects(tmp_path: Path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    items = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    source = items.create_improvement(agent_id="soc-ops", title="来源")
    target = items.create_improvement(agent_id="soc-ops", title="目标")

    for source_kind, case_id in (("feedback_inbox", "ordinary-case"), ("trace", "fbc-store-guard")):
        with pytest.raises(BusinessRuleViolation, match="attach-feedback-case"):
            content.create_feedback(
                source.improvement_id,
                agent_id="soc-ops",
                summary="伪造挂接",
                source=source_kind,
                case_id=case_id,
            )

    with factory.begin() as db:
        assert db.query(ImprovementFeedbackModel).count() == 0
        assert db.query(ImprovementFeedbackCaseAssignmentModel).count() == 0
    assert items.get_improvement(source.improvement_id).source_feedback_refs == []
    assert items.get_improvement(target.improvement_id).source_feedback_refs == []

    attached = content.attach_feedback_case(
        source.improvement_id,
        agent_id="soc-ops",
        feedback_case_id="fbc-store-guard",
        summary="正式挂接",
    )
    with pytest.raises(ConflictError, match="already assigned"):
        content.attach_feedback_case(
            target.improvement_id,
            agent_id="soc-ops",
            feedback_case_id="fbc-store-guard",
            summary="重复挂接",
        )

    with factory.begin() as db:
        assert db.query(ImprovementFeedbackModel).count() == 1
        assignment = db.get(ImprovementFeedbackCaseAssignmentModel, "fbc-store-guard")
        assert assignment is not None
        assert assignment.feedback_id == attached.feedback_id
    assert items.get_improvement(source.improvement_id).source_feedback_refs == ["fbc-store-guard"]
    assert items.get_improvement(target.improvement_id).source_feedback_refs == []


def test_feedback_case_assignment_is_unique_and_reassign_moves_authoritative_ref(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    feedback_case = _create_feedback_case(module, agent_id="soc-ops")
    case_id = feedback_case["feedback_case_id"]

    with TestClient(module.app) as client:
        source = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "来源"}).json()
        target = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "目标"}).json()
        attached = client.post(
            f"/api/improvements/{source['improvement_id']}/attach-feedback-case",
            json={"feedback_case_id": case_id},
        )
        duplicate = client.post(
            f"/api/improvements/{target['improvement_id']}/attach-feedback-case",
            json={"feedback_case_id": case_id},
        )
        moved = client.post(
            f"/api/improvements/{source['improvement_id']}/feedbacks/{attached.json()['feedback_id']}/reassign",
            json={"target_improvement_id": target["improvement_id"]},
        )
        source_after = client.get(f"/api/improvements/{source['improvement_id']}").json()
        target_after = client.get(f"/api/improvements/{target['improvement_id']}").json()
        source_feedbacks = client.get(f"/api/improvements/{source['improvement_id']}/feedbacks").json()
        target_feedbacks = client.get(f"/api/improvements/{target['improvement_id']}/feedbacks").json()

        assert client.delete(f"/api/improvements/{source['improvement_id']}").status_code == 204
        still_assigned = client.get(f"/api/improvements/{target['improvement_id']}/attachable-feedbacks").json()
        assert client.delete(f"/api/improvements/{target['improvement_id']}").status_code == 204
        fresh = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "重新接收"}).json()
        unassigned_again = client.get(f"/api/improvements/{fresh['improvement_id']}/attachable-feedbacks").json()

    assert attached.status_code == 201
    assert duplicate.status_code == 409
    assert moved.status_code == 200 and moved.json()["improvement_id"] == target["improvement_id"]
    assert source_after["source_feedback_refs"] == []
    assert target_after["source_feedback_refs"] == [case_id]
    assert source_feedbacks == []
    assert [row["feedback_id"] for row in target_feedbacks] == [attached.json()["feedback_id"]]
    assert module.improvement_store.improvement_id_for_feedback_case(case_id) is None
    assert all(case["feedback_case_id"] != case_id for case in still_assigned["feedback_cases"])
    assert any(case["feedback_case_id"] == case_id for case in unassigned_again["feedback_cases"])


def test_merge_and_split_keep_feedback_case_assignment_and_feedback_row_colocated(tmp_path: Path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    items = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    target = items.create_improvement(agent_id="soc-ops", title="target")
    source = items.create_improvement(agent_id="soc-ops", title="source")
    attached = content.attach_feedback_case(
        source.improvement_id,
        agent_id="soc-ops",
        feedback_case_id="fbc-merge-split",
        summary="case",
    )

    merged = items.merge_improvements(target.improvement_id, source_id=source.improvement_id)
    assert merged.source_feedback_refs == ["fbc-merge-split"]
    assert items.improvement_id_for_feedback_case("fbc-merge-split") == target.improvement_id
    assert [row.feedback_id for row in content.list_feedbacks(target.improvement_id)] == [attached.feedback_id]
    assert content.list_feedbacks(source.improvement_id) == []

    split = items.split_improvement(target.improvement_id, feedback_ref="fbc-merge-split")
    assert items.improvement_id_for_feedback_case("fbc-merge-split") == split.improvement_id
    assert [row.feedback_id for row in content.list_feedbacks(split.improvement_id)] == [attached.feedback_id]
    assert content.list_feedbacks(target.improvement_id) == []


def test_attach_feedback_case_rejects_cross_business_agent_without_side_effects(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    foreign_case = _create_feedback_case(module, agent_id="agent-alpha")
    local_case = _create_feedback_case(module, agent_id="agent-beta")

    with TestClient(module.app) as client:
        improvement = client.post("/api/improvements", json={"agent_id": "agent-beta", "title": "Beta 反馈治理"}).json()
        improvement_id = improvement["improvement_id"]
        attachable = client.get(f"/api/improvements/{improvement_id}/attachable-feedbacks").json()
        response = client.post(
            f"/api/improvements/{improvement_id}/attach-feedback-case",
            json={"feedback_case_id": foreign_case["feedback_case_id"]},
        )
        feedbacks = client.get(f"/api/improvements/{improvement_id}/feedbacks").json()

    assert [item["feedback_case_id"] for item in attachable["feedback_cases"]] == [local_case["feedback_case_id"]]
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Cannot attach feedback case across different business agents",
        "error_code": "BUSINESS_RULE_VIOLATION",
    }
    assert feedbacks == []
    assert module.improvement_store.get_improvement(improvement_id).source_feedback_refs == []


def test_normalized_feedback_upsert_is_1to1_and_confirmable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.upsert_normalized_feedback("imp-1", problem="告警误报", user_quote="这是误报")
    b = store.upsert_normalized_feedback("imp-1", problem="告警误报(改)", possible_reason="时间不一致")
    # 1:1：同一 improvement 复用同一行（id 不变）。
    assert a.normalized_feedback_id == b.normalized_feedback_id
    assert store.get_normalized_feedback("imp-1").problem == "告警误报(改)"
    assert store.get_normalized_feedback("imp-1").status == "draft"
    confirmed = store.set_normalized_feedback_status("imp-1", status="confirmed")
    assert confirmed.status == "confirmed"
    with pytest.raises(BusinessRuleViolation):
        store.set_normalized_feedback_status("imp-1", status="bogus")
    with pytest.raises(BusinessRuleViolation):
        store.set_normalized_feedback_status("imp-none", status="confirmed")


def test_artifact_and_stage_roll_back_together_when_stage_write_fails(tmp_path: Path, monkeypatch) -> None:
    import app.runtime.stores.improvement_content_store as content_store_module

    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    items = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    item = items.create_improvement(agent_id=ORDINARY_TEST_AGENT_ID, title="原子产物")

    def fail_stage(db, improvement_id, *, stage):
        raise RuntimeError("injected stage failure")

    monkeypatch.setattr(content_store_module, "advance_improvement_stage_in_transaction", fail_stage)
    with pytest.raises(RuntimeError, match="injected stage failure"):
        content.upsert_normalized_feedback(
            item.improvement_id,
            problem="不应部分提交",
            advance_to_stage="triage",
        )

    assert content.get_normalized_feedback(item.improvement_id) is None
    unchanged = items.get_improvement(item.improvement_id)
    assert unchanged is not None
    assert unchanged.improvement_stage == "feedback_intake"


def test_refinement_invalidates_downstream_artifacts_and_stale_confirm_cannot_advance(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        item = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "返工失效链"},
        ).json()
        improvement_id = item["improvement_id"]
        content = module.improvement_content_store
        content.upsert_normalized_feedback(improvement_id, problem="误报", advance_to_stage="triage")
        content.upsert_attribution(improvement_id, summary="数据问题", advance_to_stage="attribution")
        content.upsert_optimization_plan(
            improvement_id,
            summary="修正数据",
            changes=[{"target": "prompt", "change": "x"}],
            advance_to_stage="optimization",
        )
        _seed_execution_record(
            content,
            improvement_id,
            summary="旧执行",
            changes_applied=["prompt"],
            agent_version="old-version",
            change_set_id="old-change-set",
            advance_to_stage="execution",
        )
        content.upsert_regression_test_design(
            improvement_id,
            summary="旧回归",
            tests=[
                build_generated_agent_test(
                    improvement_id=improvement_id,
                    index=1,
                    test_code=(
                        "def test_old(agent):\n"
                        "    result = agent.run('old')\n"
                        "    assert not result.errors\n"
                        "    normalized_text = ''.join(result.text.split())\n"
                        "    assert 'old' in normalized_text\n"
                    ),
                    test_intent="旧回归",
                    assertion_rationale="验证 old 结果",
                ).to_payload()
            ],
            advance_to_stage="regression",
        )
        with module.runtime_db_session_factory.begin() as db:
            db.add(
                AgentChangeSetModel(
                    change_set_id="old-change-set",
                    agent_id="soc-ops",
                    status="candidate_committed",
                    execution_job_id=None,
                    base_commit_sha="base-sha",
                    candidate_commit_sha="candidate-sha",
                    branch_name="candidate/old",
                    worktree_path="/tmp/old-change-set",
                    payload_json={},
                )
            )
        module.improvement_store.add_link(improvement_id, kind="change_set", ref_id="old-change-set")
        module.improvement_store.add_link(improvement_id, kind="test_run", ref_id="old-test-run")

        blocked = client.post(
            f"/api/improvements/{improvement_id}/lifecycle",
            json={"stage": "optimization"},
        )
        with module.runtime_db_session_factory.begin() as db:
            db.get(AgentChangeSetModel, "old-change-set").status = "abandoned"
        refined = client.post(
            f"/api/improvements/{improvement_id}/lifecycle",
            json={"stage": "optimization"},
        )
        stale_confirm = client.post(f"/api/improvements/{improvement_id}/regression-test-design/confirm")
        links = client.get(f"/api/improvements/{improvement_id}/links").json()
        after = client.get(f"/api/improvements/{improvement_id}").json()

    assert blocked.status_code == 409
    assert refined.status_code == 200
    assert refined.json()["improvement_stage"] == "optimization"
    assert content.get_optimization_plan(improvement_id) is not None
    assert content.get_execution(improvement_id) is None
    assert content.get_regression_test_design(improvement_id) is None
    assert stale_confirm.status_code == 400
    assert links == []
    assert after["improvement_stage"] == "optimization"


def test_advanced_item_rejects_upstream_artifact_and_source_scope_mutation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        item = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "阶段围栏"}).json()
        improvement_id = item["improvement_id"]
        content = module.improvement_content_store
        content.upsert_normalized_feedback(improvement_id, problem="P1", advance_to_stage="triage")
        content.set_normalized_feedback_status(improvement_id, status="confirmed")
        content.upsert_attribution(improvement_id, summary="A1", advance_to_stage="attribution")
        content.set_attribution_status(improvement_id, status="confirmed")
        content.upsert_optimization_plan(
            improvement_id,
            summary="O1",
            changes=[{"target": "prompt", "change": "x"}],
            advance_to_stage="optimization",
        )
        content.set_optimization_plan_status(improvement_id, status="confirmed")
        _seed_execution_record(
            content,
            improvement_id,
            summary="E1",
            changes_applied=["prompt"],
            agent_version="v1",
            advance_to_stage="execution",
        )
        content.set_execution_status(improvement_id, status="confirmed")
        content.upsert_regression_test_design(
            improvement_id,
            summary="R1",
            tests=[_regression_test_payload(improvement_id, "r1")],
            advance_to_stage="regression",
        )

        replace_nf = client.put(
            f"/api/improvements/{improvement_id}/normalized-feedback",
            json={"problem": "P2"},
        )
        replace_attr = client.put(
            f"/api/improvements/{improvement_id}/attribution",
            json={"summary": "A2", "responsibility_boundary": [], "evidence": []},
        )
        replace_plan = client.put(
            f"/api/improvements/{improvement_id}/optimization-plan",
            json={"summary": "O2", "changes": [{"target": "prompt", "change": "y"}]},
        )
        add_feedback = client.post(
            f"/api/improvements/{improvement_id}/feedbacks",
            json={"summary": "late feedback"},
        )
        after = client.get(f"/api/improvements/{improvement_id}").json()

    assert [replace_nf.status_code, replace_attr.status_code, replace_plan.status_code, add_feedback.status_code] == [409, 409, 409, 409]
    assert content.get_normalized_feedback(improvement_id).problem == "P1"
    assert content.get_attribution(improvement_id).summary == "A1"
    assert content.get_optimization_plan(improvement_id).summary == "O1"
    assert content.list_feedbacks(improvement_id) == []
    assert after["improvement_stage"] == "regression"


def test_attribution_upsert_and_confirm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_attribution("imp-1", summary="MCP 数据时间不一致", responsibility_boundary=["不是主 Agent 推理错误"], evidence=["list_events 时间窗口不一致"])
    got = store.get_attribution("imp-1")
    assert got.summary == "MCP 数据时间不一致" and got.responsibility_boundary == ["不是主 Agent 推理错误"]
    assert store.set_attribution_status("imp-1", status="confirmed").status == "confirmed"


def test_structural_artifacts_reject_whitespace_only_business_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BusinessRuleViolation, match="non-empty target and change"):
        store.upsert_optimization_plan(
            "imp-1",
            summary="plan",
            changes=[{"target": "  ", "change": "  "}],
        )
    with pytest.raises(BusinessRuleViolation, match="require target_path"):
        store.upsert_regression_test_design(
            "imp-1",
            summary="regression",
            tests=[{"target_path": "  "}],
        )


def test_content_api_lifecycle(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        # 系统理解 upsert → get → confirm
        assert client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "告警误报", "possible_reason": "时间不一致"}).status_code == 200
        assert client.get(f"/api/improvements/{iid}/normalized-feedback").json()["problem"] == "告警误报"
        assert client.post(f"/api/improvements/{iid}/normalized-feedback/confirm").json()["status"] == "confirmed"
        # 归因 upsert → get
        attr = client.put(
            f"/api/improvements/{iid}/attribution", json={"summary": "MCP 数据问题", "responsibility_boundary": ["不是主 Agent"], "evidence": ["e1"]}
        )
        assert attr.status_code == 200 and attr.json()["responsibility_boundary"] == ["不是主 Agent"]
        assert client.get(f"/api/improvements/{iid}").json()["improvement_stage"] == "attribution"
        # 未知改进事项 404；无内容 get 404
        assert client.put("/api/improvements/imp-none/normalized-feedback", json={"problem": "x"}).status_code == 404
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "无内容"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/attribution").status_code == 404


def test_feedback_table_create_and_list(monkeypatch, tmp_path: Path) -> None:
    """四阶段改进治理 §8.4：来源反馈一等内容（摘要/来源/状态），1:多，未知事项 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        a = client.post(
            f"/api/improvements/{iid}/feedbacks",
            json={
                "summary": "这是误报",
                "source": "playground_run",
                "raw_text": "原文",
                "run_id": "run-1",
                "session_id": "session-1",
                "agent_version_id": "agent-v1",
                "scenario": "alert-triage",
                "task_id": "task-1",
                "alert_id": "alert-1",
                "case_id": "case-1",
            },
        )
        assert a.status_code == 201 and a.json()["status"] == "merged" and a.json()["run_id"] == "run-1"
        assert a.json()["agent_version_id"] == "agent-v1"
        assert a.json()["scenario"] == "alert-triage"
        assert a.json()["task_id"] == "task-1"
        assert a.json()["alert_id"] == "alert-1"
        assert a.json()["case_id"] == "case-1"
        client.post(f"/api/improvements/{iid}/feedbacks", json={"summary": "MCP 数据像模拟", "source": "trace"})
        rows = client.get(f"/api/improvements/{iid}/feedbacks").json()
        assert {r["summary"] for r in rows} == {"这是误报", "MCP 数据像模拟"}
        assert {r["source"] for r in rows} == {"playground_run", "trace"}
        assert {r["agent_version_id"] for r in rows} == {"agent-v1", ""}
        assert client.post("/api/improvements/imp-none/feedbacks", json={"summary": "x"}).status_code == 404


def test_optimization_plan_and_execution(monkeypatch, tmp_path: Path) -> None:
    """四阶段改进治理 §106/§107：优化方案 + 执行记录 1:1 子资源，upsert→get→confirm，未知事项/无内容 404。"""
    module = _load_app(monkeypatch, tmp_path)

    async def apply_execution(improvement_id: str):
        return _seed_execution_record(
            module.improvement_content_store,
            improvement_id,
            summary="已应用并生成版本",
            changes_applied=["prompt 更新"],
            agent_version="v1.2.0",
            generated_by="governor",
            change_set_id="agc-test",
            applied_agent_version_id="v1.2.0",
            applied_diff={"changed_files": ["CLAUDE.md"]},
            advance_to_stage="execution",
        )

    monkeypatch.setattr(module.improvement_execution_service, "generate_and_apply_execution", apply_execution)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "误报治理"}).json()["improvement_id"]
        client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "告警误报"})
        client.post(f"/api/improvements/{iid}/normalized-feedback/confirm")
        client.put(
            f"/api/improvements/{iid}/attribution",
            json={"summary": "数据时间不一致", "responsibility_boundary": [], "evidence": []},
        )
        client.post(f"/api/improvements/{iid}/attribution/confirm")
        # 优化方案
        op = client.put(
            f"/api/improvements/{iid}/optimization-plan",
            json={"summary": "收紧时间一致性校验", "changes": [{"target": "prompt", "change": "新增时间校验指令"}]},
        )
        assert op.status_code == 200 and op.json()["changes"][0]["target"] == "prompt" and op.json()["status"] == "draft"
        assert client.post(f"/api/improvements/{iid}/optimization-plan/confirm").json()["status"] == "confirmed"
        # 执行记录
        ex = client.post(f"/api/improvements/{iid}/execution/apply")
        assert ex.status_code == 200 and ex.json()["agent_version"] == "v1.2.0"
        assert client.post(f"/api/improvements/{iid}/execution/confirm").json()["status"] == "confirmed"
        assert client.get(f"/api/improvements/{iid}").json()["improvement_stage"] == "execution"
        # 未知事项 / 无内容 404
        assert (
            client.put(
                "/api/improvements/imp-none/optimization-plan",
                json={"summary": "x", "changes": [{"target": "prompt", "change": "x"}]},
            ).status_code
            == 404
        )
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "空"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/execution").status_code == 404


def test_backend_generates_initial_attribution_and_plan(monkeypatch, tmp_path: Path) -> None:
    """P2：归因/方案生成走后端治理端点，不由浏览器拼接后直接 upsert。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        client.put(
            f"/api/improvements/{iid}/normalized-feedback",
            json={
                "problem": "告警误报",
                "possible_reason": "事件时间与告警时间窗口不一致",
                "possible_object": "sec-ops-data MCP 数据",
                "suggestion": "进入归因和回归保障",
                "user_quote": "这个横向移动告警其实是误报",
            },
        )
        client.post(f"/api/improvements/{iid}/normalized-feedback/confirm")

        attr = client.post(f"/api/improvements/{iid}/attribution/generate")
        assert attr.status_code == 200
        assert "sec-ops-data MCP 数据" in attr.json()["summary"]
        assert attr.json()["evidence"] == ["用户反馈：这个横向移动告警其实是误报"]
        client.post(f"/api/improvements/{iid}/attribution/confirm")

        plan = client.post(f"/api/improvements/{iid}/optimization-plan/generate")
        assert plan.status_code == 200
        assert plan.json()["changes"][0]["target"] == "prompt"
        assert "告警误报治理" in plan.json()["summary"]
        assert client.get(f"/api/improvements/{iid}").json()["improvement_stage"] == "optimization"


def test_failed_business_artifact_does_not_advance_stage(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)

    async def fail_attribution(_improvement_id: str, **_kwargs):
        raise BusinessRuleViolation("forced attribution failure")

    monkeypatch.setattr(module.improvement_governor_service, "generate_attribution", fail_attribution)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "失败不前推"}).json()["improvement_id"]
        assert client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "误报"}).status_code == 200
        assert client.post(f"/api/improvements/{iid}/normalized-feedback/confirm").status_code == 200
        failed = client.post(f"/api/improvements/{iid}/attribution/generate")
        item = client.get(f"/api/improvements/{iid}").json()

    assert failed.status_code == 400
    assert item["improvement_stage"] == "triage"
    assert module.improvement_content_store.get_attribution(iid) is None


def test_business_artifact_prerequisites_fail_closed(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "禁止跳产物"}).json()["improvement_id"]
        assert client.post(f"/api/improvements/{iid}/attribution/generate").status_code == 400
        assert (
            client.put(
                f"/api/improvements/{iid}/attribution",
                json={"summary": "绕过整理", "responsibility_boundary": [], "evidence": []},
            ).status_code
            == 400
        )
        assert client.post(f"/api/improvements/{iid}/optimization-plan/generate").status_code == 400
        assert client.post(f"/api/improvements/{iid}/execution/apply").status_code == 400
        assert client.post(f"/api/improvements/{iid}/regression-test-design/generate").status_code == 400
        item = client.get(f"/api/improvements/{iid}").json()

    assert item["improvement_stage"] == "feedback_intake"
    assert module.improvement_content_store.get_optimization_plan(iid) is None
    assert module.improvement_content_store.get_execution(iid) is None
    assert module.improvement_content_store.get_regression_test_design(iid) is None


def test_unapplied_execution_record_does_not_advance_or_unlock_regression(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)

    async def no_action_execution(improvement_id: str):
        return _seed_execution_record(
            module.improvement_content_store,
            improvement_id,
            summary="未自动应用：没有安全的可执行操作。",
            changes_applied=[],
            agent_version="",
            generated_by="heuristic",
        )

    monkeypatch.setattr(module.improvement_execution_service, "generate_and_apply_execution", no_action_execution)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "无动作不前推"}).json()["improvement_id"]
        client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "误报"})
        client.post(f"/api/improvements/{iid}/normalized-feedback/confirm")
        client.put(
            f"/api/improvements/{iid}/attribution",
            json={"summary": "数据问题", "responsibility_boundary": [], "evidence": []},
        )
        client.post(f"/api/improvements/{iid}/attribution/confirm")
        client.put(
            f"/api/improvements/{iid}/optimization-plan",
            json={"summary": "增加校验", "changes": [{"target": "prompt", "change": "校验时间"}]},
        )
        client.post(f"/api/improvements/{iid}/optimization-plan/confirm")
        no_action = client.post(f"/api/improvements/{iid}/execution/apply")
        manual_without_evidence = client.put(
            f"/api/improvements/{iid}/execution",
            json={"summary": "声称执行", "changes_applied": ["prompt"], "agent_version": ""},
        )
        regression = client.post(f"/api/improvements/{iid}/regression-test-design/generate")
        item = client.get(f"/api/improvements/{iid}").json()

    assert no_action.status_code == 200
    assert no_action.json()["changes_applied"] == []
    assert manual_without_evidence.status_code == 405
    assert regression.status_code == 400
    assert item["improvement_stage"] == "optimization"


def test_regression_test_design_generate_get_confirm(monkeypatch, tmp_path: Path) -> None:
    """生成代码、确认待发布 commit、运行测试是三个独立动作。"""
    module = _load_app(monkeypatch, tmp_path)

    async def apply_execution(improvement_id: str):
        return _seed_execution_record(
            module.improvement_content_store,
            improvement_id,
            summary="已执行",
            changes_applied=["prompt"],
            agent_version="v-test",
            generated_by="governor",
            change_set_id="agc-regression",
            applied_agent_version_id="v-test",
            applied_diff={"changed_files": ["CLAUDE.md"]},
            advance_to_stage="execution",
        )

    def materialize_regression_tests(improvement_id: str) -> dict:
        module.improvement_content_store.rebind_execution_candidate(
            improvement_id,
            change_set_id="agc-regression",
            previous_commit_sha="v-test",
            candidate_commit_sha="a" * 40,
            applied_diff={"changed_files": ["CLAUDE.md", "tests/test_feedback_regression.py"]},
            generated_test_files=["tests/test_feedback_regression.py"],
        )
        return {
            "agent_id": "soc-ops",
            "change_set_id": "agc-regression",
            "candidate_commit_sha": "a" * 40,
            "generated_test_files": ["tests/test_feedback_regression.py"],
        }

    async def generate_regression_test_design(improvement_id: str, *, advance_to_stage: str | None = None):
        candidate = build_generated_agent_test(
            improvement_id=improvement_id,
            index=1,
            test_code=(
                "def test_time_consistency(agent):\n"
                "    result = agent.run('仅依据以下已给定事实回答，不调用任何工具或读取文件。请判断告警是否应升级；回答必须包含核验。')\n"
                "    assert not result.errors\n"
                "    normalized_text = ''.join(result.text.split())\n"
                "    assert '核验' in normalized_text\n"
                "    assert result.raw['agent_activity']['tool_calls'] == []\n"
            ),
            test_intent="验证升级前核验时间",
            assertion_rationale="回答必须出现核验动作",
        )
        return module.improvement_content_store.upsert_regression_test_design(
            improvement_id,
            summary="生成可执行 pytest",
            tests=[candidate.to_payload()],
            generated_by="governor",
            advance_to_stage=advance_to_stage,
        )

    test_run = {
        "test_run_id": "atr-feedback",
        "agent_id": "soc-ops",
        "commit_sha": "a" * 40,
        "change_set_id": "agc-regression",
        "source": "feedback_optimization",
        "status": "queued",
        "cancel_requested": False,
        "created_at": "2026-07-18T00:00:00Z",
    }

    test_runs: list[dict] = []
    monkeypatch.setattr(module.improvement_execution_service, "generate_and_apply_execution", apply_execution)
    monkeypatch.setattr(module.improvement_execution_service, "materialize_regression_tests", materialize_regression_tests)
    monkeypatch.setattr(module.improvement_governor_service, "generate_regression_test_design", generate_regression_test_design)

    def create_change_set_run(_change_set_id: str) -> dict:
        test_runs.append(test_run)
        return test_run

    monkeypatch.setattr(module.agent_testing_service, "create_change_set_run", create_change_set_run)
    monkeypatch.setattr(module.agent_testing_service.store, "list_runs", lambda **_kwargs: list(test_runs))
    monkeypatch.setattr(module.agent_testing_service.store, "get_run", lambda _test_run_id: test_run)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "误报治理"}).json()["improvement_id"]
        client.post(
            f"/api/improvements/{iid}/feedbacks",
            json={"summary": "告警误报", "raw_text": "原始用户输入：请判断这条告警是否应升级处置。"},
        )
        client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "告警误报"})
        client.post(f"/api/improvements/{iid}/normalized-feedback/confirm")
        client.put(
            f"/api/improvements/{iid}/attribution",
            json={"summary": "数据时间不一致", "responsibility_boundary": [], "evidence": []},
        )
        client.post(f"/api/improvements/{iid}/attribution/confirm")
        client.put(
            f"/api/improvements/{iid}/optimization-plan",
            json={"summary": "增加校验", "changes": [{"target": "prompt", "change": "校验时间"}]},
        )
        client.post(f"/api/improvements/{iid}/optimization-plan/confirm")
        client.post(f"/api/improvements/{iid}/execution/apply")
        client.post(f"/api/improvements/{iid}/execution/confirm")
        gen = client.post(f"/api/improvements/{iid}/regression-test-design/generate")
        assert gen.status_code == 200 and gen.json()["generated_by"] == "governor" and gen.json()["tests"]
        assert client.get(f"/api/improvements/{iid}/regression-test-design").json()["status"] == "draft"
        confirmed = client.post(f"/api/improvements/{iid}/regression-test-design/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        assert confirmed.json()["generated_test_files"] == ["tests/test_feedback_regression.py"]
        assert confirmed.json()["test_run"] is None
        started = client.post("/api/agent-change-sets/agc-regression/test-runs")
        assert started.status_code == 202 and started.json()["status"] == "queued"
        refreshed = client.get(f"/api/improvements/{iid}/regression-test-design").json()
        assert refreshed["candidate_commit_sha"] == "a" * 40
        assert refreshed["generated_test_files"] == ["tests/test_feedback_regression.py"]
        assert refreshed["test_run"]["test_run_id"] == "atr-feedback"
        assert client.get(f"/api/improvements/{iid}").json()["improvement_stage"] == "regression"
        assert client.post("/api/improvements/imp-none/regression-test-design/generate").status_code == 404
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "空"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/regression-test-design").status_code == 404
