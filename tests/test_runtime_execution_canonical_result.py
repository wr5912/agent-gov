from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest
from app.runtime.settings import AppSettings
from app.runtime_gateway._execution_support import _governed_evidence_root
from app.runtime_gateway.client import RuntimeJsonResponse
from app.runtime_gateway.contracts import (
    GOVERNED_EVIDENCE_ROOT_METADATA_KEY,
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
    RuntimeTeamInboxDelivery,
)
from app.runtime_gateway.execution import (
    AgentScopeExecutionService,
    _ExecutionResource,
)
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.models import AgentRunModel
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict


class _PersistentTeamStream:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.store = store
        self.receipt_ids = itertools.count()
        self.cancelled = False

    async def aiter_raw(self):
        run = self.store.active_run_for_session("leader-session")
        assert run is not None
        yield self._event(run, "REPLY_START", "leader-initial", {})
        yield self._raw({"type": "TEXT_BLOCK_DELTA", "delta": "wrong-stream-first"})
        yield self._event(run, "REPLY_END", "leader-initial", {"finished_reason": "completed"})

        self.store.bind_team_child(
            RuntimeChildSessionRegistration(
                run_id=run.run_id,
                parent_session_id="leader-session",
                child_session_id="worker-session",
                child_runtime_agent_id="worker-agent",
                team_id="team-1",
            ),
        )
        self.store.record_team_inbox_delivery(
            RuntimeTeamInboxDelivery(
                event_id="delivery-to-worker",
                run_id=run.run_id,
                source_session_id="leader-session",
                target_session_id="worker-session",
            ),
        )
        self._receipt(run, "MESSAGE_PERSISTED", "leader-session", "leader-initial", self._message_payload())
        self._receipt(run, "SESSION_PERSISTED", "leader-session", None, self._batch_payload(["leader-initial"], 0))
        self._receipt(run, "REPLY_START", "worker-session", "worker-reply", {})
        self._receipt(run, "REPLY_END", "worker-session", "worker-reply", {"finished_reason": "completed"})
        self._receipt(run, "MESSAGE_PERSISTED", "worker-session", "worker-reply", self._message_payload())
        self.store.record_team_inbox_delivery(
            RuntimeTeamInboxDelivery(
                event_id="delivery-to-leader",
                run_id=run.run_id,
                source_session_id="worker-session",
                target_session_id="leader-session",
            ),
        )
        self._receipt(run, "SESSION_PERSISTED", "worker-session", None, self._batch_payload(["worker-reply"], 1))
        yield self._raw({"type": "FUTURE_TEAM_EVENT", "value": {"opaque": True}})

        yield self._event(run, "REPLY_START", "leader-followup", {})
        yield self._raw({"type": "TEXT_BLOCK_DELTA", "delta": "also-not-authoritative"})
        yield self._event(run, "REPLY_END", "leader-followup", {"finished_reason": "completed"})
        self._receipt(run, "MESSAGE_PERSISTED", "leader-session", "leader-followup", self._message_payload())
        self._receipt(run, "SESSION_PERSISTED", "leader-session", None, self._batch_payload(["leader-followup"], 2))
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True

    def _event(self, run, event_type: str, reply_id: str, payload: dict[str, object]) -> bytes:
        self._receipt(run, event_type, "leader-session", reply_id, payload)
        return self._raw({"type": event_type, "reply_id": reply_id, **payload})

    def _receipt(
        self,
        run,
        event_type: str,
        session_id: str,
        reply_id: str | None,
        payload: dict[str, object],
    ) -> None:
        suffix = next(self.receipt_ids)
        self.store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"execution-receipt-{suffix}",
                event_id=f"execution-event-{suffix}",
                session_id=session_id,
                run_id=run.run_id,
                reply_id=reply_id,
                type=event_type,
                payload=payload,
                trace_id=run.trace_id,
            ),
        )

    @staticmethod
    def _message_payload() -> dict[str, object]:
        return {
            "message_persisted": True,
            "finished_reason": "completed",
            "trace_complete": True,
        }

    @staticmethod
    def _batch_payload(reply_ids: list[str], generation: int) -> dict[str, object]:
        return {
            "reply_ids": reply_ids,
            "message_count": len(reply_ids),
            "team_generation": generation,
        }

    @staticmethod
    def _raw(event: dict[str, object]) -> bytes:
        return f"data: {json.dumps(event)}\n\n".encode()


