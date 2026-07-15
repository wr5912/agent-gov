from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.errors import BusinessRuleViolation, ConflictError, DataIntegrityError, NotFoundError
from app.runtime.improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementFeedbackModel,
    ImprovementItemModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionAssessmentModel,
)
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    make_session_factory,
    utc_now,
)
from app.runtime.runtime_db import (
    TestDatasetCaseModel as DatasetCaseModel,
)
from app.runtime.runtime_db import (
    TestDatasetModel as DatasetModel,
)
from app.runtime.runtime_db import TestDatasetRevisionModel as DatasetRevisionModel
from app.runtime.state_machines import StateTransitionError
from app.runtime.stores.test_dataset_store import TestDatasetStore as DatasetStore
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _seed_confirmed_chain(
    session_factory,
    *,
    improvement_id: str = "imp-dataset",
    agent_id: str = "soc-ops",
    feedback_agent_id: str | None = None,
) -> None:
    now = utc_now()
    with session_factory.begin() as db:
        db.add(
            ImprovementItemModel(
                improvement_id=improvement_id,
                agent_id=agent_id,
                title="时间窗口误判治理",
                summary="防止重复误判",
                source_feedback_refs_json=["fbc-source"],
                improvement_stage="regression",
                improvement_status="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            ImprovementFeedbackModel(
                feedback_id="fb-source",
                improvement_id=improvement_id,
                agent_id=feedback_agent_id or agent_id,
                summary="时间窗口误判",
                source="playground_run",
                status="merged",
                raw_text="请核验告警时间",
                case_id="fbc-source",
                agent_version_id="ver-base",
                created_at=now,
            )
        )
        db.add(
            NormalizedFeedbackModel(
                normalized_feedback_id="nf-source",
                improvement_id=improvement_id,
                problem="时间窗口误判",
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            AttributionModel(
                attribution_id="attr-source",
                improvement_id=improvement_id,
                summary="缺少时间校验",
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            OptimizationPlanModel(
                optimization_plan_id="opt-source",
                improvement_id=improvement_id,
                summary="增加时间校验",
                changes_json=[{"target": "prompt", "change": "校验时间"}],
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            AgentChangeSetModel(
                change_set_id="agc-source",
                agent_id=agent_id,
                created_at=now,
                updated_at=now,
                status="candidate_committed",
                execution_job_id="exec-source",
                base_commit_sha="ver-base",
                candidate_commit_sha="ver-candidate",
                branch_name="change-set/agc-source",
                worktree_path="/tmp/agc-source",
                payload_json={
                    "change_set_id": "agc-source",
                    "agent_id": agent_id,
                    "execution_job_id": "exec-source",
                    "base_commit_sha": "ver-base",
                    "candidate_commit_sha": "ver-candidate",
                    "worktree_path": "/tmp/agc-source",
                    "source_improvement_id": improvement_id,
                    "source_attribution_id": "attr-source",
                },
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id="exec-source",
                improvement_id=improvement_id,
                summary="已应用",
                changes_applied_json=["prompt"],
                agent_version="ver-candidate",
                status="confirmed",
                change_set_id="agc-source",
                applied_agent_version_id="ver-candidate",
                applied_diff_json={"changed_files": ["CLAUDE.md"]},
                base_commit_sha="ver-base",
                source_optimization_plan_id="opt-source",
                source_optimization_plan_updated_at=now,
                source_attribution_id="attr-source",
                source_attribution_updated_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            RegressionAssessmentModel(
                regression_assessment_id="reg-source",
                improvement_id=improvement_id,
                summary="覆盖时间窗口边界",
                cases_json=[
                    {
                        "prompt": "请判断是否应升级处置",
                        "expected_behavior": "先核验时间窗口",
                        "checkpoints": ["检查事件时间", "不得直接升级"],
                        "dataset_id": "hostile-dataset",
                        "agent_id": "hostile-agent",
                        "provenance": {"execution_id": "hostile-exec"},
                    }
                ],
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )


def test_adopt_test_dataset_projects_typed_backend_owned_chain(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)

    dataset = store.adopt_from_improvement("imp-dataset")

    assert dataset.dataset_id.startswith("tds-")
    assert dataset.agent_id == "soc-ops"
    assert dataset.source_improvement_id == "imp-dataset"
    assert dataset.revision == 1 and dataset.lifecycle_state == "draft"
    assert dataset.owner_kind == "business_agent" and dataset.owner_id == "soc-ops"
    assert dataset.provenance.regression_assessment_id == "reg-source"
    assert dataset.provenance.regression_assessment_updated_at == dataset.provenance.normalized_feedback_updated_at
    assert dataset.provenance.normalized_feedback_id == "nf-source"
    assert dataset.provenance.attribution_id == "attr-source"
    assert dataset.provenance.optimization_plan_id == "opt-source"
    assert dataset.provenance.execution_id == "exec-source"
    assert dataset.provenance.execution_updated_at == dataset.provenance.optimization_plan_updated_at
    assert dataset.provenance.source_feedback_ids == ["fb-source"]
    assert dataset.provenance.baseline_agent_version_id == "ver-base"
    assert dataset.provenance.candidate_agent_version_id == "ver-candidate"
    assert [(case.position, case.prompt) for case in dataset.cases] == [(1, "请判断是否应升级处置")]
    assert not hasattr(dataset, "asset_id") and not hasattr(dataset, "test_dataset_id") and not hasattr(dataset, "body")
    revisions = store.list_revisions(dataset.dataset_id, agent_id="soc-ops")
    assert len(revisions) == 1
    assert revisions[0].previous_lifecycle_state is None
    assert revisions[0].after["lifecycle_state"] == "draft"


def test_adopt_test_dataset_is_concurrently_idempotent(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(lambda _: store.adopt_from_improvement("imp-dataset"), range(2)))

    assert records[0].dataset_id == records[1].dataset_id
    with session_factory.begin() as db:
        assert db.query(DatasetModel).count() == 1
        assert db.query(DatasetCaseModel).count() == 1


def test_reworked_improvement_adopts_new_dataset_version_without_mutating_history(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)
    original = store.adopt_from_improvement("imp-dataset")
    next_time = "2026-07-14T00:00:00+00:00"

    with session_factory.begin() as db:
        db.delete(db.get(OptimizationPlanModel, "opt-source"))
        db.delete(db.get(ExecutionRecordModel, "exec-source"))
        db.delete(db.get(RegressionAssessmentModel, "reg-source"))
        db.delete(db.get(AgentChangeSetModel, "agc-source"))
        db.add(
            OptimizationPlanModel(
                optimization_plan_id="opt-source-v2",
                improvement_id="imp-dataset",
                summary="返工后增加严格时间校验",
                changes_json=[{"target": "prompt", "change": "严格校验时间"}],
                status="confirmed",
                created_at=next_time,
                updated_at=next_time,
            )
        )
        db.add(
            AgentChangeSetModel(
                change_set_id="agc-source-v2",
                agent_id="soc-ops",
                created_at=next_time,
                updated_at=next_time,
                status="candidate_committed",
                execution_job_id="exec-source-v2",
                base_commit_sha="ver-candidate",
                candidate_commit_sha="ver-candidate-v2",
                branch_name="change-set/agc-source-v2",
                worktree_path="/tmp/agc-source-v2",
                payload_json={
                    "change_set_id": "agc-source-v2",
                    "agent_id": "soc-ops",
                    "source_improvement_id": "imp-dataset",
                    "source_attribution_id": "attr-source",
                    "execution_job_id": "exec-source-v2",
                    "base_commit_sha": "ver-candidate",
                    "candidate_commit_sha": "ver-candidate-v2",
                    "worktree_path": "/tmp/agc-source-v2",
                },
            )
        )
        db.add(
            ExecutionRecordModel(
                execution_id="exec-source-v2",
                improvement_id="imp-dataset",
                summary="返工候选已应用",
                changes_applied_json=["prompt"],
                agent_version="ver-candidate-v2",
                status="confirmed",
                change_set_id="agc-source-v2",
                applied_agent_version_id="ver-candidate-v2",
                applied_diff_json={"changed_files": ["CLAUDE.md"]},
                base_commit_sha="ver-candidate",
                source_optimization_plan_id="opt-source-v2",
                source_optimization_plan_updated_at=next_time,
                source_attribution_id="attr-source",
                source_attribution_updated_at=original.provenance.attribution_updated_at,
                created_at=next_time,
                updated_at=next_time,
            )
        )
        db.add(
            RegressionAssessmentModel(
                regression_assessment_id="reg-source-v2",
                improvement_id="imp-dataset",
                summary="返工后的边界用例",
                cases_json=[
                    {
                        "prompt": "请验证返工后的时间窗口",
                        "expected_behavior": "严格拒绝窗口外事件",
                        "checkpoints": ["检查窗口上界"],
                    }
                ],
                status="confirmed",
                created_at=next_time,
                updated_at=next_time,
            )
        )

    refreshed = store.adopt_from_improvement("imp-dataset")
    repeated = store.adopt_from_improvement("imp-dataset")

    assert refreshed.dataset_id != original.dataset_id
    assert repeated.dataset_id == refreshed.dataset_id
    assert refreshed.provenance.execution_id == "exec-source-v2"
    assert refreshed.provenance.candidate_agent_version_id == "ver-candidate-v2"
    assert refreshed.cases[0].prompt == "请验证返工后的时间窗口"
    persisted_original = store.get_dataset(original.dataset_id, agent_id="soc-ops")
    assert persisted_original.provenance.execution_id == "exec-source"
    assert persisted_original.cases[0].prompt == "请判断是否应升级处置"
    with session_factory.begin() as db:
        assert db.query(DatasetModel).count() == 2


def test_adopt_test_dataset_rejects_execution_dependency_drift(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)
    mutations = (
        ("source_optimization_plan_id", "opt-stale", "Optimization plan revision changed"),
        ("source_optimization_plan_updated_at", "2026-01-01T00:00:00Z", "Optimization plan revision changed"),
        ("source_attribution_id", "attr-stale", "Attribution revision changed"),
        ("source_attribution_updated_at", "2026-01-01T00:00:00Z", "Attribution revision changed"),
    )

    for field, stale_value, message in mutations:
        with session_factory.begin() as db:
            execution = db.get(ExecutionRecordModel, "exec-source")
            assert execution is not None
            original = getattr(execution, field)
            setattr(execution, field, stale_value)
        with pytest.raises(ConflictError, match=message):
            store.adopt_from_improvement("imp-dataset")
        with session_factory.begin() as db:
            execution = db.get(ExecutionRecordModel, "exec-source")
            assert execution is not None
            setattr(execution, field, original)

    assert store.adopt_from_improvement("imp-dataset").provenance.execution_id == "exec-source"


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ("missing", "does not exist"),
        ("agent_id", "different Agent"),
        ("execution_job_id", "different execution"),
        ("source_improvement_id", "different improvement"),
        ("candidate_commit_sha", "does not match change set candidate"),
        ("base_commit_sha", "does not match change set base"),
        ("worktree_path", "worktree is invalid"),
        ("status", "not adoptable"),
        ("payload_worktree_path", "payload disagrees"),
    ],
)
def test_adopt_test_dataset_rejects_unbound_execution_change_set(
    tmp_path: Path,
    binding: str,
    message: str,
) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    with session_factory.begin() as db:
        change_set = db.get(AgentChangeSetModel, "agc-source")
        assert change_set is not None
        if binding == "missing":
            db.delete(change_set)
        elif binding == "source_improvement_id":
            payload = dict(change_set.payload_json or {})
            payload["source_improvement_id"] = "imp-other"
            change_set.payload_json = payload
        elif binding == "worktree_path":
            change_set.worktree_path = "relative/worktree"
        elif binding == "status":
            change_set.status = "draft"
        elif binding == "payload_worktree_path":
            payload = dict(change_set.payload_json or {})
            payload["worktree_path"] = "/tmp/other-worktree"
            change_set.payload_json = payload
        else:
            setattr(change_set, binding, "other")

    with pytest.raises(DataIntegrityError, match=message):
        DatasetStore(session_factory).adopt_from_improvement("imp-dataset")


def test_adopt_test_dataset_rejects_unproven_source_feedback_ref(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    with session_factory.begin() as db:
        improvement = db.get(ImprovementItemModel, "imp-dataset")
        assert improvement is not None
        improvement.source_feedback_refs_json = ["fbc-source", "fbc-orphan"]

    with pytest.raises(DataIntegrityError, match="source feedback refs lack same-Agent evidence"):
        DatasetStore(session_factory).adopt_from_improvement("imp-dataset")


def test_test_dataset_projection_rejects_incomplete_core_provenance(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)
    dataset = store.adopt_from_improvement("imp-dataset")
    fields = (
        "source_regression_assessment_id",
        "source_regression_assessment_updated_at",
        "source_normalized_feedback_id",
        "source_normalized_feedback_updated_at",
        "source_attribution_id",
        "source_attribution_updated_at",
        "source_optimization_plan_id",
        "source_optimization_plan_updated_at",
        "source_execution_id",
        "source_execution_updated_at",
        "candidate_agent_version_id",
    )

    for field in fields:
        with session_factory.begin() as db:
            row = db.get(DatasetModel, dataset.dataset_id)
            assert row is not None
            original = getattr(row, field)
            setattr(row, field, "")
        with pytest.raises(DataIntegrityError, match=field.removeprefix("source_")):
            store.get_dataset(dataset.dataset_id, agent_id="soc-ops")
        with session_factory.begin() as db:
            row = db.get(DatasetModel, dataset.dataset_id)
            assert row is not None
            setattr(row, field, original)


def test_test_dataset_lifecycle_cas_and_agent_scope_fail_closed(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory)
    store = DatasetStore(session_factory)
    dataset = store.adopt_from_improvement("imp-dataset")

    active = store.transition_lifecycle(
        dataset.dataset_id,
        agent_id="soc-ops",
        target_state="active",
        expected_revision=1,
        operator="reviewer",
        reason="用例已复核",
    )
    assert active.lifecycle_state == "active" and active.revision == 2
    with pytest.raises(BusinessRuleViolation, match="owned by EvalRun"):
        store.transition_lifecycle(
            dataset.dataset_id,
            agent_id="soc-ops",
            target_state="evaluating",
            expected_revision=2,
            operator="evaluator",
            reason="尝试手工伪造运行态",
        )
    deprecated = store.transition_lifecycle(
        dataset.dataset_id,
        agent_id="soc-ops",
        target_state="deprecated",
        expected_revision=2,
        operator="reviewer",
        reason="旧数据集不再默认使用",
    )
    assert deprecated.lifecycle_state == "deprecated" and deprecated.revision == 3
    with pytest.raises(ConflictError, match="revision changed"):
        store.transition_lifecycle(
            dataset.dataset_id,
            agent_id="soc-ops",
            target_state="deprecated",
            expected_revision=1,
            operator="reviewer",
            reason="过期请求",
        )
    with pytest.raises(StateTransitionError, match="deprecated -> draft"):
        store.transition_lifecycle(
            dataset.dataset_id,
            agent_id="soc-ops",
            target_state="draft",
            expected_revision=3,
            operator="reviewer",
            reason="非法回退",
        )
    with pytest.raises(NotFoundError):
        store.get_dataset(dataset.dataset_id, agent_id="other-agent")
    revisions = store.list_revisions(dataset.dataset_id, agent_id="soc-ops")
    assert [(revision.revision, revision.operator) for revision in revisions] == [
        (1, "system"),
        (2, "reviewer"),
        (3, "reviewer"),
    ]
    assert revisions[-1].before["lifecycle_state"] == "active"
    assert revisions[-1].after["lifecycle_state"] == "deprecated"
    with session_factory.begin() as db:
        assert db.query(DatasetRevisionModel).count() == 3


def test_test_dataset_rejects_cross_agent_source_chain(tmp_path: Path) -> None:
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    _seed_confirmed_chain(session_factory, feedback_agent_id="other-agent")

    with pytest.raises(DataIntegrityError, match="feedback owner"):
        DatasetStore(session_factory).adopt_from_improvement("imp-dataset")


def test_eval_run_requires_active_typed_dataset_with_matching_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    _seed_confirmed_chain(module.runtime_db_session_factory)
    dataset_store = module.asset_store.test_datasets
    dataset = dataset_store.adopt_from_improvement("imp-dataset")
    with pytest.raises(ConflictError, match="Only active TestDataset"):
        module.feedback_store.create_eval_run(
            dataset_id=dataset.dataset_id,
            agent_version_id="ver-candidate",
            agent_id="soc-ops",
        )
    dataset = dataset_store.transition_lifecycle(
        dataset.dataset_id,
        agent_id="soc-ops",
        target_state="active",
        expected_revision=dataset.revision,
        operator="reviewer",
        reason="用例已复核",
    )

    eval_run = module.feedback_store.create_eval_run(
        dataset_id=dataset.dataset_id,
        agent_version_id="ver-candidate",
        agent_id="soc-ops",
    )

    assert eval_run["dataset_id"] == dataset.dataset_id
    assert eval_run["dataset_snapshot"]["lifecycle_state"] == "evaluating"
    assert eval_run["dataset_snapshot"]["revision"] == 3
    assert eval_run["dataset_snapshot"]["cases"][0]["prompt"] == "请判断是否应升级处置"
    assert module.feedback_store.get_eval_run(eval_run["eval_run_id"])["dataset_id"] == dataset.dataset_id
    with pytest.raises(ConflictError, match="does not match EvalRun agent"):
        module.feedback_store.create_eval_run(
            dataset_id=dataset.dataset_id,
            agent_version_id="ver-candidate",
            agent_id="other-agent",
        )

    module.feedback_store.fail_eval_run(
        eval_run["eval_run_id"],
        error_code="TEST_TERMINAL",
        message="close fixture run",
    )
    archived = dataset_store.transition_lifecycle(
        dataset.dataset_id,
        agent_id="soc-ops",
        target_state="archived",
        expected_revision=4,
        operator="reviewer",
        reason="停止使用",
    )
    assert archived.revision == 5
    with pytest.raises(ConflictError, match="current state is archived"):
        module.feedback_store.create_eval_run(
            dataset_id=dataset.dataset_id,
            agent_version_id="ver-candidate",
            agent_id="soc-ops",
        )


def test_test_dataset_api_rejects_generic_and_hostile_paths(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    _seed_confirmed_chain(module.runtime_db_session_factory)
    with TestClient(module.app) as client:
        generic = client.post(
            "/api/assets",
            json={"agent_id": "soc-ops", "asset_type": "test_dataset", "title": "绕过", "body": "{}"},
        )
        hostile = client.post(
            "/api/improvements/imp-dataset/test-dataset/adopt",
            json={
                "dataset_id": "hostile-dataset",
                "agent_id": "other-agent",
                "source_improvement_id": "imp-other",
                "revision": 999,
                "provenance": {"execution_id": "hostile-exec"},
            },
        )
        adopted = client.post("/api/improvements/imp-dataset/test-dataset/adopt", json={})
        payload = adopted.json()
        listed = client.get(
            "/api/test-datasets",
            params={"agent_id": "soc-ops", "source_improvement_id": "imp-dataset"},
        )
        cross_agent = client.get(
            f"/api/test-datasets/{payload['dataset_id']}",
            params={"agent_id": "other-agent"},
        )
        activated = client.post(
            f"/api/test-datasets/{payload['dataset_id']}/lifecycle",
            params={"agent_id": "soc-ops"},
            json={
                "target_state": "active",
                "expected_revision": 1,
                "operator": "reviewer",
                "reason": "用例已复核",
            },
        )
        invalid_transition = client.post(
            f"/api/test-datasets/{payload['dataset_id']}/lifecycle",
            params={"agent_id": "soc-ops"},
            json={
                "target_state": "draft",
                "expected_revision": 2,
                "operator": "reviewer",
                "reason": "非法回退",
            },
        )
        revisions = client.get(
            f"/api/test-datasets/{payload['dataset_id']}/revisions",
            params={"agent_id": "soc-ops"},
        )
        openapi = client.get("/openapi.json").json()

    assert generic.status_code == 400
    assert hostile.status_code == 422
    assert adopted.status_code == 200
    assert payload["dataset_id"].startswith("tds-")
    assert payload["agent_id"] == "soc-ops"
    assert payload["owner_kind"] == "business_agent" and payload["owner_id"] == "soc-ops"
    assert payload["lifecycle_state"] == "draft"
    assert payload["provenance"]["execution_id"] == "exec-source"
    assert payload["cases"][0]["prompt"] == "请判断是否应升级处置"
    assert {"asset_id", "test_dataset_id", "body"}.isdisjoint(payload)
    assert [item["dataset_id"] for item in listed.json()] == [payload["dataset_id"]]
    assert cross_agent.status_code == 404
    assert activated.status_code == 200
    assert activated.json()["lifecycle_state"] == "active" and activated.json()["revision"] == 2
    assert invalid_transition.status_code == 409
    assert revisions.status_code == 200
    assert [(item["revision"], item["operator"]) for item in revisions.json()] == [(1, "system"), (2, "reviewer")]
    assert revisions.json()[-1]["before"]["lifecycle_state"] == "draft"
    assert revisions.json()[-1]["after"]["lifecycle_state"] == "active"
    assert "/api/improvements/{improvement_id}/test-dataset/adopt" in openapi["paths"]
    assert "/api/test-datasets/{dataset_id}/revisions" in openapi["paths"]
    assert "TestDatasetResponse" in openapi["components"]["schemas"]
    assert "TestDatasetRevisionResponse" in openapi["components"]["schemas"]
    assert "TestCaseResponse" in openapi["components"]["schemas"]
    assert "dataset_id" in openapi["components"]["schemas"]["FeedbackEvalRunCreateRequest"]["required"]
    assert "dataset_id" in openapi["components"]["schemas"]["AgentChangeSetRegressionRunRequest"]["required"]
    manual_request = openapi["components"]["schemas"]["FeedbackEvalRunCreateRequest"]
    change_set_request = openapi["components"]["schemas"]["AgentChangeSetRegressionRunRequest"]
    eval_run = openapi["components"]["schemas"]["EvalRunResponse"]
    eval_item = openapi["components"]["schemas"]["EvalRunItemResponse"]
    assert "eval_case_ids" not in manual_request["properties"]
    assert set(change_set_request["properties"]) == {"dataset_id"}
    assert {"dataset_id", "dataset_snapshot", "regression_attempt_id"}.issubset(eval_run["properties"])
    assert "eval_case_ids" not in eval_run["properties"]
    assert "item_ids" not in eval_run["properties"]
    assert "dataset_case_id" in eval_item["properties"]
    assert "eval_case_id" not in eval_item["properties"]
