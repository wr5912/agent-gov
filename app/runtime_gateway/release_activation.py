from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import NoReturn

from agentgov_agentscope_contract import is_runtime_template_restart_response, session_workspace_id

from app.runtime.json_types import JsonObject

from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .harness_contract import agent_payload_from_workspace
from .harness_snapshots import PublishedHarnessSnapshotStore
from .models import RuntimeAgentVersionModel, RuntimeEphemeralResourceModel
from .release_probe import ReleaseWorkspaceProbe
from .store import RuntimeRunStore, RuntimeStateConflict


class RuntimeActivationCleanupPending(RuntimeStateConflict):
    """发布激活失败后的持久资源补偿尚未收敛。"""


class RuntimeActivationRestartRequired(RuntimeStateConflict):
    """保留精确发布资源，等待 Runtime 重启注册模板后续发。"""


class ReleaseActivation:
    """管理 Git 切换之前的不可变 Runtime 资源准备与补偿。"""

    def __init__(
        self,
        *,
        client: AgentScopeRuntimeClient,
        store: RuntimeRunStore,
        snapshot_store: PublishedHarnessSnapshotStore,
        session_config: JsonObject,
    ) -> None:
        self.client = client
        self.store = store
        self.snapshot_store = snapshot_store
        self.probe = ReleaseWorkspaceProbe(client, store, session_config)

    async def ensure(
        self,
        *,
        agent_id: str,
        version_id: str,
        digest: str,
        workspace: Path,
        workspace_id: str,
        source_id: str,
        activation_key: str,
    ) -> tuple[str, bool] | None:
        existing = self.store.get_agent_version(
            agent_id=agent_id,
            agent_version_id=version_id,
            digest=digest,
        )
        previous_ledger = self.store.get_ephemeral_resource(activation_key)
        source_kind = (
            previous_ledger.source_kind
            if previous_ledger is not None and previous_ledger.status != "cleanup_complete"
            else "release_probe"
            if existing is not None
            else "release_activation"
        )
        probe_workspace_id = _release_probe_workspace_id(workspace_id)
        ledger = self.store.start_ephemeral_resource(
            cache_key=activation_key,
            business_agent_id=agent_id,
            version_owner_id=agent_id,
            agent_version_id=version_id,
            digest=digest,
            source_id=source_id,
            source_kind=source_kind,
            workspace_id=probe_workspace_id,
        )
        if ledger.status == "cleanup_pending":
            try:
                await self.cleanup(activation_key)
            except Exception as exc:
                raise RuntimeActivationCleanupPending("Release activation cleanup is pending; retry the same publish command") from exc
            return None
        if ledger.status == "awaiting_restart":
            await self._cleanup_restart_probe(activation_key)

        runtime_name = published_runtime_name(workspace_id)
        if ledger.status in {"ready", "active"}:
            return await self._verify_prepared_binding(ledger, existing, runtime_name)
        matches = await self._runtime_matches(activation_key, runtime_name)
        if source_kind == "release_probe":
            return await self._probe_existing_release_binding(
                activation_key=activation_key,
                existing=existing,
                matches=matches,
                probe_workspace_id=probe_workspace_id,
            )
        if existing is not None:
            if ledger.runtime_agent_id != existing.runtime_agent_id or existing.runtime_agent_id not in matches:
                await self._compensate_after_activation_error(
                    activation_key,
                    RuntimeStateConflict("Release activation ledger conflicts with its immutable Runtime binding"),
                )
        return await self._activate_release_binding(
            agent_id=agent_id,
            version_id=version_id,
            digest=digest,
            workspace=workspace,
            source_id=source_id,
            activation_key=activation_key,
            runtime_name=runtime_name,
            matches=matches,
            probe_workspace_id=probe_workspace_id,
        )

    async def _verify_prepared_binding(
        self,
        ledger: RuntimeEphemeralResourceModel,
        existing: RuntimeAgentVersionModel | None,
        runtime_name: str,
    ) -> tuple[str, bool]:
        # Git 可能已经切换而控制面仍待收尾；查询失败不能触发资源回滚。
        if existing is None or ledger.runtime_agent_id != existing.runtime_agent_id or ledger.session_id is not None:
            raise RuntimeStateConflict("Prepared release activation conflicts with its immutable Runtime binding")
        matches = await self.client.list_agent_ids_by_name(runtime_name)
        if matches != [existing.runtime_agent_id]:
            raise RuntimeStateConflict("Prepared release activation Runtime Agent identity is missing or ambiguous")
        return existing.runtime_agent_id, True

    async def _runtime_matches(self, activation_key: str, runtime_name: str) -> list[str]:
        try:
            matches = await self.client.list_agent_ids_by_name(runtime_name)
            if len(matches) > 1:
                raise RuntimeStateConflict("Published Runtime Agent identity is ambiguous")
            return matches
        except Exception as exc:
            await self._compensate_after_activation_error(activation_key, exc)

    async def _probe_existing_release_binding(
        self,
        *,
        activation_key: str,
        existing: RuntimeAgentVersionModel | None,
        matches: list[str],
        probe_workspace_id: str,
    ) -> tuple[str, bool]:
        if existing is None or existing.runtime_agent_id not in matches:
            await self._compensate_after_activation_error(
                activation_key,
                RuntimeStateConflict("Published Runtime Agent binding is missing upstream"),
            )
        self.store.record_ephemeral_agent(activation_key, existing.runtime_agent_id)
        try:
            await self.probe.ensure(
                existing.runtime_agent_id,
                workspace_id=probe_workspace_id,
                activation_key=activation_key,
            )
            await self.probe.cleanup(activation_key)
            self.store.complete_release_probe(activation_key)
        except Exception as exc:
            await self._compensate_after_activation_error(activation_key, exc)
        return existing.runtime_agent_id, False

    async def _activate_release_binding(
        self,
        *,
        agent_id: str,
        version_id: str,
        digest: str,
        workspace: Path,
        source_id: str,
        activation_key: str,
        runtime_name: str,
        matches: list[str],
        probe_workspace_id: str,
    ) -> tuple[str, bool]:
        try:
            runtime_agent_id = await self._locate_or_create_release_agent(
                activation_key=activation_key,
                runtime_name=runtime_name,
                matches=matches,
                workspace=workspace,
            )
            await self.probe.ensure(
                runtime_agent_id,
                workspace_id=probe_workspace_id,
                activation_key=activation_key,
            )
            await self.probe.cleanup(activation_key)
            bound = self.store.bind_agent_version(
                agent_id=agent_id,
                agent_version_id=version_id,
                digest=digest,
                runtime_agent_id=runtime_agent_id,
                governance_agent_id=agent_id,
                source_kind="published",
                source_id=source_id,
            )
            self.store.mark_release_activation_bound(activation_key)
            return bound.runtime_agent_id, True
        except Exception as exc:
            await self._compensate_after_activation_error(activation_key, exc)

    async def _locate_or_create_release_agent(
        self,
        *,
        activation_key: str,
        runtime_name: str,
        matches: list[str],
        workspace: Path,
    ) -> str:
        ledger = self.store.get_ephemeral_resource(activation_key)
        if ledger is None:
            raise RuntimeStateConflict("Release activation ledger is missing")
        if ledger.runtime_agent_id is not None:
            if ledger.runtime_agent_id not in matches or len(matches) != 1:
                raise RuntimeStateConflict("Release activation Runtime Agent locator is ambiguous")
            return ledger.runtime_agent_id
        if matches:
            runtime_agent_id = matches[0]
        else:
            try:
                runtime_agent_id = await self.client.create_agent(
                    agent_payload_from_workspace(workspace, display_name=runtime_name),
                )
            except RuntimeUpstreamError as exc:
                recovered = await self.client.list_agent_ids_by_name(runtime_name)
                if exc.status_code not in {503, 504} or len(recovered) != 1:
                    raise
                runtime_agent_id = recovered[0]
        self.store.record_ephemeral_agent(activation_key, runtime_agent_id)
        return runtime_agent_id

    async def _compensate_after_activation_error(self, activation_key: str, error: Exception) -> NoReturn:
        if isinstance(error, RuntimeUpstreamError) and is_runtime_template_restart_response(error.status_code, error.body):
            self.store.mark_ephemeral_awaiting_restart(
                activation_key,
                stage="release_activation",
                error_type=type(error).__name__,
            )
            await self._cleanup_restart_probe(activation_key)
            raise RuntimeActivationRestartRequired(
                "Release activation requires an AgentScope Runtime maintenance restart; restart Runtime, then retry the same publish command",
            ) from error
        try:
            await self.cleanup(activation_key)
        except Exception as cleanup_error:
            raise RuntimeActivationCleanupPending(
                "Release activation cleanup is pending; retry the same publish command",
            ) from cleanup_error
        raise error

    async def _cleanup_restart_probe(self, activation_key: str) -> None:
        try:
            await self.probe.cleanup(activation_key)
        except Exception as cleanup_error:
            raise RuntimeActivationCleanupPending(
                "Release probe cleanup is pending; the exact snapshot and Runtime Agent are retained; retry the same publish command",
            ) from cleanup_error

    async def cleanup(self, activation_key: str) -> None:
        ledger = self.store.get_ephemeral_resource(activation_key)
        if ledger is None or ledger.status == "cleanup_complete":
            return
        if ledger.status == "active":
            raise RuntimeStateConflict("Active release activation cannot be compensated")
        self.store.mark_ephemeral_cleanup_pending(
            activation_key,
            stage="release_activation",
            error_type="CleanupRequested",
        )
        runtime_agent_id = ledger.runtime_agent_id
        if runtime_agent_id is None and ledger.source_kind == "release_activation":
            runtime_name = published_runtime_name(f"{ledger.source_id}--v-{ledger.harness_digest}")
            matches = await self.client.list_agent_ids_by_name(runtime_name)
            if len(matches) > 1:
                raise RuntimeStateConflict("Release activation Runtime Agent identity is ambiguous during cleanup")
            if matches:
                runtime_agent_id = matches[0]
                self.store.record_ephemeral_agent(activation_key, runtime_agent_id)
        await self.probe.cleanup(activation_key)
        if ledger.source_kind == "release_probe":
            self.store.complete_release_probe(activation_key)
            return
        if ledger.source_kind != "release_activation":
            raise RuntimeStateConflict("Release activation ledger has an unsupported source kind")
        if runtime_agent_id is not None:
            version = self.store.get_agent_version(
                agent_id=ledger.business_agent_id,
                agent_version_id=ledger.agent_version_id,
                digest=ledger.harness_digest,
            )
            if version is not None:
                if version.runtime_agent_id != runtime_agent_id:
                    raise RuntimeStateConflict("Release activation binding changed before compensation")
                self.store.delete_agent_version(
                    agent_id=ledger.business_agent_id,
                    agent_version_id=ledger.agent_version_id,
                    digest=ledger.harness_digest,
                )
            if self.store.sessions_for_runtime_agent(runtime_agent_id):
                raise RuntimeStateConflict("Release activation Runtime Agent acquired a local Session")
            try:
                remote_sessions = await self.client.list_session_ids(runtime_agent_id)
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
                remote_sessions = []
            if remote_sessions:
                raise RuntimeStateConflict("Release activation Runtime Agent acquired a Runtime Session")
            try:
                await self.client.delete_agent(runtime_agent_id)
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
        removed = await asyncio.to_thread(
            self.snapshot_store.remove,
            agent_id=ledger.business_agent_id,
            agent_version_id=ledger.agent_version_id,
            expected_digest=ledger.harness_digest,
        )
        if not removed:
            raise RuntimeStateConflict("Release activation snapshot cleanup was not confirmed")
        self.store.complete_ephemeral_resource(activation_key)


def published_runtime_name(workspace_id: str) -> str:
    source_id = workspace_id.split("--v-", 1)[0]
    if not source_id.startswith("published-"):
        raise RuntimeStateConflict("Published Runtime workspace identity is invalid")
    return f"agentgov-{source_id}"


def release_activation_key(agent_id: str, version_id: str, digest: str) -> str:
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"agentgov:release-activation:{agent_id}:{version_id}:{digest}")
    return f"release-activation:{identity}"


def _release_probe_workspace_id(workspace_id: str) -> str:
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"agentgov:release-probe:{workspace_id}")
    return session_workspace_id(workspace_id, identity)
