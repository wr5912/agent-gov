"""Shared types and constructors for domain-owned OpenAPI request examples."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict

OperationKey = tuple[str, str]


class OpenApiExample(TypedDict):
    summary: str
    value: object
    description: NotRequired[str]


@dataclass(frozen=True)
class RequestExampleContract:
    media_type: str
    examples: Mapping[str, OpenApiExample]
    operation_description: str | None = None


def example(summary: str, value: object, *, description: str | None = None) -> OpenApiExample:
    item: OpenApiExample = {"summary": summary, "value": value}
    if description:
        item["description"] = description
    return item
