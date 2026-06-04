from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _attribution_output,
    _batch_plan_output,
    _create_eval_case,
    _record_run,
    _store,
)


def _case_with_completed_attribution(store):
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["tool_data_incomplete"],
            comment="单反馈优化方案测试",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="单反馈优化方案测试")
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    return feedback_case, attribution_job


def test_single_feedback_optimization_plan_reuses_size_one_batch(tmp_path):
    store, _ = _store(tmp_path)
    feedback_case, _ = _case_with_completed_attribution(store)

    first_batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    second_batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    job = store.queue_feedback_case_optimization_plan_agent_job(feedback_case["feedback_case_id"])

    assert first_batch["batch_id"] == second_batch["batch_id"]
    assert first_batch["feedback_case_ids"] == [feedback_case["feedback_case_id"]]
    assert job["job_type"] == "batch_plan"
    assert job["scope_kind"] == "optimization_batch"
    assert job["scope_id"] == first_batch["batch_id"]
    assert job["input_json"]["feedback_case_ids"] == [feedback_case["feedback_case_id"]]


def test_single_feedback_plan_task_creates_task_without_internal_proposal(tmp_path):
    store, _ = _store(tmp_path)
    feedback_case, attribution_job = _case_with_completed_attribution(store)
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    plan_job = store.create_batch_plan_job(batch["batch_id"])

    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_approval",
            actionability="direct_workspace_change",
            target_type="main_agent_claude_md",
            target_path="CLAUDE.md",
            recommendation="回答工作区配置问题前读取当前配置。",
            tasks=[
                {
                    "execution_kind": "workspace_execution",
                    "status": "pending_execution",
                    "title": "补充配置读取要求",
                    "description": "回答工作区配置问题前读取当前配置。",
                    "objective": "提高单反馈场景回答完整性。",
                    "target_summary": "CLAUDE.md",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "owner": "main_agent_workspace",
                    "actionability": "direct_workspace_change",
                    "recommendation": "回答工作区配置问题前读取当前配置。",
                    "recommended_actions": ["修改 CLAUDE.md"],
                    "acceptance_criteria": ["复测单反馈场景通过。"],
                    "expected_effect": "回答更完整。",
                    "validation": "复测单反馈场景。",
                    "risk": "回答前工具调用可能增加。",
                    "feedback_case_ids": [feedback_case["feedback_case_id"]],
                    "eval_case_ids": [],
                    "attribution_job_ids": [attribution_job["job_id"]],
                }
            ],
            blocked_items=[],
        ),
    )
    plan_task = completed["validated_output_json"]["tasks"][0]

    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])
    task = prepared["optimization_task"]

    assert store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"]) == []
    assert prepared["batch"]["internal_proposal_id"] is None
    assert task["proposal_id"] is None
    assert task["proposal_ids"] == []
    assert task["source"] == "feedback_optimization_batch"
    assert task["source_batch_id"] == batch["batch_id"]
    assert task["source_plan_task_id"] == plan_task["plan_task_id"]
    assert task["proposal"]["recommendation"] == "回答工作区配置问题前读取当前配置。"


def test_workflow_list_filters_do_not_use_in_memory_filter(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    eval_case, feedback_case = _create_eval_case(store)
    eval_case = store.promote_eval_case(eval_case["eval_case_id"], {"operator": "tester", "reason": "filter coverage"})
    feedback_case = store.find_case(feedback_case["feedback_case_id"])
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    attribution_job_id = feedback_case["attribution_job_ids"][0]
    plan_job = store.create_batch_plan_job(batch["batch_id"])
    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_approval",
            actionability="direct_workspace_change",
            target_type="main_agent_claude_md",
            target_path="CLAUDE.md",
            tasks=[
                {
                    "execution_kind": "workspace_execution",
                    "status": "pending_execution",
                    "title": "补充反馈场景说明",
                    "description": "补充反馈场景说明。",
                    "objective": "提高反馈场景表现。",
                    "target_summary": "CLAUDE.md",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "owner": "main_agent_workspace",
                    "actionability": "direct_workspace_change",
                    "recommendation": "补充反馈场景说明。",
                    "recommended_actions": ["修改 CLAUDE.md"],
                    "acceptance_criteria": ["回归用例通过。"],
                    "expected_effect": "反馈场景表现提升。",
                    "validation": "回归用例通过。",
                    "risk": "需人工确认。",
                    "feedback_case_ids": [feedback_case["feedback_case_id"]],
                    "eval_case_ids": [eval_case["eval_case_id"]],
                    "attribution_job_ids": [attribution_job_id],
                }
            ],
            blocked_items=[],
        ),
    )
    plan_task = completed["validated_output_json"]["tasks"][0]
    task = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])["optimization_task"]
    eval_run = store.create_eval_run(
        eval_case_ids=[eval_case["eval_case_id"]],
        agent_version_id="main-v-test",
        optimization_task_id=task["optimization_task_id"],
        source="manual_task_regression",
    )

    def fail_filter(*args, **kwargs):
        raise AssertionError("workflow list queries should push exact filters down to SQLite")

    monkeypatch.setattr(store, "_filter_records", fail_filter)

    assert store.list_eval_cases(status="active", source_feedback_case_id=feedback_case["feedback_case_id"])[0]["eval_case_id"] == eval_case["eval_case_id"]
    assert store.list_eval_runs(status="running", agent_version_id="main-v-test", optimization_task_id=task["optimization_task_id"])[0]["eval_run_id"] == eval_run["eval_run_id"]
    assert store.list_tasks(feedback_case_id=feedback_case["feedback_case_id"], status="pending_execution")[0]["optimization_task_id"] == task["optimization_task_id"]
    assert store.list_optimization_batches(status="execution_planning")[0]["batch_id"] == batch["batch_id"]
