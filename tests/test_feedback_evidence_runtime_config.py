from __future__ import annotations

import json

from feedback_store_test_utils import FeedbackSignalCreateRequest, FeedbackStore, _settings


def test_evidence_package_includes_runtime_mcp_diagnostics(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    (settings.main_workspace_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "${MCP_SERVER_URL}"}}}),
        encoding="utf-8",
    )
    settings_dir = settings.main_workspace_dir / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"sandbox": {"network": {"allowedDomains": ["${SERVICE_HOST}"]}}}),
        encoding="utf-8",
    )
    sample_dir = settings.main_workspace_dir / "mcp_servers" / "soc_data_mcp"
    sample_dir.mkdir(parents=True)
    (sample_dir / "sample_alerts.json").write_text(
        json.dumps([{"host": {"hostname": "${SERVICE_HOST}"}, "network": {"dst_port": "${SERVICE_PORT}"}}]),
        encoding="utf-8",
    )
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.main_workspace_dir,
        agent_version_provider=lambda _aid=None: "main-v-test",
    )
    run_id = "run-mcp-config-failed"
    store.record_run(
        {
            "run_id": run_id,
            "session_id": "sess-mcp-config-failed",
            "message": "生成一份日报",
            "answer_summary": "",
            "messages": [
                {
                    "event": "SystemMessage",
                    "type": "system",
                    "subtype": "init",
                    "mcp_servers": [{"name": "sec-ops-data", "status": "failed"}],
                }
            ],
            "agent_activity": {"tool_names": [], "tool_calls": [], "tool_results": [], "skill_calls": []},
            "errors": ["Reached maximum number of turns (8)"],
            "created_at": "2026-06-04T00:00:00+00:00",
            "completed_at": "2026-06-04T00:00:01+00:00",
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            labels=["runtime_error"],
            comment="生成日报失败",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="日报失败")

    manifest = store.create_evidence_package(feedback_case["feedback_case_id"])

    completeness = manifest["completeness"]
    assert completeness["has_runtime_config_summary"] is True
    assert completeness["has_effective_mcp_config"] is True
    assert completeness["has_mcp_connection_summary"] is True
    assert completeness["has_runtime_env_snapshot"] is True
    assert completeness["has_workspace_placeholder_summary"] is True
    effective_mcp = store.get_evidence_package_file(manifest["evidence_package_id"], "effective_mcp_config.json")["content"]
    connection_summary = store.get_evidence_package_file(manifest["evidence_package_id"], "mcp_connection_summary.json")["content"]
    placeholder_summary = store.get_evidence_package_file(manifest["evidence_package_id"], "workspace_placeholder_summary.json")["content"]
    assert effective_mcp["source"] == "workspace_project"
    assert effective_mcp["unresolved_placeholders"] == [{"path": "$.sec-ops-data.url", "placeholder": "MCP_SERVER_URL"}]
    assert connection_summary["failed_server_names"] == ["sec-ops-data"]
    categories = {item["path"]: item["category"] for item in placeholder_summary["items"]}
    assert categories[".claude/settings.json"] == "claude_project_settings"
    assert categories[".mcp.json"] == "mcp_config"
    assert categories["mcp_servers/soc_data_mcp/sample_alerts.json"] == "mcp_sample_data"
    evidence_file_names = {item["path"] for item in manifest["included_files"]}
    assert {
        "runtime_config_summary.json",
        "effective_mcp_config.json",
        "mcp_connection_summary.json",
        "runtime_env_snapshot.json",
        "workspace_placeholder_summary.json",
    } <= evidence_file_names
