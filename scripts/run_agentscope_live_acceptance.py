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
from pathlib import Path
from typing import Final, TypeAlias, cast
from urllib.parse import quote

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_live_acceptance_cli import parse_args
from scripts.agentscope_live_acceptance_report import (
    BindingEvidence,
    RunEvidence,
    TerminalEvidence,
    build_live_acceptance_summary,
    summarize_http_failure,
)
from scripts.agentscope_live_acceptance_scenarios import (
    MCP_READONLY_CAPABILITY,
    LiveAcceptanceError,
    Scenario,
    has_nonempty_sse_text,
    load_scenarios,
    observed_run_concurrency,
    select_scenarios,
    validate_evidence_identities,
    validate_sse_evidence,
)
from scripts.agentscope_live_native_chat import NativeChatAttempt, lookup_native_run, require_native_run_identity, submit_native_chat
from scripts.agentscope_mcp_live_acceptance import (
    validate_mcp_evidence_if_present,
    validate_workspace_mcp_if_required,
)
from scripts.container_acceptance_inputs import (
    read_compose_env as _read_env_file,
)
from scripts.container_acceptance_inputs import (
    require_live_authorization as _require_explicit_live_authorization,
)
from scripts.runtime_technical_integration_seed import (
    MCP_TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS,
    MCP_TECHNICAL_INTEGRATION_SCOPE,
    TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS,
    TECHNICAL_INTEGRATION_SCOPE,
    TechnicalIntegrationSeedError,
    temporary_technical_integration_agent,
)

TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
TRACE_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")


EnvValues: TypeAlias = dict[str, str]
JsonObject: TypeAlias = dict[str, object]


def _json_object(response: httpx.Response, label: str) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveAcceptanceError(f"{label} 未返回 JSON object") from exc
    if not isinstance(payload, dict):
        raise LiveAcceptanceError(f"{label} 未返回 JSON object")
    return cast(JsonObject, payload)


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


