from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts import run_agentscope_live_acceptance as live
from scripts.agentscope_live_native_chat import native_chat_body, native_lookup_params, native_receipt_run_id, require_native_run_identity

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_native_live_chat_builds_only_native_fields_and_preserves_ordered_input_ids() -> None:
    from app.runtime_gateway.native_chat_input import RuntimeChatRequest

    native_input = {"id": "input-once", "name": "user", "role": "user", "content": [{"type": "text", "text": "契约输入"}]}
    body = native_chat_body("runtime-agent", "root-session", native_input)
    assert set(body) == {"agent_id", "session_id", "input"}
    assert RuntimeChatRequest.model_validate(body).raw_input == native_input
    params = native_lookup_params("runtime-agent", "root-session", [{"id": "second"}, {"id": "first"}])
    assert params == [
        ("agent_id", "runtime-agent"),
        ("session_id", "root-session"),
        ("operation_kind", "initial"),
        ("input_id", "second"),
        ("input_id", "first"),
    ]
    assert native_chat_body("runtime-agent", "root-session", native_input) == body


@pytest.mark.parametrize("input_value", [None, {}, [], [{"id": "one"}, {}]])
def test_native_live_lookup_requires_all_explicit_input_ids(input_value) -> None:
    with pytest.raises(live.LiveAcceptanceError, match="NATIVE_INPUT_ID_REQUIRED"):
        native_lookup_params("runtime-agent", "root-session", input_value)


@pytest.mark.parametrize("event_type,kind", [("USER_CONFIRM_RESULT", "user_confirmation"), ("EXTERNAL_EXECUTION_RESULT", "external_execution")])
def test_native_live_lookup_reuses_public_event_kind_contract(event_type, kind) -> None:
    assert ("operation_kind", kind) in native_lookup_params("runtime-agent", "root-session", {"id": "event-id", "type": event_type})


def test_native_live_receipt_parser_retains_worker_session_and_rejects_wrong_root() -> None:
    # 对协议字节和元数据做纯解析，不构造 HTTP 替身，也不冒充真实 Runtime 回执。
    payload = {"status": "started", "session_id": "worker-session", "additional_native_field": "retained"}
    headers = {"X-AgentGov-Run-Id": "run-one", "X-AgentGov-Session-Id": "root-session"}
    content = json.dumps(payload).encode()
    assert native_receipt_run_id(headers, content, "root-session") == "run-one"
    assert json.loads(content) == payload
    with pytest.raises(live.LiveAcceptanceError, match="NATIVE_CHAT_RECEIPT_IDENTITY_INVALID"):
        native_receipt_run_id(headers, content, "another-session")
    with pytest.raises(live.LiveAcceptanceError, match="NATIVE_CHAT_RECEIPT_JSON_INVALID"):
        native_receipt_run_id(headers, b"not-json", "root-session")


@pytest.mark.parametrize("field", ["agent_id", "runtime_agent_id", "session_id", "agent_version_id", "harness_digest", "run_id"])
def test_native_live_lookup_rejects_cross_scope_run_evidence(field) -> None:
    binding = live.BindingEvidence("business-agent", "runtime-agent", "commit", "a" * 64)
    run = {
        "run_id": "run-one",
        "agent_id": "business-agent",
        "runtime_agent_id": "runtime-agent",
        "agent_version_id": "commit",
        "session_id": "root-session",
        "harness_digest": "a" * 64,
    }
    assert require_native_run_identity(run, binding, "root-session", "run-one") == run
    with pytest.raises(live.LiveAcceptanceError, match="NATIVE_LOOKUP_IDENTITY_MISMATCH"):
        require_native_run_identity({**run, field: "other"}, binding, "root-session", "run-one")


def test_browser_acceptance_failure_metadata_uses_real_node_without_body_or_stack() -> None:
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import assert from 'node:assert/strict';
import { acceptanceError, safeAcceptanceFailure, unexpectedDiagnostics } from './scripts/improvement_ui_e2e/page_audit.mjs';
const secretError = new Error('private response content');
secretError.stack = 'private call log';
secretError.body = 'private request body';
secretError.code = 'PRIVATE_SERVER_MESSAGE';
secretError.httpStatus = 503;
assert.deepEqual(safeAcceptanceFailure(secretError), {status:'failed',code:'REAL_UI_ACCEPTANCE_FAILED'});
assert.deepEqual(safeAcceptanceFailure(acceptanceError('PLATFORM_TEST_START_FAILED',503)),
  {status:'failed',code:'PLATFORM_TEST_START_FAILED',http_status:503});
const diagnostics = unexpectedDiagnostics({consoleErrors:[{code:'BROWSER_CONSOLE_ERROR',path:''}],
  pageErrors:[{code:'BROWSER_PAGE_ERROR'}],requestFailures:[],httpErrors:[]});
assert.equal(diagnostics.consoleErrors.length,1);
assert.equal(diagnostics.pageErrors.length,1);
""",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "真实 Node 纯元数据契约检查失败"
