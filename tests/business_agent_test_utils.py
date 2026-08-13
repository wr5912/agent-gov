from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_identity import business_agent_instance_etag
from fastapi.testclient import TestClient

ORDINARY_TEST_AGENT_ID = "test-business-agent"
SECONDARY_TEST_AGENT_ID = "secondary-test-business-agent"
LEGACY_MAIN_AGENT_ID = "main-agent"


def register_test_business_agent_instance(
    session_factory: Any,
    *,
    agent_id: str,
    workspace_dir: str = "/test/business-agent/workspace",
) -> str:
    """Create one public registry authority for isolated runtime-store tests."""

    with session_factory.begin() as db:
        row = db.get(AgentRegistryModel, agent_id)
        if row is None:
            row = AgentRegistryModel(
                agent_id=agent_id,
                name=agent_id,
                category="business",
                workspace_dir=workspace_dir,
                provision_state="ready",
                provision_completed_token=f"test-instance-{agent_id}",
            )
            db.add(row)
            db.flush()
        token = row.provision_completed_token
        if row.deleted_at or row.provision_state != "ready" or not token:
            raise AssertionError(f"test business Agent is not public: {agent_id}")
        return business_agent_instance_etag(token)


def delete_test_business_agent(client: TestClient, agent_id: str):  # type: ignore[no-untyped-def]
    """Call the durable deletion API with the current exact instance CAS."""

    agents = client.get("/api/agent-registry").json()
    current = next((item for item in agents if item.get("agent_id") == agent_id), None)
    instance_etag = str(current["instance_etag"]) if current is not None else "0" * 64
    return client.request(
        "DELETE",
        f"/api/agent-registry/{agent_id}",
        headers={
            "If-Match": f'"{instance_etag}"',
            "Idempotency-Key": f"agent-delete:{instance_etag}",
        },
    )


def create_test_business_agent_workspace(
    workspace: Path,
    *,
    agent_id: str,
    name: str,
    requires_web_hitl: bool = True,
) -> None:
    """Create the minimum Claude-native Business Agent Workspace used by tests."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text(
        f"# {name}\n\nBusiness Agent ID: `{agent_id}`.\n",
        encoding="utf-8",
    )
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "permissions": {
                    "defaultMode": "default",
                    "disableBypassPermissionsMode": "disable",
                    "allow": ["Read(./**)", "Glob", "Grep", "Skill"],
                    "ask": (["Bash(*)", "Edit(./**)", "Write(./**)"] if requires_web_hitl else []),
                    "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
