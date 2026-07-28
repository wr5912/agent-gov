from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.claude_cli_raw_capture import ClaudeCliRawCapture
from app.runtime.claude_runtime_raw_events import (
    ClaudeRuntimeRawEventsBackend,
    PreparedClaudeRuntimeRawEvents,
)
from app.runtime.managed_claude_events import AgentGovControlEvent, ManagedClaudeEvent
from app.runtime.runtime_raw_events import (
    RuntimeRawCaptureUnsupportedError,
    RuntimeRawEventMetadata,
    RuntimeRawEventsRequest,
    RuntimeRawLimitExceededError,
)
from app.runtime.settings import AppSettings
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _base_load_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


def _load_app(monkeypatch, tmp_path, **kwargs):
    return _base_load_app(
        monkeypatch,
        tmp_path,
        requires_web_hitl=False,
        **kwargs,
    )


class _FakePreparedRawEvents:
    def __init__(self, chunks: list[bytes], *, runtime_kind: str = "qwen-code") -> None:
        self.chunks = chunks
        self.metadata = RuntimeRawEventMetadata(
            run_id="run-backend-owned",
            session_id="session-backend-owned",
            agent_id=ORDINARY_TEST_AGENT_ID,
            runtime_kind=runtime_kind,
            native_protocol="cli-stream-json-stdout",
            runtime_version="1.2.3",
        )
        self.closed = False

    async def collect(self) -> bytes:
        return b"".join(self.chunks)

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        self.closed = True


def test_runtime_neutral_raw_endpoint_streams_and_buffers_exact_bytes(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(
        monkeypatch,
        tmp_path,
        api_key="debug-api-key",
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
        raw_events_enabled=True,
    )
    payload = b' {"type":"unknown","dup":1,"dup":2}\r\nraw\x00\xff'
    prepared_runs: list[_FakePreparedRawEvents] = []
    seen: list[tuple[Any, Any]] = []

    async def fake_start(req, *, profile):
        seen.append((req, profile))
        prepared = _FakePreparedRawEvents([payload[:9], payload[9:]])
        prepared_runs.append(prepared)
        return prepared

    monkeypatch.setattr(module.runtime_raw_events_backend, "start", fake_start)
    request = {
        "message": "raw please",
        "agent_id": ORDINARY_TEST_AGENT_ID,
        "metadata": {
            "X-AgentGov-Run-Id": "attacker-run",
            "X-AgentGov-Runtime-Kind": "attacker-runtime",
        },
    }
    auth = {
        "Authorization": "Bearer debug-api-key",
        "Origin": "http://debug-client.test",
    }

    with TestClient(module.app) as client:
        buffered = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers=auth,
            json={**request, "stream": False},
        )
        streamed = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers=auth,
            json={**request, "stream": True},
        )

    for response in (buffered, streamed):
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["cache-control"] == "no-store, no-transform"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-agentgov-run-id"] == "run-backend-owned"
        assert response.headers["x-agentgov-session-id"] == "session-backend-owned"
        assert response.headers["x-agentgov-agent-id"] == ORDINARY_TEST_AGENT_ID
        assert response.headers["x-agentgov-runtime-kind"] == "qwen-code"
        assert response.headers["x-agentgov-execution-origin"] == "managed"
        assert response.headers["x-agentgov-native-protocol"] == "cli-stream-json-stdout"
        assert response.headers["x-agentgov-runtime-version"] == "1.2.3"
        assert response.headers["x-agentgov-raw-fidelity"] == "byte-exact"
        exposed = response.headers["access-control-expose-headers"].lower()
        assert "x-agentgov-run-id" in exposed
        assert "x-agentgov-raw-fidelity" in exposed
    assert buffered.headers["content-length"] == str(len(payload))
    assert len(seen) == 2
    assert all(profile.agent_id == ORDINARY_TEST_AGENT_ID for _, profile in seen)
    assert all(prepared.closed for prepared in prepared_runs)


