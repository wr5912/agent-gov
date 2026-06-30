import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.services.agent_job_worker import AgentJobWorker
from app.services.execution_application import ExecutionApplicationError
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _load_app(monkeypatch, tmp_path, *, api_key=""):
    root = tmp_path / "docker" / "volume"
    workspace = root / "main-workspace"
    data = root / "data"
    claude_root = root / "claude-roots" / "main"
    governor_workspace = root / "governor-workspace"
    governor_root = root / "claude-roots" / "governor"
    agent_worktrees = data / "business-agents" / "main-agent" / "version" / "worktrees"
    release_archives = data / "business-agents" / "main-agent" / "version" / "releases"
    for path in (
        workspace,
        data,
        claude_root / ".claude",
        governor_workspace,
        governor_root / ".claude",
        agent_worktrees,
        release_archives,
    ):
        path.mkdir(parents=True, exist_ok=True)
    # main 已并入业务模型：执行针对派生的 main-agent workspace（/data 下），在那里写起始 CLAUDE.md。
    main_ws = data / "business-agents" / "main-agent" / "workspace"
    main_ws.mkdir(parents=True, exist_ok=True)
    main_ws.joinpath("CLAUDE.md").write_text("原始规则\n", encoding="utf-8")

    monkeypatch.setenv("RUNTIME_CONTAINER", "0")
    monkeypatch.setenv("RUNTIME_VOLUME_MODE", "local-debug")
    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", str(root))
    monkeypatch.setenv("HOST_DATA_MOUNT", str(data))
    monkeypatch.setenv("HOST_GOVERNOR_WORKSPACE_MOUNT", str(governor_workspace))
    monkeypatch.setenv("HOST_GOVERNOR_CLAUDE_ROOT_MOUNT", str(governor_root))
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("GOVERNOR_WORKSPACE_DIR", str(governor_workspace))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("GOVERNOR_CLAUDE_ROOT", str(governor_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("MODEL_PROVIDER_API_KEY", "")
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("AGENT_GIT_REPOSITORY_DIR", str(main_ws))
    monkeypatch.setenv("AGENT_GIT_WORKTREES_DIR", str(agent_worktrees))
    monkeypatch.setenv("AGENT_RELEASE_ARCHIVES_DIR", str(release_archives))
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
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    if batch.get("eval_case_generation_job_id"):
        store._discard_job(batch["eval_case_generation_job_id"])  # noqa: SLF001 - keep this endpoint test focused on execution jobs.
    plan_job = store.create_batch_plan_job(batch["batch_id"])
    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        {
            "batch_id": batch["batch_id"],
            "status": "pending_approval",
            "title": "补充配置读取要求",
            "summary": "在 CLAUDE.md 增加回答配置类问题前读取配置的要求。",
            "confidence": "high",
            "actionability": "direct_workspace_change",
            "target_type": "main_agent_claude_md",
            "target_path": "CLAUDE.md",
            "recommendation": "在 CLAUDE.md 增加回答配置类问题前读取配置的要求。",
            "expected_effect": "回答更完整。",
            "validation": "复测 workspace 配置类问题。",
            "risk": "响应耗时可能增加。",
            "feedback_case_ids": [feedback_case["feedback_case_id"]],
            "attribution_job_ids": [attribution_job["job_id"]],
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "status": "pending_execution",
                    "title": "补充配置读取要求",
                    "description": "在 CLAUDE.md 增加回答配置类问题前读取配置的要求。",
                    "objective": "回答配置类问题前读取当前配置。",
                    "target_summary": "CLAUDE.md",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "owner": "main_agent_workspace",
                    "actionability": "direct_workspace_change",
                    "recommendation": "在 CLAUDE.md 增加回答配置类问题前读取配置的要求。",
                    "recommended_actions": ["修改 CLAUDE.md"],
                    "acceptance_criteria": ["复测 workspace 配置类问题。"],
                    "expected_effect": "回答更完整。",
                    "validation": "复测 workspace 配置类问题。",
                    "risk": "响应耗时可能增加。",
                    "feedback_case_ids": [feedback_case["feedback_case_id"]],
                    "eval_case_ids": [],
                    "attribution_job_ids": [attribution_job["job_id"]],
                }
            ],
            "blocked_items": [],
        },
    )
    plan_task = completed["validated_output_json"]["tasks"][0]
    return store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])["optimization_task"]


