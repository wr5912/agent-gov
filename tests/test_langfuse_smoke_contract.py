"""Langfuse smoke 的纯语义契约；连接、队列与轮询只在真实容器入口执行。"""

import subprocess
from collections.abc import Sequence
from typing import Any

import pytest
from scripts import langfuse_smoke
from scripts.langfuse_smoke import runtime_trace_observation_errors


def test_smoke_resolves_published_port_defaults_and_explicit_urls() -> None:
    assert langfuse_smoke.resolve_langfuse_url({}) == "http://localhost:50402"
    assert langfuse_smoke.resolve_langfuse_url({"LANGFUSE_HOST_PORT": "50499"}) == "http://localhost:50499"
    assert (
        langfuse_smoke.resolve_langfuse_url(
            {"LANGFUSE_NEXTAUTH_URL": "https://trace.example.test", "LANGFUSE_HOST_PORT": "50499"},
        )
        == "https://trace.example.test"
    )


def test_redis_command_failure_cannot_be_projected_as_empty_output() -> None:
    failed = subprocess.CompletedProcess(
        args=["docker", "exec", "langfuse-redis", "redis-cli", "type", "queue"],
        returncode=23,
        stdout="",
        stderr="connection failed",
    )

    with pytest.raises(langfuse_smoke.RedisCommandError, match="type command failed with exit code 23"):
        langfuse_smoke.require_redis_command_output(failed, "type")


def test_redis_auth_is_delivered_on_stdin_not_host_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = list(command)
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="list\n", stderr="")

    monkeypatch.setattr(langfuse_smoke.subprocess, "run", capture)
    secret = "private-redis-password"

    assert langfuse_smoke.redis("langfuse-redis", secret, "type", "queue") == "list"
    assert secret not in "\0".join(captured["command"])
    assert "--env" not in captured["command"]
    assert captured["input"] == secret


def test_stopped_or_uninspectable_redis_container_is_an_acceptance_error() -> None:
    assert langfuse_smoke.redis_container_status_error("agent-gov-langfuse-redis", running=True) is None
    assert (
        langfuse_smoke.redis_container_status_error(
            "agent-gov-langfuse-redis",
            running=False,
        )
        == "Langfuse Redis queue check failed: container agent-gov-langfuse-redis is not running"
    )


def test_only_missing_redis_key_counts_as_zero() -> None:
    assert langfuse_smoke.parse_redis_queue_count("none", "") == 0
    with pytest.raises(langfuse_smoke.RedisCommandError, match="cardinality was not returned"):
        langfuse_smoke.parse_redis_queue_count("list", "")
    with pytest.raises(langfuse_smoke.RedisCommandError, match="unexpected key type"):
        langfuse_smoke.parse_redis_queue_count("stream", "0")


def test_agentscope_trace_accepts_agent_model_and_tool_semantics() -> None:
    trace_name = "agentgov.run"
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="0123456789abcdef0123456789abcdef",
        trace_name=trace_name,
        names={trace_name, "agentgov.run.stage", "invoke_agent", "chat", "execute_tool"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert errors == []


def test_agentscope_trace_rejects_non_run_root_and_missing_semantic_spans() -> None:
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="0123456789abcdef0123456789abcdef",
        trace_name="chat",
        names={"chat"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert any("root is not the AgentGov run span" in error for error in errors)
    assert any("does not include the redacted AgentScope agent observation" in error for error in errors)


def test_agentscope_trace_rejects_langfuse_identity_rebinding() -> None:
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="fedcba9876543210fedcba9876543210",
        trace_name="agentgov.run",
        names={"agentgov.run", "agentgov.run.stage", "invoke_agent", "chat"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert errors == [
        "trace 0123456789abcdef0123456789abcdef query returned a different trace identity",
    ]


def test_agentscope_trace_rejects_error_observations() -> None:
    trace_name = "agentgov.run"
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="0123456789abcdef0123456789abcdef",
        trace_name=trace_name,
        names={trace_name, "agentgov.run.stage", "invoke_agent", "chat", "execute_tool"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
        error_observation_names={"execute_tool"},
    )

    assert errors == [
        "trace 0123456789abcdef0123456789abcdef includes error observations: execute_tool",
    ]


def test_agentscope_trace_requires_correlation_attributes_on_run_observation() -> None:
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="0123456789abcdef0123456789abcdef",
        trace_name="agentgov.run",
        names={"agentgov.run", "agentgov.run.stage", "invoke_agent", "chat"},
        root_attribute_keys={"agentgov.run.id"},
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert len(errors) == 1
    assert "agentgov.agent.version_id" in errors[0]
    assert "agentgov.run.finished_reason" in errors[0]


def test_agentscope_trace_matches_stage_replies_to_terminal_run_exactly() -> None:
    assert "agentscope.agent.reply_id" not in langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        projected_trace_id="0123456789abcdef0123456789abcdef",
        trace_name="agentgov.run",
        names={"agentgov.run", "agentgov.run.stage", "invoke_agent", "chat"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1", "reply-unpersisted"],
    )

    assert errors == [
        "trace 0123456789abcdef0123456789abcdef run stages do not exactly match persisted reply_ids",
    ]
