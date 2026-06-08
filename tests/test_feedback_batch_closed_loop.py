from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app, _run_one_agent_job

FOB_DA60_DAILY_REPORT_PROMPTS = [
    "生成一份安全运营日报，包含告警总览和高危告警明细，保存到 /data/outputs/daily-report.md",
    "生成一份安全运营日报，保存到 /data/reports/2026/06/08/daily-report.md",
]


def test_fob_da60_candidate_eval_cases_require_promotion_before_regression(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    _prepare_settings_file(module)
    calls: list[str] = []
    _install_fob_da60_agent_fakes(monkeypatch, module, calls)

    with TestClient(module.app) as client:
        batch = _create_fob_da60_like_batch(client)
        eval_generation_job = _run_one_agent_job(module)
        batch = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}").json()
        eval_cases = [module.feedback_store.find_eval_case(eval_case_id) for eval_case_id in batch["eval_case_ids"]]

        blocked_plan_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-plan")
        promote_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/promote",
            json={"operator": "tester", "reason": "fob-da60 批次回归前晋级", "asset_layer": "batch_specific", "blocking_policy": "blocking"},
        )
        promoted_plan_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-plan")

    assert eval_generation_job.status == "completed"
    assert calls == ["eval_case_generation"]
    assert len(eval_cases) == 2
    assert [case["prompt"] for case in eval_cases if case] == FOB_DA60_DAILY_REPORT_PROMPTS
    assert {case["status"] for case in eval_cases if case} == {"draft"}
    assert {case["asset_layer"] for case in eval_cases if case} == {"candidate"}
    assert {case["promotion_status"] for case in eval_cases if case} == {"candidate"}
    assert blocked_plan_response.status_code == 400
    blocked_payload = blocked_plan_response.json()
    assert blocked_payload["suggested_action"] == "promote_batch_eval_cases"
    assert blocked_payload["regression_asset_eligibility"]["summary"] == {
        "linked_total": 2,
        "eligible_linked": 0,
        "eligible_global": 0,
        "eligible_total": 0,
        "promotable_linked": 2,
        "ineligible_linked": 2,
        "missing_linked": 0,
    }
    assert promote_response.status_code == 200, promote_response.json()
    promoted_cases = promote_response.json()["promoted_eval_cases"]
    assert len(promoted_cases) == 2
    assert {case["status"] for case in promoted_cases} == {"active"}
    assert {case["asset_layer"] for case in promoted_cases} == {"batch_specific"}
    assert {case["promotion_status"] for case in promoted_cases} == {"approved"}
    assert {case["blocking_policy"] for case in promoted_cases} == {"blocking"}
    assert promote_response.json()["eligibility_summary"]["summary"]["eligible_total"] == 2
    for eval_case in promoted_cases:
        governance_events = module.feedback_store.list_eval_case_governance_events(eval_case["eval_case_id"])
        assert governance_events[0]["action"] == "promote"
    assert promoted_plan_response.status_code == 200, promoted_plan_response.json()
    assert promoted_plan_response.json()["selection_summary"]["total"] == 2


