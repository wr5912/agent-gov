from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
from agentscope_runtime.access_middleware import FixedRuntimeUserMiddleware
from agentscope_runtime.settings import RUNTIME_USER_ID
from agentscope_runtime.signing import runtime_gateway_headers
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

SECRET = "runtime-access-boundary-test-secret"


@pytest.fixture
def runtime_boundary():
    received = []
    downstream = FastAPI()

    @downstream.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    async def respond(request: Request) -> Response:
        received.append(request)
        if request.url.path == "/agent/":
            if request.method == "POST":
                return JSONResponse({"agent_id": "runtime-created"})
            return JSONResponse(
                {
                    "agents": [
                        {"id": "runtime-one", "data": {"name": "published-one"}},
                        {"id": "runtime-two", "data": {"name": "published-two"}},
                        {"id": "runtime-one", "data": {"name": "published-one"}},
                    ]
                }
            )
        if request.url.path == "/sessions/":
            return JSONResponse({"sessions": [{"session": {"id": "session-one", "config": {"workspace_id": "workspace-one"}}}]})
        if request.url.path.endswith("/stream"):
            return Response(b'data: {"type":"REPLY_END"}\n\n', media_type="text/event-stream")
        return JSONResponse({"status": "ok"})

    middleware = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    return httpx.ASGITransport(app=middleware), received


def test_public_runtime_client_agent_and_session_helpers_cross_signed_boundary(runtime_boundary) -> None:
    transport, received = runtime_boundary

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as http:
            client = AgentScopeRuntimeClient("http://runtime.test", shared_secret=SECRET, client=http)
            assert await client.list_agent_ids_by_name("published-one") == ["runtime-one"]
            assert await client.list_agent_ids_by_name("missing-version") == []
            assert await client.create_agent({"name": "published-new"}) == "runtime-created"
            assert await client.list_session_ids("runtime-one") == ["session-one"]
            assert await client.list_session_ids_for_workspace("runtime-one", "workspace-one") == ["session-one"]
            await client.delete_session("session-one", "runtime-one")
            await client.delete_agent("runtime-created")

    asyncio.run(exercise())

    assert len(received) == 7
    assert all(request.headers["X-User-ID"] == RUNTIME_USER_ID for request in received)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("POST", "/sessions/"),
        ("PATCH", "/sessions/session-one"),
        ("POST", "/chat/"),
        ("GET", "/sessions/session-one/messages"),
        ("GET", "/sessions/session-one/status"),
        ("POST", "/sessions/session-one/interrupt"),
    ],
)
def test_gateway_forwarded_public_endpoints_cross_signed_boundary(runtime_boundary, method, path) -> None:
    transport, received = runtime_boundary

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as http:
            client = AgentScopeRuntimeClient("http://runtime.test", shared_secret=SECRET, client=http)
            response = await client.request_json(method, path, params={"agent_id": "runtime-one"}, json={"content": "签名原文"})
            assert response.status_code == 200

    asyncio.run(exercise())
    assert len(received) == 1


def test_runtime_client_stream_methods_cross_signed_boundary(runtime_boundary) -> None:
    transport, received = runtime_boundary

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as http:
            client = AgentScopeRuntimeClient("http://runtime.test", shared_secret=SECRET, client=http)
            path = "/sessions/session-one/stream"
            params = {"agent_id": "runtime-one"}
            async with client.stream(path, params=params) as response:
                assert await response.aread() == b'data: {"type":"REPLY_END"}\n\n'
            response = await client.start_stream(path, params=params)
            assert response.headers["content-type"].startswith("text/event-stream")
            await response.aclose()

    asyncio.run(exercise())
    assert len(received) == 2


def test_agent_list_still_rejects_unsigned_wrong_user_replayed_and_mutated_requests(runtime_boundary) -> None:
    transport, received = runtime_boundary

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as http:
            assert (await http.get("/agent/")).status_code == 401
            other = AgentScopeRuntimeClient("http://runtime.test", user_id="other-user", shared_secret=SECRET, client=http)
            with pytest.raises(RuntimeUpstreamError) as error:
                await other.list_agent_ids_by_name("published-one")
            assert error.value.status_code == 401
            client = AgentScopeRuntimeClient("http://runtime.test", shared_secret=SECRET, client=http)
            assert await client.list_agent_ids_by_name("published-one") == ["runtime-one"]
            signed = dict(received[-1].headers)
            assert (await http.get("/agent/", headers=signed)).status_code == 401
            assert (await http.get("/agent/?user_id=other-user", headers=signed)).status_code == 401
            signed["x-user-id"] = "other-user"
            assert (await http.get("/agent/", headers=signed)).status_code == 401

    asyncio.run(exercise())
    assert len(received) == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/agent"),
        ("GET", "/agent//"),
        ("GET", "/agent/runtime-one"),
        ("GET", "/agent/runtime-one/secret"),
        ("GET", "/credential/"),
        ("GET", "/users/"),
        ("PATCH", "/agent/"),
        ("PUT", "/agent/"),
    ],
)
def test_agent_list_permission_does_not_open_management_routes(runtime_boundary, method, path) -> None:
    transport, received = runtime_boundary

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as http:
            client = AgentScopeRuntimeClient("http://runtime.test", shared_secret=SECRET, client=http)
            with pytest.raises(RuntimeUpstreamError) as error:
                await client.request_json(method, path)
            assert error.value.status_code == 403

    asyncio.run(exercise())
    assert received == []


