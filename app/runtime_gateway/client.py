from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeAlias

import httpx

from .security import sign_runtime_request

_RUNTIME_UNAVAILABLE_BODY = b'{"detail":"AgentScope Runtime unavailable","error_code":"RUNTIME_UNAVAILABLE"}'
_STREAM_ESTABLISHMENT_TIMEOUT_BODY = b'{"detail":"Runtime SSE establishment timed out"}'
RuntimeHeaders: TypeAlias = dict[str, str]


class RuntimeUpstreamError(RuntimeError):
    def __init__(self, status_code: int, body: bytes, content_type: str | None = None) -> None:
        super().__init__(f"AgentScope Runtime returned HTTP {status_code}")
        self.status_code = status_code
        self.body = body
        self.content_type = content_type or "application/json"


def canonical_session_id_from_view(value: object) -> str:
    """从原生 SessionView 读取唯一的 AgentScope Session 身份。"""

    session = value.get("session") if isinstance(value, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeUpstreamError(
            502,
            b'{"detail":"Runtime returned invalid Session entry"}',
        )
    return session_id


def canonical_session_locator_from_view(value: object) -> tuple[str, str]:
    """只从原生嵌套对象读取 Session/workspace 身份，不兼容旧字段。"""

    session_id = canonical_session_id_from_view(value)
    session = value.get("session") if isinstance(value, dict) else None
    config = session.get("config") if isinstance(session, dict) else None
    workspace_id = config.get("workspace_id") if isinstance(config, dict) else None
    if not isinstance(workspace_id, str) or not workspace_id:
        raise RuntimeUpstreamError(
            502,
            b'{"detail":"Runtime returned invalid Session entry"}',
        )
    return session_id, workspace_id


@dataclass(frozen=True)
class RuntimeJsonResponse:
    status_code: int
    headers: RuntimeHeaders
    body: Any


class AgentScopeRuntimeClient:
    """唯一生产 Runtime adapter；只调用 AgentScope 公共 HTTP API。"""

    def __init__(
        self,
        base_url: str,
        *,
        user_id: str = "agentgov-runtime",
        shared_secret: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        if not shared_secret:
            raise ValueError("AgentScope Runtime shared secret is required")
        self._shared_secret = shared_secret
        self._last_timestamp_ns = 0
        self._stream_establishment_timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, read=None),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json: object | None = None,
        timeout: float | None = None,
    ) -> RuntimeJsonResponse:
        raw_body = _json_body(json)
        headers = {"Content-Type": "application/json"} if json is not None else None
        request = self._build_signed_request(
            method,
            path,
            params=params,
            body=raw_body,
            headers=headers,
            timeout=timeout,
        )
        try:
            response = await self._client.send(request)
        except httpx.RequestError as exc:
            raise RuntimeUpstreamError(503, _RUNTIME_UNAVAILABLE_BODY) from exc
        if response.status_code >= 400:
            raise RuntimeUpstreamError(response.status_code, response.content, response.headers.get("content-type"))
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError as exc:
                raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid JSON"}') from exc
        return RuntimeJsonResponse(response.status_code, dict(response.headers), body)

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        request = self._build_signed_request(
            "GET",
            path,
            params=params,
            headers={
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
        )
        response: httpx.Response | None = None
        try:
            response = await self._send_stream_request(request)
            await _validate_stream_response(response)
            yield response
        except httpx.RequestError as exc:
            raise RuntimeUpstreamError(503, _RUNTIME_UNAVAILABLE_BODY) from exc
        finally:
            if response is not None:
                await response.aclose()

    async def start_stream(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> httpx.Response:
        request = self._build_signed_request(
            "GET",
            path,
            params=params,
            headers={
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
        )
        response = await self._send_stream_request(request)
        try:
            await _validate_stream_response(response)
        except BaseException:
            await response.aclose()
            raise
        return response

    async def _send_stream_request(self, request: httpx.Request) -> httpx.Response:
        """只限制 HTTP/SSE 建连；响应头到达后不限制长连接读时长。"""

        try:
            async with asyncio.timeout(
                self._stream_establishment_timeout_seconds,
            ):
                return await self._client.send(request, stream=True)
        except TimeoutError as exc:
            raise RuntimeUpstreamError(
                504,
                _STREAM_ESTABLISHMENT_TIMEOUT_BODY,
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeUpstreamError(503, _RUNTIME_UNAVAILABLE_BODY) from exc

    async def create_agent(self, payload: dict[str, object]) -> str:
        response = await self.request_json("POST", "/agent/", json=payload)
        agent_id = response.body.get("agent_id") if isinstance(response.body, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            raise RuntimeUpstreamError(502, b'{"detail":"Runtime did not return agent_id"}')
        return agent_id

    async def delete_agent(self, runtime_agent_id: str) -> None:
        await self.request_json("DELETE", f"/agent/{runtime_agent_id}")

    async def list_agent_ids_by_name(self, name: str) -> list[str]:
        """通过 AgentScope 公共列表 API 定位一次幂等创建的 Agent。"""

        response = await self.request_json("GET", "/agent/")
        values = response.body.get("agents") if isinstance(response.body, dict) else None
        if not isinstance(values, list):
            raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid Agent list"}')
        agent_ids: list[str] = []
        for value in values:
            data = value.get("data") if isinstance(value, dict) else None
            agent_id = value.get("id") if isinstance(value, dict) else None
            agent_name = data.get("name") if isinstance(data, dict) else None
            if not isinstance(agent_id, str) or not agent_id or not isinstance(agent_name, str):
                raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid Agent entry"}')
            if agent_name == name and agent_id not in agent_ids:
                agent_ids.append(agent_id)
        return agent_ids

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        return [session_id for session_id, _workspace_id in await self._list_session_locators(runtime_agent_id)]

    async def list_session_ids_for_workspace(
        self,
        runtime_agent_id: str,
        workspace_id: str,
    ) -> list[str]:
        """按不可变 workspace binding 定位响应丢失后的 Session。"""

        return [
            session_id for session_id, candidate_workspace_id in await self._list_session_locators(runtime_agent_id) if candidate_workspace_id == workspace_id
        ]

    async def _list_session_locators(self, runtime_agent_id: str) -> list[tuple[str, str]]:
        response = await self.request_json(
            "GET",
            "/sessions/",
            params={"agent_id": runtime_agent_id},
        )
        values = response.body.get("sessions") if isinstance(response.body, dict) else None
        if not isinstance(values, list):
            raise RuntimeUpstreamError(502, b'{"detail":"Runtime returned invalid Session list"}')
        locators: list[tuple[str, str]] = []
        for value in values:
            locator = canonical_session_locator_from_view(value)
            if locator not in locators:
                locators.append(locator)
        return locators

    async def delete_session(self, session_id: str, runtime_agent_id: str) -> None:
        await self.request_json(
            "DELETE",
            f"/sessions/{session_id}",
            params={"agent_id": runtime_agent_id},
        )

    def _build_signed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Request:
        if timeout is None:
            request = self._client.build_request(method, path, params=params, content=body, headers=headers)
        else:
            request = self._client.build_request(method, path, params=params, content=body, headers=headers, timeout=timeout)
        raw_target = request.url.raw_path.decode("ascii")
        request.headers.update(self._signed_headers(method, raw_target, body))
        return request

    def _signed_headers(self, method: str, raw_target: str, body: bytes = b"") -> RuntimeHeaders:
        timestamp_ns = max(time.time_ns(), self._last_timestamp_ns + 1)
        self._last_timestamp_ns = timestamp_ns
        timestamp = f"{timestamp_ns / 1_000_000_000:.9f}"
        signature = sign_runtime_request(
            secret=self._shared_secret,
            timestamp=timestamp,
            user_id=self.user_id,
            method=method,
            raw_target=raw_target,
            body=body,
        )
        return {
            "X-User-ID": self.user_id,
            "X-AgentGov-Timestamp": timestamp,
            "X-AgentGov-Signature": signature,
        }


def copy_response_headers(headers: RuntimeHeaders) -> RuntimeHeaders:
    """只转发端到端语义头；不传播 hop-by-hop 连接状态。"""

    canonical_names = {
        "content-type": "Content-Type",
        "cache-control": "Cache-Control",
        "content-language": "Content-Language",
        "etag": "ETag",
        "last-modified": "Last-Modified",
        "x-accel-buffering": "X-Accel-Buffering",
    }
    copied: RuntimeHeaders = {}
    for key, value in headers.items():
        canonical = canonical_names.get(key.lower())
        if canonical is not None:
            copied[canonical] = value
    return copied


async def _validate_stream_response(response: httpx.Response) -> None:
    if response.status_code >= 400:
        body = await response.aread()
        raise RuntimeUpstreamError(
            response.status_code,
            body,
            response.headers.get("content-type"),
        )
    if response.status_code != 200 or _media_type(response.headers.get("content-type")) != "text/event-stream":
        raise RuntimeUpstreamError(
            502,
            b'{"detail":"Runtime returned an invalid SSE response"}',
        )


def _media_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.partition(";")[0].strip().lower()


def _json_body(value: object | None) -> bytes:
    if value is None:
        return b""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
