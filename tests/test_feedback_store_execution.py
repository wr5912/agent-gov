from pathlib import Path
import json

from feedback_store_test_utils import (
    ClaudeRuntime,
    FeedbackSignalCreateRequest,
    LocalSessionStore,
    _attribution_output,
    _create_approved_task_for_target,
    _create_batch_with_completed_attribution,
    _record_run,
    _store,
    asyncio,
    pytest,
    validate_execution_plan_output,
)
from pydantic import ValidationError
from sqlalchemy import select, text

from app.runtime.records.execution_records import ExecutionApplicationRecord
from app.runtime.feedback_schemas import coerce_execution_plan_output_model
from app.runtime.output_formatter import OutputFormatterResult
from app.runtime.runtime_db import AgentJobModel


def _execution_formatter_result(payload: dict) -> OutputFormatterResult:
    output, error = coerce_execution_plan_output_model(payload)
    assert output is not None, error
    return OutputFormatterResult(output=output)


def test_plan_task_target_policy_and_task_deduplicates(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(
        store,
        ".claude/skills/alert-triage/SKILL.md",
        target_type="skill",
        title="补强证据链要求",
        recommendation="增加 evidence_refs 输出要求。",
    )
    batch = store.list_optimization_batches(limit=1)[0]
    plan = batch["optimization_plan"]
    plan_task = plan["tasks"][0]

    assert task["optimization_task_id"].startswith("opt-")
    assert task["proposal_id"] is None
    assert task["source_batch_id"] == batch["batch_id"]
    assert task["source_plan_task_id"] == plan_task["plan_task_id"]
    assert task["target_paths"] == [".claude/skills/alert-triage/SKILL.md"]
    assert store.create_task_from_optimization_plan(
        batch=batch,
        plan=plan,
        plan_task={**plan_task, "plan_task_id": "fopt-denied", "target_path": "node_modules/pkg/index.js"},
    ) is None
    task_again = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])
    assert task_again["optimization_task"]["optimization_task_id"] == task["optimization_task_id"]
    tasks = [item for item in store.list_tasks() if item["source_plan_task_id"] == plan_task["plan_task_id"]]
    assert len(tasks) == 1


def test_execution_job_lifecycle_updates_task(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(
        store,
        "CLAUDE.md",
        target_type="main_agent_claude_md",
        title="追加配置读取要求",
        recommendation="在 CLAUDE.md 增加配置读取要求。",
    )

    job = store.create_execution_job(task["optimization_task_id"])
    assert job["input_path"].endswith("/execution/input.json")
    input_payload = json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))
    assert input_payload["execution_job_id"] == job["execution_job_id"]
    assert input_payload["target_paths"] == ["CLAUDE.md"]

    store.start_execution_job(job["execution_job_id"])
    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "追加一条配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "让 Agent 回答前读取配置。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "响应略变慢。",
            "human_review_required": True,
        },
    )
    updated_task = store.find_task(task["optimization_task_id"])

    assert completed["status"] == "completed"
    assert completed["validated_output_json"]["status"] == "ready"
    assert updated_task["status"] == "execution_ready"
    assert updated_task["latest_execution_job_id"] == job["execution_job_id"]
    assert updated_task["latest_execution_job"]["validated_output_json"]["operations"][0]["path"] == "CLAUDE.md"


@pytest.mark.parametrize(
    ("target_path", "operation", "expected_status", "expected_diff_line"),
    [
        (
            "CLAUDE.md",
            {"operation": "append_text", "append_text": "\n允许读取配置。\n"},
            "modified",
            "+允许读取配置。",
        ),
        (
            "CLAUDE.md",
            {"operation": "replace_file", "content": "# Replaced Agent\n"},
            "modified",
            "+# Replaced Agent",
        ),
        (
            "notes/new-policy.md",
            {"operation": "create_file", "content": "# New Policy\n"},
            "added",
            "+# New Policy",
        ),
    ],
)
def test_execution_plan_stores_planned_diff_without_writing_workspace(
    tmp_path,
    target_path,
    operation,
    expected_status,
    expected_diff_line,
):
    store, settings = _store(tmp_path)
    target = settings.main_workspace_dir / target_path
    if target_path != "CLAUDE.md":
        target.parent.mkdir(parents=True, exist_ok=True)
    before = target.read_text(encoding="utf-8") if target.exists() else None
    task = _create_approved_task_for_target(store, target_path)
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": f"计划修改 {target_path}。",
            "operations": [{**operation, "path": target_path, "rationale": "测试 planned diff。"}],
            "validation": "检查 planned diff。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    after = target.read_text(encoding="utf-8") if target.exists() else None
    planned_diff = completed["validated_output_json"]["planned_diff"]
    planned_file = planned_diff["files"][0]

    assert after == before
    assert planned_diff["files"]
    assert planned_diff[expected_status] == 1
    assert planned_file["path"] == target_path
    assert planned_file["operation"] == operation["operation"]
    assert planned_file["status"] == expected_status
    assert planned_file["after_sha256"]
    assert expected_diff_line in planned_file["unified_diff"]


