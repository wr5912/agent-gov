"""预置业务 Agent 种子配置契约。"""

from __future__ import annotations

import json
from pathlib import Path

from app.runtime.agent_profiles import build_profiles, discover_seeded_business_agents, seed_business_agent_ids
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi.testclient import TestClient
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume

from test_api_execution_optimizer import _load_app

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_ROOT = REPO_ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents"
REVIEW_AGENT_ID = "security-data-standardization-review"
REVIEW_WORKSPACE = SEED_ROOT / REVIEW_AGENT_ID / "workspace"
AI_SOC_GAP_AGENT_ID = "ai-soc-gap-analyzer"
AI_SOC_GAP_WORKSPACE = SEED_ROOT / AI_SOC_GAP_AGENT_ID / "workspace"
RESPONSE_DISPOSAL_AGENT_ID = "response-disposal"
RESPONSE_DISPOSAL_WORKSPACE = SEED_ROOT / RESPONSE_DISPOSAL_AGENT_ID / "workspace"
SECOPS_EXPERT_AGENT_ID = "security-operations-expert"
SECOPS_EXPERT_WORKSPACE = SEED_ROOT / SECOPS_EXPERT_AGENT_ID / "workspace"


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


def test_ai_soc_gap_analyzer_seed_routes_through_openai_responses(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "docker" / "volume"
    bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=REPO_ROOT / "docker" / "runtime-volume-seeds",
    )
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}

    async def fake_run(req, *, profile=None, **kwargs):
        captured["req"] = req
        captured["profile"] = profile
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    monkeypatch.setattr(module.runtime, "run", fake_run)

    with TestClient(module.app) as client:
        listed = {agent["agent_id"]: agent for agent in client.get("/api/agent-registry").json()}
        assert AI_SOC_GAP_AGENT_ID in listed
        resp = client.post(
            "/v1/responses",
            json={"input": "评估 AI SOC 差距", "agentgov": {"agent_id": AI_SOC_GAP_AGENT_ID}},
        )
        assert resp.status_code == 200, resp.text

    assert captured["req"].agent_id == AI_SOC_GAP_AGENT_ID
    assert captured["profile"].name == AI_SOC_GAP_AGENT_ID
    assert captured["profile"].category == "business"
    assert captured["profile"].workspace_dir.as_posix().endswith(f"/data/business-agents/{AI_SOC_GAP_AGENT_ID}/workspace")


def test_security_operations_expert_seed_is_declared() -> None:
    assert SECOPS_EXPERT_AGENT_ID in seed_business_agent_ids()
    assert (SECOPS_EXPERT_WORKSPACE / "CLAUDE.md").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / "agent.yaml").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / ".mcp.json").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / ".claude" / "settings.json").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / ".claude" / "skills" / "security-operations-analysis" / "SKILL.md").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / ".claude" / "skills" / "threat-response-disposition" / "SKILL.md").is_file()
    assert (SECOPS_EXPERT_WORKSPACE / ".claude" / "agents" / "response-playbook-planning.md").is_file()