def test_fob_da60_optimization_closed_loop_runs_regression_after_promotion(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    settings_path = _prepare_settings_file(module)
    calls: list[str] = []
    _install_fob_da60_agent_fakes(monkeypatch, module, calls)

    with TestClient(module.app) as client:
        batch = _create_fob_da60_like_batch(client)
        eval_generation_job = _run_one_agent_job(module)
        batch = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}").json()
        blocked_plan_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-plan")
        promote_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/promote",
            json={"operator": "tester", "reason": "fob-da60 批次回归前晋级", "asset_layer": "batch_specific", "blocking_policy": "blocking"},
        )
        attribution_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/attribution-jobs", json={"force": True})
        attribution_job = _run_one_agent_job(module)
        plan_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan",
            json={"regeneration_instruction": "保持权限规则收敛，仅修复日报输出路径写入权限。"},
        )
        plan_job = _run_one_agent_job(module)
        batch = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}").json()
        plan_task = batch["optimization_plan"]["tasks"][0]
        execute_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan/tasks/{plan_task['plan_task_id']}/execute",
            json={"force": True},
        )
        execution_job = _run_one_agent_job(module)
        apply_response = client.post(
            f"/api/optimization-tasks/{execute_response.json()['optimization_task']['optimization_task_id']}"
            f"/execution-jobs/{execute_response.json()['execution_job']['execution_job_id']}/apply",
            json={"confirm": True},
        )
        regression_plan_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-plan")
        regression_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-runs",
            json={"regression_plan_id": regression_plan_response.json()["regression_plan_id"]},
        )
        impact_job = _run_one_agent_job(module)
        impact_response = client.get(f"/api/eval-runs/{regression_response.json()['eval_run']['eval_run_id']}/impact-analysis")
        change_set = apply_response.json()["optimization_task"]["latest_change_set"]
        publish_response = client.post(f"/api/agent-change-sets/{change_set['change_set_id']}/publish", json={"operator": "tester"})

    assert eval_generation_job.status == "completed"
    assert attribution_job.status == "completed"
    assert plan_job.status == "completed"
    assert execution_job.status == "completed"
    assert impact_job.status == "completed"
    assert calls == ["eval_case_generation", "attribution", "batch_plan", "execution", "regression_impact_analysis"]
    assert blocked_plan_response.status_code == 400
    assert blocked_plan_response.json()["regression_asset_eligibility"]["summary"]["promotable_linked"] == 2
    assert promote_response.status_code == 200
    assert attribution_response.status_code == 200
    assert plan_response.status_code == 200
    assert batch["optimization_plan"]["target_path"] == ".claude/settings.json"
    assert plan_task["target_path"] == ".claude/settings.json"
    assert execute_response.status_code == 200
    assert apply_response.status_code == 200
    assert regression_plan_response.status_code == 200
    assert regression_plan_response.json()["selection_summary"]["total"] == 2
    assert regression_response.status_code == 200, regression_response.json()
    assert regression_response.json()["eval_run"]["result_status"] == "passed"
    assert regression_response.json()["eval_run"]["summary"]["total"] == 2
    assert regression_response.json()["impact_analysis"]["status"] == "pending"
    assert impact_response.status_code == 200
    assert impact_response.json()["status"] == "completed"
    assert publish_response.status_code == 200, publish_response.json()
    assert publish_response.json()["commit_sha"] == change_set["candidate_commit_sha"]
    assert "daily_report_permissions" in settings_path.read_text(encoding="utf-8")
    candidate_settings = Path(change_set["worktree_path"]).joinpath(".claude/settings.json").read_text(encoding="utf-8")
    assert "Write(/data/outputs/**)" in candidate_settings
    assert "Write(/data/reports/**)" in candidate_settings


def _prepare_settings_file(module) -> Path:
    settings_path = module.settings.main_workspace_dir / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text('{"permissions":{"allow":["Read(/data/**)"]}}\n', encoding="utf-8")
    return settings_path


def _create_fob_da60_like_batch(client: TestClient) -> dict:
    signal_response = client.post(
        "/api/feedback-signals",
        json={
            "session_id": "sess-fob-da60-closed-loop",
            "labels": ["permission_gap", "daily_report"],
            "comment": "fob-da60 复现：日报输出路径写入权限不足，需重新生成并运行回归。",
            "confidence": "high",
        },
    )
    assert signal_response.status_code == 200, signal_response.json()
    batch_response = client.post(
        "/api/feedback-optimization-batches",
        json={
            "title": "反馈优化批次 fob-da60 回归闭环复现",
            "source_refs": [{"source_kind": "signal", "source_id": signal_response.json()["signal_id"]}],
        },
    )
    assert batch_response.status_code == 200, batch_response.json()
    return batch_response.json()


