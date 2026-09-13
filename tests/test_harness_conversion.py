from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agentgov_harness_digest import harness_content_digest  # noqa: E402
from agentscope_runtime.workspace_manager import harness_digest as runtime_harness_digest  # noqa: E402
from app.runtime_gateway.provisioning import agent_payload_from_workspace  # noqa: E402
from app.runtime_gateway.store import harness_digest as gateway_harness_digest  # noqa: E402

from check_agentscope_cutover import (  # noqa: E402
    check_bootstrap,
    check_production_does_not_call_converter,
    check_static_cutover,
    check_workspace,
)
from convert_claude_harness import (  # noqa: E402
    KNOWN_HOOK_DIGESTS,
    ConversionRejectedError,
    convert_workspace,
    tree_digest,
)

BOOTSTRAP_ROOT = ROOT / "docker" / "runtime-bootstrap"
BUSINESS_AGENT_ID = "security-operations-expert"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _legacy_workspace(root: Path, *, agent_id: str = BUSINESS_AGENT_ID) -> Path:
    _write(
        root / "agent.yaml",
        yaml.safe_dump(
            {
                "agent": {
                    "id": agent_id,
                    "name": "安全运营专家",
                    "version": "1.2.3",
                    "language": "zh-CN",
                    "profile": "business",
                },
                "model_profile": "default",
                "paths": {
                    "workspace": f"/data/business-agents/{agent_id}/workspace",
                    "claude_home": f"/data/business-agents/{agent_id}/claude-root/.claude",
                    "data_root": "/data",
                    "sessions": "/data/sessions",
                    "transcripts": "/data/transcripts",
                    "uploads": "/data/uploads",
                    "outputs": "/data/outputs",
                    "agent_memory": "/data/agent-memory",
                },
                "context_config": {"max_tokens": 4096},
                "react_config": {"max_iters": 12},
                "invite_config": {"invitable": False},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write(
        root / "CLAUDE.md",
        "# 安全运营专家\n\n按 .claude/rules/ 中的证据规范工作。\n\n"
        "当用户询问 workspace 配置结构、配置项含义或配置对比时，先用 Read 工具读取当前 workspace 下的 "
        "`CLAUDE.md`、`agent.yaml`、`.mcp.json` 和 `.claude/settings.json`，基于实际文件内容回答，不得仅凭训练知识或泛化格式回答。\n",
    )
    _write(
        root / ".claude" / "settings.json",
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Read(./**)",
                        "Read(/data/uploads/**)",
                        f"Read(/data/outputs/{agent_id}/**)",
                        f"Write(/data/outputs/{agent_id}/**)",
                        f"Bash(mkdir -p /data/outputs/{agent_id}/**)",
                        "Edit(.claude/**)",
                    ],
                    "deny": ["Bash(curl * | sh)"],
                },
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "filesystem": {
                        "allowWrite": [f"/data/outputs/{agent_id}"],
                        "denyRead": ["secrets/**"],
                    },
                    "network": {"allowedDomains": ["security.example"]},
                },
                "hooks": {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _write(
        root / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "support": {
                        "type": "http",
                        "url": "${SUPPORT_MCP_URL}",
                        "headers": {"Authorization": "Bearer ${SUPPORT_MCP_TOKEN}"},
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _write(
        root / ".claude" / "skills" / "triage" / "SKILL.md",
        "---\nname: triage\ndescription: 对安全告警进行证据优先分诊。\n---\n\n先读取证据，再给出结论。\n",
    )
    _write(
        root / ".claude" / "commands" / "hunt.md",
        "---\ndescription: 生成威胁狩猎步骤。\n---\n\n调用 .claude/skills/triage/SKILL.md 后生成步骤。\n",
    )
    _write(
        root / ".claude" / "agents" / "reviewer.md",
        "---\nname: reviewer\ndescription: 复核证据链。\ntools:\n  - Read\n---\n\n只复核已给出的证据。\n",
    )
    _write(root / ".claude" / "rules" / "evidence.md", "所有结论都必须关联证据。\n")
    _write(root / "README.md", "能力位于 `.claude/skills/`，入口为 `CLAUDE.md`。\n")
    _write(root / ".gitignore", "CLAUDE.local.md\n.claude/settings.local.json\n")
    _write(root / ".worktreeinclude", ".claude/**\nCLAUDE.md\n")
    _write(root / "tests" / "README.md", "旧 Harness 合约测试。\n")
    _write(root / "tests" / "test_legacy_contract.py", "def test_legacy_contract():\n    assert True\n")
    return root


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_conversion_is_deterministic_complete_and_source_read_only(tmp_path: Path) -> None:
    source = _legacy_workspace(tmp_path / "legacy")
    source_before = _file_snapshot(source)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_report = convert_workspace(source, first, kind="business")
    second_report = convert_workspace(source, second, kind="business")

    assert _file_snapshot(source) == source_before
    assert first_report == second_report
    assert _file_snapshot(first) == _file_snapshot(second)
    assert first_report["source_coverage_percent"] == 100.0
    assert first_report["rejected_count"] == 0
    assert first_report["source_file_count"] == first_report["mapped_count"] + first_report["retired_count"]
    assert first_report["output_tree_sha256"] == tree_digest(first, excluded=("conversion-report.json",))
    assert check_workspace(first) == []

    manifest = yaml.safe_load((first / "agent.yaml").read_text(encoding="utf-8"))
    harness_digest = manifest["harness"]["content_digest"]
    assert first_report["harness_digest"] == harness_digest
    assert "workspace_id" not in manifest["session"]
    assert manifest["session"]["permission_mode"] == "default"
    assert len(harness_digest) == 64
    assert harness_digest == harness_content_digest(first)
    assert harness_digest == gateway_harness_digest(first)
    assert harness_digest == runtime_harness_digest(first)
    assert not any(path.name in {".claude", ".mcp.json", "CLAUDE.md", "hooks"} for path in first.rglob("*"))

    assert manifest["paths"] == {
        "workspace": "/workspace",
        "data_root": "/workspace/data",
        "sessions": "/workspace/data/sessions",
        "transcripts": "/workspace/data/transcripts",
        "uploads": "/workspace/data",
        "outputs": "/workspace/outputs",
        "agent_memory": "/workspace/data/agent-memory",
    }
    policy = manifest["workspace_policy"]
    assert f"Write(/workspace/outputs/{BUSINESS_AGENT_ID}/**)" in policy["allowed_tools"]
    assert "Read(/workspace/data/**)" in policy["allowed_tools"]
    assert policy["writable_paths"] == [f"/workspace/outputs/{BUSINESS_AGENT_ID}"]
    assert not any("/runtime-data" in rule for rule in policy["allowed_tools"])
    assert not any("/runtime-data" in path for path in policy["writable_paths"])

    prompt = (first / "AGENT.md").read_text(encoding="utf-8")
    assert "当前 AgentScope Session 的 `/workspace` 只保存会话数据与执行产物" in prompt
    assert "先用 Read 工具读取当前 workspace 下" not in prompt


def test_conversion_maps_mcp_credentials_skills_and_subagents(tmp_path: Path) -> None:
    destination = tmp_path / "agentscope"
    convert_workspace(_legacy_workspace(tmp_path / "legacy"), destination, kind="business")

    mcp = json.loads((destination / "mcp" / "support.json").read_text(encoding="utf-8"))
    assert mcp["mcp_config"]["type"] == "http_mcp"
    assert mcp["credential_refs"] == [
        {"env": "SUPPORT_MCP_TOKEN", "path": "mcp_config.headers.Authorization"},
        {"env": "SUPPORT_MCP_URL", "path": "mcp_config.url"},
    ]
    assert mcp["enable_tools"] == []
    assert mcp["enable_resources"] == []
    assert mcp["enable_resource_templates"] == []
    assert (destination / "skills" / "triage" / "SKILL.md").is_file()
    assert (destination / "skills" / "hunt" / "SKILL.md").is_file()
    assert (destination / "subagents" / "reviewer" / "agent.yaml").is_file()
    assert ".claude/" not in (destination / "AGENT.md").read_text(encoding="utf-8")
    manifest = yaml.safe_load((destination / "agent.yaml").read_text(encoding="utf-8"))
    assert {"TeamCreate", "AgentCreate", "TeamSay", "TeamDelete"} <= set(
        manifest["workspace_policy"]["allowed_tools"],
    )


def test_provisioned_agent_prompt_lists_only_exact_versioned_subagent_types(tmp_path: Path) -> None:
    destination = tmp_path / "agentscope"
    convert_workspace(_legacy_workspace(tmp_path / "legacy"), destination, kind="business")
    digest = harness_content_digest(destination)

    payload = agent_payload_from_workspace(destination, display_name="安全运营专家")

    prompt = payload["system_prompt"]
    assert isinstance(prompt, str)
    assert "`TeamCreate`、`AgentCreate`、`TeamSay`" in prompt
    assert f"`reviewer`: `subagent_type=agentgov-{digest}-reviewer`" in prompt
    assert "subagent_type=default" not in prompt


def test_stdio_mcp_is_rejected_by_converter_and_release_checker(tmp_path: Path) -> None:
    source = _legacy_workspace(tmp_path / "legacy")
    mcp_path = source / ".mcp.json"
    payload = json.loads(mcp_path.read_text(encoding="utf-8"))
    payload["mcpServers"]["support"] = {
        "type": "stdio",
        "command": "python",
        "args": ["server.py"],
    }
    mcp_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported MCP transport: stdio"):
        convert_workspace(source, tmp_path / "rejected", kind="business")

    workspace = tmp_path / "workspace"
    convert_workspace(_legacy_workspace(tmp_path / "valid"), workspace, kind="business")
    converted_mcp = workspace / "mcp" / "support.json"
    payload = json.loads(converted_mcp.read_text(encoding="utf-8"))
    payload["mcp_config"] = {"type": "stdio_mcp", "command": "python"}
    converted_mcp.write_text(json.dumps(payload), encoding="utf-8")
    assert any(finding.code == "mcp_contract" for finding in check_workspace(workspace))


def test_unreviewed_hook_fails_closed_without_publishing(tmp_path: Path) -> None:
    source = _legacy_workspace(tmp_path / "legacy")
    _write(source / "hooks" / "unknown.py", "print('not reviewed')\n")
    destination = tmp_path / "agentscope"
    source_digest = tree_digest(source)

    with pytest.raises(ValueError, match="unreviewed Python hook"):
        convert_workspace(source, destination, kind="business")

    assert tree_digest(source) == source_digest
    assert not destination.exists()


def test_unmapped_source_is_reported_and_not_published(tmp_path: Path) -> None:
    source = _legacy_workspace(tmp_path / "legacy")
    _write(source / "unsupported.txt", "没有经过审阅的迁移输入。\n")
    destination = tmp_path / "agentscope"

    with pytest.raises(ConversionRejectedError) as raised:
        convert_workspace(source, destination, kind="business")

    assert raised.value.report["source_coverage_percent"] == 100.0
    assert raised.value.report["rejected_count"] == 1
    assert raised.value.report["entries"][-1]["status"] == "rejected"
    assert not destination.exists()


def test_committed_bootstrap_is_agentscope_only_and_hooks_are_accounted_for() -> None:
    assert check_bootstrap(BOOTSTRAP_ROOT) == []
    assert check_production_does_not_call_converter(ROOT) == []
    assert check_static_cutover(ROOT) == []

    workspace = BOOTSTRAP_ROOT / "business-agents" / BUSINESS_AGENT_ID / "workspace"
    manifest = yaml.safe_load((workspace / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["session"]["permission_mode"] == "default"
    assert manifest["paths"]["workspace"] == "/workspace"
    assert manifest["paths"]["data_root"] == "/workspace/data"
    assert manifest["paths"]["uploads"] == "/workspace/data"
    assert manifest["paths"]["outputs"] == "/workspace/outputs"
    assert not any("/runtime-data" in rule for rule in manifest["workspace_policy"]["allowed_tools"])
    assert not any("/runtime-data" in path for path in manifest["workspace_policy"]["writable_paths"])
    assert [middleware["type"] for middleware in manifest["runtime_middlewares"]] == [
        "policy_guard",
        "tool_audit",
        "system_prompt_context",
    ]
    report = json.loads((workspace / "conversion-report.json").read_text(encoding="utf-8"))
    hook_entries = {Path(entry["source_path"]).name: entry for entry in report["entries"] if entry["source_path"].startswith("hooks/")}
    assert set(hook_entries) == set(KNOWN_HOOK_DIGESTS)
    for name, expected_digest in KNOWN_HOOK_DIGESTS.items():
        entry = hook_entries[name]
        assert entry["source_sha256"] == expected_digest
        assert entry["human_confirmation"] == "approved_by_cutover_plan"
        assert entry["rule"] == f"reviewed_{name}_to_runtime_contract"

    governor = BOOTSTRAP_ROOT / "governor-workspace"
    governor_manifest = yaml.safe_load((governor / "agent.yaml").read_text(encoding="utf-8"))
    assert governor_manifest["paths"] == {
        "workspace": "/workspace",
        "data_root": "/workspace/data",
    }
    assert {"HarnessList", "HarnessRead"} <= set(
        governor_manifest["workspace_policy"]["allowed_tools"],
    )
    governor_prompt = (governor / "AGENT.md").read_text(encoding="utf-8")
    governor_skill = (governor / "skills" / "read-business-agent-config" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    assert "先调用 `HarnessList()`" in governor_prompt
    assert "`HarnessRead(path)`" in governor_prompt
    assert "先调用 `HarnessList()`" in governor_skill
    assert "用 Read/Glob/Grep 直接读该业务 Agent" not in governor_skill


def test_bootstrap_business_agent_matches_available_read_only_mcp_contract() -> None:
    workspace = BOOTSTRAP_ROOT / "business-agents" / BUSINESS_AGENT_ID / "workspace"
    manifest = yaml.safe_load((workspace / "agent.yaml").read_text(encoding="utf-8"))
    mcp = json.loads((workspace / "mcp" / "sec-ops.json").read_text(encoding="utf-8"))
    assert not mcp["mcp_config"].get("headers")
    assert mcp["credential_refs"] == [{"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"}]
    assert mcp["enable_tools"] == [
        "soc_api__dashboard_summary_api_v1_dashboard_summary_get",
        "soc_api__list_alerts_api_v1_alerts_get",
        "soc_api__list_assets_api_v1_assets_get",
        "soc_api__list_detection_findings_api_external_detection_findings_get",
        "soc_api__list_events_api_v1_events_get",
        "soc_api__list_incidents_api_v1_incidents_get",
        "soc_api__list_indicators_api_v1_indicators_get",
        "soc_api__list_vulnerabilities_api_v1_vulnerabilities_get",
    ]
    assert mcp["enable_resources"] == []
    assert mcp["enable_resource_templates"] == [
        "openapi://soc_api/api/external/detection-findings/{finding_id}/analysis-result",
    ]
    allowed = manifest["workspace_policy"]["allowed_tools"]
    assert {"mcp__sec-ops__resources_list", "mcp__sec-ops__resource_templates_list", "mcp__sec-ops__resource_read"} <= set(allowed)
    assert all(f"mcp__sec-ops__{name}" in allowed for name in mcp["enable_tools"])
    assert not any(any(character in name.partition("(")[0] for character in "*?[") for name in allowed if name.startswith("mcp__"))
    for subagent_manifest in sorted((workspace / "subagents").glob("*/agent.yaml")):
        subagent = yaml.safe_load(subagent_manifest.read_text(encoding="utf-8"))
        subagent_allowed = subagent["workspace_policy"]["allowed_tools"]
        assert "TeamSay" in subagent_allowed
        assert "mcp__sec-ops__soc_api__get_resp_playbooks_recommend" not in subagent_allowed
        assert not any(any(character in name.partition("(")[0] for character in "*?[") for name in subagent_allowed if name.startswith("mcp__"))


def test_bootstrap_retires_legacy_workspace_tests_and_keeps_real_chat_test() -> None:
    workspace = BOOTSTRAP_ROOT / "business-agents" / BUSINESS_AGENT_ID / "workspace"
    assert (workspace / "tests" / "test_live_chat.py").is_file()
    assert not (workspace / "tests" / "test_security_operations_expert_agentscope_harness.py").exists()
    report = json.loads((workspace / "conversion-report.json").read_text(encoding="utf-8"))
    assert report["source_coverage_percent"] == 100.0
    assert (report["mapped_count"], report["retired_count"], report["rejected_count"]) == (21, 3, 0)
    assert {entry["source_path"] for entry in report["entries"] if entry["status"] == "retired"} == {
        "tests/README.md",
        "tests/test_hooks.py",
        "tests/test_native_config.py",
    }


def test_report_tamper_and_production_converter_call_are_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "agentscope"
    convert_workspace(_legacy_workspace(tmp_path / "legacy"), workspace, kind="business")
    report_path = workspace / "conversion-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["harness_digest"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert any(finding.code == "harness_digest" for finding in check_workspace(workspace))

    inspected_repo = tmp_path / "repo"
    _write(inspected_repo / "app" / "start.py", "import convert_claude_harness\n")
    findings = check_production_does_not_call_converter(inspected_repo)
    assert [(finding.path, finding.code) for finding in findings] == [("app/start.py", "production_converter_call")]


def test_known_hook_digests_are_full_sha256_values() -> None:
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in KNOWN_HOOK_DIGESTS.values())


def test_canonical_harness_digest_rejects_broken_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENT.md").symlink_to(workspace / "missing-prompt")

    with pytest.raises(ValueError, match="must not be a symlink"):
        harness_content_digest(workspace)


def test_canonical_harness_digest_covers_manifest_without_self_reference(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = {
        "schema_version": "1",
        "model": {"name": "model-a"},
        "session": {"permission_mode": "default"},
        "workspace_policy": {"allowed_tools": ["Read"]},
        "harness": {"content_digest": "0" * 64},
    }
    (workspace / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    initial = harness_content_digest(workspace)

    manifest["harness"]["content_digest"] = initial
    (workspace / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert harness_content_digest(workspace) == initial

    manifest["session"]["permission_mode"] = "plan"
    (workspace / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert harness_content_digest(workspace) != initial

    manifest["session"]["permission_mode"] = "default"
    manifest["model"]["name"] = "model-b"
    (workspace / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert harness_content_digest(workspace) != initial


def test_canonical_harness_digest_ignores_only_runtime_generated_cache_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_policy.py").write_text("def test_policy():\n    assert True\n", encoding="utf-8")
    initial = harness_content_digest(workspace)

    for cache_name in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache"):
        cache = tests_dir / cache_name
        cache.mkdir()
        (cache / "generated.bin").write_bytes(b"runtime-generated")
    (tests_dir / "test_policy.pyc").write_bytes(b"compiled")
    (tests_dir / "test_policy.pyo").write_bytes(b"optimized")

    assert harness_content_digest(workspace) == initial
    (tests_dir / "test_policy.py").write_text("def test_policy():\n    assert False\n", encoding="utf-8")
    assert harness_content_digest(workspace) != initial


def test_canonical_harness_digest_rejects_excluded_name_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    outside = tmp_path / "outside"
    tests_dir.mkdir(parents=True)
    outside.mkdir()
    (tests_dir / "__pycache__").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="non-regular entry"):
        harness_content_digest(workspace)
