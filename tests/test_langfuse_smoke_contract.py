"""Langfuse smoke 的纯语义契约；连接、队列与轮询只在真实容器入口执行。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.runtime_gateway.native_chat_input import RuntimeChatRequest, explicit_native_input_ids
from scripts import langfuse_smoke
from scripts.langfuse_smoke import runtime_trace_observation_errors


@pytest.mark.parametrize("extra", [[], ["--agent-id", "an-agent"]])
def test_projected_trace_cli_validates_read_only_request_without_live_run_authorization(tmp_path: Path, extra: list[str]) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("LANGFUSE_ENABLED=false\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "scripts/langfuse_smoke.py", "--env-file", str(env_file), "--projected-trace-id", "invalid", *extra],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    expected = "不能与 --scenario-file/--agent-id" if extra else "projected trace id must be 32 lowercase hex"
    assert expected in result.stderr
    assert "REQUIRE_LIVE_RUNTIME" not in result.stderr


def test_runtime_trace_cli_still_requires_explicit_live_authorization(tmp_path: Path) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("LANGFUSE_ENABLED=false\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "scripts/langfuse_smoke.py", "--env-file", str(env_file)],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "REQUIRE_LIVE_RUNTIME" in result.stderr


def test_runtime_trace_request_uses_only_native_chat_contract() -> None:
    payload = langfuse_smoke.runtime_chat_payload("runtime-a", "session-a", "trace this run")

    assert set(payload) == {"agent_id", "session_id", "input"}
    assert "client_operation_id" not in payload
    assert "metadata" not in payload
    request = RuntimeChatRequest.model_validate(payload)
    assert request.agent_id == "runtime-a"
    assert request.session_id == "session-a"
    assert explicit_native_input_ids(request.raw_input) == (request.raw_input["id"],)
    assert request.raw_input["content"] == [{"type": "text", "text": "trace this run"}]


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


def test_redis_auth_is_delivered_on_stdin_not_host_command_line() -> None:
    secret = "private-redis-password"
    command, stdin = langfuse_smoke.redis_request("a" * 64, secret, "type", "queue")

    assert command[:4] == ["docker", "exec", "-i", "a" * 64]
    assert secret not in "\0".join(command)
    assert "--env" not in command
    assert 'REDISCLI_AUTH="$(cat)"' in command[7]
    assert stdin == secret


@pytest.mark.parametrize("project", ["checkout-a", "checkout-b"])
def test_redis_query_uses_selected_project_service_not_container_prefix(project: str) -> None:
    command = langfuse_smoke.redis_container_query({"COMPOSE_PROJECT_NAME": project, "CONTAINER_NAME_PREFIX": "another-instance"})
    assert f"label=com.docker.compose.project={project}" in command
    assert "label=com.docker.compose.service=langfuse-redis" in command
    assert "--no-trunc" in command
    assert "another-instance" not in " ".join(command)


@pytest.mark.parametrize("project", ["", "--all", "other project", "OTHER"])
def test_redis_query_rejects_missing_or_invalid_project(project: str) -> None:
    with pytest.raises(langfuse_smoke.RedisCommandError, match="selected COMPOSE_PROJECT_NAME"):
        langfuse_smoke.redis_container_query({"COMPOSE_PROJECT_NAME": project})


@pytest.mark.parametrize("legacy_value", ["", "another-instance-langfuse-redis"])
def test_redis_query_rejects_retired_container_override(legacy_value: str) -> None:
    with pytest.raises(langfuse_smoke.RedisCommandError, match="Remove retired LANGFUSE_REDIS_CONTAINER"):
        langfuse_smoke.redis_container_query({"COMPOSE_PROJECT_NAME": "checkout-a", "LANGFUSE_REDIS_CONTAINER": legacy_value})


@pytest.mark.parametrize("output", ["", "a" * 12, "a" * 64 + "\n" + "b" * 64, "--all"])
def test_redis_resolution_rejects_missing_ambiguous_or_non_id_targets(output: str) -> None:
    assert langfuse_smoke.require_single_redis_container("a" * 64) == "a" * 64
    with pytest.raises(langfuse_smoke.RedisCommandError, match="exactly one container"):
        langfuse_smoke.require_single_redis_container(output)


@pytest.mark.parametrize(
    "changed",
    [{"id": "b" * 64}, {"project": "checkout-b"}, {"service": "langfuse-web"}, {"running": False}, {"running": "true"}],
)
def test_redis_inspect_must_confirm_exact_running_service(changed: dict[str, object]) -> None:
    identity = {"id": "a" * 64, "project": "checkout-a", "service": "langfuse-redis", "running": True}
    assert langfuse_smoke.require_redis_container_identity(identity, project="checkout-a", container_id="a" * 64) == "a" * 64
    with pytest.raises(langfuse_smoke.RedisCommandError, match="selected Compose project"):
        langfuse_smoke.require_redis_container_identity({**identity, **changed}, project="checkout-a", container_id="a" * 64)


def test_selected_env_project_cannot_be_redirected_by_ambient_environment(tmp_path: Path) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("COMPOSE_PROJECT_NAME=selected-project\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from scripts.langfuse_smoke import load_env; import sys; print(load_env(Path(sys.argv[1]))['COMPOSE_PROJECT_NAME'])",
            str(env_file),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "COMPOSE_PROJECT_NAME": "ambient-project"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "selected-project"


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
