from __future__ import annotations

from pathlib import Path

from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import is_maintenance_active
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.agent_paths import business_agent_layout
from app.runtime.session_store import LocalSession
from app.services import agent_workspace_git_operations as workspace_git_operations
from app.services import agent_workspace_packages as workspace_package_service
from fastapi.testclient import TestClient
from sqlalchemy import select

from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import (
    import_new_agent as _import_new_agent,
)
from workspace_package_test_utils import (
    package_from_workspace as _package_from_workspace,
)
from workspace_package_test_utils import (
    run_git as _run_git,
)
from workspace_package_test_utils import (
    workspace_package as _workspace_package,
)


def test_new_agent_import_compensates_git_and_registry_when_finalize_fails(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module.agent_registry_store,
        "finalize_business_agent",
        lambda _reservation: (_ for _ in ()).throw(RuntimeError("injected finalize failure")),
    )
    package = _workspace_package(
        {"CLAUDE.md": b"# imported\n", ".mcp.json": b'{"mcpServers": {}}\n'},
        agent_id="finalize-failure",
    )
    with TestClient(module.app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/agent-registry/finalize-failure/workspace/import",
            data={"name": "failure"},
            files={"package": ("failure.tar.gz", package, "application/gzip")},
        )

    layout = business_agent_layout(module.settings.data_dir, "finalize-failure")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Workspace import provisioning failed and its owned partial state was removed.",
        "error_code": "WORKSPACE_IMPORT_PROVISIONING_FAILED",
    }
    assert module.agent_registry_store.get_agent("finalize-failure") is None
    assert not layout.workspace.exists()
    assert not layout.version_base.exists()
    with module.agent_testing_store.Session() as db:
        audits = list(db.scalars(select(AgentWorkspaceImportRecordModel).where(AgentWorkspaceImportRecordModel.agent_id == "finalize-failure")).all())
    assert len(audits) == 1 and audits[0].status == "failed"
    assert audits[0].error_json["error_code"] == "WORKSPACE_IMPORT_PROVISIONING_FAILED"


def test_new_agent_import_audit_write_failure_leaves_no_public_or_workspace_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    private_detail = "injected-private-create-audit-detail"

    def fail_accepted_audit(*_args, **_kwargs) -> None:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(
        module.agent_testing_store,
        "record_import_in_transaction",
        fail_accepted_audit,
    )
    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id="create-audit-failure",
            name="create audit failure",
        )

    layout = business_agent_layout(module.settings.data_dir, "create-audit-failure")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Workspace import could not be committed safely; no candidate was activated.",
        "error_code": "WORKSPACE_IMPORT_AUDIT_FAILED",
    }
    assert private_detail not in response.text
    assert module.agent_registry_store.get_agent("create-audit-failure") is None
    assert not layout.workspace.exists()
    assert not layout.version_base.exists()
    with module.agent_testing_store.Session() as db:
        records = list(db.scalars(select(AgentWorkspaceImportRecordModel).where(AgentWorkspaceImportRecordModel.agent_id == "create-audit-failure")).all())
    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].error_json["error_code"] == "WORKSPACE_IMPORT_AUDIT_FAILED"
    assert private_detail not in str(records[0].error_json)


def test_overwrite_persists_preparing_intent_before_git_configuration(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "intent-before-git-config"
    observed_operation_id: str | None = None
    original_configure = workspace_package_service._configure_raw_git_storage

    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=agent_id, name="intent before Git config")
        baseline = created.json()["current_commit_sha"]

        def configure_after_intent(repository: Path) -> None:
            nonlocal observed_operation_id
            with module.workspace_activation_service._Session() as db:
                operation = db.scalar(
                    select(AgentWorkspaceActivationOperationModel).where(
                        AgentWorkspaceActivationOperationModel.agent_id == agent_id,
                        AgentWorkspaceActivationOperationModel.state == "preparing",
                    )
                )
                assert operation is not None
                observed_operation_id = operation.operation_id
            original_configure(repository)
            raise workspace_git_operations.GitCommandError("injected failure after Git configuration")

        def forbid_bootstrap(*_args, **_kwargs) -> None:
            raise AssertionError("overwrite must not bootstrap Git before its durable intent")

        monkeypatch.setattr(workspace_package_service, "_configure_raw_git_storage", configure_after_intent)
        monkeypatch.setattr(GitAgentVersionStore, "ensure_bootstrap", forbid_bootstrap)
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package(
                        {"CLAUDE.md": b"# intent precedes Git mutation\n"},
                        agent_id=agent_id,
                    ),
                    "application/gzip",
                )
            },
        )

    assert observed_operation_id is not None
    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    with module.workspace_activation_service._Session() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, observed_operation_id)
        audit = db.get(
            AgentWorkspaceImportRecordModel,
            operation.import_id if operation else None,
        )
        assert operation is not None and operation.state == "rejected"
        assert audit is not None and audit.status == "failed"
    assert not is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=agent_id,
    )


