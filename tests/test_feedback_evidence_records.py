import json

from app.runtime.runtime_db import EvidencePackageModel
from pydantic import ValidationError

from feedback_store_test_utils import FeedbackSignalCreateRequest, _record_run, _store, pytest


def test_evidence_package_projection_rejects_invalid_persisted_file_path(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"])])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    with store.Session.begin() as db:
        row = db.get(EvidencePackageModel, evidence["evidence_package_id"])
        manifest = dict(row.manifest_json or {})
        manifest["included_files"] = [
            dict(manifest["included_files"][0]),
            {"path": "../feedback.json", "sha256": "0" * 64, "type": "feedback"},
        ]
        row.manifest_json = manifest

    with pytest.raises(ValidationError):
        store.get_evidence_package(evidence["evidence_package_id"])


def test_evidence_derives_minimal_langfuse_semantics_without_copying_run_or_trace_io(tmp_path):
    store, _ = _store(tmp_path)
    run = _record_run(store)
    trace_id = run["trace_id"]
    store.set_langfuse_trace_fetcher(
        lambda requested: (
            {
                "id": requested,
                "name": "agentgov.run",
                "timestamp": "2026-05-20T00:00:00+00:00",
                "input": {"prompt": "RAW_PROMPT_SECRET"},
                "output": {"answer": "RAW_ANSWER_SECRET"},
                "metadata": {
                    "agentgov.run.id": "run-1",
                    "agentgov.agent.id": "security-operations-expert",
                    "Authorization": "Bearer DO_NOT_PERSIST",
                },
                "observations": [
                    {
                        "id": "obs-tool",
                        "name": "execute_tool",
                        "type": "SPAN",
                        "level": "DEFAULT",
                        "input": {"api_key": "TOOL_INPUT_SECRET"},
                        "output": {"token": "TOOL_OUTPUT_SECRET"},
                        "metadata": {
                            "gen_ai.operation.name": "execute_tool",
                            "tool.name": "asset_lookup",
                            "Authorization": "Bearer DO_NOT_PERSIST",
                        },
                        "usageDetails": {"input": 12, "output": 7},
                    },
                    {
                        "id": "obs-mcp",
                        "name": "mcp.connect",
                        "type": "SPAN",
                        "level": "ERROR",
                        "statusMessage": "MCP_SECRET_ERROR_BODY",
                        "metadata": {"mcp.server.name": "sec-ops-data"},
                    },
                ],
            }
            if requested == trace_id
            else None
        )
    )
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_error"]))
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"])])

    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    assert evidence is not None
    included = {item["path"] for item in evidence["included_files"]}
    assert "messages.json" not in included
    assert "agent_activity.json" not in included
    assert evidence["completeness"]["has_messages"] is False
    assert evidence["completeness"]["has_agent_activity"] is False
    assert evidence["completeness"]["has_tool_calls"] is True
    assert evidence["completeness"]["has_langfuse_trace_details"] is True

    details = store.get_evidence_package_file(evidence["evidence_package_id"], "langfuse_trace_details.json")["content"]
    tool_calls = store.get_evidence_package_file(evidence["evidence_package_id"], "tool_calls.json")["content"]
    trace_summary = store.get_evidence_package_file(evidence["evidence_package_id"], "trace_summary.json")["content"]
    mcp_summary = store.get_evidence_package_file(evidence["evidence_package_id"], "mcp_connection_summary.json")["content"]
    serialized = json.dumps(
        {"details": details, "tool_calls": tool_calls, "trace_summary": trace_summary, "mcp_summary": mcp_summary},
        ensure_ascii=False,
    )
    for forbidden in (
        "RAW_PROMPT_SECRET",
        "RAW_ANSWER_SECRET",
        "TOOL_INPUT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "DO_NOT_PERSIST",
        "MCP_SECRET_ERROR_BODY",
    ):
        assert forbidden not in serialized
    assert "trace" not in details[0]
    assert "input" not in details[0] and "output" not in details[0]
    assert all("input" not in item and "output" not in item for item in details[0]["observations"])
    assert details[0]["input_fingerprint"]["byte_length"] > 0
    assert len(details[0]["input_fingerprint"]["sha256"]) == 64
    assert tool_calls == [
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "trace_id": trace_id,
            "observation_id": "obs-tool",
            "name": "execute_tool",
            "type": "SPAN",
            "level": "DEFAULT",
            "error": False,
            "attributes": {"gen_ai.operation.name": "execute_tool", "tool.name": "asset_lookup"},
            "usage": {"input": 12, "output": 7},
            "input_fingerprint": tool_calls[0]["input_fingerprint"],
            "output_fingerprint": tool_calls[0]["output_fingerprint"],
        }
    ]
    assert trace_summary[0]["observation_names"] == ["execute_tool", "mcp.connect"]
    assert trace_summary[0]["error_observation_names"] == ["mcp.connect"]
    assert mcp_summary["source"] == "langfuse_trace_details"
    assert mcp_summary["failed_server_names"] == ["sec-ops-data"]
