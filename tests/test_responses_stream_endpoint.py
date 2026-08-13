"""Responses streaming 端点、持久化与终态契约集成测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.runtime.async_iterators import close_async_iterator
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import ChatRequest
from fastapi.testclient import TestClient

from responses_stream_test_support import (
    ASSISTANT_FRAME as _ASSISTANT,
)
from responses_stream_test_support import (
    DONE_FRAME as _DONE,
)
from responses_stream_test_support import (
    RESULT_FRAME as _RESULT,
)
from responses_stream_test_support import (
    SESSION_FRAME as _SESSION,
)
from responses_stream_test_support import (
    drive_stream as _drive_stream,
)
from responses_stream_test_support import (
    fake_capturing_stream as _fake_capturing_stream,
)
from responses_stream_test_support import (
    fake_sdk_query_success as _fake_sdk_query_success,
)
from responses_stream_test_support import (
    fake_stream as _fake_stream,
)
from responses_stream_test_support import (
    load_app as _load_app,
)
from responses_stream_test_support import (
    parse_sse as _parse,
)
from responses_stream_test_support import (
    patch_sdk_query as _patch_sdk_query,
)
from responses_stream_test_support import (
    register_business_agent as _register_biz,
)


def test_endpoint_stream_control(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "stream_events", _fake_stream([_SESSION, _ASSISTANT, _RESULT, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True, "agentgov": {"agent_id": "soc-ops"}})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-agentgov-run-id"] == "run-9"
        assert resp.headers["x-agentgov-session-id"] == "sess-9"
        names = [n for n, _ in _parse(resp.text)]
        assert "response.created" in names and "response.output_text.delta" in names and "agentgov.session" in names


def test_endpoint_stream_control_maps_request_fields(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "stream_events", _fake_capturing_stream(captured, [_SESSION, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={
                "model": "claude-sonnet-5",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "流式输入"}]}],
                "instructions": "只输出正文",
                "stream": True,
                "store": False,
                "metadata": {"source": "playground", "__agentgov_store__": True},
                "agentgov": {"agent_id": "soc-ops", "alert_id": "alert-1", "case_id": "case-1", "max_turns": 7},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")

    req = captured["req"]
    assert req.message == "流式输入"
    assert req.model == "claude-sonnet-5"
    assert req.agent_id == "soc-ops"
    assert req.alert_id == "alert-1"
    assert req.case_id == "case-1"
    assert req.max_turns == 7
    assert req.system_append == "只输出正文"
    assert req.metadata == {
        "source": "playground",
        "__agentgov_response_mode__": "control",
        "__agentgov_store__": False,
    }
    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")


def test_endpoint_stream_strict_uses_configured_agent_without_agentgov_events(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "stream_events", _fake_capturing_stream(captured, [_SESSION, _ASSISTANT, _RESULT, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True})
        assert resp.status_code == 200, resp.text

    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")
    names = [name for name, _ in _parse(resp.text)]
    assert "response.completed" in names
    assert all(not name.startswith("agentgov.") for name in names)


def test_endpoint_stream_projects_hitl_confirmation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    required = {
        "event": "claude_user_input_required",
        "data": {
            "request_id": "cur-1",
            "decision_token": "tok-secret",
            "request_type": "tool_permission",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "tool_name": "Bash",
            "input": {"command": "echo hi"},
            "risk": {"level": "medium"},
        },
    }
    resolved = {
        "event": "claude_user_input_resolved",
        "data": {
            "request_id": "cur-1",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "status": "resolved",
            "decision": "allow_once",
            "decided_by": "tester",
        },
    }
    monkeypatch.setattr(module.runtime, "stream_events", _fake_stream([_SESSION, required, resolved, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True, "agentgov": {"agent_id": "soc-ops"}})
        assert resp.status_code == 200, resp.text

    by = dict(_parse(resp.text))
    assert by["agentgov.confirmation.requested"]["payload"]["decision_token"] == "tok-secret"
    assert by["agentgov.confirmation.requested"]["payload"]["tool_input"] == {"command": "echo hi"}
    assert by["agentgov.confirmation.resolved"]["payload"]["decision"] == "allow_once"
    assert "decision_token" not in by["agentgov.confirmation.resolved"]["payload"]


def test_endpoint_stream_fails_closed_for_unmigratable_previous_response_session(monkeypatch, tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    calls: list[str | None] = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        await anext(prompt)
        if len(calls) == 1:
            raise RuntimeError("No conversation found with session ID: stale-sdk")
        yield AssistantMessage(content=[TextBlock(text="responses stream after retry")], model="<synthetic>", session_id="new-sdk")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-sdk",
            result="responses stream after retry",
        )

    _patch_sdk_query(monkeypatch, fake_query)

    module = _load_app(monkeypatch, tmp_path)
    module.feedback_store.record_run({"run_id": "prev-stale", "session_id": "sess-stale", "agent_id": DEFAULT_BUSINESS_AGENT_ID})
    session = module.session_store.get_or_create_owned("sess-stale", agent_id=DEFAULT_BUSINESS_AGENT_ID)
    session.sdk_session_id = "stale-sdk"
    module.session_store.save(session)

    with TestClient(module.app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "input": "continue",
                "stream": True,
                "previous_response_id": "resp_prev-stale",
                "agentgov": {"agent_id": DEFAULT_BUSINESS_AGENT_ID},
            },
        )
        assert resp.status_code == 200, resp.text

    names = [name for name, _ in _parse(resp.text)]
    assert calls == []
    assert "response.failed" in names
    assert "response.completed" not in names
    saved = module.session_store.get("sess-stale")
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0


def test_stream_persists_session_and_run_before_done_terminal(monkeypatch, tmp_path: Path) -> None:
    # race 回归：result 只是 SDK 进度事实；在 done（-> response.completed）时刻，
    # session（sdk_session_id+agent_id）与 run 必须已落库，使 items/retrieve 在公开终态即可查。
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-race"))
    module = _load_app(monkeypatch, tmp_path)

    at_terminal: dict = {}
    run_id: dict = {}
    result_sdk_session_id: dict = {}

    def check(ev):
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if data.get("run_id"):
            run_id["v"] = data["run_id"]
        if ev.get("event") == "result":
            result_sdk_session_id["v"] = data.get("sdk_session_id")
        if ev.get("event") == "done":
            s = module.session_store.get("sess-race")
            at_terminal["sdk_session_id"] = s.sdk_session_id if s else None
            at_terminal["agent_id"] = s.agent_id if s else None
            at_terminal["run_found"] = bool(run_id.get("v")) and module.feedback_store.find_run(run_id=run_id["v"]) is not None

    _drive_stream(module, ChatRequest(message="hi", session_id="sess-race"), on_event=check)

    assert at_terminal.get("sdk_session_id") == result_sdk_session_id.get("v")
    assert at_terminal.get("sdk_session_id")
    assert at_terminal.get("agent_id")  # agent_id 非空（否则 items 会从空列表退化为 500）
    assert at_terminal.get("run_found") is True  # run 已记录（retrieve 完成即可查）


def test_stream_run_write_failure_rolls_back_session_completion(monkeypatch, tmp_path: Path) -> None:
    import app.runtime.session_turn_persistence as turn_persistence_module

    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-rollback"))
    module = _load_app(monkeypatch, tmp_path)

    calls = 0

    def fail_run_write(db, record):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected run write failure")

    monkeypatch.setattr(turn_persistence_module, "upsert_agent_run_record", fail_run_write)
    events = _drive_stream(module, ChatRequest(message="hi", session_id="sess-rollback"))
    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]
    saved = module.session_store.get("sess-rollback")

    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id == run_id
    assert module.feedback_store.find_run(run_id=run_id) is None
    error_event = next(event for event in events if event.get("event") == "error")
    assert error_event["data"]["error_code"] == "RUNTIME_FINALIZATION_FAILED"
    assert error_event["data"]["recovery_status"] == "deferred_to_lease_expiry"
    assert calls == 4  # 三次 finalize + 一次原子 interrupted 恢复；持续失败时保留 lease 交给过期对账器。


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit"])
def test_stream_retries_transient_turn_finalization(
    monkeypatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success(f"sdk-retry-{failure_point}"))
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.finalize_persisted_turn
    calls = 0

    def flaky_finalize(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_point == "after_commit":
                original(**kwargs)
            raise RuntimeError(f"transient {failure_point}")
        return original(**kwargs)

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", flaky_finalize)
    events = _drive_stream(
        module,
        ChatRequest(message="retry finalization", session_id=f"sess-retry-{failure_point}"),
    )

    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]
    saved = module.session_store.get(f"sess-retry-{failure_point}")
    assert calls == 2
    assert [event.get("event") for event in events][-2:] == ["result", "done"]
    assert saved is not None and saved.turns == 1 and saved.active_run_id is None
    assert module.feedback_store.find_run(run_id=run_id) is not None


def test_stream_finalization_exhaustion_interrupts_and_allows_immediate_retry(monkeypatch, tmp_path: Path) -> None:
    from app.runtime.errors import SessionConflictError
    from app.runtime.runtime_db import SessionTurnIntentModel

    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-finalize-exhausted"))
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.finalize_persisted_turn
    calls = 0

    def fail_finalize(**kwargs):
        nonlocal calls
        calls += 1
        raise SessionConflictError("injected finalization version conflict")

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", fail_finalize)
    events = _drive_stream(module, ChatRequest(message="first", session_id="sess-finalize-exhausted"))
    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]

    assert calls == 3
    # SDK ResultMessage 已经真实发生，故 result 作为进度事实保留；持久化失败由后续
    # error 覆盖最终状态，Responses projector 只会在 done 时发布 response.failed。
    assert any(event.get("event") == "result" for event in events)
    error_event = next(event for event in events if event.get("event") == "error")
    assert error_event["data"]["error_code"] == "RUNTIME_FINALIZATION_FAILED"
    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    saved = module.session_store.get("sess-finalize-exhausted")
    assert saved is not None and saved.active_run_id is None and saved.turns == 0
    with module.session_store.Session() as db:
        intent = db.get(SessionTurnIntentModel, run_id)
        assert intent is not None and intent.status == "interrupted"

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", original)
    retried = _drive_stream(module, ChatRequest(message="retry", session_id="sess-finalize-exhausted"))
    assert any(event.get("event") == "result" for event in retried)
    saved = module.session_store.get("sess-finalize-exhausted")
    assert saved is not None and saved.active_run_id is None and saved.turns == 1


def test_stream_persists_exactly_once(monkeypatch, tmp_path: Path) -> None:
    # 幂等：SDK 原生消息源排空后的主动落库 + finalize 兜底不得双落库。
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-once"))
    module = _load_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    original = module.runtime._complete_runtime_request

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(module.runtime, "_complete_runtime_request", counting)
    _drive_stream(module, ChatRequest(message="hi", session_id="sess-once"))
    assert calls["n"] == 1  # 恰好落库一次


def test_stream_syncs_trace_before_done_allows_client_to_disconnect(monkeypatch, tmp_path: Path) -> None:
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-trace-before-done"))
    module = _load_app(monkeypatch, tmp_path)
    trace_upserts = []
    monkeypatch.setattr(module.runtime.langfuse, "current_trace_ref", lambda: ("trace-before-done", None))
    monkeypatch.setattr(
        module.runtime.langfuse,
        "upsert_trace",
        lambda trace_id, **kwargs: trace_upserts.append({"trace_id": trace_id, **kwargs}),
    )

    async def consume_until_done() -> None:
        source = module.runtime.stream(ChatRequest(message="hi", session_id="sess-trace-before-done"))
        try:
            async for event in source:
                if event.get("event") == "done":
                    assert trace_upserts
                    break
        finally:
            await close_async_iterator(source)

    asyncio.run(consume_until_done())

    assert trace_upserts[0]["trace_id"] == "trace-before-done"
    assert trace_upserts[0]["output"]["answer"] == "收到"


def test_stream_error_path_persists_once_in_finally(monkeypatch, tmp_path: Path) -> None:
    # error/无 ResultMessage 路径：finally 兜底落库一次，仍发 error+done。
    async def fake_query(*, prompt, options, transport=None):
        await anext(prompt)
        raise RuntimeError("boom before result")
        yield  # pragma: no cover

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    original = module.runtime._abort_runtime_request

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(module.runtime, "_abort_runtime_request", counting)
    events = _drive_stream(module, ChatRequest(message="hi", session_id="sess-err"))
    names = [e.get("event") for e in events]
    assert calls["n"] == 1
    assert "error" in names and "done" in names
    saved = module.session_store.get("sess-err")
    assert saved is not None and saved.turns == 0 and saved.active_run_id is None


def test_endpoint_empty_sdk_stream_fails_and_retrieve_preserves_error(monkeypatch, tmp_path: Path) -> None:
    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        if False:
            yield None

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={
                "input": "empty SDK stream",
                "stream": True,
                "conversation": "conv_missing-result-stream",
                "agentgov": {"agent_id": "soc-ops"},
            },
        )
        events = _parse(response.text)
        by = dict(events)
        run_id = by["agentgov.session"]["payload"]["run_id"]
        retrieved = client.get(f"/v1/responses/resp_{run_id}")

    names = [name for name, _ in events]
    assert response.status_code == 200
    assert names.count("response.failed") == 1
    assert "response.completed" not in names
    assert by["response.failed"]["error"]["error_code"] == "STREAM_TERMINATED_WITHOUT_RESULT"
    assert retrieved.status_code == 200
    assert retrieved.json()["status"] == "failed"
    assert retrieved.json()["agentgov"]["errors"] == ["SDK query ended without ResultMessage"]


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit"])
def test_stream_retries_transient_turn_abort(
    monkeypatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        raise RuntimeError("query failed before result")
        yield  # pragma: no cover

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.abort_persisted_turn
    calls = 0

    def flaky_abort(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_point == "after_commit":
                original(**kwargs)
            raise RuntimeError(f"transient {failure_point}")
        return original(**kwargs)

    monkeypatch.setattr(module.session_store, "abort_persisted_turn", flaky_abort)
    events = _drive_stream(
        module,
        ChatRequest(message="retry abort", session_id=f"sess-abort-{failure_point}"),
    )

    saved = module.session_store.get(f"sess-abort-{failure_point}")
    assert calls == 2
    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    assert saved is not None and saved.turns == 0 and saved.active_run_id is None


def test_endpoint_sdk_result_error_is_failed_terminal_for_control_client(monkeypatch, tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "sdk-error-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="bad model")], model="<synthetic>", session_id=sdk_session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id=sdk_session_id,
            result="bad model",
            api_error_status=404,
        )

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "stream": True, "conversation": "conv_sdk-error-api", "agentgov": {"agent_id": "soc-ops"}},
        )

    assert response.status_code == 200
    events = _parse(response.text)
    names = [name for name, _ in events]
    assert "response.completed" not in names
    assert names.count("response.failed") == 1
    assert names.count("agentgov.error") == 1
    by = dict(events)
    assert by["agentgov.error"]["payload"]["errors"] == ["Claude Code API error (404): bad model"]
    assert by["agentgov.result"]["payload"]["sdk_session_id"]
