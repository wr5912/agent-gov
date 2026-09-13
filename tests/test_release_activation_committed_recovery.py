from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from agentscope_runtime.service import create_runtime_app
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_contract import agent_payload_from_workspace
from app.runtime_gateway.release_activation import _release_probe_workspace_id, published_runtime_name
from app.runtime_gateway.store import RuntimeStateConflict, harness_digest

from release_activation_test_utils import AGENT_ID, ReleaseFixture, release_fixture
from runtime_loopback import serve_loopback


async def _prepare_committed_release(fixture: ReleaseFixture, base_url: str, *, ledger_status: str) -> tuple[str, str]:
    """用真实 Git/SQLite/原生 HTTP 建立已提交边界，不声称完成模型或 Workspace 执行。"""
    digest = harness_digest(fixture.worktree)
    snapshot = fixture.snapshots.materialize(
        version_store=fixture.versions,
        agent_id=AGENT_ID,
        agent_version_id=fixture.candidate,
        expected_digest=digest,
    )
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        runtime_agent_id = await client.create_agent(
            agent_payload_from_workspace(snapshot.workspace, display_name=published_runtime_name(snapshot.workspace_id)),
        )
        fixture.store.start_ephemeral_resource(
            cache_key=fixture.activation_key,
            business_agent_id=AGENT_ID,
            version_owner_id=AGENT_ID,
            agent_version_id=fixture.candidate,
            digest=digest,
            source_id=snapshot.source_id,
            source_kind="release_activation",
            workspace_id=_release_probe_workspace_id(snapshot.workspace_id),
        )
        fixture.store.record_ephemeral_agent(fixture.activation_key, runtime_agent_id)
        fixture.store.bind_agent_version(
            agent_id=AGENT_ID,
            agent_version_id=fixture.candidate,
            digest=digest,
            runtime_agent_id=runtime_agent_id,
            governance_agent_id=AGENT_ID,
            source_kind="published",
            source_id=snapshot.source_id,
        )
        fixture.store.mark_release_activation_bound(fixture.activation_key)
        fixture.versions.publish_commit(fixture.candidate, tag_name="committed-recovery-release", message="发布恢复边界测试")
        if ledger_status == "active":
            fixture.store.mark_release_activation_active(fixture.activation_key)
        fixture.registry.activate_business_agent_after_release(AGENT_ID)
        response = await client.request_json(
            "POST",
            "/sessions/",
            json={
                "agent_id": runtime_agent_id,
                "workspace_id": session_workspace_id(snapshot.workspace_id, uuid.uuid4()),
                "chat_model_config": {
                    "type": fixture.settings.credential_type,
                    "credential_id": fixture.settings.credential_id,
                    "model": "not-invoked-by-recovery-contract",
                    "parameters": {},
                },
                "name": "已发布版本的用户会话",
            },
        )
        session_id = response.body["session_id"]
        fixture.store.bind_session(
            session_id=session_id, agent_id=AGENT_ID, agent_version_id=fixture.candidate, runtime_agent_id=runtime_agent_id, digest=digest
        )
        return runtime_agent_id, session_id
    finally:
        await client.close()


def _assert_committed_resources(fixture: ReleaseFixture, runtime_agent_id: str, session_id: str, *, ledger_status: str) -> None:
    ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
    assert ledger is not None and ledger.status == ledger_status
    assert ledger.runtime_agent_id == runtime_agent_id and ledger.session_id is None
    assert fixture.versions.current_commit_sha() == fixture.candidate
    assert fixture.versions.published_identity_matches(fixture.candidate, "committed-recovery-release")
    binding = fixture.store.get_agent_version(agent_id=AGENT_ID, agent_version_id=fixture.candidate, digest=harness_digest(fixture.worktree))
    assert binding is not None and binding.runtime_agent_id == runtime_agent_id
    assert fixture.store.get_session(session_id, runtime_agent_id=runtime_agent_id).agent_version_id == fixture.candidate
    fixture.snapshots.require_existing(agent_id=AGENT_ID, agent_version_id=fixture.candidate, expected_digest=harness_digest(fixture.worktree))


