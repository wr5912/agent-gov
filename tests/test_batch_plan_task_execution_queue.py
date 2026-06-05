from fastapi.testclient import TestClient

from app.runtime.feedback_schemas import ExecutionPlanFormatterOutput, FeedbackEvalCaseGenerationFormatterOutput
from feedback_store_test_utils import _create_batch_with_completed_attribution
from test_api_execution_optimizer import _load_app, _run_one_agent_job


def _batch_plan_task(batch: dict, plan_task_id: str) -> dict:
    for task in batch["optimization_plan"]["tasks"]:
        if task["plan_task_id"] == plan_task_id:
            return task
    raise AssertionError(f"plan task not found: {plan_task_id}")


def test_batch_plan_task_execute_endpoint_is_consumed_by_worker(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    async def fake_run_profile_json(**kwargs):
        if kwargs["job_type"] == "eval_case_generation":
            return FeedbackEvalCaseGenerationFormatterOutput.model_validate(
                {
                    "eval_cases": [],
                    "no_action_reason": "测试只验证 execute 队列链路，跳过用例生成。",
                }
            )
        assert kwargs["job_type"] == "execution"
        return ExecutionPlanFormatterOutput.model_validate(
            {
                "status": "ready",
                "summary": "追加配置核查要求。",
                "operations": [
                    {
                        "operation": "append_text",
                        "path": "CLAUDE.md",
                        "append_text": "\n回答配置类问题前必须读取当前 workspace 配置。\n",
                        "rationale": "补强主智能体指令。",
                    }
                ],
                "validation": "复测 workspace 配置类问题。",
                "risk": "回答耗时可能增加。",
                "human_review_required": True,
            }
        )

    monkeypatch.setattr(module.runtime, "_run_profile_json", fake_run_profile_json)
    batch = _create_batch_with_completed_attribution(module.feedback_store)
    batch = module.feedback_store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]

    with TestClient(module.app) as client:
        response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan/tasks/{plan_task['plan_task_id']}/execute",
            json={"force": True},
        )

    assert response.status_code == 200, response.json()
    queued_payload = response.json()
    execution_job_id = queued_payload["execution_job"]["execution_job_id"]
    assert queued_payload["execution_job"]["status"] == "queued"
    queued_task = module.feedback_store.find_task(queued_payload["optimization_task"]["optimization_task_id"])
    queued_batch = module.feedback_store.find_optimization_batch(batch["batch_id"])
    queued_plan_task = _batch_plan_task(queued_batch, plan_task["plan_task_id"])
    assert queued_task["status"] == "execution_planning"
    assert queued_plan_task["status"] == "execution_planning"

    completed = None
    for _ in range(3):
        result = _run_one_agent_job(module)
        if result and result.job_id == execution_job_id:
            completed = result
            break
    updated_job = module.feedback_store.get_execution_job(execution_job_id)
    updated_batch = module.feedback_store.find_optimization_batch(batch["batch_id"])
    updated_plan_task = _batch_plan_task(updated_batch, plan_task["plan_task_id"])

    assert completed is not None
    assert completed.job_id == execution_job_id
    assert completed.status == "completed"
    assert updated_job["status"] == "completed"
    assert updated_job["validated_output_json"]["status"] == "ready"
    assert updated_batch["status"] == "execution_ready"
    assert updated_plan_task["status"] == "execution_ready"
    assert updated_plan_task["latest_execution_job"]["execution_job_id"] == execution_job_id
