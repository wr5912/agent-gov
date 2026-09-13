"""Langfuse 只读查询客户端的不联网边界；成功查询由真实容器 smoke 覆盖。"""

from __future__ import annotations

import json

import pytest
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient, _to_plain, project_validation_trace
from app.runtime.settings import AppSettings
from app.runtime_gateway.contracts import AgentRunTraceResponse
from pydantic import ValidationError


@pytest.mark.parametrize(
    "config",
    [
        {"LANGFUSE_ENABLED": False},
        {"LANGFUSE_PUBLIC_KEY": ""},
        {"LANGFUSE_SECRET_KEY": ""},
    ],
)
def test_unconfigured_query_returns_no_trace(config: dict[str, object]) -> None:
    settings = AppSettings(
        _env_file=None,
        **(
            {
                "LANGFUSE_ENABLED": True,
                "LANGFUSE_PUBLIC_KEY": "test-public",
                "LANGFUSE_SECRET_KEY": "test-secret",
            }
            | config
        ),
    )

    assert RuntimeLangfuseClient(settings).fetch_trace("test-trace") is None


def test_query_connection_failure_returns_only_sanitized_error_type() -> None:
    settings = AppSettings(
        _env_file=None,
        LANGFUSE_ENABLED=True,
        LANGFUSE_BASE_URL="http://127.0.0.1:1",
        LANGFUSE_PUBLIC_KEY="test-public",
        LANGFUSE_SECRET_KEY="test-secret",
    )

    result = RuntimeLangfuseClient(settings).fetch_trace("test-trace")

    assert result is not None
    assert result.get("fetch_status") == "failed"
    assert set(result) == {"fetch_status", "error_type"}
    assert "test-secret" not in str(result)


def test_to_plain_converts_nested_json_safe_values() -> None:
    assert _to_plain({"observations": ({"id": "obs-1", "metrics": {"latency": 1.25}},)}) == {"observations": [{"id": "obs-1", "metrics": {"latency": 1.25}}]}


def test_validation_projection_keeps_only_approved_semantics_and_drops_hostile_content() -> None:
    canaries = (
        "RAW_PROMPT_CANARY",
        "RAW_OUTPUT_CANARY",
        "TOOL_ARGUMENT_CANARY",
        "EVENT_BODY_CANARY",
        "UNKNOWN_METADATA_CANARY",
        "URL_QUERY_CANARY",
    )
    projected = project_validation_trace(
        {
            "id": "trace-1",
            "name": "agentgov.run",
            "url": f"https://langfuse.invalid/project/agent-gov/traces/trace-1?token={canaries[5]}",
            "input": {"prompt": canaries[0]},
            "output": {"answer": canaries[1]},
            "metadata": {
                "Authorization": canaries[4],
                "attributes": {
                    "agentgov.run.id": "run-1",
                    "gen_ai.input.messages": canaries[0],
                },
            },
            "observations": [
                {
                    "id": "observation-1",
                    "name": "execute_tool",
                    "type": "SPAN",
                    "traceId": "trace-1",
                    "parentObservationId": None,
                    "endTime": "2026-09-11T00:00:00Z",
                    "input": {"arguments": canaries[2]},
                    "output": {"result": canaries[1]},
                    "body": canaries[3],
                    "events": [{"body": canaries[3]}],
                    "attributes": {
                        "gen_ai.tool.call.id": "tool-call-1",
                        "gen_ai.tool.call.arguments": canaries[2],
                    },
                    "metadata": {
                        "Authorization": canaries[4],
                        "attributes": {
                            "gen_ai.request.model": "model-a",
                            "gen_ai.output.messages": canaries[1],
                        },
                        "resourceAttributes": {"service.secret": canaries[4]},
                    },
                }
            ],
        }
    )

    serialized = json.dumps(projected, ensure_ascii=False)
    assert all(canary not in serialized for canary in canaries)
    assert projected["url"] == "/project/agent-gov/traces/trace-1"
    assert projected["metadata"] == {"attributes": {"agentgov.run.id": "run-1"}}
    assert projected["observations"] == [
        {
            "id": "observation-1",
            "traceId": "trace-1",
            "parentObservationId": None,
            "name": "execute_tool",
            "type": "SPAN",
            "endTime": "2026-09-11T00:00:00Z",
            "attributes": {"gen_ai.tool.call.id": "tool-call-1"},
            "metadata": {"attributes": {"gen_ai.request.model": "model-a"}},
        }
    ]


def test_public_run_trace_contract_has_no_langfuse_payload_field() -> None:
    properties = AgentRunTraceResponse.model_json_schema()["properties"]

    assert set(properties) == {"run_id", "trace_id", "trace_url", "trace_status"}
    with pytest.raises(ValidationError):
        AgentRunTraceResponse.model_validate(
            {
                "run_id": "run-1",
                "trace_id": "1" * 32,
                "trace_url": "/project/agent-gov/traces/trace-1",
                "trace_status": "complete",
                "trace": {"input": "PUBLIC_API_RAW_INPUT_CANARY"},
            }
        )
