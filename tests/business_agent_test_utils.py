from __future__ import annotations

import json
from pathlib import Path

ORDINARY_TEST_AGENT_ID = "test-business-agent"
SECONDARY_TEST_AGENT_ID = "secondary-test-business-agent"
LEGACY_MAIN_AGENT_ID = "main-agent"


def create_test_business_agent_workspace(
    workspace: Path,
    *,
    agent_id: str,
    name: str,
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
                    "ask": ["Bash(*)", "Edit(./**)", "Write(./**)"],
                    "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
