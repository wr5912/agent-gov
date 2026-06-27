from __future__ import annotations

_MISSING_SDK_SESSION_MARKER = "No conversation found with session ID"
_SDK_PROCESS_ERROR_STDERR_HINT = "Check stderr output for details"


def _exception_text(exc: BaseException) -> str:
    parts: list[str] = [str(exc)]
    for attr in ("stderr", "error_output", "output", "stdout"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        if isinstance(value, bytes):
            parts.append(value.decode("utf-8", errors="replace"))
        else:
            parts.append(str(value))
    for nested in (exc.__cause__, exc.__context__):
        if nested is not None and nested is not exc:
            parts.append(_exception_text(nested))
    return "\n".join(part for part in parts if part)


def is_missing_sdk_session_error(exc: BaseException) -> bool:
    exception_text = _exception_text(exc)
    return _MISSING_SDK_SESSION_MARKER in exception_text or (
        exc.__class__.__name__ == "ProcessError" and _SDK_PROCESS_ERROR_STDERR_HINT in exception_text
    )
