"""正式验收的原生 chat 边界：真实响应、作用域查询与不重投的失回执恢复。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx
from app.runtime.json_types import JsonObject
from app.runtime_gateway.native_chat_input import explicit_native_input_ids, native_operation_kind

from scripts.agentscope_live_acceptance_report import BindingEvidence
from scripts.agentscope_live_acceptance_scenarios import LiveAcceptanceError


@dataclass
class NativeChatAttempt:
    submitted: bool = False
    run_id: str | None = None
    receipt_received: bool = False


def native_chat_body(runtime_agent_id: str, session_id: str, input_value: dict[str, object]) -> JsonObject:
    return cast(JsonObject, {"agent_id": runtime_agent_id, "session_id": session_id, "input": input_value})


def native_lookup_params(runtime_agent_id: str, session_id: str, input_value: object) -> list[tuple[str, str]]:
    input_ids = explicit_native_input_ids(input_value)
    if input_ids is None:
        raise LiveAcceptanceError("NATIVE_INPUT_ID_REQUIRED")
    return [
        ("agent_id", runtime_agent_id),
        ("session_id", session_id),
        ("operation_kind", native_operation_kind(input_value).value),
        *(("input_id", identifier) for identifier in input_ids),
    ]


def native_receipt_run_id(headers: Mapping[str, str], content: bytes, session_id: str) -> str:
    run_id = headers.get("X-AgentGov-Run-Id", "").strip()
    if not run_id or headers.get("X-AgentGov-Session-Id") != session_id:
        raise LiveAcceptanceError("NATIVE_CHAT_RECEIPT_IDENTITY_INVALID")
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise LiveAcceptanceError("NATIVE_CHAT_RECEIPT_JSON_INVALID") from exc
    if not isinstance(payload, dict) or payload.get("status") != "started" or not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        raise LiveAcceptanceError("NATIVE_CHAT_RECEIPT_SHAPE_INVALID")
    # 原生 body 的 Session 可以是 worker；根归属仅从上方响应头核对。
    return run_id


def require_native_run_identity(run: object, binding: BindingEvidence, session_id: str, run_id: str | None = None) -> JsonObject:
    expected = {
        "agent_id": binding.governance_agent_id,
        "runtime_agent_id": binding.runtime_agent_id,
        "agent_version_id": binding.agent_version_id,
        "session_id": session_id,
        "harness_digest": binding.harness_digest,
    }
    if not isinstance(run, dict) or not isinstance(run.get("run_id"), str) or not run["run_id"]:
        raise LiveAcceptanceError("NATIVE_LOOKUP_RUN_INVALID")
    if any(run.get(key) != value for key, value in expected.items()) or (run_id is not None and run.get("run_id") != run_id):
        raise LiveAcceptanceError("NATIVE_LOOKUP_IDENTITY_MISMATCH")
    return cast(JsonObject, run)


async def lookup_native_run(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    session_id: str,
    input_value: object,
    *,
    timeout_seconds: float,
    run_id: str | None = None,
) -> JsonObject | None:
    response = await client.get(
        "/api/agent-runs/by-input-identity", params=native_lookup_params(binding.runtime_agent_id, session_id, input_value), timeout=timeout_seconds
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise LiveAcceptanceError("NATIVE_LOOKUP_HTTP_FAILED")
    try:
        return require_native_run_identity(response.json(), binding, session_id, run_id)
    except ValueError as exc:
        raise LiveAcceptanceError("NATIVE_LOOKUP_JSON_INVALID") from exc


async def recover_native_run(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    session_id: str,
    input_value: object,
    *,
    timeout_seconds: float,
) -> JsonObject:
    deadline = asyncio.get_running_loop().time() + min(timeout_seconds, 15.0)
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise LiveAcceptanceError("NATIVE_CHAT_RECEIPT_UNCONFIRMED")
        try:
            run = await lookup_native_run(client, binding, session_id, input_value, timeout_seconds=remaining)
        except httpx.RequestError:
            run = None
        if run is not None:
            return run
        await asyncio.sleep(min(0.25, remaining))


async def submit_native_chat(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    session_id: str,
    input_value: dict[str, object],
    attempt: NativeChatAttempt,
    *,
    timeout_seconds: float,
) -> str:
    native_lookup_params(binding.runtime_agent_id, session_id, input_value)
    attempt.submitted = True
    try:
        response = await client.post("/api/runtime/chat/", json=native_chat_body(binding.runtime_agent_id, session_id, input_value), timeout=timeout_seconds)
    except httpx.RequestError:
        response = None
    if response is not None and response.status_code < 500:
        if response.status_code != 200:
            raise LiveAcceptanceError("NATIVE_CHAT_HTTP_REJECTED")
        attempt.run_id = native_receipt_run_id(response.headers, response.content, session_id)
        attempt.receipt_received = True
    run = await recover_native_run(client, binding, session_id, input_value, timeout_seconds=timeout_seconds)
    verified = require_native_run_identity(run, binding, session_id, attempt.run_id)
    attempt.run_id = str(verified["run_id"])
    return attempt.run_id
