from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from .agent_profiles import AgentRuntimeProfile
from .async_iterators import close_async_iterator
from .claude_cli_raw_capture import ClaudeCliRawCapture
from .claude_runtime import ClaudeRuntime
from .errors import FeedbackStoreError
from .json_types import JsonObject
from .managed_claude_events import AgentGovControlEvent, ManagedClaudeEvent
from .runtime_raw_events import (
    RuntimeRawCaptureUnavailableError,
    RuntimeRawCaptureUnsupportedError,
    RuntimeRawEventMetadata,
    RuntimeRawEventsRequest,
    RuntimeRawLimitExceededError,
    RuntimeRawPreflightError,
)
from .settings import AppSettings

logger = logging.getLogger(__name__)
_CAPTURE_DRAIN_TIMEOUT_SECONDS = 5


class ClaudeRuntimeRawEventsBackend:
    """Expose exact Claude Code stdout while preserving the managed SDK turn."""

    def __init__(self, runtime: ClaudeRuntime, settings: AppSettings) -> None:
        self.runtime = runtime
        self.settings = settings

    async def start(
        self,
        req: RuntimeRawEventsRequest,
        *,
        profile: AgentRuntimeProfile,
    ) -> PreparedClaudeRuntimeRawEvents:
        if not sys.platform.startswith("linux"):
            raise RuntimeRawCaptureUnsupportedError("Raw Runtime capture currently requires Linux and Unix domain sockets")

        capture = await ClaudeCliRawCapture.open(self.settings.claude_cli_path)
        source = self.runtime.stream_events(
            req,
            profile=profile,
            cli_path_override=capture.wrapper_path,
        )
        try:
            session_event = await anext(source)
            session_data = _session_event_data(session_event)
            prepared = PreparedClaudeRuntimeRawEvents(
                source=source,
                capture=capture,
                metadata=RuntimeRawEventMetadata(
                    run_id=str(session_data["run_id"]),
                    session_id=str(session_data["session_id"]),
                    agent_id=profile.agent_id,
                    runtime_kind="claude-code",
                    native_protocol="cli-stream-json-stdout",
                    runtime_version=capture.runtime_version,
                ),
                max_bytes=self.settings.agent_runtime_raw_events_max_bytes,
            )
            await prepared.prepare()
            return prepared
        except BaseException:
            await close_async_iterator(source)
            await capture.aclose()
            raise


