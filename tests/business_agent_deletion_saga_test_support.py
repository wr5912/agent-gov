from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.agent_testing.models import AgentTestRunModel
from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.agent_deletion_fs import purge_quarantined_agent_layout, quarantine_agent_layout
from app.runtime.agent_maintenance_db import (
    AgentReleaseOperationModel,
    AgentWorkspaceActivationOperationModel,
    AgentWorktreeCleanupTaskModel,
)
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.runtime_db import (
    AgentChangeSetModel,
    Base,
    SessionRecordModel,
    SessionTurnIntentModel,
    utc_now,
)
from app.runtime.stores.agent_deletion_store import (
    AgentDeletionStore,
    business_agent_instance_etag,
)
from app.services.business_agent_deletion import BusinessAgentDeletionService
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass
class DeletionSagaHarness:
    data_dir: Path
    session_factory: sessionmaker
    store: AgentDeletionStore
    service: BusinessAgentDeletionService
    agent_id: str
    instance_token: str

    @property
    def layout_root(self) -> Path:
        return business_agent_layout(self.data_dir, self.agent_id).root

    def begin(self, *, key: str = "delete-key"):
        return self.store.begin(
            agent_id=self.agent_id,
            agent_instance_etag=business_agent_instance_etag(self.instance_token),
            idempotency_key=key,
        )

    def delete(self, *, key: str = "delete-key"):
        return self.service.delete(
            agent_id=self.agent_id,
            agent_instance_etag=business_agent_instance_etag(self.instance_token),
            idempotency_key=key,
        )


def build_deletion_saga_harness(tmp_path: Path) -> DeletionSagaHarness:
    data_dir = tmp_path / "data"
    db_path = data_dir / "runtime.sqlite3"
    db_path.parent.mkdir(parents=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    agent_id = "delete-probe"
    token = "2026-08-09T00:00:00+00:00"
    layout = business_agent_layout(data_dir, agent_id)
    layout.workspace.mkdir(parents=True)
    layout.workspace.joinpath("CLAUDE.md").write_text("private\n", encoding="utf-8")
    layout.claude_root.mkdir()
    layout.version_base.mkdir()
    with factory.begin() as db:
        db.add(
            AgentRegistryModel(
                agent_id=agent_id,
                name="Delete probe",
                category="business",
                workspace_dir=str(layout.workspace),
                created_at=token,
                status="active",
                provision_state="ready",
                provision_completed_token=token,
            )
        )
    store = AgentDeletionStore(factory, data_dir=data_dir)
    return DeletionSagaHarness(
        data_dir=data_dir,
        session_factory=factory,
        store=store,
        service=BusinessAgentDeletionService(store, data_dir=data_dir),
        agent_id=agent_id,
        instance_token=token,
    )


def waiting_hitl(agent_id: str) -> ClaudeUserInputRequestModel:
    return ClaudeUserInputRequestModel(
        request_id="hitl",
        decision_token_hash="hash",
        business_agent_id=agent_id,
        run_id="run",
        api_session_id="session",
        request_type="permission",
        tool_name="Bash",
        status="waiting",
        expires_at="2099-01-01T00:00:00+00:00",
    )


def active_test(agent_id: str) -> AgentTestRunModel:
    return AgentTestRunModel(
        test_run_id="test-run",
        agent_id=agent_id,
        commit_sha="a" * 40,
        source="manual",
        status="queued",
        created_at=utc_now(),
    )


def active_change_set(harness: DeletionSagaHarness) -> AgentChangeSetModel:
    return AgentChangeSetModel(
        change_set_id="change",
        agent_id=harness.agent_id,
        created_at=utc_now(),
        updated_at=utc_now(),
        status="draft",
        base_commit_sha="a" * 40,
        branch_name="change",
        worktree_path=str(harness.data_dir / "worktree"),
    )


def active_release(agent_id: str, *, status: str = "reserved") -> AgentReleaseOperationModel:
    return AgentReleaseOperationModel(
        operation_id="release-operation",
        agent_id=agent_id,
        release_id="release",
        operation_kind="publish",
        status=status,
        expected_head_sha="a" * 40,
        target_commit_sha="b" * 40,
        release_expected_status="published",
        release_expected_updated_at=utc_now(),
        operator="test",
    )


def pending_cleanup(agent_id: str) -> AgentWorktreeCleanupTaskModel:
    return AgentWorktreeCleanupTaskModel(
        change_set_id="cleanup-change",
        agent_id=agent_id,
        status="pending",
    )


def active_activation(harness: DeletionSagaHarness) -> AgentWorkspaceActivationOperationModel:
    return AgentWorkspaceActivationOperationModel(
        operation_id="activation",
        agent_id=harness.agent_id,
        action="restore",
        state="preparing",
        original_head_sha="a" * 40,
        original_index_fingerprint="b" * 64,
        original_workspace_fingerprint="c" * 64,
        recovery_phase="none",
        maintenance_token="claim",
        maintenance_generation=1,
        maintenance_expires_at="2099-01-01T00:00:00+00:00",
    )


def complete_without_witness_cleanup(harness: DeletionSagaHarness, *, key: str):
    operation = harness.begin(key=key)
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    purged = purge_quarantined_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert purged.state == "completed"
    operation = harness.store.confirm_purge(operation.operation_id)
    return harness.store.complete(operation.operation_id)


def operation_row(*, index: int, state: str) -> AgentDeletionOperationModel:
    operation_id = f"fair-{state}-{index}"
    completed = state == "completed"
    timestamp = f"2026-01-0{index + 1}T00:00:00+00:00"
    return AgentDeletionOperationModel(
        operation_id=operation_id,
        idempotency_key=f"fair-key-{state}-{index}",
        agent_id=f"fair-agent-{state}-{index}",
        agent_instance_etag=f"fair-instance-{index}",
        state=state,
        workspace_path=f"/data/business-agents/fair-{index}",
        expected_device=1,
        expected_inode=2 + index,
        expected_mount_id=3,
        quarantine_path=f"/data/.agent-deletion-quarantine/{operation_id}",
        quarantine_confirmed=completed,
        purge_confirmed=completed,
        witness_removed=False,
        deleted_json={},
        impact_json={},
        error_json={},
        attempt_count=0,
        created_at=timestamp,
        updated_at=timestamp,
        completed_at=timestamp if completed else None,
    )


def add_running_turn_intent(db: Session, agent_id: str) -> None:
    db.add(SessionRecordModel(session_id="intent-session", agent_id=agent_id))
    db.add(
        SessionTurnIntentModel(
            run_id="intent-run",
            session_id="intent-session",
            agent_id=agent_id,
            attempted_sdk_session_id="sdk-session",
            sdk_project_key="project",
            base_turns=0,
            status="running",
        )
    )
