from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agentscope_runtime.settings import RuntimeSettings
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime_gateway.client import AgentScopeRuntimeClient
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner
from app.runtime_gateway.release_activation import release_activation_key
from app.runtime_gateway.store import RuntimeRunStore, harness_digest

from business_agent_test_utils import create_test_business_agent_workspace

AGENT_ID = "security-operations-expert"
BUSINESS_WORKSPACE = Path(__file__).resolve().parents[1] / "docker/runtime-bootstrap/business-agents" / AGENT_ID / "workspace"


@dataclass
class ReleaseFixture:
    settings: RuntimeSettings
    store: RuntimeRunStore
    registry: AgentRegistryStore
    versions: GitAgentVersionStore
    snapshots: PublishedHarnessSnapshotStore
    worktree: Path
    base: str
    candidate: str

    def provisioner(self, client: AgentScopeRuntimeClient, *, configured: bool = True) -> RuntimeAgentProvisioner:
        return RuntimeAgentProvisioner(
            client=client,
            store=self.store,
            registry=self.registry,
            version_store_for=lambda _agent_id: self.versions,
            snapshot_store=self.snapshots,
            release_session_config={
                "type": self.settings.credential_type,
                "credential_id": self.settings.credential_id,
                "model": "not-invoked-by-workspace-probe",
                "parameters": {},
            }
            if configured
            else {},
        )

    def prepare(self, provisioner: RuntimeAgentProvisioner):
        return provisioner.ensure_version(agent_id=AGENT_ID, agent_version_id=self.candidate, candidate_worktree=self.worktree)

    @property
    def activation_key(self) -> str:
        return release_activation_key(AGENT_ID, self.candidate, harness_digest(self.worktree))


def release_fixture(tmp_path: Path) -> ReleaseFixture:
    layout = business_agent_layout(tmp_path / "data", AGENT_ID)
    create_test_business_agent_workspace(layout.workspace, agent_id=AGENT_ID, name="Release lifecycle", requires_web_hitl=False)
    shutil.copytree(BUSINESS_WORKSPACE / "subagents", layout.workspace / "subagents")
    versions = GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )
    versions.ensure_bootstrap()
    base = versions.current_commit_sha()
    assert base is not None
    worktree = versions.create_worktree("release-candidate", base_ref=base).worktree_path
    prompt = worktree / "AGENT.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n发布生命周期验证。\n", encoding="utf-8")
    candidate = versions.commit_worktree(worktree, message="candidate")
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    registry = AgentRegistryStore(factory)
    reservation = registry.reserve_business_agent(name="SOC", agent_id=AGENT_ID, workspace_dir=str(layout.workspace), lifecycle_status="draft")
    registry.finalize_business_agent(reservation)
    settings = RuntimeSettings(
        shared_secret="release-contract-local-signing-key",
        provider_api_key="unused-by-workspace-status",
        data_dir=tmp_path / "runtime-data",
        business_agents_root=tmp_path / "runtime-business",
        candidates_root=tmp_path / "runtime-sources",
        workspaces_root=tmp_path / "runtime-workspaces",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-data' / 'agentscope.db'}",
    )
    settings.business_agents_root.mkdir()
    settings.candidates_root.mkdir()
    return ReleaseFixture(
        settings, RuntimeRunStore(factory), registry, versions, PublishedHarnessSnapshotStore(settings.candidates_root), worktree, base, candidate
    )
