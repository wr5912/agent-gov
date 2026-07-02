import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from app.routers.claude_user_input import create_claude_user_input_router
from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.claude_user_input_service import (
    ClaudeUserInputConflict,
    ClaudeUserInputInvalid,
    ClaudeUserInputService,
)
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _service(tmp_path, *, timeout_seconds: int = 5) -> ClaudeUserInputService:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    return ClaudeUserInputService(ClaudeUserInputStore(factory), timeout_seconds=timeout_seconds)


def _decision(token: str, **overrides: object) -> ClaudeUserInputDecisionRequest:
    data: dict[str, object] = {
        "action": "allow_once",
        "decision_token": token,
        "run_id": "run-1",
        "session_id": "sess-1",
        "business_agent_id": "main-agent",
    }
    data.update(overrides)
    return ClaudeUserInputDecisionRequest.model_validate(data)


async def _start_wait(
    service: ClaudeUserInputService,
    *,
    tool_name: str = "Bash",
    input_data: object | None = None,
    run_id: str = "run-1",
):
    event_queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        service.create_and_wait(
            event_queue=event_queue,
            business_agent_id="main-agent",
            run_id=run_id,
            api_session_id="sess-1",
            sdk_session_id="sdk-1",
            tool_name=tool_name,
            input_data=input_data if input_data is not None else {"command": "echo safe", "api_key": "sk-secret"},
            context={"tool_use_id": "toolu-1", "agent_id": "subagent-1"},
        )
    )
    event = await event_queue.get()
    request = event["data"]
    return event_queue, task, request


def test_tool_permission_allow_once_resolves_sdk_wait_and_keeps_debug_input(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service)

        assert request["request_type"] == "tool_permission"
        assert request["input"]["api_key"] == "sk-secret"
        assert request["context"]["tool_use_id"] == "toolu-1"
        assert request["business_agent_id"] == "main-agent"

        record = service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"]),
            decided_by="tester",
        )
        sdk_decision = await task

        assert sdk_decision.action == "allow_once"
        assert record.status == "resolved"
        assert record.decision == "allow_once"
        stored = service.list_requests(run_id="run-1")[0]
        assert stored.decision_token_hash != request["decision_token"]

    asyncio.run(scenario())


def test_user_input_request_expiry_uses_configured_timeout(tmp_path):
    async def scenario():
        service = _service(tmp_path, timeout_seconds=11)
        before = datetime.now(timezone.utc)
        _event_queue, task, request = await _start_wait(service)

        expires_at = datetime.fromisoformat(request["expires_at"])
        assert before + timedelta(seconds=9) <= expires_at <= before + timedelta(seconds=13)

        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="deny"),
            decided_by="tester",
        )
        assert (await task).action == "deny"

    asyncio.run(scenario())


def test_tool_permission_rejects_answer_question_and_context_mismatch(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service)

        with pytest.raises(ClaudeUserInputConflict):
            service.submit_decision(
                request["request_id"],
                decision=_decision(request["decision_token"], run_id="wrong-run"),
                decided_by="tester",
            )
        with pytest.raises(ClaudeUserInputInvalid):
            service.submit_decision(
                request["request_id"],
                decision=_decision(request["decision_token"], action="answer_question", answers={"q1": "A"}),
                decided_by="tester",
            )

        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="deny", message="not approved"),
            decided_by="tester",
        )
        sdk_decision = await task
        assert sdk_decision.action == "deny"
        assert sdk_decision.message == "not approved"

    asyncio.run(scenario())


def test_tool_permission_allow_for_run_auto_allows_current_run_tool_permissions(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        event_queue, task, request = await _start_wait(service, input_data={"command": "date +%F"})

        assert request["risk"]["run_allow_eligible"] is True
        assert request["risk"]["run_allow_category"] == "bash_clock_read"

        record = service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="allow_for_run"),
            decided_by="tester",
        )
        sdk_decision = await task

        assert sdk_decision.action == "allow_once"
        assert record.decision == "allow_for_run"
        resolved_event = await event_queue.get()
        assert resolved_event["event"] == "claude_user_input_resolved"

        second_decision = await service.create_and_wait(
            event_queue=event_queue,
            business_agent_id="main-agent",
            run_id="run-1",
            api_session_id="sess-1",
            sdk_session_id="sdk-1",
            tool_name="Write",
            input_data={"file_path": "/data/reports/smoke.md", "content": "ok"},
            context={"tool_use_id": "toolu-2", "agent_id": "subagent-1"},
        )

        assert second_decision.action == "allow_once"
        assert event_queue.empty()

        # Product contract: allow_for_run grants the whole current run, including later high-risk tools.
        third_decision = await service.create_and_wait(
            event_queue=event_queue,
            business_agent_id="main-agent",
            run_id="run-1",
            api_session_id="sess-1",
            sdk_session_id="sdk-1",
            tool_name="Bash",
            input_data={"command": "rm -rf /tmp/agentgov-hitl-smoke"},
            context={"tool_use_id": "toolu-3", "agent_id": "subagent-1"},
        )

        assert third_decision.action == "allow_once"
        assert event_queue.empty()

        service.clear_run_grants("run-1")
        _fourth_queue, fourth_task, fourth_request = await _start_wait(service, input_data={"command": "date +%F"})
        assert fourth_request["risk"]["run_allow_eligible"] is True
        service.submit_decision(
            fourth_request["request_id"],
            decision=_decision(fourth_request["decision_token"], action="deny"),
            decided_by="tester",
        )
        assert (await fourth_task).action == "deny"

    asyncio.run(scenario())


