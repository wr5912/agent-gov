from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import agentscope_live_acceptance_scenarios as scenario_contract
from scripts import agentscope_mcp_live_acceptance as mcp_live
from scripts import run_agentscope_live_acceptance as live

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/run_agentscope_live_acceptance.py"
REPO_ROOT = SCRIPT_PATH.parents[1]
AGENT_ID = "security-operations-expert"
BROWSER_CONTRACT = REPO_ROOT / "scripts/improvement_ui_e2e/browser_acceptance_contract.mjs"


def _mcp_scenario() -> dict[str, object]:
    return {
        "scenario_id": "reviewed-mcp-readonly",
        "purpose": "mcp_readonly",
        "capability": "mcp_readonly",
        "input": "执行平台 MCP 只读链路验收。",
        "source_ref": "operator-mcp-service-2026-09-12",
        "reviewed_by": "release-operator",
        "reviewed_at": "2026-09-12T10:00:00+08:00",
        "mcp_expectation": {
            "server_name": scenario_contract.MCP_TECHNICAL_SERVER_NAME,
            "allowed_tool_names": [scenario_contract.MCP_TECHNICAL_RAW_TOOL_NAME],
            "unapproved_tool_names": sorted(scenario_contract.MCP_TECHNICAL_UNAPPROVED_RAW_TOOLS),
            "resource_uris": [scenario_contract.MCP_TECHNICAL_RESOURCE_URI],
            "resource_templates": [scenario_contract.MCP_TECHNICAL_RESOURCE_TEMPLATE],
            "read_resource_uri": scenario_contract.MCP_TECHNICAL_RESOURCE_URI,
        },
    }


