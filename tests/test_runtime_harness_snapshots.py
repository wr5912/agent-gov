from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime_gateway.client import AgentScopeRuntimeClient
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, harness_digest
from app.services.agent_governance import AgentGovernanceService


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _version_store(tmp_path: Path) -> GitAgentVersionStore:
    layout = business_agent_layout(tmp_path / "data", "soc")
    repository = layout.workspace
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "test")
    _git(repository, "config", "user.email", "test@example.local")
    (repository / "agent.yaml").write_text(
        "schema_version: 1\nagent: {id: soc, runtime: agentscope}\nharness: {content_digest: ignored}\n",
        encoding="utf-8",
    )
    (repository / "AGENT.md").write_text("published prompt\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "published")
    return GitAgentVersionStore(
        repository_dir=repository,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )


def _governance_for_versions(tmp_path: Path, versions: GitAgentVersionStore) -> AgentGovernanceService:
    return AgentGovernanceService(
        feedback_store=FeedbackStore(data_dir=tmp_path / "data"),
        agent_version_store=versions,
        runtime_mode="local-debug",
    )


@pytest.fixture
def runtime_client() -> Iterator[AgentScopeRuntimeClient]:
    client = AgentScopeRuntimeClient(
        "http://127.0.0.1:1",
        shared_secret="test-only-runtime-shared-secret",
    )
    try:
        yield client
    finally:
        asyncio.run(client.close())


def test_materializes_exact_commit_and_survives_live_workspace_change(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(versions.repository_dir)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    first = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    (versions.repository_dir / "AGENT.md").write_text("unpublished mutation\n", encoding="utf-8")
    second = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )

    assert first == second
    assert first.workspace_id.startswith("published-")
    assert first.workspace.joinpath("AGENT.md").read_text(encoding="utf-8") == "published prompt\n"
    assert not first.workspace.joinpath("AGENT.md").stat().st_mode & 0o222


def test_existing_snapshot_tamper_fails_closed(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(versions.repository_dir)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    prompt = snapshot.workspace / "AGENT.md"
    prompt.chmod(0o600)
    prompt.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeStateConflict, match="read-only|immutable tuple"):
        snapshots.materialize(
            version_store=versions,
            agent_id="soc",
            agent_version_id=commit,
            expected_digest=digest,
        )


def test_read_only_current_inspection_never_creates_a_missing_snapshot(
    tmp_path: Path,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    versions = _version_store(tmp_path)
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(versions.repository_dir)
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(versions.repository_dir))
    run_store = RuntimeRunStore(factory)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    governance = _governance_for_versions(tmp_path, versions)
    provisioner = RuntimeAgentProvisioner(
        client=runtime_client,
        store=run_store,
        registry=registry,
        version_store_for=governance._store_for,
        read_version_store_for=governance._store_for_read_only,
        snapshot_store=snapshots,
    )

    current = provisioner.inspect_current("soc")
    assert current.runtime_agent_id is None
    assert not snapshots.root.exists()
    run_store.bind_agent_version(
        agent_id="soc",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-orphan",
    )
    with pytest.raises(RuntimeStateConflict, match="snapshot is missing"):
        provisioner.inspect_current("soc")
    assert not snapshots.root.exists()


def test_read_only_inspection_does_not_bootstrap_an_uninitialized_workspace(
    tmp_path: Path,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    layout = business_agent_layout(tmp_path / "data", "soc")
    workspace = layout.workspace
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text(
        "schema_version: 1\nagent: {id: soc, runtime: agentscope}\n",
        encoding="utf-8",
    )
    (workspace / "AGENT.md").write_text("prompt\n", encoding="utf-8")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    governance = _governance_for_versions(tmp_path, versions)
    provisioner = RuntimeAgentProvisioner(
        client=runtime_client,
        store=RuntimeRunStore(factory),
        registry=registry,
        version_store_for=governance._store_for,
        read_version_store_for=governance._store_for_read_only,
        snapshot_store=snapshots,
    )

    with pytest.raises(AgentGitError, match="not initialized"):
        provisioner.inspect_current("soc")
    assert not (workspace / ".git").exists()
    assert not snapshots.root.exists()


def test_production_read_store_provider_does_not_bootstrap_or_create_version_dirs(
    tmp_path: Path,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    data_dir = tmp_path / "data"
    workspace = data_dir / "business-agents" / "soc" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text(
        "schema_version: 1\nagent: {id: soc, runtime: agentscope}\n",
        encoding="utf-8",
    )
    (workspace / "AGENT.md").write_text("prompt\n", encoding="utf-8")
    default_store = GitAgentVersionStore(
        repository_dir=tmp_path / "default",
        worktrees_dir=tmp_path / "default-worktrees",
        releases_dir=tmp_path / "default-releases",
    )
    feedback = FeedbackStore(data_dir=data_dir, workspace_dir=tmp_path / "feedback-workspace")
    governance = AgentGovernanceService(
        feedback_store=feedback,
        agent_version_store=default_store,
        runtime_mode="local-debug",
    )
    registry = AgentRegistryStore(feedback.Session)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    provisioner = RuntimeAgentProvisioner(
        client=runtime_client,
        store=RuntimeRunStore(feedback.Session),
        registry=registry,
        version_store_for=governance._store_for,
        read_version_store_for=governance._store_for_read_only,
        snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "runtime-sources"),
    )

    with pytest.raises(AgentGitError, match="not initialized"):
        provisioner.inspect_current("soc")
    assert not (workspace / ".git").exists()
    assert not (workspace.parent / "version").exists()


def test_existing_current_and_session_validation_preserve_git_and_snapshot_bytes(
    tmp_path: Path,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    versions = _version_store(tmp_path)
    with (versions.repository_dir / "agent.yaml").open("a", encoding="utf-8") as manifest:
        manifest.write("session: {permission_mode: dont_ask, cwd: '.', model_profile: default}\n")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "session")
    commit = versions.inspect_clean_head()[0]
    digest = harness_digest(versions.repository_dir)
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(versions.repository_dir))
    store = RuntimeRunStore(factory)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    store.bind_agent_version(
        agent_id="soc",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-soc",
    )
    store.bind_session(
        session_id="session-soc",
        agent_id="soc",
        agent_version_id=commit,
        runtime_agent_id="runtime-soc",
        digest=digest,
    )
    governance = _governance_for_versions(tmp_path, versions)
    provisioner = RuntimeAgentProvisioner(
        client=runtime_client,
        store=store,
        registry=registry,
        version_store_for=governance._store_for,
        read_version_store_for=governance._store_for_read_only,
        snapshot_store=snapshots,
    )
    observed = [
        versions.repository_dir / ".git" / "config",
        versions.repository_dir / ".git" / "index",
        versions.repository_dir / ".git" / "info" / "exclude",
        snapshot.workspace / "agent.yaml",
        snapshot.workspace / "AGENT.md",
    ]
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in observed}

    assert provisioner.inspect_current("soc").runtime_agent_id == "runtime-soc"
    current = provisioner.current("soc")
    assert current is not None and current.runtime_agent_id == "runtime-soc"
    assert provisioner.require_session("session-soc", "runtime-soc").session_id == "session-soc"
    assert {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in observed} == before
    assert not (versions.repository_dir / ".git" / "index.lock").exists()


