from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict

OperationKey = tuple[str, str]


class OpenApiExample(TypedDict):
    summary: str
    value: object
    description: NotRequired[str]


@dataclass(frozen=True)
class RequestExampleContract:
    media_type: str
    examples: Mapping[str, OpenApiExample]
    operation_description: str | None = None


def _example(summary: str, value: object, *, description: str | None = None) -> OpenApiExample:
    item: OpenApiExample = {"summary": summary, "value": value}
    if description:
        item["description"] = description
    return item


_AGENT_ID = "security-operations-expert"
_OPERATOR = "platform-operator"


REQUEST_EXAMPLE_CONTRACTS: Mapping[OperationKey, RequestExampleContract] = {
    ("/api/chat", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "default": _example(
                "Run the deprecated native chat projection",
                {
                    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
                    "agent_id": _AGENT_ID,
                    "max_turns": 8,
                },
            )
        },
    ),
    ("/api/chat/stream", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "default": _example(
                "Stream the deprecated native chat projection",
                {
                    "message": "请分析当前告警并持续输出调查过程",
                    "agent_id": _AGENT_ID,
                    "max_turns": 8,
                },
            )
        },
    ),
    ("/api/agent-runtime/sdk-events", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "managed_turn": _example(
                "Run the managed-turn source-of-truth stream",
                {
                    "message": "请核查当前告警并给出处置建议",
                    "agent_id": _AGENT_ID,
                    "metadata": {"source": "soc-console"},
                },
            )
        },
    ),
    ("/api/debug/agent-runtime/raw-events", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "stream_raw_bytes": _example(
                "Stream byte-exact Runtime output",
                {
                    "message": "请输出本轮 Runtime 原生事件",
                    "agent_id": _AGENT_ID,
                    "stream": True,
                },
            )
        },
    ),
    (
        "/v1/agentgov/confirmation-requests/{request_id}/decision",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        examples={
            "deny": _example(
                "Deny the pending tool request",
                {
                    "action": "deny",
                    "decision_token": "token-from-exact-waiting-request",
                    "message": "当前上下文不足，拒绝执行该工具。",
                },
                description=(
                    "Use request_id and decision_token from the exact run_id + status=waiting query. The token is one-time and must never be persisted."
                ),
            ),
            "allow_once": _example(
                "Allow this tool request once",
                {
                    "action": "allow_once",
                    "decision_token": "token-from-exact-waiting-request",
                },
            ),
            "allow_for_run": _example(
                "Allow the low-risk category for this run",
                {
                    "action": "allow_for_run",
                    "decision_token": "token-from-exact-waiting-request",
                },
            ),
            "answer_question": _example(
                "Answer an AskUserQuestion request",
                {
                    "action": "answer_question",
                    "decision_token": "token-from-exact-waiting-request",
                    "answer": {"response": "只处理当前告警资产"},
                },
            ),
        },
    ),
    ("/api/agent-config-file", "put"): RequestExampleContract(
        media_type="application/json",
        operation_description="Replace the selected editable UTF-8 config file. Read the file first and pass its sha256 as expected_sha256 to reject stale concurrent edits; content is the complete replacement, not a patch.",
        examples={
            "replace_mcp_config": _example(
                "Replace the current editable config",
                {
                    "content": '{\n  "mcpServers": {}\n}\n',
                    "expected_sha256": "sha256-from-get-agent-config-file",
                },
            )
        },
    ),
    ("/v1/chat/completions", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "minimal_text_only": _example(
                "Run the deprecated non-streaming text shim",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "请总结这起告警的关键风险",
                        }
                    ],
                    "stream": False,
                },
            )
        },
    ),
    ("/v1/responses", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "agentgov_control_stream": _example(
                "AgentGov control mode with Responses-style SSE",
                {
                    "input": "请核查当前告警并给出处置建议",
                    "stream": True,
                    "agentgov": {
                        "agent_id": _AGENT_ID,
                        "include_trace": True,
                    },
                },
            ),
            "agentgov_control_structured": _example(
                "AgentGov control mode with structured input",
                {
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "请复核该告警的处置结论",
                                }
                            ],
                        }
                    ],
                    "instructions": "补充说明证据不足的判断，不替换业务 Agent 的受治理指令。",
                    "agentgov": {"agent_id": _AGENT_ID},
                },
            ),
            "strict_openai": _example(
                "Strict OpenAI-shaped transitional request",
                {
                    "input": "请概括当前任务的处理结果",
                    "stream": False,
                },
            ),
        },
    ),
    ("/v1/conversations", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "empty_conversation": _example(
                "Create a conversation without client metadata",
                {},
            ),
            "with_metadata": _example(
                "Create a conversation with observability metadata",
                {"metadata": {"source": "soc-console", "tenant": "north-region"}},
            ),
        },
    ),
    ("/api/settings/openai-compat-agent", "put"): RequestExampleContract(
        media_type="application/json",
        operation_description="Select the registered business Agent used by strict /v1 Responses and the deprecated Chat Completions shim. Obtain agent_id from GET /api/agent-registry.",
        examples={
            "select_registered_agent": _example(
                "Select the built-in business Agent",
                {"agent_id": _AGENT_ID},
            )
        },
    ),
    ("/api/agent-repository/discard-changes", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Discard only the listed dirty workspace paths for the selected business Agent. Read repository status first; an empty paths list is a no-op and the operation never means an implicit whole-workspace discard.",
        examples={
            "discard_one_file": _example(
                "Discard one confirmed workspace file",
                {"paths": [".mcp.json"]},
            )
        },
    ),
    ("/api/agent-repository/snapshot", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Commit the selected business Agent's current dirty workspace as a version snapshot. The operation records operator and note for audit and has no effect when the workspace has no changes.",
        examples={
            "manual_snapshot": _example(
                "Save a reviewed workspace snapshot",
                {
                    "operator": _OPERATOR,
                    "note": "保存已复核的 MCP 配置调整。",
                },
            )
        },
    ),
    ("/api/agent-change-sets", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create an isolated change-set worktree for the selected business Agent. Omit base_commit_sha to use the current published commit; a supplied SHA must come from the Agent repository API.",
        examples={
            "current_published_base": _example(
                "Create from the current published commit",
                {
                    "title": "收口告警研判说明",
                    "note": "为本轮改进建立隔离候选版本。",
                },
            ),
            "explicit_base": _example(
                "Create from a reviewed repository commit",
                {
                    "base_commit_sha": "commit-sha-from-agent-repository",
                    "title": "基于指定版本修订",
                },
            ),
        },
    ),
    ("/api/agent-change-sets/{change_set_id}/approve", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Approve the identified change set after reviewing its diff and test evidence. Obtain change_set_id from GET /api/agent-change-sets; approval changes lifecycle state but does not publish.",
        examples={
            "approve_reviewed_change_set": _example(
                "Approve a reviewed change set",
                {
                    "operator": _OPERATOR,
                    "note": "Diff 与测试证据均已复核。",
                },
            )
        },
    ),
    ("/api/agent-change-sets/{change_set_id}/reject", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Reject the identified change set and record the reason. Obtain change_set_id from the change-set list; the state transition is terminal for the rejected candidate.",
        examples={
            "reject_change_set": _example(
                "Reject a change set with an audit reason",
                {
                    "operator": _OPERATOR,
                    "note": "回归证据不足，退回重新整改。",
                },
            )
        },
    ),
    ("/api/agent-change-sets/{change_set_id}/abandon", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Abandon an open change set that is no longer needed. The change_set_id must come from the change-set list and the operation records a terminal governance decision.",
        examples={
            "abandon_obsolete_change_set": _example(
                "Abandon an obsolete candidate",
                {
                    "operator": _OPERATOR,
                    "note": "需求已撤销，不再发布该候选。",
                },
            )
        },
    ),
    ("/api/agent-change-sets/{change_set_id}/publish", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Publish an approved change set for its owning business Agent. Normal publication requires the current candidate test gate to pass; force is restricted to eligible manual change sets and requires a reason.",
        examples={
            "normal_publish": _example(
                "Publish through the normal tested path",
                {
                    "operator": _OPERATOR,
                    "tag_name": "release-candidate-2026-07-29",
                    "note": "发布已批准且测试通过的候选。",
                },
            ),
            "force_publish": _example(
                "Force-publish an eligible manual candidate",
                {
                    "operator": _OPERATOR,
                    "force": True,
                    "force_reason": "紧急修复已完成人工复核，接受当前已记录的非反馈测试阻塞项。",
                },
                description="Never use force for feedback-linked candidates or incomplete provenance.",
            ),
        },
    ),
    (
        "/api/agent-change-sets/{change_set_id}/worktree-cleanup/retry",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Retry persisted worktree cleanup for a terminal change set after inspecting its cleanup error. The operation does not change the published Agent version.",
        examples={
            "retry_cleanup": _example(
                "Retry terminal worktree cleanup",
                {
                    "operator": _OPERATOR,
                    "note": "存储故障已恢复，重试清理隔离 worktree。",
                },
            )
        },
    ),
    ("/api/agent-releases/{release_id}/restore", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Restore a historical release tree as a new auditable commit. Obtain release_id from the release list; the historical release record remains unchanged.",
        examples={
            "restore_release_tree": _example(
                "Restore a reviewed historical release",
                {
                    "operator": _OPERATOR,
                    "note": "恢复该版本的 workspace tree 形成新提交。",
                },
            )
        },
    ),
    ("/api/agent-releases/{release_id}/rollback", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Roll the owning business Agent back to the selected published or archived release. Obtain release_id from GET /api/agent-releases and review current run impact before executing.",
        examples={
            "rollback_release": _example(
                "Rollback to a reviewed release",
                {
                    "operator": _OPERATOR,
                    "note": "当前版本出现回归，回滚到最近稳定版本。",
                },
            )
        },
    ),
    ("/api/agent-registry/{agent_id}/lifecycle", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Transition the registered business Agent to a valid target lifecycle state. The URL agent_id comes from GET /api/agent-registry; valid values can still be rejected when the current transition is illegal.",
        examples={
            "enter_evaluation": _example(
                "Move an active Agent into evaluation",
                {"status": "evaluating"},
            )
        },
    ),
    ("/api/agent-registry/{agent_id}/workspace/import", "post"): RequestExampleContract(
        media_type="multipart/form-data",
        operation_description="Upload one exact workspace.tar.gz package. A new Agent requires name; replacing an existing Agent requires expected_current_commit_sha from GET /api/agent-registry and is rejected while its workspace or governance state is busy.",
        examples={
            "create_agent": _example(
                "Create a business Agent from a workspace package",
                {
                    "package": "workspace.tar.gz",
                    "name": "SOC 值班助手",
                },
            ),
            "replace_existing_agent": _example(
                "Replace an existing Agent with optimistic concurrency",
                {
                    "package": "workspace.tar.gz",
                    "expected_current_commit_sha": "current-commit-sha-from-agent-registry",
                    "reason": "应用已完成离线回归的 Workspace 更新。",
                },
            ),
        },
    ),
    ("/api/agent-registry/{agent_id}/workspace/restore", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Restore a historical workspace tree as a new commit without rewriting history. Both SHAs must come from the Agent's repository history; expected_current_commit_sha protects against concurrent changes.",
        examples={
            "restore_historical_tree": _example(
                "Restore a historical tree with a concurrency guard",
                {
                    "target_commit_sha": "historical-commit-sha-from-agent-history",
                    "expected_current_commit_sha": "current-commit-sha-from-agent-registry",
                    "reason": "恢复已验证的历史 Workspace 内容。",
                },
            )
        },
    ),
    ("/api/agent-registry/{agent_id}/test-schedule", "put"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create or replace the business Agent's test schedule. cron_expression is a five-field cron expression, timezone is an IANA timezone, and saving the schedule does not trigger an immediate run.",
        examples={
            "nightly_schedule": _example(
                "Run the full workspace test suite every night",
                {
                    "enabled": True,
                    "cron_expression": "0 2 * * *",
                    "timezone": "Asia/Shanghai",
                },
            )
        },
    ),
    ("/api/agent-test-runs", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create a durable full-suite test run for one business Agent. Omit commit_sha to pin the current active commit at request time, or pass a commit returned by the Agent test/repository APIs.",
        examples={
            "current_active_commit": _example(
                "Test the current active commit",
                {"agent_id": _AGENT_ID},
            ),
            "specific_commit": _example(
                "Test a specific Agent commit",
                {
                    "agent_id": _AGENT_ID,
                    "commit_sha": "commit-sha-from-agent-test-assets",
                },
            ),
        },
    ),
    ("/api/agent-test-sessions", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create a temporary interactive test session pinned to one Agent commit. Use change_set_id only when testing that Agent's candidate; IDs and SHAs must come from the corresponding governance APIs.",
        examples={
            "active_agent": _example(
                "Create a test session for the active Agent",
                {"agent_id": _AGENT_ID},
            ),
            "change_set_candidate": _example(
                "Create a test session for a change-set candidate",
                {
                    "agent_id": _AGENT_ID,
                    "change_set_id": "change-set-id-from-list",
                },
            ),
        },
    ),
    (
        "/api/agent-test-sessions/{test_session_id}/messages",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Send one non-empty test message to the temporary session returned by POST /api/agent-test-sessions. Metadata is observability context and does not change Agent routing.",
        examples={
            "regression_prompt": _example(
                "Exercise one regression scenario",
                {
                    "message": "请复核该告警并说明证据不足时的处理边界",
                    "metadata": {"scenario": "alert-triage-regression"},
                },
            )
        },
    ),
    ("/api/improvements", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create an ImprovementItem owned by a registered business Agent. source_feedback_refs must refer to existing feedback records; auto_merge may merge them into a similar open item instead of creating one.",
        examples={
            "from_feedback": _example(
                "Create an improvement from reviewed feedback",
                {
                    "agent_id": _AGENT_ID,
                    "title": "补强证据不足时的回答边界",
                    "summary": "当前回答在证据不足时仍给出确定性结论。",
                    "source_feedback_refs": ["feedback-id-from-improvement-workbench"],
                    "auto_merge": False,
                },
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/lifecycle",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Return an ImprovementItem to a valid earlier refinement stage. Obtain improvement_id from the improvement list; this endpoint cannot bypass artifacts to advance the workflow.",
        examples={
            "return_to_intake": _example(
                "Return the item for feedback refinement",
                {"stage": "feedback_intake"},
            )
        },
    ),
    ("/api/improvements/{improvement_id}/merge", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Merge another open ImprovementItem owned by the same Agent into the URL target. The source item is archived; obtain both IDs from GET /api/improvements and review their feedback references first.",
        examples={
            "merge_duplicate_item": _example(
                "Merge a duplicate source item",
                {"source_improvement_id": "source-improvement-id-from-list"},
            )
        },
    ),
    ("/api/improvements/{improvement_id}/split", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Move one source feedback reference out of the URL ImprovementItem into a new item. feedback_ref must already belong to the source item and is returned by the improvement detail API.",
        examples={
            "split_feedback": _example(
                "Split one unrelated feedback reference",
                {"feedback_ref": "feedback-ref-from-improvement-detail"},
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/feedbacks",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Attach one generic feedback record while the ImprovementItem is in feedback_intake. FeedbackCase records use the dedicated attach-feedback-case endpoint instead.",
        examples={
            "playground_feedback": _example(
                "Attach feedback from a Playground run",
                {
                    "summary": "回答遗漏了告警时间线中的关键证据。",
                    "source": "playground_run",
                    "raw_text": "处置建议没有引用触发该结论的事件。",
                    "run_id": "run-id-from-agent-run",
                },
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/normalized-feedback",
        "put",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Create or replace the system-understanding artifact for the ImprovementItem. This writes content only; confirmation and lifecycle progression use their dedicated operations.",
        examples={
            "system_understanding": _example(
                "Save a normalized problem statement",
                {
                    "problem": "Agent 在证据不足时输出了确定性结论。",
                    "possible_reason": "回答约束未要求显式标记证据缺口。",
                    "impact": "高",
                    "suggestion": "补充证据不足时的保守回答规则。",
                    "user_quote": "为什么没有说明这个结论缺少证据？",
                },
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/attribution",
        "put",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Create or replace the attribution artifact for the ImprovementItem. The request owns business content only; backend lifecycle and provenance fields are injected by the service.",
        examples={
            "attribution": _example(
                "Save attribution evidence",
                {
                    "summary": "主要责任在回答边界规则缺失，而非工具数据缺失。",
                    "responsibility_boundary": [
                        "业务 Agent 负责对证据充分性进行判断",
                        "上层系统负责提供完整告警上下文",
                    ],
                    "evidence": [
                        "Trace 中已包含告警时间线",
                        "最终回答未引用或质疑该时间线",
                    ],
                },
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/optimization-plan",
        "put",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Create or replace the optimization-plan artifact. Each change names a governed target and a concrete business change; applying workspace changes remains a separate confirmed action.",
        examples={
            "optimization_plan": _example(
                "Save a concrete governed optimization plan",
                {
                    "summary": "在回答规则中增加证据不足处理并补回归测试。",
                    "changes": [
                        {
                            "target": "CLAUDE.md",
                            "change": "要求不充分证据只能输出假设并列出待补证据。",
                        },
                        {
                            "target": "tests",
                            "change": "新增证据不足场景的保守回答回归用例。",
                        },
                    ],
                },
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/attach-feedback-case",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Attach one existing FeedbackCase to an ImprovementItem and register its feedback reference. Obtain feedback_case_id from GET /api/feedback-cases; ownership must be compatible with the target Agent.",
        examples={
            "attach_case": _example(
                "Attach a reviewed FeedbackCase",
                {"feedback_case_id": "feedback-case-id-from-list"},
            )
        },
    ),
    (
        "/api/improvements/{improvement_id}/feedbacks/{feedback_id}/reassign",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Move the identified feedback record to another ImprovementItem owned by the same Agent. The target ID comes from GET /api/improvements and both items must be in a state that permits feedback changes.",
        examples={
            "move_feedback": _example(
                "Move feedback to the correct improvement",
                {"target_improvement_id": "target-improvement-id-from-list"},
            )
        },
    ),
    ("/api/assets", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create a methodology, execution, or audit governance asset owned by a registered business Agent. Workspace tests are not generic assets and must remain in the Agent's Git-backed tests directory.",
        examples={
            "methodology_asset": _example(
                "Create a reusable methodology asset",
                {
                    "agent_id": _AGENT_ID,
                    "asset_type": "methodology",
                    "title": "证据不足时的保守回答方法",
                    "body": "先列证据缺口，再区分事实、推断和待验证项。",
                    "source_improvement_id": "improvement-id-from-list",
                },
            )
        },
    ),
    ("/api/assets/{asset_id}/inherit", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create a derived copy of the URL asset for another registered business Agent. Obtain asset_id from the asset list; tests and private workspace configuration are outside this inheritance contract.",
        examples={
            "inherit_methodology": _example(
                "Inherit an asset into another Agent",
                {"target_agent_id": "target-agent-id-from-registry"},
            )
        },
    ),
    ("/api/feedback-cases", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Create one FeedbackCase from typed feedback sources owned by the same business Agent. Source IDs must come from feedback-signal, SOC-event, or resolved pending-correlation APIs; backend correlation fields are projected from those sources.",
        examples={
            "from_feedback_signal": _example(
                "Create a case from one reviewed signal",
                {
                    "source_refs": [
                        {
                            "source_kind": "signal",
                            "source_id": "feedback-signal-id-from-list",
                        }
                    ],
                    "title": "告警处置建议缺少证据引用",
                    "priority": "medium",
                },
            )
        },
    ),
    ("/api/feedback-signals", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Collect one feedback signal without running attribution or proposal generation. Omit signal_id and timestamp to let the backend assign them; provide only correlation IDs actually known by the caller.",
        examples={
            "explicit_feedback_for_run": _example(
                "Collect explicit feedback for a known run",
                {
                    "source_type": "explicit_feedback",
                    "run_id": "run-id-from-agent-run",
                    "labels": ["evidence-gap"],
                    "comment": "处置建议没有引用支撑结论的事件。",
                    "confidence": "high",
                    "requires_review": True,
                    "metadata": {"source": "soc-console"},
                },
            ),
            "analyst_annotation": _example(
                "Collect an analyst annotation",
                {
                    "source_type": "analyst_annotation",
                    "alert_id": "alert-id-from-business-system",
                    "labels": ["analyst-review"],
                    "comment": "该告警属于已知维护活动。",
                    "confidence": "medium",
                },
            ),
        },
    ),
    (
        "/api/feedback-signals/{signal_id}/reassign-agent",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Correct the owning business Agent of one feedback signal and persist an audit record. Obtain signal_id from the feedback-signal list and agent_id from GET /api/agent-registry.",
        examples={
            "correct_agent_ownership": _example(
                "Reassign a signal with an audit reason",
                {
                    "agent_id": _AGENT_ID,
                    "operator": _OPERATOR,
                    "reason": "原始自动关联选择了错误的业务 Agent。",
                },
            )
        },
    ),
    ("/api/soc-events", "post"): RequestExampleContract(
        media_type="application/json",
        operation_description="Collect one immutable SOC event and attempt deterministic correlation to an Agent run. event_id must be unique in the source system; before/after and entities should contain domain fields, not opaque dumps.",
        examples={
            "case_verdict_changed": _example(
                "Collect a case verdict change",
                {
                    "event_id": "soc-event-20260729-001",
                    "source_system": "soc-console",
                    "event_type": "case.verdict_changed",
                    "timestamp": "2026-07-29T10:30:00Z",
                    "case_id": "case-id-from-business-system",
                    "actor_id": "analyst-42",
                    "before": {"verdict": "suspicious"},
                    "after": {"verdict": "benign", "reason": "confirmed maintenance"},
                    "entities": {"host": ["server-17"], "user": ["svc-backup"]},
                    "confidence": "high",
                    "requires_review": False,
                    "metadata": {"source_region": "north"},
                },
            )
        },
    ),
    (
        "/api/pending-correlations/{pending_id}/resolve",
        "post",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Resolve one pending correlation by supplying only confirmed business identifiers. Obtain pending_id from the pending-correlation list; omitted fields remain unknown rather than receiving placeholder data.",
        examples={
            "resolve_to_run": _example(
                "Resolve the source to a known Agent run",
                {
                    "run_id": "run-id-from-agent-run",
                    "comment": "已通过业务时间线确认该事件对应此运行。",
                },
            )
        },
    ),
    (
        "/api/feedback-sources/{source_kind}/{source_id}",
        "patch",
    ): RequestExampleContract(
        media_type="application/json",
        operation_description="Patch developer-owned annotations for one canonical feedback source. Omitted properties are unchanged; source_kind is signal, soc_event, or pending_correlation and source_id comes from the matching list API.",
        examples={
            "add_comment": _example(
                "Add one reviewer comment",
                {"comment": "已复核原始证据，保留该反馈。"},
            ),
            "mark_for_review": _example(
                "Mark one source for manual review",
                {
                    "requires_review": True,
                    "labels": ["needs-domain-review"],
                },
            ),
            "resolve_annotation": _example(
                "Resolve the developer annotation",
                {"status": "resolved"},
            ),
        },
    ),
}
