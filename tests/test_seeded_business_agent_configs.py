"""预置业务 Agent 种子配置契约。"""

from __future__ import annotations

import json
from pathlib import Path

from app.runtime.agent_profiles import seed_business_agent_ids

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_ROOT = REPO_ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents"
REVIEW_AGENT_ID = "security-data-standardization-review"
REVIEW_WORKSPACE = SEED_ROOT / REVIEW_AGENT_ID / "workspace"


def test_security_data_standardization_review_seed_is_declared() -> None:
    assert REVIEW_AGENT_ID in seed_business_agent_ids()
    assert (REVIEW_WORKSPACE / "CLAUDE.md").is_file()
    assert (REVIEW_WORKSPACE / "agent.yaml").is_file()
    assert (REVIEW_WORKSPACE / ".mcp.json").is_file()
    assert (REVIEW_WORKSPACE / ".claude" / "settings.json").is_file()


def test_security_data_standardization_review_seed_permissions_are_review_only() -> None:
    settings = json.loads((REVIEW_WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = settings["permissions"]

    assert permissions.get("ask") == []
    assert "mcp__sec-ops-data__*" in permissions["allow"]
    assert "Write(/data/outputs/security-data-standardization-review/**)" in permissions["allow"]
    for forbidden in (
        "mcp__*__*write*",
        "mcp__*__*update*",
        "mcp__*__*delete*",
        "mcp__*__*execute*",
        "mcp__*__*submit*",
        "Edit(./**)",
        "Write(./**)",
    ):
        assert forbidden in permissions["deny"]


def test_security_data_standardization_review_seed_config_matches_agent_id() -> None:
    agent_yaml = (REVIEW_WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    mcp = json.loads((REVIEW_WORKSPACE / ".mcp.json").read_text(encoding="utf-8"))
    skill = (REVIEW_WORKSPACE / ".claude" / "skills" / "security-data-standardization-review" / "SKILL.md").read_text(encoding="utf-8")

    assert f"id: {REVIEW_AGENT_ID}" in agent_yaml
    assert f"profile: {REVIEW_AGENT_ID}" in agent_yaml
    assert f"/data/business-agents/{REVIEW_AGENT_ID}/workspace" in agent_yaml
    assert mcp["mcpServers"]["sec-ops-data"]["url"] == "${MCP_SERVER_URL}"
    assert "name: security-data-standardization-review" in skill
    assert "不直接修改生产规则或图谱" in skill
