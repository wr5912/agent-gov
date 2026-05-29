import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.runtime.schemas import FeedbackSignalCreateRequest
from app.services.execution_application import ExecutionApplicationError


def _load_app(monkeypatch, tmp_path):
    root = tmp_path / "docker" / "volume"
    workspace = root / "main-workspace"
    data = root / "data"
    claude_root = root / "claude-roots" / "main"
    attribution_workspace = root / "attribution-analyzer-workspace"
    proposal_workspace = root / "proposal-generator-workspace"
    optimizer_workspace = root / "execution-optimizer-workspace"
    attribution_root = root / "claude-roots" / "attribution-analyzer"
    proposal_root = root / "claude-roots" / "proposal-generator"
    optimizer_root = root / "claude-roots" / "execution-optimizer"
    for path in (
        workspace,
        data,
        claude_root / ".claude",
        attribution_workspace,
        proposal_workspace,
        optimizer_workspace,
        attribution_root / ".claude",
        proposal_root / ".claude",
        optimizer_root / ".claude",
    ):
        path.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("CLAUDE.md").write_text("原始规则\n", encoding="utf-8")

    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ATTRIBUTION_ANALYZER_WORKSPACE_DIR", str(attribution_workspace))
    monkeypatch.setenv("PROPOSAL_GENERATOR_WORKSPACE_DIR", str(proposal_workspace))
    monkeypatch.setenv("EXECUTION_OPTIMIZER_WORKSPACE_DIR", str(optimizer_workspace))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("ATTRIBUTION_ANALYZER_CLAUDE_ROOT", str(attribution_root))
    monkeypatch.setenv("PROPOSAL_GENERATOR_CLAUDE_ROOT", str(proposal_root))
    monkeypatch.setenv("EXECUTION_OPTIMIZER_CLAUDE_ROOT", str(optimizer_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("MODEL_PROVIDER_API_KEY", "")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    if "app.main" in sys.modules:
        module = importlib.reload(sys.modules["app.main"])
    else:
        module = importlib.import_module("app.main")
    return module


def _approved_task(module):
    store = module.feedback_store
    store.record_run(
        {
            "run_id": "run-api",
            "session_id": "sess-api",
            "message": "列出 workspace 配置",
            "answer_summary": "未读取配置。",
            "agent_activity": {"tool_calls": []},
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-api",
            labels=["tool_data_incomplete"],
            comment="执行优化 API 测试",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="执行优化 API 测试")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "instruction_gap",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答前未核查配置。"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "主智能体指令需要补强。"},
            "rationale": "回答 workspace 配置类问题前应读取配置文件。",
            "recommended_next_step": "generate_proposal",
        },
    )
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-api-exec",
                    "title": "补充配置读取要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "在 CLAUDE.md 增加回答配置类问题前读取配置的要求。",
                    "expected_effect": "回答更完整。",
                    "validation": "复测 workspace 配置类问题。",
                    "risk": "响应耗时可能增加。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    return store.create_task(proposal_id=proposal["proposal_id"])


def _ready_execution_job(module, task):
    job = module.feedback_store.create_execution_job(task["optimization_task_id"], force=True)
    module.feedback_store.start_execution_job(job["execution_job_id"])
    return module.feedback_store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "追加配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n回答配置类问题前必须读取当前 workspace 配置。\n",
                    "rationale": "补强主智能体指令。",
                }
            ],
            "validation": "复测 workspace 配置类问题。",
            "risk": "响应耗时可能增加。",
            "human_review_required": True,
        },
    )