def _install_fob_da60_agent_fakes(monkeypatch, module, calls: list[str]) -> None:
    async def fake_run_profile_json(**kwargs):
        job_type = kwargs["job_type"]
        calls.append(job_type)
        job_input = kwargs["job_input"]
        if job_type == "eval_case_generation":
            feedback_case = job_input["feedback_cases"][0]["feedback_case"]
            source_run = job_input["feedback_cases"][0].get("source_run") or {}
            return {
                "job_id": job_input["job_id"],
                "scope_kind": job_input["scope_kind"],
                "scope_id": job_input["scope_id"],
                "status": "completed",
                "eval_cases": [
                    {
                        "schema_version": "feedback-eval-case/v1",
                        "status": "draft",
                        "source": "eval_case_governor",
                        "source_feedback_case_id": feedback_case["feedback_case_id"],
                        "source_run_id": source_run.get("run_id"),
                        "source_kind": "optimization_batch",
                        "source_id": job_input["batch_id"],
                        "source_refs": job_input.get("source_refs") or [],
                        "asset_layer": "candidate",
                        "promotion_status": "candidate",
                        "blocking_policy": "non_blocking",
                        "flaky_status": "stable",
                        "variant_role": "original_reproduction",
                        "prompt": prompt,
                        "expected_behavior": "日报文件必须成功写入指定路径，回答包含输出文件路径且无运行错误。",
                        "checks_json": {"requires_non_empty_answer": True, "requires_no_runtime_errors": True},
                        "labels": ["feedback_optimization", "daily_report", "permission_gap"],
                    }
                    for prompt in FOB_DA60_DAILY_REPORT_PROMPTS
                ],
                "results": [],
            }
        if job_type == "attribution":
            return {
                "feedback_case_id": job_input["feedback_case_id"],
                "attribution_job_id": job_input["job_id"],
                "status": "completed",
                "problem_type": "instruction_gap",
                "optimization_object_type": "main_agent_claude_md",
                "actionability": "direct_workspace_change",
                "confidence": "high",
                "human_review_required": False,
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "日报输出路径写入失败。"}],
                "responsibility_boundary": {"owner": "main_agent_workspace", "reason": ".claude/settings.json 缺少日报输出目录写权限。"},
                "rationale": "用户要求写入 /data/outputs 和 /data/reports 下的日报文件，当前权限规则未覆盖。",
                "recommended_next_step": "generate_proposal",
            }
        if job_type == "batch_plan":
            return _fob_da60_batch_plan_output(job_input)
        if job_type == "execution":
            return _fob_da60_execution_output(job_input)
        if job_type == "regression_impact_analysis":
            eval_run = job_input["eval_run"]
            return {
                "eval_run_id": job_input["eval_run_id"],
                "status": "completed",
                "result_status": eval_run["result_status"],
                "gate_result": eval_run["gate_result"],
                "impacted_assets": [],
                "recommendations": ["回归通过，可保留权限修复并继续发布。"],
                "summary": "日报输出路径相关回归用例均通过。",
                "risk_assessment": "low",
                "next_steps": [],
            }
        raise AssertionError(f"unexpected job_type: {job_type}")

    async def fake_run_feedback_eval(
        *,
        eval_case_ids=None,
        optimization_task_id=None,
        source="optimization_batch_regression",
        regression_plan_id=None,
        **kwargs,
    ):
        eval_case_ids = [str(item) for item in eval_case_ids or []]
        run = module.feedback_store.create_eval_run(
            eval_case_ids=eval_case_ids,
            agent_version_id=module.agent_version_store.current_version_id(),
            optimization_task_id=optimization_task_id,
            source=source,
            regression_plan_id=regression_plan_id,
        )
        for eval_case_id in eval_case_ids:
            eval_case = module.feedback_store.find_eval_case(eval_case_id)
            module.feedback_store.append_eval_run_item(
                run["eval_run_id"],
                eval_case=eval_case,
                agent_result={
                    "run_id": f"run-regression-{eval_case_id}",
                    "agent_version_id": module.agent_version_store.current_version_id(),
                    "answer": "日报已生成，输出路径写入成功。",
                },
                status="passed",
                score=1.0,
                check_results=[
                    {"name": "requires_non_empty_answer", "passed": True},
                    {"name": "requires_no_runtime_errors", "passed": True},
                ],
            )
        return module.feedback_store.finish_eval_run(run["eval_run_id"])

    monkeypatch.setattr(module.runtime, "_run_profile_json", fake_run_profile_json)
    monkeypatch.setattr(module.runtime, "run_feedback_eval", fake_run_feedback_eval)


