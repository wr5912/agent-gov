from __future__ import annotations

import asyncio
import errno
import os
import subprocess
from pathlib import Path

import pytest
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime_gateway.client import RuntimeUpstreamError
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
    repository = tmp_path / "workspace"
    repository.mkdir()
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
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )


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


def test_read_only_current_inspection_never_creates_a_missing_snapshot(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    commit = versions.current_commit_sha()
    assert commit is not None
    digest = harness_digest(versions.repository_dir)
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(
        name="SOC",
        agent_id="soc",
        workspace_dir=str(versions.repository_dir),
    )
    run_store = RuntimeRunStore(factory)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    provisioner = RuntimeAgentProvisioner(
        client=object(),  # type: ignore[arg-type]
        store=run_store,
        registry=registry,
        version_store_for=lambda _agent_id: versions,
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


def test_read_only_inspection_does_not_bootstrap_an_uninitialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "agent.yaml").write_text(
        "schema_version: 1\nagent: {id: soc, runtime: agentscope}\n",
        encoding="utf-8",
    )
    (workspace / "AGENT.md").write_text("prompt\n", encoding="utf-8")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    provisioner = RuntimeAgentProvisioner(
        client=object(),  # type: ignore[arg-type]
        store=RuntimeRunStore(factory),
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=snapshots,
    )

    with pytest.raises(AgentGitError, match="not initialized"):
        provisioner.inspect_current("soc")

    assert not (workspace / ".git").exists()
    assert not snapshots.root.exists()


def test_production_read_store_provider_does_not_bootstrap_or_create_version_dirs(tmp_path: Path) -> None:
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
    governance.agent_exists = lambda agent_id: agent_id == "soc"
    registry = AgentRegistryStore(feedback.Session)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    provisioner = RuntimeAgentProvisioner(
        client=object(),  # type: ignore[arg-type]
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


def test_existing_current_and_session_validation_never_bootstrap_or_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    registry.create_business_agent(
        name="SOC",
        agent_id="soc",
        workspace_dir=str(versions.repository_dir),
    )
    store = RuntimeRunStore(factory)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    snapshots.materialize(
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
    provisioner = RuntimeAgentProvisioner(
        client=object(),  # type: ignore[arg-type]
        store=store,
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=snapshots,
    )
    monkeypatch.setattr(versions, "ensure_bootstrap", lambda: pytest.fail("read path bootstrapped Git"))
    monkeypatch.setattr(snapshots, "materialize", lambda **_kwargs: pytest.fail("read path materialized snapshot"))
    git_files = [
        versions.repository_dir / ".git" / "config",
        versions.repository_dir / ".git" / "index",
        versions.repository_dir / ".git" / "info" / "exclude",
    ]
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in git_files}

    assert provisioner.inspect_current("soc").runtime_agent_id == "runtime-soc"
    current = provisioner.current("soc")
    assert current is not None and current.runtime_agent_id == "runtime-soc"
    assert provisioner.require_session("session-soc", "runtime-soc").session_id == "session-soc"
    assert {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in git_files} == before
    assert not (versions.repository_dir / ".git" / "index.lock").exists()


def test_git_symlink_is_rejected_and_partial_snapshot_is_removed(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    (versions.repository_dir / "skills").mkdir()
    (versions.repository_dir / "skills" / "escape").symlink_to("/tmp")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "unsafe")
    commit = versions.current_commit_sha()
    assert commit is not None
    # Git tree 内的 symlink 在 live digest 阶段也应拒绝；用占位摘要进入 archive 安全门。
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
def test_remove_exact_source_rejects_traversal_and_similar_prefixes(
    tmp_path: Path,
    source_id: str,
) -> None:
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


def test_materialize_accepts_only_exact_concurrent_rename_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = _version_store(tmp_path)
    commit = versions.inspect_clean_head()[0]
    digest = harness_digest(versions.repository_dir)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    original_rename = os.rename

    def concurrent_winner(source: Path, target: Path) -> None:
        original_rename(source, target)
        raise OSError(errno.EEXIST, "simulated concurrent winner")

    monkeypatch.setattr("app.runtime_gateway.harness_snapshots.os.rename", concurrent_winner)
    snapshot = snapshots.materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )

    assert snapshot.workspace.is_dir()


def test_materialize_does_not_swallow_non_concurrency_rename_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = _version_store(tmp_path)
    commit = versions.inspect_clean_head()[0]
    digest = harness_digest(versions.repository_dir)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")

    def denied(_source: Path, _target: Path) -> None:
        raise OSError(errno.EACCES, "simulated permission failure")

    monkeypatch.setattr("app.runtime_gateway.harness_snapshots.os.rename", denied)
    with pytest.raises(OSError) as caught:
        snapshots.materialize(
            version_store=versions,
            agent_id="soc",
            agent_version_id=commit,
            expected_digest=digest,
        )

    assert caught.value.errno == errno.EACCES
    assert list(snapshots.root.iterdir()) == []


def test_provisioner_pins_each_published_version_to_its_commit_snapshot(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    with (versions.repository_dir / "agent.yaml").open("a", encoding="utf-8") as manifest:
        manifest.write("session: {permission_mode: dont_ask, cwd: '.', model_profile: default}\n")
        manifest.write("workspace_policy: {allowed_network_domains: [], fail_closed: true}\n")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "runtime contract")

    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(
        name="SOC",
        agent_id="soc",
        workspace_dir=str(versions.repository_dir),
    )

    class _Client:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []
            self.names: dict[str, str] = {}

        async def list_agent_ids_by_name(self, name: str) -> list[str]:
            return [agent_id for agent_id, candidate in self.names.items() if candidate == name]

        async def create_agent(self, payload: dict[str, object]) -> str:
            self.payloads.append(payload)
            runtime_agent_id = f"runtime-{len(self.payloads)}"
            self.names[runtime_agent_id] = str(payload["name"])
            return runtime_agent_id

        async def delete_agent(self, runtime_agent_id: str) -> None:
            self.names.pop(runtime_agent_id, None)

    client = _Client()
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    run_store = RuntimeRunStore(factory)
    provisioner = RuntimeAgentProvisioner(
        client=client,  # type: ignore[arg-type]
        store=run_store,
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=snapshots,
    )

    first = asyncio.run(provisioner.ensure("soc"))
    run_store.bind_session(
        session_id="old-session",
        agent_id="soc",
        agent_version_id=first.agent_version_id,
        runtime_agent_id=first.runtime_agent_id,
        digest=first.harness_digest,
    )
    first_source = snapshots.root / first.workspace_id.split("--v-", 1)[0] / "workspace"
    assert first_source.joinpath("AGENT.md").read_text(encoding="utf-8") == "published prompt\n"

    (versions.repository_dir / "AGENT.md").write_text("second published prompt\n", encoding="utf-8")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "second version")
    second = asyncio.run(provisioner.ensure("soc"))

    assert second.agent_version_id != first.agent_version_id
    assert second.workspace_id != first.workspace_id
    assert client.payloads[0]["system_prompt"] == "published prompt\n"
    assert client.payloads[1]["system_prompt"] == "second published prompt\n"
    assert first_source.joinpath("AGENT.md").read_text(encoding="utf-8") == "published prompt\n"
    old_binding = run_store.get_session("old-session")
    assert old_binding.agent_version_id == first.agent_version_id
    assert old_binding.runtime_agent_id == first.runtime_agent_id