def test_task_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    with store.Session.begin() as db:
        db.execute(text("UPDATE optimization_tasks SET status = 'unknown_status' WHERE optimization_task_id = :task_id"), {"task_id": task["optimization_task_id"]})

    with pytest.raises(ValidationError):
        store.find_task(task["optimization_task_id"])


def test_complete_execution_job_rolls_back_when_task_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    def fail_task_update(*args, **kwargs):
        raise RuntimeError("task update failed")

    monkeypatch.setattr(store, "_attach_execution_job_to_task_row", fail_task_update)

    with pytest.raises(RuntimeError, match="task update failed"):
        store.complete_execution_job(
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
                        "append_text": "\n配置读取要求。\n",
                        "rationale": "测试回滚。",
                    }
                ],
                "validation": "复测反馈场景。",
                "risk": "测试风险。",
                "human_review_required": True,
            },
        )

    unchanged_job = store.get_execution_job(job["execution_job_id"])
    unchanged_task = store.find_task(task["optimization_task_id"])
    assert unchanged_job["status"] == "running"
    assert unchanged_job["raw_output_json"] is None
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_task["status"] == "execution_planning"


def test_fail_execution_job_rolls_back_when_task_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    def fail_task_update(*args, **kwargs):
        raise RuntimeError("task update failed")

    monkeypatch.setattr(store, "_attach_execution_job_to_task_row", fail_task_update)

    with pytest.raises(RuntimeError, match="task update failed"):
        store.fail_execution_job(job["execution_job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")

    unchanged_job = store.get_execution_job(job["execution_job_id"])
    unchanged_task = store.find_task(task["optimization_task_id"])
    assert unchanged_job["status"] == "running"
    assert unchanged_job["error_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_task["status"] == "execution_planning"


def test_execution_projection_failure_preserves_formatter_raw_output(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    raw_output = {
        "_formatter": {"name": "dspy", "status": "failed", "error_type": "ValidationError"},
        "raw_text": "reasoning only",
    }

    failed = store.fail_projected_agent_job(
        job,
        error_code="AGENT_RUNTIME_ERROR",
        message="formatter failed",
        raw_output_json=raw_output,
    )
    updated_task = store.find_task(task["optimization_task_id"])

    assert failed["status"] == "failed"
    assert failed["raw_output_json"] == raw_output
    assert failed["error_json"]["error_code"] == "AGENT_RUNTIME_ERROR"
    assert updated_task["status"] == "execution_failed"


def test_complete_execution_job_rolls_back_when_batch_plan_task_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="执行任务")
    task = prepared["optimization_task"]
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    def fail_batch_plan_task_update(*args, **kwargs):
        raise RuntimeError("batch plan task update failed")

    monkeypatch.setattr(store, "_update_batch_plan_task_row", fail_batch_plan_task_update)

    with pytest.raises(RuntimeError, match="batch plan task update failed"):
        store.complete_execution_job(
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
                        "append_text": "\n配置读取要求。\n",
                        "rationale": "测试批次任务回滚。",
                    }
                ],
                "validation": "复测反馈场景。",
                "risk": "测试风险。",
                "human_review_required": True,
            },
        )

    unchanged_job = store.get_execution_job(job["execution_job_id"])
    unchanged_task = store.find_task(task["optimization_task_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    unchanged_plan_task = next(item for item in unchanged_batch["optimization_plan"]["tasks"] if item["plan_task_id"] == plan_task["plan_task_id"])
    assert unchanged_job["status"] == "running"
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_task["status"] == "execution_planning"
    assert unchanged_plan_task["status"] == "execution_planning"
    assert unchanged_plan_task.get("latest_execution_job") is None


def test_execution_application_rolls_back_when_task_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    ready_job = store.complete_execution_job(
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
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "测试应用回滚。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    def fail_task_applied(*args, **kwargs):
        raise RuntimeError("task applied update failed")

    monkeypatch.setattr(store, "_mark_task_applied_row", fail_task_applied)

    with pytest.raises(RuntimeError, match="task applied update failed"):
        store.record_execution_application_applied(
            ready_job["execution_job_id"],
            pre_execution_version={"agent_version_id": "main-v-before"},
            applied_agent_version={"agent_version_id": "main-v-after"},
            applied_diff={"changed_files": []},
        )

    unchanged_job = store.get_execution_job(job["execution_job_id"])
    unchanged_task = store.find_task(task["optimization_task_id"])
    assert unchanged_job["status"] == "completed"
    assert unchanged_job.get("applied_agent_version_id") is None
    assert unchanged_task["status"] == "execution_ready"
    assert unchanged_task.get("applied_agent_version_id") is None
    assert store.latest_execution_application(job["execution_job_id"]) is None


def test_execution_application_syncs_batch_plan_task(tmp_path):
    store, _ = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="执行任务")
    task = prepared["optimization_task"]
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    ready_job = store.complete_execution_job(
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
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "测试批次应用同步。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    applied = store.record_execution_application_applied(
        ready_job["execution_job_id"],
        pre_execution_version={"agent_version_id": "main-v-before"},
        applied_agent_version={"agent_version_id": "main-v-after"},
        applied_diff={"changed_files": ["CLAUDE.md"]},
    )

    updated_task = store.find_task(task["optimization_task_id"])
    updated_batch = store.find_optimization_batch(batch["batch_id"])
    updated_plan_task = next(item for item in updated_batch["optimization_plan"]["tasks"] if item["plan_task_id"] == plan_task["plan_task_id"])
    assert applied["status"] == "applied"
    assert ExecutionApplicationRecord.model_validate(applied).status == "applied"
    assert applied["applied_agent_version_id"] == "main-v-after"
    assert updated_task["status"] == "applied_pending_regression"
    assert updated_batch["status"] == "applied_pending_regression"
    assert updated_plan_task["status"] == "applied_pending_regression"
    assert updated_plan_task["applied_agent_version_id"] == "main-v-after"
    assert updated_plan_task["latest_execution_job"]["status"] == "completed"


def test_execution_application_rolls_back_when_batch_plan_task_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="执行任务")
    task = prepared["optimization_task"]
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    ready_job = store.complete_execution_job(
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
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "测试批次应用回滚。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    def fail_batch_plan_task_update(*args, **kwargs):
        raise RuntimeError("batch plan task applied update failed")

    monkeypatch.setattr(store, "_update_batch_plan_task_row", fail_batch_plan_task_update)

    with pytest.raises(RuntimeError, match="batch plan task applied update failed"):
        store.record_execution_application_applied(
            ready_job["execution_job_id"],
            pre_execution_version={"agent_version_id": "main-v-before"},
            applied_agent_version={"agent_version_id": "main-v-after"},
            applied_diff={"changed_files": ["CLAUDE.md"]},
        )

    unchanged_job = store.get_execution_job(job["execution_job_id"])
    unchanged_task = store.find_task(task["optimization_task_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    unchanged_plan_task = next(item for item in unchanged_batch["optimization_plan"]["tasks"] if item["plan_task_id"] == plan_task["plan_task_id"])
    assert unchanged_job["status"] == "completed"
    assert unchanged_job.get("applied_agent_version_id") is None
    assert unchanged_task["status"] == "execution_ready"
    assert unchanged_task.get("applied_agent_version_id") is None
    assert unchanged_plan_task["status"] == "execution_ready"
    assert unchanged_plan_task.get("applied_agent_version_id") is None
    assert store.latest_execution_application(job["execution_job_id"]) is None


def test_execution_job_accepts_any_managed_workspace_file(tmp_path):
    store, settings = _store(tmp_path)
    target = settings.main_workspace_dir / "mcp_servers" / "security_kb_mcp" / "kb.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("rules:\n  - old\n", encoding="utf-8")
    task = _create_approved_task_for_target(store, "mcp_servers/security_kb_mcp/kb.yaml")

    job = store.create_execution_job(task["optimization_task_id"])
    input_payload = json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))
    context = input_payload["target_file_contexts"][0]

    assert input_payload["allowed_target_paths"] == ["mcp_servers/security_kb_mcp/kb.yaml"]
    assert input_payload["target_policy"]["type"] == "main_workspace_managed_full_with_excludes"
    assert context["path"] == "mcp_servers/security_kb_mcp/kb.yaml"
    assert context["managed"] is True
    assert context["exists"] is True
    assert context["type"] == "file"
    assert context["content_text"] == "rules:\n  - old\n"
    assert context["sha256"]


def test_execution_targets_reject_workspace_excluded_paths(tmp_path):
    store, _ = _store(tmp_path)

    assert store.target_allowed("README.md") is True
    assert store.target_allowed("mcp_servers/security_kb_mcp/kb.yaml") is True
    assert store.target_allowed("node_modules/pkg/index.js") is False
    assert store.target_allowed(".git/config") is False
    assert store.target_allowed("dist/bundle.js") is False
    assert store.target_allowed(".venv/bin/python") is False
    assert store.target_allowed("../escape") is False
    assert store.target_allowed("/main-workspace/CLAUDE.md") is False


def test_execution_plan_binds_expected_sha_from_target_context(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    context = job["input_json"]["target_file_contexts"][0]
    store.start_execution_job(job["execution_job_id"])

    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "替换 CLAUDE.md。",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": "CLAUDE.md",
                    "content": "# Updated\n",
                    "rationale": "测试绑定 hash。",
                }
            ],
            "validation": "检查文件内容。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    operation = completed["validated_output_json"]["operations"][0]
    assert operation["expected_sha256"] == context["sha256"]


def test_create_execution_job_cleans_record_and_tmp_when_task_attach_fails(tmp_path, monkeypatch):
    store, settings = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    task_id = task["optimization_task_id"]

    def fail_task_attach(*args, **kwargs):
        raise RuntimeError("task attach failed")

    monkeypatch.setattr(store, "_attach_execution_job_to_task", fail_task_attach)

    with pytest.raises(RuntimeError, match="task attach failed"):
        store.create_execution_job(task_id, force=True)

    with store.Session() as db:
        assert db.scalars(select(AgentJobModel).where(AgentJobModel.job_type == "execution")).all() == []
    assert [path for path in (settings.data_dir / ".runtime-tmp" / "jobs").iterdir() if path.name.startswith("fbe-")] == []
    assert store.find_task(task_id).get("latest_execution_job_id") is None


def test_execution_output_fills_system_fields_from_job_context(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, ".mcp.json")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "execution_job_id": "fbe-agent-wrong",
            "optimization_task_id": "fot-agent-wrong",
            "baseline_agent_version_id": "agent-version-wrong",
            "status": "needs_human_review",
            "summary": "目标文件与提案意图不匹配，需要人工确认。",
            "operations": [],
            "no_action_reason": "提案要求调整 Agent 行为，但 .mcp.json 仅用于 MCP 连接配置。",
            "validation": None,
            "risk": None,
        },
    )

    assert completed["status"] == "needs_human_review"
    assert completed["validated_output_json"]["execution_job_id"] == job["execution_job_id"]
    assert completed["validated_output_json"]["optimization_task_id"] == task["optimization_task_id"]
    assert completed["validated_output_json"]["baseline_agent_version_id"] == task["baseline_agent_version_id"]
    assert completed["error_json"] is None


