from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry, WorkspaceProvisionPlan
from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services.agent_candidate_writer import (
    AgentCandidateWriteError,
    AgentCandidateWriter,
)
from app.services.agent_governance import (
    TERMINAL_CHANGE_SET_STATES,
    AgentGovernanceError,
    AgentGovernanceService,
)
from app.services.agent_workspace_git_operations import cleanup_imported_versioning
from app.services.business_agent_deletion import purge_business_agent_storage
from app.services.business_agent_provisioning import provision_business_agent


class AgentCandidateCreationError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class CandidateStageReceipt:
    action: Literal["created", "candidate_committed", "unchanged"]
    agent: AgentRegistryRecord
    change_set: JsonObject
    base_commit_sha: str
    candidate_commit_sha: str
    changed_paths: tuple[str, ...]


@dataclass
class _DraftCandidateState:
    base_commit_sha: str | None = None


@dataclass(frozen=True)
class OpenCandidateSource:
    change_set_id: str
    change_set_status: str
    base_commit_sha: str
    candidate_commit_sha: str
    manifest_text: str
    system_prompt: str


class AgentCandidateCreationService:
    """Create one recoverable change set without mutating the live Workspace HEAD."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        registry_store: AgentRegistryStore,
        governance: AgentGovernanceService,
        candidate_writer: AgentCandidateWriter,
        has_open_change_sets: Callable[[str], bool],
    ) -> None:
        self._settings = settings
        self._registry = registry_store
        self._governance = governance
        self._writer = candidate_writer
        self._has_open_change_sets = has_open_change_sets

    def stage_entries(
        self,
        *,
        agent_id: str,
        name: str | None,
        entries: tuple[WorkspaceProvisionEntry, ...],
        expected_current_commit_sha: str | None,
        replace_tree: bool,
        operator: str,
        title: str,
        note: str | None,
        change_set_id: str | None = None,
        expected_candidate_commit_sha: str | None = None,
    ) -> CandidateStageReceipt:
        record = self._registry.get_agent(agent_id)
        self._validate_continuation_refs(
            change_set_id=change_set_id,
            expected_candidate_commit_sha=expected_candidate_commit_sha,
            expected_current_commit_sha=expected_current_commit_sha,
        )
        if change_set_id is not None and expected_candidate_commit_sha is not None:
            if record is None:
                raise AgentCandidateCreationError(
                    404,
                    "CANDIDATE_AGENT_NOT_FOUND",
                    f"Business Agent not found: {agent_id}",
                )
            return self._stage_continuation(
                record=record,
                change_set_id=change_set_id,
                expected_candidate_commit_sha=expected_candidate_commit_sha,
                entries=entries,
                replace_tree=replace_tree,
                operator=operator,
                note=note,
            )
        if record is None:
            if expected_current_commit_sha is not None:
                raise AgentCandidateCreationError(
                    422,
                    "CANDIDATE_BASE_UNEXPECTED",
                    "A new draft Agent must not carry expected_current_commit_sha",
                )
            clean_name = (name or "").strip()
            if not clean_name:
                raise AgentCandidateCreationError(422, "CANDIDATE_NAME_REQUIRED", "A new draft Agent requires a non-empty name")
            return self._stage_new_draft(
                agent_id=agent_id,
                name=clean_name,
                entries=entries,
                replace_tree=replace_tree,
                operator=operator,
                title=title,
                note=note,
            )
        if not expected_current_commit_sha:
            raise AgentCandidateCreationError(
                422,
                "CANDIDATE_BASE_REQUIRED",
                "An existing Agent requires expected_current_commit_sha",
            )
        return self._stage_existing(
            record=record,
            entries=entries,
            expected_current_commit_sha=expected_current_commit_sha,
            replace_tree=replace_tree,
            operator=operator,
            title=title,
            note=note,
        )

    def open_candidate_source(
        self,
        *,
        agent_id: str,
        change_set_id: str | None = None,
    ) -> OpenCandidateSource | None:
        change_set = self._resolve_open_change_set(agent_id=agent_id, change_set_id=change_set_id)
        if change_set is None:
            return None
        resolved_id = str(change_set["change_set_id"])
        try:
            manifest = self._writer.read_text_file(change_set_id=resolved_id, path="agent.yaml")
            prompt = self._writer.read_text_file(change_set_id=resolved_id, path="AGENT.md")
        except AgentCandidateWriteError as exc:
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_SOURCE_INVALID", exc.detail) from exc
        manifest_commit = str(manifest["candidate_commit_sha"])
        prompt_commit = str(prompt["candidate_commit_sha"])
        if manifest_commit != prompt_commit:
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_SOURCE_CONFLICT",
                "Candidate changed while its native form source was being read",
            )
        manifest_text = manifest.get("content")
        system_prompt = prompt.get("content")
        if not manifest.get("exists") or not isinstance(manifest_text, str):
            raise AgentCandidateCreationError(409, "CANDIDATE_SOURCE_INVALID", "Candidate agent.yaml is missing")
        if not prompt.get("exists") or not isinstance(system_prompt, str):
            raise AgentCandidateCreationError(409, "CANDIDATE_SOURCE_INVALID", "Candidate AGENT.md is missing")
        return OpenCandidateSource(
            change_set_id=resolved_id,
            change_set_status=str(change_set["status"]),
            base_commit_sha=str(change_set["base_commit_sha"]),
            candidate_commit_sha=manifest_commit,
            manifest_text=manifest_text,
            system_prompt=system_prompt,
        )

    @staticmethod
    def _validate_continuation_refs(
        *,
        change_set_id: str | None,
        expected_candidate_commit_sha: str | None,
        expected_current_commit_sha: str | None,
    ) -> None:
        if (change_set_id is None) != (expected_candidate_commit_sha is None):
            raise AgentCandidateCreationError(
                422,
                "CANDIDATE_CONTINUATION_INCOMPLETE",
                "change_set_id and expected_candidate_commit_sha must be provided together",
            )
        if change_set_id is not None and expected_current_commit_sha is not None:
            raise AgentCandidateCreationError(
                422,
                "CANDIDATE_BASE_AMBIGUOUS",
                "Candidate continuation must not carry expected_current_commit_sha",
            )

    def _stage_continuation(
        self,
        *,
        record: AgentRegistryRecord,
        change_set_id: str,
        expected_candidate_commit_sha: str,
        entries: tuple[WorkspaceProvisionEntry, ...],
        replace_tree: bool,
        operator: str,
        note: str | None,
    ) -> CandidateStageReceipt:
        change_set = self._resolve_open_change_set(
            agent_id=record.agent_id,
            change_set_id=change_set_id,
        )
        if change_set is None:
            raise AgentCandidateCreationError(404, "CANDIDATE_CHANGE_SET_NOT_FOUND", "Agent change set not found")
        self._require_live_base_unchanged(record.agent_id, change_set)
        try:
            result = self._writer.write_entries(
                change_set_id=change_set_id,
                entries=entries,
                expected_candidate_commit_sha=expected_candidate_commit_sha,
                replace_tree=replace_tree,
                operator=operator,
                note=note,
            )
        except AgentCandidateWriteError as exc:
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_WRITE_FAILED", exc.detail) from exc
        action: Literal["candidate_committed", "unchanged"] = (
            "unchanged" if str(result["candidate_commit_sha"]) == expected_candidate_commit_sha else "candidate_committed"
        )
        return self._receipt(record=record, change_set=change_set, result=result, action=action)

    def _resolve_open_change_set(
        self,
        *,
        agent_id: str,
        change_set_id: str | None,
    ) -> JsonObject | None:
        if change_set_id is not None:
            change_set = self._governance.get_change_set(change_set_id)
            if change_set is None:
                raise AgentCandidateCreationError(404, "CANDIDATE_CHANGE_SET_NOT_FOUND", "Agent change set not found")
            if str(change_set.get("agent_id") or "") != agent_id:
                raise AgentCandidateCreationError(
                    409,
                    "CANDIDATE_CHANGE_SET_OWNER_CONFLICT",
                    "Agent change set does not belong to the requested Agent",
                )
            if str(change_set.get("status") or "") in TERMINAL_CHANGE_SET_STATES | {"publishing"}:
                raise AgentCandidateCreationError(409, "CANDIDATE_CHANGE_SET_NOT_EDITABLE", "Agent change set is not editable")
            return change_set
        open_sets = [
            item for item in self._governance.list_change_sets(agent_id=agent_id, limit=1000) if str(item.get("status") or "") not in TERMINAL_CHANGE_SET_STATES
        ]
        if len(open_sets) > 1:
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_RECOVERY_PENDING",
                "Multiple unfinished Agent change sets require recovery before editing",
            )
        return open_sets[0] if open_sets else None

    def _require_live_base_unchanged(self, agent_id: str, change_set: JsonObject) -> None:
        try:
            current, dirty = self._governance._store_for(agent_id).inspect_clean_head()
        except AgentGitError as exc:
            raise AgentCandidateCreationError(409, "CANDIDATE_BASE_UNAVAILABLE", "Agent Git base is unavailable") from exc
        if dirty:
            raise AgentCandidateCreationError(409, "CANDIDATE_BASE_DIRTY", "Live Agent Workspace changed while a candidate is open")
        if current != str(change_set["base_commit_sha"]):
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_BASE_CONFLICT",
                "Live Agent Workspace HEAD changed while a candidate is open",
            )

    def _stage_existing(
        self,
        *,
        record: AgentRegistryRecord,
        entries: tuple[WorkspaceProvisionEntry, ...],
        expected_current_commit_sha: str,
        replace_tree: bool,
        operator: str,
        title: str,
        note: str | None,
    ) -> CandidateStageReceipt:
        store = self._governance._store_for(record.agent_id)
        try:
            current, dirty = store.inspect_clean_head()
        except AgentGitError as exc:
            raise AgentCandidateCreationError(409, "CANDIDATE_BASE_UNAVAILABLE", "Agent Git base is unavailable") from exc
        if dirty:
            raise AgentCandidateCreationError(409, "CANDIDATE_BASE_DIRTY", "Live Agent Workspace must be clean before creating a candidate")
        if current != expected_current_commit_sha:
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_BASE_CONFLICT",
                f"Agent Workspace HEAD changed (expected {expected_current_commit_sha}, found {current})",
            )
        change_set = self._create_change_set(
            agent_id=record.agent_id,
            base_commit_sha=current,
            operator=operator,
            title=title,
            note=note,
        )
        return self._write_change_set(
            record=record,
            change_set=change_set,
            entries=entries,
            replace_tree=replace_tree,
            operator=operator,
            note=note,
        )

    def _stage_new_draft(
        self,
        *,
        agent_id: str,
        name: str,
        entries: tuple[WorkspaceProvisionEntry, ...],
        replace_tree: bool,
        operator: str,
        title: str,
        note: str | None,
    ) -> CandidateStageReceipt:
        if not replace_tree:
            raise AgentCandidateCreationError(422, "CANDIDATE_TREE_REQUIRED", "A new draft Agent requires a complete candidate tree")
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        self._require_new_storage_absent(agent_id)
        state = _DraftCandidateState()
        try:
            record = provision_business_agent(
                store=self._registry,
                agent_id=agent_id,
                name=name,
                workspace_dir=layout.workspace,
                plan=WorkspaceProvisionPlan(entries=()),
                finalize_workspace=partial(self._initialize_draft_base, agent_id=agent_id, state=state),
                rollback_workspace_finalization=partial(self._rollback_draft_base, agent_id=agent_id),
                lifecycle_status="draft",
            )
        except AgentCandidateCreationError:
            raise
        except AgentCandidateWriteError as exc:
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_WRITE_FAILED", exc.detail) from exc
        except AgentGovernanceError as exc:
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_CHANGE_SET_FAILED", exc.detail) from exc
        except FeedbackStoreError as exc:
            raise AgentCandidateCreationError(exc.status_code, exc.error_code, str(exc)) from exc
        except (AgentGitError, OSError) as exc:
            raise AgentCandidateCreationError(409, "CANDIDATE_STORAGE_FAILED", "Draft Agent candidate storage failed") from exc
        if state.base_commit_sha is None:
            raise AgentCandidateCreationError(409, "CANDIDATE_RECOVERY_PENDING", "Draft Agent Git base receipt is incomplete")
        try:
            receipt = self._stage_existing(
                record=record,
                entries=entries,
                expected_current_commit_sha=state.base_commit_sha,
                replace_tree=True,
                operator=operator,
                title=title,
                note=note,
            )
        except Exception:
            self._compensate_new_draft(record, operator=operator)
            raise
        return replace(receipt, action="created")

    def _require_new_storage_absent(self, agent_id: str) -> None:
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        paths = (layout.workspace, layout.version_base)
        if any(path.exists() or path.is_symlink() for path in paths):
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_STORAGE_RESIDUE",
                f"Unregistered Agent storage already exists: {agent_id}",
            )

    def _initialize_draft_base(self, _: Path, *, agent_id: str, state: _DraftCandidateState) -> None:
        store = self._new_store(agent_id)
        summary = store.ensure_bootstrap()
        base = str(summary.get("commit_sha") or summary.get("version_id") or store.current_commit_sha() or "")
        if not base:
            raise AgentCandidateCreationError(409, "CANDIDATE_BASE_UNAVAILABLE", "Draft Agent Git base was not created")
        state.base_commit_sha = base

    def _rollback_draft_base(self, _: Path, *, agent_id: str) -> bool:
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        return cleanup_imported_versioning(layout.workspace, layout.version_base)

    def _create_change_set(
        self,
        *,
        agent_id: str,
        base_commit_sha: str,
        operator: str,
        title: str,
        note: str | None,
    ) -> JsonObject:
        if self._has_open_change_sets(agent_id):
            raise AgentCandidateCreationError(409, "CANDIDATE_CHANGE_SET_ACTIVE", f"Agent {agent_id} has an unfinished change set")
        try:
            return self._governance.create_change_set(
                agent_id=agent_id,
                base_commit_sha=base_commit_sha,
                title=title,
                note=note,
                operator=operator,
            )
        except AgentGovernanceError as exc:
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_CHANGE_SET_FAILED", exc.detail) from exc

    def _write_change_set(
        self,
        *,
        record: AgentRegistryRecord,
        change_set: JsonObject,
        entries: tuple[WorkspaceProvisionEntry, ...],
        replace_tree: bool,
        operator: str,
        note: str | None,
    ) -> CandidateStageReceipt:
        base = str(change_set["base_commit_sha"])
        try:
            result = self._writer.write_entries(
                change_set_id=str(change_set["change_set_id"]),
                entries=entries,
                expected_candidate_commit_sha=base,
                replace_tree=replace_tree,
                operator=operator,
                note=note,
            )
        except AgentCandidateWriteError as exc:
            self._compensate_change_set(change_set, operator=operator)
            raise AgentCandidateCreationError(exc.status_code, "CANDIDATE_WRITE_FAILED", exc.detail) from exc
        action: Literal["candidate_committed", "unchanged"] = "unchanged" if str(result["candidate_commit_sha"]) == base else "candidate_committed"
        return self._receipt(record=record, change_set=change_set, result=result, action=action)

    def _compensate_change_set(self, change_set: JsonObject, *, operator: str) -> None:
        try:
            self._governance.abandon_change_set(
                str(change_set["change_set_id"]),
                operator=operator,
                note="Compensate failed candidate write",
            )
        except Exception as exc:
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_RECOVERY_PENDING",
                "Candidate write failed and change-set cleanup is pending",
            ) from exc

    def _compensate_new_draft(self, record: AgentRegistryRecord, *, operator: str) -> None:
        """精确补偿本调用刚创建的 draft，不触碰同 ID 的后继代际。"""

        try:
            for change_set in self._governance.list_change_sets(agent_id=record.agent_id, limit=1000):
                if str(change_set.get("status") or "") in TERMINAL_CHANGE_SET_STATES:
                    continue
                self._governance.abandon_change_set(
                    str(change_set["change_set_id"]),
                    operator=operator,
                    note="Compensate failed new draft candidate",
                )
            self._registry.tombstone_failed_draft_generation(
                record.agent_id,
                expected_created_at=record.created_at,
                expected_workspace_dir=record.workspace_dir,
            )
            self._governance.evict_agent_store(record.agent_id)
            cleanup = purge_business_agent_storage(
                data_dir=self._settings.data_dir,
                agent_id=record.agent_id,
            )
            if not cleanup.cleanup_complete:
                raise RuntimeError("draft storage cleanup incomplete")
        except Exception as exc:
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_RECOVERY_PENDING",
                "New draft candidate failed and exact-generation cleanup is pending",
            ) from exc

    @staticmethod
    def _receipt(
        *,
        record: AgentRegistryRecord,
        change_set: JsonObject,
        result: JsonObject,
        action: Literal["created", "candidate_committed", "unchanged"],
    ) -> CandidateStageReceipt:
        changed_paths = result.get("changed_paths")
        return CandidateStageReceipt(
            action=action,
            agent=record,
            change_set=result,
            base_commit_sha=str(change_set["base_commit_sha"]),
            candidate_commit_sha=str(result["candidate_commit_sha"]),
            changed_paths=tuple(str(path) for path in changed_paths) if isinstance(changed_paths, list) else (),
        )

    def _new_store(self, agent_id: str) -> GitAgentVersionStore:
        layout = business_agent_layout(self._settings.data_dir, agent_id)
        return GitAgentVersionStore(
            repository_dir=layout.workspace,
            worktrees_dir=layout.version_base / "worktrees",
            releases_dir=layout.version_base / "releases",
            repository_name=f"{agent_id}-config",
            git_user_name=self._settings.agent_git_user_name,
            git_user_email=self._settings.agent_git_user_email,
        )