def test_read_only_bash_probe_with_fallback_stays_run_allow_eligible(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(
            service,
            input_data={"command": 'ls mcp_docs/ 2>/dev/null || echo "no mcp_docs dir"'},
        )

        assert request["risk"]["run_allow_eligible"] is True
        assert request["risk"]["run_allow_category"] == "bash_read_only"
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="deny"),
            decided_by="tester",
        )
        assert (await task).action == "deny"

    asyncio.run(scenario())


def test_read_only_bash_run_grant_auto_allows_next_safe_probe(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        event_queue, task, request = await _start_wait(
            service,
            input_data={"command": 'ls mcp_docs/ 2>/dev/null || echo "no mcp_docs dir"'},
        )
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="allow_for_run"),
            decided_by="tester",
        )
        assert (await task).action == "allow_once"
        assert (await event_queue.get())["event"] == "claude_user_input_resolved"

        second_decision = await service.create_and_wait(
            event_queue=event_queue,
            business_agent_id="main-agent",
            run_id="run-1",
            api_session_id="sess-1",
            sdk_session_id="sdk-1",
            tool_name="Bash",
            input_data={
                "command": "find /data/business-agents/main-agent/workspace -maxdepth 4 -type f "
                "2>/dev/null | grep -v node_modules | grep -v .git | head -60"
            },
            context={"tool_use_id": "toolu-2", "agent_id": "subagent-1"},
        )

        assert second_decision.action == "allow_once"
        assert event_queue.empty()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        "ls >/tmp/out",
        "find . -delete",
        "sed -i s/a/b/ templates/reports/daily-secops-report.md",
        "cat /data/runtime.sqlite3",
        "cat /data/business-agents/main-agent/workspace/.env",
        "ls; rm -rf /",
    ],
)
def test_mutating_or_private_bash_keeps_high_risk_run_allow_available_by_design(tmp_path, command):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service, input_data={"command": command})

        assert request["risk"]["level"] == "high"
        assert request["risk"]["run_allow_eligible"] is True
        assert "run_allow_category" not in request["risk"]
        assert request["risk"]["run_allow_scope"] == "run"
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="deny"),
            decided_by="tester",
        )
        assert (await task).action == "deny"

    asyncio.run(scenario())


def test_allow_for_run_does_not_cross_run_boundary(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service, input_data={"command": "date +%F"})
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="allow_for_run"),
            decided_by="tester",
        )
        assert (await task).action == "allow_once"
        assert (await _event_queue.get())["event"] == "claude_user_input_resolved"

        other_run_queue, other_run_task, other_run_request = await _start_wait(
            service,
            input_data={"command": "date +%F"},
            run_id="run-2",
        )
        assert other_run_request["run_id"] == "run-2"
        assert other_run_request["risk"]["run_allow_eligible"] is True
        service.submit_decision(
            other_run_request["request_id"],
            decision=_decision(other_run_request["decision_token"], action="deny", run_id="run-2"),
            decided_by="tester",
        )
        assert (await other_run_task).action == "deny"
        assert (await other_run_queue.get())["event"] == "claude_user_input_resolved"

        high_risk_decision = await service.create_and_wait(
            event_queue=_event_queue,
            business_agent_id="main-agent",
            run_id="run-1",
            api_session_id="sess-1",
            sdk_session_id="sdk-1",
            tool_name="Bash",
            input_data={"command": "rm -rf /tmp/agentgov-hitl-smoke"},
            context={"tool_use_id": "toolu-3", "agent_id": "subagent-1"},
        )
        assert high_risk_decision.action == "allow_once"
        assert _event_queue.empty()

    asyncio.run(scenario())