def test_git_symlink_is_rejected_and_partial_snapshot_is_removed(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (versions.repository_dir / "skills").mkdir()
    (versions.repository_dir / "skills" / "escape").symlink_to(outside)
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "unsafe")
    commit = versions.current_commit_sha()
    assert commit is not None
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")

    with pytest.raises(RuntimeStateConflict, match="non-regular"):
        snapshots.materialize(
            version_store=versions,
            agent_id="soc",
            agent_version_id=commit,
            expected_digest="a" * 64,
        )
    assert list((tmp_path / "runtime-sources").iterdir()) == []


def test_remove_only_exact_validated_snapshot(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(versions.repository_dir)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )

    assert snapshots.remove(agent_id="soc", agent_version_id=commit, expected_digest=digest)
    assert not snapshot.workspace.parent.exists()
    assert snapshots.remove(agent_id="soc", agent_version_id=commit, expected_digest=digest)


@pytest.mark.parametrize(
    "source_id",
    [
        "candidate-../escape",
        f"candidate-{'a' * 48}x",
        f"published-{'a' * 47}",
        f"candidate-{'a' * 40}/../escape",
    ],
)
def test_remove_exact_source_rejects_traversal_and_similar_prefixes(tmp_path: Path, source_id: str) -> None:
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")

    with pytest.raises(RuntimeStateConflict, match="source kind"):
        snapshots.remove_exact_source(
            source_id=source_id,
            agent_id="soc",
            agent_version_id="a" * 40,
            expected_digest="b" * 64,
        )


def test_remove_exact_source_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "runtime-sources"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source_id = f"candidate-{'a' * 48}"
    (root / source_id).symlink_to(outside, target_is_directory=True)
    snapshots = PublishedHarnessSnapshotStore(root)

    with pytest.raises(RuntimeStateConflict, match="root is unsafe"):
        snapshots.remove_exact_source(
            source_id=source_id,
            agent_id="soc",
            agent_version_id="a" * 40,
            expected_digest="b" * 64,
        )
    assert outside.is_dir()


def test_concurrent_materialization_converges_on_one_immutable_snapshot(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    commit = versions.inspect_clean_head()[0]
    digest = harness_digest(versions.repository_dir)
    root = tmp_path / "runtime-sources"
    barrier = threading.Barrier(2)

    def materialize() -> object:
        barrier.wait(timeout=2)
        return PublishedHarnessSnapshotStore(root).materialize(
            version_store=versions,
            agent_id="soc",
            agent_version_id=commit,
            expected_digest=digest,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(materialize)
        second_future = executor.submit(materialize)
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first == second
    assert len(list(root.glob("published-*"))) == 1
    assert list(root.glob(".published-*")) == []
