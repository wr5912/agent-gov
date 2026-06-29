import asyncio

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


async def _start_wait(service: ClaudeUserInputService, *, tool_name: str = "Bash", input_data: object | None = None):
    event_queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        service.create_and_wait(
            event_queue=event_queue,
            business_agent_id="main-agent",
            run_id="run-1",
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


def test_tool_permission_allow_once_resolves_sdk_wait_and_redacts_input(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        _event_queue, task, request = await _start_wait(service)

        assert request["request_type"] == "tool_permission"
        assert request["redacted_input"]["api_key"] == "<redacted>"
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
