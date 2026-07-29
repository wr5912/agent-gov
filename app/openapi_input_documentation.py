"""OpenAPI request-input documentation projection.

Runtime validation remains owned by FastAPI/Pydantic. This module only fills the
presentation metadata that Pydantic cannot share cleanly across legacy request
models and route parameters, then renders a flattened Responses field table for
Swagger UI. Every fallback is semantic and named; there is no type-only
``string``/``additionalProp`` placeholder generation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass

OpenApiObject = dict[str, object]
OpenApiMapping = Mapping[str, object]
OpenApiMutableMapping = MutableMapping[str, object]

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
RESPONSES_PATH = "/v1/responses"


@dataclass(frozen=True)
class InputDoc:
    description: str
    example: object


_COMPONENT_DESCRIPTIONS: Mapping[str, str] = {
    "AgentChangeSetActionRequest": "Operator decision recorded against one Agent change set.",
    "AgentChangeSetCreateRequest": "Create a Git-backed candidate change set from the current Agent repository state.",
    "AgentChangeSetPublishRequest": "Publish an approved Agent change set, with an explicit forced-publication escape hatch.",
    "AgentConfigFileUpdateRequest": "Complete replacement of one editable Agent configuration file with optimistic concurrency.",
    "AgentGovDebug": "Control-mode debug switches for the transitional Responses stream.",
    "AgentLifecycleTransitionRequest": "Requested lifecycle transition for one registered business Agent.",
    "AgentReleaseRestoreRequest": "Restore a published Agent release into a new candidate workspace state.",
    "AgentReleaseRollbackRequest": "Rollback the active Agent release to the selected release.",
    "AgentRepositoryDiscardChangesRequest": "Discard selected uncommitted paths in the business Agent repository.",
    "AgentRepositorySnapshotRequest": "Create a Git snapshot of the current business Agent repository state.",
    "AgentTestMessageRequest": "Send one message through an isolated Agent test session.",
    "AgentTestRunCreateRequest": "Start a platform-owned Agent regression test run.",
    "AgentTestScheduleUpdateRequest": "Replace the scheduled regression-test policy for one business Agent.",
    "AgentTestSessionCreateRequest": "Create an isolated interactive test session for one Agent revision.",
    "AssetCreateRequest": "Create one governed reusable asset owned by a business Agent.",
    "AssetInheritRequest": "Copy one governed asset into another business Agent's ownership.",
    "AttachFeedbackCaseRequest": "Attach an existing first-class feedback case to the current improvement.",
    "AttributionUpsertRequest": "Replace the editable attribution content for one improvement.",
    "ClaudeUserInputDecisionRequest": "Resolve one exact waiting Claude tool-permission or user-question request.",
    "ConversationCreateRequest": "Create an empty AgentGov conversation projection with optional client metadata.",
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
    "OpenAIChatCompletionRequest": "Text-only non-streaming request for the deprecated Chat Completions compatibility shim.",
    "OpenAIChatMessage": "One text-only message in a deprecated Chat Completions request.",
    "OpenAICompatAgentUpdate": "Select the registered business Agent used by strict OpenAI-compatible surfaces.",
    "OptimizationChange": "One concrete target and change pair in an optimization plan.",
    "OptimizationPlanUpsertRequest": "Replace the editable optimization-plan artifact for one improvement.",
    "PendingCorrelationResolveRequest": "Supply identifiers that resolve one pending SOC event correlation.",
    "ResponsesInputMessage": "Typed message item accepted in the transitional Responses input array.",
    "ResponsesInputText": "Typed text block nested in a Responses input message.",
    "RuntimeRawEventsRequest": "Managed Agent turn whose response boundary exposes byte-exact Runtime stdout.",
    "SocEventIngestRequest": "Ingest one typed SOC event and attempt deterministic run correlation.",
    "WorkspaceRestoreRequest": "Restore a historical Agent workspace tree as a new commit.",
}


_FIELD_DOCS: Mapping[str, InputDoc] = {
    "action": InputDoc("Decision action allowed for the exact waiting request.", "deny"),
    "actor_id": InputDoc("Identifier of the human or system actor that caused the SOC event.", "analyst-17"),
    "after": InputDoc("Structured value after the observed SOC change.", {"verdict": "malicious"}),
    "agent_id": InputDoc("Registered business Agent identifier.", "security-operations-expert"),
    "agent_version_id": InputDoc("Agent revision associated with the feedback.", "agent-ver-20260729"),
    "agentgov": InputDoc(
        "AgentGov control extension; omit it to select strict mode.",
        {"agent_id": "security-operations-expert"},
    ),
    "alert_id": InputDoc("SOC alert identifier used for correlation or feedback routing.", "alert-20260729-001"),
    "answer": InputDoc("Structured answers for an AskUserQuestion decision.", {"response": "只处理当前告警资产"}),
    "asset_type": InputDoc("Governed asset category.", "methodology"),
    "auto_captured": InputDoc("Whether the source was captured automatically rather than entered by an analyst.", True),
    "auto_merge": InputDoc("Whether deterministic duplicate detection may merge the new improvement automatically.", False),
    "base_commit_sha": InputDoc("Expected Git commit from which the candidate change set starts.", "a1b2c3d4e5f6"),
    "before": InputDoc("Structured value before the observed SOC change.", {"verdict": "unknown"}),
    "body": InputDoc("Governed asset body.", "所有高危处置必须同时记录证据来源。"),
    "case_id": InputDoc("SOC business-case identifier used for correlation or feedback routing.", "case-20260729-001"),
    "change": InputDoc("Concrete modification to make to the selected target.", "补充停止后续聊的并发回归测试。"),
    "change_set_id": InputDoc("Agent change set associated with this test session.", "chg-20260729-001"),
    "changes": InputDoc(
        "Non-empty ordered optimization changes.",
        [{"target": "tests/runtime", "change": "补充停止后续聊的并发回归测试。"}],
    ),
    "comment": InputDoc("Optional analyst or operator comment.", "已复核原始运行证据。"),
    "commit_sha": InputDoc("Git commit to test; omit to use the route's documented current revision.", "a1b2c3d4e5f6"),
    "confidence": InputDoc("Confidence assigned to the feedback or SOC event.", "high"),
    "conversation": InputDoc("AgentGov conversation projection to continue.", "conv_sess-20260729"),
    "cron_expression": InputDoc("Five-field cron expression interpreted in the supplied timezone.", "0 2 * * *"),
    "debug": InputDoc("Optional control-stream debug switches.", {"sdk_raw": True}),
    "decision_token": InputDoc("One-time token returned for this exact authenticated waiting request.", "token-from-exact-waiting-request"),
    "enabled": InputDoc("Whether scheduled Agent regression testing is enabled.", True),
    "entities": InputDoc("Entity identifiers grouped by entity kind.", {"host": ["host-17"], "user": ["alice"]}),
    "event_id": InputDoc("Caller-stable SOC event identifier used for idempotent ingestion.", "soc-event-20260729-001"),
    "event_type": InputDoc("Closed SOC event type that describes the observed change.", "case.verdict_changed"),
    "evidence": InputDoc("Evidence points supporting the attribution.", ["停止后同一 session 的 active turn 已释放。"]),
    "expected_current_commit_sha": InputDoc("Current workspace HEAD used as an optimistic concurrency guard.", "a1b2c3d4e5f6"),
    "expected_sha256": InputDoc("SHA-256 returned by the preceding read; rejects stale replacement writes.", "7f83b1657ff1fc53b92dc18148a1d65dfa13514e"),
    "feedback_case_id": InputDoc("Existing first-class feedback case identifier.", "fbc-20260729-001"),
    "feedback_ref": InputDoc("Feedback reference to move into a new split improvement.", "feedback-20260729-001"),
    "force": InputDoc("Whether to use the audited forced-publication path.", False),
    "force_reason": InputDoc("Required audit reason when force is true.", "紧急修复已由值班负责人复核。"),
    "impact": InputDoc("Observed or expected impact.", "高：停止后的下一轮无法继续会话。"),
    "include_trace": InputDoc("Whether control mode emits complete semantic trace-event envelopes.", True),
    "input": InputDoc("Current prompt string or typed message items containing a user message.", "请核查当前告警并给出处置建议"),
    "instructions": InputDoc("Append-only control instruction; strict mode rejects this field.", "补充列出证据不足的判断。"),
    "labels": InputDoc("Analyst-defined labels used for filtering and triage.", ["session", "concurrency"]),
    "max_turns": InputDoc("Per-request Claude Code turn cap.", 8),
    "message": InputDoc("Non-blank user message or operator note for this action.", "请核查当前告警并给出处置建议"),
    "messages": InputDoc(
        "Non-empty text-only chat message sequence.",
        [{"role": "user", "content": "请总结这起告警的关键风险"}],
    ),
    "metadata": InputDoc("Caller-provided JSON metadata retained for correlation or observability.", {"source": "soc-console"}),
    "model": InputDoc("Optional per-request model override; never a business Agent handle.", "claude-sonnet-4-5"),
    "note": InputDoc("Optional operator note written to the governance audit trail.", "已核对候选差异与测试证据。"),
    "operator": InputDoc("Operator identity recorded in the governance audit trail.", "platform-operator"),
    "paths": InputDoc("Repository-relative paths whose uncommitted changes should be discarded.", [".mcp.json"]),
    "possible_object": InputDoc("Component or governance asset that may own the problem.", "session turn admission"),
    "possible_reason": InputDoc("Current hypothesis for the observed problem.", "停止路径未等待 session fence 释放。"),
    "previous_response_id": InputDoc("Prior AgentGov response whose owning conversation should be continued.", "resp_run-20260729-001"),
    "priority": InputDoc("Analyst-assigned triage priority.", "high"),
    "problem": InputDoc("One-sentence normalized problem statement.", "停止流式输出后再次发送消息发生会话冲突。"),
    "raw_text": InputDoc("Original feedback text retained as evidence.", "停止后再次发送消息时报 SESSION_CONFLICT。"),
    "reason": InputDoc("Optional audited reason for the requested operation.", "恢复到已验证的 workspace 版本。"),
    "requires_review": InputDoc("Whether the source must remain in the human-review queue.", True),
    "responsibility_boundary": InputDoc("Responsibility-boundary statements for the attribution.", ["Runtime owns session fencing."]),
    "role": InputDoc("Role of this text input message.", "user"),
    "run_id": InputDoc("Managed Agent run identifier used for correlation.", "run-20260729-001"),
    "scenario": InputDoc("Business scenario associated with this feedback.", "playground-stop-and-resend"),
    "sdk_raw": InputDoc("Whether control streaming emits wrapped raw SDK debugging facts.", True),
    "session_id": InputDoc("AgentGov session identifier used for continuation or correlation.", "sess-20260729"),
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
    "source_system": InputDoc("System that produced the SOC event.", "soc-console"),
    "source_type": InputDoc("Feedback signal source category.", "explicit_feedback"),
    "stage": InputDoc("Target improvement lifecycle stage.", "attribution"),
    "status": InputDoc("Target or filter status from the operation's documented closed enum.", "active"),
    "store": InputDoc("Whether the response remains publicly retrievable through the Responses retrieve endpoint.", False),
    "stream": InputDoc("Whether the endpoint streams its documented response protocol.", True),
    "suggestion": InputDoc("Suggested direction for resolving the normalized problem.", "停止接口等待 run 终态与 fence 释放。"),
    "summary": InputDoc("Human-editable summary for this governed artifact.", "停止后续聊需要统一释放 session fence。"),
    "system_append": InputDoc("Additional instruction appended to the governed Agent prompt.", "输出结论时列出关键证据。"),
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
    "type": InputDoc("Typed Responses input discriminator.", "message"),
    "user_quote": InputDoc("Original user wording supporting the normalized feedback.", "停止后再发消息就报会话冲突。"),
    "with_speech_summary": InputDoc(
        "Opt in to best-effort speech-summary events on the documented streaming surface.",
        True,
    ),
}


_FIELD_OVERRIDES: Mapping[tuple[str, str], InputDoc] = {
    ("AgentConfigFileUpdateRequest", "content"): InputDoc(
        "Complete UTF-8 replacement content, not a patch.",
        '{\n  "mcpServers": {}\n}\n',
    ),
    ("AgentConfigFileUpdateRequest", "session_id"): InputDoc(
        "Optional session whose SDK resume state must be invalidated after a successful config replacement.",
        "sess-20260729",
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
    ("OpenAIChatMessage", "content"): InputDoc(
        "Non-blank text content for this compatibility message.",
        "请总结这起告警的关键风险",
    ),
    ("ResponsesInputMessage", "content"): InputDoc(
        "Non-blank text or a non-empty array of typed input_text blocks.",
        [{"type": "input_text", "text": "请复核该告警的处置结论"}],
    ),
    ("ResponsesInputMessage", "type"): InputDoc("Discriminator for a message input item.", "message"),
    ("ResponsesInputText", "type"): InputDoc("Discriminator for a text input block.", "input_text"),
}


_PATH_PARAMETER_DOCS: Mapping[str, InputDoc] = {
    "agent_id": InputDoc("Registered business Agent identifier addressed by this operation.", "security-operations-expert"),
    "asset_id": InputDoc("Governed asset identifier addressed by this operation.", "asset-20260729-001"),
    "change_set_id": InputDoc("Agent change set identifier addressed by this operation.", "chg-20260729-001"),
    "conversation_id": InputDoc("AgentGov conversation projection identifier (conv_<session_id>).", "conv_sess-20260729"),
    "event_id": InputDoc("SOC event identifier addressed by this operation.", "soc-event-20260729-001"),
    "evidence_package_id": InputDoc("Evidence package identifier addressed by this operation.", "evp-20260729-001"),
    "feedback_case_id": InputDoc("First-class feedback case identifier addressed by this operation.", "fbc-20260729-001"),
    "feedback_id": InputDoc("Improvement feedback identifier addressed by this operation.", "feedback-20260729-001"),
    "file_name": InputDoc("Included evidence-package file name.", "manifest.json"),
    "improvement_id": InputDoc("Improvement item identifier addressed by this operation.", "imp-20260729-001"),
    "job_id": InputDoc("Historical Agent job identifier addressed by this read-only operation.", "job-20260729-001"),
    "pending_id": InputDoc("Pending-correlation identifier addressed by this operation.", "pending-20260729-001"),
    "release_id": InputDoc("Agent release identifier addressed by this operation.", "rel-20260729-001"),
    "request_id": InputDoc("Exact waiting Claude user-input request identifier.", "uir-20260729-001"),
    "response_id": InputDoc("AgentGov response projection identifier (resp_<run_id>).", "resp_run-20260729-001"),
    "run_id": InputDoc("Managed Agent run identifier addressed by this operation.", "run-20260729-001"),
    "session_id": InputDoc("AgentGov session identifier addressed by this deprecated native route.", "sess-20260729"),
    "signal_id": InputDoc("Feedback signal identifier addressed by this operation.", "signal-20260729-001"),
    "source_id": InputDoc("Identifier within the source_kind namespace.", "signal-20260729-001"),
    "source_kind": InputDoc("Feedback source namespace: signal, soc_event, or pending_correlation.", "signal"),
    "test_run_id": InputDoc("Platform Agent test-run identifier.", "test-run-20260729-001"),
    "test_session_id": InputDoc("Isolated Agent test-session identifier.", "test-session-20260729-001"),
    "trace_id": InputDoc("Langfuse trace identifier addressed by this debug operation.", "trace-20260729-001"),
}


_QUERY_PARAMETER_DOCS: Mapping[str, InputDoc] = {
    "after": InputDoc("Return conversation items after this msg_<index> cursor.", "msg_0"),
    "agent_id": InputDoc("Registered business Agent selector or ownership filter for this operation.", "security-operations-expert"),
    "alert_id": InputDoc("Filter records correlated with this SOC alert.", "alert-20260729-001"),
    "asset_type": InputDoc("Filter assets by the closed governed asset category.", "methodology"),
    "business_agent_id": InputDoc("Filter waiting input requests by registered business Agent.", "security-operations-expert"),
    "case_id": InputDoc("Filter records correlated with this SOC business case.", "case-20260729-001"),
    "change_set_id": InputDoc("Filter test runs by Agent change set.", "chg-20260729-001"),
    "commit_sha": InputDoc("Read or filter against this exact Agent repository commit.", "a1b2c3d4e5f6"),
    "cursor": InputDoc("Opaque pagination cursor returned by the preceding history page.", "cursor-20260729-001"),
    "event_mode": InputDoc("Chat SSE projection: raw legacy projection or semantic trace projection.", "semantic"),
    "event_type": InputDoc("Filter SOC events by the documented closed event-type enum.", "case.verdict_changed"),
    "include": InputDoc("OpenAI-shaped include selector; currently accepted as a no-op.", "items"),
    "include_host_mounts": InputDoc("Include host mount paths in operator diagnostics.", False),
    "include_messages": InputDoc("Include full SDK messages and reconstructed answer for explicit debug inspection.", True),
    "job_type": InputDoc("Filter historical Agent jobs by the documented closed job type.", "feedback_attribution"),
    "limit": InputDoc("Maximum number of records returned by this operation, within its documented bounds.", 100),
    "offset": InputDoc("Zero-based message offset used by the deprecated session route.", 0),
    "order": InputDoc("Conversation item order; only chronological asc is currently accepted.", "asc"),
    "path": InputDoc("Repository-relative file path interpreted by this operation.", ".mcp.json"),
    "q": InputDoc("Case-insensitive free-text search over the feedback-case title and source identifiers.", "会话冲突"),
    "run_id": InputDoc("Filter records by managed Agent run identifier.", "run-20260729-001"),
    "scope_id": InputDoc("Filter historical jobs by backend-owned scope identifier.", "fbc-20260729-001"),
    "scope_kind": InputDoc("Filter historical jobs by backend-owned scope category.", "feedback_case"),
    "session_id": InputDoc("Filter records by AgentGov session identifier.", "sess-20260729"),
    "source": InputDoc("Filter test-run history by trigger source.", "manual"),
    "source_improvement_id": InputDoc("Filter assets by their originating improvement item.", "imp-20260729-001"),
    "source_type": InputDoc("Filter feedback signals by the documented source-type enum.", "explicit_feedback"),
    "status": InputDoc("Filter records by the closed status enum documented for this operation.", "running"),
}


_QUERY_PARAMETER_OVERRIDES: Mapping[tuple[str, str, str], InputDoc] = {
    ("/api/claude-user-input-requests", "get", "status"): InputDoc(
        "Filter Claude user-input requests by waiting, resolved, or cancelled state.",
        "waiting",
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
        ".mcp.json",
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
    ("/v1/conversations/{conversation_id}/items", "get", "limit"): InputDoc(
        "Maximum number of chronological conversation items to return (1–100).",
        20,
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
            documentation = _FIELD_OVERRIDES.get((component_name, field_name)) or _FIELD_DOCS.get(field_name)
            if documentation is None:
                continue
            if not _meaningful(raw_property.get("description")):
                raw_property["description"] = documentation.description
            if not raw_property.get("examples") and "example" not in raw_property:
                raw_property["examples"] = [deepcopy(documentation.example)]

    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, MutableMapping):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, MutableMapping):
                continue
            _document_parameters(path, method, operation)
            _document_request_body(operation)

    _append_responses_request_guide(schema)


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


def _append_responses_request_guide(schema: OpenApiMutableMapping) -> None:
    paths = _mapping(schema.get("paths", {}))
    operation = _mapping(_mapping(paths.get(RESPONSES_PATH, {})).get("post", {}))
    components = _component_schemas(schema)
    root = _mapping(components.get("ResponsesRequest", {}))
    rows = _flatten_fields(root, components)
    if len(rows) != 22:
        return
    marker = "### Request-body field guide"
    existing = str(operation.get("description", "")).strip()
    if marker in existing:
        return
    table_lines = [
        marker,
        "",
        (
            "Swagger UI's **Parameters → No parameters** means this operation has no path, query, header, or "
            "cookie parameters. The JSON inputs below are under **Request body**. Supply the Bearer API key "
            "through **Authorize**."
        ),
        "",
        "| JSON path | Required | Type | Default | Example | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for path, required, field_schema in rows:
        table_lines.append(
            "| "
            + " | ".join(
                (
                    f"`{_escape_table(path)}`",
                    "yes" if required else "no",
                    _escape_table(_schema_type(field_schema)),
                    _escape_table(_render_value(field_schema.get("default"), absent="—")),
                    _escape_table(_render_example(field_schema)),
                    _escape_table(str(field_schema.get("description", ""))),
                )
            )
            + " |"
        )
    suffix = "\n".join(table_lines)
    operation["description"] = f"{existing}\n\n{suffix}" if existing else suffix


def _flatten_fields(
    root: OpenApiMapping,
    components: OpenApiMutableMapping,
) -> list[tuple[str, bool, OpenApiMapping]]:
    rows: list[tuple[str, bool, OpenApiMapping]] = []

    def walk(component: OpenApiMapping, prefix: str, stack: frozenset[str]) -> None:
        required = set(component.get("required", [])) if isinstance(component.get("required"), list) else set()
        properties = component.get("properties", {})
        if not isinstance(properties, Mapping):
            return
        for field_name, raw_field in properties.items():
            if not isinstance(field_name, str) or not isinstance(raw_field, Mapping):
                continue
            field_path = f"{prefix}.{field_name}" if prefix else field_name
            rows.append((field_path, field_name in required, raw_field))
            for reference, array_item in _nested_references(raw_field):
                if reference in stack:
                    continue
                nested = components.get(reference)
                if isinstance(nested, Mapping):
                    walk(nested, f"{field_path}[]" if array_item else field_path, stack | {reference})

    walk(root, "", frozenset({"ResponsesRequest"}))
    return rows


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


def _schema_type(fragment: OpenApiMapping) -> str:
    if isinstance(fragment.get("const"), str):
        return f"literal {fragment['const']}"
    if isinstance(fragment.get("enum"), list):
        return "enum"
    direct = fragment.get("type")
    if isinstance(direct, str):
        return direct
    for keyword in ("anyOf", "oneOf"):
        children = fragment.get(keyword)
        if isinstance(children, list):
            types = [_schema_type(child) for child in children if isinstance(child, Mapping)]
            return " | ".join(dict.fromkeys(types))
    if "$ref" in fragment:
        return str(fragment["$ref"]).rsplit("/", 1)[-1]
    return "object"


def _render_example(fragment: OpenApiMapping) -> str:
    examples = fragment.get("examples")
    if isinstance(examples, list) and examples:
        return _render_value(examples[0], absent="—")
    if "example" in fragment:
        return _render_value(fragment["example"], absent="—")
    return "—"


def _render_value(value: object, *, absent: str) -> str:
    if value is None:
        return absent
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _meaningful(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _component_schemas(schema: OpenApiMutableMapping) -> OpenApiMutableMapping:
    components = _mapping(schema.get("components", {}))
    return _mapping(components.get("schemas", {}))


def _mapping(value: object) -> OpenApiMutableMapping:
    return value if isinstance(value, MutableMapping) else {}
