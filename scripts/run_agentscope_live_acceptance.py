#!/usr/bin/env python3
"""通过 AgentGov 公共 API 执行真实 AgentScope Runtime 验收。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias, cast
from urllib.parse import quote

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.runtime_acceptance_fixture import (
    FIXTURE_EXCLUDED_CLAIMS,
    FIXTURE_SCOPE,
    FixtureAgentError,
    temporary_runtime_agent,
)

TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
TRACE_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
PLACEHOLDER_MARKERS: Final = ("replace-with", "change-me", "example", "dummy", "test-only")


class LiveAcceptanceError(RuntimeError):
    """真实验收前置或公共契约不满足。"""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    prompt: str
    feedback_comment: str


@dataclass(frozen=True)
class RunEvidence:
    scenario_id: str
    binding: BindingEvidence
    session_id: str
    run_id: str
    reply_ids: tuple[str, ...]
    trace_id: str
    trace_status: str
    feedback_signal_id: str
    sse_event_types: tuple[str, ...]


@dataclass(frozen=True)
class TerminalEvidence:
    """已通过 run/session/reply/trace 身份校验的终态证据。"""

    reply_ids: tuple[str, ...]
    trace_id: str


@dataclass(frozen=True)
class BindingEvidence:
    """公开 provision 响应确认的不可变发布身份，不是客户端自造的 Agent ID。"""

    governance_agent_id: str
    runtime_agent_id: str
    agent_version_id: str
    harness_digest: str


EnvValues: TypeAlias = dict[str, str]
JsonObject: TypeAlias = dict[str, object]


DEFAULT_SCENARIOS: Final = (
    Scenario(
        scenario_id="basic-runtime-contract",
        prompt="这是 AgentScope Runtime 真实验收。请用一句简短中文确认已收到本消息，不调用外部工具。",
        feedback_comment="AgentScope 真实验收自动提交的正向链路反馈。",
    ),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 AgentGov 公共 API 验收真实 AgentScope 运行链路。")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--scenario-file", type=Path)
    parser.add_argument("--fixture-agent", action="store_true", help="显式创建无 MCP/subagents 的临时业务 Agent；仅验收通用 Runtime 基础链路")
    parser.add_argument("--runs", type=int, default=int(os.environ.get("LIVE_ACCEPTANCE_RUNS", "1")))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("LIVE_ACCEPTANCE_CONCURRENCY", "1")))
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--require-trace-complete",
        action="store_true",
        default=os.environ.get("LIVE_ACCEPTANCE_REQUIRE_TRACE_COMPLETE", "").strip().lower() in TRUTHY,
    )
    return parser.parse_args(argv)


def _read_env_file(path: Path) -> EnvValues:
    if not path.is_file():
        raise LiveAcceptanceError("所选隔离 Compose env 文件不存在")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _require_explicit_live_authorization(env: EnvValues, *, require_trace_complete: bool = False) -> None:
    if os.environ.get("REQUIRE_LIVE_RUNTIME", "").strip().lower() not in TRUTHY:
        raise LiveAcceptanceError("必须显式设置 REQUIRE_LIVE_RUNTIME=1 才能调用真实模型")
    if os.environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE") != "1":
        raise LiveAcceptanceError("必须通过 make container-live-test 的隔离容器 runner 执行")
    if not os.environ.get("AGENT_GOV_ACCEPTANCE_RUN_ID", "").strip():
        raise LiveAcceptanceError("缺少隔离容器验收 run id")
    profile = os.environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE")
    if profile not in {"core", "langfuse"}:
        raise LiveAcceptanceError("AgentScope live 验收必须使用 core 或 langfuse 隔离 profile")
    if require_trace_complete and profile != "langfuse":
        raise LiveAcceptanceError("完整 Trace 验收必须使用 langfuse 隔离 profile")
    for key in ("API_KEY", "MODEL_PROVIDER_API_KEY", "AGENTSCOPE_MODEL_NAME"):
        value = env.get(key, "").strip()
        if not value or any(marker in value.lower() for marker in PLACEHOLDER_MARKERS):
            raise LiveAcceptanceError(f"真实验收要求所选 env 提供非占位 {key}")
    if env.get("AGENTGOV_API_MODE") == "acceptance":
        if not env.get("AGENTGOV_ACCEPTANCE_IDENTITY", "").strip():
            raise LiveAcceptanceError("cutover acceptance 模式缺少一次性 identity")
        if env.get("AGENTGOV_ACCEPTANCE_API_KEY") != env.get("API_KEY"):
            raise LiveAcceptanceError("cutover acceptance Bearer key 与 API_KEY 未绑定")


def load_scenarios(path: Path | None) -> tuple[Scenario, ...]:
    if path is None:
        return DEFAULT_SCENARIOS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveAcceptanceError("场景文件必须是可读 JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise LiveAcceptanceError("场景文件必须是非空 JSON array")
    scenarios: list[Scenario] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise LiveAcceptanceError(f"场景 {index} 必须是 JSON object")
        scenario_id = str(item.get("id") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        feedback = str(item.get("feedback_comment") or "AgentScope 真实验收链路反馈。 ").strip()
        if not scenario_id or not prompt:
            raise LiveAcceptanceError(f"场景 {index} 缺少非空 id 或 prompt")
        scenarios.append(Scenario(scenario_id, prompt, feedback))
    if len({item.scenario_id for item in scenarios}) != len(scenarios):
        raise LiveAcceptanceError("场景 id 必须唯一")
    if len({" ".join(item.prompt.split()) for item in scenarios}) != len(scenarios):
        raise LiveAcceptanceError("场景 prompt 必须实质不同，不能只重复同一输入")
    return tuple(scenarios)


def select_scenarios(scenarios: tuple[Scenario, ...], runs: int, concurrency: int) -> tuple[Scenario, ...]:
    if runs < 1:
        raise LiveAcceptanceError("--runs 必须大于零")
    if concurrency < 1 or concurrency > runs:
        raise LiveAcceptanceError("--concurrency 必须在 1 与 --runs 之间")
    if runs > len(scenarios):
        raise LiveAcceptanceError(f"要求 {runs} 次实质不同 run，但场景文件只有 {len(scenarios)} 条；不得循环复制场景制造 50-run 证据")
    return scenarios[:runs]


def _json_object(response: httpx.Response, label: str) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveAcceptanceError(f"{label} 未返回 JSON object") from exc
    if not isinstance(payload, dict):
        raise LiveAcceptanceError(f"{label} 未返回 JSON object")
    return cast(JsonObject, payload)


def _sse_event_types(raw: bytes) -> tuple[str, ...]:
    normalized = raw.replace(b"\r\n", b"\n")
    event_types: list[str] = []
    for frame in normalized.split(b"\n\n"):
        data = b"\n".join(line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:"))
        if not data:
            continue
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            event_types.append(payload["type"])
    return tuple(event_types)


async def _consume_sse(
    client: httpx.AsyncClient,
    session_id: str,
    agent_id: str,
    ready: asyncio.Event,
    raw: bytearray,
) -> None:
    async with client.stream(
        "GET",
        f"/api/runtime/sessions/{session_id}/stream",
        params={"agent_id": agent_id},
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            raise LiveAcceptanceError("Runtime stream 未返回 text/event-stream")
        ready.set()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    run_id: str,
    timeout_seconds: float,
) -> JsonObject:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/agent-runs/{run_id}")
        response.raise_for_status()
        run = _json_object(response, "run 查询")
        if run.get("status") in TERMINAL_STATUSES:
            return run
        await asyncio.sleep(0.25)
    raise LiveAcceptanceError(f"run {run_id} 未在时限内进入终态")


async def _best_effort_cleanup(client: httpx.AsyncClient, session_id: str, agent_id: str, run_id: str | None) -> None:
    if run_id:
        try:
            run_response = await client.get(f"/api/agent-runs/{run_id}")
            if run_response.status_code == 200 and _json_object(run_response, "run 清理查询").get("status") not in TERMINAL_STATUSES:
                await client.post(f"/api/agent-runs/{run_id}/cancel")
                await _wait_for_terminal(client, run_id, 30.0)
        except Exception:
            pass
    with suppress(Exception):
        await client.delete(
            f"/api/runtime/sessions/{session_id}",
            params={"agent_id": agent_id},
        )


async def _create_session(client: httpx.AsyncClient, scenario: Scenario, agent_id: str) -> str:
    create = await client.post(
        "/api/runtime/sessions/",
        headers={"Idempotency-Key": f"live-{scenario.scenario_id}-{uuid.uuid4().hex}"},
        json={"agent_id": agent_id, "name": f"live acceptance: {scenario.scenario_id}"},
    )
    create.raise_for_status()
    session_id = str(_json_object(create, "session 创建").get("session_id") or "")
    if not session_id or create.headers.get("X-AgentGov-Session-Id") != session_id:
        raise LiveAcceptanceError("session 响应体与 X-AgentGov-Session-Id 不一致")
    return session_id


async def _provision_agent(client: httpx.AsyncClient, governance_agent_id: str) -> BindingEvidence:
    response = await client.post(f"/api/runtime/agents/{quote(governance_agent_id, safe='')}/provision")
    response.raise_for_status()
    binding = _json_object(response, "Agent provision")
    if binding.get("governance_agent_id") != governance_agent_id or binding.get("provisioned") is not True:
        raise LiveAcceptanceError("provision 未确认所选治理 Agent 的发布绑定")
    fields = [binding.get(key) for key in ("runtime_agent_id", "agent_version_id", "harness_digest")]
    if not all(isinstance(value, str) and value.strip() for value in fields):
        raise LiveAcceptanceError("provision 缺少 Runtime Agent 或发布版本绑定")
    runtime_agent_id, version_id, digest = cast(list[str], fields)
    return BindingEvidence(governance_agent_id, runtime_agent_id, version_id, digest)


async def _trigger_run(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    agent_id: str,
    session_id: str,
    timeout_seconds: float,
) -> str:
    client_operation_id = f"live-op-{scenario.scenario_id}-{uuid.uuid4().hex}"
    chat = await client.post(
        "/api/runtime/chat/",
        json={
            "agent_id": agent_id,
            "session_id": session_id,
            "client_operation_id": client_operation_id,
            "input": {
                "name": "user",
                "role": "user",
                "content": [{"type": "text", "text": scenario.prompt}],
            },
            "metadata": {
                "source": "agentscope_live_acceptance",
                "scenario_id": scenario.scenario_id,
                "acceptance_run_id": os.environ["AGENT_GOV_ACCEPTANCE_RUN_ID"],
            },
        },
        timeout=timeout_seconds,
    )
    chat.raise_for_status()
    run_id = str(chat.headers.get("X-AgentGov-Run-Id") or "")
    if not run_id or chat.headers.get("X-AgentGov-Session-Id") != session_id:
        raise LiveAcceptanceError("chat 未返回一致的 run/session headers")
    return run_id


async def _validate_terminal_run(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
    timeout_seconds: float,
) -> TerminalEvidence:
    run = await _wait_for_terminal(client, run_id, timeout_seconds)
    if run.get("status") != "succeeded":
        raise LiveAcceptanceError(f"真实 AgentScope run 未成功: {run.get('status')}")
    expected = {
        "run_id": run_id,
        "session_id": session_id,
        "agent_id": binding.governance_agent_id,
        "runtime_agent_id": binding.runtime_agent_id,
        "agent_version_id": binding.agent_version_id,
        "harness_digest": binding.harness_digest,
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise LiveAcceptanceError("run 与 session/Agent 发布绑定不一致")
    raw_reply_ids = run.get("reply_ids")
    reply_ids = tuple(value for value in raw_reply_ids if isinstance(value, str) and value) if isinstance(raw_reply_ids, list) else ()
    if not reply_ids:
        raise LiveAcceptanceError("终态 run 缺少 AgentScope reply_id")
    persisted = run.get("persisted_reply_ids")
    if not isinstance(persisted, list) or not set(reply_ids).issubset(persisted):
        raise LiveAcceptanceError("终态 run 缺少本次 reply 的持久化确认")
    trace_id = str(run.get("trace_id") or "")
    if not TRACE_ID_PATTERN.fullmatch(trace_id):
        raise LiveAcceptanceError("终态 run 缺少合法 OTel trace_id")
    return TerminalEvidence(reply_ids, trace_id)


async def _validate_messages_and_trace(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    session_id: str,
    agent_id: str,
    trace_id: str,
    reply_ids: tuple[str, ...],
    require_trace_complete: bool,
    timeout_seconds: float,
) -> str:
    messages_response = await client.get(
        f"/api/runtime/sessions/{session_id}/messages",
        params={"agent_id": agent_id, "limit": 200},
    )
    messages_response.raise_for_status()
    messages = _json_object(messages_response, "canonical messages").get("messages")
    _validate_canonical_replies(messages, reply_ids)
    return await _wait_for_trace(client, run_id, trace_id, require_trace_complete, timeout_seconds)


def _validate_canonical_replies(messages: object, reply_ids: tuple[str, ...]) -> None:
    if not isinstance(messages, list):
        raise LiveAcceptanceError("AgentScope canonical messages 为空")
    replies = {item.get("id"): item for item in messages if isinstance(item, dict) and item.get("role") == "assistant"}
    for reply_id in reply_ids:
        message = replies.get(reply_id)
        if not message or message.get("finished_reason") != "completed" or message.get("error"):
            raise LiveAcceptanceError("canonical messages 缺少本次成功终态 assistant reply")
    content = replies[reply_ids[-1]].get("content")
    if not isinstance(content, list) or not any(
        isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip() for block in content
    ):
        raise LiveAcceptanceError("本次 canonical assistant reply 没有非空文本结果")


async def _wait_for_trace(client: httpx.AsyncClient, run_id: str, trace_id: str, require_complete: bool, timeout_seconds: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        trace_response = await client.get(f"/api/agent-runs/{run_id}/trace")
        trace_response.raise_for_status()
        trace = _json_object(trace_response, "trace 查询")
        if trace.get("run_id") != run_id or trace.get("trace_id") != trace_id:
            raise LiveAcceptanceError("run 与 trace_id 映射不一致")
        trace_status = str(trace.get("trace_status") or "")
        if trace_status not in {"pending", "complete", "incomplete"}:
            raise LiveAcceptanceError("trace 查询返回未知完整性状态")
        if not require_complete or trace_status == "complete":
            return trace_status
        if asyncio.get_running_loop().time() >= deadline:
            raise LiveAcceptanceError("已要求 OTLP/Langfuse 完整证据，但 trace_status 未在时限内达到 complete")
        await asyncio.sleep(0.25)


async def _submit_feedback(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    run_id: str,
    session_id: str,
    governance_agent_id: str,
) -> str:
    feedback = await client.post(
        "/api/feedback-signals",
        json={
            "source_type": "explicit_feedback",
            "run_id": run_id,
            "session_id": session_id,
            "labels": ["agentscope-live-acceptance", scenario.scenario_id],
            "comment": scenario.feedback_comment,
            "confidence": "high",
            "metadata": {"acceptance_run_id": os.environ["AGENT_GOV_ACCEPTANCE_RUN_ID"]},
        },
    )
    feedback.raise_for_status()
    signal = _json_object(feedback, "反馈提交")
    expected = {
        "source_type": "explicit_feedback",
        "run_id": run_id,
        "matched_run_id": run_id,
        "session_id": session_id,
        "agent_id": governance_agent_id,
    }
    if any(signal.get(key) != value for key, value in expected.items()):
        raise LiveAcceptanceError("反馈没有关联到本次 run/session/治理 Agent 来源")
    signal_id = str(signal.get("signal_id") or "")
    if not signal_id:
        raise LiveAcceptanceError("反馈响应缺少 signal_id")
    persisted_response = await client.get(f"/api/feedback-signals/{quote(signal_id, safe='')}")
    persisted_response.raise_for_status()
    persisted = _json_object(persisted_response, "反馈来源查询")
    if persisted.get("signal_id") != signal_id or any(persisted.get(key) != value for key, value in expected.items()):
        raise LiveAcceptanceError("持久化反馈来源与本次 run/session/治理 Agent 不一致")
    return signal_id


async def run_scenario(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    binding: BindingEvidence,
    timeout_seconds: float,
    require_trace_complete: bool,
) -> RunEvidence:
    agent_id = binding.runtime_agent_id
    session_id = await _create_session(client, scenario, agent_id)
    run_id: str | None = None
    stream_raw = bytearray()
    stream_ready = asyncio.Event()
    stream_task = asyncio.create_task(_consume_sse(client, session_id, agent_id, stream_ready, stream_raw))
    try:
        await asyncio.wait_for(stream_ready.wait(), timeout=30.0)
        run_id = await _trigger_run(
            client,
            scenario,
            agent_id=agent_id,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )
        terminal = await _validate_terminal_run(
            client,
            run_id,
            session_id=session_id,
            binding=binding,
            timeout_seconds=timeout_seconds,
        )
        trace_status = await _validate_messages_and_trace(
            client,
            run_id,
            session_id=session_id,
            agent_id=agent_id,
            trace_id=terminal.trace_id,
            reply_ids=terminal.reply_ids,
            require_trace_complete=require_trace_complete,
            timeout_seconds=timeout_seconds,
        )
        signal_id = await _submit_feedback(client, scenario, run_id=run_id, session_id=session_id, governance_agent_id=binding.governance_agent_id)

        try:
            await asyncio.wait_for(stream_task, timeout=5.0)
        except TimeoutError:
            stream_task.cancel()
        event_types = _sse_event_types(bytes(stream_raw))
        if not event_types:
            raise LiveAcceptanceError("SSE 未观测到任何 AgentScope 原生事件")
        return RunEvidence(
            scenario_id=scenario.scenario_id,
            binding=binding,
            session_id=session_id,
            run_id=run_id,
            reply_ids=terminal.reply_ids,
            trace_id=terminal.trace_id,
            trace_status=trace_status,
            feedback_signal_id=signal_id,
            sse_event_types=event_types,
        )
    finally:
        if not stream_task.done():
            stream_task.cancel()
        await asyncio.gather(stream_task, return_exceptions=True)
        await _best_effort_cleanup(client, session_id, agent_id, run_id)


async def _run_selected_scenarios(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    selected: tuple[Scenario, ...],
    binding: BindingEvidence,
) -> list[RunEvidence]:
    semaphore = asyncio.Semaphore(args.concurrency)

    async def limited(scenario: Scenario) -> RunEvidence:
        async with semaphore:
            return await run_scenario(
                client,
                scenario,
                binding=binding,
                timeout_seconds=args.timeout_seconds,
                require_trace_complete=args.require_trace_complete,
            )

    tasks = [asyncio.create_task(limited(scenario)) for scenario in selected]
    try:
        return list(await asyncio.gather(*tasks))
    finally:
        # 部分场景失败时先完成兄弟任务的 Session/run 清理，再删除临时 Agent。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_live_acceptance(args: argparse.Namespace, env: EnvValues, scenarios: tuple[Scenario, ...]) -> list[RunEvidence]:
    api_base = (env.get("API_BASE") or "").rstrip("/")
    api_key = env["API_KEY"]
    agent_id = (env.get("LIVE_ACCEPTANCE_AGENT_ID") or "security-operations-expert").strip()
    if not api_base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise LiveAcceptanceError("隔离 live 验收只允许访问 runner 生成的本机公开 API")
    selected = select_scenarios(scenarios, args.runs, args.concurrency)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    acceptance_identity = env.get("AGENTGOV_ACCEPTANCE_IDENTITY", "").strip()
    if env.get("AGENTGOV_API_MODE") == "acceptance" and acceptance_identity:
        headers["X-AgentGov-Acceptance-Identity"] = acceptance_identity
    timeout = httpx.Timeout(args.timeout_seconds, connect=30.0)
    async with httpx.AsyncClient(base_url=api_base, headers=headers, timeout=timeout) as client:
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        if args.fixture_agent:
            async with temporary_runtime_agent(client) as imported:
                binding = await _provision_agent(client, imported.agent.agent_id)
                if binding.agent_version_id != imported.current_commit_sha:
                    raise LiveAcceptanceError("临时 Agent provision 版本与本次导入 Git 提交不一致")
                return await _run_selected_scenarios(client, args, selected, binding)
        binding = await _provision_agent(client, agent_id)
        return await _run_selected_scenarios(client, args, selected, binding)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        env = _read_env_file(args.env_file.resolve())
        _require_explicit_live_authorization(env, require_trace_complete=args.require_trace_complete)
        scenarios = load_scenarios(args.scenario_file)
        evidence = asyncio.run(run_live_acceptance(args, env, scenarios))
    except (LiveAcceptanceError, FixtureAgentError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"AGENTSCOPE_LIVE_ACCEPTANCE_FAIL: {exc}", file=sys.stderr)
        return 1
    summary = {
        "schema_version": 1,
        "runtime": "agentscope",
        "acceptance_scope": FIXTURE_SCOPE if args.fixture_agent else "selected-agent-runtime",
        "excluded_claims": list(FIXTURE_EXCLUDED_CLAIMS) if args.fixture_agent else [],
        "acceptance_run_id": os.environ["AGENT_GOV_ACCEPTANCE_RUN_ID"],
        "runs": [
            {
                "scenario_id": item.scenario_id,
                "governance_agent_id": item.binding.governance_agent_id,
                "runtime_agent_id": item.binding.runtime_agent_id,
                "agent_version_id": item.binding.agent_version_id,
                "harness_digest": item.binding.harness_digest,
                "session_id": item.session_id,
                "run_id": item.run_id,
                "reply_ids": list(item.reply_ids),
                "trace_id": item.trace_id,
                "trace_status": item.trace_status,
                "feedback_signal_id": item.feedback_signal_id,
                "sse_event_types": list(item.sse_event_types),
            }
            for item in evidence
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