class PreparedClaudeRuntimeRawEvents:
    def __init__(
        self,
        *,
        source: AsyncIterator[ManagedClaudeEvent],
        capture: ClaudeCliRawCapture,
        metadata: RuntimeRawEventMetadata,
        max_bytes: int,
    ) -> None:
        self.source = source
        self.capture = capture
        self.metadata = metadata
        self.max_bytes = max_bytes
        self.first_chunk = b""
        self._bytes_seen = 0
        self._drain_task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._capture_eof = False
        self._consumed = False
        self._closed = False

    async def prepare(self) -> None:
        self._drain_task = asyncio.create_task(
            self._drain_runtime_stream(),
            name=f"runtime-raw-drain-{self.metadata.run_id}",
        )
        try:
            await self._wait_for_capture_connection()
            first = await self._read_next(fail_loud=True)
            self.first_chunk = first or b""
        except Exception:
            await self.aclose()
            raise

    async def collect(self) -> bytes:
        self._claim_consumer()
        body = bytearray(self.first_chunk)
        try:
            while not self._capture_eof:
                chunk = await self._read_next(fail_loud=True)
                if chunk is None:
                    break
                body.extend(chunk)
            await self._finish_drain(fail_loud=True)
            return bytes(body)
        except Exception:
            await self.aclose()
            raise

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        self._claim_consumer()
        try:
            if self.first_chunk:
                yield self.first_chunk
            while not self._capture_eof:
                chunk = await self._read_next(fail_loud=False)
                if chunk is None:
                    break
                yield chunk
            await self._finish_drain(fail_loud=False)
        finally:
            await self.aclose()

    async def _wait_for_capture_connection(self) -> None:
        assert self._drain_task is not None
        connection_task = asyncio.create_task(self.capture.wait_connected())
        done, _ = await asyncio.wait(
            {connection_task, self._drain_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._drain_task in done:
            if self._failure is not None:
                connection_task.cancel()
                with suppress(asyncio.CancelledError):
                    await connection_task
                self._raise_failure()
            if not connection_task.done():
                try:
                    await asyncio.wait_for(connection_task, timeout=_CAPTURE_DRAIN_TIMEOUT_SECONDS)
                except TimeoutError as exc:
                    raise RuntimeRawCaptureUnavailableError("Managed Runtime ended without opening the raw capture channel") from exc
        await connection_task

    async def _read_next(self, *, fail_loud: bool) -> bytes | None:
        if self._failure is not None:
            if fail_loud:
                self._raise_failure()
            return None
        assert self._drain_task is not None

        read_task = asyncio.create_task(self.capture.read())
        done, _ = await asyncio.wait(
            {read_task, self._drain_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._drain_task in done and self._failure is not None:
            read_task.cancel()
            with suppress(asyncio.CancelledError):
                await read_task
            if fail_loud:
                self._raise_failure()
            return None
        if read_task not in done:
            try:
                chunk = await asyncio.wait_for(read_task, timeout=_CAPTURE_DRAIN_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                self._failure = RuntimeRawCaptureUnavailableError("Raw capture channel did not close after the managed Runtime ended")
                if fail_loud:
                    raise self._failure from exc
                return None
        else:
            try:
                chunk = read_task.result()
            except Exception as exc:
                self._failure = RuntimeRawCaptureUnavailableError(f"Raw capture channel failed ({exc.__class__.__name__})")
                if fail_loud:
                    raise self._failure from exc
                return None

        if not chunk:
            self._capture_eof = True
            await self._finish_drain(fail_loud=fail_loud)
            return None
        if self._bytes_seen + len(chunk) > self.max_bytes:
            self._failure = RuntimeRawLimitExceededError(f"Raw Runtime stdout exceeded the configured {self.max_bytes}-byte limit")
            if fail_loud:
                raise self._failure
            return None
        self._bytes_seen += len(chunk)
        return chunk

    async def _finish_drain(self, *, fail_loud: bool) -> None:
        if self._drain_task is None:
            return
        if self._failure is not None and not self._drain_task.done():
            self._drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._drain_task
        if self._failure is not None and fail_loud:
            self._raise_failure()

    async def _drain_runtime_stream(self) -> None:
        try:
            async for event in self.source:
                if not isinstance(event, AgentGovControlEvent) or event.name != "error":
                    continue
                self._failure = _platform_failure(event.data)
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc
        finally:
            try:
                await close_async_iterator(self.source)
            except Exception as exc:
                if self._failure is None:
                    self._failure = exc

    def _raise_failure(self) -> None:
        failure = self._failure
        if isinstance(failure, FeedbackStoreError):
            raise failure
        if failure is None:
            raise RuntimeRawCaptureUnavailableError("Raw Runtime capture failed without a diagnostic")
        raise RuntimeRawCaptureUnavailableError(f"Managed Runtime failed before raw output completed ({failure.__class__.__name__})") from failure

    def _claim_consumer(self) -> None:
        if self._consumed:
            raise RuntimeError("Raw Runtime response can only be consumed once")
        self._consumed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._drain_task
        await self.capture.aclose()


def _session_event_data(event: ManagedClaudeEvent) -> JsonObject:
    if not isinstance(event, AgentGovControlEvent) or event.name != "session":
        raise RuntimeRawCaptureUnavailableError("Managed Runtime did not emit its session metadata before execution")
    data = event.data
    if not data.get("run_id") or not data.get("session_id"):
        raise RuntimeRawCaptureUnavailableError("Managed Runtime session metadata is incomplete")
    return data


def _platform_failure(value: Any) -> FeedbackStoreError:
    data = value if isinstance(value, dict) else {}
    error_code = data.get("error_code") if isinstance(data.get("error_code"), str) else None
    detail = data.get("detail") if isinstance(data.get("detail"), str) else None
    errors = data.get("errors")
    if detail is None and isinstance(errors, list):
        detail = next((str(item) for item in errors if item), None)
    return RuntimeRawPreflightError(
        detail or "Managed Runtime failed before native stdout completed",
        error_code=error_code,
    )
