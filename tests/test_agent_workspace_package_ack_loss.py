from __future__ import annotations

from pathlib import Path

from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import is_maintenance_active
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSession
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_workspace_git_operations import run_git
from fastapi.testclient import TestClient
from sqlalchemy import select

from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import (
    import_new_agent as _import_new_agent,
)
from workspace_package_test_utils import (
    run_git as _run_git,
)
from workspace_package_test_utils import (
    workspace_package as _workspace_package,
)


def _activation_operation(module, agent_id: str) -> AgentWorkspaceActivationOperationModel:
    with module.workspace_activation_service._Session() as db:
        operation = db.scalar(select(AgentWorkspaceActivationOperationModel).where(AgentWorkspaceActivationOperationModel.agent_id == agent_id))
        assert operation is not None
        db.expunge(operation)
        return operation


def _assert_no_activation_refs(workspace: Path) -> None:
    assert _run_git(workspace, "for-each-ref", "--format=%(refname)", "refs/agentgov/workspace-activations") == ""


def test_create_import_returns_committed_result_when_finalize_commit_ack_is_lost(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "create-commit-ack-loss"
    package = _workspace_package(
        {
            "CLAUDE.md": b"# durable create\n",
            ".mcp.json": b'{"mcpServers": {}}\n',
            ".claude/settings.json": b'{"permissions":{"ask":[]}}\n',
        },
        agent_id=agent_id,
    )
    session_class = module.agent_registry_store._session_factory.class_
    original_commit = session_class.commit
    committed_token: str | None = None

    def commit_then_lose_ack(db_session) -> None:
        nonlocal committed_token
        row = next(
            (
                item
                for item in db_session.identity_map.values()
                if isinstance(item, AgentRegistryModel) and item.agent_id == agent_id and item.provision_state == "ready" and item.provision_completed_token
            ),
            None,
        )
        original_commit(db_session)
        if row is not None and committed_token is None:
            committed_token = str(row.provision_completed_token)
            raise RuntimeError("injected create commit acknowledgement loss")

    with TestClient(module.app) as client:
        monkeypatch.setattr(session_class, "commit", commit_then_lose_ack)
        response = _import_new_agent(client, agent_id=agent_id, name="create ack loss", package=package)
        public_agents = client.get("/api/agent-registry").json()

    assert response.status_code == 200
    body = response.json()
    workspace = Path(body["agent"]["workspace_dir"])
    assert committed_token is not None
    assert not workspace.is_symlink() and workspace.is_dir()
    assert workspace.joinpath(".git").is_dir() and not workspace.joinpath(".git").is_symlink()
    version_base = workspace.parent / "version"
    assert not version_base.is_symlink() and version_base.is_dir()
    assert version_base.joinpath("worktrees").is_dir()
    assert version_base.joinpath("releases").is_dir()
    assert _run_git(workspace, "rev-parse", "HEAD") == body["current_commit_sha"]
    entries = package_codec.read_commit_entries(workspace, body["current_commit_sha"], run_git=run_git)
    assert package_codec.tree_sha256(entries) == body["tree_sha256"]
    assert any(item["agent_id"] == agent_id for item in public_agents)
    profile = module.runtime._resolve_runtime_profile(ChatRequest(message="probe", agent_id=agent_id), None)
    assert profile.workspace_dir == workspace
    with module.agent_registry_store._session_factory() as db:
        row = db.get(AgentRegistryModel, agent_id)
        audit = db.get(AgentWorkspaceImportRecordModel, body["import_record_id"])
        assert row is not None
        assert row.provision_state == "ready" and row.provision_token is None
        assert row.provision_completed_token == committed_token
        assert audit is not None
        assert (audit.action, audit.status, audit.commit_sha) == ("created", "accepted", body["current_commit_sha"])
        assert (audit.package_sha256, audit.tree_sha256) == (body["package_sha256"], body["tree_sha256"])
        assert audit.suite_status == body["test_suite_status"]
        assert audit.diagnostics_json == body["test_suite_diagnostics"]


def test_overwrite_returns_typed_success_after_terminal_commit_ack_loss(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "overwrite-commit-ack-loss"
    acknowledged = False
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=agent_id, name="overwrite ack loss")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = _run_git(workspace, "rev-parse", "HEAD")
        session = LocalSession(
            session_id=f"{agent_id}-session",
            sdk_session_id=f"{agent_id}-sdk",
            agent_id=agent_id,
            turns=1,
        )
        module.session_store.save(session)
        original_finalize = module.workspace_activation_service._finalize_completion

        def finalize_then_lose_ack(operation_id: str) -> None:
            nonlocal acknowledged
            original_finalize(operation_id)
            if not acknowledged:
                acknowledged = True
                raise RuntimeError("injected overwrite terminal commit acknowledgement loss")

        monkeypatch.setattr(module.workspace_activation_service, "_finalize_completion", finalize_then_lose_ack)
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package({"CLAUDE.md": b"# committed replacement\n"}, agent_id=agent_id),
                    "application/gzip",
                )
            },
        )

    assert acknowledged and response.status_code == 200
    body = response.json()
    operation = _activation_operation(module, agent_id)
    saved = module.session_store.get(session.session_id)
    assert body["previous_commit_sha"] == baseline
    assert body["current_commit_sha"] != baseline
    assert _run_git(workspace, "rev-parse", "HEAD") == body["current_commit_sha"]
    assert operation.state == "completed" and operation.candidate_commit_sha == body["current_commit_sha"]
    assert saved is not None and saved.sdk_session_id is None
    assert not is_maintenance_active(module.workspace_activation_service._Session, agent_id=agent_id)
    _assert_no_activation_refs(workspace)
    with module.agent_testing_store.Session() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, body["import_record_id"])
        assert audit is not None
        assert (audit.action, audit.status, audit.commit_sha) == ("overwritten", "accepted", body["current_commit_sha"])