def _ready_execution_job(module, task):
    job = module.feedback_store.create_execution_job(task["optimization_task_id"], force=True)
    module.feedback_store.start_execution_job(job["execution_job_id"])
    return module.feedback_store.complete_execution_job(
        job["execution_job_id"],
        {
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


def test_batch_attribution_force_rerun_allows_completed_attribution_batch(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    store = module.feedback_store
    store.record_run(
        {
            "run_id": "run-force-attribution",
            "session_id": "sess-force-attribution",
            "message": "日报文件没有写入",
            "answer_summary": "需要重新归因。",
            "agent_activity": {"tool_calls": []},
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-force-attribution",
            labels=["tool_data_incomplete"],
            comment="重新归因 API 测试",
        )
    )
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="重新归因 API 测试")
    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "feedback_case_id": feedback_case_id,
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "instruction_gap",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答没有完成用户要求。"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "主智能体指令需要补强。"},
            "rationale": "回答前应确认文件写入结果。",
            "recommended_next_step": "generate_proposal",
        },
    )
    completed_batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])

    with TestClient(module.app) as client:
        response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/attribution-jobs",
            json={"force": True},
        )

    assert completed_batch["status"] == "attribution_completed"
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["batch"]["status"] == "attribution_running"
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["job_id"] != attribution_job["job_id"]
    assert payload["batch"]["attribution_job_ids"] == [payload["jobs"][0]["job_id"]]
    assert store.get_job(attribution_job["job_id"]) is None


def _run_one_agent_job(module):
    worker = AgentJobWorker(feedback_store=module.feedback_store, run_profile_json=module.runtime._run_profile_json)
    return asyncio.run(worker.run_once())


