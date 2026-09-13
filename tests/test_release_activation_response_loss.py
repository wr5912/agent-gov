from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agentscope_runtime.service import create_runtime_app
from app.runtime_gateway.client import AgentScopeRuntimeClient
from app.runtime_gateway.release_activation import RuntimeActivationRestartRequired

from release_activation_test_utils import AGENT_ID, ReleaseFixture, release_fixture
from runtime_loopback import ForwardedHttpResponse, serve_loopback, serve_single_response_loss_proxy


def _assert_probe_requests(responses: list[ForwardedHttpResponse], *, attempts: int) -> None:
    agents = [item for item in responses if (item.method, item.path) == ("POST", "/agent/")]
    sessions = [item for item in responses if (item.method, item.path) == ("POST", "/sessions/")]
    probes = [item for item in responses if (item.method, item.path) == ("GET", "/workspace/status")]
    cleanups = [item for item in responses if item.method == "DELETE" and item.path.startswith("/sessions/")]
    assert len(agents) == 1 and 200 <= agents[0].status_code < 300
    assert len(sessions) == attempts and all(200 <= item.status_code < 300 for item in sessions)
    assert len(probes) == attempts and all(item.status_code == 409 for item in probes)
    assert len(cleanups) == attempts and all(item.status_code == 204 for item in cleanups)
    assert len({item.path for item in cleanups}) == attempts


async def _recover_and_retry(
    fixture: ReleaseFixture,
    proxy_url: str,
    responses: list[ForwardedHttpResponse],
) -> None:
    client = AgentScopeRuntimeClient(proxy_url, shared_secret=fixture.settings.shared_secret)
    try:
        runtime_agent_id: str | None = None
        for attempt in range(1, 3):
            with pytest.raises(RuntimeActivationRestartRequired, match="maintenance restart"):
                await fixture.prepare(fixture.provisioner(client))
            ledger = fixture.store.get_ephemeral_resource(fixture.activation_key)
            assert ledger is not None and ledger.status == "awaiting_restart"
            assert ledger.runtime_agent_id is not None and ledger.session_id is None
            if runtime_agent_id is None:
                runtime_agent_id = ledger.runtime_agent_id
            assert ledger.runtime_agent_id == runtime_agent_id
            assert await client.list_agent_ids_by_name(f"agentgov-{ledger.source_id}") == [runtime_agent_id]
            assert await client.list_session_ids(runtime_agent_id) == []
            assert fixture.store.agent_versions_for_agent(AGENT_ID) == []
            assert fixture.versions.current_commit_sha() == fixture.base
            assert fixture.registry.status_of(AGENT_ID) == "draft"
            fixture.snapshots.require_existing(
                agent_id=AGENT_ID,
                agent_version_id=fixture.candidate,
                expected_digest=ledger.harness_digest,
            )
            _assert_probe_requests(responses, attempts=attempt)
    finally:
        await client.close()


@pytest.mark.parametrize("drop_path", ["/agent/", "/sessions/"], ids=["agent-response", "session-response"])
def test_release_activation_recovers_committed_response_loss_without_duplicates(tmp_path: Path, drop_path: str) -> None:
    """丢弃真实 Runtime 已成功提交的响应，生产重找恢复原对象并清理探测 Session。"""

    fixture = release_fixture(tmp_path)
    responses: list[ForwardedHttpResponse] = []
    with serve_loopback(create_runtime_app(fixture.settings), lifespan="on") as upstream_url:
        with serve_single_response_loss_proxy(upstream_url, drop_request=("POST", drop_path), responses=responses) as (proxy_url, response_dropped):
            asyncio.run(_recover_and_retry(fixture, proxy_url, responses))
            assert response_dropped.is_set()
    lost = [item for item in responses if item.dropped]
    assert len(lost) == 1
    assert (lost[0].method, lost[0].path) == ("POST", drop_path)
    assert 200 <= lost[0].status_code < 300
