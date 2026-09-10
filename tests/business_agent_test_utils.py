from __future__ import annotations

from pathlib import Path

import yaml

ORDINARY_TEST_AGENT_ID = "test-business-agent"
SECONDARY_TEST_AGENT_ID = "secondary-test-business-agent"
LEGACY_MAIN_AGENT_ID = "main-agent"


def create_test_business_agent_workspace(
    workspace: Path,
    *,
    agent_id: str,
    name: str,
    requires_web_hitl: bool = True,
) -> None:
    """Create the minimum governed AgentScope Harness used by tests."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENT.md").write_text(
        f"# {name}\n\nBusiness Agent ID: `{agent_id}`.\n",
        encoding="utf-8",
    )
    (workspace / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agent": {
                    "id": agent_id,
                    "runtime": "agentscope",
                    "runtime_contract": "agentscope-app/2.0.8",
                    "system_prompt": "AGENT.md",
                },
                "session": {
                    "permission_mode": "default" if requires_web_hitl else "dont_ask",
                    "cwd": ".",
                    "model_profile": "default",
                },
                "workspace_policy": {
                    "fail_closed": True,
                    "immutable_harness": True,
                    "allow_for_run": False,
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