def test_restore_persists_preparing_intent_before_git_configuration(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "restore-intent-before-git"
    observed_operation_id: str | None = None
    original_configure = workspace_package_service._configure_raw_git_storage

    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=agent_id, name="restore intent before Git")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        target = created.json()["current_commit_sha"]
        workspace.joinpath("CLAUDE.md").write_text("# later version\n", encoding="utf-8")
        _run_git(workspace, "add", "--", "CLAUDE.md")
        _run_git(workspace, "commit", "-m", "Create later restore source")
        current = _run_git(workspace, "rev-parse", "HEAD")

        def configure_after_intent(repository: Path) -> None:
            nonlocal observed_operation_id
            with module.workspace_activation_service._Session() as db:
                operation = db.scalar(
                    select(AgentWorkspaceActivationOperationModel).where(
                        AgentWorkspaceActivationOperationModel.agent_id == agent_id,
                        AgentWorkspaceActivationOperationModel.state == "preparing",
                    )
                )
                assert operation is not None and operation.action == "restore"
                observed_operation_id = operation.operation_id
            original_configure(repository)
            raise workspace_git_operations.GitCommandError("injected restore Git configuration failure")

        def forbid_bootstrap(*_args, **_kwargs) -> None:
            raise AssertionError("restore must not bootstrap Git before its durable intent")

        monkeypatch.setattr(workspace_package_service, "_configure_raw_git_storage", configure_after_intent)
        monkeypatch.setattr(GitAgentVersionStore, "ensure_bootstrap", forbid_bootstrap)
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/restore",
            json={
                "expected_current_commit_sha": current,
                "target_commit_sha": target,
            },
        )

    assert observed_operation_id is not None
    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    with module.workspace_activation_service._Session() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, observed_operation_id)
        assert operation is not None and operation.state == "rejected"
    assert not is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=agent_id,
    )


def test_overwrite_audit_write_failure_rolls_back_head_and_session_mapping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    private_detail = "injected-private-overwrite-audit-detail"
    with TestClient(module.app) as client:
        created = _import_new_agent(
            client,
            agent_id="overwrite-audit-failure",
            name="overwrite audit failure",
        )
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = _run_git(workspace, "rev-parse", "HEAD")
        baseline_bytes = workspace.joinpath("CLAUDE.md").read_bytes()
        session = LocalSession(
            session_id="overwrite-audit-failure-session",
            sdk_session_id="overwrite-audit-failure-sdk",
            agent_id="overwrite-audit-failure",
            turns=1,
        )
        module.session_store.save(session)

        def fail_accepted_audit(*_args, **_kwargs) -> None:
            raise RuntimeError(private_detail)

        monkeypatch.setattr(
            module.agent_testing_store,
            "record_import_in_transaction",
            fail_accepted_audit,
        )
        response = client.post(
            "/api/agent-registry/overwrite-audit-failure/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package(
                        {"CLAUDE.md": b"# candidate must not activate\n"},
                        agent_id="overwrite-audit-failure",
                    ),
                    "application/gzip",
                )
            },
        )

    saved = module.session_store.get(session.session_id)
    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_AUDIT_FAILED"
    assert private_detail not in response.text
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline
    assert workspace.joinpath("CLAUDE.md").read_bytes() == baseline_bytes
    assert saved is not None and saved.sdk_session_id == "overwrite-audit-failure-sdk"
    with module.agent_testing_store.Session() as db:
        records = list(
            db.scalars(
                select(AgentWorkspaceImportRecordModel)
                .where(AgentWorkspaceImportRecordModel.agent_id == "overwrite-audit-failure")
                .order_by(AgentWorkspaceImportRecordModel.created_at)
            ).all()
        )
    assert [record.status for record in records] == ["accepted", "failed"]
    assert records[0].action == "created"
    assert records[1].error_json["error_code"] == "WORKSPACE_IMPORT_AUDIT_FAILED"


def test_unchanged_import_audit_failure_restores_dirty_snapshot_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(
            client,
            agent_id="unchanged-audit-failure",
            name="unchanged audit failure",
        )
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = _run_git(workspace, "rev-parse", "HEAD")
        changed_claude = b"# dirty content already represented by package\n"
        dirty_payload = b"operator-owned dirty bytes\n"
        workspace.joinpath("CLAUDE.md").write_bytes(changed_claude)
        workspace.joinpath("dirty.txt").write_bytes(dirty_payload)
        package = _package_from_workspace(workspace, overrides={})
        baseline_status = _run_git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
        )

        def fail_accepted_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("injected-private-unchanged-audit-detail")

        monkeypatch.setattr(
            module.agent_testing_store,
            "record_import_in_transaction",
            fail_accepted_audit,
        )
        response = client.post(
            "/api/agent-registry/unchanged-audit-failure/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={
                "package": (
                    "unchanged.tar.gz",
                    package,
                    "application/gzip",
                )
            },
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_AUDIT_FAILED"
    assert "injected-private-unchanged-audit-detail" not in response.text
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline
    assert workspace.joinpath("CLAUDE.md").read_bytes() == changed_claude
    assert workspace.joinpath("dirty.txt").read_bytes() == dirty_payload
    assert (
        _run_git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
        )
        == baseline_status
    )
    with module.agent_testing_store.Session() as db:
        records = list(db.scalars(select(AgentWorkspaceImportRecordModel).where(AgentWorkspaceImportRecordModel.agent_id == "unchanged-audit-failure")).all())
    assert [(record.action, record.status) for record in records] == [
        ("created", "accepted"),
        ("overwrite", "failed"),
    ]
