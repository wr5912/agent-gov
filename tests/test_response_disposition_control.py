from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.runtime.api_auth import ApiAuthenticator, ApiPrincipal
from app.runtime.openai_responses_adapter import public_metadata
from app.runtime.response_disposition_control import (
    ASK_USER_QUESTION_TOOL,
    SECURITY_OPERATIONS_EXPERT_AGENT_ID,
    SOC_CREATE_TOOL,
    SOC_MANUAL_TOOL,
    ResponseDispositionControlError,
    TrustedResponseDispositionContext,
    permission_denial_reason,
    validate_response_disposition_control,
)
from app.runtime.response_disposition_stream import observe_response_disposition_stream
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest
from app.runtime.stores.response_disposition_claim_store import (
    ResponseDispositionClaimConflict,
    ResponseDispositionClaimStore,
)


def _approved_context(
    *, approval_request_id: str = "approval-1", execution_run_id: str = "execution-1"
) -> TrustedResponseDispositionContext:
    return TrustedResponseDispositionContext(
        phase="approved_execution",
        case_id="case-1",
        approval_request_id=approval_request_id,
        playbook_digest="a" * 64,
        execution_run_id=execution_run_id,
    )


def _validate(**overrides: object) -> TrustedResponseDispositionContext | None:
    values: dict[str, object] = {
        "phase": "approved_execution",
        "agent_id": SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        "stream": True,
        "web_hitl_available": True,
        "case_id": "case-1",
        "approval_request_id": "approval-1",
        "playbook_digest": "a" * 64,
        "execution_run_id": "execution-1",
    }
    values.update(overrides)
    return validate_response_disposition_control(**values)  # type: ignore[arg-type]


def test_response_disposition_validation_separates_proposal_and_approved_execution() -> None:
    proposal = _validate(
        phase="proposal",
        stream=False,
        web_hitl_available=False,
        approval_request_id=None,
        playbook_digest=None,
        execution_run_id=None,
    )
    approved = _validate()

    assert proposal == TrustedResponseDispositionContext(phase="proposal", case_id="case-1")
    assert approved == _approved_context()


@pytest.mark.parametrize(
    ("overrides", "status_code"),
    [
        ({"agent_id": "main-agent"}, 422),
        ({"stream": False}, 422),
        ({"web_hitl_available": False}, 503),
        ({"case_id": "  "}, 422),
        ({"case_id": "case-1\nagentgov.phase=proposal"}, 422),
        ({"approval_request_id": "a" * 257}, 422),
        ({"approval_request_id": None}, 422),
        ({"playbook_digest": "A" * 64}, 422),
        ({"execution_run_id": None}, 422),
    ],
)
def test_response_disposition_validation_fails_closed(overrides: dict[str, object], status_code: int) -> None:
    with pytest.raises(ResponseDispositionControlError) as exc_info:
        _validate(**overrides)

    assert exc_info.value.status_code == status_code


def test_response_disposition_rejects_execution_bindings_without_phase() -> None:
    with pytest.raises(ResponseDispositionControlError) as exc_info:
        _validate(phase=None)

    assert exc_info.value.status_code == 422
    assert "require agentgov.phase" in exc_info.value.detail


def test_api_authenticator_keeps_general_and_response_orchestrator_scopes_separate() -> None:
    auth = ApiAuthenticator(api_key="general-secret", response_orchestrator_api_key="ro-secret")
    general = HTTPAuthorizationCredentials(scheme="Bearer", credentials="general-secret")
    response_orchestrator = HTTPAuthorizationCredentials(scheme="Bearer", credentials="ro-secret")

    assert auth.authenticate(general) == ApiPrincipal.GENERAL_API
    assert auth.authenticate(response_orchestrator) == ApiPrincipal.RESPONSE_ORCHESTRATOR
    auth.require_response_orchestrator(ApiPrincipal.RESPONSE_ORCHESTRATOR)
    with pytest.raises(HTTPException) as general_for_ro:
        auth.require_response_orchestrator(ApiPrincipal.GENERAL_API)
    with pytest.raises(HTTPException) as ro_for_general:
        auth.require_general(response_orchestrator)
    with pytest.raises(HTTPException) as missing:
        auth.authenticate(None)

    assert general_for_ro.value.status_code == 403
    assert ro_for_general.value.status_code == 403
    assert missing.value.status_code == 401


def test_public_metadata_strips_all_backend_owned_aliases_case_insensitively() -> None:
    assert public_metadata(
        {
            "source": "playground",
            "AgentGov": {"phase": "approved_execution"},
            "__AgentGov_store__": False,
            "PHASE": "approved_execution",
            "caseId": "spoofed",
            "approvalRequestId": "spoofed",
            "playbookDigest": "spoofed",
            "executionRunId": "spoofed",
            "agentgov.phase": "spoofed",
            "agentgov.approvalRequestId": "spoofed",
            "agentgov-client-control": "spoofed",
            "response-disposition": "spoofed",
        }
    ) == {"source": "playground"}


