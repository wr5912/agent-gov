#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict


class MulticaError(RuntimeError):
    """Multica CLI is unavailable or rejected the requested operation."""


class MulticaComment(TypedDict):
    content: str


@dataclass(frozen=True)
class MulticaConfig:
    profile: str


def sanitized_environment() -> Mapping[str, str]:
    environment = os.environ.copy()
    for credential_name in ("GITHUB_TOKEN", "GH_TOKEN", "CREDENTIALS_DIRECTORY"):
        environment.pop(credential_name, None)
    return environment


def multica_executable() -> str:
    executable = shutil.which("multica")
    if executable is None:
        raise MulticaError("multica CLI is unavailable")
    return executable


def run_multica_json(config: MulticaConfig, arguments: Sequence[str]) -> Any:
    completed = subprocess.run(
        [
            multica_executable(),
            "--profile",
            config.profile,
            *arguments,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise MulticaError(f"multica {' '.join(arguments)} failed: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MulticaError(f"multica {' '.join(arguments)} returned invalid JSON") from exc


def comments_from_payload(payload: object) -> list[MulticaComment]:
    values: list[object]
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("comments"), list):
        values = payload["comments"]
    else:
        values = []
    return [{"content": str(value.get("content") or "")} for value in values if isinstance(value, dict)]


def deliver_comment(
    config: MulticaConfig,
    *,
    aid: str,
    marker: str,
    content: str,
) -> bool:
    """Deliver one idempotent top-level comment.

    Returns True when a new comment was created and False when the marker was
    already present. The marker check lets a pending SQLite outbox recover from
    a crash after Multica accepted the comment but before local delivery state
    was committed.
    """

    existing = run_multica_json(config, ("issue", "comment", "list", aid, "--full"))
    if any(marker in str(comment.get("content") or "") for comment in comments_from_payload(existing)):
        return False
    completed = subprocess.run(
        [
            multica_executable(),
            "--profile",
            config.profile,
            "issue",
            "comment",
            "add",
            aid,
            "--content-stdin",
            "--output",
            "json",
        ],
        input=content,
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise MulticaError(f"failed to comment on {aid}: {detail}")
    return True