def test_create_execution_job_endpoint_reports_agent_failure(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    async def fail_run_profile_json(**kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(module.runtime, "_run_profile_json", fail_run_profile_json)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        response = client.post(f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs", json={"force": True})

    assert response.status_code == 200
    payload = response.json()
    failed = _run_one_agent_job(module)
    assert payload["status"] == "queued"
    assert failed.job_id == payload["job_id"]
    assert failed.status == "failed"
    assert failed.error_json is not None
    assert failed.error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert failed.error_json.message is not None
    assert "model unavailable" in failed.error_json.message


def test_regression_impact_analysis_endpoints_can_force_requeue(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    store = module.feedback_store
    store.record_run(
        {
            "run_id": "run-impact-force",
            "session_id": "sess-impact-force",
            "message": "复测回归影响分析",
            "answer_summary": "通过",
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-impact-force",
            labels=["tool_data_incomplete"],
            comment="验证 impact analysis force requeue",
        )
    )
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
        title="impact force 批次",
    )
    eval_run = store.create_eval_run(eval_case_ids=[], agent_version_id="main-v-test", source="optimization_batch_regression")
    existing = store.queue_regression_impact_agent_job(eval_run["eval_run_id"], force=True)

    with TestClient(module.app) as client:
        reused_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-runs/{eval_run['eval_run_id']}/impact-analysis")
        batch_forced_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-runs/{eval_run['eval_run_id']}/impact-analysis",
            params={"force": True},
        )
        eval_forced_response = client.post(
            f"/api/eval-runs/{eval_run['eval_run_id']}/impact-analysis",
            params={"force": True},
        )

    assert reused_response.status_code == 200
    assert batch_forced_response.status_code == 200
    assert eval_forced_response.status_code == 200
    assert reused_response.json()["job_id"] == existing["job_id"]
    assert batch_forced_response.json()["job_id"] != existing["job_id"]
    assert eval_forced_response.json()["job_id"] != batch_forced_response.json()["job_id"]
    assert store.get_regression_impact_analysis(eval_run["eval_run_id"])["job_id"] == eval_forced_response.json()["job_id"]


def test_apply_execution_job_endpoint_writes_file_and_creates_versions(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        updated_task = response.json()["optimization_task"]
        change_set = updated_task["latest_change_set"]
        diff_response = client.get(
            f"/api/agent-change-sets/{change_set['change_set_id']}/file-diff",
            params={"path": "CLAUDE.md"},
        )

    assert response.status_code == 200
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert "回答配置类问题前必须读取" in Path(change_set["worktree_path"]).joinpath("CLAUDE.md").read_text(encoding="utf-8")
    assert updated_task["status"] == "applied_pending_regression"
    assert updated_task["latest_change_set_id"] == change_set["change_set_id"]
    assert change_set["status"] == "candidate_committed"
    assert change_set["candidate_commit_sha"]
    assert updated_task["pre_execution_agent_version_id"]
    assert updated_task["applied_agent_version_id"]
    assert diff_response.status_code == 200
    assert diff_response.json()["status"] == "modified"
    assert "+回答配置类问题前必须读取" in diff_response.json()["unified_diff"]


def test_agent_repository_status_reports_dirty_files_and_discards_confirmed_paths(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.agent_version_store.ensure_bootstrap()
    secret_path = module.settings.main_workspace_dir / ".mcp.json"
    secret_path.write_text('{"api_key":"secret-value"}\n', encoding="utf-8")

    with TestClient(module.app) as client:
        status_response = client.get("/api/agent-repository")
        discard_response = client.post("/api/agent-repository/discard-changes", json={"paths": [".mcp.json"]})

    assert status_response.status_code == 200
    status = status_response.json()
    assert status["dirty"] is True
    assert status["changed_file_count"] == 1
    assert status["changed_files"][0]["path"] == ".mcp.json"
    assert status["file_diffs"][0]["path"] == ".mcp.json"
    assert "[redacted sensitive line]" in status["file_diffs"][0]["unified_diff"]
    assert "secret-value" not in status["file_diffs"][0]["unified_diff"]
    assert discard_response.status_code == 200
    assert discard_response.json()["dirty"] is False
    assert not secret_path.exists()


def test_apply_execution_job_rejects_baseline_conflict(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        module.settings.main_workspace_dir.joinpath("CLAUDE.md").write_text("制造 baseline 冲突\n", encoding="utf-8")
        module.agent_version_store.create_snapshot(reason="manual_snapshot", note="制造 baseline 冲突。")
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert "baseline" in response.json()["detail"].lower()


def test_mark_task_applied_endpoint_creates_agent_version(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        task = _approved_task(module)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/mark-applied",
            json={"note": "人工已应用"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "applied_pending_regression"
    assert payload["applied_agent_version_id"]
    assert payload["application_note"] == "人工已应用"


def test_mark_task_applied_rejects_invalid_status_without_snapshot(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        task = _approved_task(module)
        module.feedback_store.update_task_status(task["optimization_task_id"], status="execution_planning")
        change_set_count = len(module.agent_governance.list_change_sets())
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/mark-applied",
            json={"note": "不应创建版本"},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert len(module.agent_governance.list_change_sets()) == change_set_count


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
        application = module.feedback_store.latest_execution_application(job["execution_job_id"])

    assert response.status_code == 409
    assert response.json()["error_code"] == "MAIN_WORKSPACE_DIRTY"
    assert "uncommitted changes" in response.json()["detail"]
    assert response.json()["changed_files"][0]["path"] == "CLAUDE.md"
    assert failed_job["status"] == "completed"
    assert application is None


def test_apply_execution_job_restores_workspace_when_state_sync_fails(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")

    def fail_application_record(*args, **kwargs):
        raise RuntimeError("mark failed")

    monkeypatch.setattr(module.feedback_store, "record_execution_application_applied", fail_application_record)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        job_response = client.get(f"/api/agent-jobs/{job['execution_job_id']}")
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])
        application = module.feedback_store.latest_execution_application(job["execution_job_id"])

    assert response.status_code == 409
    assert "restored to pre-execution version" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert failed_job["status"] == "completed"
    assert application["status"] == "compensated"
    assert application["error_json"]["error_code"] == "EXECUTION_APPLY_FAILED"
    compensations = module.feedback_store.list_execution_compensations(execution_job_id=job["execution_job_id"])
    assert len(compensations) == 1
    assert compensations[0]["status"] == "resolved"
    assert compensations[0]["restore_status"] == "restored"
    assert compensations[0]["optimization_task_id"] == task["optimization_task_id"]
    assert job_response.status_code == 200
    assert job_response.json()["compensations"][0]["compensation_id"] == compensations[0]["compensation_id"]


def test_apply_execution_job_restores_workspace_when_applied_snapshot_fails(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    workspace = module.settings.main_workspace_dir
    original_text = workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")

    def fail_candidate_commit(*args, **kwargs):
        raise RuntimeError("candidate commit failed")

    monkeypatch.setattr(module.agent_version_store, "commit_worktree", fail_candidate_commit)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])
        application = module.feedback_store.latest_execution_application(job["execution_job_id"])

    assert response.status_code == 409
    assert "restored to pre-execution version" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert failed_job["status"] == "completed"
    assert application["status"] == "compensated"
    assert application["error_json"]["error_code"] == "EXECUTION_APPLY_FAILED"
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

    def fail_application_record(*args, **kwargs):
        raise RuntimeError("mark failed")

    def fail_restore(*args, **kwargs):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(module.feedback_store, "record_execution_application_applied", fail_application_record)
    monkeypatch.setattr(module.agent_version_store, "restore_version", fail_restore)

    with TestClient(module.app) as client:
        task = _approved_task(module)
        job = _ready_execution_job(module, task)
        response = client.post(
            f"/api/optimization-tasks/{task['optimization_task_id']}/execution-jobs/{job['execution_job_id']}/apply",
            json={"confirm": True},
        )
        failed_job = module.feedback_store.get_execution_job(job["execution_job_id"])
        application = module.feedback_store.latest_execution_application(job["execution_job_id"])

    assert response.status_code == 409
    assert "automatic restore also failed" in response.json()["detail"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert failed_job["status"] == "completed"
    assert application["status"] == "pending_manual_recovery"
    compensations = module.feedback_store.list_execution_compensations(status="pending_manual_recovery")
    assert len(compensations) == 1
    assert compensations[0]["execution_job_id"] == job["execution_job_id"]
    assert compensations[0]["restore_status"] == "restore_failed"
    assert "restore failed" in compensations[0]["restore_error"]

    monkeypatch.setattr(module.agent_version_store, "restore_version", original_restore)
    with TestClient(module.app) as client:
        restore_response = client.post(f"/api/execution-compensations/{compensations[0]['compensation_id']}/restore")
        second_restore_response = client.post(f"/api/execution-compensations/{compensations[0]['compensation_id']}/restore")
        job_response = client.get(f"/api/agent-jobs/{job['execution_job_id']}")

    assert restore_response.status_code == 200
    restored = restore_response.json()
    assert restored["status"] == "resolved"
    assert restored["restore_status"] == "restored"
    assert restored["restore_error"] is None
    assert restored["manual_restore_result"]["current_version"]["agent_version_id"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == original_text
    assert job_response.json()["compensations"][0]["status"] == "resolved"
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
        if job_type == "eval_case_generation":
            eval_cases = []
            for item in job_input["feedback_cases"]:
                feedback_case = item["feedback_case"]
                source_run = item.get("source_run") or {}
                eval_cases.append(
                    {
                        "schema_version": "feedback-eval-case/v1",
                        "status": "draft",
                        "source": "eval_case_governor",
                        "source_feedback_case_id": feedback_case["feedback_case_id"],
                        "source_run_id": source_run.get("run_id"),
                        "source_kind": "optimization_batch",
                        "source_id": job_input["batch_id"],
                        "source_refs": item.get("source_refs") or [],
                        "asset_layer": "batch_specific",
                        "promotion_status": "candidate",
                        "blocking_policy": "non_blocking",
                        "flaky_status": "stable",
                        "variant_role": "original_reproduction",
                        "prompt": source_run.get("message") or "复测：回答 workspace 配置前读取当前配置。",
                        "expected_behavior": "必须读取当前配置并完整回答。",
                        "checks_json": {"requires_non_empty_answer": True, "requires_no_runtime_errors": True},
                        "labels": ["feedback_optimization", "workspace_config"],
                    }
                )
            return {
                "job_id": job_input["job_id"],
                "scope_kind": job_input["scope_kind"],
                "scope_id": job_input["scope_id"],
                "status": "completed",
                "eval_cases": eval_cases,
                "results": [],
            }
        if job_type == "attribution":
            return {
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
        if job_type == "regression_impact_analysis":
            eval_run = job_input["eval_run"]
            return {
                "eval_run_id": job_input["eval_run_id"],
                "status": "completed",
                "result_status": eval_run["result_status"],
                "gate_result": eval_run["gate_result"],
                "impacted_assets": [],
                "recommendations": ["保留当前优化结果并继续观察同类反馈。"],
                "summary": "回归用例通过，未发现新增影响。",
                "risk_assessment": "low",
                "next_steps": [],
            }
        raise AssertionError(f"unexpected job_type: {job_type}")

    async def fake_run_feedback_eval(
        *, eval_case_ids=None, optimization_task_id=None, source="optimization_batch_regression", regression_plan_id=None, **kwargs
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
                    "answer": "已读取当前配置并完整回答。",
                },
                status="passed",
                score=1.0,
                check_results=[{"name": "requires_non_empty_answer", "passed": True}],
            )
        return module.feedback_store.finish_eval_run(run["eval_run_id"])

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
        eval_generation_job = _run_one_agent_job(module)
        batch = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}").json()
        empty_regression_plan_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-plan")
        promote_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/promote",
            json={"operator": "tester", "reason": "E2E 批次回归用例晋级", "asset_layer": "batch_specific", "blocking_policy": "blocking"},
        )
        attribution_response = client.post(f"/api/feedback-optimization-batches/{batch['batch_id']}/attribution-jobs", json={"force": True})
        attribution_job = _run_one_agent_job(module)
        plan_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan",
            json={"regeneration_instruction": "保持指令简洁，避免修改无关文件。"},
        )
        assert plan_response.status_code == 200, plan_response.json()
        plan_job = _run_one_agent_job(module)
        batch = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}").json()
        plan = batch["optimization_plan"]
        plan_task = plan["tasks"][0]
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
        regression_plan = regression_plan_response.json()
        regression_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/regression-runs",
            json={"regression_plan_id": regression_plan["regression_plan_id"]},
        )
        assert regression_response.status_code == 200, regression_response.json()
        impact_job = _run_one_agent_job(module)
        impact_response = client.get(f"/api/eval-runs/{regression_response.json()['eval_run']['eval_run_id']}/impact-analysis")
        change_set = apply_response.json()["optimization_task"]["latest_change_set"]
        publish_response = client.post(
            f"/api/agent-change-sets/{change_set['change_set_id']}/publish",
            json={"operator": "tester"},
        )
        final_batch = regression_response.json()["batch"]

    assert signal_response.status_code == 200
    assert batch_response.status_code == 200
    assert empty_regression_plan_response.status_code == 400
    assert empty_regression_plan_response.json()["suggested_action"] == "promote_batch_eval_cases"
    assert empty_regression_plan_response.json()["regression_asset_eligibility"]["summary"]["promotable_linked"] == 1
    assert promote_response.status_code == 200
    assert promote_response.json()["promoted_eval_cases"][0]["status"] == "active"
    assert promote_response.json()["promoted_eval_cases"][0]["asset_layer"] == "batch_specific"
    assert promote_response.json()["promoted_eval_cases"][0]["promotion_status"] == "approved"
    assert attribution_response.status_code == 200
    assert plan_response.status_code == 200
    assert execute_response.status_code == 200
    assert apply_response.status_code == 200
    assert regression_plan_response.status_code == 200
    assert regression_response.status_code == 200
    assert impact_response.status_code == 200
    assert publish_response.status_code == 200, publish_response.json()
    assert calls == ["eval_case_generation", "attribution", "batch_plan", "execution", "regression_impact_analysis"]
    assert eval_generation_job.status == "completed"
    assert attribution_job.status == "completed"
    assert plan_job.status == "completed"
    assert execution_job.status == "completed"
    assert impact_job.status == "completed"
    assert plan["generated_by"] == "governor"
    assert plan["optimization_plan_job_id"]
    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v3"
    assert plan_task["task_context"]["target_file"] == "CLAUDE.md"
    assert execute_response.json()["execution_job"]["status"] == "queued"
    assert apply_response.json()["optimization_task"]["applied_agent_version_id"]
    assert "回答 workspace 配置类问题前必须读取当前配置文件" in Path(change_set["worktree_path"]).joinpath("CLAUDE.md").read_text(encoding="utf-8")
    assert "回答 workspace 配置类问题前必须读取当前配置文件" in workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8")
    assert publish_response.json()["commit_sha"] == change_set["candidate_commit_sha"]
    assert regression_response.json()["eval_run"]["result_status"] == "passed"
    assert regression_response.json()["regression_plan"]["eval_case_ids"]
    assert regression_response.json()["impact_analysis"]["status"] == "pending"
    assert impact_response.json()["status"] == "completed"
    assert final_batch["status"] == "completed"


def test_batch_eval_case_management_api(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        signal_response = client.post(
            "/api/feedback-signals",
            json={
                "session_id": "sess-batch-eval-cases",
                "labels": ["tool_data_incomplete"],
                "comment": "需要补充批次回归用例",
            },
        )
        batch_response = client.post(
            "/api/feedback-optimization-batches",
            json={
                "title": "批次用例管理",
                "source_refs": [{"source_kind": "signal", "source_id": signal_response.json()["signal_id"]}],
            },
        )
        batch = batch_response.json()
        create_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases",
            json={
                "prompt": "复测：回答前读取当前配置。",
                "expected_behavior": "必须说明已读取配置。",
                "checks_json": {"requires_non_empty_answer": True},
                "labels": ["manual"],
            },
        )
        eval_case = create_response.json()
        list_response = client.get(f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases")
        patch_response = client.patch(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/{eval_case['eval_case_id']}",
            json={"status": "archived"},
        )
        invalid_patch_response = client.patch(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/evc-not-linked",
            json={"status": "active"},
        )
        remove_response = client.delete(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/{eval_case['eval_case_id']}",
        )

    assert signal_response.status_code == 200
    assert batch_response.status_code == 200
    assert create_response.status_code == 200
    assert eval_case["eval_case_id"] in [item["eval_case_id"] for item in list_response.json()]
    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "archived"
    assert invalid_patch_response.status_code == 404
    assert remove_response.status_code == 200
    assert eval_case["eval_case_id"] not in remove_response.json()["eval_case_ids"]


def test_batch_eval_case_promotion_api_skips_non_promotable_cases(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        signal_response = client.post(
            "/api/feedback-signals",
            json={
                "session_id": "sess-batch-eval-promotion",
                "labels": ["tool_data_incomplete"],
                "comment": "需要批次回归用例晋级",
            },
        )
        batch_response = client.post(
            "/api/feedback-optimization-batches",
            json={
                "title": "批次用例晋级",
                "source_refs": [{"source_kind": "signal", "source_id": signal_response.json()["signal_id"]}],
            },
        )
        batch = batch_response.json()
        eligible_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases",
            json={
                "prompt": "复测：已批准用例。",
                "expected_behavior": "必须通过。",
                "checks_json": {"requires_non_empty_answer": True},
            },
        )
        draft_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases",
            json={
                "prompt": "复测：草稿用例。",
                "expected_behavior": "晋级后参与回归。",
                "checks_json": {"requires_non_empty_answer": True},
                "status": "draft",
            },
        )
        archived_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases",
            json={
                "prompt": "复测：归档用例。",
                "expected_behavior": "不应复活。",
                "checks_json": {"requires_non_empty_answer": True},
            },
        )
        archived_case_id = archived_response.json()["eval_case_id"]
        client.patch(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/{archived_case_id}",
            json={"status": "archived"},
        )
        promote_response = client.post(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/eval-cases/promote",
            json={"operator": "tester", "reason": "批次回归前晋级", "asset_layer": "batch_specific", "blocking_policy": "blocking"},
        )

    assert signal_response.status_code == 200
    assert batch_response.status_code == 200
    assert eligible_response.status_code == 200
    assert draft_response.status_code == 200
    assert archived_response.status_code == 200
    assert promote_response.status_code == 200, promote_response.json()
    promoted_ids = {item["eval_case_id"] for item in promote_response.json()["promoted_eval_cases"]}
    skipped = {item["eval_case_id"]: item["reasons"] for item in promote_response.json()["skipped_eval_cases"]}
    assert promoted_ids == {draft_response.json()["eval_case_id"]}
    assert skipped[eligible_response.json()["eval_case_id"]] == ["already_eligible"]
    assert "status:archived" in skipped[archived_case_id]
    governance_events = module.feedback_store.list_eval_case_governance_events(draft_response.json()["eval_case_id"])
    assert governance_events[0]["action"] == "promote"
