"""为仍公开的 AgentScope 与治理 API 补充请求输入文档。"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass

OpenApiObject = dict[str, object]
OpenApiMapping = Mapping[str, object]
OpenApiMutableMapping = MutableMapping[str, object]

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})


@dataclass(frozen=True)
class InputDoc:
    description: str
    example: object


_COMPONENT_DESCRIPTIONS: Mapping[str, str] = {
    "AgentChangeSetActionRequest": "Operator decision recorded against one Agent change set.",
    "AgentChangeSetApproveRequest": "Approve the exact candidate, complete Diff, test run, suite, and per-file review evidence inspected by the operator.",
    "AgentChangeSetCreateRequest": "Create a Git-backed candidate change set from the current Agent repository state.",
    "AgentChangeSetPublishRequest": "Publish an approved Agent change set, with an explicit forced-publication escape hatch.",
    "AgentChangeSetReviewedFileRequest": "SHA-256 identity of one complete file Diff explicitly reviewed for candidate approval.",
    "AgentCandidateFilesWriteRequest": "Commit one or more controlled files to one isolated unpublished Agent candidate.",
    "AgentCandidateTextFileWrite": "One UTF-8 candidate file replacement with optional per-file optimistic concurrency.",
    "AgentLifecycleTransitionRequest": "Requested lifecycle transition for one registered business Agent.",
    "NativeAgentCandidateRequest": "Create or continue one isolated unpublished Git candidate from reviewed AgentScope fields.",
    "NativeAgentDataInput": "Reviewed Agent-owned subset of AgentScope AgentData; identity and credentials remain backend-owned.",
    "NativeContextConfig": "AgentScope context-window and compression behavior stored in the candidate Harness.",
    "NativeInviteConfig": "AgentScope invitation behavior stored in the candidate Harness.",
    "NativeReactConfig": "AgentScope ReAct-loop behavior stored in the candidate Harness.",
    "RuntimeSessionRenameRequest": "Rename one owned AgentScope Session without changing its immutable Runtime binding.",
    "AgentTestMessageRequest": "Send one message through an isolated Agent test session.",
    "AgentTestRunCreateRequest": "Start a platform-owned Agent regression test run.",
    "AgentTestScheduleUpdateRequest": "Replace the scheduled regression-test policy for one business Agent.",
    "AgentTestSessionCreateRequest": "Create an isolated interactive test session for one Agent revision.",
    "AssetCreateRequest": "Create one governed reusable asset owned by a business Agent.",
    "AssetInheritRequest": "Copy one governed asset into another business Agent's ownership.",
    "AttachFeedbackCaseRequest": "Attach an existing first-class feedback case to the current improvement.",
    "AttributionUpsertRequest": "Replace the editable attribution content for one improvement.",
    "ConfirmationScope": "Permission scope for one native AgentScope user-confirmation result.",
    "FeedbackCaseCreateRequest": "Create one feedback case from typed sources owned by the same business Agent.",
    "FeedbackSignalCreateRequest": "Ingest one explicit, implicit, or analyst-authored feedback signal.",
    "FeedbackSignalReassignRequest": "Correct the business-Agent ownership of one feedback signal with audit attribution.",
    "FeedbackSourceRef": "Typed reference to one existing feedback source.",
    "FeedbackSourceUpdateRequest": "Patch the analyst-owned annotation fields of one feedback source.",
    "ImprovementCreateRequest": "Create one governed improvement item from feedback references.",
    "ImprovementFeedbackCreateRequest": "Attach one general feedback record to an improvement.",
    "ImprovementFeedbackReassignRequest": "Move one improvement feedback record to another improvement.",
    "ImprovementMergeRequest": "Merge another improvement item into the current target improvement.",
    "ImprovementSplitRequest": "Split one feedback reference into a new improvement item.",
    "ImprovementStageTransitionRequest": "Move one improvement item through its governed lifecycle.",
    "JsonValue": "Recursive JSON value accepted inside caller-provided metadata objects.",
    "NormalizedFeedbackUpsertRequest": "Replace the editable normalized-feedback artifact for one improvement.",
    "OptimizationChange": "One concrete target and change pair in an optimization plan.",
    "OptimizationPlanUpsertRequest": "Replace the editable optimization-plan artifact for one improvement.",
    "PendingCorrelationResolveRequest": "Supply identifiers that resolve one pending business event correlation.",
    "RuntimeChatRequest": "Start an AgentScope turn or resume the exact run waiting for a native HITL result.",
    "RuntimeSessionCreateRequest": "Create an AgentScope session pinned to the published immutable Harness version.",
    "FeedbackEventIngestRequest": "Ingest one business event and attempt deterministic run correlation. Reusing event_id requires the same normalized immutable request.",
    "WorkspaceRestoreRequest": "Restore a historical Agent workspace tree as a new commit.",
}


_FIELD_DOCS: Mapping[str, InputDoc] = {
    "actor_id": InputDoc("Identifier of the human or system actor that caused the business event.", "analyst-17"),
    "after": InputDoc("Structured value after the observed business change.", {"verdict": "malicious"}),
    "agent_id": InputDoc("Registered business Agent identifier.", "security-operations-expert"),
    "agent_data": InputDoc("Reviewed AgentScope fields to write into the isolated Git candidate.", {"name": "SOC evidence reviewer"}),
    "agent_version_id": InputDoc("Agent revision associated with the feedback.", "agent-ver-20260729"),
    "asset_type": InputDoc("Governed asset category.", "methodology"),
    "auto_captured": InputDoc("Whether the source was captured automatically rather than entered by an analyst.", True),
    "auto_merge": InputDoc("Whether deterministic duplicate detection may merge the new improvement automatically.", False),
    "base_commit_sha": InputDoc("Expected Git commit from which the candidate change set starts.", "a1b2c3d4e5f6"),
    "before": InputDoc("Structured value before the observed business change.", {"verdict": "unknown"}),
    "body": InputDoc("Governed asset body.", "所有高危处置必须同时记录证据来源。"),
    "candidate_commit_sha": InputDoc("Exact immutable candidate Git commit being approved.", "b" * 40),
    "change": InputDoc("Concrete modification to make to the selected target.", "补充停止后续聊的并发回归测试。"),
    "change_set_id": InputDoc("Agent change set associated with this test session.", "chg-20260729-001"),
    "changes": InputDoc(
        "Non-empty ordered optimization changes.",
        [{"target": "tests/runtime", "change": "补充停止后续聊的并发回归测试。"}],
    ),
    "comment": InputDoc("Optional analyst or operator comment.", "已复核原始运行证据。"),
    "compression_fallback_to_truncation": InputDoc("Whether context compression may fall back to truncation.", True),
    "compression_prompt": InputDoc("Optional prompt used by AgentScope when compressing context.", "Summarize retained evidence."),
    "compression_tool_enabled": InputDoc("Whether AgentScope may use its compression tool.", False),
    "context_buffer_ratio": InputDoc("Fraction of the context window retained as a safety buffer.", 0.2),
    "context_config": InputDoc("AgentScope context-window and compression settings.", {}),
    "commit_sha": InputDoc("Git commit to test; omit to use the route's documented current revision.", "a1b2c3d4e5f6"),
    "confidence": InputDoc("Confidence assigned to the feedback or business event.", "high"),
    "cron_expression": InputDoc("Five-field cron expression interpreted in the supplied timezone.", "0 2 * * *"),
    "detail_sha256": InputDoc("SHA-256 of the canonical complete file-Diff response.", "c" * 64),
    "diff_digest": InputDoc("SHA-256 of the complete candidate Diff summary.", "d" * 64),
    "enabled": InputDoc("Whether scheduled Agent regression testing is enabled.", True),
    "entities": InputDoc("业务对象引用按类型分组；不承载治理 feedback_case_id 的归属关系。", {"document": ["guide-1"]}),
    "event_id": InputDoc(
        "Caller-stable globally unique idempotency identifier. A retry must keep the same normalized immutable request; different content returns 409 FEEDBACK_EVENT_ID_CONFLICT.",
        "business-event-20260729-001",
    ),
    "event_type": InputDoc("Nonempty business event type describing the observed change.", "case.verdict_changed"),
    "evidence": InputDoc("Evidence points supporting the attribution.", ["停止后同一 session 的 active turn 已释放。"]),
    "expected_current_commit_sha": InputDoc("Current workspace HEAD used as an optimistic concurrency guard.", "a1b2c3d4e5f6"),
    "expected_candidate_commit_sha": InputDoc(
        "Exact candidate commit reviewed by the caller for continuation, approval, or publication.",
        "b" * 40,
    ),
    "expected_diff_digest": InputDoc("SHA-256 of the complete reviewed candidate Diff.", "d" * 64),
    "expected_suite_digest": InputDoc("SHA-256 identity of the reviewed candidate test suite.", "e" * 64),
    "expected_test_run_id": InputDoc("Exact candidate test run reviewed for normal publication.", "atr-20260729-tested-candidate"),
    "expected_sha256": InputDoc("SHA-256 returned by the preceding read; rejects stale replacement writes.", "7f83b1657ff1fc53b92dc18148a1d65dfa13514e"),
    "feedback_case_id": InputDoc("Existing first-class feedback case identifier.", "fbc-20260729-001"),
    "feedback_ref": InputDoc("Feedback reference to move into a new split improvement.", "feedback-20260729-001"),
    "files": InputDoc("Non-empty list of reviewed candidate file replacements.", [{"path": "AGENT.md", "content": "# Agent\n"}]),
    "force": InputDoc("Whether to use the audited forced-publication path.", False),
    "force_reason": InputDoc("Required audit reason when force is true.", "紧急修复已由值班负责人复核。"),
    "impact": InputDoc("Observed or expected impact.", "高：停止后的下一轮无法继续会话。"),
    "input": InputDoc(
        "AgentScope 原生 Msg、Msg 列表、确认/外部执行事件或 null；同次重试复用显式 input.id，无 ID 请求不自动重发。",
        {
            "id": "user-input-20260913-001",
            "name": "user",
            "role": "user",
            "content": [{"type": "text", "text": "请核查当前告警并给出处置建议"}],
        },
    ),
    "labels": InputDoc("Analyst-defined labels used for filtering and triage.", ["session", "concurrency"]),
    "invite_config": InputDoc("AgentScope invitation settings for the candidate.", {"invitable": False}),
    "invite_description": InputDoc("Description shown when the Agent is invitable.", "Invite for evidence review."),
    "invitable": InputDoc("Whether other Agents may invite this Agent.", False),
    "interruption_message": InputDoc("Assistant message emitted after an interrupted ReAct loop.", "The run was interrupted."),
    "interruption_raise_cancelled_error": InputDoc("Whether interruption is surfaced as a cancellation error.", False),
    "message": InputDoc("Non-blank user message or operator note for this action.", "请核查当前告警并给出处置建议"),
    "max_image_num": InputDoc("Maximum number of images retained in context.", 5),
    "max_iters": InputDoc("Maximum ReAct iterations for one run.", 50),
    "mode": InputDoc("POSIX file mode preserved in the candidate Git tree.", 420),
    "metadata": InputDoc("Caller-provided JSON metadata retained for correlation or observability.", {"source": "soc-console"}),
    "name": InputDoc("Optional human-readable AgentScope session name.", "SOC console investigation"),
    "note": InputDoc("Optional operator note written to the governance audit trail.", "已核对候选差异与测试证据。"),
    "operator": InputDoc("Operator identity recorded in the governance audit trail.", "platform-operator"),
    "path": InputDoc("Controlled workspace-relative path inside the isolated candidate.", "AGENT.md"),
    "possible_object": InputDoc("Component or governance asset that may own the problem.", "session turn admission"),
    "possible_reason": InputDoc("Current hypothesis for the observed problem.", "停止路径未等待 session fence 释放。"),
    "priority": InputDoc("Analyst-assigned triage priority.", "high"),
    "problem": InputDoc("One-sentence normalized problem statement.", "停止流式输出后再次发送消息发生会话冲突。"),
    "raw_text": InputDoc("Original feedback text retained as evidence.", "停止后再次发送消息时报 SESSION_CONFLICT。"),
    "reason": InputDoc("Optional audited reason for the requested operation.", "恢复到已验证的 workspace 版本。"),
    "requires_review": InputDoc("Whether the source must remain in the human-review queue.", True),
    "reserve_ratio": InputDoc("Fraction of context reserved before compression.", 0.1),
    "react_config": InputDoc("AgentScope ReAct-loop settings for the candidate.", {}),
    "reviewed_files": InputDoc(
        "Every changed file and the digest of its complete Diff explicitly reviewed by the operator.",
        [{"path": "AGENT.md", "detail_sha256": "c" * 64}],
    ),
    "responsibility_boundary": InputDoc("Responsibility-boundary statements for the attribution.", ["Runtime owns session fencing."]),
    "role": InputDoc("Role of this text input message.", "user"),
    "run_id": InputDoc("Managed Agent run identifier used for correlation.", "run-20260729-001"),
    "scenario": InputDoc("Business scenario associated with this feedback.", "playground-stop-and-resend"),
    "session_id": InputDoc("AgentScope session identifier used for continuation or correlation.", "session-20260909-001"),
    "signal_id": InputDoc("Optional caller-stable feedback signal identifier.", "signal-20260729-001"),
    "source": InputDoc("Origin category for general improvement feedback.", "playground_run"),
    "source_feedback_refs": InputDoc("Feedback references that justify the improvement.", ["signal-20260729-001"]),
    "source_id": InputDoc("Identifier within the selected feedback-source kind.", "signal-20260729-001"),
    "source_improvement_id": InputDoc("Improvement item from which this operation or asset originates.", "imp-20260729-001"),
    "source_kind": InputDoc("Typed feedback-source discriminator.", "signal"),
    "source_refs": InputDoc(
        "Non-empty typed source list; all sources must resolve to the same business Agent.",
        [{"source_kind": "signal", "source_id": "signal-20260729-001"}],
    ),
    "source_system": InputDoc("System that produced the business event.", "soc-console"),
    "source_type": InputDoc("Feedback signal source category.", "explicit_feedback"),
    "stage": InputDoc("Target improvement lifecycle stage.", "attribution"),
    "suite_digest": InputDoc("SHA-256 identity of the exact candidate test suite.", "e" * 64),
    "test_run_id": InputDoc("Exact platform candidate test-run identifier reviewed for approval.", "atr-20260729-tested-candidate"),
    "status": InputDoc("Target or filter status from the operation's documented closed enum.", "active"),
    "stop_on_reject": InputDoc("Whether a rejected tool confirmation stops the ReAct loop.", False),
    "structured_output_grace_iters": InputDoc("Additional iterations allowed to complete structured output.", 5),
    "summary_template": InputDoc("Optional template used for compressed context summaries.", "Retain evidence and uncertainty."),
    "system_prompt": InputDoc("Agent-owned system prompt committed to the candidate Harness.", "Review governed evidence."),
    "suggestion": InputDoc("Suggested direction for resolving the normalized problem.", "停止接口等待 run 终态与 fence 释放。"),
    "summary": InputDoc("Human-editable summary for this governed artifact.", "停止后续聊需要统一释放 session fence。"),
    "tag_name": InputDoc("Optional release tag; omit to use the server's release naming policy.", "agent-release-20260729"),
    "target": InputDoc("Prompt, skill, profile, config, test, or other asset changed by this item.", "tests/runtime"),
    "target_agent_id": InputDoc("Registered business Agent that receives the inherited asset.", "soc-analyst"),
    "target_commit_sha": InputDoc("Historical workspace commit whose tree should be restored.", "9f8e7d6c5b4a"),
    "target_improvement_id": InputDoc("Improvement item that will receive the moved feedback.", "imp-20260729-002"),
    "task_id": InputDoc("Business task identifier associated with the feedback.", "task-20260729-001"),
    "text": InputDoc("Non-blank text in this typed input block.", "请复核该告警的处置结论"),
    "timestamp": InputDoc("RFC 3339 timestamp supplied by the source system.", "2026-07-29T12:00:00Z"),
    "timezone": InputDoc("IANA timezone used to interpret the cron expression.", "Asia/Shanghai"),
    "title": InputDoc("Human-readable title for this governed object.", "修复停止后再次发送的会话冲突"),
    "tool_result_limit": InputDoc("Maximum retained tool-result size in AgentScope context.", 50000),
    "trigger_ratio": InputDoc("Context usage ratio that triggers compression.", 0.8),
    "type": InputDoc("Typed source or native AgentScope input discriminator.", "message"),
    "user_quote": InputDoc("Original user wording supporting the normalized feedback.", "停止后再发消息就报会话冲突。"),
}


# 仅补文档注释；类型、必填项、枚举与默认值继续由固定版本 AgentScope 导出。
_NATIVE_COMPONENTS = frozenset(
    {
        "Base64Source",
        "URLSource",
        "ConfirmResult",
        "DataBlock",
        "ExternalExecutionResultEvent",
        "HintBlock",
        "Msg",
        "PermissionRule",
        "TextBlock",
        "ThinkingBlock",
        "ToolCallBlock",
        "ToolResultBlock",
        "Usage",
        "UserConfirmResultEvent",
        "ErrorInfo",
    }
)
_NATIVE_FIELD_DOCS: Mapping[str, InputDoc] = {
    "id": InputDoc("AgentScope 原生对象 ID；消息或事件重试复用显式 ID，工具结果保留对应调用 ID。", "native-input-001"),
    "created_at": InputDoc("AgentScope 原生对象的创建时间；省略时由原生模型生成。", "2026-09-14T00:00:00+00:00"),
    "finished_at": InputDoc("原生对象完成时间；未完成时为 null。", None),
    "data": InputDoc("Base64 编码的文件内容，不含 data URL 前缀。", "SGVsbG8="),
    "media_type": InputDoc("文件内容的 MIME 类型。", "text/plain"),
    "url": InputDoc("原生多模态资料的 URL；可用性与读取能力由 Runtime 决定。", "https://example.com/document.txt"),
    "confirmed": InputDoc("是否允许当前精确工具调用继续执行。", True),
    "tool_call": InputDoc("从待确认事件原样取得的工具调用。", {"type": "tool_call", "id": "tool-001", "name": "Read", "input": "{}"}),
    "rules": InputDoc("原生确认规则字段；AgentGov 客户端应省略，不用它修改已发布权限。", None),
    "reply_id": InputDoc("需要继续的原生 reply ID，取自当前待确认或待外部执行事件。", "reply-001"),
    "confirm_results": InputDoc("当前 reply 的逐项原生确认结果。", [{"confirmed": True, "tool_call": {"id": "tool-001", "name": "Read", "input": "{}"}}]),
    "execution_results": InputDoc(
        "当前 reply 的原生外部执行结果，逐项对应原工具调用 ID。", [{"id": "tool-001", "name": "Read", "output": "资料读取完成", "state": "success"}]
    ),
    "hint": InputDoc("原生提示文本或文本/资料块列表。", "请先核对已提供的资料。"),
    "content": InputDoc("有序的 AgentScope 原生消息内容块。", [{"type": "text", "text": "你好"}]),
    "usage": InputDoc("原生模型 token 用量；尚无统计时为 null。", None),
    "finished_reason": InputDoc("原生回复结束原因；消息尚未完成时为 null。", None),
    "structured_output": InputDoc("原生结构化输出；未产生时为 null。", None),
    "error": InputDoc("原生结构化错误；无错误时为 null，不替代正常消息正文。", None),
    "tool_name": InputDoc("该原生权限规则作用的工具名称。", "Read"),
    "rule_content": InputDoc("原生工具规则的匹配内容；没有额外条件时为 null。", None),
    "behavior": InputDoc("原生权限匹配行为，取值以 AgentScope 枚举为准。", "ask"),
    "thinking": InputDoc("原生推理内容块的文本；是否提供由模型决定。", "核对资料中的事实。"),
    "suggested_rules": InputDoc("工具提出的原生规则建议，不等于已批准权限。", []),
    "output": InputDoc("工具结果的原生文本或内容块列表。", "资料读取完成"),
    "metadata": InputDoc("AgentScope 原生元数据；不表示 AgentGov 或 Langfuse 会保存原始业务正文。", {}),
    "input_tokens": InputDoc("此次原生模型调用的输入 token 数。", 10),
    "output_tokens": InputDoc("此次原生模型调用的输出 token 数。", 5),
    "cache_input_tokens": InputDoc("此次调用命中的输入缓存 token 数。", 0),
    "cache_creation_input_tokens": InputDoc("此次调用创建输入缓存使用的 token 数。", 0),
}


_FIELD_OVERRIDES: Mapping[tuple[str, str], InputDoc] = {
    ("FeedbackEventIngestRequest", "timestamp"): InputDoc(
        "Timezone-aware RFC 3339 timestamp; equivalent offsets are normalized to the same UTC instant without losing fractional precision.",
        "2026-07-29T12:00:00Z",
    ),
    ("Msg", "name"): InputDoc("原生消息发送者名称；不是会话名或幂等键。", "user"),
    ("DataBlock", "name"): InputDoc("资料块的可选显示名称。", "document.txt"),
    ("DataBlock", "source"): InputDoc("原生 Base64 或 URL 资料来源对象。", {"type": "base64", "data": "SGVsbG8=", "media_type": "text/plain"}),
    ("ToolCallBlock", "name"): InputDoc("原生工具调用名称。", "Read"),
    ("ToolCallBlock", "input"): InputDoc("原生工具调用参数的 JSON 字符串，确认时保留原值。", '{"path":"references/README.md"}'),
    ("ToolCallBlock", "state"): InputDoc("原生工具调用状态。", "pending"),
    ("ToolResultBlock", "name"): InputDoc("与原工具调用对应的名称。", "Read"),
    ("ToolResultBlock", "state"): InputDoc("原生工具结果状态。", "running"),
    ("ErrorInfo", "type"): InputDoc("原生结构化错误类别。", "unknown"),
    ("ErrorInfo", "message"): InputDoc("原生错误的可读说明，与正常回答正文分开。", "The session could not be prepared."),
    ("RuntimeChatRequest", "agent_id"): InputDoc(
        "AgentScope runtime_agent_id pinned by the target Session.",
        "runtime-agent-version-20260909-001",
    ),
    ("RuntimeSessionCreateRequest", "agent_id"): InputDoc(
        "AgentScope runtime_agent_id bound by the successful published-release activation.",
        "runtime-agent-version-20260909-001",
    ),
    ("AgentCandidateTextFileWrite", "content"): InputDoc(
        "Complete UTF-8 candidate file content, not a patch; it remains inactive until release.",
        '{\n  "mcp_config": {"type": "http_mcp", "url": "${SEC_OPS_MCP_URL}"},\n  "credential_refs": []\n}\n',
    ),
    ("NativeAgentCandidateRequest", "reason"): InputDoc(
        "Optional audited reason for creating or revising this candidate.",
        "Create a reviewed candidate for platform tests.",
    ),
    ("NativeAgentDataInput", "name"): InputDoc(
        "Human-readable Agent name committed to the candidate Harness.",
        "SOC evidence reviewer",
    ),
    ("RuntimeSessionRenameRequest", "name"): InputDoc(
        "New human-readable name for the existing owned Session.",
        "SOC console follow-up",
    ),
    ("AgentLifecycleTransitionRequest", "status"): InputDoc(
        "Target lifecycle status: active, evaluating, deprecated, or archived.",
        "evaluating",
    ),
    ("AgentTestMessageRequest", "metadata"): InputDoc(
        "Test-only message metadata retained inside the isolated test session.",
        {"case": "stop-and-resend"},
    ),
    ("FeedbackSourceUpdateRequest", "metadata"): InputDoc(
        "Replacement annotation metadata; omit the field to leave metadata unchanged.",
        {"reviewed_by": "analyst-17"},
    ),
    ("FeedbackSourceUpdateRequest", "status"): InputDoc(
        "Annotation workflow status: new, triaged, in_batch, resolved, or archived.",
        "triaged",
    ),
}


_PATH_PARAMETER_DOCS: Mapping[str, InputDoc] = {
    "agent_id": InputDoc("Registered business Agent identifier addressed by this operation.", "security-operations-expert"),
    "asset_id": InputDoc("Governed asset identifier addressed by this operation.", "asset-20260729-001"),
    "change_set_id": InputDoc("Agent change set identifier addressed by this operation.", "chg-20260729-001"),
    "event_id": InputDoc("Business event identifier addressed by this operation.", "business-event-20260729-001"),
    "evidence_package_id": InputDoc("Evidence package identifier addressed by this operation.", "evp-20260729-001"),
    "feedback_case_id": InputDoc("First-class feedback case identifier addressed by this operation.", "fbc-20260729-001"),
    "feedback_id": InputDoc("Improvement feedback identifier addressed by this operation.", "feedback-20260729-001"),
    "governance_agent_id": InputDoc(
        "Registered business Agent whose current published Runtime version is addressed.",
        "security-operations-expert",
    ),
    "file_name": InputDoc("Included evidence-package file name.", "manifest.json"),
    "improvement_id": InputDoc("Improvement item identifier addressed by this operation.", "imp-20260729-001"),
    "job_id": InputDoc("Historical Agent job identifier addressed by this read-only operation.", "job-20260729-001"),
    "pending_id": InputDoc("Pending-correlation identifier addressed by this operation.", "pending-20260729-001"),
    "release_id": InputDoc("Agent release identifier addressed by this operation.", "rel-20260729-001"),
    "run_id": InputDoc("Managed Agent run identifier addressed by this operation.", "run-20260729-001"),
    "session_id": InputDoc("AgentScope session identifier addressed by this operation.", "session-20260909-001"),
    "signal_id": InputDoc("Feedback signal identifier addressed by this operation.", "signal-20260729-001"),
    "source_id": InputDoc("Identifier within the source_kind namespace.", "signal-20260729-001"),
    "source_kind": InputDoc("Feedback source namespace: signal, event, or pending_correlation.", "signal"),
    "test_run_id": InputDoc("Platform Agent test-run identifier.", "test-run-20260729-001"),
    "test_session_id": InputDoc("Isolated Agent test-session identifier.", "test-session-20260729-001"),
    "trace_id": InputDoc("Langfuse trace identifier addressed by this debug operation.", "trace-20260729-001"),
}


_QUERY_PARAMETER_DOCS: Mapping[str, InputDoc] = {
    "before": InputDoc("Opaque AgentScope message cursor returned by the previous page.", "message-cursor-from-previous-page"),
    "agent_id": InputDoc("Registered business Agent selector or ownership filter for this operation.", "security-operations-expert"),
    "entity_type": InputDoc("业务对象类型；必须与 entity_id 成对，精确匹配。", "document"),
    "asset_type": InputDoc("Filter assets by the closed governed asset category.", "methodology"),
    "entity_id": InputDoc("所选业务对象类型中的完整标识；不是反馈 Case 归属。", "guide-1"),
    "change_set_id": InputDoc("Filter test runs by Agent change set.", "chg-20260729-001"),
    "commit_sha": InputDoc("Read or filter against this exact Agent repository commit.", "a1b2c3d4e5f6"),
    "input_id": InputDoc("按原顺序重复提供显式原生消息/事件 ID，查询同一 Session 绑定内的精确操作。", ["user-input-20260913-001"]),
    "operation_kind": InputDoc("原生输入动作类别；初始消息与确认/外部执行分别关联。", "initial"),
    "before_created_at": InputDoc("上一页最后一条 run 的 created_at，必须与 before_run_id 一同使用。", "2026-09-13T00:00:00+00:00"),
    "before_run_id": InputDoc("上一页最后一条 run 的 run_id，用于时间相同记录的稳定分页。", "run-previous-page-last"),
    "cursor": InputDoc("Opaque pagination cursor returned by the preceding history page.", "cursor-20260729-001"),
    "event_type": InputDoc("Filter business events by the exact caller-defined event type.", "case.verdict_changed"),
    "include_host_mounts": InputDoc("Include host mount paths in operator diagnostics.", False),
    "include_messages": InputDoc("Deprecated no-op; canonical messages must be read from AgentScope.", False),
    "job_type": InputDoc("Filter historical Agent jobs by the documented closed job type.", "feedback_attribution"),
    "governance_agent_id": InputDoc(
        "List Sessions across every retained Runtime version of this registered business Agent.",
        "security-operations-expert",
    ),
    "limit": InputDoc("Maximum number of records returned by this operation, within its documented bounds.", 100),
    "order": InputDoc("Conversation item order; only chronological asc is currently accepted.", "asc"),
    "path": InputDoc("Editable AgentScope Harness path interpreted by this operation.", "mcp/soc-readonly.json"),
    "q": InputDoc("Case-insensitive free-text search over the feedback-case title and source identifiers.", "会话冲突"),
    "run_id": InputDoc("Filter records by managed Agent run identifier.", "run-20260729-001"),
    "scope_id": InputDoc("Filter historical jobs by backend-owned scope identifier.", "fbc-20260729-001"),
    "scope_kind": InputDoc("Filter historical jobs by backend-owned scope category.", "feedback_case"),
    "session_id": InputDoc("Filter records by AgentScope session identifier.", "session-20260909-001"),
    "source": InputDoc("Filter test-run history by trigger source.", "manual"),
    "source_improvement_id": InputDoc("Filter assets by their originating improvement item.", "imp-20260729-001"),
    "source_type": InputDoc("Filter feedback signals by the documented source-type enum.", "explicit_feedback"),
    "status": InputDoc("Filter records by the closed status enum documented for this operation.", "running"),
}


_HEADER_PARAMETER_DOCS: Mapping[str, InputDoc] = {
    "X-AgentGov-Confirmation-Scope": InputDoc("仅对明确选择本次运行允许的 USER_CONFIRM_RESULT 发送 run；其他消息及单次允许/拒绝不发送。", "run"),
    "Idempotency-Key": InputDoc(
        "Caller-stable key that makes supported resource creation safe to retry after an ambiguous transport failure.",
        "session-create-20260909-001",
    ),
}


_QUERY_PARAMETER_OVERRIDES: Mapping[tuple[str, str, str], InputDoc] = {
    ("/api/agent-runs/by-input-identity", "get", "agent_id"): InputDoc(
        "目标 Session 已绑定的 AgentScope runtime_agent_id，不是业务 Agent 名称。", "runtime-agent-version-20260909-001"
    ),
    ("/api/agent-change-sets", "get", "status"): InputDoc(
        "Filter Agent change sets by their governed change-set lifecycle state.",
        "draft",
    ),
    ("/api/agent-releases", "get", "status"): InputDoc(
        "Filter Agent releases by published, archived, rolled-back, or rollback-failed state.",
        "published",
    ),
    ("/api/agent-jobs", "get", "job_type"): InputDoc(
        "Filter historical Agent jobs by their registered governance job type.",
        "attribution",
    ),
    ("/api/feedback-cases", "get", "status"): InputDoc(
        "Filter feedback cases by their governed evidence/attribution/review state.",
        "pending_evidence",
    ),
    ("/api/pending-correlations", "get", "status"): InputDoc(
        "Filter pending correlations by pending or resolved state.",
        "pending",
    ),
    ("/api/agent-change-sets/{change_set_id}/file-diff", "get", "path"): InputDoc(
        "Repository-relative changed file whose unified diff should be returned.",
        "mcp/soc-readonly.json",
    ),
    ("/api/agent-registry/{agent_id}/test-suite/file", "get", "path"): InputDoc(
        "Non-empty workspace-relative pytest file path from the Agent test suite.",
        "tests/test_runtime.py",
    ),
    ("/api/feedback-sources", "get", "limit"): InputDoc(
        "Maximum number of unified feedback sources to return (1–1000).",
        500,
    ),
    ("/api/agent-test-runs/history", "get", "limit"): InputDoc(
        "Maximum number of historical test runs to return (1–200).",
        50,
    ),
}


_INLINE_MULTIPART_DOCS: Mapping[str, InputDoc] = {
    "package": InputDoc(
        "A .tar.gz archive with exactly one workspace/ root and a matching workspace/agent.yaml id.",
        "business-agent-workspace.tar.gz",
    ),
    "name": InputDoc("Required display name only when importing a new business Agent.", "SOC Analyst"),
    "expected_current_commit_sha": InputDoc(
        "Required optimistic-concurrency commit when overwriting an existing Agent.",
        "a1b2c3d4e5f6",
    ),
    "reason": InputDoc("Optional audit reason used as the overwrite commit message.", "导入已离线验收的 workspace 包。"),
}


def apply_request_input_documentation(schema: OpenApiMutableMapping) -> None:
    """Fill complete body/field/parameter docs without changing validation."""

    components = _component_schemas(schema)
    paths = _mapping(schema.get("paths", {}))
    reachable = _request_component_names(paths, components)
    for component_name in sorted(reachable):
        component = _mapping(components.get(component_name, {}))
        description = _COMPONENT_DESCRIPTIONS.get(component_name)
        if description and not _meaningful(component.get("description")):
            component["description"] = description
        properties = _mapping(component.get("properties", {}))
        for field_name, raw_property in properties.items():
            if not isinstance(field_name, str) or not isinstance(raw_property, MutableMapping):
                continue
            documentation = _FIELD_OVERRIDES.get((component_name, field_name))
            if component_name in _NATIVE_COMPONENTS:
                documentation = documentation or _NATIVE_FIELD_DOCS.get(field_name)
            documentation = documentation or _FIELD_DOCS.get(field_name)
            if documentation is None:
                continue
            if not _meaningful(raw_property.get("description")):
                raw_property["description"] = documentation.description
            if not raw_property.get("examples") and "example" not in raw_property:
                raw_property["examples"] = [deepcopy(raw_property.get("const", documentation.example))]

    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, MutableMapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, MutableMapping):
                continue
            _document_parameters(path, method, operation)
            _document_request_body(operation)


def _document_parameters(path: str, method: str, operation: OpenApiMutableMapping) -> None:
    parameters = operation.get("parameters", [])
    if not isinstance(parameters, list):
        return
    for parameter in parameters:
        if not isinstance(parameter, MutableMapping):
            continue
        location = parameter.get("in")
        name = parameter.get("name")
        if not isinstance(name, str):
            continue
        documentation = None
        if location == "path":
            documentation = _PATH_PARAMETER_DOCS.get(name)
        elif location == "query":
            documentation = _QUERY_PARAMETER_OVERRIDES.get((path, method, name)) or _QUERY_PARAMETER_DOCS.get(name)
        elif location == "header":
            documentation = _HEADER_PARAMETER_DOCS.get(name)
        if documentation is None:
            continue
        if not _meaningful(parameter.get("description")):
            parameter["description"] = documentation.description
        if "example" not in parameter and not parameter.get("examples"):
            parameter["example"] = deepcopy(documentation.example)


def _document_request_body(operation: OpenApiMutableMapping) -> None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, MutableMapping):
        return
    summary = operation.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) and summary.strip() else "Submit the documented request"
    if not _meaningful(request_body.get("description")):
        request_body["description"] = (
            f"{summary_text} payload. Use the schema for field constraints and select a named example for a "
            "validated scenario; optional fields should be omitted instead of sent as null placeholders."
        )
    content = _mapping(request_body.get("content", {}))
    multipart = _mapping(content.get("multipart/form-data", {}))
    multipart_schema = _mapping(multipart.get("schema", {}))
    properties = _mapping(multipart_schema.get("properties", {}))
    for field_name, raw_property in properties.items():
        if not isinstance(field_name, str) or not isinstance(raw_property, MutableMapping):
            continue
        documentation = _INLINE_MULTIPART_DOCS.get(field_name)
        if documentation is None:
            continue
        if not _meaningful(raw_property.get("description")):
            raw_property["description"] = documentation.description
        if not raw_property.get("examples") and "example" not in raw_property:
            raw_property["examples"] = [deepcopy(documentation.example)]


def _nested_references(fragment: object, *, array_item: bool = False) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    if isinstance(fragment, Mapping):
        reference = fragment.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            found.append((reference.rsplit("/", 1)[-1], array_item))
        for key, value in fragment.items():
            found.extend(_nested_references(value, array_item=array_item or key == "items"))
    elif isinstance(fragment, list):
        for value in fragment:
            found.extend(_nested_references(value, array_item=array_item))
    return list(dict.fromkeys(found))


def _request_component_names(
    paths: OpenApiMutableMapping,
    components: OpenApiMutableMapping,
) -> set[str]:
    found: set[str] = set()
    queue: list[str] = []
    for path_item in paths.values():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            queue.extend(name for name, _ in _nested_references(operation.get("requestBody", {})))
    while queue:
        name = queue.pop()
        if name in found:
            continue
        found.add(name)
        queue.extend(reference for reference, _ in _nested_references(components.get(name, {})) if reference not in found)
    return found


def _meaningful(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _component_schemas(schema: OpenApiMutableMapping) -> OpenApiMutableMapping:
    components = _mapping(schema.get("components", {}))
    return _mapping(components.get("schemas", {}))


def _mapping(value: object) -> OpenApiMutableMapping:
    return value if isinstance(value, MutableMapping) else {}
