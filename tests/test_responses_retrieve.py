"""GET /v1/responses/{id} retrieve：从持久化 run 重建、output_text 来自 messages（非截断
answer_summary）、status 由 errors/stop_reason 派生、store=false 公开 404、not-found 404。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _record(module, **overrides) -> str:
    """写一条持久化 run，返回 run_id。"""
    record = {
        "run_id": "run-ret-1",
        "session_id": "sess-ret",
        "sdk_session_id": "sdk-ret",
        "agent_id": "soc-ops",
        "agent_version_id": "ver-ret",
        "answer_summary": "截断摘要不该被当作权威输出",
        "messages": [{"event": "AssistantMessage", "content": [{"text": "完整日报正文"}]}],
        "usage": {"input_tokens": 2, "output_tokens": 4},
        "stop_reason": "end_turn",
        "errors": [],
        "metadata": {"source": "playground"},
    }
    record.update(overrides)
    module.feedback_store.record_run(record)
    return record["run_id"]


def test_retrieve_reconstructs_output_from_messages(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    run_id = _record(module)
    with TestClient(module.app) as client:
        body = client.get(f"/v1/responses/resp_{run_id}").json()
    assert body["id"] == f"resp_{run_id}"
    assert body["status"] == "completed"
    # 权威 output 从 messages 重建，不是截断 answer_summary
    assert body["output"][0]["content"][0]["text"] == "完整日报正文"
    assert body["agentgov"]["output_text"] == "完整日报正文"
    assert body["agentgov"]["conversation_id"] == "conv_sess-ret"
    assert body["agentgov"]["agent_id"] == "soc-ops"


def test_retrieve_status_failed_from_errors(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    run_id = _record(module, run_id="run-fail", errors=["ToolError: boom"])
    with TestClient(module.app) as client:
        body = client.get(f"/v1/responses/resp_{run_id}").json()
    assert body["status"] == "failed"
    assert body["agentgov"]["errors"] == ["ToolError: boom"]


def test_retrieve_store_false_public_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    # store=false 的 run 打了保留标记：公开 retrieve 404（内部审计仍在）
    run_id = _record(module, run_id="run-nostore", metadata={"__agentgov_store__": False, "source": "x"})
    with TestClient(module.app) as client:
        assert client.get(f"/v1/responses/resp_{run_id}").status_code == 404


def test_retrieve_not_found_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.get("/v1/responses/resp_ghost").status_code == 404
        assert client.get("/v1/responses/").status_code in (404, 405)


def test_retrieve_strips_reserved_metadata_on_echo(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    # 即便 run 内部 metadata 混入保留 key，回显也必须剥掉（此 run store 未禁，只是脏 metadata）
    run_id = _record(module, run_id="run-clean", metadata={"__agentgov_internal__": "x", "source": "playground"})
    with TestClient(module.app) as client:
        body = client.get(f"/v1/responses/resp_{run_id}").json()
    assert body["metadata"] == {"source": "playground"}
