"""AgentScope 远端资源与本地不可变快照的可恢复删除 saga。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from app.runtime.agent_paths import business_agent_layout
from app.runtime.errors import BusinessRuleViolation
from app.runtime.protected_business_agents import is_protected_business_agent
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.models import RuntimeAgentDeletionIntentModel
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from app.services.business_agent_deletion import purge_business_agent_storage


@dataclass(frozen=True)
class RuntimeAgentDeletionResult:
    intent_id: str
    workspace_removed: bool
    cleanup_complete: bool


class _DeletionVersion(TypedDict):
    runtime_agent_id: str
    agent_version_id: str
    harness_digest: str
    version_owner_id: str
    source_kind: str
    source_id: str | None


class _DeletionSession(TypedDict):
    session_id: str
    runtime_agent_id: str


class RuntimeAgentDeletionService:
    """先保存定位证据，再逐项幂等清除 AgentScope 与本地运行资源。"""

    def __init__(
        self,
        *,
        client: AgentScopeRuntimeClient,
        store: RuntimeRunStore,
        registry: AgentRegistryStore,
        snapshots: PublishedHarnessSnapshotStore,
        data_dir: Path,
        evict_agent_store: Callable[[str], None],
    ) -> None:
        self.client = client
        self.store = store
        self.registry = registry
        self.snapshots = snapshots
        self.data_dir = data_dir
        self.evict_agent_store = evict_agent_store

    def start(self, record: AgentRegistryRecord) -> RuntimeAgentDeletionIntentModel:
        if is_protected_business_agent(record.agent_id):
            raise BusinessRuleViolation(f"Business agent '{record.agent_id}' is protected: its built-in Workspace lives in the project repository")
        expected_workspace = business_agent_layout(self.data_dir, record.agent_id).workspace
        if record.workspace_dir != str(expected_workspace):
            raise RuntimeStateConflict("Business Agent workspace is outside its governed deletion root")
        return self.store.start_agent_deletion(
            agent_id=record.agent_id,
            agent_generation=record.created_at,
            workspace_dir=record.workspace_dir,
        )

    async def resume(
        self,
        intent_id: str,
        *,
        assert_maintenance_active: Callable[[], None],
    ) -> RuntimeAgentDeletionResult:
        intent = self.store.get_agent_deletion(intent_id)
        if intent.status == "cleanup_complete":
            return RuntimeAgentDeletionResult(intent.intent_id, intent.workspace_removed, True)
        self.store.record_agent_deletion_progress(intent_id, increment_attempt=True)
        stage = "tombstone"
        try:
            intent = self._tombstone(intent, assert_maintenance_active)
            stage = "enumerate_runtime_sessions"
            intent = await self._enumerate_sessions(intent, assert_maintenance_active)
            stage = "delete_runtime_session"
            intent = await self._delete_sessions(intent, assert_maintenance_active)
            stage = "delete_runtime_agent"
            intent = await self._delete_runtime_agents(intent, assert_maintenance_active)
            stage = "delete_local_bindings"
            intent = self._delete_local_bindings(intent, assert_maintenance_active)
            stage = "delete_published_snapshot"
            intent = self._delete_snapshots(intent, assert_maintenance_active)
            stage = "complete_ephemeral_ledgers"
            self._complete_ephemeral_ledgers(intent, assert_maintenance_active)
            stage = "delete_workspace"
            intent = self._delete_workspace(intent, assert_maintenance_active)
            stage = "complete"
            assert_maintenance_active()
            intent = self.store.complete_agent_deletion(intent_id)
            return RuntimeAgentDeletionResult(intent.intent_id, intent.workspace_removed, True)
        except Exception as exc:  # noqa: BLE001 - durable saga stores only safe class/stage metadata.
            pending = self.store.record_agent_deletion_progress(
                intent_id,
                error={"stage": stage, "error_type": type(exc).__name__},
            )
            return RuntimeAgentDeletionResult(pending.intent_id, pending.workspace_removed, False)

    def _tombstone(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        assert_active()
        self.registry.tombstone_business_agent_generation(
            intent.agent_id,
            expected_created_at=intent.agent_generation,
        )
        return self.store.record_agent_deletion_progress(intent.intent_id, tombstoned=True)

    async def _enumerate_sessions(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        for version in _versions(intent):
            runtime_agent_id = version["runtime_agent_id"]
            if runtime_agent_id in set(intent.enumerated_runtime_agent_ids_json or []):
                continue
            assert_active()
            try:
                session_ids = await self.client.list_session_ids(runtime_agent_id)
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
                session_ids = []
            intent = self.store.record_agent_deletion_enumeration(
                intent.intent_id,
                runtime_agent_id=runtime_agent_id,
                session_ids=session_ids,
            )
        return intent

    async def _delete_sessions(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        for session in _sessions(intent):
            session_id = session["session_id"]
            if session_id in set(intent.deleted_session_ids_json or []):
                continue
            assert_active()
            try:
                await self.client.delete_session(session_id, session["runtime_agent_id"])
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
            intent = self.store.record_agent_deletion_progress(
                intent.intent_id,
                deleted_session_id=session_id,
            )
        return intent

    async def _delete_runtime_agents(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        for version in _versions(intent):
            runtime_agent_id = version["runtime_agent_id"]
            if runtime_agent_id in set(intent.deleted_runtime_agent_ids_json or []):
                continue
            assert_active()
            try:
                await self.client.delete_agent(runtime_agent_id)
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
            intent = self.store.record_agent_deletion_progress(
                intent.intent_id,
                deleted_runtime_agent_id=runtime_agent_id,
            )
        return intent

    def _delete_local_bindings(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        assert_active()
        self.store.remove_agent_deletion_local_bindings(intent.intent_id)
        return self.store.get_agent_deletion(intent.intent_id)

    def _delete_snapshots(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        for version in _versions(intent):
            snapshot_id = _snapshot_id(version)
            if snapshot_id in set(intent.removed_snapshot_ids_json or []):
                continue
            assert_active()
            source_id = version.get("source_id")
            remove = self.snapshots.remove_exact_source if source_id else self.snapshots.remove
            remove_kwargs = {
                "agent_id": intent.agent_id,
                "agent_version_id": version["agent_version_id"],
                "expected_digest": version["harness_digest"],
            }
            removed = remove(source_id=source_id, **remove_kwargs) if source_id else remove(**remove_kwargs)
            if not removed:
                raise RuntimeStateConflict("Published Harness snapshot removal was not confirmed")
            intent = self.store.record_agent_deletion_progress(
                intent.intent_id,
                removed_snapshot_id=snapshot_id,
            )
        return intent

    def _complete_ephemeral_ledgers(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> None:
        assert_active()
        self.store.complete_ephemeral_resources_for_runtime_agents(
            {version["runtime_agent_id"] for version in _versions(intent)},
        )

    def _delete_workspace(
        self,
        intent: RuntimeAgentDeletionIntentModel,
        assert_active: Callable[[], None],
    ) -> RuntimeAgentDeletionIntentModel:
        assert_active()
        purge = purge_business_agent_storage(data_dir=self.data_dir, agent_id=intent.agent_id)
        if not purge.workspace_removed:
            raise RuntimeStateConflict("Business Agent workspace removal was not confirmed")
        self.evict_agent_store(intent.agent_id)
        return self.store.record_agent_deletion_progress(
            intent.intent_id,
            workspace_removed=True,
        )


def _versions(intent: RuntimeAgentDeletionIntentModel) -> list[_DeletionVersion]:
    values: list[_DeletionVersion] = []
    for item in intent.versions_json or []:
        runtime_agent_id = item.get("runtime_agent_id") if isinstance(item, dict) else None
        version_id = item.get("agent_version_id") if isinstance(item, dict) else None
        digest = item.get("harness_digest") if isinstance(item, dict) else None
        version_owner_id = item.get("version_owner_id") if isinstance(item, dict) else None
        source_kind = item.get("source_kind") if isinstance(item, dict) else None
        source_id = item.get("source_id") if isinstance(item, dict) else None
        if not all(isinstance(value, str) and value for value in (runtime_agent_id, version_id, digest)):
            raise RuntimeStateConflict("Agent deletion Runtime version tuple is invalid")
        if not isinstance(version_owner_id, str) or not version_owner_id:
            raise RuntimeStateConflict("Agent deletion Runtime version owner is invalid")
        if source_kind not in {"published", "candidate_snapshot"}:
            raise RuntimeStateConflict("Agent deletion Runtime source kind is invalid")
        if source_id is not None and (not isinstance(source_id, str) or not source_id):
            raise RuntimeStateConflict("Agent deletion Runtime source id is invalid")
        values.append(
            {
                "runtime_agent_id": runtime_agent_id,
                "agent_version_id": version_id,
                "harness_digest": digest,
                "version_owner_id": version_owner_id,
                "source_kind": source_kind,
                "source_id": source_id,
            },
        )
    return values


def _sessions(intent: RuntimeAgentDeletionIntentModel) -> list[_DeletionSession]:
    values: list[_DeletionSession] = []
    for item in intent.sessions_json or []:
        session_id = item.get("session_id") if isinstance(item, dict) else None
        runtime_agent_id = item.get("runtime_agent_id") if isinstance(item, dict) else None
        if not isinstance(session_id, str) or not session_id or not isinstance(runtime_agent_id, str) or not runtime_agent_id:
            raise RuntimeStateConflict("Agent deletion Runtime Session tuple is invalid")
        values.append({"session_id": session_id, "runtime_agent_id": runtime_agent_id})
    return values


def _snapshot_id(version: _DeletionVersion) -> str:
    return f"{version['source_kind']}:{version.get('source_id') or ''}:{version['agent_version_id']}:{version['harness_digest']}"
