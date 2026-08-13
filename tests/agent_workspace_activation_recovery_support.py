from __future__ import annotations

import io
import subprocess
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path

import app.services.agent_workspace_git_operations as git_operations
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import is_maintenance_active
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_version_maintenance import AgentVersionMaintenanceLease
from fastapi.testclient import TestClient
from sqlalchemy import select


@dataclass(frozen=True)
class PreparedActivation:
    operation_id: str
    import_id: str
    agent_id: str
    workspace: Path
    original_head: str
    base_commit: str
    candidate_commit: str
    snapshot: git_operations.SnapshotState
    lease: AgentVersionMaintenanceLease
    store: GitAgentVersionStore


@dataclass(frozen=True)
class PreparingActivation:
    operation_id: str
    agent_id: str
    workspace: Path
    lease: AgentVersionMaintenanceLease


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _workspace_package(files: dict[str, bytes], *, agent_id: str) -> bytes:
    package_files = dict(files)
    package_files.setdefault("agent.yaml", f"agent:\n  id: {agent_id}\n".encode())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for path, content in sorted(package_files.items()):
            member = tarfile.TarInfo(f"workspace/{path}")
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def import_new_agent(client: TestClient, *, agent_id: str, name: str):
    content = _workspace_package(
        {
            "CLAUDE.md": f"# {name}\n".encode(),
            ".mcp.json": b'{"mcpServers": {}}\n',
            ".claude/settings.json": b'{"permissions":{"ask":["Bash(*)"]}}\n',
        },
        agent_id=agent_id,
    )
    return client.post(
        f"/api/agent-registry/{agent_id}/workspace/import",
        data={"name": name},
        files={"package": (f"{agent_id}.tar.gz", content, "application/gzip")},
    )


def validated_package(tmp_path: Path, *, agent_id: str, content: bytes):
    package_path = tmp_path / f"{agent_id}.tar.gz"
    return package_codec.read_workspace_package(
        io.BytesIO(_workspace_package({"CLAUDE.md": content}, agent_id=agent_id)),
        package_path,
        filename=package_path.name,
    )


def _prepare_workspace_state(
    workspace: Path,
    *,
    dirty: bool,
    staged_partial: bool,
    index_flags: bool,
) -> None:
    if index_flags:
        git(workspace, "update-index", "--assume-unchanged", "--", "CLAUDE.md")
        git(workspace, "update-index", "--skip-worktree", "--", ".mcp.json")
    if dirty:
        workspace.joinpath("operator-dirty.txt").write_text("operator bytes\n", encoding="utf-8")
    if staged_partial:
        workspace.joinpath("CLAUDE.md").write_text("# staged bytes\n", encoding="utf-8")
        git(workspace, "add", "--", "CLAUDE.md")
        workspace.joinpath("CLAUDE.md").write_text("# unstaged bytes\n", encoding="utf-8")


