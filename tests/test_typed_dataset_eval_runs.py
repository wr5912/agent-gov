from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.runtime.errors import ConflictError
from app.runtime.runtime_db import AgentChangeSetModel, utc_now
from app.runtime.runtime_db import TestDatasetCaseModel as DatasetCaseModel
from app.runtime.runtime_db import TestDatasetModel as DatasetModel
from app.runtime.schemas import ChatResponse
from app.runtime.test_dataset_schemas import TestCaseRecord as DatasetCaseRecord
from app.services.feedback_eval_runner import FeedbackEvalRunner

from feedback_store_test_utils import _seed_test_dataset, _store


def test_runner_executes_only_the_persisted_typed_dataset_snapshot(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-runner")
    requests = []

    async def run_chat(request):
        requests.append(request)
        with store.Session.begin() as db:
            dataset = db.get(DatasetModel, dataset_id)
            case = db.get(DatasetCaseModel, f"tdc-{dataset_id}")
            assert dataset is not None and case is not None
            dataset.lifecycle_state = "archived"
            case.prompt = "hostile post-start mutation"
        return ChatResponse(run_id="run-dataset", session_id=request.session_id or "", answer="ok")

    runner = FeedbackEvalRunner(
        feedback_store=store,
        run_chat=run_chat,
    )

    result = asyncio.run(runner.run_feedback_eval(dataset_id=dataset_id))

    assert result is not None
    assert [request.message for request in requests] == ["验证 typed dataset 执行路径"]
    assert requests[0].agent_id == "soc-ops"
    assert result.dataset_snapshot.lifecycle_state == "evaluating"
    assert result.dataset_snapshot.cases[0].prompt == "验证 typed dataset 执行路径"
    assert result.items[0].dataset_case_id == f"tdc-{dataset_id}"
    assert result.items[0].dataset_case_snapshot.prompt == "验证 typed dataset 执行路径"
    assert result.items[0].agent_version_id == result.agent_version_id == "main-v-test"
    assert result.result_status == "needs_human_review"
    assert result.gate_result.status == "review_required"
    assert result.items[0].status == "needs_human_review"
    assert result.items[0].check_results[-1].name == "semantic_requirements_require_human_review"
    assert result.items[0].check_results[-1].passed is False


def test_runner_never_auto_passes_unverified_natural_language_expectations(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-semantic-review")

    async def run_chat(request):
        return ChatResponse(run_id="run-wrong-answer", session_id=request.session_id or "", answer="完全错误但非空的回答")

    result = asyncio.run(FeedbackEvalRunner(feedback_store=store, run_chat=run_chat).run_feedback_eval(dataset_id=dataset_id))

    assert result is not None
    assert result.summary.passed == 0
    assert result.summary.needs_human_review == 1
    assert result.gate_result.review_dataset_case_ids == [f"tdc-{dataset_id}"]


def test_runner_persists_terminal_eval_run_before_propagating_cancellation(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-cancelled")

    async def run_chat(_request):
        raise asyncio.CancelledError

    runner = FeedbackEvalRunner(feedback_store=store, run_chat=run_chat)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_feedback_eval(dataset_id=dataset_id))

    runs = store.list_eval_runs(agent_id="soc-ops")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["result_status"] == "failed"
    assert runs[0]["error_json"]["error_code"] == "EVAL_RUN_CANCELLED"


def test_runner_fails_closed_on_chat_response_agent_version_pollution(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-hostile-version")

    async def run_chat(request):
        return ChatResponse(
            run_id="run-hostile-version",
            session_id=request.session_id or "",
            answer="hostile",
            agent_version_id="version-from-untrusted-response",
        )

    runner = FeedbackEvalRunner(
        feedback_store=store,
        run_chat=run_chat,
    )

    result = asyncio.run(runner.run_feedback_eval(dataset_id=dataset_id))

    assert result is not None
    assert result.status == "failed" and result.result_status == "failed"
    assert result.items == []
    assert result.error_json is not None
    assert result.error_json.error_code == "EVAL_RUN_RUNTIME_ERROR"
    assert "agent version does not match" in result.error_json.message


def test_runner_records_typed_item_runtime_error_without_legacy_eval_case_code(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-item-error")

    async def run_chat(_request):
        raise RuntimeError("candidate runtime failed")

    runner = FeedbackEvalRunner(
        feedback_store=store,
        run_chat=run_chat,
    )

    result = asyncio.run(runner.run_feedback_eval(dataset_id=dataset_id))

    assert result is not None
    assert result.result_status == "failed"
    assert len(result.items) == 1
    assert result.items[0].error_json is not None
    assert result.items[0].error_json.error_code == "EVAL_RUN_ITEM_RUNTIME_ERROR"
    assert "candidate runtime failed" in result.items[0].error_json.message


def test_eval_run_rejects_empty_archived_and_cross_agent_datasets(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    empty_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-empty")
    with store.Session.begin() as db:
        db.delete(db.get(DatasetCaseModel, f"tdc-{empty_id}"))

    with pytest.raises(ConflictError, match="has no cases"):
        store.create_eval_run(dataset_id=empty_id, agent_version_id="v1", agent_id="soc-ops")

    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-owner")
    with pytest.raises(ConflictError, match="does not match EvalRun agent"):
        store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="other-agent")
    with store.Session.begin() as db:
        dataset = db.get(DatasetModel, dataset_id)
        assert dataset is not None
        dataset.lifecycle_state = "archived"
    with pytest.raises(ConflictError, match="current state is archived"):
        store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")


@pytest.mark.parametrize("lifecycle_state", ["draft", "evaluating", "deprecated", "archived"])
def test_eval_run_rejects_every_non_active_dataset_state(tmp_path, lifecycle_state) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id=f"tds-{lifecycle_state}")
    with store.Session.begin() as db:
        dataset = db.get(DatasetModel, dataset_id)
        assert dataset is not None
        dataset.lifecycle_state = lifecycle_state

    with pytest.raises(ConflictError, match=f"current state is {lifecycle_state}"):
        store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")
    assert store.list_eval_runs(agent_id="soc-ops") == []


def test_eval_run_owns_dataset_lifecycle_until_terminal_state(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-lifecycle")

    run = store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")
    with store.Session() as db:
        evaluating = db.get(DatasetModel, dataset_id)
        assert evaluating is not None
        assert (evaluating.lifecycle_state, evaluating.revision) == ("evaluating", 2)
    assert run["dataset_snapshot"]["lifecycle_state"] == "evaluating"
    assert run["dataset_snapshot"]["revision"] == 2

    failed = store.fail_eval_run(run["eval_run_id"], error_code="TEST_FAILURE", message="expected")
    assert failed is not None and failed["status"] == "failed"
    with store.Session() as db:
        active = db.get(DatasetModel, dataset_id)
        assert active is not None
        assert (active.lifecycle_state, active.revision) == ("active", 3)


def test_runtime_reconciliation_only_fails_expired_eval_run_and_releases_dataset(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-restart-recovery")
    run = store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")

    assert store.reconcile_orphan_eval_runs() == []
    assert store.renew_eval_run_lease(run["eval_run_id"], now="2099-01-01T00:00:00+00:00") is True
    assert store.reconcile_orphan_eval_runs(now="2099-01-01T00:14:59+00:00") == []
    assert store.reconcile_orphan_eval_runs(now="2099-01-01T00:15:00+00:00") == [run["eval_run_id"]]
    assert store.reconcile_orphan_eval_runs(now="2099-01-01T00:16:00+00:00") == []
    recovered = store.get_eval_run(run["eval_run_id"])
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert recovered["error_json"]["error_code"] == "EVAL_RUN_LEASE_EXPIRED"
    with store.Session() as db:
        dataset = db.get(DatasetModel, dataset_id)
        assert dataset is not None
        assert (dataset.lifecycle_state, dataset.revision) == ("active", 3)


def test_concurrent_eval_run_start_has_one_dataset_lifecycle_owner(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-concurrent-start")

    def start():
        return store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start) for _ in range(2)]
        outcomes = [future.exception() or future.result() for future in futures]

    runs = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    assert len(runs) == 1
    assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
    store.fail_eval_run(runs[0]["eval_run_id"], error_code="TEST_CLEANUP", message="close winning run")


def test_concurrent_eval_run_terminal_writers_share_one_immutable_outcome(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-terminal-race")
    run = store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")
    dataset_case = DatasetCaseRecord.model_validate(run["dataset_snapshot"]["cases"][0])
    store.append_eval_run_item(
        run["eval_run_id"],
        dataset_case=dataset_case,
        agent_result={"run_id": "run-terminal-race", "agent_version_id": "v1", "answer": "ok"},
        status="passed",
        score=1.0,
        check_results=[],
    )
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        return store.finish_eval_run(run["eval_run_id"])

    def fail():
        barrier.wait()
        return store.fail_eval_run(run["eval_run_id"], error_code="RACE", message="concurrent failure")

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result() for future in (executor.submit(finish), executor.submit(fail))]

    persisted = store.get_eval_run(run["eval_run_id"])
    assert persisted is not None
    assert all(outcome is not None for outcome in outcomes)
    assert {outcome["status"] for outcome in outcomes if outcome} == {persisted["status"]}
    assert {outcome["completed_at"] for outcome in outcomes if outcome} == {persisted["completed_at"]}
    with store.Session() as db:
        dataset = db.get(DatasetModel, dataset_id)
        assert dataset is not None
        assert (dataset.lifecycle_state, dataset.revision) == ("active", 3)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"regression_attempt_id": None}, "complete backend-owned bindings"),
        ({"agent_version_id": "v2"}, "version must equal its candidate"),
        ({"candidate_worktree_path": "relative/worktree"}, "absolute candidate worktree"),
        ({"source": "manual_feedback_dataset"}, "only valid for Agent change set regression"),
        ({}, "Agent change set does not exist"),
    ],
)
def test_regression_eval_run_creation_rejects_unbound_backend_fields(tmp_path, overrides, message) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="main-agent", dataset_id="tds-regression-bindings")
    request = {
        "dataset_id": dataset_id,
        "agent_version_id": "v1",
        "source": "agent_change_set_regression",
        "change_set_id": "agc-missing",
        "regression_attempt_id": "evr-intent-missing",
        "candidate_commit_sha": "v1",
        "candidate_worktree_path": "/tmp/agc-missing",
    }
    request.update(overrides)

    with pytest.raises(ConflictError, match=message):
        store.create_eval_run(**request)


