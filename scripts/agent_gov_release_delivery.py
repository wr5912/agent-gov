from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from agent_gov_release_state import ControllerConfig, ControllerError, StateStore


class MulticaComment(TypedDict):
    content: str


def multica_environment() -> Mapping[str, str]:
    environment = os.environ.copy()
    for credential_name in ("GITHUB_TOKEN", "GH_TOKEN", "CREDENTIALS_DIRECTORY"):
        environment.pop(credential_name, None)
    return environment


def multica_executable() -> str:
    executable = shutil.which("multica")
    if executable is None:
        raise ControllerError("multica CLI is unavailable for release result delivery")
    return executable


def run_multica_json(config: ControllerConfig, arguments: Sequence[str]) -> Any:
    completed = subprocess.run(
        [
            multica_executable(),
            "--profile",
            config.multica_profile,
            *arguments,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=multica_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ControllerError(f"multica {' '.join(arguments)} failed: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError(
            f"multica {' '.join(arguments)} returned invalid JSON"
        ) from exc


def comments_from_payload(payload: object) -> list[MulticaComment]:
    values: list[object]
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("comments"), list):
        values = payload["comments"]
    else:
        values = []
    return [
        {"content": str(value.get("content") or "")}
        for value in values
        if isinstance(value, dict)
    ]


def deliver_comment(
    config: ControllerConfig,
    aid: str,
    marker: str,
    content: str,
) -> None:
    existing = run_multica_json(config, ("issue", "comment", "list", aid))
    if any(
        marker in str(comment.get("content") or "")
        for comment in comments_from_payload(existing)
    ):
        return
    completed = subprocess.run(
        [
            multica_executable(),
            "--profile",
            config.multica_profile,
            "issue",
            "comment",
            "add",
            aid,
            "--content-stdin",
        ],
        input=content,
        check=False,
        capture_output=True,
        text=True,
        env=multica_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ControllerError(f"failed to comment on {aid}: {detail}")


def resolve_release_sre_agent_id(config: ControllerConfig) -> str:
    agents = run_multica_json(config, ("agent", "list"))
    matching = (
        [
            agent
            for agent in agents
            if isinstance(agent, dict)
            and agent.get("name") == config.release_sre_agent
            and agent.get("archived_at") is None
        ]
        if isinstance(agents, list)
        else []
    )
    if len(matching) != 1 or not matching[0].get("id"):
        raise ControllerError(
            f"expected exactly one active {config.release_sre_agent} agent"
        )
    return str(matching[0]["id"])


def activate_release_sre(config: ControllerConfig, aid: str) -> None:
    parent = run_multica_json(config, ("issue", "get", aid))
    if not isinstance(parent, dict):
        raise ControllerError(f"Multica parent {aid} has an unexpected shape")
    metadata = parent.get("metadata")
    child_ref = (
        str(metadata.get(config.release_sre_metadata_key) or "")
        if isinstance(metadata, dict)
        else ""
    )
    if not child_ref:
        raise ControllerError(f"{aid} metadata is missing {config.release_sre_metadata_key}")
    child = run_multica_json(config, ("issue", "get", child_ref))
    if not isinstance(child, dict):
        raise ControllerError(f"release SRE child {child_ref} has an unexpected shape")
    if str(child.get("parent_issue_id") or "") != str(parent.get("id") or ""):
        raise ControllerError(f"{child_ref} is not a child of {aid}")
    assigned_agent = str(child.get("assignee_id") or "")
    if (
        child.get("assignee_type") != "agent"
        or assigned_agent != resolve_release_sre_agent_id(config)
    ):
        raise ControllerError(f"{child_ref} is not assigned to {config.release_sre_agent}")
    status = str(child.get("status") or "")
    if status == "backlog":
        run_multica_json(config, ("issue", "status", child_ref, "todo"))
        return
    if status in {"todo", "in_progress", "done"}:
        return
    raise ControllerError(f"release SRE child {child_ref} is not activatable from {status}")


def flush_outbox(config: ControllerConfig, store: StateStore) -> None:
    for row in store.pending_outbox():
        try:
            payload = json.loads(str(row["payload"]))
            if row["kind"] == "multica_comment":
                deliver_comment(
                    config,
                    str(payload["aid"]),
                    str(payload["marker"]),
                    str(payload["content"]),
                )
            elif row["kind"] == "activate_release_sre":
                dependency = str(payload["comment_key"])
                if not store.outbox_delivered(dependency):
                    raise ControllerError(f"outbox dependency is pending: {dependency}")
                activate_release_sre(config, str(payload["aid"]))
            else:
                raise ControllerError(f"unknown outbox kind: {row['kind']}")
        except (ControllerError, KeyError, json.JSONDecodeError) as exc:
            store.mark_outbox_failed(int(row["id"]), str(exc))
            continue
        store.mark_outbox_delivered(int(row["id"]))