def test_ask_user_question_free_text_becomes_updated_input(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        input_data = {
            "questions": [
                {
                    "header": "范围",
                    "question": "要处理哪些资产？",
                    "options": [{"label": "当前告警资产"}, {"label": "全部相关资产"}],
                }
            ]
        }
        _event_queue, task, request = await _start_wait(service, tool_name="AskUserQuestion", input_data=input_data)

        record = service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="answer_question", response="只处理当前告警资产"),
            decided_by="tester",
        )
        sdk_decision = await task

        assert record.decision == "answer_question"
        assert sdk_decision.action == "answer_question"
        assert sdk_decision.ask_user_question_input == {
            "questions": input_data["questions"],
            "response": "只处理当前告警资产",
        }

    asyncio.run(scenario())


def test_ask_user_question_rejects_allow_for_run(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(
            service,
            tool_name="AskUserQuestion",
            input_data={"questions": [{"question": "继续吗？", "options": [{"label": "继续"}]}]},
        )

        assert request["risk"]["run_allow_eligible"] is False
        with pytest.raises(ClaudeUserInputInvalid):
            service.submit_decision(
                request["request_id"],
                decision=_decision(request["decision_token"], action="allow_for_run"),
                decided_by="tester",
            )
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="answer_question", response="继续"),
            decided_by="tester",
        )
        assert (await task).action == "answer_question"

    asyncio.run(scenario())


def test_write_report_path_is_run_allow_eligible(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(
            service,
            tool_name="Write",
            input_data={"file_path": "/data/reports/2026/06/29/daily.md", "content": "report"},
        )

        assert request["risk"]["run_allow_eligible"] is True
        assert request["risk"]["run_allow_category"] == "report_write"
        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"], action="deny"),
            decided_by="tester",
        )
        assert (await task).action == "deny"

    asyncio.run(scenario())


def test_cancel_orphan_waiting_requests_blocks_late_decision(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, _task, request = await _start_wait(service)

        cancelled = service.cancel_orphan_waiting_requests(reason="service_restarted")

        assert len(cancelled) == 1
        assert cancelled[0].status == "cancelled"
        assert cancelled[0].decision == "service_restarted"
        with pytest.raises(ClaudeUserInputConflict):
            service.submit_decision(
                request["request_id"],
                decision=_decision(request["decision_token"]),
                decided_by="tester",
            )

    asyncio.run(scenario())


def test_decision_api_rejects_allow_modified_and_updated_input_extra(tmp_path):
    service = _service(tmp_path)
    app = FastAPI()
    app.include_router(create_claude_user_input_router(service=service, require_api_key=lambda: None))
    client = TestClient(app)

    async def create_request():
        _event_queue, _task, request = await _start_wait(service)
        return request

    request = asyncio.run(create_request())
    base = {
        "decision_token": request["decision_token"],
        "run_id": request["run_id"],
        "session_id": request["session_id"],
        "business_agent_id": request["business_agent_id"],
    }

    allow_modified = client.post(
        f"/api/claude-user-input-requests/{request['request_id']}/decision",
        json={**base, "action": "allow_modified"},
    )
    updated_input = client.post(
        f"/api/claude-user-input-requests/{request['request_id']}/decision",
        json={**base, "action": "allow_once", "updated_input": {"command": "rm -rf /"}},
    )

    assert allow_modified.status_code == 422
    assert updated_input.status_code == 422


def test_decision_api_rejects_unknown_request_and_wrong_token(tmp_path):
    service = _service(tmp_path)
    app = FastAPI()
    app.include_router(create_claude_user_input_router(service=service, require_api_key=lambda: None))
    client = TestClient(app)

    async def create_request() -> dict[str, object]:
        _event_queue, _task, request = await _start_wait(service)
        return request

    request = asyncio.run(create_request())
    base = {
        "action": "allow_once",
        "decision_token": request["decision_token"],
        "run_id": request["run_id"],
        "session_id": request["session_id"],
        "business_agent_id": request["business_agent_id"],
    }

    missing = client.post("/api/claude-user-input-requests/cur-missing/decision", json=base)
    wrong_token = client.post(
        f"/api/claude-user-input-requests/{request['request_id']}/decision",
        json={**base, "decision_token": "wrong-token"},
    )

    assert missing.status_code == 404
    assert wrong_token.status_code == 409
    assert "token is invalid" in wrong_token.json()["detail"]


def test_submit_decision_rejects_duplicate_submit(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service)

        service.submit_decision(
            request["request_id"],
            decision=_decision(request["decision_token"]),
            decided_by="tester",
        )
        assert (await task).action == "allow_once"
        with pytest.raises(ClaudeUserInputConflict, match="already resolved"):
            service.submit_decision(
                request["request_id"],
                decision=_decision(request["decision_token"]),
                decided_by="tester",
            )

    asyncio.run(scenario())