def test_restore_returns_success_after_terminal_commit_ack_loss(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "restore-commit-ack-loss"
    acknowledged = False
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=agent_id, name="restore ack loss")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = _run_git(workspace, "rev-parse", "HEAD")
        workspace.joinpath("CLAUDE.md").write_text("# historical target\n", encoding="utf-8")
        _run_git(workspace, "add", "--", "CLAUDE.md")
        _run_git(workspace, "commit", "-m", "Historical target")
        target = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "reset", "--hard", baseline)
        session = LocalSession(
            session_id=f"{agent_id}-session",
            sdk_session_id=f"{agent_id}-sdk",
            agent_id=agent_id,
            turns=1,
        )
        module.session_store.save(session)
        original_finalize = module.workspace_activation_service._finalize_completion

        def finalize_then_lose_ack(operation_id: str) -> None:
            nonlocal acknowledged
            original_finalize(operation_id)
            if not acknowledged:
                acknowledged = True
                raise RuntimeError("injected restore terminal commit acknowledgement loss")

        monkeypatch.setattr(module.workspace_activation_service, "_finalize_completion", finalize_then_lose_ack)
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/restore",
            json={
                "expected_current_commit_sha": baseline,
                "target_commit_sha": target,
                "reason": "restore after acknowledgement loss",
            },
        )

    assert acknowledged and response.status_code == 200
    body = response.json()
    operation = _activation_operation(module, agent_id)
    saved = module.session_store.get(session.session_id)
    assert body["previous_commit_sha"] == baseline and body["restored_tree_commit_sha"] == target
    assert _run_git(workspace, "rev-parse", "HEAD") == body["current_commit_sha"]
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "# historical target\n"
    assert operation.state == "completed" and operation.action == "restore"
    assert saved is not None and saved.sdk_session_id is None
    assert not is_maintenance_active(module.workspace_activation_service._Session, agent_id=agent_id)
    _assert_no_activation_refs(workspace)