async def _retry_while_offline(fixture: ReleaseFixture, base_url: str) -> None:
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        with pytest.raises(RuntimeUpstreamError) as caught:
            await fixture.prepare(fixture.provisioner(client))
        assert caught.value.status_code == 503
    finally:
        await client.close()


async def _retry_after_recovery(fixture: ReleaseFixture, base_url: str, runtime_agent_id: str, session_id: str) -> None:
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        for _ in range(2):
            binding = await fixture.prepare(fixture.provisioner(client))
            assert binding.runtime_agent_id == runtime_agent_id and binding.activation_key == fixture.activation_key
            assert binding.agent_version_id == fixture.candidate
            assert await client.list_agent_ids_by_name(published_runtime_name(binding.workspace_id)) == [runtime_agent_id]
            assert await client.list_session_ids(runtime_agent_id) == [session_id]
    finally:
        await client.close()


@pytest.mark.parametrize("ledger_status", ["ready", "active"])
def test_committed_release_retry_survives_real_runtime_outage_without_compensation(tmp_path: Path, ledger_status: str) -> None:
    """Git 已切换但发布元数据未完成时，断网和重试不得把不可变 binding 变成清理任务。"""
    fixture = release_fixture(tmp_path)
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        runtime_agent_id, session_id = asyncio.run(_prepare_committed_release(fixture, base_url, ledger_status=ledger_status))
    _assert_committed_resources(fixture, runtime_agent_id, session_id, ledger_status=ledger_status)

    asyncio.run(_retry_while_offline(fixture, base_url))
    _assert_committed_resources(fixture, runtime_agent_id, session_id, ledger_status=ledger_status)

    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as recovered_url:
        asyncio.run(_retry_after_recovery(fixture, recovered_url, runtime_agent_id, session_id))
    _assert_committed_resources(fixture, runtime_agent_id, session_id, ledger_status=ledger_status)


async def _reject_active_cleanup(fixture: ReleaseFixture, base_url: str, runtime_agent_id: str, session_id: str) -> None:
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        with pytest.raises(RuntimeStateConflict):
            await fixture.provisioner(client).release_activation.cleanup(fixture.activation_key)
        _assert_committed_resources(fixture, runtime_agent_id, session_id, ledger_status="active")
        assert await client.list_session_ids(runtime_agent_id) == [session_id]
    finally:
        await client.close()


def test_active_release_cleanup_rejects_compensation_and_preserves_user_session(tmp_path: Path) -> None:
    """已完成 Runtime 激活的资源不能由发布前补偿入口回收。"""
    fixture = release_fixture(tmp_path)
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        runtime_agent_id, session_id = asyncio.run(_prepare_committed_release(fixture, base_url, ledger_status="active"))
        asyncio.run(_reject_active_cleanup(fixture, base_url, runtime_agent_id, session_id))


async def _reject_ambiguous_binding(fixture: ReleaseFixture, base_url: str, runtime_agent_id: str, session_id: str) -> None:
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
        assert ledger is not None
        runtime_name = published_runtime_name(ledger.workspace_id)
        competing_agent_id = await client.create_agent({"name": runtime_name})
        assert competing_agent_id != runtime_agent_id
        with pytest.raises(RuntimeStateConflict, match="missing or ambiguous"):
            await fixture.prepare(fixture.provisioner(client))
        _assert_committed_resources(fixture, runtime_agent_id, session_id, ledger_status="active")
        assert set(await client.list_agent_ids_by_name(runtime_name)) == {runtime_agent_id, competing_agent_id}
        assert await client.list_session_ids(runtime_agent_id) == [session_id]
    finally:
        await client.close()


def test_committed_release_ambiguous_runtime_identity_fails_closed_without_cleanup(tmp_path: Path) -> None:
    """真实上游出现同名 Agent 时拒绝重绑，不顺手删除任一既存对象。"""
    fixture = release_fixture(tmp_path)
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        runtime_agent_id, session_id = asyncio.run(_prepare_committed_release(fixture, base_url, ledger_status="active"))
        asyncio.run(_reject_ambiguous_binding(fixture, base_url, runtime_agent_id, session_id))
