from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType

from sqlalchemy.orm import sessionmaker

from app.runtime.agent_admission import (
    AgentMaintenanceClaim,
    AgentMaintenanceClaimLost,
    acquire_maintenance,
    assert_maintenance_claim_active,
    is_maintenance_active,
    release_maintenance,
    renew_maintenance,
)

DEFAULT_MAINTENANCE_LEASE_SECONDS = 300.0
DEFAULT_MAINTENANCE_HEARTBEAT_SECONDS = 30.0


class AgentVersionMaintenanceCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        lease_seconds: float = DEFAULT_MAINTENANCE_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_MAINTENANCE_HEARTBEAT_SECONDS,
    ) -> None:
        if lease_seconds <= heartbeat_seconds or heartbeat_seconds <= 0:
            raise ValueError("maintenance lease must be longer than its positive heartbeat interval")
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def lease(self, *, agent_id: str, kind: str, owner_id: str) -> AgentVersionMaintenanceLease:
        return AgentVersionMaintenanceLease(self, agent_id=agent_id, kind=kind, owner_id=owner_id)

    def is_active(self, agent_id: str) -> bool:
        return is_maintenance_active(self.session_factory, agent_id=agent_id)


class AgentVersionMaintenanceLease:
    def __init__(self, coordinator: AgentVersionMaintenanceCoordinator, *, agent_id: str, kind: str, owner_id: str) -> None:
        self._coordinator = coordinator
        self._agent_id = agent_id
        self._kind = kind
        self._owner_id = owner_id
        self._claim: AgentMaintenanceClaim | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: Exception | None = None
        self._claim_lock = threading.Lock()

    @property
    def claim(self) -> AgentMaintenanceClaim:
        with self._claim_lock:
            if self._claim is None:
                raise RuntimeError("maintenance lease has not been acquired")
            return self._claim

    def __enter__(self) -> AgentVersionMaintenanceLease:
        claim = acquire_maintenance(
            self._coordinator.session_factory,
            agent_id=self._agent_id,
            kind=self._kind,
            owner_id=self._owner_id,
            lease_seconds=self._coordinator.lease_seconds,
        )
        with self._claim_lock:
            self._claim = claim
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"agent-maintenance:{self._agent_id}:{self._kind}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._coordinator.heartbeat_seconds + 1.0)
        claim = self.claim
        released = release_maintenance(self._coordinator.session_factory, claim)
        if exc is None:
            self.check()
            if not released:
                raise AgentMaintenanceClaimLost(f"Agent {claim.agent_id} maintenance claim was lost before release")
        return False

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def assert_active(self) -> None:
        self.check()
        assert_maintenance_claim_active(
            self._coordinator.session_factory,
            self.claim,
        )

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._coordinator.heartbeat_seconds):
            try:
                renewed = renew_maintenance(
                    self._coordinator.session_factory,
                    self.claim,
                    lease_seconds=self._coordinator.lease_seconds,
                )
                with self._claim_lock:
                    self._claim = renewed
            except Exception as exc:  # noqa: BLE001 - surfaced synchronously by check/exit
                self._failure = exc
                return


def is_agent_version_maintenance_active(
    *,
    session_factory: sessionmaker,
    agent_id: str,
    store_for: Callable[..., object] | None = None,
) -> bool:
    if is_maintenance_active(session_factory, agent_id=agent_id):
        return True
    # Conservative fallback for direct Git maintenance calls that have not entered an API workflow.
    if store_for is None:
        return False
    store = store_for(agent_id)
    probe = getattr(store, "is_maintenance_active", None)
    return bool(probe()) if callable(probe) else False
