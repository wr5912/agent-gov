"""Durable business-Agent deletion with exact-instance and inode-CAS fences."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Literal, TypedDict

from app.runtime.agent_deletion_fs import (
    purge_quarantined_agent_layout,
    quarantine_agent_layout,
    remove_quarantine_witness,
)
from app.runtime.errors import FeedbackStoreError
from app.runtime.stores.agent_deletion_store import (
    AgentDeletionOperation,
    AgentDeletionStore,
    AgentDeletionStoreError,
)


class BusinessAgentDeletionError(FeedbackStoreError):
    """A stable API-facing deletion failure."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = code
        self.code = code
        self.detail = detail


class AgentDeletionReconciliationSummary(TypedDict):
    completed: int
    cleanup_pending: int


class BusinessAgentDeletionService:
    def __init__(
        self,
        store: AgentDeletionStore,
        *,
        data_dir: Path,
        mutation_guard_for: Callable[[str], AbstractContextManager[None]] | None = None,
        evict_agent: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._data_dir = data_dir
        self._mutation_guard_for = mutation_guard_for or _no_mutation_guard
        self._evict_agent = evict_agent or _no_evict

    def delete(
        self,
        *,
        agent_id: str,
        agent_instance_etag: str,
        idempotency_key: str,
    ) -> AgentDeletionOperation:
        with self._mutation_guard_for(agent_id):
            return self._delete_locked(
                agent_id=agent_id,
                agent_instance_etag=agent_instance_etag,
                idempotency_key=idempotency_key,
            )

    def _delete_locked(
        self,
        *,
        agent_id: str,
        agent_instance_etag: str,
        idempotency_key: str,
    ) -> AgentDeletionOperation:
        try:
            operation = self._store.begin(
                agent_id=agent_id,
                agent_instance_etag=agent_instance_etag,
                idempotency_key=idempotency_key,
            )
        except AgentDeletionStoreError as exc:
            raise BusinessAgentDeletionError(exc.status_code, exc.code, exc.detail) from exc
        self._evict_agent(agent_id)
        if operation.state == "completed":
            return self._cleanup_completed_witness(operation)
        return self._reconcile_operation_locked(operation)

    def reconcile_operation(self, operation: AgentDeletionOperation) -> AgentDeletionOperation:
        with self._mutation_guard_for(operation.agent_id):
            self._evict_agent(operation.agent_id)
            return self._reconcile_operation_locked(operation)

    def get_status(self, operation_id: str) -> AgentDeletionOperation:
        operation = self._store.get(operation_id)
        if operation is None:
            raise BusinessAgentDeletionError(
                404,
                "AGENT_DELETION_OPERATION_NOT_FOUND",
                "Agent deletion operation was not found",
            )
        return operation

    def list_statuses(
        self,
        *,
        state: Literal["cleanup_pending", "completed"],
        limit: int,
    ) -> list[AgentDeletionOperation]:
        return self._store.list_recent(state=state, limit=limit)

    def _reconcile_operation_locked(self, operation: AgentDeletionOperation) -> AgentDeletionOperation:
        operation = self._ensure_durable_quarantine(operation)
        if not operation.quarantine_confirmed:
            return operation
        result = purge_quarantined_agent_layout(
            data_dir=self._data_dir,
            workspace_path=operation.workspace_path,
            quarantine_path=operation.quarantine_path,
            expected=operation.expected_identity,
        )
        if result.state != "completed":
            return self._store.record_cleanup_failure(
                operation.operation_id,
                error_code=result.error_code or "AGENT_DELETION_CLEANUP_PENDING",
            )
        operation = self._store.confirm_purge(operation.operation_id)
        if not operation.purge_confirmed:
            return self._store.record_cleanup_failure(
                operation.operation_id,
                error_code="AGENT_DELETION_PURGE_ACK_PENDING",
            )
        try:
            completed = self._store.complete(operation.operation_id)
        except AgentDeletionStoreError as exc:
            raise BusinessAgentDeletionError(exc.status_code, exc.code, exc.detail) from exc
        except Exception:
            reread = self._store.get(operation.operation_id)
            if reread is not None and reread.state == "completed":
                return reread
            if reread is not None:
                try:
                    return self._store.record_cleanup_failure(
                        operation.operation_id,
                        error_code="AGENT_DELETION_COMPLETION_ACK_PENDING",
                    )
                except Exception:
                    final_reread = self._store.get(operation.operation_id)
                    if final_reread is not None:
                        return final_reread
            raise
        return self._cleanup_completed_witness(completed)

    def _ensure_durable_quarantine(self, operation: AgentDeletionOperation) -> AgentDeletionOperation:
        if operation.quarantine_confirmed:
            return operation
        result = quarantine_agent_layout(
            data_dir=self._data_dir,
            workspace_path=operation.workspace_path,
            quarantine_path=operation.quarantine_path,
            expected=operation.expected_identity,
        )
        if result.state == "cleanup_pending":
            return self._store.record_cleanup_failure(
                operation.operation_id,
                error_code=result.error_code or "AGENT_DELETION_QUARANTINE_PENDING",
            )
        if result.state == "absent" and operation.expected_identity is not None:
            return self._store.record_cleanup_failure(
                operation.operation_id,
                error_code="AGENT_DELETION_SOURCE_MISSING_BEFORE_QUARANTINE",
            )
        confirmed = self._store.confirm_quarantine(operation.operation_id)
        if confirmed.quarantine_confirmed:
            return confirmed
        return self._store.record_cleanup_failure(
            operation.operation_id,
            error_code="AGENT_DELETION_QUARANTINE_ACK_PENDING",
        )

    def reconcile(self, *, limit: int = 100) -> AgentDeletionReconciliationSummary:
        completed, pending = self._reconcile_pending_batch(limit=limit)
        self._reconcile_witness_batch(limit=limit)
        return {"completed": completed, "cleanup_pending": pending}

    def _reconcile_pending_batch(self, *, limit: int) -> tuple[int, int]:
        completed = 0
        pending = 0
        for operation in self._store.list_pending(limit=limit):
            try:
                result = self.reconcile_operation(operation)
            except Exception:
                _log_reconcile_failure(operation.operation_id, "AGENT_DELETION_RECONCILE_FAILED")
                _record_pending_failure_best_effort(self._store, operation.operation_id)
                pending += 1
                continue
            if result.state == "completed":
                completed += 1
            else:
                pending += 1
        return completed, pending

    def _reconcile_witness_batch(self, *, limit: int) -> None:
        for operation in self._store.list_witness_cleanup(limit=limit):
            try:
                with self._mutation_guard_for(operation.agent_id):
                    self._evict_agent(operation.agent_id)
                    self._cleanup_completed_witness(operation)
            except Exception:
                _log_reconcile_failure(operation.operation_id, "AGENT_DELETION_WITNESS_RECONCILE_FAILED")
                _record_witness_failure_best_effort(self._store, operation.operation_id)

    def _cleanup_completed_witness(self, operation: AgentDeletionOperation) -> AgentDeletionOperation:
        if operation.state != "completed" or operation.witness_removed:
            return operation
        if operation.expected_identity is None or remove_quarantine_witness(
            quarantine_path=operation.quarantine_path,
            expected=operation.expected_identity,
        ):
            try:
                return self._store.confirm_witness_removed(operation.operation_id)
            except Exception:
                reread = self._store.get(operation.operation_id)
                if reread is not None:
                    return reread
                raise
        return self._store.record_witness_cleanup_failure(operation.operation_id)


def _no_mutation_guard(_: str) -> AbstractContextManager[None]:
    return nullcontext()


def _no_evict(_: str) -> None:
    return None


_LOGGER = logging.getLogger(__name__)


def _log_reconcile_failure(operation_id: str, error_code: str) -> None:
    _LOGGER.warning("Agent deletion reconcile isolated operation=%s error_code=%s", operation_id, error_code)


def _record_pending_failure_best_effort(store: AgentDeletionStore, operation_id: str) -> None:
    try:
        store.record_cleanup_failure(operation_id, error_code="AGENT_DELETION_RECONCILE_FAILED")
    except Exception:
        _log_reconcile_failure(operation_id, "AGENT_DELETION_RECONCILE_RECORD_FAILED")


def _record_witness_failure_best_effort(store: AgentDeletionStore, operation_id: str) -> None:
    try:
        store.record_witness_cleanup_failure(operation_id)
    except Exception:
        _log_reconcile_failure(operation_id, "AGENT_DELETION_WITNESS_RECORD_FAILED")


_STRONG_AGENT_ETAG = re.compile(r'^"([0-9a-f]{64})"$')


def parse_agent_if_match(value: str | None) -> str:
    """Parse one strong HTTP entity-tag and return its opaque instance CAS."""

    match = _STRONG_AGENT_ETAG.fullmatch((value or "").strip())
    if match is None:
        raise BusinessAgentDeletionError(
            409,
            "AGENT_DELETION_PRECONDITION",
            'If-Match must contain exactly one quoted strong Agent ETag, for example "<etag>".',
        )
    return match.group(1)
