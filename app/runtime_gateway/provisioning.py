from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.json_types import JsonObject
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore

from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .contracts import AgentRunResponse
from .harness_snapshots import PublishedHarnessSnapshotStore
from .models import RuntimeSessionBindingModel
from .store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict, harness_digest

_PROVISION_TIMEOUT_SECONDS = 30.0


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


async def _settle_provision_operation(operation: Awaitable[str]) -> str:
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
    ) -> None:
        self.client = client
        self.store = store
        self.registry = registry
        self.version_store_for = version_store_for
        self.read_version_store_for = read_version_store_for or version_store_for
        self.snapshot_store = snapshot_store

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
        runtime_name = _published_runtime_name(workspace_id)
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
            if b"published after Runtime startup; restart Runtime" in exc.body:
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
        self._require_active_agent_generation(binding.agent_id, binding.created_at)
        self.snapshot_store.require_existing(
            agent_id=binding.agent_id,
            agent_version_id=binding.agent_version_id,
            expected_digest=binding.harness_digest,
        )
        return binding

    def authorize_run(self, run: AgentRunResponse) -> None:
        self._require_active_agent_generation(run.agent_id, run.created_at)
        self.snapshot_store.require_existing(
            agent_id=run.agent_id,
            agent_version_id=run.agent_version_id,
            expected_digest=run.harness_digest,
        )

    def _require_active_agent_generation(self, agent_id: str, bound_at: str) -> None:
        record = self.registry.get_agent(agent_id)
        if record is None or record.status != "active" or record.created_at > bound_at or self.store.agent_deletion_pending(agent_id):
            raise RuntimeObjectNotFound("Runtime resource does not belong to an active Agent generation")

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

    def _current_version_source(
        self,
        agent_id: str,
        *,
        bootstrap: bool = False,
    ) -> tuple[AgentRegistryRecord, Path, GitAgentVersionStore, str, str]:
        if self.store.agent_deletion_pending(agent_id):
            raise RuntimeObjectNotFound(f"Business Agent deletion is pending: {agent_id}")
        record = self.registry.get_agent(agent_id)
        if record is None:
            raise RuntimeObjectNotFound(f"Business Agent not found: {agent_id}")
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


def agent_payload_from_workspace(workspace: Path, *, display_name: str) -> JsonObject:
    manifest_path = workspace / "agent.yaml"
    instructions_path = workspace / "AGENT.md"
    if not manifest_path.is_file() or not instructions_path.is_file():
        raise RuntimeObjectNotFound("Harness must contain agent.yaml and AGENT.md")
    loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise RuntimeObjectNotFound("agent.yaml must be a mapping")
    agent = loaded.get("agent")
    if not isinstance(agent, dict) or agent.get("runtime") != "agentscope":
        raise RuntimeObjectNotFound("agent.yaml must declare the AgentScope runtime")
    context = loaded.get("context_config")
    react = loaded.get("react_config")
    invite = loaded.get("invite_config")
    system_prompt = instructions_path.read_text(encoding="utf-8")
    subagent_instructions = _subagent_runtime_instructions(workspace)
    if subagent_instructions:
        system_prompt = system_prompt.rstrip() + "\n\n" + subagent_instructions
    request_data: JsonObject = {
        "name": display_name,
        "system_prompt": system_prompt,
    }
    if isinstance(context, dict):
        request_data["context_config"] = context
    if isinstance(react, dict):
        request_data["react_config"] = react
    if isinstance(invite, dict):
        request_data["invite_config"] = invite
    return request_data


def _published_runtime_name(workspace_id: str) -> str:
    source_id = workspace_id.split("--v-", 1)[0]
    if not source_id.startswith("published-"):
        raise RuntimeStateConflict("Published Runtime workspace identity is invalid")
    return f"agentgov-{source_id}"


def _subagent_runtime_instructions(workspace: Path) -> str:
    root = workspace / "subagents"
    if not root.exists():
        return ""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeObjectNotFound("subagents must be a safe directory")
    digest = harness_digest(workspace)
    templates: list[tuple[str, str]] = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_dir() or not (path / "agent.yaml").is_file():
            raise RuntimeObjectNotFound("subagent entry is invalid")
        templates.append((path.name, f"agentgov-{digest}-{path.name}"))
    if not templates:
        return ""
    declarations = "\n".join(f"- `{name}`: `subagent_type={template_type}`" for name, template_type in templates)
    return (
        "## AgentScope 团队委派契约\n\n"
        "需要委派时依次调用 `TeamCreate`、`AgentCreate`、`TeamSay`，结束后调用 `TeamDelete`。"
        "`AgentCreate` 必须使用下列当前 Harness 版本的精确 `subagent_type`，不得使用 `default` 或其他版本：\n"
        f"{declarations}\n"
    )


def session_settings_from_workspace(workspace: Path) -> tuple[str, str, str]:
    """读取只能由已发布 Harness 控制的 Session 字段。"""

    manifest_path = workspace / "agent.yaml"
    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeObjectNotFound("agent.yaml is not readable") from exc
    session = loaded.get("session") if isinstance(loaded, dict) else None
    if not isinstance(session, dict):
        raise RuntimeObjectNotFound("agent.yaml must declare session settings")
    permission_mode = session.get("permission_mode")
    if permission_mode not in {"default", "explore", "accept_edits", "dont_ask"}:
        raise RuntimeStateConflict("Harness session.permission_mode is unsupported or unsafe")
    cwd = session.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd.strip():
        raise RuntimeStateConflict("Harness session.cwd must be a non-empty string")
    model_profile = session.get("model_profile", "default")
    if model_profile != "default":
        raise RuntimeStateConflict("Only the governed default model profile is configured")
    return permission_mode, cwd, model_profile
