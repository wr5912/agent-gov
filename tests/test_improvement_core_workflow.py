"""核心治理主链的公共 API 行为测试。

这些用例使用生产 FastAPI app、真实 SQLite 和真实业务 Agent Workspace，不替换 HTTP
接口或业务 service。Runtime 推理属于独立 live lane，本文件只覆盖不需要模型调用的治理闭环。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app_test_utils import load_test_app

AGENT_ALPHA = "core-agent-alpha"
AGENT_BETA = "core-agent-beta"


def _load_core_app(process_environment, tmp_path: Path):
    return load_test_app(
        process_environment,
        tmp_path,
        extra_agent_ids=(AGENT_ALPHA, AGENT_BETA),
    )


def _record_completed_run(module, *, run_id: str, session_id: str, agent_id: str) -> None:
    timestamp = "2026-09-11T00:00:00Z"
    module.feedback_store.record_run(
        {
            "run_id": run_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_version_id": "a" * 40,
            "runtime_agent_id": f"runtime-{agent_id}",
            "harness_digest": "b" * 64,
            "status": "succeeded",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )


def _collect_and_triage_feedback(client: TestClient) -> str:
    signal_response = client.post(
        "/api/feedback-signals",
        json={
            "source_type": "explicit_feedback",
            "run_id": "run-core-feedback",
            "labels": ["duplicate_tool_call"],
            "comment": "相同工具在一次运行中重复调用",
        },
    )
    assert signal_response.status_code == 200
    signal = signal_response.json()
    assert signal["agent_id"] == AGENT_ALPHA
    assert signal["matched_run_id"] == "run-core-feedback"

    filtered_signals = client.get(
        "/api/feedback-signals",
        params={
            "agent_id": AGENT_ALPHA,
            "run_id": "run-core-feedback",
            "session_id": "session-core-feedback",
            "source_type": "explicit_feedback",
        },
    )
    assert filtered_signals.status_code == 200
    assert [item["signal_id"] for item in filtered_signals.json()] == [signal["signal_id"]]

    annotated_response = client.patch(
        f"/api/feedback-sources/signal/{signal['signal_id']}",
        json={
            "status": "triaged",
            "priority": "high",
            "requires_review": False,
            "comment": "已确认是可复现的 Harness 问题",
            "labels": ["harness", "repeatable"],
        },
    )
    assert annotated_response.status_code == 200
    annotated = annotated_response.json()
    assert annotated["status"] == "triaged"
    assert annotated["priority"] == "high"
    assert annotated["comment"] == "已确认是可复现的 Harness 问题"
    assert annotated["labels"] == ["harness", "repeatable"]
    assert annotated["requires_review"] is False

    case_response = client.post(
        "/api/feedback-cases",
        json={
            "source_refs": [{"source_kind": "signal", "source_id": signal["signal_id"]}],
            "title": "重复工具调用治理",
            "priority": "high",
        },
    )
    assert case_response.status_code == 200
    feedback_case = case_response.json()
    assert feedback_case["agent_id"] == AGENT_ALPHA
    assert feedback_case["signal_ids"] == [signal["signal_id"]]
    assert feedback_case["run_ids"] == ["run-core-feedback"]
    return str(feedback_case["feedback_case_id"])


def _attach_feedback_case(client: TestClient, feedback_case_id: str) -> str:
    improvement_response = client.post(
        "/api/improvements",
        json={"agent_id": AGENT_ALPHA, "title": "复用同一运行内的工具结果"},
    )
    assert improvement_response.status_code == 201
    improvement_id = str(improvement_response.json()["improvement_id"])

    attachable = client.get(f"/api/improvements/{improvement_id}/attachable-feedbacks")
    assert attachable.status_code == 200
    assert [item["feedback_case_id"] for item in attachable.json()["feedback_cases"]] == [feedback_case_id]

    attached_response = client.post(
        f"/api/improvements/{improvement_id}/attach-feedback-case",
        json={"feedback_case_id": feedback_case_id},
    )
    assert attached_response.status_code == 201
    assert attached_response.json()["run_id"] == "run-core-feedback"
    assert attached_response.json()["case_id"] == feedback_case_id

    provenance_response = client.get(f"/api/asset-registry/feedback/{feedback_case_id}")
    assert provenance_response.status_code == 200
    provenance = provenance_response.json()
    assert provenance["agent_ids"] == [AGENT_ALPHA]
    assert provenance["improvements"][0]["improvement_id"] == improvement_id
    return improvement_id


def _create_and_inherit_asset(client: TestClient, improvement_id: str) -> None:
    asset_response = client.post(
        "/api/assets",
        json={
            "agent_id": AGENT_ALPHA,
            "asset_type": "methodology",
            "title": "运行内工具结果复用方法",
            "body": "调用前先检查当前 run 已获得的同类结果。",
            "source_improvement_id": improvement_id,
        },
    )
    assert asset_response.status_code == 201
    asset = asset_response.json()

    inherited_response = client.post(
        f"/api/assets/{asset['asset_id']}/inherit",
        json={"target_agent_id": AGENT_BETA},
    )
    assert inherited_response.status_code == 201
    inherited = inherited_response.json()
    assert inherited["agent_id"] == AGENT_BETA
    assert inherited["inherited_from"] == asset["asset_id"]
    assert inherited["source_improvement_id"] == improvement_id

    assets = client.get(
        "/api/assets",
        params={
            "agent_id": AGENT_BETA,
            "asset_type": "methodology",
            "source_improvement_id": improvement_id,
        },
    )
    assert assets.status_code == 200
    assert [item["asset_id"] for item in assets.json()] == [inherited["asset_id"]]


def test_feedback_to_improvement_to_asset_public_workflow(process_environment, tmp_path: Path) -> None:
    """一条真实反馈经 Case、改进事项和资产继承形成可查询闭环。"""

    module = _load_core_app(process_environment, tmp_path)
    _record_completed_run(
        module,
        run_id="run-core-feedback",
        session_id="session-core-feedback",
        agent_id=AGENT_ALPHA,
    )

    with TestClient(module.app) as client:
        feedback_case_id = _collect_and_triage_feedback(client)
        improvement_id = _attach_feedback_case(client, feedback_case_id)
        _create_and_inherit_asset(client, improvement_id)


def test_pending_soc_event_resolves_into_queryable_feedback_case(process_environment, tmp_path: Path) -> None:
    """暂未匹配的真实事件可在 run 到达后解析，并继续进入反馈 Case。"""

    module = _load_core_app(process_environment, tmp_path)
    event_payload = {
        "event_id": "event-core-pending",
        "source_system": "operator-console",
        "event_type": "recommendation.modified",
        "timestamp": "2026-09-11T00:00:01Z",
        "session_id": "session-core-late",
        "comment": "人工修改了处置建议",
    }

    with TestClient(module.app) as client:
        pending_response = client.post("/api/soc-events", json=event_payload)
        assert pending_response.status_code == 200
        pending_result = pending_response.json()
        assert pending_result["correlation_status"] == "pending_correlation"
        pending_id = pending_result["pending_correlation"]["pending_id"]

        duplicate_response = client.post("/api/soc-events", json=event_payload)
        assert duplicate_response.status_code == 200
        assert duplicate_response.json()["correlation_status"] == "duplicate"
        assert len(client.get("/api/pending-correlations", params={"status": "pending"}).json()) == 1

        _record_completed_run(
            module,
            run_id="run-core-late",
            session_id="session-core-late",
            agent_id=AGENT_ALPHA,
        )
        resolved_response = client.post(
            f"/api/pending-correlations/{pending_id}/resolve",
            json={"run_id": "run-core-late", "comment": "已关联到迟到的运行记录"},
        )
        assert resolved_response.status_code == 200
        resolved = resolved_response.json()
        assert resolved["status"] == "resolved"
        assert resolved["resolved_run_id"] == "run-core-late"

        event_response = client.get("/api/soc-events/event-core-pending")
        assert event_response.status_code == 200
        assert event_response.json()["agent_id"] == AGENT_ALPHA
        assert event_response.json()["matched_run_id"] == "run-core-late"

        matched_events = client.get("/api/soc-events", params={"run_id": "run-core-late"})
        assert matched_events.status_code == 200
        assert [item["event_id"] for item in matched_events.json()] == ["event-core-pending"]

        case_response = client.post(
            "/api/feedback-cases",
            json={
                "source_refs": [{"source_kind": "pending_correlation", "source_id": pending_id}],
                "title": "人工修改建议复盘",
            },
        )
        assert case_response.status_code == 200
        feedback_case = case_response.json()
        assert feedback_case["agent_id"] == AGENT_ALPHA
        assert feedback_case["pending_correlation_ids"] == [pending_id]
        assert feedback_case["event_ids"] == ["event-core-pending"]

        sources_response = client.get("/api/feedback-sources")
        assert sources_response.status_code == 200
        sources = {(item["source_kind"], item["source_id"]): item for item in sources_response.json()}
        assert sources[("soc_event", "event-core-pending")]["feedback_case_id"] == feedback_case["feedback_case_id"]
        assert sources[("pending_correlation", pending_id)]["feedback_case_id"] == feedback_case["feedback_case_id"]