@pytest.mark.parametrize("drift", ["source_improvement", "source_execution", "candidate_version"])
def test_regression_eval_run_rejects_dataset_provenance_drift_before_persisting(tmp_path, drift) -> None:
    store, _settings = _store(tmp_path)
    change_set_id = "agc-dataset-provenance"
    dataset_id = _seed_test_dataset(
        store,
        agent_id="main-agent",
        dataset_id="tds-dataset-provenance",
        candidate_agent_version_id="candidate-v1",
        source_improvement_id="imp-bound",
        source_execution_id="exec-bound",
    )
    now = utc_now()
    payload = {
        "change_set_id": change_set_id,
        "source_improvement_id": "imp-other" if drift == "source_improvement" else "imp-bound",
        "latest_eval_run_id": "evr-intent-bound",
        "regression_dataset_id": dataset_id,
    }
    with store.Session.begin() as db:
        db.add(
            AgentChangeSetModel(
                change_set_id=change_set_id,
                agent_id="main-agent",
                created_at=now,
                updated_at=now,
                status="regression_running",
                execution_job_id="exec-other" if drift == "source_execution" else "exec-bound",
                base_commit_sha="baseline-v1",
                candidate_commit_sha="candidate-v2" if drift == "candidate_version" else "candidate-v1",
                branch_name="agentgov/dataset-provenance",
                worktree_path="/tmp/agc-dataset-provenance",
                payload_json=payload,
            )
        )

    candidate = "candidate-v2" if drift == "candidate_version" else "candidate-v1"
    with pytest.raises(ConflictError, match="regression provenance drifted"):
        store.create_eval_run(
            dataset_id=dataset_id,
            agent_version_id=candidate,
            source="agent_change_set_regression",
            change_set_id=change_set_id,
            regression_attempt_id="evr-intent-bound",
            candidate_commit_sha=candidate,
            candidate_worktree_path="/tmp/agc-dataset-provenance",
        )

    assert store.list_eval_runs(agent_id="main-agent") == []
    with store.Session() as db:
        dataset = db.get(DatasetModel, dataset_id)
        assert dataset is not None and dataset.lifecycle_state == "active"


