from __future__ import annotations

import hashlib
import hmac
import math
import re
import time

TIMESTAMP_TOLERANCE_SECONDS = 60


def sign_internal_request(*, secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    canonical = b"\n".join(
        (
            timestamp.encode("ascii"),
            method.upper().encode("ascii"),
            path.encode("utf-8"),
            body,
        )
    )
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def sign_runtime_request(
    *,
    secret: str,
    timestamp: str,
    user_id: str,
    method: str,
    raw_target: str,
    body: bytes,
) -> str:
    """签署 Gateway 到 Runtime 的完整资源选择信息。

    ``raw_target`` 必须是 HTTP 请求线上实际使用的原始 path 与 query，避免
    ``agent_id`` 等 query 参数在签名后被替换；内部用户也被绑定到签名，防止
    未来扩展多个内部身份时跨身份重放。
    """

    canonical = b"\n".join(
        (
            timestamp.encode("ascii"),
            user_id.encode("utf-8"),
            method.upper().encode("ascii"),
            raw_target.encode("ascii"),
            body,
        )
    )
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify_internal_request(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", timestamp) is None or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        return False
    observed = float(timestamp)
    current = time.time() if now is None else now
    if not math.isfinite(observed) or not math.isfinite(current) or abs(current - observed) > TIMESTAMP_TOLERANCE_SECONDS:
        return False
    expected = sign_internal_request(secret=secret, timestamp=timestamp, method=method, path=path, body=body)
    return hmac.compare_digest(expected, signature)