def test_raw_endpoint_is_feature_gated_authenticated_and_agent_scoped(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(
        monkeypatch,
        tmp_path,
        api_key="debug-api-key",
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
    )
    request = {"message": "raw please", "agent_id": ORDINARY_TEST_AGENT_ID}

    with TestClient(module.app) as client:
        disabled = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers={"Authorization": "Bearer debug-api-key"},
            json=request,
        )
        module.settings.enable_agent_runtime_raw_events = True
        unauthenticated = client.post("/api/debug/agent-runtime/raw-events", json=request)
        unknown = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers={"Authorization": "Bearer debug-api-key"},
            json={**request, "agent_id": "ghost-agent"},
        )
        blank = client.post(
            "/api/debug/agent-runtime/raw-events",
            headers={"Authorization": "Bearer debug-api-key"},
            json={**request, "agent_id": "  "},
        )

    assert disabled.status_code == 403
    assert disabled.json()["error_code"] == "AGENT_RUNTIME_RAW_EVENTS_DISABLED"
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error_code"] == "UNAUTHORIZED"
    assert unknown.status_code == 404
    assert blank.status_code == 422


def test_enabling_raw_endpoint_without_api_key_fails_application_startup(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(
        monkeypatch,
        tmp_path,
        api_key="",
        raw_events_enabled=True,
    )

    with pytest.raises(RuntimeError, match="requires a non-empty API_KEY"):
        with TestClient(module.app):
            pass


class _FakeCapture:
    def __init__(self, chunks: list[bytes], *, block_after_chunks: bool = False) -> None:
        self.chunks = list(chunks)
        self.block_after_chunks = block_after_chunks
        self.closed = False
        self._release = asyncio.Event()
        self.wrapper_path = Path("/tmp/fake-agentgov-raw-wrapper")
        self.runtime_version = "2.3.4"

    async def wait_connected(self) -> None:
        return None

    async def read(self, _size: int = 64 * 1024) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.block_after_chunks:
            await self._release.wait()
        return b""

    async def aclose(self) -> None:
        self.closed = True
        self._release.set()


async def _runtime_events(*events: dict[str, Any]) -> AsyncIterator[ManagedClaudeEvent]:
    for event in events:
        await asyncio.sleep(0)
        data = event.get("data")
        yield AgentGovControlEvent(
            name=str(event["event"]),
            data=data if isinstance(data, dict) else {},
        )


def _metadata() -> RuntimeRawEventMetadata:
    return RuntimeRawEventMetadata(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        runtime_kind="claude-code",
        native_protocol="cli-stream-json-stdout",
        runtime_version="2.0.0",
    )


def test_prepared_raw_events_collects_one_exact_source_and_enforces_limit() -> None:
    payload = b"first\r\n\x00\xffsecond"

    async def exact() -> None:
        capture = _FakeCapture([payload[:5], payload[5:], b""])
        prepared = PreparedClaudeRuntimeRawEvents(
            source=_runtime_events({"event": "done", "data": "[DONE]"}),
            capture=capture,  # type: ignore[arg-type]
            metadata=_metadata(),
            max_bytes=len(payload),
        )
        await prepared.prepare()
        assert await prepared.collect() == payload
        await prepared.aclose()
        assert capture.closed

    async def over_limit() -> None:
        capture = _FakeCapture([payload, b""])
        prepared = PreparedClaudeRuntimeRawEvents(
            source=_runtime_events({"event": "done", "data": "[DONE]"}),
            capture=capture,  # type: ignore[arg-type]
            metadata=_metadata(),
            max_bytes=len(payload) - 1,
        )
        with pytest.raises(RuntimeRawLimitExceededError):
            await prepared.prepare()
        assert capture.closed

    asyncio.run(exact())
    asyncio.run(over_limit())


def test_prepared_raw_events_returns_structured_error_before_first_byte() -> None:
    async def exercise() -> None:
        capture = _FakeCapture([], block_after_chunks=True)
        prepared = PreparedClaudeRuntimeRawEvents(
            source=_runtime_events(
                {
                    "event": "error",
                    "data": {
                        "error_code": "AGENT_AUTH_REQUIRED",
                        "detail": "configured model route is not authenticated",
                    },
                }
            ),
            capture=capture,  # type: ignore[arg-type]
            metadata=_metadata(),
            max_bytes=1024,
        )
        with pytest.raises(Exception) as exc_info:
            await prepared.prepare()
        assert getattr(exc_info.value, "error_code", None) == "AGENT_AUTH_REQUIRED"
        assert capture.closed

    asyncio.run(exercise())


def test_stream_closes_without_platform_frames_after_first_raw_byte_failure() -> None:
    release_failure = asyncio.Event()
    release_tail = asyncio.Event()

    class CoordinatedCapture(_FakeCapture):
        async def read(self, _size: int = 64 * 1024) -> bytes:
            if self.chunks:
                return self.chunks.pop(0)
            await release_tail.wait()
            return b""

        async def aclose(self) -> None:
            release_tail.set()
            await super().aclose()

    async def failing_runtime() -> AsyncIterator[ManagedClaudeEvent]:
        await release_failure.wait()
        yield AgentGovControlEvent(
            name="error",
            data={"error_code": "RUNTIME_FINALIZATION_FAILED", "detail": "persistence failed"},
        )

    async def exercise() -> None:
        capture = CoordinatedCapture([b'{"type":"assistant"}\n'])
        prepared = PreparedClaudeRuntimeRawEvents(
            source=failing_runtime(),
            capture=capture,  # type: ignore[arg-type]
            metadata=_metadata(),
            max_bytes=1024,
        )
        await prepared.prepare()
        release_failure.set()
        chunks = [chunk async for chunk in prepared.iter_bytes()]
        assert chunks == [b'{"type":"assistant"}\n']
        assert capture.closed

    asyncio.run(exercise())


def test_stream_limit_cancels_blocked_managed_runtime() -> None:
    runtime_release = asyncio.Event()
    runtime_closed = asyncio.Event()

    async def blocked_runtime() -> AsyncIterator[ManagedClaudeEvent]:
        try:
            await runtime_release.wait()
            yield AgentGovControlEvent(name="done", data={})
        finally:
            runtime_closed.set()

    async def exercise() -> None:
        capture = _FakeCapture([b"first", b"over-limit"], block_after_chunks=True)
        prepared = PreparedClaudeRuntimeRawEvents(
            source=blocked_runtime(),
            capture=capture,  # type: ignore[arg-type]
            metadata=_metadata(),
            max_bytes=5,
        )
        await prepared.prepare()

        async def consume() -> list[bytes]:
            return [chunk async for chunk in prepared.iter_bytes()]

        chunks = await asyncio.wait_for(consume(), timeout=1)
        assert chunks == [b"first"]
        assert runtime_closed.is_set()
        assert capture.closed

    asyncio.run(exercise())


def test_claude_backend_uses_execution_local_cli_override_and_managed_stream(monkeypatch, tmp_path: Path) -> None:
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        RUNTIME_VOLUME_MODE="local-debug",
        AGENT_RUNTIME_RAW_EVENTS_MAX_BYTES=1024,
    )
    profile = build_business_agent_profile(
        settings,
        agent_id="agent-1",
        workspace_dir=tmp_path / "workspace",
    )
    capture = _FakeCapture([b"native-stdout\n", b""])
    seen: dict[str, Any] = {}

    async def fake_open(_configured_cli_path):
        return capture

    class FakeRuntime:
        def stream_events(self, req, *, profile, cli_path_override):
            seen.update(
                {
                    "request": req,
                    "profile": profile,
                    "cli_path_override": cli_path_override,
                }
            )
            return _runtime_events(
                {
                    "event": "session",
                    "data": {"run_id": "run-managed", "session_id": "session-managed"},
                },
                {"event": "done", "data": "[DONE]"},
            )

    monkeypatch.setattr(ClaudeCliRawCapture, "open", staticmethod(fake_open))
    backend = ClaudeRuntimeRawEventsBackend(FakeRuntime(), settings)  # type: ignore[arg-type]

    async def exercise() -> None:
        prepared = await backend.start(
            RuntimeRawEventsRequest(message="raw", agent_id="agent-1"),
            profile=profile,
        )
        assert await prepared.collect() == b"native-stdout\n"
        await prepared.aclose()

    asyncio.run(exercise())

    assert seen["profile"] is profile
    assert seen["cli_path_override"] == capture.wrapper_path
    assert seen["request"].message == "raw"
    assert capture.closed


def test_claude_backend_reports_unsupported_host_before_starting_runtime(monkeypatch, tmp_path: Path) -> None:
    import app.runtime.claude_runtime_raw_events as raw_module

    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    profile = build_business_agent_profile(
        settings,
        agent_id="agent-1",
        workspace_dir=tmp_path / "workspace",
    )

    class NeverRuntime:
        def stream(self, *_args, **_kwargs):
            raise AssertionError("unsupported host must fail before starting a managed turn")

    monkeypatch.setattr(raw_module.sys, "platform", "win32")
    backend = ClaudeRuntimeRawEventsBackend(NeverRuntime(), settings)  # type: ignore[arg-type]

    with pytest.raises(RuntimeRawCaptureUnsupportedError):
        asyncio.run(
            backend.start(
                RuntimeRawEventsRequest(message="raw", agent_id="agent-1"),
                profile=profile,
            )
        )
