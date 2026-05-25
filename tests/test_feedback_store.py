import asyncio
import json
from pathlib import Path

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import FeedbackAnalysisJobResponse, FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


def _settings(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "main-workspace"
    attribution_workspace = tmp_path / "docker" / "volume" / "attribution-workspace"
    proposal_workspace = tmp_path / "docker" / "volume" / "proposal-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-roots" / "main"
    attribution_root = tmp_path / "docker" / "volume" / "claude-roots" / "attribution"
    proposal_root = tmp_path / "docker" / "volume" / "claude-roots" / "proposal"
    for path in (workspace, attribution_workspace, proposal_workspace, claude_root / ".claude", attribution_root / ".claude", proposal_root / ".claude"):
        path.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        ATTRIBUTION_WORKSPACE_DIR=attribution_workspace,
        PROPOSAL_WORKSPACE_DIR=proposal_workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        ATTRIBUTION_CLAUDE_ROOT=attribution_root,
        PROPOSAL_CLAUDE_ROOT=proposal_root,
        CLAUDE_HOME=claude_root / ".claude",
        ENABLE_POLICY_HOOKS=True,
    )


def _store(tmp_path):
    settings = _settings(tmp_path)
    return FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test"), settings


def _record_run(store: FeedbackStore):
    return store.record_run(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "alert_id": "alert-1",
            "case_id": "case-1",
            "message": "研判告警",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "告警研判摘要"}]}],
            "langfuse_trace_id": "trace-1",
            "langfuse_trace_url": "http://langfuse.local/project/traces/trace-1",
            "answer_summary": "告警研判摘要",
            "agent_activity": {
                "tool_names": ["mcp__sec-ops-data__asset"],
                "tool_calls": [{"name": "mcp__sec-ops-data__asset", "input": {"token": "secret-token"}}],
            },
            "created_at": "2026-05-20T00:00:00+00:00",
            "completed_at": "2026-05-20T00:00:01+00:00",
        }
    )


def test_feedback_signal_only_writes_signal_pool(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)

    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            source_type="explicit_feedback",
            labels=["evidence_gap"],
            comment="证据不足",
        )
    )

    assert signal["signal_id"].startswith("fbs-")
    assert signal["matched_run_id"] == "run-1"
    assert signal["session_id"] == "session-1"
    assert store.list_proposals() == []
    assert store.get_job("missing-job") is None


def test_implicit_signal_defaults_to_review(tmp_path):
    store, _ = _store(tmp_path)

    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            source_type="implicit_feedback",
            session_id="session-2",
            labels=["timeout"],
        )
    )

    assert signal["auto_captured"] is True
    assert signal["requires_review"] is True


def test_soc_event_idempotency_and_pending_correlation(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    matched = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-1",
            source_system="sec-ops-ui",
            event_type="case.verdict_changed",
            timestamp="2026-05-20T00:02:00+00:00",
            run_id="run-1",
            case_id="case-1",
        )
    )
    duplicate = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-1",
            source_system="sec-ops-ui",
            event_type="case.verdict_changed",
            timestamp="2026-05-20T00:02:00+00:00",
            run_id="run-1",
        )
    )
    pending = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-2",
            source_system="sec-ops-ui",
            event_type="evidence.added",
            timestamp="2026-05-20T00:03:00+00:00",
            case_id="missing-case",
        )
    )

    assert matched["correlation_status"] == "matched"
    assert duplicate["correlation_status"] == "duplicate"
    assert pending["correlation_status"] == "pending_correlation"
    assert pending["pending_correlation"]["pending_id"].startswith("pc-")