def prepare_import_activation(
    module,
    client: TestClient,
    tmp_path: Path,
    *,
    agent_id: str,
    dirty: bool = False,
    staged_partial: bool = False,
    index_flags: bool = False,
) -> PreparedActivation:
    created = import_new_agent(client, agent_id=agent_id, name=agent_id)
    workspace = Path(created.json()["agent"]["workspace_dir"])
    original_head = git(workspace, "rev-parse", "HEAD")
    _prepare_workspace_state(
        workspace,
        dirty=dirty,
        staged_partial=staged_partial,
        index_flags=index_flags,
    )
    package = validated_package(
        tmp_path,
        agent_id=agent_id,
        content=f"# candidate {agent_id}\n".encode(),
    )
    lease = module.agent_governance.version_maintenance.lease(
        agent_id=agent_id,
        kind="workspace_import",
        owner_id=f"test:{agent_id}",
    )
    lease.__enter__()
    store = module.agent_governance._store_for(agent_id)
    with store.mutation_guard():
        observation = git_operations.observe_live_workspace(store, expected_head=original_head)
        preparation = module.workspace_activation_service.begin_import(
            agent_id=agent_id,
            observation=observation,
            claim=lease.claim,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
        )
        snapshot = git_operations.prepare_workspace_snapshot(
            store,
            observation=observation,
            operation_id=preparation.operation_id,
        )
        replacement = git_operations.replace_tree_from_entries(
            store,
            base_commit=snapshot.current_head,
            entries=package.entries,
            message="Prepare durable activation test candidate",
            operation_id=preparation.operation_id,
        )
        prepared_audit = module.agent_testing_service.prepare_import(
            agent_id=agent_id,
            action="overwritten",
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            commit_sha=replacement.current_commit_sha,
        )
        prepared_audit = replace(prepared_audit, import_id=preparation.import_id)
        module.workspace_activation_service.prepare_import(
            preparation.operation_id,
            snapshot=snapshot,
            replacement=replacement,
            prepared_audit=prepared_audit,
        )
    return PreparedActivation(
        operation_id=preparation.operation_id,
        import_id=prepared_audit.import_id,
        agent_id=agent_id,
        workspace=workspace,
        original_head=original_head,
        base_commit=snapshot.current_head,
        candidate_commit=replacement.current_commit_sha,
        snapshot=snapshot,
        lease=lease,
        store=store,
    )


def begin_import_activation_intent(
    module,
    client: TestClient,
    *,
    agent_id: str,
) -> PreparingActivation:
    created = import_new_agent(client, agent_id=agent_id, name=agent_id)
    workspace = Path(created.json()["agent"]["workspace_dir"])
    lease = module.agent_governance.version_maintenance.lease(
        agent_id=agent_id,
        kind="workspace_import",
        owner_id=f"test:{agent_id}",
    )
    lease.__enter__()
    store = module.agent_governance._store_for(agent_id)
    with store.mutation_guard():
        observation = git_operations.observe_live_workspace(store)
        preparation = module.workspace_activation_service.begin_import(
            agent_id=agent_id,
            observation=observation,
            claim=lease.claim,
            package_sha256="a" * 64,
            tree_sha256="b" * 64,
        )
    return PreparingActivation(
        operation_id=preparation.operation_id,
        agent_id=agent_id,
        workspace=workspace,
        lease=lease,
    )


def activate_git_only(prepared: PreparedActivation) -> None:
    with prepared.store.workspace_activation_guard():
        git_operations.activate_candidate(
            prepared.store,
            snapshot=prepared.snapshot,
            candidate_commit=prepared.candidate_commit,
            operation_id=prepared.operation_id,
            before_activate=lambda: None,
        )


def operation(module, operation_id: str) -> AgentWorkspaceActivationOperationModel:
    with module.workspace_activation_service._Session() as db:
        row = db.get(AgentWorkspaceActivationOperationModel, operation_id)
        assert row is not None
        db.expunge(row)
        return row


def authority_projection(module, prepared: PreparedActivation) -> tuple[object, ...]:
    with module.agent_testing_store.Session() as db:
        audits = tuple(
            (
                row.import_id,
                row.status,
                row.commit_sha,
                dict(row.error_json or {}),
            )
            for row in db.scalars(
                select(AgentWorkspaceImportRecordModel)
                .where(AgentWorkspaceImportRecordModel.agent_id == prepared.agent_id)
                .order_by(AgentWorkspaceImportRecordModel.import_id)
            ).all()
        )
    refs = git(
        prepared.workspace,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        f"refs/agentgov/workspace-activations/{prepared.operation_id}",
    )
    activation = operation(module, prepared.operation_id)
    return (
        git(prepared.workspace, "rev-parse", "HEAD"),
        git_operations.workspace_status(prepared.workspace),
        git_operations.index_fingerprint(prepared.workspace),
        git_operations.index_snapshot(prepared.workspace),
        git_operations.workspace_fingerprint(prepared.workspace),
        audits,
        refs,
        activation.state,
        activation.recovery_phase,
        is_maintenance_active(module.workspace_activation_service._Session, agent_id=prepared.agent_id),
    )
