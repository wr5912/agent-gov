from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.subagent_templates import discover_subagent_templates
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.release_activation import RuntimeActivationRestartRequired
from app.runtime_gateway.store import RuntimeStateConflict, harness_digest

from release_activation_test_utils import AGENT_ID, ReleaseFixture, release_fixture
from runtime_loopback import serve_loopback


async def _wait_for_restart(fixture: ReleaseFixture, base_url: str) -> str:
    client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeActivationRestartRequired, match="maintenance restart"):
                await fixture.prepare(fixture.provisioner(client))
            ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
            assert ledger is not None and ledger.status == "awaiting_restart" and ledger.session_id is None
            assert ledger.runtime_agent_id is not None
            assert await client.list_session_ids(ledger.runtime_agent_id) == []
            assert fixture.versions.current_commit_sha() == fixture.base
            assert fixture.store.agent_versions_for_agent(AGENT_ID) == []
            fixture.snapshots.require_existing(agent_id=AGENT_ID, agent_version_id=fixture.candidate, expected_digest=harness_digest(fixture.worktree))
        return ledger.runtime_agent_id
    finally:
        await client.close()


def test_release_activation_restart_retains_resources_for_template_registration(tmp_path: Path) -> None:
    """真实 HTTP 等待、Git/SQLite 保留和启动模板注册；成功续发布在容器验收。"""
    fixture = release_fixture(tmp_path)
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        runtime_agent_id = asyncio.run(_wait_for_restart(fixture, base_url))
    restarted_app = create_runtime_app(fixture.settings)
    expected_templates = discover_subagent_templates(fixture.settings.candidates_root)
    assert expected_templates
    assert set(restarted_app.state.custom_subagent_templates) == set(expected_templates)
    ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
    assert ledger is not None and ledger.runtime_agent_id == runtime_agent_id and ledger.status == "awaiting_restart"
    assert fixture.versions.current_commit_sha() == fixture.base


def test_regular_release_failure_fully_compensates_without_waiting_for_restart(tmp_path: Path) -> None:
    fixture = release_fixture(tmp_path)

    async def fail_release(base_url: str) -> None:
        client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
        try:
            with pytest.raises(RuntimeStateConflict, match="Session configuration is missing"):
                await fixture.prepare(fixture.provisioner(client, configured=False))
            ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
            assert ledger is not None and ledger.status == "cleanup_complete"
            assert ledger.runtime_agent_id is not None
            assert await client.list_agent_ids_by_name(f"agentgov-{ledger.source_id}") == []
            assert fixture.versions.current_commit_sha() == fixture.base
            assert fixture.registry.status_of(AGENT_ID) == "draft"
            assert fixture.store.agent_versions_for_agent(AGENT_ID) == []
            with pytest.raises(RuntimeStateConflict, match="snapshot is missing"):
                fixture.snapshots.require_existing(agent_id=AGENT_ID, agent_version_id=fixture.candidate, expected_digest=harness_digest(fixture.worktree))
        finally:
            await client.close()

    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        asyncio.run(fail_release(base_url))


def test_cleanup_connection_failure_preserves_locators_and_retry_rematerializes_snapshot(tmp_path: Path) -> None:
    fixture = release_fixture(tmp_path)
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as base_url:
        runtime_agent_id = asyncio.run(_wait_for_restart(fixture, base_url))

    async def cleanup_offline() -> None:
        client = AgentScopeRuntimeClient(base_url, shared_secret=fixture.settings.shared_secret)
        try:
            with pytest.raises(RuntimeUpstreamError):
                await fixture.provisioner(client).release_activation.cleanup(fixture.activation_key)
            ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
            assert ledger is not None and ledger.status == "cleanup_pending" and ledger.runtime_agent_id == runtime_agent_id
            fixture.snapshots.require_existing(agent_id=AGENT_ID, agent_version_id=fixture.candidate, expected_digest=harness_digest(fixture.worktree))
        finally:
            await client.close()

    asyncio.run(cleanup_offline())

    async def retry_after_cleanup(restarted_url: str) -> None:
        client = AgentScopeRuntimeClient(restarted_url, shared_secret=fixture.settings.shared_secret)
        try:
            # 故意缺少发布 Session 配置；应到达同一次请求重新创建后的契约校验。
            with pytest.raises(RuntimeStateConflict, match="Session configuration is missing"):
                await fixture.prepare(fixture.provisioner(client, configured=False))
            ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
            assert ledger is not None and ledger.status == "cleanup_complete"
            assert ledger.runtime_agent_id is not None and ledger.runtime_agent_id != runtime_agent_id
            assert await client.list_agent_ids_by_name(f"agentgov-{ledger.source_id}") == []
            assert fixture.versions.current_commit_sha() == fixture.base
        finally:
            await client.close()

    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as restarted_url:
        asyncio.run(retry_after_cleanup(restarted_url))