class _ExecutionClient:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.stream_response = _PersistentTeamStream(store)
        self.posted_input: object = None

    @asynccontextmanager
    async def stream(self, path: str, **kwargs):
        assert path == "/sessions/leader-session/stream"
        assert kwargs == {"params": {"agent_id": "leader-agent"}}
        yield self.stream_response

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        if (method, path) == ("POST", "/chat/"):
            self.posted_input = kwargs["json"]["input"]
            return RuntimeJsonResponse(202, {}, {"status": "started"})
        if (method, path) == ("GET", "/sessions/leader-session/messages"):
            return RuntimeJsonResponse(
                200,
                {},
                {
                    "messages": [
                        self._assistant("older-session-reply", "older session answer", 1),
                        self._assistant("leader-initial", "wrong first batch", 2),
                        self._assistant("leader-followup", "canonical final answer", 3),
                    ],
                },
            )
        raise AssertionError((method, path, kwargs))

    @staticmethod
    def _assistant(reply_id: str, text: str, tokens: int) -> dict[str, object]:
        return {
            "id": reply_id,
            "name": "assistant",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": tokens, "output_tokens": tokens + 1},
            "finished_reason": "completed",
        }


def _service(tmp_path: Path) -> tuple[AgentScopeExecutionService, RuntimeRunStore, _ExecutionClient, _ExecutionResource]:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="governor",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="leader-agent",
    )
    store.bind_session(
        session_id="leader-session",
        agent_id="governor",
        agent_version_id="version-a",
        runtime_agent_id="leader-agent",
        digest="a" * 64,
    )
    client = _ExecutionClient(store)
    settings = AppSettings(
        _env_file=None,
        RUNTIME_VOLUME_MODE="local-debug",
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        RUNTIME_CANDIDATES_DIR=tmp_path / "candidates",
        AGENTGOV_RUNTIME_SHARED_SECRET="test-runtime-shared-secret",
        GOVERNANCE_AGENT_TIMEOUT_SECONDS=3,
    )
    service = AgentScopeExecutionService(
        settings=settings,
        client=client,  # type: ignore[arg-type]
        store=store,
        version_store_for=lambda _agent_id: pytest.fail("not used"),
        snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "candidates"),
    )
    resource = _ExecutionResource(
        cache_key="governor:test",
        business_agent_id="governor",
        version_id="version-a",
        harness_digest="a" * 64,
        source_id="governor-source",
        source_root=tmp_path / "governor-source",
        version_owner_id="governor",
        source_kind="staged",
        runtime_agent_id="leader-agent",
        session_id="leader-session",
        workspace_id="governor-workspace",
    )
    return service, store, client, resource


def test_execution_waits_for_full_team_batch_and_uses_last_canonical_reply(tmp_path: Path) -> None:
    service, store, client, resource = _service(tmp_path)
    evidence_root = "/business-agents/soc-ops/workspace"
    response = asyncio.run(
        service._run_resource(
            resource,
            ChatRequest(
                message="analyze",
                metadata={GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/attacker/workspace"},
            ),
            governed_evidence_root=evidence_root,
        ),
    )

    assert response.answer == "canonical final answer"
    assert response.usage == {"input_tokens": 3, "output_tokens": 4}
    assert response.agent_activity["reply_ids"] == ["leader-initial", "leader-followup"]
    assert response.agent_activity["event_types"].count("REPLY_END") == 2
    assert "FUTURE_TEAM_EVENT" in response.agent_activity["event_types"]
    assert client.stream_response.cancelled is True
    expected_input = {
        "name": "user",
        "role": "user",
        "content": [{"type": "text", "text": "analyze"}],
        "metadata": {GOVERNED_EVIDENCE_ROOT_METADATA_KEY: evidence_root},
    }
    assert client.posted_input == expected_input
    terminal = store.get_run(response.run_id)
    expected_fingerprint = hashlib.sha256(
        json.dumps(expected_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    assert terminal.status.value == "succeeded"
    assert terminal.metadata[GOVERNED_EVIDENCE_ROOT_METADATA_KEY] == evidence_root
    with store.Session() as db:
        row = db.get(AgentRunModel, response.run_id)
        assert row is not None and row.input_fingerprint == expected_fingerprint


@pytest.mark.parametrize(
    "job_input",
    [
        {"target_agent_context": {"workspace_dir": "/runtime-workspaces/governor"}},
        {"target_agent_context": {"workspace_dir": "/business-agents/../workspace"}},
        {
            "target_agent_context": {
                "agent_id": "other",
                "workspace_dir": "/business-agents/soc-ops/workspace",
            },
        },
    ],
)
def test_governed_evidence_root_rejects_untrusted_or_mismatched_paths(job_input) -> None:
    with pytest.raises(RuntimeStateConflict):
        _governed_evidence_root(job_input)
