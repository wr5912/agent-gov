from __future__ import annotations

from typing import TypeVar

from app.runtime.errors import BusinessRuleViolation, ConflictError, NotFoundError


T = TypeVar("T")


def ensure_found(value: T | None, detail: str) -> T:
    if not value:
        raise NotFoundError(detail)
    return value


def require_request(condition: bool, detail: str) -> None:
    if not condition:
        raise BusinessRuleViolation(detail)


def raise_conflict(detail: str) -> None:
    raise ConflictError(detail)