async def _best_effort_cleanup(client: httpx.AsyncClient, session_id: str, binding: BindingEvidence, run_id: str | None) -> None:
    if run_id:
        try:
            run_response = await client.get(f"/api/agent-runs/{run_id}")
            run = require_native_run_identity(_json_object(run_response, "run 清理查询"), binding, session_id, run_id)
            if run_response.status_code == 200 and run.get("status") not in TERMINAL_STATUSES:
                await client.post(f"/api/agent-runs/{run_id}/cancel")
                await _wait_for_terminal(client, run_id, 30.0)
        except Exception:
            pass
    with suppress(Exception):
        await client.delete(
            f"/api/runtime/sessions/{session_id}",
            params={"agent_id": binding.runtime_agent_id},
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


async def _current_agent_binding(client: httpx.AsyncClient, governance_agent_id: str) -> BindingEvidence:
    response = await client.get(f"/api/runtime/agents/{quote(governance_agent_id, safe='')}/current")
    response.raise_for_status()
    binding = _json_object(response, "Agent current binding")
    if binding.get("governance_agent_id") != governance_agent_id or binding.get("provisioned") is not True:
        raise LiveAcceptanceError("当前发布版本尚无已验证的 Runtime 绑定")
    fields = [binding.get(key) for key in ("runtime_agent_id", "agent_version_id", "harness_digest")]
    if not all(isinstance(value, str) and value.strip() for value in fields):
        raise LiveAcceptanceError("current 缺少 Runtime Agent 或发布版本绑定")
    runtime_agent_id, version_id, digest = cast(list[str], fields)
    return BindingEvidence(governance_agent_id, runtime_agent_id, version_id, digest)


def _validate_run_identity(
    run: JsonObject,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
) -> None:
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


async def _wait_for_session_idle(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    runtime_agent_id: str,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(
            f"/api/runtime/sessions/{session_id}/status",
            params={"agent_id": runtime_agent_id},
        )
        response.raise_for_status()
        status = _json_object(response, "Runtime Session 状态")
        if status.get("session_id") != session_id:
            raise LiveAcceptanceError("Runtime Session 状态返回了不同 session_id")
        if status.get("status") == "idle":
            return
        await asyncio.sleep(0.25)
    raise LiveAcceptanceError(f"Runtime Session {session_id} 未在时限内释放执行槽")


async def _cancel_exact_run(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
    timeout_seconds: float,
) -> JsonObject:
    response = await client.post(f"/api/agent-runs/{run_id}/cancel")
    if response.status_code != 202:
        response.raise_for_status()
        raise LiveAcceptanceError("精确 run 取消未返回 HTTP 202")
    cancelled = _json_object(response, "run 取消")
    if (
        cancelled.get("run_id") != run_id
        or cancelled.get("session_id") != session_id
        or response.headers.get("X-AgentGov-Run-Id") != run_id
        or response.headers.get("X-AgentGov-Session-Id") != session_id
    ):
        raise LiveAcceptanceError("取消回执未绑定精确 run/session")
    terminal = await _wait_for_terminal(client, run_id, timeout_seconds)
    _validate_run_identity(terminal, run_id, session_id=session_id, binding=binding)
    if terminal.get("status") != "cancelled":
        raise LiveAcceptanceError(f"取消场景未进入 cancelled 终态: {terminal.get('status')}")
    metadata = terminal.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("cancellation_requested") is not True:
        raise LiveAcceptanceError("取消终态缺少服务端 cancellation_requested 事实")
    await _wait_for_session_idle(
        client,
        session_id=session_id,
        runtime_agent_id=binding.runtime_agent_id,
        timeout_seconds=timeout_seconds,
    )
    return terminal


async def _wait_for_partial_text(raw: bytearray, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if has_nonempty_sse_text(bytes(raw)):
            return
        await asyncio.sleep(0.05)
    raise LiveAcceptanceError("partial_cancel 场景在时限内未观测到真实文本增量")


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
    _validate_run_identity(run, run_id, session_id=session_id, binding=binding)
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


async def _cancelled_evidence(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
    require_trace_complete: bool,
    timeout_seconds: float,
) -> tuple[tuple[str, ...], str, str]:
    cancelled = await _cancel_exact_run(
        client,
        run_id,
        session_id=session_id,
        binding=binding,
        timeout_seconds=timeout_seconds,
    )
    raw_reply_ids = cancelled.get("reply_ids")
    reply_ids = tuple(value for value in raw_reply_ids if isinstance(value, str) and value) if isinstance(raw_reply_ids, list) else ()
    trace_id = str(cancelled.get("trace_id") or "")
    if not TRACE_ID_PATTERN.fullmatch(trace_id):
        raise LiveAcceptanceError("取消终态缺少合法 OTel trace_id")
    trace_status = await _wait_for_trace(
        client,
        run_id,
        trace_id,
        require_trace_complete,
        timeout_seconds,
    )
    return reply_ids, trace_id, trace_status


async def _successful_evidence(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
    require_trace_complete: bool,
    timeout_seconds: float,
) -> tuple[tuple[str, ...], str, str]:
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
        agent_id=binding.runtime_agent_id,
        trace_id=terminal.trace_id,
        reply_ids=terminal.reply_ids,
        require_trace_complete=require_trace_complete,
        timeout_seconds=timeout_seconds,
    )
    return terminal.reply_ids, terminal.trace_id, trace_status


async def _finish_stream_evidence(
    task: asyncio.Task[None],
    raw: bytearray,
    *,
    purpose: str,
    terminal_reply_ids: tuple[str, ...],
) -> tuple[str, ...]:
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        task.cancel()
    return validate_sse_evidence(
        bytes(raw),
        purpose=purpose,
        terminal_reply_ids=terminal_reply_ids,
    )


async def _trigger_with_required_replay(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    binding: BindingEvidence,
    session_id: str,
    input_value: dict[str, object],
    attempt: NativeChatAttempt,
    timeout_seconds: float,
) -> tuple[str, bool]:
    run_id = await submit_native_chat(client, binding, session_id, input_value, attempt, timeout_seconds=timeout_seconds)
    if scenario.purpose != "retry":
        return run_id, False
    if not attempt.receipt_received:
        raise LiveAcceptanceError("NATIVE_REPLAY_REQUIRES_CONFIRMED_FIRST_RECEIPT")
    replay = NativeChatAttempt()
    replayed_run_id = await submit_native_chat(client, binding, session_id, input_value, replay, timeout_seconds=timeout_seconds)
    if not replay.receipt_received:
        raise LiveAcceptanceError("NATIVE_REPLAY_RECEIPT_UNCONFIRMED")
    if replayed_run_id != run_id:
        raise LiveAcceptanceError("NATIVE_REPLAY_CREATED_DIFFERENT_RUN")
    return run_id, True


async def _scenario_terminal_evidence(
    client: httpx.AsyncClient,
    scenario: Scenario,
    run_id: str,
    *,
    session_id: str,
    binding: BindingEvidence,
    require_trace_complete: bool,
    timeout_seconds: float,
) -> tuple[tuple[str, ...], str, str]:
    if scenario.purpose in {"early_cancel", "partial_cancel"}:
        return await _cancelled_evidence(
            client,
            run_id,
            session_id=session_id,
            binding=binding,
            require_trace_complete=require_trace_complete,
            timeout_seconds=timeout_seconds,
        )
    return await _successful_evidence(
        client,
        run_id,
        session_id=session_id,
        binding=binding,
        require_trace_complete=require_trace_complete,
        timeout_seconds=timeout_seconds,
    )


async def _optional_feedback(
    client: httpx.AsyncClient,
    scenario: Scenario,
    run_id: str,
    session_id: str,
    governance_agent_id: str,
) -> str | None:
    if not scenario.feedback_comment or scenario.purpose in {"early_cancel", "partial_cancel"}:
        return None
    return await _submit_feedback(
        client,
        scenario,
        run_id=run_id,
        session_id=session_id,
        governance_agent_id=governance_agent_id,
    )


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
    input_value: dict[str, object] = {"id": uuid.uuid4().hex, "name": "user", "role": "user", "content": [{"type": "text", "text": scenario.input_text}]}
    attempt = NativeChatAttempt()
    run_id: str | None = None
    stream_raw = bytearray()
    stream_ready = asyncio.Event()
    stream_task = asyncio.create_task(_consume_sse(client, session_id, agent_id, stream_ready, stream_raw))
    try:
        mcp_workspace = await validate_workspace_mcp_if_required(
            client,
            scenario,
            session_id=session_id,
            runtime_agent_id=agent_id,
        )
        await asyncio.wait_for(stream_ready.wait(), timeout=60.0)
        run_id, operation_replayed = await _trigger_with_required_replay(
            client,
            scenario,
            binding=binding,
            session_id=session_id,
            input_value=input_value,
            attempt=attempt,
            timeout_seconds=timeout_seconds,
        )

        if scenario.purpose == "partial_cancel":
            await _wait_for_partial_text(stream_raw, min(timeout_seconds, 60.0))

        reply_ids, trace_id, trace_status = await _scenario_terminal_evidence(
            client,
            scenario,
            run_id,
            session_id=session_id,
            binding=binding,
            require_trace_complete=require_trace_complete,
            timeout_seconds=timeout_seconds,
        )
        signal_id = await _optional_feedback(client, scenario, run_id, session_id, binding.governance_agent_id)

        event_types = await _finish_stream_evidence(
            stream_task,
            stream_raw,
            purpose=scenario.purpose,
            terminal_reply_ids=reply_ids,
        )
        mcp_evidence = await validate_mcp_evidence_if_present(
            client,
            scenario,
            session_id=session_id,
            runtime_agent_id=agent_id,
            reply_ids=reply_ids,
            raw_sse=bytes(stream_raw),
            workspace=mcp_workspace,
        )
        return RunEvidence(
            scenario_id=scenario.scenario_id,
            purpose=scenario.purpose,
            capability=scenario.capability,
            binding=binding,
            session_id=session_id,
            run_id=run_id,
            reply_ids=reply_ids,
            trace_id=trace_id,
            trace_status=trace_status,
            feedback_signal_id=signal_id,
            sse_event_types=event_types,
            operation_replayed=operation_replayed,
            mcp=mcp_evidence,
        )
    finally:
        await _cleanup_scenario(client, binding, session_id, input_value, attempt, run_id, stream_task)


async def _cleanup_scenario(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    session_id: str,
    input_value: dict[str, object],
    attempt: NativeChatAttempt,
    run_id: str | None,
    stream_task: asyncio.Task[None],
) -> None:
    if not stream_task.done():
        stream_task.cancel()
    await asyncio.gather(stream_task, return_exceptions=True)
    if run_id is None and attempt.submitted:
        with suppress(Exception):
            owned_run = await lookup_native_run(client, binding, session_id, input_value, timeout_seconds=10.0)
            if owned_run is not None:
                run_id = str(owned_run["run_id"])
    await _best_effort_cleanup(client, session_id, binding, run_id or attempt.run_id)


async def _run_selected_scenarios(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    selected: tuple[Scenario, ...],
    binding: BindingEvidence,
) -> tuple[list[RunEvidence], int]:
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
        evidence = list(await asyncio.gather(*tasks))
        return evidence, await _server_observed_concurrency(client, evidence)
    finally:
        # 部分场景失败时先完成兄弟任务的 Session/run 清理，再删除临时 Agent。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _server_observed_concurrency(
    client: httpx.AsyncClient,
    evidence: list[RunEvidence],
) -> int:
    """从 API 持久化的 started_at/completed_at 计算半开运行区间重叠峰值。"""

    async def fetch(item: RunEvidence) -> JsonObject:
        response = await client.get(f"/api/agent-runs/{item.run_id}")
        response.raise_for_status()
        run = _json_object(response, "并发运行查询")
        _validate_run_identity(
            run,
            item.run_id,
            session_id=item.session_id,
            binding=item.binding,
        )
        return run

    runs = tuple(await asyncio.gather(*(fetch(item) for item in evidence)))
    return observed_run_concurrency(runs)


async def run_live_acceptance(
    args: argparse.Namespace,
    env: EnvValues,
    scenarios: tuple[Scenario, ...],
) -> tuple[list[RunEvidence], int]:
    api_base = (env.get("API_BASE") or "").rstrip("/")
    api_key = env["API_KEY"]
    agent_id = (args.agent_id or "").strip()
    if not (args.technical_integration_seed or args.mcp_technical_seed) and not agent_id:
        raise LiveAcceptanceError("正式验收 Agent ID 不能为空")
    if not api_base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise LiveAcceptanceError("隔离 live 验收只允许访问 runner 生成的本机公开 API")
    selected = select_scenarios(scenarios, args.runs, args.concurrency, capability=args.capability)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    acceptance_identity = env.get("AGENTGOV_ACCEPTANCE_IDENTITY", "").strip()
    if env.get("AGENTGOV_API_MODE") == "acceptance" and acceptance_identity:
        headers["X-AgentGov-Acceptance-Identity"] = acceptance_identity
    timeout = httpx.Timeout(args.timeout_seconds, connect=30.0)
    async with httpx.AsyncClient(
        base_url=api_base,
        headers=headers,
        timeout=timeout,
        trust_env=False,
    ) as client:
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        if args.technical_integration_seed or args.mcp_technical_seed:
            async with temporary_technical_integration_agent(client, mcp_readonly=args.mcp_technical_seed) as activated:
                binding = await _current_agent_binding(client, activated.agent_id)
                if (
                    binding.agent_version_id != activated.agent_version_id
                    or binding.runtime_agent_id != activated.runtime_agent_id
                    or binding.harness_digest != activated.harness_digest
                ):
                    raise LiveAcceptanceError("技术集成 Agent 发布回执与 current Runtime 绑定不一致")
                return await _run_selected_scenarios(client, args, selected, binding)
        binding = await _current_agent_binding(client, agent_id)
        return await _run_selected_scenarios(client, args, selected, binding)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        env = _read_env_file(args.env_file.resolve())
        _require_explicit_live_authorization(env, require_trace_complete=args.require_trace_complete)
        if (args.capability == MCP_READONLY_CAPABILITY) != args.mcp_technical_seed:
            raise LiveAcceptanceError("mcp_readonly capability 必须且只能使用 --mcp-technical-seed")
        expected_agent_id = (
            MCP_TECHNICAL_INTEGRATION_SCOPE
            if args.mcp_technical_seed
            else TECHNICAL_INTEGRATION_SCOPE
            if args.technical_integration_seed
            else str(args.agent_id or "")
        )
        scenario_set = load_scenarios(args.scenario_file, expected_agent_id=expected_agent_id)
        evidence, max_concurrency = asyncio.run(run_live_acceptance(args, env, scenario_set.scenarios))
        validate_evidence_identities(
            expected_runs=args.runs,
            configured_concurrency=args.concurrency,
            max_concurrency_observed=max_concurrency,
            scenario_ids=tuple(item.scenario_id for item in evidence),
            session_ids=tuple(item.session_id for item in evidence),
            run_ids=tuple(item.run_id for item in evidence),
            trace_ids=tuple(item.trace_id for item in evidence),
            reply_ids=tuple(reply_id for item in evidence for reply_id in item.reply_ids),
            expected_capability=args.capability,
            capabilities=tuple(item.capability for item in evidence),
        )
    except httpx.HTTPError as exc:
        print(f"AGENTSCOPE_LIVE_ACCEPTANCE_FAIL: {summarize_http_failure(exc)}", file=sys.stderr)
        return 1
    except (LiveAcceptanceError, TechnicalIntegrationSeedError, OSError, ValueError) as exc:
        print(f"AGENTSCOPE_LIVE_ACCEPTANCE_FAIL: {exc}", file=sys.stderr)
        return 1
    scope = (
        MCP_TECHNICAL_INTEGRATION_SCOPE
        if args.mcp_technical_seed
        else TECHNICAL_INTEGRATION_SCOPE
        if args.technical_integration_seed
        else "published-business-agent-runtime"
    )
    excluded = (
        MCP_TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS
        if args.mcp_technical_seed
        else TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS
        if args.technical_integration_seed
        else ()
    )
    summary = build_live_acceptance_summary(
        acceptance_scope=scope,
        excluded_claims=excluded,
        scenario_file_sha256=scenario_set.sha256,
        acceptance_run_id=os.environ["AGENT_GOV_ACCEPTANCE_RUN_ID"],
        configured_concurrency=args.concurrency,
        max_concurrency_observed=max_concurrency,
        requested_runs=args.runs,
        requested_capability=args.capability,
        evidence=evidence,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