def test_execution_plan_rejects_non_text_or_skipped_target(tmp_path):
    store, settings = _store(tmp_path)
    target = settings.main_workspace_dir / "assets" / "logo.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01binary")
    task = _create_approved_task_for_target(store, "assets/logo.bin")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    failed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "替换二进制文件。",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": "assets/logo.bin",
                    "content": "not-binary",
                    "rationale": "二进制目标不应自动改。",
                }
            ],
            "validation": "不应通过。",
            "risk": "不应通过。",
            "human_review_required": True,
        },
    )

    assert failed["status"] == "failed"
    assert failed["error_json"]["error_code"] == "EXECUTION_PLAN_UNSAFE"
    assert "not safely editable" in failed["error_json"]["message"]


def test_execution_optimizer_uses_materialized_input_path(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    store, settings = _store(tmp_path)
    task = _create_approved_task_for_target(
        store,
        "CLAUDE.md",
        target_type="main_agent_claude_md",
        title="补充配置读取要求",
        recommendation="在 CLAUDE.md 增加配置读取要求。",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        prompt_text = prompt_items[0]["message"]["content"]
        input_path = prompt_text.split("输入文件：", 1)[1].splitlines()[0]
        input_payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        output = {
            "optimization_task_id": input_payload["optimization_task_id"],
            "execution_job_id": input_payload["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": input_payload["baseline_agent_version_id"],
            "summary": "追加配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n回答配置类问题前必须读取当前 workspace 配置。\n",
                    "rationale": "根据已确认任务补充主智能体配置读取要求。",
                }
            ],
            "validation": "运行评估套件。",
            "risk": "用例格式需人工确认。",
            "human_review_required": True,
        }
        seen["formatted_payload"] = output
        text = json.dumps(output, ensure_ascii=False)
        seen["prompt_text"] = prompt_text
        seen["input_path"] = input_path
        seen["allowed_tools"] = options.allowed_tools
        seen["disallowed_tools"] = options.disallowed_tools
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-execution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-execution-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    class FakeFormatter:
        def format(self, *, job_type, raw_text, job_input):
            seen["formatter_job_type"] = job_type
            seen["formatter_raw_text"] = raw_text
            return _execution_formatter_result(seen["formatted_payload"])

    from app.runtime.output_formatter import OutputFormatterResult

    runtime.output_formatter = FakeFormatter()
    job = asyncio.run(runtime.run_execution_job(task["optimization_task_id"]))
    job_payload = job.model_dump(mode="json")
    updated_task = store.find_task(task["optimization_task_id"])

    assert seen["formatter_job_type"] == "execution"
    assert "追加配置读取要求" in str(seen["formatter_raw_text"])
    assert job.status == "completed"
    assert job.validated_output_json is not None
    assert job.validated_output_json["status"] == "ready"
    assert seen["input_path"] == job.input_path
    assert str(seen["input_path"]).endswith("/execution/input.json")
    assert "execution-input.json" not in str(seen["input_path"])
    assert seen["allowed_tools"] == []
    assert set(seen["disallowed_tools"]) >= {"Read", "Grep", "Glob", "Bash", "Edit", "Write"}
    assert updated_task["latest_execution_job_id"] == job_payload["execution_job_id"]
    assert updated_task["latest_execution_job"]["validated_output_json"]["operations"][0]["path"] == "CLAUDE.md"


def test_execution_optimizer_uses_deterministic_eval_plan_without_agent(tmp_path, monkeypatch):
    import claude_agent_sdk

    store, settings = _store(tmp_path)
    task = _create_approved_task_for_target(
        store,
        "evals/alert-triage-false-positive.json",
        target_type="eval_case",
        title="增加告警误报评估用例",
        recommendation="创建告警误报评估用例。",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    async def fail_query(*, prompt, options, transport=None):
        raise AssertionError("eval execution plans should be generated deterministically")
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", fail_query)

    job = asyncio.run(runtime.run_execution_job(task["optimization_task_id"]))
    assert job.validated_output_json is not None
    operation = job.validated_output_json["operations"][0]

    assert job.status == "completed"
    assert job.validated_output_json["status"] == "ready"
    assert operation["operation"] == "create_file"
    assert operation["path"] == "evals/alert-triage-false-positive.json"
    assert "feedback-eval-case/v1" in operation["content"]
    assert "创建告警误报评估用例" in operation["content"]
    assert "手动运行回归验证" in job.validated_output_json["validation"]


def test_execution_plan_output_normalizes_agent_friendly_fields():
    validated, error = validate_execution_plan_output(
        {
            "optimization_task_id": "opt-1",
            "execution_job_id": "fbe-1",
            "status": "safe_to_apply",
            "summary": "创建评估用例。",
            "operations": [
                {
                    "operation": "create_file",
                    "path": "evals/example.json",
                    "content": "{}",
                    "rationale": {"reason": "根据建议创建文件"},
                }
            ],
            "validation": {"steps": ["检查 JSON 语法"], "expected_result": "评估用例可加载"},
            "risk": {"level": "low", "reason": "仅新增评估文件"},
            "human_review_required": True,
        }
    )

    assert error is None
    assert validated["status"] == "ready"
    assert "检查 JSON 语法" in validated["validation"]
    assert "仅新增评估文件" in validated["risk"]
    assert "根据建议创建文件" in validated["operations"][0]["rationale"]
