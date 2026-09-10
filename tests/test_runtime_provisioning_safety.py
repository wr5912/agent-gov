from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import app.runtime_gateway.provisioning as provisioning_module
import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict


class _Client(AgentScopeRuntimeClient):
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.sessions: dict[str, list[str]] = {}
        self.create_calls = 0
        self.deleted: list[str] = []
        self.delete_failures = 0
        self.after_create: asyncio.Event | None = None
        self.finish_create: asyncio.Event | None = None
        self.before_create: asyncio.Event | None = None
        self.commit_create: asyncio.Event | None = None
        self.create_fails = False

    async def list_agent_ids_by_name(self, name: str) -> list[str]:
        await asyncio.sleep(0)
        return [identifier for identifier, candidate in self.names.items() if candidate == name]

    async def create_agent(self, payload: dict[str, object]) -> str:
        self.create_calls += 1
        identifier = f"runtime-{self.create_calls}"
        if self.before_create is not None:
            self.before_create.set()
            assert self.commit_create is not None
            await self.commit_create.wait()
        if self.create_fails:
            raise RuntimeUpstreamError(503, b'{"detail":"temporary test create outage"}')
        self.names[identifier] = str(payload["name"])
        if self.after_create is not None:
            self.after_create.set()
            assert self.finish_create is not None
            await self.finish_create.wait()
        await asyncio.sleep(0)
        return identifier

    async def list_session_ids(self, runtime_agent_id: str) -> list[str]:
        return self.sessions.get(runtime_agent_id, [])

    async def delete_agent(self, runtime_agent_id: str) -> None:
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeUpstreamError(503, b'{"detail":"temporary test outage"}')
        self.names.pop(runtime_agent_id, None)
        self.deleted.append(runtime_agent_id)


@pytest.fixture
def provisioning(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "sources" / "published-test" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("agent: {id: soc, runtime: agentscope}\n", encoding="utf-8")
    (workspace / "AGENT.md").write_text("test instructions\n", encoding="utf-8")
    factory = make_session_factory(tmp_path / "runtime.db")
    registry = AgentRegistryStore(factory)
    record = registry.create_business_agent(name="SOC", agent_id="soc", workspace_dir=str(workspace))
    client = _Client()
    store = RuntimeRunStore(factory)
    snapshots = PublishedHarnessSnapshotStore(tmp_path / "sources")

    def current_source(_self, agent_id):
        assert agent_id == "soc"
        return record, workspace, "version-one", "digest-one", "published-test--v-digest-one", ("dont_ask", ".", "default")

    monkeypatch.setattr(RuntimeAgentProvisioner, "_current_source", current_source)

    def create_provisioner() -> RuntimeAgentProvisioner:
        return RuntimeAgentProvisioner(
            client=client,
            store=RuntimeRunStore(factory),
            registry=registry,
            version_store_for=lambda _agent_id: GitAgentVersionStore(
                repository_dir=workspace,
                worktrees_dir=tmp_path / "worktrees",
                releases_dir=tmp_path / "releases",
            ),
            snapshot_store=snapshots,
        )

    return client, store, create_provisioner


def test_concurrent_provisioners_create_one_agent_for_the_same_tuple(provisioning) -> None:
    client, store, create = provisioning

    async def exercise():
        return await asyncio.gather(*(create().ensure("soc") for _ in range(4)))

    bindings = asyncio.run(exercise())
    assert {binding.runtime_agent_id for binding in bindings} == {"runtime-1"}
    assert client.create_calls == 1
    assert client.deleted == []
    assert len(store.agent_versions_for_agent("soc")) == 1


def test_cancelled_create_is_recovered_by_name_after_new_provisioner(provisioning) -> None:
    client, _store, create = provisioning

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


@pytest.mark.parametrize("create_fails", [False, True])
def test_repeated_cancellation_waits_for_uncommitted_create_to_settle(provisioning, create_fails) -> None:
    client, _store, create = provisioning

    async def exercise():
        client.before_create = asyncio.Event()
        client.commit_create = asyncio.Event()
        client.create_fails = create_fails
        cancelled = asyncio.create_task(create().ensure("soc"))
        await asyncio.wait_for(client.before_create.wait(), timeout=2)
        cancelled.cancel()
        await asyncio.sleep(0)
        retry = asyncio.create_task(create().ensure("soc"))
        cancelled.cancel()
        await asyncio.sleep(0.1)
        assert not cancelled.done()
        assert not retry.done()
        assert client.create_calls == 1
        assert client.names == {}
        client.commit_create.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await cancelled
        if create_fails:
            assert isinstance(caught.value.__cause__, RuntimeUpstreamError)
        client.create_fails = False
        return await asyncio.wait_for(retry, timeout=2)

    binding = asyncio.run(exercise())
    assert client.create_calls == (2 if create_fails else 1)
    assert set(client.names) == {binding.runtime_agent_id}


@pytest.mark.parametrize("cancel_caller", [False, True])
def test_hung_provision_is_bounded_and_preserves_cancellation(provisioning, monkeypatch, cancel_caller) -> None:
    client, _store, create = provisioning
    monkeypatch.setattr(provisioning_module, "_PROVISION_TIMEOUT_SECONDS", 0.1)

    async def exercise():
        client.before_create = asyncio.Event()
        client.commit_create = asyncio.Event()
        task = asyncio.create_task(create().ensure("soc"))
        await asyncio.wait_for(client.before_create.wait(), timeout=2)
        if cancel_caller:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel_caller else RuntimeUpstreamError) as caught:
            await asyncio.wait_for(task, timeout=2)
        if not cancel_caller:
            assert caught.value.status_code == 504
            assert b"remote result is unknown" in caught.value.body
        client.before_create = None
        return await asyncio.wait_for(create().ensure("soc"), timeout=2)

    binding = asyncio.run(exercise())
    assert client.create_calls == 2
    assert set(client.names) == {binding.runtime_agent_id}


