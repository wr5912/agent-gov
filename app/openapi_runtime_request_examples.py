"""Runtime, OpenAI-shaped, and conversation request examples."""

from __future__ import annotations

from collections.abc import Mapping

from app.openapi_example_contracts import OperationKey, RequestExampleContract, example

_AGENT_ID = "security-operations-expert"

RUNTIME_REQUEST_EXAMPLE_CONTRACTS: Mapping[OperationKey, RequestExampleContract] = {
    ("/api/chat", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "default": example(
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
            "default": example(
                "Stream the deprecated native chat projection",
                {
                    "message": "请分析当前告警并持续输出调查过程",
                    "agent_id": _AGENT_ID,
                    "max_turns": 8,
                },
            ),
            "speech_summary": example(
                "Stream Chat events with best-effort speech summaries",
                {
                    "message": "请分析当前告警，并为可播报边界生成简短摘要",
                    "agent_id": _AGENT_ID,
                    "with_speech_summary": True,
                },
                description=(
                    "with_speech_summary defaults to false. When true, both raw and semantic event modes may emit "
                    "agentgov.speech_summary before done; generation is best-effort and no event is guaranteed."
                ),
            ),
        },
    ),
    ("/api/agent-runtime/sdk-events", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "managed_turn": example(
                "Run the managed-turn source-of-truth stream",
                {
                    "message": "请核查当前告警并给出处置建议",
                    "agent_id": _AGENT_ID,
                    "metadata": {"source": "soc-console"},
                },
            ),
            "speech_summary": example(
                "Stream SDK-native events with best-effort speech summaries",
                {
                    "message": "请核查当前告警，并为可播报边界生成简短摘要",
                    "agent_id": _AGENT_ID,
                    "with_speech_summary": True,
                },
                description=(
                    "The request remains an SDK-native managed stream. Eligible boundaries may add "
                    "agentgov.speech_summary events; generation failure is silent."
                ),
            ),
        },
    ),
    ("/api/debug/agent-runtime/raw-events", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "stream_raw_bytes": example(
                "Stream byte-exact Runtime output",
                {
                    "message": "请输出本轮 Runtime 原生事件",
                    "agent_id": _AGENT_ID,
                    "stream": True,
                },
            )
        },
    ),
    ("/v1/chat/completions", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "minimal_text_only": example(
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
            "agentgov_control_stream": example(
                "AgentGov control stream with every control switch",
                {
                    "input": "请核查当前告警并给出处置建议",
                    "model": "claude-sonnet-4-5",
                    "stream": True,
                    "store": True,
                    "metadata": {
                        "source": "soc-console",
                        "tenant": "north-region",
                    },
                    "agentgov": {
                        "agent_id": _AGENT_ID,
                        "alert_id": "alert-20260729-001",
                        "case_id": "case-20260729-001",
                        "max_turns": 8,
                        "include_trace": True,
                        "with_speech_summary": True,
                        "debug": {"sdk_raw": True},
                    },
                },
                description=(
                    "with_speech_summary=true requires the top-level stream=true and may emit best-effort "
                    "agentgov.speech_summary events. debug.sdk_raw and trace events remain control-only."
                ),
            ),
            "agentgov_control_structured": example(
                "Control mode with both structured content forms",
                {
                    "model": "claude-sonnet-4-5",
                    "input": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": "仅使用当前请求提供的告警证据。",
                        },
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "请复核该告警的处置结论",
                                }
                            ],
                        },
                    ],
                    "instructions": "补充说明证据不足的判断，不替换业务 Agent 的受治理指令。",
                    "agentgov": {"agent_id": _AGENT_ID},
                },
            ),
            "strict_openai": example(
                "Strict OpenAI-shaped transitional request",
                {
                    "input": "请概括当前任务的处理结果",
                    "stream": False,
                    "store": False,
                    "model": "claude-sonnet-4-5",
                    "metadata": {"source": "openai-compatible-client"},
                },
                description="No agentgov extension: the operator-selected business Agent is used and control-only fields are rejected.",
            ),
            "continue_with_conversation": example(
                "Continue one AgentGov conversation",
                {
                    "input": "继续核查上一轮尚未确认的证据",
                    "conversation": "conv_sess-20260729",
                    "agentgov": {"agent_id": _AGENT_ID},
                },
                description=(
                    "Recommended continuation form when the conversation id is known. Do not also send "
                    "previous_response_id unless compatibility with AgentGov's documented deviation is required."
                ),
            ),
            "continue_with_previous_response_id": example(
                "Continue from one previous AgentGov response",
                {
                    "input": "基于上一轮结果补充处置步骤",
                    "previous_response_id": "resp_run-20260729-001",
                    "agentgov": {"agent_id": _AGENT_ID},
                },
                description=(
                    "Recommended continuation form when only the prior response id is known. The server resolves "
                    "its owning conversation and rejects missing or non-resumable mappings."
                ),
            ),
        },
    ),
    ("/v1/conversations", "post"): RequestExampleContract(
        media_type="application/json",
        examples={
            "empty_conversation": example(
                "Create a conversation without client metadata",
                {},
            ),
            "with_metadata": example(
                "Create a conversation with observability metadata",
                {"metadata": {"source": "soc-console", "tenant": "north-region"}},
            ),
        },
    ),
    ("/api/settings/openai-compat-agent", "put"): RequestExampleContract(
        media_type="application/json",
        operation_description=(
            "Select the registered business Agent used by strict /v1 Responses and the deprecated Chat Completions "
            "shim. Obtain agent_id from GET /api/agent-registry."
        ),
        examples={
            "select_registered_agent": example(
                "Select the built-in business Agent",
                {"agent_id": _AGENT_ID},
            )
        },
    ),
}