def _fob_da60_batch_plan_output(job_input: dict) -> dict:
    return {
        "batch_id": job_input["batch_id"],
        "status": "pending_execution",
        "title": "修复日报输出路径写入权限",
        "summary": "在主智能体项目配置中补充日报输出目录写权限。",
        "problem_types": ["instruction_gap"],
        "confidence": "high",
        "actionability": "direct_workspace_change",
        "target_type": "main_agent_claude_md",
        "target_path": ".claude/settings.json",
        "recommendation": "补充 /data/outputs 与 /data/reports 的 Write 权限，确保日报可保存到用户指定路径。",
        "expected_effect": "日报生成任务可以完成文件写入并返回输出路径。",
        "validation": "运行本批次两条日报路径回归用例。",
        "risk": "权限范围仅覆盖日报输出目录。",
        "source_refs": job_input["source_refs"],
        "feedback_case_ids": job_input["feedback_case_ids"],
        "eval_case_ids": job_input["eval_case_ids"],
        "attribution_job_ids": job_input["attribution_job_ids"],
        "attribution_summaries": [],
        "rationale": "归因结果指向 .claude/settings.json 权限缺口。",
        "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈说明日报路径写入失败。"}],
        "tasks": [
            {
                "execution_kind": "workspace_execution",
                "status": "pending_execution",
                "title": "增加日报输出目录写权限",
                "description": "在 .claude/settings.json 中增加日报输出目录的 Write 权限。",
                "objective": "允许主智能体把日报保存到 /data/outputs 和 /data/reports。",
                "target_summary": "workspace:.claude/settings.json",
                "target_type": "main_agent_claude_md",
                "target_path": ".claude/settings.json",
                "owner": "main_agent_workspace",
                "actionability": "direct_workspace_change",
                "confidence": "high",
                "problem_type": "instruction_gap",
                "recommendation": "补充收敛的日报输出路径写权限。",
                "recommended_actions": ["更新 .claude/settings.json permissions.allow。"],
                "acceptance_criteria": ["两条日报路径回归用例通过。"],
                "expected_effect": "日报文件写入不再被权限拒绝。",
                "validation": "运行批次回归测试。",
                "risk": "权限范围需保持在日报输出目录。",
                "analysis_summary": "权限规则缺少日报输出目录。",
                "evidence_summary": "反馈指出日报保存路径写入失败。",
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "日报输出失败。"}],
                "task_context": {"target_file": ".claude/settings.json", "permission_scope": "daily_report_outputs"},
                "feedback_case_ids": job_input["feedback_case_ids"],
                "eval_case_ids": job_input["eval_case_ids"],
                "attribution_job_ids": job_input["attribution_job_ids"],
            }
        ],
        "blocked_items": [],
    }


def _fob_da60_execution_output(job_input: dict) -> dict:
    return {
        "optimization_task_id": job_input["optimization_task_id"],
        "execution_job_id": job_input["execution_job_id"],
        "status": "ready",
        "baseline_agent_version_id": job_input["baseline_agent_version_id"],
        "summary": "替换 .claude/settings.json，补充日报输出路径权限。",
        "operations": [
            {
                "operation": "replace_file",
                "path": ".claude/settings.json",
                "content": (
                    '{\n'
                    '  "daily_report_permissions": true,\n'
                    '  "permissions": {\n'
                    '    "allow": [\n'
                    '      "Read(/data/**)",\n'
                    '      "Write(/data/outputs/**)",\n'
                    '      "Write(/data/reports/**)"\n'
                    "    ]\n"
                    "  }\n"
                    "}\n"
                ),
                "rationale": "允许日报保存到本批次回归用例指定的输出目录。",
            }
        ],
        "validation": "运行批次回归测试。",
        "risk": "仅增加日报输出目录写权限。",
        "human_review_required": True,
    }