def test_another_process_serializes_provision_and_cancelled_waiter_releases_fd(provisioning) -> None:
    client, _store, create = provisioning
    source = create().snapshot_store.root / "published-test"
    locker = (
        "import fcntl, os, sys; "
        "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY); "
        "fcntl.flock(fd, fcntl.LOCK_EX); "
        "print('locked', flush=True); "
        "sys.stdin.read(1); os.close(fd)"
    )

    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            locker,
            str(source),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stdin is not None
        try:
            assert await asyncio.wait_for(process.stdout.readline(), timeout=2) == b"locked\n"
            blocked = asyncio.create_task(create().ensure("soc"))
            await asyncio.sleep(0.15)
            assert not blocked.done()
            assert client.create_calls == 0
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            process.stdin.write(b"x")
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), timeout=2)
            return await asyncio.wait_for(create().ensure("soc"), timeout=2)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process.stdin.close()

    binding = asyncio.run(exercise())
    assert binding.runtime_agent_id == "runtime-1"
    assert client.create_calls == 1


def test_bound_tuple_retries_partial_orphan_cleanup_after_new_provisioner(provisioning) -> None:
    client, _store, create = provisioning
    binding = asyncio.run(create().ensure("soc"))
    client.names["orphan"] = client.names[binding.runtime_agent_id]
    client.names["unrelated"] = "another-published-name"
    client.delete_failures = 1

    with pytest.raises(RuntimeUpstreamError):
        asyncio.run(create().ensure("soc"))
    assert "orphan" in client.names
    recovered = asyncio.run(create().ensure("soc"))

    assert recovered == binding
    assert set(client.names) == {binding.runtime_agent_id, "unrelated"}
    assert client.deleted == ["orphan"]
    assert client.create_calls == 1


@pytest.mark.parametrize("blocker", ["other_binding", "local_session", "remote_session"])
def test_cleanup_never_deletes_a_bound_or_session_owning_agent(provisioning, blocker) -> None:
    client, store, create = provisioning
    binding = asyncio.run(create().ensure("soc"))
    client.names["not-an-orphan"] = client.names[binding.runtime_agent_id]
    if blocker == "other_binding":
        store.bind_agent_version(agent_id="other", agent_version_id="other-version", digest="other-digest", runtime_agent_id="not-an-orphan")
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


def test_unbound_ambiguous_identity_fails_without_guessing_or_deleting(provisioning) -> None:
    client, _store, create = provisioning
    client.names.update({"first": "agentgov-published-test", "second": "agentgov-published-test"})

    with pytest.raises(RuntimeStateConflict, match="ambiguous"):
        asyncio.run(create().ensure("soc"))
    assert client.create_calls == 0
    assert client.deleted == []


def test_bound_identity_missing_upstream_does_not_rebind(provisioning) -> None:
    client, store, create = provisioning
    binding = asyncio.run(create().ensure("soc"))
    client.names.clear()

    with pytest.raises(RuntimeStateConflict, match="missing upstream"):
        asyncio.run(create().ensure("soc"))
    assert client.create_calls == 1
    assert store.get_agent_version_by_runtime_id(binding.runtime_agent_id) is not None


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_binding_failure_compensation_keeps_failed_delete_discoverable(provisioning, monkeypatch, cleanup_fails) -> None:
    client, _store, create = provisioning
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
