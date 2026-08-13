from __future__ import annotations

import re
import shlex
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import (
    AgentAdmissionStateModel,
    AgentWorkspaceActivationOperationModel,
)
from app.runtime.agent_paths import business_agent_repository_lock_path
from app.runtime.runtime_db_base import Base, utc_now
from app.runtime.runtime_db_migrations_0057 import (
    migrate_0057_workspace_activation_operator_recovery,
)
from app.runtime.workspace_activation_recovery import (
    RecoveryAttemptRequest,
    WorkspaceActivationRecoveryAttemptStore,
)
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_activation import WorkspaceActivationService
from app.services.agent_workspace_activation_recovery import (
    RecoveryOperatorContext,
    WorkspaceActivationOperatorRecoveryService,
)
from app.services.agent_workspace_git_evidence import index_fingerprint
from app.services.agent_workspace_git_operations import (
    run_git,
    workspace_fingerprint,
    workspace_status,
)
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import sessionmaker

CONCURRENCY_TIMEOUT_SECONDS = 30.0
_DIGEST = "sha256:" + "a" * 64


@dataclass(frozen=True)
class ColumnSignature:
    name: str
    type: str
    not_null: int
    default: str | None
    primary_key: int


@dataclass(frozen=True)
class IndexSignature:
    name: str
    unique: int
    origin: str
    partial: int
    columns: tuple[str, ...]
    predicate: str


@dataclass(frozen=True)
class TriggerSignature:
    name: str
    sql: str


@dataclass(frozen=True)
class ForeignKeySignature:
    referenced_table: str
    source_column: str
    target_column: str
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True)
class RecoverySchemaSignature:
    columns: tuple[ColumnSignature, ...]
    indexes: tuple[IndexSignature, ...]
    triggers: tuple[TriggerSignature, ...]
    foreign_keys: tuple[ForeignKeySignature, ...]


def recovery_schema_signature(connection: Connection) -> RecoverySchemaSignature:
    table = "agent_workspace_activation_recovery_attempts"
    columns = tuple(
        ColumnSignature(
            name=str(row[1]),
            type=str(row[2]),
            not_null=int(row[3]),
            default=str(row[4]) if row[4] is not None else None,
            primary_key=int(row[5]),
        )
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
    )
    indexes = tuple(_index_signature(connection, table=table, row=row) for row in connection.exec_driver_sql(f"PRAGMA index_list({table})"))
    triggers = tuple(
        TriggerSignature(name=str(row[0]), sql=_normalized_sql(str(row[1])))
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
            (table,),
        )
    )
    foreign_keys = tuple(
        ForeignKeySignature(
            referenced_table=str(row[2]),
            source_column=str(row[3]),
            target_column=str(row[4]),
            on_update=str(row[5]),
            on_delete=str(row[6]),
            match=str(row[7]),
        )
        for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({table})")
    )
    return RecoverySchemaSignature(columns, indexes, triggers, foreign_keys)