def test_security_operations_expert_fuses_response_disposal_permissions() -> None:
    settings = json.loads((SECOPS_EXPERT_WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = settings["permissions"]

    assert "mcp__sec-ops-data__*" in permissions["allow"]
    assert "mcp__soc-ops-query__*" in permissions["allow"]
    assert "mcp__soc-playbook-query__*" in permissions["allow"]
    assert "mcp__soc-playbook-execution-result-query__*" in permissions["allow"]
    assert "Write(/data/outputs/security-operations-expert/**)" in permissions["allow"]
    assert "Write(/data/outputs/**)" not in permissions["allow"]

    assert "mcp__soc-playbook-execution__*" in permissions["ask"]
    assert "mcp__soc-playbook-registry__*" in permissions["ask"]
    assert "Bash(*)" in permissions["ask"]
    assert "Edit(./**)" in permissions["ask"]

    serialized = json.dumps(settings, ensure_ascii=False)
    assert "response-disposal/claude-root" not in serialized
    assert "Read(/data/business-agents/security-operations-expert/claude-root/.claude.json)" in permissions["deny"]
    assert "Bash(rm -rf /)" in permissions["deny"]


def test_security_operations_expert_config_matches_agent_id_and_response_contract() -> None:
    agent_yaml = (SECOPS_EXPERT_WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    claude_md = (SECOPS_EXPERT_WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    mcp = json.loads((SECOPS_EXPERT_WORKSPACE / ".mcp.json").read_text(encoding="utf-8"))
    response_mcp = json.loads((RESPONSE_DISPOSAL_WORKSPACE / ".mcp.json").read_text(encoding="utf-8"))
    analysis_skill = (SECOPS_EXPERT_WORKSPACE / ".claude" / "skills" / "security-operations-analysis" / "SKILL.md").read_text(encoding="utf-8")
    response_skill = (SECOPS_EXPERT_WORKSPACE / ".claude" / "skills" / "threat-response-disposition" / "SKILL.md").read_text(encoding="utf-8")

    assert f"id: {SECOPS_EXPERT_AGENT_ID}" in agent_yaml
    assert f"profile: {SECOPS_EXPERT_AGENT_ID}" in agent_yaml
    assert f"/data/business-agents/{SECOPS_EXPERT_AGENT_ID}/workspace" in agent_yaml
    assert "alert_triage" in agent_yaml
    assert "response_case_intake" in agent_yaml
    assert "response-playbook-planning" in agent_yaml
    assert mcp == response_mcp

    assert "响应处置部分直接继承并融合" in claude_md
    assert "threat-response-disposition" in claude_md
    assert "response-playbook-builder" in claude_md
    assert "告警分流" in analysis_skill
    assert "真实响应处置交给 threat-response-disposition" in analysis_skill
    assert "整本剧本交给 SOC" in response_skill


def test_hitl_required_deployment_contract_and_low_fixes() -> None:
    from app.runtime.agent_profiles import read_requires_web_hitl

    for workspace in (SECOPS_EXPERT_WORKSPACE, RESPONSE_DISPOSAL_WORKSPACE):
        # 部署契约：agent.yaml 声明 requires_web_hitl 且被 read_requires_web_hitl 识别为 True。
        assert read_requires_web_hitl(workspace) is True
        assert "requires_web_hitl: true" in (workspace / "agent.yaml").read_text(encoding="utf-8")
        settings = json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "skillOverrides" not in settings  # #5：no-op 键已删（门控在 SKILL.md frontmatter）
        assert [h["matcher"] for h in settings["hooks"]["PreToolUse"]] == ["Bash"]  # #6：matcher 收窄
        session_start = (workspace / "hooks" / "session_start.py").read_text(encoding="utf-8")
        assert "hookSpecificOutput" in session_start  # #17：SessionStart 规范包裹
        assert "duration_ms" not in (workspace / "hooks" / "post_tool_audit.py").read_text(encoding="utf-8")  # #18
        # #16：pre_tool_guard 对畸形 stdin fail-closed（try/except 包裹 json.load）
        assert "except Exception:" in (workspace / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")

    # #4：secops 跨 Agent outputs 读收窄到本 Agent 子路径。
    secops_allow = json.loads((SECOPS_EXPERT_WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Read(/data/outputs/**)" not in secops_allow
    assert "Read(/data/outputs/security-operations-expert/**)" in secops_allow


def test_security_operations_expert_seed_bootstraps_into_registry_for_playground(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=REPO_ROOT / "docker" / "runtime-volume-seeds",
    )
    workspace = runtime_root / "data" / "business-agents" / SECOPS_EXPERT_AGENT_ID / "workspace"
    assert workspace.is_dir()
    assert any(f"/data/business-agents/{SECOPS_EXPERT_AGENT_ID}/workspace/CLAUDE.md" in path for path in result["copied"])

    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    settings = AppSettings()
    profiles = build_profiles(settings)
    for profile in discover_seeded_business_agents(settings):
        profiles.setdefault(profile.name, profile)

    store = AgentRegistryStore(make_session_factory(runtime_root / "data" / "runtime.sqlite3"))
    store.sync_business_agents(profiles, seed_agent_ids=seed_business_agent_ids())

    registered = store.get_agent(SECOPS_EXPERT_AGENT_ID)
    assert registered is not None
    assert registered.category == "business"
    assert registered.origin == "seed"
    assert registered.status == "active"
    assert registered.workspace_dir.endswith(f"/data/business-agents/{SECOPS_EXPERT_AGENT_ID}/workspace")


def test_security_operations_expert_seed_routes_through_openai_responses(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "docker" / "volume"
    bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=REPO_ROOT / "docker" / "runtime-volume-seeds",
    )
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}

    async def fake_run(req, *, profile=None, **kwargs):
        captured["req"] = req
        captured["profile"] = profile
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    monkeypatch.setattr(module.runtime, "run", fake_run)

    with TestClient(module.app) as client:
        listed = {agent["agent_id"]: agent for agent in client.get("/api/agent-registry").json()}
        assert SECOPS_EXPERT_AGENT_ID in listed
        resp = client.post(
            "/v1/responses",
            json={"input": "调查高危告警并给出响应处置建议", "agentgov": {"agent_id": SECOPS_EXPERT_AGENT_ID}},
        )
        assert resp.status_code == 200, resp.text

    assert captured["req"].agent_id == SECOPS_EXPERT_AGENT_ID
    assert captured["profile"].name == SECOPS_EXPERT_AGENT_ID
    assert captured["profile"].category == "business"
    assert captured["profile"].workspace_dir.as_posix().endswith(f"/data/business-agents/{SECOPS_EXPERT_AGENT_ID}/workspace")
