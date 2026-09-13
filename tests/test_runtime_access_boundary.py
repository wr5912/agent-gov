from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx
import pytest
from agentscope_runtime.access_middleware import FixedRuntimeUserMiddleware
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.receipt_middleware import (
    build_interrupted_receipt,
    post_runtime_receipt,
)
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from agentscope_runtime.signing import runtime_gateway_headers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway._router_operations import _interrupt_active_run
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError, copy_response_headers
from app.runtime_gateway.router import create_internal_runtime_router, reconcile_runtime_gateway
from app.runtime_gateway.store import RuntimeRunStore
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from runtime_loopback import serve_loopback

SECRET = "runtime-access-boundary-test-secret"


@contextmanager
def _running_runtime(tmp_path: Path) -> Iterator[tuple[str, RuntimeRunStore]]:
    control_store = RuntimeRunStore(make_session_factory(tmp_path / "control.db"))
    control_app = FastAPI()
    control_app.include_router(
        create_internal_runtime_router(store=control_store, shared_secret=SECRET),
    )
    with serve_loopback(control_app, lifespan="on") as control_url:
        data_dir = tmp_path / "runtime-data"
        business_root = tmp_path / "business-agents"
        candidates_root = tmp_path / "candidates"
        workspaces_root = tmp_path / "workspaces"
        business_root.mkdir()
        candidates_root.mkdir()
        workspaces_root.mkdir()
        settings = RuntimeSettings(
            shared_secret=SECRET,
            provider_api_key="runtime-boundary-provider-key",
            agentgov_api_base_url=control_url,
            data_dir=data_dir,
            business_agents_root=business_root,
            candidates_root=candidates_root,
            workspaces_root=workspaces_root,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
        )
        with serve_loopback(create_runtime_app(settings), lifespan="on") as runtime_url:
            yield runtime_url, control_store


def test_public_runtime_client_uses_real_agentscope_signed_boundary(tmp_path: Path) -> None:
    with _running_runtime(tmp_path) as (runtime_url, _control_store):

        async def exercise() -> None:
            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                health = await client.request_json("GET", "/health")
                assert health.status_code == 200
                assert isinstance(health.body, dict)
                assert health.body["status"] == "ok"
                assert await client.list_agent_ids_by_name("absent") == []
                runtime_agent_id = await client.create_agent({"name": "signed-boundary"})
                assert await client.list_agent_ids_by_name("signed-boundary") == [runtime_agent_id]
                assert await client.list_session_ids(runtime_agent_id) == []
                await client.delete_agent(runtime_agent_id)
            finally:
                await client.close()

        asyncio.run(exercise())


def test_runtime_boundary_rejects_unsigned_wrong_user_replay_and_management_route(tmp_path: Path) -> None:
    with _running_runtime(tmp_path) as (runtime_url, _control_store):

        async def exercise() -> None:
            async with httpx.AsyncClient(base_url=runtime_url) as http:
                assert (await http.get("/health")).status_code == 401
                signed = {
                    "X-User-ID": RUNTIME_USER_ID,
                    **runtime_gateway_headers(SECRET, RUNTIME_USER_ID, "GET", "/health"),
                }
                assert (await http.get("/health", headers=signed)).status_code == 200
                assert (await http.get("/health", headers=signed)).status_code == 401

            other = AgentScopeRuntimeClient(
                runtime_url,
                user_id="other-user",
                shared_secret=SECRET,
            )
            try:
                with pytest.raises(RuntimeUpstreamError) as wrong_user:
                    await other.request_json("GET", "/health")
                assert wrong_user.value.status_code == 401
            finally:
                await other.close()

            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                with pytest.raises(RuntimeUpstreamError) as forbidden:
                    await client.request_json("GET", "/credential/")
                assert forbidden.value.status_code == 403
            finally:
                await client.close()

        asyncio.run(exercise())


def test_runtime_stream_rejects_real_non_sse_and_preserves_network_failure(tmp_path: Path) -> None:
    with _running_runtime(tmp_path) as (runtime_url, _control_store):

        async def reject_non_sse() -> None:
            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                with pytest.raises(RuntimeUpstreamError) as invalid_stream:
                    await client.start_stream("/health")
                assert invalid_stream.value.status_code == 502
                assert invalid_stream.value.body == b'{"detail":"Runtime returned an invalid SSE response"}'
            finally:
                await client.close()

        asyncio.run(reject_non_sse())

    unavailable = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unavailable.bind(("127.0.0.1", 0))
    port = int(unavailable.getsockname()[1])
    unavailable.close()

    async def reject_unavailable() -> None:
        client = AgentScopeRuntimeClient(
            f"http://127.0.0.1:{port}",
            shared_secret=SECRET,
            timeout_seconds=0.2,
        )
        try:
            with pytest.raises(RuntimeUpstreamError) as failed:
                await client.start_stream("/sessions/missing/stream")
            assert failed.value.status_code == 503
            assert failed.value.body == (b'{"detail":"AgentScope Runtime unavailable","error_code":"RUNTIME_UNAVAILABLE"}')
            assert str(port).encode() not in failed.value.body
        finally:
            await client.close()

    asyncio.run(reject_unavailable())


