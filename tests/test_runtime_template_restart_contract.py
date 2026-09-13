from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from agentgov_agentscope_contract import RUNTIME_TEMPLATE_RESTART_REQUIRED, is_runtime_template_restart_response
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.settings import RuntimeSettings
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime_gateway._execution_support import _requires_runtime_restart
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshot, PublishedHarnessSnapshotStore
from app.runtime_gateway.release_activation import _release_probe_workspace_id
from app.runtime_gateway.store import harness_digest

from runtime_loopback import serve_loopback

BUSINESS_WORKSPACE = Path(__file__).resolve().parents[1] / "docker/runtime-bootstrap/business-agents/security-operations-expert/workspace"


def _publish_snapshot(tmp_path: Path, settings: RuntimeSettings) -> PublishedHarnessSnapshot:
    workspace = tmp_path / "repository"
    shutil.copytree(BUSINESS_WORKSPACE, workspace)
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    versions.ensure_bootstrap()
    commit = versions.current_commit_sha()
    assert commit is not None
    return PublishedHarnessSnapshotStore(settings.candidates_root).materialize(
        version_store=versions,
        agent_id="security-operations-expert",
        agent_version_id=commit,
        expected_digest=harness_digest(workspace),
    )


async def _probe_after_runtime_started(base_url: str, settings: RuntimeSettings, snapshot: PublishedHarnessSnapshot) -> RuntimeUpstreamError:
    client = AgentScopeRuntimeClient(base_url, shared_secret=settings.shared_secret)
    try:
        agent_id = await client.create_agent({"name": "Late published Harness contract"})
        session = await client.request_json(
            "POST",
            "/sessions/",
            json={
                "agent_id": agent_id,
                "workspace_id": _release_probe_workspace_id(snapshot.workspace_id),
                "chat_model_config": {
                    "type": settings.credential_type,
                    "credential_id": settings.credential_id,
                    "model": "not-invoked-by-workspace-probe",
                    "parameters": {},
                },
            },
        )
        session_id = session.body["session_id"]
        with pytest.raises(RuntimeUpstreamError) as caught:
            await client.request_json("GET", "/workspace/status", params={"agent_id": agent_id, "session_id": session_id})
        await client.delete_session(session_id, agent_id)
        await client.delete_agent(agent_id)
        return caught.value
    finally:
        await client.close()


def test_real_workspace_http_returns_restart_code_for_late_published_templates(tmp_path: Path) -> None:
    """真实 Git 快照、原生 Agent/Session/Workspace HTTP 路由；不请求模型或模拟 API。"""
    settings = RuntimeSettings(
        shared_secret="workspace-contract-local-signing-key",
        provider_api_key="unused-by-workspace-status",
        data_dir=tmp_path / "data",
        business_agents_root=tmp_path / "business",
        candidates_root=tmp_path / "candidates",
        workspaces_root=tmp_path / "runtime-workspaces",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'agentscope.db'}",
    )
    settings.business_agents_root.mkdir()
    settings.candidates_root.mkdir()
    app = create_runtime_app(settings)
    with serve_loopback(app, lifespan="on") as base_url:
        snapshot = _publish_snapshot(tmp_path, settings)
        error = asyncio.run(_probe_after_runtime_started(base_url, settings, snapshot))

    assert error.status_code == 409
    assert json.loads(error.body)["error_code"] == RUNTIME_TEMPLATE_RESTART_REQUIRED
    assert is_runtime_template_restart_response(error.status_code, error.body)
    assert _requires_runtime_restart(error, snapshot.workspace.parent)
    assert snapshot.harness_digest.encode() not in error.body
    assert str(tmp_path).encode() not in error.body


@pytest.mark.parametrize(
    "status_code,body",
    [
        (503, b'{"error_code":"RUNTIME_TEMPLATE_RESTART_REQUIRED"}'),
        (409, b'{"detail":"published after Runtime startup; restart Runtime"}'),
        (409, b'{"error_code":"OTHER_CONFLICT"}'),
        (409, b'{"error_code":["RUNTIME_TEMPLATE_RESTART_REQUIRED"]}'),
        (409, b"null"),
        (409, b"not-json"),
        (409, b"\xff"),
    ],
)
def test_restart_detection_requires_exact_structured_code(tmp_path: Path, status_code: int, body: bytes) -> None:
    (tmp_path / "workspace" / "subagents").mkdir(parents=True)
    assert not is_runtime_template_restart_response(status_code, body)
    assert not _requires_runtime_restart(RuntimeUpstreamError(status_code, body), tmp_path)
