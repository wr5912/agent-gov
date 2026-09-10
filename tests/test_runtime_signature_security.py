from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Literal

import pytest
from agentscope_runtime.access_middleware import FixedRuntimeUserMiddleware
from agentscope_runtime.settings import RUNTIME_USER_ID
from agentscope_runtime.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, runtime_gateway_headers, verify_runtime_gateway_request, verify_signed_request
from app.runtime_gateway.security import sign_internal_request, sign_runtime_request, verify_internal_request
from starlette.types import Message, Receive, Scope, Send

SECRET = "signature-security-test-secret"
NOW = 1_700_000_000.0
BODY = '{"text":"原始字节"}'.encode()
TARGET = "/sessions/session-one/status?agent_id=runtime-one"
Boundary = Literal["api", "runtime_callback", "runtime_gateway"]


def _signature(boundary: Boundary, timestamp: str) -> str:
    # 独立构造恶意请求的有效 HMAC，证明拒绝来自输入边界而非不匹配的签名。
    fields = [timestamp.encode(), b"POST", TARGET.encode(), BODY]
    if boundary == "runtime_gateway":
        fields.insert(1, RUNTIME_USER_ID.encode())
    return hmac.new(SECRET.encode(), b"\n".join(fields), hashlib.sha256).hexdigest()


def _verify(boundary: Boundary, timestamp: str | None, signature: str | None, *, now: float = NOW) -> bool:
    if boundary == "api":
        return verify_internal_request(secret=SECRET, timestamp=timestamp, signature=signature, method="POST", path=TARGET, body=BODY, now=now)
    if boundary == "runtime_callback":
        return verify_signed_request(SECRET, timestamp, signature, "POST", TARGET, BODY, now=now)
    return verify_runtime_gateway_request(SECRET, timestamp, signature, RUNTIME_USER_ID, "POST", TARGET, BODY, now=now)


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
@pytest.mark.parametrize("timestamp", ["1700000000", "1700000000.123456789", "1699999940", "1700000060"])
def test_signature_verifiers_accept_exact_integer_fractional_and_window_edges(boundary: Boundary, timestamp: str) -> None:
    signature = _signature(boundary, timestamp)
    assert _verify(boundary, timestamp, signature)
    if boundary == "runtime_gateway":
        expected = sign_runtime_request(secret=SECRET, timestamp=timestamp, user_id=RUNTIME_USER_ID, method="POST", raw_target=TARGET, body=BODY)
    else:
        expected = sign_internal_request(secret=SECRET, timestamp=timestamp, method="POST", path=TARGET, body=BODY)
    assert signature == expected


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
@pytest.mark.parametrize(
    "timestamp",
    [
        "nan",
        "NaN",
        "inf",
        "-inf",
        "+inf",
        "Infinity",
        "1" * 400,
        "1699999939.999999",
        "1700000060.000001",
        "-1",
        "-0",
        "+1700000000",
        "1.7e9",
        "1_700_000_000",
        " 1700000000",
        "1700000000\n",
        "1700000000\x00",
        "１７００００００００",
        "١٧٠٠٠٠٠٠٠٠",
        "é",
        "",
        None,
    ],
)
def test_signature_verifiers_reject_invalid_or_stale_timestamp(boundary: Boundary, timestamp: str | None) -> None:
    assert not _verify(boundary, timestamp, _signature(boundary, timestamp or ""))


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
@pytest.mark.parametrize("signature", [None, "", "a" * 63, "a" * 65, "g" * 64, "é" * 64, "a" * 63 + "\n", "Ｆ" * 64])
def test_signature_verifiers_reject_invalid_signature_without_exception(boundary: Boundary, signature: str | None) -> None:
    assert not _verify(boundary, "1700000000", signature)


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_signature_verifiers_fail_closed_for_non_finite_clock(boundary: Boundary, now: float) -> None:
    assert not _verify(boundary, "1700000000", _signature(boundary, "1700000000"), now=now)


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
def test_signature_verifiers_reject_negative_timestamp_even_inside_window(boundary: Boundary) -> None:
    assert not _verify(boundary, "-1", _signature(boundary, "-1"), now=0)
    assert _verify(boundary, "0", _signature(boundary, "0"), now=0)


@pytest.mark.parametrize("boundary", ["api", "runtime_callback", "runtime_gateway"])
def test_signature_verifiers_preserve_timestamp_bytes_and_secret_binding(boundary: Boundary) -> None:
    signature = _signature(boundary, "1700000000.000000000")
    assert not _verify(boundary, "1700000000", signature)
    assert not _verify(boundary, "1700000000.000000000", "0" * 64)