def test_case_evidence_and_job_outputs(tmp_path):
    store, _ = _store(tmp_path)
    store.set_langfuse_trace_fetcher(lambda trace_id: {"id": trace_id, "input": {"raw": True}, "observations": [{"name": "tool"}]})
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"], comment="证据不足")
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], priority="high")
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    duplicate_evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    assert feedback_case["status"] == "pending_evidence"
    assert evidence["schema_version"] == "evidence-package/v1"
    assert duplicate_evidence["evidence_package_id"] == evidence["evidence_package_id"]
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "feedback.json")["file_name"] == "feedback.json"
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "../feedback.json") is None
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "manifest.json") is None
    assert evidence["completeness"]["has_feedback"] is True
    assert {item["path"] for item in evidence["included_files"]} >= {
        "feedback.json",
        "runs.json",
        "sessions.json",
        "tool_calls.json",
        "soc_events.json",
        "trace_summary.json",
        "main_agent_version.json",
        "redaction_report.json",
        "messages.json",
        "agent_activity.json",
        "langfuse_trace_refs.json",
    }
    assert evidence["source_refs"]["trace_ids"] == ["trace-1"]
    assert evidence["completeness"]["has_messages"] is True
    assert evidence["completeness"]["has_langfuse_trace_refs"] is True
    assert evidence["completeness"]["has_langfuse_trace_details"] is False
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "messages.json")["content"][0]["messages"]
    trace_refs = store.get_evidence_package_file(evidence["evidence_package_id"], "langfuse_trace_refs.json")["content"]
    assert trace_refs[0]["trace_url"] == "http://langfuse.local/project/traces/trace-1"
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "langfuse_traces.json") is None

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.start_job(attribution_job["job_id"])
    completed = store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert completed["status"] == "completed"
    assert store.create_attribution_job(feedback_case["feedback_case_id"])["job_id"] == attribution_job["job_id"]
    assert output["schema_version"] == "attribution-output/v1"
    assert output["actionability"] == "needs_human_analysis"

    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.start_job(proposal_job["job_id"])
    completed_proposal = store.complete_proposal_job(proposal_job["job_id"], store.offline_proposal_output(proposal_job))
    proposal_output = store.get_job_output(proposal_job["job_id"], "proposal")

    assert completed_proposal["status"] == "completed"
    assert store.create_proposal_job(feedback_case["feedback_case_id"])["job_id"] == proposal_job["job_id"]
    assert proposal_output["external_guidance"]
    assert store.list_proposals() == []


