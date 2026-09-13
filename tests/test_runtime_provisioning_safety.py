from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner, _published_provision_lock
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, harness_digest
from app.services.agent_governance import AgentGovernanceService


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, GitAgentVersionStore, str, str]:
    layout = business_agent_layout(tmp_path / "data", "soc")
    workspace = layout.workspace
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text(
        "agent: {id: soc, runtime: agentscope}\nsession: {permission_mode: dont_ask, cwd: '.', model_profile: default}\n",
        encoding="utf-8",
    )
    (workspace / "AGENT.md").write_text("production instructions\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "published")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
    )
    commit = versions.inspect_clean_head()[0]
    return workspace, versions, commit, harness_digest(workspace)


@pytest.fixture
def unavailable_runtime_endpoint() -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        listener.close()


def _provisioner(
    tmp_path: Path,
    base_url: str,
) -> tuple[RuntimeAgentProvisioner, RuntimeRunStore, AgentScopeRuntimeClient, str, str]:
    workspace, versions, commit, digest = _workspace(tmp_path)
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    store = RuntimeRunStore(factory)
    client = AgentScopeRuntimeClient(
        base_url,
        shared_secret="test-only-runtime-shared-secret",
        timeout_seconds=1,
    )
    governance = AgentGovernanceService(
        feedback_store=FeedbackStore(data_dir=tmp_path / "data"),
        agent_version_store=versions,
        runtime_mode="local-debug",
    )
    provisioner = RuntimeAgentProvisioner(
        client=client,
        store=store,
        registry=registry,
        version_store_for=governance._store_for,
        read_version_store_for=governance._store_for_read_only,
        snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "runtime-sources"),
    )
    return provisioner, store, client, commit, digest


def test_real_network_refusal_keeps_materialized_snapshot_unbound(
    tmp_path: Path,
    unavailable_runtime_endpoint: str,
) -> None:
    provisioner, store, client, commit, digest = _provisioner(tmp_path, unavailable_runtime_endpoint)
    try:
        with pytest.raises(RuntimeUpstreamError) as caught:
            asyncio.run(provisioner.ensure("soc"))
    finally:
        asyncio.run(client.close())

    assert caught.value.status_code == 503
    assert store.get_agent_version(agent_id="soc", agent_version_id=commit, digest=digest) is None
    assert len(list(provisioner.snapshot_store.root.glob("published-*"))) == 1


