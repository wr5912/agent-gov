"""容器验收脚本共用的 durable Business Agent 清理客户端。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypedDict

import httpx
from app.runtime.agent_paths import AGENT_ID_PATTERN

JsonObject = dict[str, object]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
AsyncSleeper = Callable[[float], Awaitable[None]]
DeletionHeaders = TypedDict("DeletionHeaders", {"If-Match": str, "Idempotency-Key": str})

_INSTANCE_ETAG = re.compile(r"^[0-9a-f]{64}$")
_AGENT_ID = re.compile(AGENT_ID_PATTERN)
_OPERATION_ID = re.compile(r"^adop-[0-9a-f-]{36}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "agent_instance_etag",
        "device",
        "expected_device",
        "expected_inode",
        "expected_mount_id",
        "inode",
        "mount_id",
        "provision_completed_token",
        "provision_token",
        "quarantine_path",
        "workspace_dir",
    }
)
_FORBIDDEN_VALUE_FRAGMENTS = ("/.agent-deletion-quarantine/", "/data/business-agents/")


class DurableAgentCleanupError(RuntimeError):
    """清理契约失败；消息不得包含服务端响应或私有路径。"""


class SyncDeletionClient(Protocol):
    def delete(self, url: str, *, headers: DeletionHeaders) -> httpx.Response: ...

    def get(self, url: str) -> httpx.Response: ...


class AsyncDeletionClient(Protocol):
    async def delete(self, url: str, *, headers: DeletionHeaders) -> httpx.Response: ...

    async def get(self, url: str) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    operation_id: str
    state: str


def instance_etag_from_import(payload: JsonObject) -> str:
    agent = payload.get("agent")
    etag = agent.get("instance_etag") if isinstance(agent, dict) else None
    if not isinstance(etag, str) or _INSTANCE_ETAG.fullmatch(etag) is None:
        raise DurableAgentCleanupError("Workspace import did not return an exact instance ETag")
    return etag


def delete_business_agent_and_wait(
    client: SyncDeletionClient,
    *,
    agent_id: str,
    instance_etag: str,
    timeout_seconds: float = 45.0,
    poll_seconds: float = 0.2,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> DeletionOutcome:
    headers = _deletion_headers(agent_id, instance_etag)
    response = client.delete(f"/api/agent-registry/{agent_id}", headers=headers)
    outcome = _validate_delete_response(response, agent_id=agent_id, instance_etag=instance_etag)
    if outcome.state == "completed":
        return outcome
    location = _required_location(response, outcome.operation_id)
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        status_response = client.get(location)
        outcome = _validate_status_response(status_response, agent_id=agent_id, instance_etag=instance_etag)
        if outcome.state == "completed":
            return outcome
        sleep(poll_seconds)
    raise DurableAgentCleanupError("Business Agent cleanup did not reach completed before the deadline")


async def async_delete_business_agent_and_wait(
    client: AsyncDeletionClient,
    *,
    agent_id: str,
    instance_etag: str,
    timeout_seconds: float = 45.0,
    poll_seconds: float = 0.2,
    clock: Clock = time.monotonic,
    sleep: AsyncSleeper = asyncio.sleep,
) -> DeletionOutcome:
    headers = _deletion_headers(agent_id, instance_etag)
    response = await client.delete(f"/api/agent-registry/{agent_id}", headers=headers)
    outcome = _validate_delete_response(response, agent_id=agent_id, instance_etag=instance_etag)
    if outcome.state == "completed":
        return outcome
    location = _required_location(response, outcome.operation_id)
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        status_response = await client.get(location)
        outcome = _validate_status_response(status_response, agent_id=agent_id, instance_etag=instance_etag)
        if outcome.state == "completed":
            return outcome
        await sleep(poll_seconds)
    raise DurableAgentCleanupError("Business Agent cleanup did not reach completed before the deadline")


def _deletion_headers(agent_id: str, instance_etag: str) -> DeletionHeaders:
    if _AGENT_ID.fullmatch(agent_id) is None:
        raise DurableAgentCleanupError("Business Agent id is invalid")
    if _INSTANCE_ETAG.fullmatch(instance_etag) is None:
        raise DurableAgentCleanupError("Business Agent instance ETag is invalid")
    idempotency_key = f"agent-delete:{instance_etag}"
    return {"If-Match": f'"{instance_etag}"', "Idempotency-Key": idempotency_key}


def _validate_delete_response(response: httpx.Response, *, agent_id: str, instance_etag: str) -> DeletionOutcome:
    if response.status_code not in {200, 202}:
        raise DurableAgentCleanupError(f"Business Agent deletion returned HTTP {response.status_code}")
    outcome = _operation_outcome(response, agent_id=agent_id, instance_etag=instance_etag)
    expected_state = "completed" if response.status_code == 200 else "cleanup_pending"
    if outcome.state != expected_state:
        raise DurableAgentCleanupError("Business Agent deletion HTTP status and durable state disagree")
    return outcome


def _validate_status_response(response: httpx.Response, *, agent_id: str, instance_etag: str) -> DeletionOutcome:
    if response.status_code != 200:
        raise DurableAgentCleanupError(f"Business Agent deletion status returned HTTP {response.status_code}")
    return _operation_outcome(response, agent_id=agent_id, instance_etag=instance_etag)


def _operation_outcome(response: httpx.Response, *, agent_id: str, instance_etag: str) -> DeletionOutcome:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DurableAgentCleanupError("Business Agent deletion response was not JSON") from exc
    if not isinstance(payload, dict) or _contains_forbidden_evidence(payload, instance_etag=instance_etag):
        raise DurableAgentCleanupError("Business Agent deletion response exposed unsafe or invalid evidence")
    operation_id = payload.get("operation_id")
    state = payload.get("state")
    deleted = payload.get("deleted")
    impact = payload.get("impact")
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise DurableAgentCleanupError("Business Agent deletion response omitted a valid operation id")
    if state not in {"cleanup_pending", "completed"}:
        raise DurableAgentCleanupError("Business Agent deletion response omitted a valid durable state")
    if not isinstance(deleted, dict) or deleted.get("agent_id") != agent_id or not isinstance(impact, dict):
        raise DurableAgentCleanupError("Business Agent deletion response identified the wrong target")
    last_error_code = payload.get("last_error_code")
    attempt_count = payload.get("attempt_count")
    updated_at = payload.get("updated_at")
    if last_error_code is not None and (not isinstance(last_error_code, str) or _ERROR_CODE.fullmatch(last_error_code) is None):
        raise DurableAgentCleanupError("Business Agent deletion response exposed an invalid error code")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
        or not isinstance(updated_at, str)
        or not updated_at
        or len(updated_at) > 128
    ):
        raise DurableAgentCleanupError("Business Agent deletion response omitted bounded status metadata")
    completed = state == "completed"
    if payload.get("workspace_removed") is not completed or payload.get("cleanup_complete") is not completed:
        raise DurableAgentCleanupError("Business Agent deletion receipt disagrees with its durable state")
    return DeletionOutcome(operation_id=operation_id, state=state)


def _required_location(response: httpx.Response, operation_id: str) -> str:
    expected = f"/api/agent-deletion-operations/{operation_id}"
    if response.headers.get("Location") != expected:
        raise DurableAgentCleanupError("Pending Business Agent deletion omitted its exact status location")
    return expected


def _contains_forbidden_evidence(value: object, *, instance_etag: str) -> bool:
    if isinstance(value, dict):
        return any(str(key) in _FORBIDDEN_KEYS or _contains_forbidden_evidence(item, instance_etag=instance_etag) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_evidence(item, instance_etag=instance_etag) for item in value)
    return isinstance(value, str) and (value == instance_etag or any(fragment in value for fragment in _FORBIDDEN_VALUE_FRAGMENTS))