def _signed_scope(method: str, path: str, body: bytes = b"") -> Scope:
    headers = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(SECRET, RUNTIME_USER_ID, method, path, body),
    }
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
    }


@pytest.mark.parametrize("mutated", [False, True])
def test_signed_chunked_body_replays_once_then_preserves_disconnect(mutated) -> None:
    original = '{"text":"签名正文"}'.encode()
    transmitted = original + b" " if mutated else original
    messages = iter(
        [
            {"type": "http.request", "body": transmitted[:5], "more_body": True},
            {"type": "http.request", "body": transmitted[5:], "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    downstream_messages: list[Message] = []
    response_messages: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        response_messages.append(message)

    async def downstream(scope: Scope, replay: Receive, send: Send) -> None:
        downstream_messages.extend([await replay(), await replay()])
        await Response(status_code=204)(scope, replay, send)

    boundary = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    asyncio.run(boundary(_signed_scope("POST", "/chat/", original), receive, send))
    assert response_messages[0]["status"] == (401 if mutated else 204)
    assert downstream_messages == ([] if mutated else [{"type": "http.request", "body": original, "more_body": False}, {"type": "http.disconnect"}])


@pytest.mark.parametrize("partial_body", [False, True])
def test_disconnect_before_complete_body_does_not_become_signed_empty_request(partial_body) -> None:
    messages: list[Message] = [{"type": "http.disconnect"}]
    if partial_body:
        messages.insert(0, {"type": "http.request", "body": b"partial", "more_body": True})
    inbound = iter(messages)

    async def receive() -> Message:
        return next(inbound)

    async def send(_message: Message) -> None:
        pytest.fail("断开的请求不能伪造 HTTP 响应")

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        pytest.fail("未收完的 body 不能变成已验签空请求")

    boundary = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    asyncio.run(boundary(_signed_scope("POST", "/chat/"), receive, send))


async def _exercise_real_sse(termination: str) -> None:
    """子进程内执行真实 StreamingResponse；父进程防止旧忙循环挂死测试。"""

    inbound: asyncio.Queue[Message] = asyncio.Queue()
    inbound.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    headers_ready, body_ready, release_body = asyncio.Event(), asyncio.Event(), asyncio.Event()
    disconnected, receive_cancelled, source_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    output: list[Message] = []
    frame = b'event: UNKNOWN_NATIVE\r\ndata: {"type":"UNKNOWN_NATIVE","value":1}\r\n\r\n'

    async def receive() -> Message:
        try:
            message = await inbound.get()
        except asyncio.CancelledError:
            receive_cancelled.set()
            raise
        if message["type"] == "http.disconnect":
            disconnected.set()
        return message

    async def source():
        try:
            await release_body.wait()
            yield frame
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async def send(message: Message) -> None:
        output.append(message)
        if message["type"] == "http.response.start":
            headers_ready.set()
        elif message.get("body"):
            body_ready.set()

    async def downstream(scope: Scope, replay: Receive, send: Send) -> None:
        await StreamingResponse(source(), media_type="text/event-stream")(scope, replay, send)

    boundary = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    task = asyncio.create_task(boundary(_signed_scope("GET", "/sessions/session-one/stream"), receive, send))
    try:
        await asyncio.wait_for(headers_ready.wait(), timeout=1)
        assert output[0]["status"] == 200
        assert not body_ready.is_set()  # 新 Session 无事件时也必须先发送响应头。
        await asyncio.wait_for(asyncio.sleep(0), timeout=1)  # 其他任务能够公平调度。
        release_body.set()
        await asyncio.wait_for(body_ready.wait(), timeout=1)
        assert b"".join(item.get("body", b"") for item in output) == frame
        if termination == "disconnect":
            inbound.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, timeout=1)
            assert disconnected.is_set()
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert receive_cancelled.is_set()
        assert source_closed.is_set()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("termination", ["disconnect", "cancel"])
def test_real_sse_headers_fairness_bytes_and_termination(termination) -> None:
    child = "import asyncio,runpy,sys; module=runpy.run_path(sys.argv[1]); asyncio.run(module['_exercise_real_sse'](sys.argv[2]))"
    result = subprocess.run(
        [sys.executable, "-c", child, str(Path(__file__).resolve()), termination],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