def test_eval_run_item_is_fenced_to_one_snapshot_case_under_concurrency(tmp_path) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-fenced")
    other_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-other")
    run = store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")
    case = DatasetCaseRecord.model_validate(run["dataset_snapshot"]["cases"][0])
    other_run = store.create_eval_run(dataset_id=other_id, agent_version_id="v1", agent_id="soc-ops")
    other_case = DatasetCaseRecord.model_validate(other_run["dataset_snapshot"]["cases"][0])

    with pytest.raises(ConflictError, match="does not belong"):
        store.append_eval_run_item(
            run["eval_run_id"],
            dataset_case=other_case,
            agent_result={"run_id": "wrong", "answer": "no"},
            status="failed",
            score=0.0,
            check_results=[],
        )

    def append(run_id: str):
        return store.append_eval_run_item(
            run_id,
            dataset_case=case,
            agent_result={"run_id": "one", "agent_version_id": "v1", "answer": "ok"},
            status="passed",
            score=1.0,
            check_results=[],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.exception() or future.result() for future in [executor.submit(append, run["eval_run_id"]) for _ in range(2)]]

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
    persisted = store.get_eval_run(run["eval_run_id"])
    assert persisted is not None
    assert len(persisted["items"]) == 1
    incomplete = store.finish_eval_run(other_run["eval_run_id"])
    completed = store.finish_eval_run(run["eval_run_id"])
    assert incomplete is not None and incomplete["error_json"]["error_code"] == "EVAL_RUN_INCOMPLETE_DATASET_SNAPSHOT"
    assert completed is not None and completed["status"] == "completed"
    with pytest.raises(ConflictError, match="already terminal"):
        append(run["eval_run_id"])


@pytest.mark.parametrize("actual_agent_version_id", [None, "", "v2"])
def test_eval_run_item_requires_exact_agent_version_binding(tmp_path, actual_agent_version_id) -> None:
    store, _settings = _store(tmp_path)
    dataset_id = _seed_test_dataset(store, agent_id="soc-ops", dataset_id="tds-version-bound")
    run = store.create_eval_run(dataset_id=dataset_id, agent_version_id="v1", agent_id="soc-ops")
    dataset_case = DatasetCaseRecord.model_validate(run["dataset_snapshot"]["cases"][0])
    agent_result = {
        "run_id": "run-version-bound",
        "agent_version_id": actual_agent_version_id,
        "answer": "ok",
    }

    with pytest.raises(ConflictError, match="agent version does not match"):
        store.append_eval_run_item(
            str(run["eval_run_id"]),
            dataset_case=dataset_case,
            agent_result=agent_result,
            status="passed",
            score=1.0,
            check_results=[],
        )

    persisted = store.get_eval_run(str(run["eval_run_id"]))
    assert persisted is not None and persisted["items"] == []