def test_existing_binding_is_not_rebound_when_real_runtime_is_unavailable(
    tmp_path: Path,
    unavailable_runtime_endpoint: str,
) -> None:
    provisioner, store, client, commit, digest = _provisioner(tmp_path, unavailable_runtime_endpoint)
    snapshot = provisioner.snapshot_store.materialize(
        version_store=provisioner.version_store_for("soc"),
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    store.bind_agent_version(
        agent_id="soc",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-existing",
        source_id=snapshot.source_id,
    )
    try:
        with pytest.raises(RuntimeUpstreamError) as caught:
            asyncio.run(provisioner.ensure("soc"))
    finally:
        asyncio.run(client.close())

    assert caught.value.status_code == 503
    binding = store.get_agent_version(agent_id="soc", agent_version_id=commit, digest=digest)
    assert binding is not None and binding.runtime_agent_id == "runtime-existing"
    assert len(store.agent_versions_for_agent("soc")) == 1


def test_evaluating_agent_keeps_its_published_runtime_binding_runnable(
    tmp_path: Path,
    unavailable_runtime_endpoint: str,
) -> None:
    provisioner, store, client, commit, digest = _provisioner(tmp_path, unavailable_runtime_endpoint)
    snapshot = provisioner.snapshot_store.materialize(
        version_store=provisioner.version_store_for("soc"),
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    store.bind_agent_version(
        agent_id="soc",
        agent_version_id=commit,
        digest=digest,
        runtime_agent_id="runtime-existing",
        source_id=snapshot.source_id,
    )
    provisioner.registry.transition_business_agent("soc", status="evaluating")
    try:
        current = provisioner.require_current_runtime("runtime-existing")
    finally:
        asyncio.run(client.close())

    assert current.agent_id == "soc"
    assert current.agent_version_id == commit


def test_cancelled_waiter_releases_real_cross_process_directory_lock(tmp_path: Path) -> None:
    workspace, versions, commit, digest = _workspace(tmp_path)
    snapshot = PublishedHarnessSnapshotStore(tmp_path / "runtime-sources").materialize(
        version_store=versions,
        agent_id="soc",
        agent_version_id=commit,
        expected_digest=digest,
    )
    locker = (
        "import fcntl, os, sys; "
        "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY); "
        "fcntl.flock(fd, fcntl.LOCK_EX); "
        "print('locked', flush=True); "
        "sys.stdin.read(1); os.close(fd)"
    )

    async def acquire() -> None:
        async with _published_provision_lock(snapshot.workspace):
            return None

    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            locker,
            str(snapshot.workspace.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stdin is not None
        try:
            assert await asyncio.wait_for(process.stdout.readline(), timeout=2) == b"locked\n"
            blocked = asyncio.create_task(acquire())
            await asyncio.sleep(0.15)
            assert not blocked.done()
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            process.stdin.write(b"x")
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), timeout=2)
            await asyncio.wait_for(acquire(), timeout=2)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process.stdin.close()

    asyncio.run(exercise())


class _FaultProvisionClient(AgentScopeRuntimeClient):
    """为不可由网络时序稳定触发的 provision 部分失败提供边界注入。"""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.sessions: dict[str, list[str]] = {}
        self.create_calls = 0
        self.deleted: list[str] = []
        self.delete_failures = 0
        self.after_create: asyncio.Event | None = None
        self.finish_create: asyncio.Event | None = None

    async def list_agent_ids_by_name(self, name: str) -> list[str]:
        await asyncio.sleep(0)
        return [identifier for identifier, candidate in self.names.items() if candidate == name]

    async def create_agent(self, payload: dict[str, object]) -> str:
        self.create_calls += 1
        identifier = f"runtime-{self.create_calls}"
        self.names[identifier] = str(payload["name"])
        if self.after_create is not None:
            self.after_create.set()
            assert self.finish_create is not None
            await self.finish_create.wait()
        return identifier

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        return self.sessions.get(runtime_agent_id, [])

    async def delete_agent(self, runtime_agent_id: str) -> None:
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeUpstreamError(503, b'{"detail":"temporary outage"}')
        self.names.pop(runtime_agent_id, None)
        self.deleted.append(runtime_agent_id)


@pytest.fixture
def fault_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "sources" / "published-test" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("agent: {id: soc, runtime: agentscope}\n", encoding="utf-8")
    (workspace / "AGENT.md").write_text("test instructions\n", encoding="utf-8")
    factory = make_session_factory(tmp_path / "fault-runtime.db")
    registry = AgentRegistryStore(factory)
    record = registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    client = _FaultProvisionClient()
    store = RuntimeRunStore(factory)

    def current_source(_self, agent_id: str):
        assert agent_id == "soc"
        return (
            record,
            workspace,
            "version-one",
            "digest-one",
            "published-test--v-digest-one",
            ("dont_ask", ".", "default"),
        )

    monkeypatch.setattr(RuntimeAgentProvisioner, "_current_source", current_source)

    def create() -> RuntimeAgentProvisioner:
        return RuntimeAgentProvisioner(
            client=client,
            store=RuntimeRunStore(factory),
            registry=registry,
            version_store_for=lambda _agent_id: GitAgentVersionStore(
                repository_dir=workspace,
                worktrees_dir=tmp_path / "worktrees",
                releases_dir=tmp_path / "releases",
            ),
            snapshot_store=PublishedHarnessSnapshotStore(tmp_path / "sources"),
        )

    return client, store, create


def test_fault_injection_concurrent_provisioners_create_one_runtime_agent(fault_provisioning) -> None:
    client, store, create = fault_provisioning

    async def exercise():
        return await asyncio.gather(*(create().ensure("soc") for _ in range(4)))

    bindings = asyncio.run(exercise())
    assert {binding.runtime_agent_id for binding in bindings} == {"runtime-1"}
    assert client.create_calls == 1
    assert client.deleted == []
    assert len(store.agent_versions_for_agent("soc")) == 1


def test_fault_injection_cancelled_create_is_recovered_by_stable_name(fault_provisioning) -> None:
    client, _store, create = fault_provisioning

    async def exercise():
        client.after_create = asyncio.Event()
        client.finish_create = asyncio.Event()
        task = asyncio.create_task(create().ensure("soc"))
        await asyncio.wait_for(client.after_create.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        client.finish_create.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.after_create = None
        return await asyncio.wait_for(create().ensure("soc"), timeout=2)

    recovered = asyncio.run(exercise())
    assert recovered.runtime_agent_id == "runtime-1"
    assert client.create_calls == 1
    assert client.deleted == []


@pytest.mark.parametrize("blocker", ["other_binding", "local_session", "remote_session"])
def test_fault_injection_cleanup_never_deletes_owned_runtime_agent(fault_provisioning, blocker: str) -> None:
    client, store, create = fault_provisioning
    binding = asyncio.run(create().ensure("soc"))
    client.names["not-an-orphan"] = client.names[binding.runtime_agent_id]
    if blocker == "other_binding":
        store.bind_agent_version(
            agent_id="other",
            agent_version_id="other-version",
            digest="other-digest",
            runtime_agent_id="not-an-orphan",
        )
    elif blocker == "local_session":
        store.bind_session(
            session_id="existing-session",
            agent_id="soc",
            agent_version_id=binding.agent_version_id,
            digest=binding.harness_digest,
            runtime_agent_id="not-an-orphan",
        )
    else:
        client.sessions["not-an-orphan"] = ["existing-session"]

    with pytest.raises(RuntimeStateConflict, match="cleanup target"):
        asyncio.run(create().ensure("soc"))
    assert client.deleted == []
    assert set(client.names) == {binding.runtime_agent_id, "not-an-orphan"}


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_fault_injection_binding_failure_compensates_or_remains_discoverable(
    fault_provisioning,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    client, _store, create = fault_provisioning
    provisioner = create()
    original_bind = provisioner.store.bind_agent_version

    def fail_binding(**_kwargs):
        raise RuntimeStateConflict("injected binding failure")

    monkeypatch.setattr(provisioner.store, "bind_agent_version", fail_binding)
    client.delete_failures = int(cleanup_fails)
    with pytest.raises(RuntimeUpstreamError if cleanup_fails else RuntimeStateConflict) as caught:
        asyncio.run(provisioner.ensure("soc"))
    if cleanup_fails:
        assert isinstance(caught.value.__context__, RuntimeStateConflict)
        assert set(client.names) == {"runtime-1"}
    else:
        assert client.names == {}
    monkeypatch.setattr(provisioner.store, "bind_agent_version", original_bind)

    recovered = asyncio.run(create().ensure("soc"))
    assert recovered.runtime_agent_id == ("runtime-1" if cleanup_fails else "runtime-2")
    assert client.create_calls == (1 if cleanup_fails else 2)
