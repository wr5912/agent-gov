from feedback_store_test_utils import *
from app.runtime.errors import BusinessRuleViolation


def test_data_incomplete_bbb_feedback_eval_calls_main_agent_and_records_result(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-after")
    run_id = "a0fb5319-1752-45eb-972f-0e7edee30e92"
    store.record_run(
        {
            "run_id": run_id,
            "agent_version_id": "main-v-before",
            "session_id": "sess-bbb",
            "message": "请说明当前 workspace 中有哪些 subagents 和 skills。",
            "answer_summary": "当前 workspace 中可用的 subagents 和 skills 如下。",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "当前 workspace 中可用的 subagents 和 skills 如下。"}]}],
            "agent_activity": {"tool_names": [], "tool_calls": [], "tool_results": [], "skill_calls": []},
            "created_at": "2026-05-22T15:44:50+00:00",
            "completed_at": "2026-05-22T15:44:59+00:00",
            "errors": [],
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            session_id="sess-bbb",
            labels=["tool_data_incomplete"],
            comment="数据不全BBB",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "原回答没有工具调用"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要要求读取配置文件"},
            "rationale": "Agent 回答 workspace 能力清单时没有读取配置。",
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
                    "proposal_id": "prop-bbb",
                    "title": "要求回答 workspace 能力清单前读取配置",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "在 CLAUDE.md 增加 Read/Grep/Glob 核查配置的要求。",
                    "expected_effect": "回答更完整。",
                    "validation": "复测数据不全BBB 原始输入，并确认产生工具调用。",
                    "risk": "响应耗时增加。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])
    task = store.mark_task_applied(task["optimization_task_id"], agent_version={"agent_version_id": "main-v-after"})
    sync = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])
    eval_case = sync["eval_cases"][0]
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        seen["prompt"] = prompt_items[0]["message"]["content"]
        yield AssistantMessage(content=[TextBlock(text="我会先读取当前 workspace 配置后再回答。")], model="<synthetic>", session_id="sdk-eval-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-eval-session",
            result="我会先读取当前 workspace 配置后再回答。",
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    eval_run = asyncio.run(
        runtime.run_feedback_eval(
            eval_case_ids=[eval_case["eval_case_id"]],
            optimization_task_id=task["optimization_task_id"],
            source="manual_task_regression",
        )
    )
    updated_task = store.find_task(task["optimization_task_id"])
    regression_run = store.get_eval_run(eval_run["eval_run_id"])
    eval_agent_run = store.find_run(run_id=regression_run["items"][0]["agent_run_id"])

    assert sync["created"] == 1
    assert "subagents 和 skills" in eval_case["prompt"]
    assert eval_case["checks_json"]["requires_tool_use"] is True
    assert "subagents 和 skills" in str(seen["prompt"])
    assert eval_run["status"] == "completed"
    assert eval_run["result_status"] == "failed"
    assert regression_run["items"][0]["status"] == "failed"
    assert regression_run["items"][0]["check_results"]
    assert updated_task["status"] == "failed"
    assert updated_task["latest_regression_run_id"] == eval_run["eval_run_id"]
    assert eval_agent_run["metadata"]["source"] == "regression_eval"


def test_update_eval_case_directly_overwrites_content(tmp_path):
    store, _ = _store(tmp_path)
    eval_case, _ = _create_eval_case(store)

    updated = store.update_eval_case(
        eval_case["eval_case_id"],
        {
            "prompt": "复测：请列出当前 workspace 的 subagents 和 skills。",
            "expected_behavior": "必须读取配置文件后回答。",
            "checks_json": {"requires_non_empty_answer": True, "requires_tool_use": False},
            "labels": [" tool_data_incomplete ", "tool_data_incomplete", "manual"],
            "status": "archived",
        },
    )

    assert updated is not None
    assert updated["eval_case_id"] == eval_case["eval_case_id"]
    assert updated["prompt"] == "复测：请列出当前 workspace 的 subagents 和 skills。"
    assert updated["expected_behavior"] == "必须读取配置文件后回答。"
    assert updated["checks_json"]["requires_tool_use"] is False
    assert updated["labels"] == ["tool_data_incomplete", "manual"]
    assert updated["status"] == "archived"
    assert store.find_eval_case(eval_case["eval_case_id"])["prompt"] == updated["prompt"]


def test_update_eval_case_rejects_empty_prompt(tmp_path):
    store, _ = _store(tmp_path)
    eval_case, _ = _create_eval_case(store)

    with pytest.raises(BusinessRuleViolation, match="prompt"):
        store.update_eval_case(eval_case["eval_case_id"], {"prompt": "  "})


def test_archived_eval_case_is_not_selected_for_automatic_feedback_eval(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-after")
    eval_case, _ = _create_eval_case(store)
    store.update_eval_case(eval_case["eval_case_id"], {"status": "archived"})
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    assert runtime._selected_eval_cases(None) == []  # noqa: SLF001 - regression coverage for active-only eval selection.


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

    assert attribution_job["profile_name"] == "attribution-analyzer"
    assert attribution_job["status"] == "completed"
    assert reused_attribution_job["job_id"] == attribution_job["job_id"]
    assert reused_attribution_job["status"] == "completed"
    assert attribution_job["profile_version"]["profile_name"] == "attribution-analyzer"
    assert proposal_job["profile_name"] == "proposal-generator"
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
    assert "归因分析智能体" in str(seen["prompt_text"])
    assert seen["cwd"] == settings.attribution_analyzer_workspace_dir
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


def test_attribution_agent_fragment_output_is_formatted_before_validation(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    from app.runtime.output_formatter import OutputFormatterResult

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["verdict_mismatch"],
            comment="告警结论错误，应该是误报",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="告警结论错误，应该是误报")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        text = json.dumps(
            {
                "type": "evidence_file",
                "id": "feedback.json",
                "reason": "分析师明确反馈告警结论错误，应该是误报。",
            },
            ensure_ascii=False,
        )
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-attribution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-attribution-session",
            result=text,
        )

    class FakeFormatter:
        def format(self, *, job_type, raw_text, job_input, expected_schema_version):
            seen["job_type"] = job_type
            seen["raw_text"] = raw_text
            seen["job_input"] = job_input
            payload = {
                "schema_version": "attribution-output/v1",
                "feedback_case_id": job_input["feedback_case_id"],
                "attribution_job_id": job_input["job_id"],
                "status": "needs_human_review",
                "problem_type": "insufficient_information",
                "optimization_object_type": "not_actionable",
                "actionability": "needs_human_analysis",
                "confidence": "medium",
                "human_review_required": True,
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈指出原告警结论错误。"}],
                "responsibility_boundary": {"owner": "needs_human_analysis", "reason": "原始输出只有证据片段，需要人工确认真实责任边界。"},
                "rationale": "归因分析智能体只输出了证据片段，格式化器保守转为需人工复核。",
                "recommended_next_step": "needs_human_review",
                "_formatter": {"name": "fake-dspy"},
            }
            return OutputFormatterResult(payload=payload, source="fake")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)
    runtime.output_formatter = FakeFormatter()

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert seen["job_type"] == "attribution"
    assert "feedback.json" in str(seen["raw_text"])
    assert attribution_job["status"] == "completed"
    assert attribution_job["raw_output_json"]["_formatter"]["name"] == "fake-dspy"
    assert output["schema_version"] == "attribution-output/v1"
    assert output["recommended_next_step"] == "needs_human_review"
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