def _sse(*payloads: object) -> bytes:
    return b"".join(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n" for payload in payloads)


def _write_scenarios(path: Path, *, agent_id: str = AGENT_ID, scenarios: list[dict[str, object]] | None = None) -> Path:
    payload = {
        "agent_id": agent_id,
        "scenarios": scenarios
        or [
            {
                "scenario_id": "reviewed-success",
                "purpose": "success",
                "capability": "generic_runtime",
                "input": "分析这条真实业务输入并给出可核验结论。",
                "source_ref": "operator-session-2026-09-11",
                "reviewed_by": "release-operator",
                "reviewed_at": "2026-09-11T10:00:00+08:00",
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_reviewed_scenario_file_is_external_typed_and_bound_to_agent(tmp_path: Path) -> None:
    path = _write_scenarios(tmp_path / "reviewed.json")
    loaded = live.load_scenarios(path, expected_agent_id=AGENT_ID)
    assert loaded.agent_id == AGENT_ID
    assert loaded.sha256
    assert loaded.scenarios[0].purpose == "success"
    assert loaded.scenarios[0].capability == "generic_runtime"
    assert loaded.scenarios[0].feedback_comment is None


def test_formal_schema_and_real_browser_effect_gate_share_required_literal_contract() -> None:
    schema = json.loads((REPO_ROOT / "config/live_acceptance_scenario.schema.json").read_text(encoding="utf-8"))
    scenario_schema = schema["$defs"]["scenario"]
    acceptance_schema = schema["$defs"]["acceptance"]
    browser_flow = (REPO_ROOT / "scripts/improvement_ui_e2e/real_container_flow.mjs").read_text(encoding="utf-8")

    assert "capability" in scenario_schema["required"]
    assert acceptance_schema["properties"]["required_test_literals"]["minItems"] == 1
    assert "if (!requiredLiterals.length)" in browser_flow
    assert "requiredLiterals.every((literal) => compactBaseline.includes(literal))" in browser_flow
    assert "requiredLiterals.some((literal) => !compactCandidate.includes(literal))" in browser_flow


def test_reviewed_scenario_rejects_agent_mismatch(tmp_path: Path) -> None:
    path = _write_scenarios(tmp_path / "reviewed.json", agent_id="another-agent")
    with pytest.raises(live.LiveAcceptanceError, match="agent_id"):
        live.load_scenarios(path, expected_agent_id=AGENT_ID)


def test_reviewed_scenario_rejects_duplicate_inputs(tmp_path: Path) -> None:
    common = {
        "purpose": "success",
        "capability": "generic_runtime",
        "input": "相同输入",
        "source_ref": "operator-session-2026-09-11",
        "reviewed_by": "release-operator",
        "reviewed_at": "2026-09-11T10:00:00+08:00",
    }
    path = _write_scenarios(
        tmp_path / "reviewed.json",
        scenarios=[{"scenario_id": "one", **common}, {"scenario_id": "two", **common}],
    )
    with pytest.raises(live.LiveAcceptanceError, match="不能重复"):
        live.load_scenarios(path, expected_agent_id=AGENT_ID)


def test_improvement_scenario_requires_feedback_target_paths_and_effect_literal(tmp_path: Path) -> None:
    scenario = {
        "scenario_id": "reviewed-improvement",
        "purpose": "improvement",
        "capability": "improvement_effect",
        "input": "根据真实反馈执行受控改进。",
        "feedback_comment": "该真实回复遗漏必要处置步骤。",
        "source_ref": "operator-session-2026-09-11-improvement",
        "reviewed_by": "release-operator",
        "reviewed_at": "2026-09-11T10:00:00+08:00",
        "acceptance": {
            "allowed_target_paths": ["AGENT.md"],
            "required_test_literals": ["必须升级"],
        },
    }
    path = _write_scenarios(tmp_path / "reviewed.json", scenarios=[scenario])
    loaded = live.load_scenarios(path, expected_agent_id=AGENT_ID)
    assert loaded.scenarios[0].acceptance is not None
    assert loaded.scenarios[0].acceptance.allowed_target_paths == ("AGENT.md",)
    assert loaded.scenarios[0].acceptance.required_test_literals == ("必须升级",)

    scenario["acceptance"] = {"allowed_target_paths": ["AGENT.md"], "required_test_literals": []}
    _write_scenarios(path, scenarios=[scenario])
    with pytest.raises(live.LiveAcceptanceError, match="required_test_literals"):
        live.load_scenarios(path, expected_agent_id=AGENT_ID)


def test_scenario_capability_must_match_purpose(tmp_path: Path) -> None:
    path = _write_scenarios(tmp_path / "reviewed.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scenarios"][0]["capability"] = "runtime_cancel"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(live.LiveAcceptanceError, match="capability"):
        live.load_scenarios(path, expected_agent_id=AGENT_ID)


def test_generic_run_quota_does_not_count_platform_scenarios(tmp_path: Path) -> None:
    base = {
        "input": "真实输入",
        "source_ref": "operator-session-2026-09-11",
        "reviewed_by": "release-operator",
        "reviewed_at": "2026-09-11T10:00:00+08:00",
    }
    path = _write_scenarios(
        tmp_path / "reviewed.json",
        scenarios=[
            {"scenario_id": "generic-one", "purpose": "success", "capability": "generic_runtime", **base},
            {
                "scenario_id": "cancel-one",
                "purpose": "early_cancel",
                "capability": "runtime_cancel",
                **{**base, "input": "不同的取消输入"},
            },
        ],
    )
    loaded = live.load_scenarios(path, expected_agent_id=AGENT_ID)

    with pytest.raises(live.LiveAcceptanceError, match="不得混算"):
        live.select_scenarios(loaded.scenarios, runs=2, concurrency=1)
    selected = live.select_scenarios(
        loaded.scenarios,
        runs=1,
        concurrency=1,
        capability="runtime_cancel",
    )
    assert tuple(item.scenario_id for item in selected) == ("cancel-one",)


def test_improvement_effect_cannot_use_generic_runtime_selector(tmp_path: Path) -> None:
    path = _write_scenarios(
        tmp_path / "reviewed.json",
        scenarios=[
            {
                "scenario_id": "effect-one",
                "purpose": "improvement",
                "capability": "improvement_effect",
                "input": "核查改进效果",
                "feedback_comment": "缺少升级结论",
                "source_ref": "operator-session-2026-09-11",
                "reviewed_by": "release-operator",
                "reviewed_at": "2026-09-11T10:00:00+08:00",
                "acceptance": {
                    "allowed_target_paths": ["AGENT.md"],
                    "required_test_literals": ["必须升级"],
                },
            }
        ],
    )
    loaded = live.load_scenarios(path, expected_agent_id=AGENT_ID)

    with pytest.raises(live.LiveAcceptanceError, match="尚无精确 Runtime 验证器"):
        live.select_scenarios(
            loaded.scenarios,
            runs=1,
            concurrency=1,
            capability="improvement_effect",
        )


def test_mcp_scenario_is_exact_typed_platform_contract(tmp_path: Path) -> None:
    path = _write_scenarios(
        tmp_path / "mcp-reviewed.json",
        agent_id="runtime-mcp-technical-integration-package",
        scenarios=[_mcp_scenario()],
    )

    loaded = live.load_scenarios(path, expected_agent_id="runtime-mcp-technical-integration-package")
    selected = live.select_scenarios(loaded.scenarios, runs=1, concurrency=1, capability="mcp_readonly")

    assert selected[0].mcp_expectation is not None
    assert selected[0].mcp_expectation.read_resource_uri == scenario_contract.MCP_TECHNICAL_RESOURCE_URI


def test_mcp_technical_seed_does_not_require_an_existing_agent_id() -> None:
    args = argparse.Namespace(
        technical_integration_seed=False,
        mcp_technical_seed=True,
        agent_id=None,
        runs=1,
        concurrency=1,
        capability="mcp_readonly",
        timeout_seconds=1.0,
        require_trace_complete=False,
    )

    with pytest.raises(live.LiveAcceptanceError, match="隔离 live 验收"):
        asyncio.run(
            live.run_live_acceptance(
                args,
                {"API_BASE": "invalid", "API_KEY": "private"},
                (),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("allowed_tool_names", ["soc_api__list_alerts_api_v1_alerts_get"]),
        ("unapproved_tool_names", ["soc_api__list_alerts_api_v1_alerts_get"]),
        ("resource_uris", ["openapi://soc_api/unreviewed"]),
        ("read_resource_uri", "openapi://soc_api/unreviewed"),
    ),
)
def test_mcp_scenario_rejects_weakened_or_drifted_expectation(tmp_path: Path, field: str, value: object) -> None:
    scenario = _mcp_scenario()
    expectation = scenario["mcp_expectation"]
    assert isinstance(expectation, dict)
    expectation[field] = value
    path = _write_scenarios(
        tmp_path / "mcp-reviewed.json",
        agent_id="runtime-mcp-technical-integration-package",
        scenarios=[scenario],
    )

    with pytest.raises(live.LiveAcceptanceError, match="精确匹配"):
        live.load_scenarios(path, expected_agent_id="runtime-mcp-technical-integration-package")


def _mcp_messages(*, include_dashboard: bool = True, read_uri: str | None = None) -> list[dict[str, object]]:
    prefix = f"mcp__{scenario_contract.MCP_TECHNICAL_SERVER_NAME}__"
    specifications = [
        (
            f"{prefix}resources_list",
            {},
            {
                "resources": [{"name": "health", "uri": scenario_contract.MCP_TECHNICAL_RESOURCE_URI}],
                "next_cursor": None,
            },
        ),
        (
            f"{prefix}resource_templates_list",
            {},
            {
                "resource_templates": [
                    {"name": "analysis", "uri_template": scenario_contract.MCP_TECHNICAL_RESOURCE_TEMPLATE},
                ],
                "next_cursor": None,
            },
        ),
        (
            f"{prefix}resource_read",
            {"uri": read_uri or scenario_contract.MCP_TECHNICAL_RESOURCE_URI},
            {
                "contents": [
                    {
                        "uri": scenario_contract.MCP_TECHNICAL_RESOURCE_URI,
                        "mime_type": "application/json",
                        "text": '{"status":"healthy"}',
                    },
                ],
            },
        ),
        (
            f"{prefix}{scenario_contract.MCP_TECHNICAL_RAW_TOOL_NAME}",
            {},
            {"status": "available"},
        ),
    ]
    if not include_dashboard:
        specifications.pop()
    blocks: list[dict[str, object]] = []
    for index, (name, arguments, output) in enumerate(specifications):
        tool_id = f"tool-{index}"
        blocks.extend(
            (
                {
                    "type": "tool_call",
                    "id": tool_id,
                    "name": name,
                    "input": json.dumps(arguments),
                    "state": "finished",
                },
                {
                    "type": "tool_result",
                    "id": tool_id,
                    "name": name,
                    "output": [{"type": "text", "text": json.dumps(output)}],
                    "state": "success",
                },
            ),
        )
    blocks.append({"type": "text", "text": scenario_contract.MCP_TECHNICAL_COMPLETION_TEXT})
    return [{"id": "reply-mcp", "role": "assistant", "content": blocks}]


def _mcp_sse(*, result_state: str = "success") -> bytes:
    payloads: list[dict[str, object]] = [{"type": "REPLY_START", "reply_id": "reply-mcp"}]
    for index, name in enumerate(scenario_contract.MCP_TECHNICAL_TOOL_NAMES):
        tool_id = f"tool-{index}"
        payloads.extend(
            (
                {"type": "TOOL_CALL_START", "reply_id": "reply-mcp", "tool_call_id": tool_id, "tool_call_name": name},
                {"type": "TOOL_CALL_END", "reply_id": "reply-mcp", "tool_call_id": tool_id},
                {"type": "TOOL_RESULT_START", "reply_id": "reply-mcp", "tool_call_id": tool_id, "tool_call_name": name},
                {"type": "TOOL_RESULT_END", "reply_id": "reply-mcp", "tool_call_id": tool_id, "state": result_state},
            ),
        )
    payloads.extend(
        (
            {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-mcp", "delta": scenario_contract.MCP_TECHNICAL_COMPLETION_TEXT},
            {"type": "REPLY_END", "reply_id": "reply-mcp"},
        ),
    )
    return _sse(*payloads)


def test_mcp_evidence_requires_exact_public_roster_canonical_tools_resources_and_sse(tmp_path: Path) -> None:
    path = _write_scenarios(
        tmp_path / "mcp-reviewed.json",
        agent_id="runtime-mcp-technical-integration-package",
        scenarios=[_mcp_scenario()],
    )
    scenario = live.load_scenarios(path, expected_agent_id="runtime-mcp-technical-integration-package").scenarios[0]
    expected_workspace_tool = f"mcp__{scenario_contract.MCP_TECHNICAL_SERVER_NAME}__{scenario_contract.MCP_TECHNICAL_RAW_TOOL_NAME}"
    workspace = mcp_live.validate_workspace_mcp_payload(
        [
            {
                "name": scenario_contract.MCP_TECHNICAL_SERVER_NAME,
                "is_stateful": False,
                "is_healthy": True,
                "error": None,
                "tools": [{"name": expected_workspace_tool}],
            },
        ],
        scenario,
    )

    evidence = mcp_live.validate_canonical_mcp_payload(
        _mcp_messages(),
        scenario,
        reply_ids=("reply-mcp",),
        raw_sse=_mcp_sse(),
        workspace=workspace,
    )
    summary = evidence.summary()

    assert evidence.workspace.tool_names == (expected_workspace_tool,)
    assert evidence.resource_uris == (scenario_contract.MCP_TECHNICAL_RESOURCE_URI,)
    assert evidence.resource_templates == (scenario_contract.MCP_TECHNICAL_RESOURCE_TEMPLATE,)
    assert {item.tool_name for item in evidence.tool_results} == set(scenario_contract.MCP_TECHNICAL_TOOL_NAMES)
    assert summary["resource_content_count"] == 1
    assert "healthy" not in json.dumps(summary)


def test_mcp_workspace_roster_rejects_unapproved_server_tool(tmp_path: Path) -> None:
    path = _write_scenarios(
        tmp_path / "mcp-reviewed.json",
        agent_id="runtime-mcp-technical-integration-package",
        scenarios=[_mcp_scenario()],
    )
    scenario = live.load_scenarios(path, expected_agent_id="runtime-mcp-technical-integration-package").scenarios[0]
    prefix = f"mcp__{scenario_contract.MCP_TECHNICAL_SERVER_NAME}__"

    with pytest.raises(live.LiveAcceptanceError, match="allowlist"):
        mcp_live.validate_workspace_mcp_payload(
            [
                {
                    "name": scenario_contract.MCP_TECHNICAL_SERVER_NAME,
                    "is_healthy": True,
                    "error": None,
                    "tools": [
                        {"name": f"{prefix}{scenario_contract.MCP_TECHNICAL_RAW_TOOL_NAME}"},
                        {"name": f"{prefix}soc_api__list_alerts_api_v1_alerts_get"},
                    ],
                },
            ],
            scenario,
        )


@pytest.mark.parametrize(
    ("messages", "raw_sse", "message"),
    (
        (_mcp_messages(include_dashboard=False), _mcp_sse(), "四个批准工具"),
        (_mcp_messages(read_uri="openapi://soc_api/unreviewed"), _mcp_sse(), "批准 URI"),
        (_mcp_messages(), _mcp_sse().replace(b"tool-0", b"sse-tool-0"), "精确对账"),
        (_mcp_messages(), _mcp_sse(result_state="error"), "生命周期不完整"),
    ),
)
def test_mcp_canonical_or_sse_evidence_fails_closed(
    tmp_path: Path,
    messages: list[dict[str, object]],
    raw_sse: bytes,
    message: str,
) -> None:
    path = _write_scenarios(
        tmp_path / "mcp-reviewed.json",
        agent_id="runtime-mcp-technical-integration-package",
        scenarios=[_mcp_scenario()],
    )
    scenario = live.load_scenarios(path, expected_agent_id="runtime-mcp-technical-integration-package").scenarios[0]
    workspace = mcp_live.McpWorkspaceEvidence(scenario_contract.MCP_TECHNICAL_SERVER_NAME, (), ())

    with pytest.raises(live.LiveAcceptanceError, match=message):
        mcp_live.validate_canonical_mcp_payload(
            messages,
            scenario,
            reply_ids=("reply-mcp",),
            raw_sse=raw_sse,
            workspace=workspace,
        )


def test_live_cli_has_no_default_scenario_or_agent() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--env-file", "/tmp/not-used"],
        cwd="/tmp",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--scenario-file" in result.stderr


@pytest.mark.parametrize(
    "raw",
    (
        b'data: {"type":\n\n',
        b"data\n\n",
        _sse(["REPLY_START"]),
        _sse({"id": "event-without-type"}),
    ),
)
def test_sse_parser_rejects_every_malformed_complete_data_frame(raw: bytes) -> None:
    with pytest.raises(live.LiveAcceptanceError, match="SSE data frame"):
        scenario_contract.parse_sse_events(raw)


def test_success_sse_requires_ordered_nonempty_exact_reply_chain() -> None:
    raw = _sse(
        {"id": "start", "type": "REPLY_START", "reply_id": "reply-one"},
        {"id": "future", "type": "FUTURE_AGENT_EVENT", "value": {"kept": True}},
        {"id": "empty", "type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": ""},
        {"id": "text", "type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": "真实结果"},
        {"id": "end", "type": "REPLY_END", "reply_id": "reply-one"},
    )

    assert scenario_contract.validate_sse_evidence(
        raw,
        purpose="success",
        terminal_reply_ids=("reply-one",),
    ) == (
        "REPLY_START",
        "FUTURE_AGENT_EVENT",
        "TEXT_BLOCK_DELTA",
        "TEXT_BLOCK_DELTA",
        "REPLY_END",
    )


@pytest.mark.parametrize(
    ("payloads", "terminal_reply_ids", "message"),
    (
        (
            ({"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": "越序"},),
            (),
            "TEXT_BLOCK_DELTA",
        ),
        (
            (
                {"type": "REPLY_START", "reply_id": "reply-one"},
                {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": "   "},
                {"type": "REPLY_END", "reply_id": "reply-one"},
            ),
            ("reply-one",),
            "非空 TEXT_BLOCK_DELTA",
        ),
        (
            (
                {"type": "REPLY_START", "reply_id": "reply-one"},
                {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-two", "delta": "错绑"},
            ),
            (),
            "reply_id",
        ),
        (
            (
                {"type": "REPLY_START", "reply_id": "reply-one"},
                {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": "无终态"},
            ),
            (),
            "缺少 REPLY_END",
        ),
        (
            (
                {"type": "REPLY_START", "reply_id": "reply-one"},
                {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-one", "delta": "完成"},
                {"type": "REPLY_END", "reply_id": "reply-one"},
            ),
            ("reply-other",),
            "持久终态 reply_ids",
        ),
    ),
)
def test_success_sse_rejects_incomplete_or_cross_reply_evidence(
    payloads: tuple[dict[str, object], ...],
    terminal_reply_ids: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(live.LiveAcceptanceError, match=message):
        scenario_contract.validate_sse_evidence(
            _sse(*payloads),
            purpose="retry",
            terminal_reply_ids=terminal_reply_ids,
        )


def test_cancel_sse_contract_keeps_early_and_partial_semantics_distinct() -> None:
    assert (
        scenario_contract.validate_sse_evidence(
            b"",
            purpose="early_cancel",
            terminal_reply_ids=(),
        )
        == ()
    )
    partial = _sse(
        {"type": "REPLY_START", "reply_id": "reply-partial"},
        {"type": "TEXT_BLOCK_DELTA", "reply_id": "reply-partial", "delta": "部分结果"},
    )
    assert scenario_contract.validate_sse_evidence(
        partial,
        purpose="partial_cancel",
        terminal_reply_ids=(),
    ) == ("REPLY_START", "TEXT_BLOCK_DELTA")
    with pytest.raises(live.LiveAcceptanceError, match="没有非空文本增量"):
        scenario_contract.validate_sse_evidence(
            _sse({"type": "REPLY_START", "reply_id": "reply-partial"}),
            purpose="partial_cancel",
            terminal_reply_ids=(),
        )


def test_formal_browser_contract_requires_both_engines_exactly_three_times() -> None:
    module_uri = BROWSER_CONTRACT.resolve().as_uri()
    program = f"""
import {{ browserExecutionPlan, requireFormalBrowserResultMatrix }} from {json.dumps(module_uri)};
const plan = browserExecutionPlan("both", {{ formal: true }});
if (plan.join(",") !== "chromium,firefox") process.exit(10);
const good = plan.flatMap((engine) => [1, 2, 3].map((attempt) => ({{ engine, attempt }})));
requireFormalBrowserResultMatrix(good, {{ formal: true }});
let rejectedSingle = false;
let rejectedMissing = false;
try {{ browserExecutionPlan("chromium", {{ formal: true }}); }} catch {{ rejectedSingle = true; }}
try {{ requireFormalBrowserResultMatrix(good.slice(0, 5), {{ formal: true }}); }} catch {{ rejectedMissing = true; }}
if (!rejectedSingle || !rejectedMissing) process.exit(11);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_public_browser_and_release_candidate_targets_force_formal_matrix() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile.count("AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both") == 4
    verifier = (REPO_ROOT / "scripts/verify_playground_cancel.mjs").read_text(encoding="utf-8")
    improvement_verifier = (REPO_ROOT / "scripts/verify_improvement_ui_real_container.mjs").read_text(encoding="utf-8")
    evidence = (REPO_ROOT / "scripts/improvement_ui_e2e/playground_cancel_evidence.mjs").read_text(encoding="utf-8")
    assert "requireFormalBrowserResultMatrix(results" in verifier
    assert "requireFormalBrowserResultMatrix(results" in improvement_verifier
    assert "const { chromium, firefox }" in improvement_verifier
    assert "FORMAL_BROWSER_REPETITIONS" in improvement_verifier
    assert improvement_verifier.count("runRealContainerAcceptance(") == 1
    assert "verifyCompletedImprovementAcceptance(" in improvement_verifier
    assert "mutation_runs: 1" in improvement_verifier
    assert 'mode: "read-only-completed-loop"' in improvement_verifier
    real_flow = (REPO_ROOT / "scripts/improvement_ui_e2e/real_container_flow.mjs").read_text(encoding="utf-8")
    assert 'page.getByTestId("improvement-scope-filter")' in real_flow
    assert 'url.searchParams.get("agent_id") === seed.agent.agent_id' in real_flow
    assert "formal_browser_acceptance: formalBrowserAcceptance" in verifier
    assert "cancelClosesRecoveredStream: Boolean(cancellation && cancelledStream" in evidence
    assert "reloadRecoveredExactActiveRun: reloadRecovered === true" in evidence
    assert "successfulStreamClosed: Boolean(successfulStream?.closedAt)" in evidence
    assert "const allStreamsClosed = await network.settle(config.actionTimeoutMs);" in evidence
    assert verifier.index("const resourceFailures = await cleanupResources") < verifier.index("const expectedDiagnostics = new Set(")
    assert '"expected_reload_stream_cancel"' in verifier


def test_public_browser_technical_target_is_real_isolated_and_non_formal() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "ui-playground-technical-smoke: browser-technical-live-preflight" in makefile
    assert 'REAL_SCENARIO_FILE="$${BROWSER_TECHNICAL_SCENARIO_FILE}"' in makefile
    assert "REAL_ACCEPTANCE_AGENT_ID=security-operations-expert" in makefile
    assert "AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=0" in makefile
    assert "BROWSER=both REAL_ACCEPTANCE_AGENT_ID=security-operations-expert" in makefile
    assert "$(CONTAINER_ACCEPTANCE) --profile core" in makefile


def test_public_candidate_lifecycle_target_is_real_isolated_and_dual_browser() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    verifier = (REPO_ROOT / "scripts/verify_agent_candidate_lifecycle.mjs").read_text(encoding="utf-8")
    package_scripts = json.loads((REPO_ROOT / "frontend/package.json").read_text(encoding="utf-8"))["scripts"]

    recipe = makefile.split("\nui-agent-candidate-technical-smoke: technical-live-preflight", 1)[1].split("\n\n", 1)[0]
    private_recipe = makefile.split("\n_ui-agent-candidate-technical-smoke:", 1)[1].split("\n\n", 1)[0]
    assert "$(CONTAINER_ACCEPTANCE) --profile langfuse --" in recipe
    assert "_ui-agent-candidate-technical-smoke BROWSER=both" in recipe
    assert "$(REQUIRE_CONTAINER_ACCEPTANCE)" in private_recipe
    assert '"$${AGENTGOV_ACCEPTANCE_NODE:?missing bound acceptance node}" scripts/verify_agent_candidate_lifecycle.mjs' in private_recipe
    assert "verify:agent-candidate:impl" not in private_recipe
    assert "npm" not in private_recipe
    assert package_scripts["verify:agent-candidate"] == "cd .. && make ui-agent-candidate-technical-smoke"
    assert package_scripts["verify:agent-candidate:impl"] == "cd .. && node scripts/verify_agent_candidate_lifecycle.mjs"
    assert "requireContainerAcceptance();" in verifier
    assert 'process.env.AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE !== "langfuse"' in verifier
    assert "loadReviewedScenarios(" in verifier
    assert "process.env.TECHNICAL_SCENARIO_FILE" in verifier
    assert 'browserPlan.includes("chromium")' in verifier
    assert 'browserPlan.includes("firefox")' in verifier
    assert ".route(" not in verifier
    assert ".setContent(" not in verifier
    assert ".addInitScript(" not in verifier


def test_evidence_summary_rejects_cross_scenario_identity_reuse() -> None:
    with pytest.raises(live.LiveAcceptanceError, match="run_id"):
        live.validate_evidence_identities(
            expected_runs=2,
            configured_concurrency=2,
            max_concurrency_observed=2,
            scenario_ids=("one", "two"),
            session_ids=("session-one", "session-two"),
            run_ids=("same-run", "same-run"),
            trace_ids=("1" * 32, "2" * 32),
            reply_ids=("reply-one", "reply-two"),
            expected_capability="generic_runtime",
            capabilities=("generic_runtime", "generic_runtime"),
        )


def test_evidence_summary_requires_configured_concurrency_peak() -> None:
    with pytest.raises(live.LiveAcceptanceError, match="服务端 run 生命周期重叠"):
        live.validate_evidence_identities(
            expected_runs=2,
            configured_concurrency=2,
            max_concurrency_observed=1,
            scenario_ids=("one", "two"),
            session_ids=("session-one", "session-two"),
            run_ids=("run-one", "run-two"),
            trace_ids=("1" * 32, "2" * 32),
            reply_ids=(),
            expected_capability="generic_runtime",
            capabilities=("generic_runtime", "generic_runtime"),
        )


def test_concurrency_peak_uses_server_run_intervals_not_client_tasks() -> None:
    runs = (
        {
            "status": "succeeded",
            "started_at": "2026-09-11T10:00:00+00:00",
            "completed_at": "2026-09-11T10:00:03+00:00",
        },
        {
            "status": "cancelled",
            "started_at": "2026-09-11T10:00:01+00:00",
            "completed_at": "2026-09-11T10:00:02+00:00",
        },
        {
            "status": "succeeded",
            "started_at": "2026-09-11T10:00:03+00:00",
            "completed_at": "2026-09-11T10:00:04+00:00",
        },
    )

    assert live.observed_run_concurrency(runs) == 2