def test_create_execution_job_endpoint_uses_offline_review_when_provider_missing(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        task = _approved_task(module)
        response = client.post(f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs", json={"force": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_human_review"
    assert payload["validated_output_json"]["no_action_reason"] == "MODEL_PROVIDER_NOT_CONFIGURED"


def test_apply_execution_job_endpoint_writes_file_and_creates_versions(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        updated_task = response.json()["optimization_task"]
        diff_response = client.get(
            "/api/agent-versions/main/file-diff",
            params={
                "from_version_id": updated_task["pre_execution_agent_version_id"],
                "to_version_id": updated_task["applied_agent_version_id"],
                "path": "CLAUDE.md",
            },
        )

    assert response.status_code == 200
    assert "回答配置类问题前必须读取" in workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    assert updated_task["status"] == "applied_pending_regression"
    assert updated_task["pre_execution_agent_version_id"]
    assert updated_task["applied_agent_version_id"]
    assert diff_response.status_code == 200
    assert diff_response.json()["status"] == "modified"
    assert "+回答配置类问题前必须读取" in diff_response.json()["unified_diff"]


def test_apply_execution_job_rejects_baseline_conflict(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        module.agent_version_store.create_snapshot(reason="manual_snapshot", note="制造 baseline 冲突。")
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert "baseline" in response.json()["detail"].lower()


def test_apply_execution_job_rejects_target_hash_conflict(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        workspace.joinpath("CLAUDE.md").write_text("人工提前修改\n", encoding="utf-8")
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert "changed before apply" in response.json()["detail"]
    assert failed_job["status"] == "failed"
    assert failed_job["error_json"]["error_code"] == "EXECUTION_APPLY_FAILED"


def test_apply_execution_job_restores_workspace_when_state_sync_fails(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")

    def fail_mark(*args, **kwargs):
        raise RuntimeError("mark failed")

    monkeypatch.setattr(module.feedback_store, "mark_execution_job_applied", fail_mark)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        job_response = client.get(f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs")
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])

    assert response.status_code == 409
    assert "restored to pre-execution version" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert failed_job["status"] == "failed"
    assert failed_job["error_json"]["error_code"] == "EXECUTION_APPLY_STATE_SYNC_FAILED"
    compensations = module.feedback_store.list_execution_compensations(execution_job_id=job["execution_job_id"])
    assert len(compensations) == 1
    assert compensations[0]["status"] == "resolved"
    assert compensations[0]["restore_status"] == "restored"
    assert compensations[0]["optimization_task_id"] == task["optimization_task_id"]
    assert job_response.status_code == 200
    assert job_response.json()[0]["compensations"][0]["compensation_id"] == compensations[0]["compensation_id"]


def test_apply_execution_job_restores_workspace_when_applied_snapshot_fails(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    create_snapshot = module.agent_version_store.create_snapshot

    def fail_applied_snapshot(*args, **kwargs):
        if kwargs.get("reason") == "execution_optimizer_applied":
            raise RuntimeError("snapshot failed")
        return create_snapshot(*args, **kwargs)

    monkeypatch.setattr(module.agent_version_store, "create_snapshot", fail_applied_snapshot)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])

    assert response.status_code == 409
    assert "restored to pre-execution version" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert failed_job["status"] == "failed"
    assert failed_job["error_json"]["error_code"] == "EXECUTION_APPLY_STATE_SYNC_FAILED"
    compensations = module.feedback_store.list_execution_compensations(status="resolved")
    assert len(compensations) == 1
    assert compensations[0]["execution_job_id"] == job["execution_job_id"]


def test_execution_compensation_api_lists_filters_and_gets_records(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    resolved = module.feedback_store.record_execution_compensation(
        optimization_task_id="opt-resolved",
        execution_job_id="fbe-resolved",
        pre_execution_agent_version_id="agent-version-before",
        restore_status="restored",
        original_error="state sync failed",
    )
    pending = module.feedback_store.record_execution_compensation(
        optimization_task_id="opt-pending",
        execution_job_id="fbe-pending",
        pre_execution_agent_version_id="agent-version-before",
        restore_status="restore_failed",
        original_error="state sync failed",
        restore_error="restore failed",
    )

    with TestClient(module.app) as client:
        pending_response = client.get("/api/execution-compensations?status=pending_manual_recovery")
        task_response = client.get("/api/execution-compensations?optimization_task_id=opt-resolved")
        job_response = client.get("/api/execution-compensations?execution_job_id=fbe-pending")
        detail_response = client.get(f"/api/execution-compensations/{pending['compensation_id']}")
        missing_response = client.get("/api/execution-compensations/fco-missing")

    assert pending_response.status_code == 200
    assert [item["compensation_id"] for item in pending_response.json()] == [pending["compensation_id"]]
    assert task_response.status_code == 200
    assert [item["compensation_id"] for item in task_response.json()] == [resolved["compensation_id"]]
    assert job_response.status_code == 200
    assert [item["compensation_id"] for item in job_response.json()] == [pending["compensation_id"]]
    assert detail_response.status_code == 200
    assert detail_response.json()["restore_error"] == "restore failed"
    assert missing_response.status_code == 404
    assert missing_response.json()["error_code"] == "NOT_FOUND"


def test_apply_execution_job_records_pending_compensation_when_restore_fails(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    original_restore = module.agent_version_store.restore_version

    def fail_mark(*args, **kwargs):
        raise RuntimeError("mark failed")

    def fail_restore(*args, **kwargs):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(module.feedback_store, "mark_execution_job_applied", fail_mark)
    monkeypatch.setattr(module.agent_version_store, "restore_version", fail_restore)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])

    assert response.status_code == 409
    assert "automatic restore also failed" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") != original_text
    assert failed_job["status"] == "failed"
    compensations = module.feedback_store.list_execution_compensations(status="pending_manual_recovery")
    assert len(compensations) == 1
    assert compensations[0]["execution_job_id"] == job["execution_job_id"]
    assert compensations[0]["restore_status"] == "restore_failed"
    assert "restore failed" in compensations[0]["restore_error"]

    monkeypatch.setattr(module.agent_version_store, "restore_version", original_restore)
    with TestClient(module.app) as client:
        restore_response = client.post(
            f"/api/execution-compensations/{compensations[0]['compensation_id']}/restore"
        )
        second_restore_response = client.post(
            f"/api/execution-compensations/{compensations[0]['compensation_id']}/restore"
        )
        job_response = client.get(f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs")

    assert restore_response.status_code == 200
    restored = restore_response.json()
    assert restored["status"] == "resolved"
    assert restored["restore_status"] == "restored"
    assert restored["restore_error"] is None
    assert restored["manual_restore_result"]["current_version"]["agent_version_id"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert job_response.json()[0]["compensations"][0]["status"] == "resolved"
    assert second_restore_response.status_code == 200
    assert second_restore_response.json()["compensation_id"] == restored["compensation_id"]
    assert second_restore_response.json()["status"] == "resolved"


def test_execution_compensation_restore_rejects_missing_pre_execution_version(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    compensation = module.feedback_store.record_execution_compensation(
        optimization_task_id="opt-missing-version",
        execution_job_id="fbe-missing-version",
        pre_execution_agent_version_id=None,
        restore_status="restore_failed",
        original_error="state sync failed",
        restore_error="restore failed",
    )

    with TestClient(module.app) as client:
        response = client.post(f"/api/execution-compensations/{compensation['compensation_id']}/restore")

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert "pre-execution version" in response.json()["detail"]


def test_execution_compensation_rejects_invalid_restore_status(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with pytest.raises(ValidationError):
        module.feedback_store.record_execution_compensation(
            optimization_task_id="opt-invalid",
            execution_job_id="fbe-invalid",
            pre_execution_agent_version_id=None,
            restore_status="unknown",
            original_error="state sync failed",
        )

    assert module.feedback_store.list_execution_compensations(limit=10) == []


def test_execution_application_rejects_symlink_escape(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not edit\n", encoding="utf-8")
    link = module.settings.main_workspace_dir / "escape.txt"
    link.symlink_to(outside)

    with pytest.raises(ExecutionApplicationError, match="escapes main workspace") as exc_info:
        module.execution_application.safe_workspace_target("escape.txt")
    assert exc_info.value.error_code == "CONFLICT"
    assert exc_info.value.status_code == 409


def test_feedback_optimization_batch_full_api_e2e(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    calls: list[str] = []

    async def fake_run_profile_json(**kwargs):
        job_type = kwargs["job_type"]
        calls.append(job_type)
        job_input = kwargs["job_input"]
        if job_type == "attribution":
            return {
                "schema_version": "attribution-output/v1",
                "feedback_case_id": job_input["feedback_case_id"],
                "attribution_job_id": job_input["job_id"],
                "status": "completed",
                "problem_type": "tool_misuse",
                "optimization_object_type": "main_agent_claude_md",
                "actionability": "direct_workspace_change",
                "confidence": "high",
                "human_review_required": False,
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈指出回答未读取当前配置。"}],
                "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "主智能体指令缺少配置核查要求。"},
                "rationale": "回答 workspace 配置类问题前没有读取当前 CLAUDE.md 或相关配置文件。",
                "recommended_next_step": "generate_proposal",
            }
        if job_type == "batch_plan":
            return {
                "schema_version": "feedback-optimization-plan-output/v1",
                "batch_id": job_input["batch_id"],
                "status": "pending_approval",
                "title": "补强工作区配置核查",
                "summary": "根据归因结果生成一个 workspace 优化任务。",
                "problem_types": ["tool_misuse"],
                "confidence": "high",
                "actionability": "direct_workspace_change",
                "target_type": "main_agent_claude_md",
                "target_path": "CLAUDE.md",
                "recommendation": "在 CLAUDE.md 中补充回答工作区配置问题前必须读取配置文件的要求。",
                "expected_effect": "Agent 回答同类问题时基于当前配置作答。",
                "validation": "使用批次回归用例验证回答是否完整且无运行错误。",
                "risk": "可能增加一次文件读取工具调用。",
                "source_refs": job_input["source_refs"],
                "feedback_case_ids": job_input["feedback_case_ids"],
                "eval_case_ids": job_input["eval_case_ids"],
                "attribution_job_ids": job_input["attribution_job_ids"],
                "attribution_summaries": [],
                "rationale": "归因结果显示配置类问题回答依赖记忆，没有读取当前工作区文件。",
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈备注要求数据完整。"}],
                "tasks": [
                    {
                        "execution_kind": "workspace_execution",
                        "status": "pending_execution",
                        "title": "补充配置核查指令",
                        "description": "在主智能体 CLAUDE.md 中增加配置类问题必须读取当前配置的要求。",
                        "objective": "让 Agent 对 workspace 配置枚举类问题使用当前文件内容作答。",
                        "target_summary": "workspace:CLAUDE.md",
                        "target_type": "main_agent_claude_md",
                        "target_path": "CLAUDE.md",
                        "owner": "main_agent_workspace",
                        "actionability": "direct_workspace_change",
                        "confidence": "high",
                        "problem_type": "tool_misuse",
                        "recommendation": "追加一条配置核查规则。",
                        "recommended_actions": ["由 execution-optimizer 生成 CLAUDE.md 的受控追加方案。"],
                        "acceptance_criteria": ["批次回归用例通过，回答配置问题前读取当前配置文件。"],
                        "expected_effect": "同类反馈不再复现。",
                        "validation": "运行批次回归测试。",
                        "risk": "回答耗时略增。",
                        "analysis_summary": "Agent 未读取当前配置。",
                        "evidence_summary": "反馈和归因均指向配置核查缺口。",
                        "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈指出数据不全。"}],
                        "task_context": {"target_file": "CLAUDE.md", "config_section": "workspace-capability-answering"},
                        "feedback_case_ids": job_input["feedback_case_ids"],
                        "eval_case_ids": job_input["eval_case_ids"],
                        "attribution_job_ids": job_input["attribution_job_ids"],
                    }
                ],
                "blocked_items": [],
            }
        if job_type == "execution":
            return {
                "schema_version": "execution-plan-output/v1",
                "optimization_task_id": job_input["optimization_task_id"],
                "execution_job_id": job_input["execution_job_id"],
                "status": "ready",
                "baseline_agent_version_id": job_input["baseline_agent_version_id"],
                "summary": "追加配置核查指令。",
                "operations": [
                    {
                        "operation": "append_text",
                        "path": "CLAUDE.md",
                        "append_text": "\n回答 workspace 配置类问题前必须读取当前配置文件。\n",
                        "rationale": "落实优化方案中的配置核查要求。",
                    }
                ],
                "validation": "运行批次回归测试。",
                "risk": "响应耗时可能略增。",
                "human_review_required": True,
            }
        raise AssertionError(f"unexpected job_type: {job_type}")

    async def fake_run_feedback_eval(*, eval_case_ids=None, optimization_task_id=None, source="optimization_batch_regression"):
        eval_case_ids = [str(item) for item in eval_case_ids or []]
        run = module.feedback_store.create_eval_run(
            eval_case_ids=eval_case_ids,
            agent_version_id=module.agent_version_store.current_version_id(),
            optimization_task_id=optimization_task_id,
            source=source,
        )
        for eval_case_id in eval_case_ids:
            eval_case = module.feedback_store.find_eval_case(eval_case_id)
            module.feedback_store.append_eval_run_item(
                run["eval_run_id"],
                eval_case=eval_case,
                agent_result={
                    "run_id": f"run-regression-{eval_case_id}",
                    "agent_version_id": module.agent_version_store.current_version_id(),
                    "answer": "已读取当前配置并完整回答。",
                },
                status="passed",
                score=1.0,
                check_results=[{"name": "requires_non_empty_answer", "passed": True}],
            )
        return module.feedback_store.finish_eval_run(run["eval_run_id"])

    monkeypatch.setattr(module.runtime, "_provider_configured", lambda: True)
    monkeypatch.setattr(module.runtime, "_run_profile_json", fake_run_profile_json)
    monkeypatch.setattr(module.runtime, "run_feedback_eval", fake_run_feedback_eval)

    with TestClient(module.app) as client:
        signal_response = client.post(
            "/api/feedback-signals",
            json={
                "session_id": "sess-batch-e2e",
                "labels": ["tool_data_incomplete"],
                "comment": "E2E 批次闭环：回答 workspace 配置时数据不全",
                "confidence": "high",
            },
        )
        signal = signal_response.json()
        batch_response = client.post(
            "/api/feedback-optimization-batches",
            json={
                "title": "E2E 批次闭环",
                "source_refs": [{"source_kind": "signal", "source_id": signal["signal_id"]}],
            },
        )
        batch = batch_response.json()
        attribution_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/attribution-jobs", json={"force": True})
        plan_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan",
            json={"regeneration_instruction": "保持指令简洁，避免修改无关文件。"},
        )
        plan = plan_response.json()["optimization_plan"]
        plan_task = plan["tasks"][0]
        execute_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan/tasks/{plan_task['plan_task_id']}/execute",
            json={"force": True},
        )
        regression_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-runs")
        final_batch = regression_response.json()["batch"]

    assert signal_response.status_code == 200
    assert batch_response.status_code == 200
    assert attribution_response.status_code == 200
    assert plan_response.status_code == 200
    assert execute_response.status_code == 200
    assert regression_response.status_code == 200
    assert calls == ["attribution", "batch_plan", "execution"]
    assert plan["generated_by"] == "proposal-generator"
    assert plan["optimization_plan_job_id"]
    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v2"
    assert plan_task["task_context"]["target_file"] == "CLAUDE.md"
    assert execute_response.json()["execution_job"]["status"] == "ready"
    assert execute_response.json()["optimization_task"]["applied_agent_version_id"]
    assert "回答 workspace 配置类问题前必须读取当前配置文件" in workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    assert regression_response.json()["eval_run"]["result_status"] == "passed"
    assert final_batch["status"] == "completed"
