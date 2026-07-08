"""预置业务 Agent 种子配置契约。"""

from __future__ import annotations

import json
from pathlib import Path

from app.runtime.agent_profiles import build_profiles, discover_seeded_business_agents, seed_business_agent_ids
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_ROOT = REPO_ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents"
REVIEW_AGENT_ID = "security-data-standardization-review"
REVIEW_WORKSPACE = SEED_ROOT / REVIEW_AGENT_ID / "workspace"
AI_SOC_GAP_AGENT_ID = "ai-soc-gap-analyzer"
AI_SOC_GAP_WORKSPACE = SEED_ROOT / AI_SOC_GAP_AGENT_ID / "workspace"


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


def test_ai_soc_gap_analyzer_seed_is_declared() -> None:
    assert AI_SOC_GAP_AGENT_ID in seed_business_agent_ids()
    assert (AI_SOC_GAP_WORKSPACE / "CLAUDE.md").is_file()
    assert (AI_SOC_GAP_WORKSPACE / "agent.yaml").is_file()
    assert (AI_SOC_GAP_WORKSPACE / ".mcp.json").is_file()
    assert (AI_SOC_GAP_WORKSPACE / ".claude" / "settings.json").is_file()
    assert (AI_SOC_GAP_WORKSPACE / ".claude" / "skills" / "ai-soc-gap-analysis" / "SKILL.md").is_file()


def test_ai_soc_gap_analyzer_seed_permissions_are_assessment_only() -> None:
    settings = json.loads((AI_SOC_GAP_WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = settings["permissions"]

    assert permissions.get("ask") == []
    assert "Read(/data/uploads/**)" in permissions["allow"]
    assert "Write(/data/outputs/ai-soc-gap-analyzer/**)" in permissions["allow"]
    for forbidden in (
        "Edit(./**)",
        "Write(./**)",
        "mcp__*__*write*",
        "mcp__*__*update*",
        "mcp__*__*delete*",
        "mcp__*__*execute*",
        "mcp__*__*submit*",
    ):
        assert forbidden in permissions["deny"]


def test_ai_soc_gap_analyzer_seed_config_matches_agent_id_and_contract() -> None:
    agent_yaml = (AI_SOC_GAP_WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    mcp = json.loads((AI_SOC_GAP_WORKSPACE / ".mcp.json").read_text(encoding="utf-8"))
    claude_md = (AI_SOC_GAP_WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    skill = (AI_SOC_GAP_WORKSPACE / ".claude" / "skills" / "ai-soc-gap-analysis" / "SKILL.md").read_text(encoding="utf-8")

    assert f"id: {AI_SOC_GAP_AGENT_ID}" in agent_yaml
    assert f"profile: {AI_SOC_GAP_AGENT_ID}" in agent_yaml
    assert f"/data/business-agents/{AI_SOC_GAP_AGENT_ID}/workspace" in agent_yaml
    assert mcp == {"mcpServers": {}}
    assert "name: ai-soc-gap-analysis" in skill
    assert "overall_maturity" in skill
    assert "backend-owned" in claude_md
    assert "数据接入" in claude_md
    assert "威胁分析" in claude_md
    assert "响应处置" in claude_md


def test_ai_soc_gap_analyzer_seed_bootstraps_into_registry_for_playground(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=REPO_ROOT / "docker" / "runtime-volume-seeds",
    )
    workspace = runtime_root / "data" / "business-agents" / AI_SOC_GAP_AGENT_ID / "workspace"
    assert workspace.is_dir()
    assert any(f"/data/business-agents/{AI_SOC_GAP_AGENT_ID}/workspace/CLAUDE.md" in path for path in result["copied"])

    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    settings = AppSettings()
    profiles = build_profiles(settings)
    for profile in discover_seeded_business_agents(settings):
        profiles.setdefault(profile.name, profile)

    store = AgentRegistryStore(make_session_factory(runtime_root / "data" / "runtime.sqlite3"))
    store.sync_business_agents(profiles, seed_agent_ids=seed_business_agent_ids())

    registered = store.get_agent(AI_SOC_GAP_AGENT_ID)
    assert registered is not None
    assert registered.category == "business"
    assert registered.origin == "seed"
    assert registered.status == "active"
    assert registered.workspace_dir.endswith(f"/data/business-agents/{AI_SOC_GAP_AGENT_ID}/workspace")
