from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from agentgov_agentscope_contract import is_runtime_template_restart_response

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_worktree_inspection import inspect_clean_worktree
from app.runtime.json_types import JsonObject
from app.runtime.state_machines import is_agent_lifecycle_runnable
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore

from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .contracts import AgentRunResponse
from .harness_contract import agent_payload_from_workspace, session_settings_from_workspace
from .harness_snapshots import PublishedHarnessSnapshotStore
from .models import RuntimeSessionBindingModel
from .release_activation import ReleaseActivation, published_runtime_name, release_activation_key
from .release_registry_activation import activate_registry_after_release
from .store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict, harness_digest

_PROVISION_TIMEOUT_SECONDS = 30.0
_T = TypeVar("_T")


@dataclass(frozen=True)
class RuntimeAgentBinding:
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str
    workspace_id: str
    permission_mode: str
    cwd: str
    model_profile: str
    activation_key: str | None = None


@dataclass(frozen=True)
class RuntimeCurrentVersion:
    governance_agent_id: str
    agent_version_id: str
    harness_digest: str
    runtime_agent_id: str | None

    @property
    def provisioned(self) -> bool:
        return self.runtime_agent_id is not None


@asynccontextmanager
async def _published_provision_lock(workspace: Path) -> AsyncIterator[None]:
    """锁住既有不可变快照目录，不创建第二份状态或可漂移的锁文件。"""

    descriptor = os.open(workspace.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


async def _settle_provision_operation(operation: Awaitable[_T]) -> _T:
    """取消调用方不能提前释放锁；非流式请求在有界期限内完成对账。"""

    pending = asyncio.create_task(asyncio.wait_for(operation, timeout=_PROVISION_TIMEOUT_SECONDS))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            if pending.cancelled():
                raise
            cancellation = cancellation or exc
        except Exception as exc:
            if cancellation is not None:
                raise cancellation from exc
            if isinstance(exc, TimeoutError):
                # HTTP 截止不证明远端未提交。稳定名称必须保留，供后续重试
                # 查询；这里不宣称跨服务 exactly-once。
                raise RuntimeUpstreamError(504, b'{"detail":"Published Runtime provisioning timed out; remote result is unknown"}') from exc
            raise
        else:
            if cancellation is not None:
                raise cancellation
            return result


class RuntimeAgentProvisioner:
    """按 Git 版本懒创建不可变 AgentScope Agent。"""

    def __init__(
        self,
        *,
        client: AgentScopeRuntimeClient,
        store: RuntimeRunStore,
        registry: AgentRegistryStore,
        version_store_for: Callable[[str], GitAgentVersionStore],
        snapshot_store: PublishedHarnessSnapshotStore,
        read_version_store_for: Callable[[str], GitAgentVersionStore] | None = None,
        release_session_config: JsonObject | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.registry = registry
        self.version_store_for = version_store_for
        self.read_version_store_for = read_version_store_for or version_store_for
        self.snapshot_store = snapshot_store
        self.release_activation = ReleaseActivation(
            client=client,
            store=store,
            snapshot_store=snapshot_store,
            session_config=dict(release_session_config or {}),
        )

    async def ensure(self, agent_id: str) -> RuntimeAgentBinding:
        _record, workspace, version_id, digest, workspace_id, settings = await asyncio.to_thread(
            self._current_source,
            agent_id,
        )
        permission_mode, cwd, model_profile = settings
        async with _published_provision_lock(workspace):
            runtime_agent_id = await _settle_provision_operation(
                self._ensure_runtime_agent(
                    agent_id=agent_id,
                    version_id=version_id,
                    digest=digest,
                    workspace=workspace,
                    workspace_id=workspace_id,
                ),
            )
        return RuntimeAgentBinding(
            agent_id,
            version_id,
            runtime_agent_id,
            digest,
            workspace_id,
            permission_mode,
            cwd,
            model_profile,
        )

    async def ensure_version(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        candidate_worktree: Path,
    ) -> RuntimeAgentBinding:
        """在 Git 活动指针切换前准备并验证一个精确候选版本。"""

        for _ in range(2):
            workspace, digest, workspace_id, settings = await asyncio.to_thread(
                self._candidate_source,
                agent_id,
                agent_version_id,
                candidate_worktree,
            )
            permission_mode, cwd, model_profile = settings
            activation_key = release_activation_key(agent_id, agent_version_id, digest)
            source_id = workspace_id.split("--v-", 1)[0]
            async with _published_provision_lock(workspace):
                result = await _settle_provision_operation(
                    self.release_activation.ensure(
                        agent_id=agent_id,
                        version_id=agent_version_id,
                        digest=digest,
                        workspace=workspace,
                        workspace_id=workspace_id,
                        source_id=source_id,
                        activation_key=activation_key,
                    ),
                )
            if result is not None:
                runtime_agent_id, owns_activation = result
                break
            # 补偿删除了旧快照；释放目录锁后重新从同一 Git commit 准备。
        else:
            raise RuntimeStateConflict("Release activation cleanup changed repeatedly; retry the same publish command")
        return RuntimeAgentBinding(
            agent_id,
            agent_version_id,
            runtime_agent_id,
            digest,
            workspace_id,
            permission_mode,
            cwd,
            model_profile,
            activation_key if owns_activation else None,
        )

    async def complete_release_activation(self, binding: RuntimeAgentBinding) -> None:
        """Git 活动指针确认切换后，完成 ledger 与 draft 生命周期激活。"""

        if binding.activation_key is not None:
            await asyncio.to_thread(
                self.store.mark_release_activation_active,
                binding.activation_key,
            )
        await asyncio.to_thread(activate_registry_after_release, self.registry, binding.agent_id)

    async def compensate_release_activation(self, binding: RuntimeAgentBinding) -> None:
        """Git 未激活时只清理本次 activation ledger 明确拥有的资源。"""

        if binding.activation_key is not None:
            await self.release_activation.cleanup(binding.activation_key)

    async def _ensure_runtime_agent(
        self,
        *,
        agent_id: str,
        version_id: str,
        digest: str,
        workspace: Path,
        workspace_id: str,
    ) -> str:
        # 远端稳定名称也是响应丢失和补偿失败后的恢复定位符。已绑定时不能
        # 提前返回，否则此前创建但未成功删除的同名资源将永远不可见。
        existing = self.store.get_agent_version(agent_id=agent_id, agent_version_id=version_id, digest=digest)
        runtime_name = published_runtime_name(workspace_id)
        matches = await self.client.list_agent_ids_by_name(runtime_name)
        if existing is not None:
            if existing.runtime_agent_id not in matches:
                raise RuntimeStateConflict("Published Runtime Agent binding is missing upstream")
            for runtime_agent_id in matches:
                if runtime_agent_id != existing.runtime_agent_id:
                    await self._delete_unbound_agent(runtime_agent_id)
            return existing.runtime_agent_id
        if len(matches) > 1:
            raise RuntimeStateConflict("Published Runtime Agent identity is ambiguous")
        created_here = not matches
        try:
            runtime_agent_id = (
                matches[0]
                if matches
                else await self.client.create_agent(
                    agent_payload_from_workspace(workspace, display_name=runtime_name),
                )
            )
        except RuntimeUpstreamError as exc:
            if is_runtime_template_restart_response(exc.status_code, exc.body):
                raise RuntimeStateConflict(
                    "Published subagent templates are prepared; restart AgentScope Runtime and retry provision",
                ) from exc
            raise
        try:
            bound = self.store.bind_agent_version(
                agent_id=agent_id,
                agent_version_id=version_id,
                digest=digest,
                runtime_agent_id=runtime_agent_id,
                governance_agent_id=agent_id,
                source_kind="published",
                source_id=workspace_id.split("--v-", 1)[0],
            )
        except Exception:
            if created_here:
                # 删除失败必须向调用方报告；名称不变，下次 ensure 可以发现并
                # 重新绑定或续清，不能静默遗失远端资源定位证据。
                await self._delete_unbound_agent(runtime_agent_id)
            raise
        return bound.runtime_agent_id

    async def _delete_unbound_agent(self, runtime_agent_id: str) -> None:
        if self.store.get_agent_version_by_runtime_id(runtime_agent_id) is not None:
            raise RuntimeStateConflict("Published Runtime Agent cleanup target is already bound")
        if self.store.sessions_for_runtime_agent(runtime_agent_id):
            raise RuntimeStateConflict("Published Runtime Agent cleanup target has local Sessions")
        try:
            if await self.client.list_session_ids(runtime_agent_id):
                raise RuntimeStateConflict("Published Runtime Agent cleanup target has Runtime Sessions")
            await self.client.delete_agent(runtime_agent_id)
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise

    def current(self, agent_id: str) -> RuntimeAgentBinding | None:
        """只读投影当前 Git Harness；未 provision 时返回 None。"""

        _record, _workspace, _version_store, version_id, digest = self._current_version_source(agent_id)
        existing = self.store.get_agent_version(
            agent_id=agent_id,
            agent_version_id=version_id,
            digest=digest,
        )
        if existing is None:
            return None
        snapshot = self.snapshot_store.require_existing(
            agent_id=agent_id,
            agent_version_id=version_id,
            expected_digest=digest,
        )
        settings = session_settings_from_workspace(snapshot.workspace)
        permission_mode, cwd, model_profile = settings
        return RuntimeAgentBinding(
            agent_id,
            version_id,
            existing.runtime_agent_id,
            digest,
            snapshot.workspace_id,
            permission_mode,
            cwd,
            model_profile,
        )

    def inspect_current(self, agent_id: str) -> RuntimeCurrentVersion:
        _record, _workspace, _version_store, version_id, digest = self._current_version_source(agent_id)
        existing = self.store.get_agent_version(
            agent_id=agent_id,
            agent_version_id=version_id,
            digest=digest,
        )
        if existing is not None:
            self.snapshot_store.require_existing(
                agent_id=agent_id,
                agent_version_id=version_id,
                expected_digest=digest,
            )
        return RuntimeCurrentVersion(
            governance_agent_id=agent_id,
            agent_version_id=version_id,
            harness_digest=digest,
            runtime_agent_id=existing.runtime_agent_id if existing is not None else None,
        )

    def require_current_runtime(self, runtime_agent_id: str) -> RuntimeAgentBinding:
        """反查并校验传入 ID 精确对应当前发布 tuple，绝不 lazy provision。"""

        version = self.store.get_agent_version_by_runtime_id(runtime_agent_id)
        if version is None:
            raise RuntimeObjectNotFound(f"Runtime Agent is not provisioned: {runtime_agent_id}")
        current = self.current(version.agent_id)
        if current is None or current.runtime_agent_id != runtime_agent_id:
            raise RuntimeStateConflict("Runtime Agent is not the current published Agent version")
        return current

    def require_session(self, session_id: str, runtime_agent_id: str) -> RuntimeSessionBindingModel:
        binding = self.store.get_session(session_id, runtime_agent_id=runtime_agent_id)
        self._require_runnable_agent_generation(binding.agent_id, binding.created_at)
        self.snapshot_store.require_existing(
            agent_id=binding.agent_id,
            agent_version_id=binding.agent_version_id,
            expected_digest=binding.harness_digest,
        )
        return binding

    def authorize_run(self, run: AgentRunResponse) -> None:
        self._require_runnable_agent_generation(run.agent_id, run.created_at)
        self.snapshot_store.require_existing(
            agent_id=run.agent_id,
            agent_version_id=run.agent_version_id,
            expected_digest=run.harness_digest,
        )

    def _require_runnable_agent_generation(self, agent_id: str, bound_at: str) -> None:
        record = self.registry.get_agent(agent_id)
        if record is None or not is_agent_lifecycle_runnable(record.status) or record.created_at > bound_at or self.store.agent_deletion_pending(agent_id):
            raise RuntimeObjectNotFound("Runtime resource does not belong to a runnable Agent generation")

    def _current_source(
        self,
        agent_id: str,
    ) -> tuple[AgentRegistryRecord, Path, str, str, str, tuple[str, str, str]]:
        record, _workspace, version_store, version_id, digest = self._current_version_source(
            agent_id,
            bootstrap=True,
        )
        snapshot = self.snapshot_store.materialize(
            version_store=version_store,
            agent_id=agent_id,
            agent_version_id=version_id,
            expected_digest=digest,
        )
        return (
            record,
            snapshot.workspace,
            version_id,
            digest,
            snapshot.workspace_id,
            session_settings_from_workspace(snapshot.workspace),
        )

    def _candidate_source(
        self,
        agent_id: str,
        agent_version_id: str,
        candidate_worktree: Path,
    ) -> tuple[Path, str, str, tuple[str, str, str]]:
        if self.store.agent_deletion_pending(agent_id):
            raise RuntimeObjectNotFound(f"Business Agent deletion is pending: {agent_id}")
        record = self.registry.get_agent(agent_id)
        if record is None or record.status not in {"active", "draft"}:
            raise RuntimeObjectNotFound(f"Business Agent not found: {agent_id}")
        version_store = self.version_store_for(agent_id)
        resolved = version_store.resolve_commit_sha(agent_version_id)
        if resolved != agent_version_id:
            raise RuntimeStateConflict("Release candidate must be a fully resolved Git commit")
        worktree = Path(candidate_worktree)
        worktree_commit, dirty = inspect_clean_worktree(version_store, worktree)
        if worktree_commit != resolved:
            raise RuntimeStateConflict("Release candidate worktree HEAD does not match the publication intent")
        if dirty:
            raise RuntimeStateConflict("Release candidate worktree has uncommitted changes")
        digest = harness_digest(worktree)
        existing_versions = [version for version in self.store.agent_versions_for_agent(agent_id) if version.agent_version_id == resolved]
        if existing_versions and any(version.harness_digest != digest for version in existing_versions):
            raise RuntimeStateConflict("Release candidate commit conflicts with its immutable Runtime binding")
        snapshot = self.snapshot_store.materialize(
            version_store=version_store,
            agent_id=agent_id,
            agent_version_id=resolved,
            expected_digest=digest,
        )
        # 在任何远程副作用前完成本地 Harness 契约校验。
        agent_payload_from_workspace(snapshot.workspace, display_name=published_runtime_name(snapshot.workspace_id))
        settings = session_settings_from_workspace(snapshot.workspace)
        return snapshot.workspace, digest, snapshot.workspace_id, settings

    def _current_version_source(
        self,
        agent_id: str,
        *,
        bootstrap: bool = False,
    ) -> tuple[AgentRegistryRecord, Path, GitAgentVersionStore, str, str]:
        if self.store.agent_deletion_pending(agent_id):
            raise RuntimeObjectNotFound(f"Business Agent deletion is pending: {agent_id}")
        record = self.registry.get_agent(agent_id)
        if record is None or not is_agent_lifecycle_runnable(record.status):
            raise RuntimeObjectNotFound(f"Business Agent is not runnable: {agent_id}")
        workspace = Path(record.workspace_dir)
        provider = self.version_store_for if bootstrap else self.read_version_store_for
        version_store: GitAgentVersionStore = provider(agent_id)
        if bootstrap:
            version_store.ensure_bootstrap()
        version_id, dirty = version_store.inspect_clean_head()
        if dirty:
            raise RuntimeStateConflict(
                f"Business Agent Harness must be a clean governed Git version before Runtime use: {agent_id}",
            )
        digest = harness_digest(workspace)
        return record, workspace, version_store, version_id, digest