def test_published_subagent_restart_requirement_is_safe_and_keeps_snapshot(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    with (versions.repository_dir / "agent.yaml").open("a", encoding="utf-8") as manifest:
        manifest.write("session: {permission_mode: dont_ask, cwd: '.', model_profile: default}\n")
    subagent = versions.repository_dir / "subagents" / "helper"
    subagent.mkdir(parents=True)
    (subagent / "agent.yaml").write_text("agent: {id: helper}\n", encoding="utf-8")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "subagent")
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(
        name="SOC",
        agent_id="soc",
        workspace_dir=str(versions.repository_dir),
    )

    class _Client:
        async def list_agent_ids_by_name(self, _name: str) -> list[str]:
            return []

        async def create_agent(self, _payload: dict[str, object]) -> str:
            raise RuntimeUpstreamError(
                500,
                b'{"detail":"template was published after Runtime startup; restart Runtime secret-body"}',
            )

    snapshots = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources")
    provisioner = RuntimeAgentProvisioner(
        client=_Client(),  # type: ignore[arg-type]
        store=RuntimeRunStore(factory),
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=snapshots,
    )

    with pytest.raises(RuntimeStateConflict, match="restart AgentScope Runtime") as caught:
        asyncio.run(provisioner.ensure("soc"))

    assert "secret-body" not in str(caught.value)
    assert len(list(snapshots.root.glob("published-*"))) == 1


def test_provisioner_recovers_agent_created_before_response_loss(tmp_path: Path) -> None:
    versions = _version_store(tmp_path)
    with (versions.repository_dir / "agent.yaml").open("a", encoding="utf-8") as manifest:
        manifest.write("session: {permission_mode: dont_ask, cwd: '.', model_profile: default}\n")
    _git(versions.repository_dir, "add", "-A")
    _git(versions.repository_dir, "commit", "-m", "runtime contract")
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="Mutable Display Name", agent_id="soc", workspace_dir=str(versions.repository_dir))

    class _ResponseLossClient:
        def __init__(self) -> None:
            self.names: dict[str, str] = {}
            self.create_calls = 0

        async def list_agent_ids_by_name(self, name: str) -> list[str]:
            return [agent_id for agent_id, candidate in self.names.items() if candidate == name]

        async def create_agent(self, request_data: dict[str, object]) -> str:
            self.create_calls += 1
            runtime_agent_id = "runtime-created-before-loss"
            self.names[runtime_agent_id] = str(request_data["name"])
            raise RuntimeUpstreamError(503, b'{"detail":"response lost"}')

        async def delete_agent(self, runtime_agent_id: str) -> None:
            self.names.pop(runtime_agent_id, None)

    client = _ResponseLossClient()
    provisioner = RuntimeAgentProvisioner(
        client=client,  # type: ignore[arg-type]
        store=RuntimeRunStore(factory),
        registry=registry,
        version_store_for=lambda _agent_id: versions,
        snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "runtime-sources"),
    )

    with pytest.raises(RuntimeUpstreamError):
        asyncio.run(provisioner.ensure("soc"))
    recovered = asyncio.run(provisioner.ensure("soc"))

    assert recovered.runtime_agent_id == "runtime-created-before-loss"
    assert client.create_calls == 1
    assert set(client.names.values()) == {f"agentgov-{recovered.workspace_id.split('--v-', 1)[0]}"}
