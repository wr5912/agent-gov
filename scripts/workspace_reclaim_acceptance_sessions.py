"""Workspace 回收验收的公共 Session 身份、恢复与清理边界。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal, TypedDict
from urllib.parse import quote

import httpx

from scripts.agentscope_live_acceptance_report import BindingEvidence
from scripts.workspace_reclaim_acceptance_runtime import (
    AcceptanceFailure,
    RuntimeMount,
    SessionArtifact,
    WorkspaceReclaimLocator,
    safe_workspace_path,
    validate_artifact,
    wait_reclaimed,
)

SESSION_RECOVERY_TIMEOUT_SECONDS = 210.0
MATERIALIZATION_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class PublicSessionRow:
    session_id: str
    workspace_id: str
    name: str | None


@dataclass
class TrackedSession:
    idempotency_key: str
    name: str
    session_id: str | None = None
    workspace_id: str | None = None


SessionCreateHeaders = TypedDict("SessionCreateHeaders", {"Idempotency-Key": str})


class SessionCreatePayload(TypedDict):
    agent_id: str
    name: str


def session_create_request(
    handle: TrackedSession,
    binding: BindingEvidence,
) -> tuple[SessionCreateHeaders, SessionCreatePayload]:
    return (
        SessionCreateHeaders(**{"Idempotency-Key": handle.idempotency_key}),
        SessionCreatePayload(agent_id=binding.runtime_agent_id, name=handle.name),
    )


def _json_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise AcceptanceFailure("PUBLIC_API_RESPONSE_INVALID") from exc


def session_rows(payload: object) -> tuple[PublicSessionRow, ...]:
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    total = payload.get("total") if isinstance(payload, dict) else None
    if not isinstance(sessions, list) or type(total) is not int or total != len(sessions):
        raise AcceptanceFailure("SESSION_LIST_FAILED")
    rows: list[PublicSessionRow] = []
    for value in sessions:
        session = value.get("session") if isinstance(value, dict) else None
        config = session.get("config") if isinstance(session, dict) else None
        session_id = session.get("id") if isinstance(session, dict) else None
        workspace_id = config.get("workspace_id") if isinstance(config, dict) else None
        name = config.get("name") if isinstance(config, dict) else None
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(workspace_id, str)
            or not workspace_id
            or (name is not None and not isinstance(name, str))
        ):
            raise AcceptanceFailure("SESSION_LIST_FAILED")
        rows.append(PublicSessionRow(session_id, workspace_id, name))
    if len({row.session_id for row in rows}) != len(rows):
        raise AcceptanceFailure("SESSION_LIST_FAILED")
    return tuple(rows)


async def list_public_sessions(client: httpx.AsyncClient, governance_agent_id: str) -> tuple[PublicSessionRow, ...]:
    response = await client.get("/api/runtime/sessions/", params={"governance_agent_id": governance_agent_id})
    if response.status_code in {409, 502, 503, 504}:
        raise AcceptanceFailure("SESSION_LIST_TRANSIENT")
    if response.status_code != 200:
        raise AcceptanceFailure("SESSION_LIST_FAILED")
    return session_rows(_json_payload(response))


def _bind_exact_public_identity(
    handle: TrackedSession,
    rows: tuple[PublicSessionRow, ...],
    response_session_id: str,
    mount: RuntimeMount,
) -> bool:
    named = [row for row in rows if row.name == handle.name]
    if len(named) > 1:
        raise AcceptanceFailure("SESSION_CREATE_IDENTITY_CONFLICT")
    if not named:
        return False
    row = named[0]
    if row.session_id != response_session_id or (handle.session_id is not None and handle.session_id != row.session_id):
        raise AcceptanceFailure("SESSION_CREATE_IDENTITY_CONFLICT")
    if handle.workspace_id is not None and handle.workspace_id != row.workspace_id:
        raise AcceptanceFailure("SESSION_CREATE_IDENTITY_CONFLICT")
    handle.session_id = row.session_id
    handle.workspace_id = row.workspace_id
    safe_workspace_path(mount.root, row.workspace_id)
    return True


async def recover_session(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    handle: TrackedSession,
    mount: RuntimeMount,
    *,
    timeout_seconds: float = SESSION_RECOVERY_TIMEOUT_SECONDS,
) -> TrackedSession:
    """只用稳定 idempotency key、唯一名称和公共列表恢复精确 Session。"""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            headers, payload = session_create_request(handle, binding)
            response = await client.post(
                "/api/runtime/sessions/",
                headers=headers,
                json=payload,
                timeout=120.0,
            )
            if response.status_code in {409, 502, 503, 504}:
                await asyncio.sleep(0.25)
                continue
            if response.status_code not in {200, 201}:
                raise AcceptanceFailure("SESSION_CREATE_FAILED")
            payload = _json_payload(response)
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
            if not isinstance(session_id, str) or not session_id or response.headers.get("X-AgentGov-Session-Id") != session_id:
                raise AcceptanceFailure("SESSION_CREATE_FAILED")
            if handle.session_id is not None and handle.session_id != session_id:
                raise AcceptanceFailure("SESSION_CREATE_IDENTITY_CONFLICT")
            rows = await list_public_sessions(client, binding.governance_agent_id)
            if _bind_exact_public_identity(handle, rows, session_id, mount):
                return handle
        except httpx.RequestError:
            pass
        except AcceptanceFailure as exc:
            if str(exc) != "SESSION_LIST_TRANSIENT":
                raise
        await asyncio.sleep(0.25)
    raise AcceptanceFailure("SESSION_CREATE_RECOVERY_FAILED")


async def create_tracked_session(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    mount: RuntimeMount,
    registry: dict[str, TrackedSession],
    label: str,
) -> TrackedSession:
    nonce = uuid.uuid4().hex
    handle = TrackedSession(
        idempotency_key=f"workspace-reclaim-{nonce}",
        name=f"workspace-reclaim-{label}-{nonce}",
    )
    registry[handle.idempotency_key] = handle
    return await recover_session(client, binding, handle, mount)


async def materialize_session(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    handle: TrackedSession,
    mount: RuntimeMount,
    *,
    timeout_seconds: float = MATERIALIZATION_TIMEOUT_SECONDS,
) -> SessionArtifact:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        await recover_session(client, binding, handle, mount, timeout_seconds=max(1.0, deadline - asyncio.get_running_loop().time()))
        if handle.session_id is None or handle.workspace_id is None:
            raise AcceptanceFailure("SESSION_CREATE_RECOVERY_FAILED")
        try:
            response = await client.get(
                f"/api/runtime/sessions/{quote(handle.session_id, safe='')}/workspace/status",
                params={"agent_id": binding.runtime_agent_id},
                timeout=120.0,
            )
            if response.status_code in {409, 502, 503, 504}:
                await asyncio.sleep(0.25)
                continue
            if response.status_code != 200:
                raise AcceptanceFailure("REAL_VENV_NOT_MATERIALIZED")
            payload = _json_payload(response)
            if not isinstance(payload, dict) or payload.get("available") is not True or payload.get("at_workspace_root") is not True:
                await asyncio.sleep(0.25)
                continue
            await recover_session(client, binding, handle, mount, timeout_seconds=max(1.0, deadline - asyncio.get_running_loop().time()))
            artifact = await asyncio.to_thread(
                validate_artifact,
                mount.root,
                handle.session_id,
                handle.workspace_id,
                binding.harness_digest,
            )
            return artifact
        except httpx.RequestError:
            pass
        except AcceptanceFailure as exc:
            if str(exc) not in {"REAL_VENV_NOT_MATERIALIZED", "WORKSPACE_USAGE_UNSTABLE", "SESSION_WORKSPACE_INVALID"}:
                raise
        await asyncio.sleep(0.25)
    raise AcceptanceFailure("REAL_VENV_NOT_MATERIALIZED")


async def delete_status(client: httpx.AsyncClient, binding: BindingEvidence, session_id: str) -> int:
    try:
        response = await client.delete(
            f"/api/runtime/sessions/{quote(session_id, safe='')}",
            params={"agent_id": binding.runtime_agent_id},
            timeout=60.0,
        )
        return response.status_code
    except httpx.RequestError:
        return 0


def require_public_absence(
    rows: tuple[PublicSessionRow, ...],
    locator: WorkspaceReclaimLocator | SessionArtifact,
    acceptance_name: str | None = None,
) -> tuple[Literal[True], Literal[True]]:
    if any(row.session_id == locator.session_id for row in rows):
        raise AcceptanceFailure("DELETED_SESSION_STILL_LISTED")
    if any(row.workspace_id == locator.workspace_id for row in rows):
        raise AcceptanceFailure("DELETED_WORKSPACE_STILL_REFERENCED")
    if acceptance_name is not None and any(row.name == acceptance_name for row in rows):
        raise AcceptanceFailure("DELETED_SESSION_NAME_STILL_LISTED")
    return True, True


async def public_session_absence(
    client: httpx.AsyncClient,
    governance_agent_id: str,
    locator: WorkspaceReclaimLocator | SessionArtifact,
    acceptance_name: str | None = None,
) -> tuple[Literal[True], Literal[True]]:
    rows = await list_public_sessions(client, governance_agent_id)
    return require_public_absence(rows, locator, acceptance_name)


def _known_locator(handle: TrackedSession, mount: RuntimeMount) -> WorkspaceReclaimLocator | None:
    if handle.session_id is None or handle.workspace_id is None:
        return None
    return WorkspaceReclaimLocator(
        session_id=handle.session_id,
        workspace_id=handle.workspace_id,
        target=safe_workspace_path(mount.root, handle.workspace_id),
    )


async def cleanup_tracked_sessions(
    client: httpx.AsyncClient,
    binding: BindingEvidence,
    mount: RuntimeMount,
    registry: dict[str, TrackedSession],
) -> bool:
    for key, handle in tuple(registry.items()):
        locator = _known_locator(handle, mount)
        try:
            if locator is not None:
                rows = await list_public_sessions(client, binding.governance_agent_id)
                if not any(row.session_id == locator.session_id or row.name == handle.name for row in rows):
                    await wait_reclaimed(mount, locator)
                    require_public_absence(rows, locator, handle.name)
                    registry.pop(key, None)
                    continue
            handle = await recover_session(client, binding, handle, mount)
            locator = _known_locator(handle, mount)
            if locator is None:
                raise AcceptanceFailure("SESSION_CREATE_RECOVERY_FAILED")
            status = await delete_status(client, binding, locator.session_id)
            if status not in {204, 404}:
                continue
            await wait_reclaimed(mount, locator)
            await public_session_absence(client, binding.governance_agent_id, locator, handle.name)
            registry.pop(key, None)
        except (AcceptanceFailure, httpx.HTTPError):
            continue
    return not registry
