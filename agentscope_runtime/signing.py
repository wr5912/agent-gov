"""AgentScope Runtime 调用 AgentGov 内部 API 的 HMAC 签名。"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from typing import TypeAlias

TIMESTAMP_HEADER = "X-AgentGov-Timestamp"
SIGNATURE_HEADER = "X-AgentGov-Signature"
SignedHeaders: TypeAlias = dict[str, str]
TIMESTAMP_TOLERANCE_SECONDS = 60.0


def _valid_signature_fields(timestamp: str | None, signature: str | None, now: float | None) -> bool:
    if timestamp is None or signature is None:
        return False
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", timestamp) is None or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        return False
    observed = float(timestamp)
    current = time.time() if now is None else now
    return math.isfinite(observed) and math.isfinite(current) and abs(current - observed) <= TIMESTAMP_TOLERANCE_SECONDS


def signature_payload(timestamp: str, method: str, path: str, raw_body: bytes) -> bytes:
    """Build the byte-exact shared signing payload."""

    return b"\n".join(
        (
            timestamp.encode("utf-8"),
            method.upper().encode("ascii"),
            path.encode("utf-8"),
            raw_body,
        ),
    )


def signed_headers(
    secret: str,
    method: str,
    path: str,
    raw_body: bytes = b"",
    *,
    timestamp: str | None = None,
) -> SignedHeaders:
    """Return the two headers required by AgentGov internal endpoints."""

    current = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        signature_payload(current, method, path, raw_body),
        hashlib.sha256,
    ).hexdigest()
    return {TIMESTAMP_HEADER: current, SIGNATURE_HEADER: digest}


def verify_signed_request(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    method: str,
    path: str,
    raw_body: bytes,
    *,
    now: float | None = None,
) -> bool:
    if not secret or not _valid_signature_fields(timestamp, signature, now):
        return False
    assert timestamp is not None and signature is not None
    expected = signed_headers(
        secret,
        method,
        path,
        raw_body,
        timestamp=timestamp,
    )[SIGNATURE_HEADER]
    return hmac.compare_digest(expected, signature)


def runtime_gateway_signature_payload(
    timestamp: str,
    user_id: str,
    method: str,
    raw_target: str,
    raw_body: bytes,
) -> bytes:
    """Build the identity- and query-bound Gateway-to-Runtime payload."""

    return b"\n".join(
        (
            timestamp.encode("ascii"),
            user_id.encode("utf-8"),
            method.upper().encode("ascii"),
            raw_target.encode("ascii"),
            raw_body,
        )
    )


def runtime_gateway_headers(
    secret: str,
    user_id: str,
    method: str,
    raw_target: str,
    raw_body: bytes = b"",
    *,
    timestamp: str | None = None,
) -> SignedHeaders:
    current = timestamp or f"{time.time_ns() / 1_000_000_000:.9f}"
    digest = hmac.new(
        secret.encode("utf-8"),
        runtime_gateway_signature_payload(current, user_id, method, raw_target, raw_body),
        hashlib.sha256,
    ).hexdigest()
    return {TIMESTAMP_HEADER: current, SIGNATURE_HEADER: digest}


def verify_runtime_gateway_request(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    user_id: str,
    method: str,
    raw_target: str,
    raw_body: bytes,
    *,
    now: float | None = None,
) -> bool:
    if not secret or not _valid_signature_fields(timestamp, signature, now):
        return False
    assert timestamp is not None and signature is not None
    expected = runtime_gateway_headers(
        secret,
        user_id,
        method,
        raw_target,
        raw_body,
        timestamp=timestamp,
    )[SIGNATURE_HEADER]
    return hmac.compare_digest(expected, signature)