def test_list_cases_returns_latest_case_versions_only(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    assert len(store.list_cases()) == 1

    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    cases_after_evidence = store.list_cases()

    assert len(cases_after_evidence) == 1
    assert cases_after_evidence[0]["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert cases_after_evidence[0]["status"] == "pending_attribution"
    assert cases_after_evidence[0]["evidence_package_ids"] == [evidence["evidence_package_id"]]
    assert store.list_cases(status="pending_evidence") == []

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    cases_after_attribution = store.list_cases()

    assert len(cases_after_attribution) == 1
    assert cases_after_attribution[0]["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert cases_after_attribution[0]["status"] == "attribution_queued"
    assert cases_after_attribution[0]["evidence_package_ids"] == [evidence["evidence_package_id"]]
    assert cases_after_attribution[0]["attribution_job_ids"] == [attribution_job["job_id"]]


def test_debug_evidence_can_be_disabled(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(
        data_dir=settings.data_dir,
        agent_version_provider=lambda: "main-v-test",
        enable_debug_evidence=False,
    )
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    included = {item["path"] for item in evidence["included_files"]}
    tool_calls = store.get_evidence_package_file(evidence["evidence_package_id"], "tool_calls.json")["content"]

    assert "messages.json" not in included
    assert "langfuse_traces.json" not in included
    assert tool_calls[0]["input"]["token"] == "[REDACTED]"


def test_failed_feedback_jobs_can_retry_without_duplicating_active_jobs(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    assert store.create_attribution_job(feedback_case["feedback_case_id"])["job_id"] == attribution_job["job_id"]
    failed_attribution = store.fail_job(attribution_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_case = store.find_case(feedback_case["feedback_case_id"])
    retried_attribution = store.create_attribution_job(feedback_case["feedback_case_id"])

    assert failed_attribution["error_json"]["message"] == "failed"
    assert FeedbackAnalysisJobResponse(**failed_attribution).error_json["error_code"] == "AGENT_RUNTIME_ERROR"
    assert failed_case["status"] == "pending_attribution"
    assert retried_attribution["job_id"] != attribution_job["job_id"]
    store.complete_attribution_job(retried_attribution["job_id"], store.offline_attribution_output(retried_attribution))

    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    assert store.create_proposal_job(feedback_case["feedback_case_id"])["job_id"] == proposal_job["job_id"]
    failed_proposal = store.fail_job(proposal_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_proposal_case = store.find_case(feedback_case["feedback_case_id"])
    retried_proposal = store.create_proposal_job(feedback_case["feedback_case_id"])

    assert failed_proposal["error_json"]["message"] == "failed"
    assert failed_proposal_case["status"] == "pending_proposal"
    assert retried_proposal["job_id"] != proposal_job["job_id"]


def test_schema_review_jobs_are_retryable(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])

    reviewed_attribution = store.complete_attribution_job(attribution_job["job_id"], {"schema_version": "attribution-output/v1"})
    attribution_case = store.find_case(feedback_case["feedback_case_id"])
    retried_attribution = store.create_attribution_job(feedback_case["feedback_case_id"])

    assert reviewed_attribution["status"] == "needs_human_review"
    assert reviewed_attribution["error_json"]["message"] == "分析 Agent 输出不符合 schema。"
    assert reviewed_attribution["error_json"]["validation_errors"]
    assert attribution_case["status"] == "pending_attribution"
    assert retried_attribution["job_id"] != attribution_job["job_id"]

    store.complete_attribution_job(retried_attribution["job_id"], store.offline_attribution_output(retried_attribution))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    reviewed_proposal = store.complete_proposal_job(proposal_job["job_id"], {"schema_version": "proposal-output/v1"})
    proposal_case = store.find_case(feedback_case["feedback_case_id"])
    retried_proposal = store.create_proposal_job(feedback_case["feedback_case_id"])

    assert reviewed_proposal["status"] == "needs_human_review"
    assert proposal_case["status"] == "pending_proposal"
    assert retried_proposal["job_id"] != proposal_job["job_id"]


def test_legacy_schema_error_message_is_normalized_on_read(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    validation_errors = [{"type": "literal_error", "loc": ["problem_type"], "msg": "invalid enum"}]

    store._set_job_json(  # noqa: SLF001 - regression coverage for legacy persisted job payloads.
        attribution_job["job_id"],
        error_json={
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": json.dumps(validation_errors),
            "job_id": attribution_job["job_id"],
        },
    )
    job = store.get_job(attribution_job["job_id"])

    assert job["error_json"]["message"] == "分析 Agent 输出不符合 schema。"
    assert job["error_json"]["validation_errors"] == validation_errors


def test_proposal_target_allowlist_and_task_requires_approval(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["skill_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "skill_gap",
            "optimization_object_type": "skill",
            "actionability": "direct_workspace_change",
            "confidence": "medium",
            "human_review_required": True,
            "evidence_refs": [{"type": "run", "id": "run-1", "reason": "缺少证据链"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "skill 说明不足"},
            "rationale": "需要补强技能",
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
                    "title": "补强证据链要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "skill",
                    "target_path": ".claude/skills/alert-triage/SKILL.md",
                    "recommendation": "增加 evidence_refs 输出要求。",
                    "expected_effect": "提高可核查性。",
                    "validation": "新增回归样例。",
                    "risk": "回答略变长。",
                    "requires_approval": True,
                },
                {
                    "title": "非法目标",
                    "actionability": "direct_workspace_change",
                    "target_type": "secret",
                    "target_path": ".env",
                    "recommendation": "不应进入 task。",
                    "expected_effect": "无",
                    "validation": "无",
                    "risk": "高",
                    "requires_approval": True,
                },
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )

    proposals = store.list_proposals()
    assert len(proposals) == 1
    assert proposals[0]["target_path"] == ".claude/skills/alert-triage/SKILL.md"
    assert store.create_task(proposal_id=proposals[0]["proposal_id"]) is None

    store.review_proposal(proposals[0]["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposals[0]["proposal_id"], comment="执行")
    assert task["optimization_task_id"].startswith("opt-")
    assert task["target_paths"] == [".claude/skills/alert-triage/SKILL.md"]
    task_again = store.create_task(proposal_id=proposals[0]["proposal_id"], comment="重复点击")
    assert task_again["optimization_task_id"] == task["optimization_task_id"]
    tasks = [item for item in store.list_tasks() if item["proposal_id"] == proposals[0]["proposal_id"]]
    assert len(tasks) == 1


def test_proposal_output_normalizes_minimal_agent_proposal(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])

    completed = store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "rationale": "Agent 未验证 workspace 能力清单。",
                    "recommendation": "Add a Workspace Discovery section to CLAUDE.md.",
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])

    assert completed["status"] == "completed"
    assert output["proposals"][0]["proposal_id"] == "prop-001"
    assert output["proposals"][0]["target_type"] == "main_agent_claude_md"
    assert output["proposals"][0]["title"] == "Add a Workspace Discovery section to CLAUDE.md."
    assert output["proposals"][0]["expected_effect"]
    assert proposals[0]["target_path"] == "CLAUDE.md"


def test_runtime_feedback_jobs_use_offline_outputs_without_provider(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    reused_attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))
    reused_proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))

    assert attribution_job["profile_name"] == "feedback-attribution"
    assert attribution_job["status"] == "completed"
    assert reused_attribution_job["job_id"] == attribution_job["job_id"]
    assert reused_attribution_job["status"] == "completed"
    assert attribution_job["profile_version"]["profile_name"] == "feedback-attribution"
    assert proposal_job["profile_name"] == "feedback-proposal"
    assert proposal_job["status"] == "completed"
    assert reused_proposal_job["job_id"] == proposal_job["job_id"]
    assert reused_proposal_job["status"] == "completed"


def test_data_incomplete_bbb_case_calls_attribution_agent_and_generates_output(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    run_id = "a0fb5319-1752-45eb-972f-0e7edee30e92"
    session_id = "sess_74a6b45e-4883-45cd-9fae-0c5323ddbcd2"
    store.record_run(
        {
            "run_id": run_id,
            "agent_version_id": "agent-version-20260522T104329Z-628569dc",
            "session_id": session_id,
            "sdk_session_id": "38b2b5ae-5c40-42a7-9dcb-4ded2192f323",
            "message": "请说明当前 workspace 中有哪些 subagents 和 skills。",
            "answer_summary": "当前 workspace 中可用的 subagents 和 skills 如下。",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "当前 workspace 中可用的 subagents 和 skills 如下。"}]}],
            "agent_activity": {
                "requested_skills": [],
                "skills_mode": "default",
                "allowed_tools": ["Read", "Grep", "Glob", "mcp__sec-ops-data__*"],
                "disallowed_tools": ["Bash", "WebFetch", "WebSearch"],
                "tool_names": [],
                "tool_calls": [],
                "tool_results": [],
                "skill_calls": [],
            },
            "langfuse_trace_id": "97eb6e0f1dd8b91a6956f4572f90b7f8",
            "langfuse_trace_url": "http://langfuse.local/project/traces/97eb6e0f1dd8b91a6956f4572f90b7f8",
            "created_at": "2026-05-22T15:44:50+00:00",
            "completed_at": "2026-05-22T15:44:59+00:00",
            "errors": [],
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            session_id=session_id,
            labels=["tool_data_incomplete"],
            comment="数据不全BBB",
            metadata={"analyst_action": "partially_accepted", "affected_tools": []},
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
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
            "schema_version": "attribution-output/v1",
            "feedback_case_id": input_payload["feedback_case_id"],
            "attribution_job_id": input_payload["job_id"],
            "status": "needs_human_review",
            "problem_type": "tool_usage_deficiency",
            "optimization_object_type": "agent_behavior",
            "actionability": "low",
            "confidence": "low",
            "human_review_required": True,
            "evidence_refs": input_payload["allowed_evidence_paths"],
            "responsibility_boundary": "agent",
            "rationale": "该 run 有 messages 和 trace summary，但 tool_calls.json 为空；归因为工具证据链不足。",
            "recommended_next_step": "Human reviewer should examine whether the agent should have used tools before answering capability queries.",
        }
        seen["prompt_text"] = prompt_text
        seen["input_path"] = input_path
        seen["cwd"] = options.cwd
        seen["max_turns"] = options.max_turns
        text = json.dumps(output, ensure_ascii=False)
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-attribution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="sdk-attribution-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert evidence["completeness"]["has_runs"] is True
    assert evidence["completeness"]["has_tool_calls"] is False
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "tool_calls.json")["content"] == []
    assert "归因分析 Agent" in str(seen["prompt_text"])
    assert seen["cwd"] == settings.attribution_workspace_dir
    assert seen["max_turns"] == settings.max_turns
    assert attribution_job["status"] == "completed"
    assert output["schema_version"] == "attribution-output/v1"
    assert output["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert output["problem_type"] == "tool_data_quality"
    assert output["optimization_object_type"] == "main_agent_claude_md"
    assert output["actionability"] == "needs_human_analysis"
    assert output["evidence_refs"][0]["type"] == "evidence_file"
    assert output["responsibility_boundary"]["owner"] == "agent"
    assert store.find_case(feedback_case["feedback_case_id"])["status"] == "pending_proposal"


def test_proposal_agent_ignores_intermediate_permissions_json(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全BBB"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_misuse",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "agent_activity.json", "reason": "未调用工具"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要补充行为准则"},
            "rationale": "Agent 未验证 workspace 能力清单。",
            "recommended_next_step": "generate_proposal",
        },
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        prompt_text = prompt_items[0]["message"]["content"]
        input_payload = json.loads(prompt_text.split("proposal_input_json:\n", 1)[1].split("\n\nattribution_output_json:", 1)[0])
        output = {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": input_payload["feedback_case_id"],
            "proposal_job_id": input_payload["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [],
            "no_action_reason": "当前归因需要先由人确认具体缺失项。",
        }
        text = '{"permissions":{"allow":["Bash(npm *)"]}}\n' + json.dumps(output, ensure_ascii=False)
        seen["prompt_text"] = prompt_text
        seen["allowed_tools"] = options.allowed_tools
        seen["disallowed_tools"] = options.disallowed_tools
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-proposal-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="sdk-proposal-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(proposal_job["job_id"], "proposal")

    assert "proposal_input_json" in str(seen["prompt_text"])
    assert "attribution_output_json" in str(seen["prompt_text"])
    assert seen["allowed_tools"] == []
    assert set(seen["disallowed_tools"]) >= {"Read", "Grep", "Glob"}
    assert proposal_job["status"] == "completed"
    assert output["schema_version"] == "proposal-output/v1"
    assert proposal_job["raw_output_json"]["schema_version"] == "proposal-output/v1"


def test_sqlite_store_does_not_create_legacy_runtime_dirs(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(job["job_id"], store.offline_attribution_output(job))

    assert settings.runtime_db_path.exists()
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "feedback.json")["content"]
    assert not (settings.data_dir / "feedback-cases").exists()
    assert not (settings.data_dir / "feedback-analysis").exists()
    assert not (settings.data_dir / "evidence-packages").exists()
    assert not (settings.data_dir / ".runtime-tmp" / "jobs" / job["job_id"]).exists()
