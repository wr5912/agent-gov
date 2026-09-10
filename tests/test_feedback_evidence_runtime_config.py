from __future__ import annotations

import json

import yaml

from feedback_store_test_utils import FeedbackSignalCreateRequest, FeedbackStore, _run_payload, _settings


def test_evidence_package_includes_runtime_mcp_diagnostics(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    (settings.default_workspace_dir / "mcp" / "sec-ops.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "sec-ops-data",
                "credential_refs": [{"env": "MCP_SERVER_URL", "path": "mcp_config.url"}],
                "mcp_config": {"type": "http_mcp", "url": "${MCP_SERVER_URL}"},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = settings.default_workspace_dir / "agent.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["workspace_policy"]["allowed_network_domains"] = ["${SERVICE_HOST}"]
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    sample_dir = settings.default_workspace_dir / "mcp_servers" / "soc_data_mcp"
    sample_dir.mkdir(parents=True)
    (sample_dir / "sample_alerts.json").write_text(
        json.dumps([{"host": {"hostname": "${SERVICE_HOST}"}, "network": {"dst_port": "${SERVICE_PORT}"}}]),
        encoding="utf-8",
    )
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
        agent_version_provider=lambda _aid=None: "main-v-test",
    )
    run_id = "run-mcp-config-failed"
    store.record_run(
        _run_payload(
            run_id=run_id,
            session_id="sess-mcp-config-failed",
            status="failed",
            terminal_reason="runtime_error",
            error={"type": "MCP_CONNECTION_FAILED", "message": "MCP server unavailable"},
            created_at="2026-06-04T00:00:00+00:00",
            started_at="2026-06-04T00:00:00+00:00",
            updated_at="2026-06-04T00:00:01+00:00",
            completed_at="2026-06-04T00:00:01+00:00",
        )
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            labels=["runtime_error"],
            comment="生成日报失败",
        )
    )
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"])], title="日报失败")

    manifest = store.create_evidence_package(feedback_case["feedback_case_id"])

    completeness = manifest["completeness"]
    assert completeness["has_runtime_config_summary"] is True
    assert completeness["has_effective_mcp_config"] is True
    assert completeness["has_mcp_connection_summary"] is True
    assert completeness["has_runtime_env_snapshot"] is True
    assert completeness["has_workspace_placeholder_summary"] is True
    runtime_summary = store.get_evidence_package_file(manifest["evidence_package_id"], "runtime_config_summary.json")["content"]
    effective_mcp = store.get_evidence_package_file(manifest["evidence_package_id"], "effective_mcp_config.json")["content"]
    connection_summary = store.get_evidence_package_file(manifest["evidence_package_id"], "mcp_connection_summary.json")["content"]
    placeholder_summary = store.get_evidence_package_file(manifest["evidence_package_id"], "workspace_placeholder_summary.json")["content"]
    assert runtime_summary["agent_manifest"]["source"] == "workspace_agent_manifest"
    assert runtime_summary["agent_manifest"]["exists"] is True
    assert runtime_summary["agent_manifest"]["runtime"] == "agentscope"
    assert len(runtime_summary["agent_manifest"]["sha256"]) == 64
    assert "main_profile_writable_paths" not in runtime_summary
    assert effective_mcp["source"] == "workspace_mcp_directory"
    assert effective_mcp["selected_servers"] == ["sec-ops"]
    assert effective_mcp["server_summaries"][0]["unresolved_placeholders"] == ["MCP_SERVER_URL"]
    # AgentGov run 仅保存引用与终态；逐消息 MCP 事件由 AgentScope/Langfuse 持有，
    # 因而本地证据包不伪造连接状态。
    assert connection_summary["failed_server_names"] == []
    categories = {item["path"]: item["category"] for item in placeholder_summary["items"]}
    assert categories["agent.yaml"] == "agent_manifest"
    assert categories["mcp/sec-ops.json"] == "mcp_config"
    assert categories["mcp_servers/soc_data_mcp/sample_alerts.json"] == "workspace_template_file"
    evidence_file_names = {item["path"] for item in manifest["included_files"]}
    assert {
        "runtime_config_summary.json",
        "effective_mcp_config.json",
        "mcp_connection_summary.json",
        "runtime_env_snapshot.json",
        "workspace_placeholder_summary.json",
    } <= evidence_file_names