def test_runtime_response_headers_are_case_insensitively_normalized() -> None:
    assert copy_response_headers(
        {
            "content-type": "text/event-stream; charset=utf-8",
            "CACHE-CONTROL": "no-cache",
            "x-accel-buffering": "no",
            "Connection": "keep-alive",
        },
    ) == {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }


def test_cancel_recovery_uses_real_runtime_404_and_two_quiescent_probes(tmp_path: Path) -> None:
    with _running_runtime(tmp_path) as (runtime_url, store):
        store.bind_agent_version(
            agent_id="agent-missing",
            agent_version_id="version-missing",
            digest="a" * 64,
            runtime_agent_id="runtime-missing",
        )
        store.bind_session(
            session_id="session-missing",
            agent_id="agent-missing",
            agent_version_id="version-missing",
            runtime_agent_id="runtime-missing",
            digest="a" * 64,
        )
        run = store.begin_run(
            session_id="session-missing",
            runtime_agent_id="runtime-missing",
            input_value={"role": "user", "content": []},
            alert_id=None,
            case_id=None,
            metadata={},
        )
        store.mark_trigger_started(run.run_id)
        store.mark_cancel_requested(run.run_id)

        async def recover() -> tuple[int, int, int]:
            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                accepted = await _interrupt_active_run(
                    client,
                    store,
                    store.get_run(run.run_id),
                    primary_session_id=run.session_id,
                )
                first_idle = await reconcile_runtime_gateway(client=client, store=store)
                second_idle = await reconcile_runtime_gateway(client=client, store=store)
                return (
                    accepted.status_code,
                    first_idle.runs_finalized,
                    second_idle.runs_finalized,
                )
            finally:
                await client.close()

        assert asyncio.run(recover()) == (202, 0, 1)
        terminal = store.get_run(run.run_id)
        assert terminal.status.value == "cancelled"
        assert terminal.terminal_reason == "observation_incomplete"
        assert terminal.trace_status == "incomplete"
        assert store.active_run_for_session(run.session_id) is None


def test_interrupted_receipt_crosses_real_signed_control_http_boundary(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "receipt-control.db"))
    store.bind_agent_version(
        agent_id="agent-receipt",
        agent_version_id="version-receipt",
        digest="c" * 64,
        runtime_agent_id="runtime-receipt",
    )
    store.bind_session(
        session_id="session-receipt",
        agent_id="agent-receipt",
        agent_version_id="version-receipt",
        runtime_agent_id="runtime-receipt",
        digest="c" * 64,
    )
    run = store.begin_run(
        session_id="session-receipt",
        runtime_agent_id="runtime-receipt",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.mark_cancel_requested(run.run_id)
    context = RuntimeContext(
        run_id=run.run_id,
        session_id=run.session_id,
        root_session_id=run.session_id,
        role="root",
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        runtime_agent_id=run.runtime_agent_id,
        harness_digest=run.harness_digest,
        trace_id=run.trace_id or "",
        team_generation=0,
    )
    control_app = FastAPI()
    control_app.include_router(
        create_internal_runtime_router(store=store, shared_secret=SECRET),
    )

    with serve_loopback(control_app) as control_url:
        settings = RuntimeSettings(
            shared_secret=SECRET,
            provider_api_key="runtime-receipt-provider-key",
            agentgov_api_base_url=control_url,
            data_dir=tmp_path / "runtime-data",
            business_agents_root=tmp_path / "business",
            candidates_root=tmp_path / "candidates",
            workspaces_root=tmp_path / "workspaces",
            database_url=(f"sqlite+aiosqlite:///{tmp_path / 'runtime-data' / 'agentscope.db'}"),
        )

        async def post_receipt() -> tuple[str, str]:
            async with httpx.AsyncClient(
                base_url=control_url,
                trust_env=False,
            ) as http:
                acknowledgement = await post_runtime_receipt(
                    http,
                    settings,
                    build_interrupted_receipt(context),
                )
                return acknowledgement.run_id, acknowledgement.status

        assert asyncio.run(post_receipt()) == (run.run_id, "running")

    recovering = store.get_run(run.run_id)
    assert recovering.status.value == "running"
    assert recovering.metadata["runtime_interrupted_session_ids"] == [run.session_id]
    assert store.active_run_for_session(run.session_id) is not None


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
    child = (
        "import asyncio,pathlib,runpy,sys;"
        "sys.path.insert(0,str(pathlib.Path(sys.argv[1]).parent));"
        "module=runpy.run_path(sys.argv[1]);"
        "asyncio.run(module['_exercise_real_sse'](sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", child, str(Path(__file__).resolve()), termination],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