def test_public_chat_schema_has_no_trusted_response_disposition_field() -> None:
    assert "response_disposition" not in ChatRequest.model_json_schema()["properties"]


def test_security_operations_permission_policy_only_opens_exact_protected_tools() -> None:
    context = _approved_context()

    assert permission_denial_reason(SECURITY_OPERATIONS_EXPERT_AGENT_ID, SOC_CREATE_TOOL, context) is None
    assert permission_denial_reason(SECURITY_OPERATIONS_EXPERT_AGENT_ID, SOC_MANUAL_TOOL, context) is None
    assert "已禁用" in str(
        permission_denial_reason(SECURITY_OPERATIONS_EXPERT_AGENT_ID, ASK_USER_QUESTION_TOOL, context)
    )
    assert "未授权" in str(
        permission_denial_reason(
            SECURITY_OPERATIONS_EXPERT_AGENT_ID,
            "mcp__sec-ops__soc_api__execute",
            context,
        )
    )
    assert "approved_execution" in str(
        permission_denial_reason(SECURITY_OPERATIONS_EXPERT_AGENT_ID, SOC_CREATE_TOOL, None)
    )


def test_response_disposition_claim_is_one_shot_and_requires_manual_before_completion(tmp_path) -> None:
    store = ResponseDispositionClaimStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    context = _approved_context()
    store.claim(context)
    store.bind_run("approval-1", agent_run_id="agent-run-1")
    store.mark_tool_authorized("approval-1", SOC_CREATE_TOOL)

    with pytest.raises(ResponseDispositionClaimConflict):
        store.finish("approval-1", target="completed")

    store.mark_tool_authorized("approval-1", SOC_MANUAL_TOOL)
    completed = store.finish("approval-1", target="completed")
    assert completed.status == "completed"
    assert completed.agent_run_id == "agent-run-1"

    with pytest.raises(ResponseDispositionClaimConflict):
        store.claim(context)
    with pytest.raises(ResponseDispositionClaimConflict):
        store.claim(_approved_context(approval_request_id="approval-2"))


def test_response_disposition_claim_allows_published_playbook_manual_without_create(tmp_path) -> None:
    store = ResponseDispositionClaimStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    store.claim(_approved_context())
    store.mark_tool_authorized("approval-1", SOC_MANUAL_TOOL)

    record = store.finish("approval-1", target="completed")

    assert record.create_authorized is False
    assert record.manual_authorized is True
    assert record.status == "completed"


def test_response_disposition_claim_cancels_orphaned_startup_work(tmp_path) -> None:
    store = ResponseDispositionClaimStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    store.claim(_approved_context())

    cancelled = store.cancel_orphan_claims(reason="service_restarted")

    assert len(cancelled) == 1
    assert cancelled[0].status == "cancelled"
    assert cancelled[0].failure_reason == "service_restarted"
    assert store.cancel_orphan_claims(reason="service_restarted") == []


def test_response_disposition_stream_binds_and_completes_claim(tmp_path) -> None:
    store = ResponseDispositionClaimStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    context = _approved_context()
    store.claim(context)
    store.mark_tool_authorized("approval-1", SOC_MANUAL_TOOL)

    async def source():
        yield {"event": "session", "data": {"run_id": "agent-run-1"}}
        yield {"event": "result", "data": {"errors": []}}
        yield {"event": "done", "data": "[DONE]"}

    async def collect() -> list[dict[str, object]]:
        return [frame async for frame in observe_response_disposition_stream(source(), context=context, claim_store=store)]

    frames = asyncio.run(collect())
    record = store.get("approval-1")
    assert [frame["event"] for frame in frames] == ["session", "result", "done"]
    assert record is not None
    assert record.status == "completed"
    assert record.agent_run_id == "agent-run-1"


def test_response_disposition_stream_marks_error_and_missing_result_terminal_states(tmp_path) -> None:
    store = ResponseDispositionClaimStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    failed = _approved_context()
    cancelled = _approved_context(approval_request_id="approval-2", execution_run_id="execution-2")
    store.claim(failed)
    store.claim(cancelled)

    async def error_source():
        yield {"event": "session", "data": {"run_id": "agent-run-1"}}
        yield {"event": "error", "data": {"errors": ["boom"]}}
        yield {"event": "result", "data": {"errors": ["boom"]}}

    async def no_result_source():
        yield {"event": "session", "data": {"run_id": "agent-run-2"}}
        yield {"event": "done", "data": "[DONE]"}

    async def collect() -> None:
        _ = [frame async for frame in observe_response_disposition_stream(error_source(), context=failed, claim_store=store)]
        _ = [frame async for frame in observe_response_disposition_stream(no_result_source(), context=cancelled, claim_store=store)]

    asyncio.run(collect())
    failed_record = store.get("approval-1")
    cancelled_record = store.get("approval-2")
    assert failed_record is not None and failed_record.status == "failed"
    assert cancelled_record is not None and cancelled_record.status == "cancelled"