@pytest.mark.parametrize(
    "user_id,method,target,body",
    [
        ("other-user", "POST", TARGET, BODY),
        (RUNTIME_USER_ID, "GET", TARGET, BODY),
        (RUNTIME_USER_ID, "POST", TARGET.replace("runtime-one", "runtime-two"), BODY),
        (RUNTIME_USER_ID, "POST", TARGET.replace("session-one", "session-two"), BODY),
        (RUNTIME_USER_ID, "POST", TARGET, BODY + b" "),
    ],
)
def test_gateway_signature_keeps_identity_method_target_and_body_binding(user_id: str, method: str, target: str, body: bytes) -> None:
    signature = _signature("runtime_gateway", "1700000000")
    assert not verify_runtime_gateway_request(SECRET, "1700000000", signature, user_id, method, target, body, now=NOW)


def _scope() -> Scope:
    headers = {"X-User-ID": RUNTIME_USER_ID, **runtime_gateway_headers(SECRET, RUNTIME_USER_ID, "GET", "/health")}
    return {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
    }


async def _request(boundary: FixedRuntimeUserMiddleware, scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await boundary(scope, receive, send)
    return messages


async def _must_not_forward(_scope: Scope, _receive: Receive, _send: Send) -> None:
    pytest.fail("非法签名不能到达 Runtime")


@pytest.mark.parametrize("header", [TIMESTAMP_HEADER.lower().encode(), SIGNATURE_HEADER.lower().encode(), b"x-user-id"])
@pytest.mark.parametrize("mutation", ["invalid_utf8", "non_ascii_utf8", "duplicate", "missing"])
def test_runtime_boundary_rejects_malformed_or_duplicate_raw_headers_with_401(header: bytes, mutation: str) -> None:
    scope = _scope()
    headers = scope["headers"]
    original = next(value for name, value in headers if name == header)
    if mutation == "duplicate":
        headers.append((header, original))
    else:
        scope["headers"] = [(name, value) for name, value in headers if name != header]
        if mutation != "missing":
            scope["headers"].append((header, b"\xff" if mutation == "invalid_utf8" else "é".encode()))
    boundary = FixedRuntimeUserMiddleware(_must_not_forward, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    assert asyncio.run(_request(boundary, scope))[0]["status"] == 401


@pytest.mark.parametrize("field", ["raw_path", "query_string", "decoded_path"])
def test_runtime_boundary_rejects_non_ascii_raw_target_with_401(field: str) -> None:
    scope = _scope()
    if field == "decoded_path":
        scope["path"] = "/sessions/é/status"
        scope.pop("raw_path")
    else:
        scope[field] = b"\xff"
    boundary = FixedRuntimeUserMiddleware(_must_not_forward, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    assert asyncio.run(_request(boundary, scope))[0]["status"] == 401


@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf", "1", "9999999999"])
def test_runtime_boundary_rejects_valid_hmac_with_invalid_timestamp(timestamp: str) -> None:
    scope = _scope()
    signed = runtime_gateway_headers(SECRET, RUNTIME_USER_ID, "GET", "/health", timestamp=timestamp)
    scope["headers"] = [(b"x-user-id", RUNTIME_USER_ID.encode()), *((name.lower().encode(), value.encode()) for name, value in signed.items())]
    boundary = FixedRuntimeUserMiddleware(_must_not_forward, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    assert asyncio.run(_request(boundary, scope))[0]["status"] == 401


def test_runtime_boundary_still_blocks_concurrent_replay() -> None:
    forwarded: list[Scope] = []

    async def downstream(scope: Scope, _receive: Receive, send: Send) -> None:
        forwarded.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    boundary = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)
    scope = _scope()

    async def exercise() -> None:
        responses = await asyncio.gather(_request(boundary, scope), _request(boundary, scope))
        assert sorted(response[0]["status"] for response in responses) == [204, 401]

    asyncio.run(exercise())
    assert len(forwarded) == 1


def test_runtime_boundary_blocks_future_timestamp_replay_until_signature_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [NOW]
    monkeypatch.setattr("agentscope_runtime.signing.time.time", lambda: clock[0])
    signed = runtime_gateway_headers(SECRET, RUNTIME_USER_ID, "GET", "/health", timestamp=str(NOW + 60))
    scope = _scope()
    scope["headers"] = [(b"x-user-id", RUNTIME_USER_ID.encode()), *((name.lower().encode(), value.encode()) for name, value in signed.items())]
    forwarded: list[Scope] = []

    async def downstream(scope: Scope, _receive: Receive, send: Send) -> None:
        forwarded.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    boundary = FixedRuntimeUserMiddleware(downstream, expected_user_id=RUNTIME_USER_ID, shared_secret=SECRET)

    async def exercise() -> None:
        assert (await _request(boundary, scope))[0]["status"] == 204
        for elapsed in (61, 120):
            clock[0] = NOW + elapsed
            assert verify_runtime_gateway_request(SECRET, signed[TIMESTAMP_HEADER], signed[SIGNATURE_HEADER], RUNTIME_USER_ID, "GET", "/health", b"")
            assert (await _request(boundary, scope))[0]["status"] == 401
        clock[0] = NOW + 121
        assert (await _request(boundary, scope))[0]["status"] == 401

    asyncio.run(exercise())
    assert len(forwarded) == 1
