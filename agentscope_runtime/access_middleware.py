"""独立 Runtime 的固定内部用户边界。"""

from __future__ import annotations

import asyncio
import json
import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ._generated_operation_policy import ALLOWED_RUNTIME_OPERATIONS
from .signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, TIMESTAMP_TOLERANCE_SECONDS, verify_runtime_gateway_request

MAX_RUNTIME_REQUEST_BODY_BYTES = 1_048_576
_PATH_PARAMETER = re.compile(r"\{[^/{}]+\}")


def _compile_operation_path(path_template: str) -> re.Pattern[str]:
    cursor = 0
    parts = ["^"]
    for match in _PATH_PARAMETER.finditer(path_template):
        parts.extend((re.escape(path_template[cursor : match.start()]), r"[^/]+"))
        cursor = match.end()
    parts.extend((re.escape(path_template[cursor:]), "$"))
    return re.compile("".join(parts))


_ALLOWED_HTTP = {
    method: tuple(_compile_operation_path(path_template) for candidate_method, path_template in ALLOWED_RUNTIME_OPERATIONS if candidate_method == method)
    for method in {candidate_method for candidate_method, _ in ALLOWED_RUNTIME_OPERATIONS}
}


class FixedRuntimeUserMiddleware:
    """Allow only signed, non-replayed calls on the Gateway data plane."""

    def __init__(self, app: ASGIApp, *, expected_user_id: str, shared_secret: str) -> None:
        self._app = app
        self._expected_user_id = expected_user_id.encode("utf-8")
        self._shared_secret = shared_secret
        self._seen_signatures: dict[str, float] = {}
        self._replay_lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        if not self._is_allowed(method, path):
            await self._reject(send, 403, "Runtime management endpoint is not exposed")
            return
        try:
            declared_length = self._declared_content_length(scope)
        except ValueError:
            await self._reject(send, 400, "Runtime request Content-Length is invalid")
            return
        if declared_length is not None and declared_length > MAX_RUNTIME_REQUEST_BODY_BYTES:
            await self._reject(send, 413, "Runtime request body is too large")
            return
        try:
            raw_body = await self._read_body(receive)
        except _RequestBodyTooLarge:
            await self._reject(send, 413, "Runtime request body is too large")
            return
        if raw_body is None:
            return
        signature = self._verified_signature(scope, method, raw_body)
        if signature is None:
            await self._reject(send, 401, "AgentScope Runtime requires a signed AgentGov request")
            return
        if not await self._accept_once(signature):
            await self._reject(send, 401, "AgentScope Runtime request signature was already used")
            return
        await self._app(scope, self._replay_body(raw_body, receive), send)

    def _verified_signature(self, scope: Scope, method: str, raw_body: bytes) -> str | None:
        timestamp = self._single_header(scope, TIMESTAMP_HEADER.lower().encode())
        signature = self._single_header(scope, SIGNATURE_HEADER.lower().encode())
        user_id = self._single_header(scope, b"x-user-id")
        if user_id != self._expected_user_id or timestamp is None or signature is None:
            return None
        try:
            timestamp_text = timestamp.decode("ascii")
            signature_text = signature.decode("ascii")
            raw_target = self._raw_target(scope)
        except UnicodeError:
            return None
        if not verify_runtime_gateway_request(
            self._shared_secret,
            timestamp_text,
            signature_text,
            self._expected_user_id.decode("utf-8"),
            method,
            raw_target,
            raw_body,
        ):
            return None
        return signature_text

    @staticmethod
    def _single_header(scope: Scope, expected_name: bytes) -> bytes | None:
        values = [value for name, value in scope.get("headers", ()) if name.lower() == expected_name]
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _declared_content_length(scope: Scope) -> int | None:
        values = [value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"]
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("duplicate Content-Length")
        try:
            text = values[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("non-ASCII Content-Length") from exc
        if not text.isdigit():
            raise ValueError("invalid Content-Length")
        return int(text)

    @staticmethod
    def _is_allowed(method: str, path: str) -> bool:
        return any(pattern.fullmatch(path) for pattern in _ALLOWED_HTTP.get(method, ()))

    @staticmethod
    def _raw_target(scope: Scope) -> str:
        raw_path = bytes(scope.get("raw_path") or str(scope.get("path", "")).encode("ascii"))
        query = bytes(scope.get("query_string") or b"")
        target = raw_path + (b"?" + query if query else b"")
        return target.decode("ascii")

    async def _accept_once(self, signature: str) -> bool:
        now = time.time()
        async with self._replay_lock:
            self._seen_signatures = {key: expiry for key, expiry in self._seen_signatures.items() if expiry >= now}
            if signature in self._seen_signatures:
                return False
            # 首次请求可处于未来容忍边界；记忆须覆盖其余完整有效窗口。
            self._seen_signatures[signature] = now + 2 * TIMESTAMP_TOLERANCE_SECONDS
            return True

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_RUNTIME_REQUEST_BODY_BYTES:
                raise _RequestBodyTooLarge
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    def _replay_body(body: bytes, original_receive: Receive) -> Receive:
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if sent:
                # 验签只消费请求体；后续断连和背压仍由原始 ASGI 通道负责。
                return await original_receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    @staticmethod
    async def _reject(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            },
        )
        await send({"type": "http.response.body", "body": body})


class _RequestBodyTooLarge(Exception):
    """ASGI body exceeded the fixed internal Runtime boundary."""