def _index_signature(
    connection: Connection,
    *,
    table: str,
    row: tuple[object, ...],
) -> IndexSignature:
    name = str(row[1])
    columns = tuple(str(item[2]) for item in connection.exec_driver_sql(f'PRAGMA index_info("{name}")'))
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).scalar_one_or_none()
    normalized = _normalized_sql(str(sql)) if sql else ""
    predicate = normalized.partition(" where ")[2]
    return IndexSignature(
        name=name,
        unique=int(row[2]),
        origin=str(row[3]),
        partial=int(row[4]),
        columns=columns,
        predicate=predicate,
    )


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def git(repository: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    return process.stdout.strip()


@dataclass
class RecoveryHarness:
    Session: sessionmaker
    data_dir: Path
    workspace: Path
    operation_id: str
    store: GitAgentVersionStore
    service: WorkspaceActivationOperatorRecoveryService
    exact_calls: list[RecoveryOperatorContext]


@dataclass(frozen=True)
class ResumeRaceOutcome:
    apply_state: str
    resume_result: str
    exact_call_count: int
    attempt_state: str


@dataclass(frozen=True)
class RecoveryRepositorySeed:
    workspace: Path
    operation_id: str
    original: str
    original_tree: str
    original_status: str
    original_index: str
    original_workspace: str
    original_index_bytes: bytes
    candidate: str
    candidate_tree: str
    package_tree_sha: str


def _seed_recovery_repository(data_dir: Path) -> RecoveryRepositorySeed:
    workspace = data_dir / "business-agents" / "agent-a" / "workspace"
    workspace.mkdir(parents=True)
    git(workspace, "init")
    git(workspace, "config", "user.name", "AgentGov Test")
    git(workspace, "config", "user.email", "agentgov@example.local")
    workspace.joinpath("CLAUDE.md").write_text("base\n", encoding="utf-8")
    nested = workspace / "nested-agent"
    nested.mkdir()
    nested.joinpath("nested.txt").write_text("nested-base\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "base")
    git(nested, "init")
    git(nested, "config", "user.name", "Nested Agent Test")
    git(nested, "config", "user.email", "nested-agent@example.local")
    git(nested, "add", "-A")
    git(nested, "commit", "-m", "nested base")
    original = git(workspace, "rev-parse", "HEAD")
    original_tree = git(workspace, "rev-parse", "HEAD^{tree}")
    original_status = workspace_status(workspace)
    original_index = index_fingerprint(workspace)
    original_workspace = workspace_fingerprint(workspace)
    original_index_bytes = workspace.joinpath(".git", "index").read_bytes()
    workspace.joinpath("CLAUDE.md").write_text("candidate\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "candidate")
    candidate = git(workspace, "rev-parse", "HEAD")
    candidate_tree = git(workspace, "rev-parse", "HEAD^{tree}")
    package_tree_sha = package_codec.tree_sha256(package_codec.read_commit_entries(workspace, candidate, run_git=run_git))
    operation_id = f"wao-{uuid.uuid4()}"
    refs = {
        "original": original,
        "base": original,
        "candidate": candidate,
        "original-index-tree": original_tree,
    }
    for name, object_sha in refs.items():
        git(
            workspace,
            "update-ref",
            f"refs/agentgov/workspace-activations/{operation_id}/{name}",
            object_sha,
        )
    return RecoveryRepositorySeed(
        workspace=workspace,
        operation_id=operation_id,
        original=original,
        original_tree=original_tree,
        original_status=original_status,
        original_index=original_index,
        original_workspace=original_workspace,
        original_index_bytes=original_index_bytes,
        candidate=candidate,
        candidate_tree=candidate_tree,
        package_tree_sha=package_tree_sha,
    )


def _seed_recovery_database(
    data_dir: Path,
    seed: RecoveryRepositorySeed,
) -> sessionmaker:
    engine = create_engine(f"sqlite:///{data_dir / 'runtime.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        migrate_0057_workspace_activation_operator_recovery(connection)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    now = utc_now()
    with Session.begin() as db:
        db.add(
            AgentWorkspaceActivationOperationModel(
                operation_id=seed.operation_id,
                import_id=f"awi-{uuid.uuid4()}",
                agent_id="agent-a",
                action="import_overwrite",
                state="recovery_required",
                original_head_sha=seed.original,
                base_commit_sha=seed.original,
                candidate_commit_sha=seed.candidate,
                candidate_tree_sha=seed.candidate_tree,
                target_commit_sha=None,
                snapshot_created=False,
                original_status_text=seed.original_status + "secret-status-entry",
                original_index_fingerprint=seed.original_index,
                original_workspace_fingerprint=seed.original_workspace,
                original_index_tree_sha=seed.original_tree,
                original_index_snapshot=seed.original_index_bytes,
                recovery_phase="none",
                package_sha256="1" * 64,
                tree_sha256=seed.package_tree_sha,
                suite_status="passed",
                suite_json={"secret": "suite-secret"},
                diagnostics_json=[{"secret": "diagnostic-secret"}],
                maintenance_token="maintenance-super-secret",
                maintenance_generation=1,
                maintenance_expires_at=now,
                error_json={"detail": "error-secret"},
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
        db.add(
            AgentAdmissionStateModel(
                agent_id="agent-a",
                generation=1,
                maintenance_token="maintenance-super-secret",
                maintenance_generation=1,
                maintenance_kind="workspace_import",
                maintenance_owner_id=seed.operation_id,
                maintenance_expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return Session


def _build_recovery_service(
    data_dir: Path,
    seed: RecoveryRepositorySeed,
    Session: sessionmaker,
) -> tuple[
    GitAgentVersionStore,
    WorkspaceActivationOperatorRecoveryService,
    list[RecoveryOperatorContext],
]:
    worktrees = data_dir / "business-agents" / "agent-a" / "version" / "worktrees"
    releases = data_dir / "business-agents" / "agent-a" / "version" / "releases"
    worktrees.mkdir(parents=True)
    releases.mkdir(parents=True)
    store = GitAgentVersionStore(
        repository_dir=seed.workspace,
        worktrees_dir=worktrees,
        releases_dir=releases,
        process_lock_path=business_agent_repository_lock_path(data_dir, "agent-a"),
    )
    activation = WorkspaceActivationService(
        session_factory=Session,
        store_for=lambda _agent_id: store,
        invalidate_sessions=lambda _db, _agent_id: None,
        persist_accepted_import=AgentTestingStore.record_import_in_transaction,
    )
    exact_calls: list[RecoveryOperatorContext] = []

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        exact_calls.append(context)
        return activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    service = WorkspaceActivationOperatorRecoveryService(
        session_factory=Session,
        data_dir=data_dir,
        store_for=lambda _agent_id: store,
        reconcile_exact=reconcile_exact,
    )
    return store, service, exact_calls


def build_recovery_harness(tmp_path: Path) -> RecoveryHarness:
    data_dir = tmp_path / "data"
    seed = _seed_recovery_repository(data_dir)
    Session = _seed_recovery_database(data_dir, seed)
    store, service, exact_calls = _build_recovery_service(data_dir, seed, Session)
    return RecoveryHarness(
        Session=Session,
        data_dir=data_dir,
        workspace=seed.workspace,
        operation_id=seed.operation_id,
        store=store,
        service=service,
        exact_calls=exact_calls,
    )


def recovery_request(
    recovery_harness: RecoveryHarness,
    *,
    action: str = "reconcile",
    digest: str | None = None,
    recovery_id: str | None = None,
) -> RecoveryAttemptRequest:
    inspection = recovery_harness.service.inspector.inspect(
        recovery_harness.operation_id,
    )
    return RecoveryAttemptRequest(
        recovery_id=recovery_id or f"war-{uuid.uuid4()}",
        operation_id=recovery_harness.operation_id,
        action=action,  # type: ignore[arg-type]
        state_digest=digest or inspection.state_digest,
        operator="operator-a",
        reason="confirmed exact durable evidence",
    )


def recovery_apply_cli_args(request: RecoveryAttemptRequest) -> list[str]:
    return [
        "apply",
        "--operation-id",
        request.operation_id,
        "--recovery-id",
        request.recovery_id,
        "--state-digest",
        request.state_digest,
        "--operator",
        request.operator,
        "--reason",
        request.reason,
    ]


def activation_service(
    recovery_harness: RecoveryHarness,
) -> WorkspaceActivationService:
    return WorkspaceActivationService(
        session_factory=recovery_harness.Session,
        store_for=lambda _agent_id: recovery_harness.store,
        invalidate_sessions=lambda _db, _agent_id: None,
        persist_accepted_import=AgentTestingStore.record_import_in_transaction,
    )


def activation_authority(recovery_harness: RecoveryHarness) -> tuple[object, ...]:
    with recovery_harness.Session() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None
        operation_count = db.query(AgentWorkspaceActivationOperationModel).count()
        state = operation.state
    refs = git(
        recovery_harness.workspace,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        f"refs/agentgov/workspace-activations/{recovery_harness.operation_id}",
    )
    return (
        git(recovery_harness.workspace, "rev-parse", "HEAD"),
        workspace_status(recovery_harness.workspace),
        index_fingerprint(recovery_harness.workspace),
        workspace_fingerprint(recovery_harness.workspace),
        refs,
        operation_count,
        state,
    )


def convert_to_restore_recovery(recovery_harness: RecoveryHarness) -> None:
    with recovery_harness.Session.begin() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        admission = db.get(AgentAdmissionStateModel, "agent-a")
        assert operation is not None and admission is not None
        operation.action = "restore"
        operation.import_id = None
        operation.package_sha256 = None
        operation.tree_sha256 = None
        operation.target_commit_sha = operation.candidate_commit_sha
        operation.suite_status = None
        operation.suite_json = {}
        operation.diagnostics_json = []
        admission.maintenance_kind = "workspace_restore"
        target_commit = str(operation.target_commit_sha)
    git(
        recovery_harness.workspace,
        "update-ref",
        f"refs/agentgov/workspace-activations/{recovery_harness.operation_id}/target",
        target_commit,
    )


def reset_to_original_evidence(recovery_harness: RecoveryHarness) -> None:
    with recovery_harness.Session() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None
        original = operation.original_head_sha
    git(recovery_harness.workspace, "reset", "--hard", original)
    status = workspace_status(recovery_harness.workspace)
    index_digest = index_fingerprint(recovery_harness.workspace)
    workspace_digest = workspace_fingerprint(recovery_harness.workspace)
    index_snapshot = recovery_harness.workspace.joinpath(".git", "index").read_bytes()
    with recovery_harness.Session.begin() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None
        operation.original_status_text = status
        operation.original_index_fingerprint = index_digest
        operation.original_workspace_fingerprint = workspace_digest
        operation.original_index_snapshot = index_snapshot


def remove_activation_refs(recovery_harness: RecoveryHarness) -> None:
    prefix = f"refs/agentgov/workspace-activations/{recovery_harness.operation_id}"
    rows = git(
        recovery_harness.workspace,
        "for-each-ref",
        "--format=%(refname)",
        prefix,
    )
    for ref_name in rows.splitlines():
        git(recovery_harness.workspace, "update-ref", "-d", ref_name)


def stage_preparing_recovery(recovery_harness: RecoveryHarness) -> None:
    reset_to_original_evidence(recovery_harness)
    remove_activation_refs(recovery_harness)
    with recovery_harness.Session.begin() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None
        operation.state = "preparing"
        operation.recovery_phase = "none"
        operation.base_commit_sha = None
        operation.candidate_commit_sha = None
        operation.candidate_tree_sha = None
        operation.target_commit_sha = None
        operation.original_index_tree_sha = None
        operation.snapshot_created = False


def stage_restore_outcome(
    recovery_harness: RecoveryHarness,
    *,
    target: str,
) -> None:
    convert_to_restore_recovery(recovery_harness)
    if target == "rejection_outcome":
        reset_to_original_evidence(recovery_harness)
    with recovery_harness.Session.begin() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None
        operation.state = "recovery_required"
        operation.recovery_phase = target
        operation.error_json = {"error_code": "STAGED_REJECTION", "detail": "resume exact rejection"} if target == "rejection_outcome" else {}


def exact_operator_service(
    recovery_harness: RecoveryHarness,
) -> WorkspaceActivationOperatorRecoveryService:
    activation = activation_service(recovery_harness)

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        return activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    return WorkspaceActivationOperatorRecoveryService(
        session_factory=recovery_harness.Session,
        data_dir=recovery_harness.data_dir,
        store_for=lambda _agent_id: recovery_harness.store,
        reconcile_exact=reconcile_exact,
    )


def run_apply_resume_race(
    recovery_harness: RecoveryHarness,
) -> ResumeRaceOutcome:
    entered = Event()
    release = Event()
    exact_calls = 0
    activation = activation_service(recovery_harness)

    def reconcile_exact(context: RecoveryOperatorContext) -> str:
        nonlocal exact_calls
        exact_calls += 1
        entered.set()
        if not release.wait(CONCURRENCY_TIMEOUT_SECONDS):
            raise RuntimeError("resume race did not release exact reconciliation")
        return activation.reconcile_exact_operation(
            context.operation_id,
            recovery_attempt_id=context.recovery_id,
            expected_state_digest=context.expected_state_digest,
        )

    operator = WorkspaceActivationOperatorRecoveryService(
        session_factory=recovery_harness.Session,
        data_dir=recovery_harness.data_dir,
        store_for=lambda _agent_id: recovery_harness.store,
        reconcile_exact=reconcile_exact,
    )
    request = recovery_request(recovery_harness)
    with ThreadPoolExecutor(max_workers=2) as executor:
        applying = executor.submit(operator.apply, request)
        try:
            if not entered.wait(CONCURRENCY_TIMEOUT_SECONDS):
                raise RuntimeError("apply did not enter exact reconciliation")
            resuming = executor.submit(operator.resume, request.recovery_id)
            release.set()
            apply_result = applying.result(timeout=CONCURRENCY_TIMEOUT_SECONDS)
            try:
                resume_result = str(resuming.result(timeout=CONCURRENCY_TIMEOUT_SECONDS)["state"])
            except Exception as exc:  # noqa: BLE001 - projected below for the race assertion.
                resume_result = str(getattr(exc, "code", exc.__class__.__name__))
        finally:
            release.set()
    attempt = WorkspaceActivationRecoveryAttemptStore(recovery_harness.Session).get(
        request.recovery_id,
    )
    assert attempt is not None
    return ResumeRaceOutcome(
        apply_state=str(apply_result["state"]),
        resume_result=resume_result,
        exact_call_count=exact_calls,
        attempt_state=attempt.state,
    )


def add_accepted_import_audit(recovery_harness: RecoveryHarness) -> None:
    with recovery_harness.Session.begin() as db:
        operation = db.get(
            AgentWorkspaceActivationOperationModel,
            recovery_harness.operation_id,
        )
        assert operation is not None and operation.import_id is not None
        db.add(
            AgentWorkspaceImportRecordModel(
                import_id=operation.import_id,
                agent_id=operation.agent_id,
                action=("unchanged" if operation.action == "import_unchanged" else "overwritten"),
                status="accepted",
                package_sha256=operation.package_sha256,
                tree_sha256=operation.tree_sha256,
                commit_sha=operation.candidate_commit_sha,
                created_at=operation.created_at,
                completed_at=utc_now(),
                suite_json=dict(operation.suite_json or {}),
                suite_status=operation.suite_status,
                diagnostics_json=list(operation.diagnostics_json or []),
                warnings_json=[],
                error_json={},
            )
        )


def inspect_missing_workspace(tmp_path: Path) -> tuple[dict[str, object], Path]:
    data_dir = tmp_path / "missing-data"
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        migrate_0057_workspace_activation_operator_recovery(connection)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    operation_id = f"wao-{uuid.uuid4()}"
    now = utc_now()
    with Session.begin() as db:
        db.add(
            AgentWorkspaceActivationOperationModel(
                operation_id=operation_id,
                import_id=None,
                agent_id="missing-agent",
                action="restore",
                state="recovery_required",
                original_head_sha="1" * 40,
                base_commit_sha="1" * 40,
                candidate_commit_sha="2" * 40,
                candidate_tree_sha="3" * 40,
                target_commit_sha="4" * 40,
                snapshot_created=False,
                original_status_text="",
                original_index_fingerprint="5" * 64,
                original_workspace_fingerprint="6" * 64,
                original_index_tree_sha="7" * 40,
                original_index_snapshot=b"index",
                recovery_phase="none",
                package_sha256=None,
                tree_sha256=None,
                suite_status=None,
                suite_json={},
                diagnostics_json=[],
                maintenance_token="token",
                maintenance_generation=1,
                maintenance_expires_at=now,
                error_json={},
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
    service = WorkspaceActivationOperatorRecoveryService(
        session_factory=Session,
        data_dir=data_dir,
    )
    return service.inspect(operation_id), data_dir


@dataclass(frozen=True)
class HostileGitConfiguration:
    markers: tuple[Path, ...]
    private_value: str
    private_path: Path


def install_hostile_git_configuration(
    recovery_harness: RecoveryHarness,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> HostileGitConfiguration:
    hostile_root = root / "private-git-config"
    hostile_root.mkdir()
    scripts = {
        name: _write_marker_script(hostile_root, name)
        for name in (
            "fsmonitor",
            "pager",
            "external-diff",
            "textconv",
            "hook",
            "lazy-fetch",
            "global",
            "system",
            "environment",
            "submodule-fsmonitor",
        )
    }
    repository = recovery_harness.workspace
    external_worktree = hostile_root / "external-worktree"
    external_worktree.mkdir()
    external_worktree.joinpath("private.txt").write_text(
        "must-not-be-inspected\n",
        encoding="utf-8",
    )
    git(repository, "config", "core.worktree", str(external_worktree))
    git(repository, "config", "core.fsmonitor", str(scripts["fsmonitor"][0]))
    git(
        repository / "nested-agent",
        "config",
        "core.fsmonitor",
        str(scripts["submodule-fsmonitor"][0]),
    )
    git(repository, "config", "core.pager", str(scripts["pager"][0]))
    git(repository, "config", "pager.status", str(scripts["pager"][0]))
    git(repository, "config", "diff.external", str(scripts["external-diff"][0]))
    git(repository, "config", "diff.hostile.textconv", str(scripts["textconv"][0]))
    repository.joinpath(".git", "info", "attributes").write_text(
        "* diff=hostile\n",
        encoding="utf-8",
    )
    hooks = hostile_root / "hooks"
    hooks.mkdir()
    hooks.joinpath("post-index-change").symlink_to(scripts["hook"][0])
    git(repository, "config", "core.hooksPath", str(hooks))
    git(repository, "config", "core.repositoryformatversion", "1")
    git(repository, "config", "remote.origin.url", f"ext::{scripts['lazy-fetch'][0]}")
    git(repository, "config", "remote.origin.promisor", "true")
    git(repository, "config", "remote.origin.partialclonefilter", "blob:none")
    git(repository, "config", "extensions.partialClone", "origin")
    global_config = _write_config(hostile_root, "global", scripts["global"][0])
    system_config = _write_config(hostile_root, "system", scripts["system"][0])
    private_value = "operator-private-environment-value"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(scripts["environment"][0]))
    monkeypatch.setenv("GIT_PAGER", str(scripts["environment"][0]))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(scripts["environment"][0]))
    monkeypatch.setenv("RECOVERY_PRIVATE_VALUE", private_value)
    return HostileGitConfiguration(
        markers=tuple(marker for _script, marker in scripts.values()),
        private_value=private_value,
        private_path=hostile_root,
    )


def _write_marker_script(root: Path, name: str) -> tuple[Path, Path]:
    marker = root / f"{name}.executed"
    script = root / f"{name}.sh"
    script.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nprintf '0\\n'\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script, marker


def _write_config(root: Path, name: str, marker_script: Path) -> Path:
    config = root / f"{name}.gitconfig"
    config.write_text(
        f"[core]\n\tfsmonitor = {marker_script.as_posix()}\n\tpager = {marker_script.as_posix()}\n",
        encoding="utf-8",
    )
    return config
