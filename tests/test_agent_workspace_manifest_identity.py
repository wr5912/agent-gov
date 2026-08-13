from __future__ import annotations

import stat
from pathlib import Path
from typing import cast

import app.services.agent_workspace_create_service as workspace_create_module
import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.agent_testing.service import AgentTestingError
from app.runtime.agent_paths import (
    BUSINESS_AGENT_REPOSITORY_LOCKS_DIRNAME,
    business_agent_layout,
    business_agents_root,
)
from app.runtime.agent_registry_db import AgentRegistryModel
from app.services import business_agent_provisioning
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

TARGET_AGENT_ID = "identity-target"
_INSPECTION_FAILURE_CODE = "AGENT_SOURCE_MCP_SECRET_LITERAL"
_UNEXPECTED_INSPECTION_FAILURE_CODE = "AGENT_TEST_SUITE_INSPECTION_FAILED"
_INSPECTION_PRIVATE_DETAIL = "must-not-leak-from-test-suite-inspection"


@pytest.mark.parametrize(
    ("manifest", "expected_code", "detail_fragment", "forbidden_fragment"),
    [
        (None, "WORKSPACE_MANIFEST_AGENT_ID_REQUIRED", "缺少 agent.yaml", None),
        (b"metadata: {}\n", "WORKSPACE_MANIFEST_AGENT_ID_REQUIRED", "缺少必填字段 agent.yaml.agent", None),
        (b"agent: {}\n", "WORKSPACE_MANIFEST_AGENT_ID_REQUIRED", "缺少必填字段 agent.yaml.agent.id", None),
        (b"agent:\n  id: 123\n", "WORKSPACE_MANIFEST_AGENT_ID_INVALID", "必须是字符串", None),
        (
            b"agent:\n  id: ../../private\n",
            "WORKSPACE_MANIFEST_AGENT_ID_INVALID",
            "只能包含英文字母",
            "../../private",
        ),
        (b"agent:\n  id: ' identity-target '\n", "WORKSPACE_MANIFEST_AGENT_ID_INVALID", "不能包含首尾空白", None),
        (b"agent: [\n", "WORKSPACE_MANIFEST_INVALID", "不是可解析的安全 YAML", None),
        (
            b"agent:\n  id: identity-target\n  id: identity-target\n",
            "WORKSPACE_MANIFEST_INVALID",
            "重复的 agent.yaml.agent.id",
            None,
        ),
    ],
)
def test_create_rejects_missing_or_invalid_manifest_identity_before_mutation(
    monkeypatch,
    tmp_path: Path,
    manifest: bytes | None,
    expected_code: str,
    detail_fragment: str,
    forbidden_fragment: str | None,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    files = {"CLAUDE.md": b"# rejected\n"}
    if manifest is not None:
        files["agent.yaml"] = manifest
    package = _workspace_package(files)

    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error_code"] == expected_code
    assert body["detail"].startswith("导入被拒绝：")
    assert detail_fragment in body["detail"]
    assert body["field"].startswith("agent.yaml.agent")
    assert body["import_action"] == "create"
    assert body["expected_agent_id"] == TARGET_AGENT_ID
    assert body["remediation"]
    if forbidden_fragment is not None:
        assert forbidden_fragment not in response.text
    _assert_create_rejection_has_no_target_state(module, expected_code=expected_code)


def test_create_rejects_deep_manifest_without_partial_state(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    lines = ["agent:", f"  id: {TARGET_AGENT_ID}", "nested:"]
    for depth in range(350):
        lines.append(f"{'  ' * (depth + 1)}level_{depth}:")
    lines.append(f"{'  ' * 351}value")
    package = _workspace_package(
        {
            "CLAUDE.md": b"# rejected\n",
            "agent.yaml": ("\n".join(lines) + "\n").encode(),
        }
    )

    with TestClient(module.app, raise_server_exceptions=False) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "WORKSPACE_MANIFEST_INVALID"
    assert response.json()["detail"].startswith("导入被拒绝：")
    _assert_create_rejection_has_no_target_state(module, expected_code="WORKSPACE_MANIFEST_INVALID")


def test_create_rejects_source_identity_mismatch_with_actionable_error_and_audit(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    source = Path(__file__).resolve().parents[1] / "docker" / "runtime-bootstrap" / "business-agents" / "security-operations-expert" / "workspace"
    package = _package_from_workspace(source, overrides={})

    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    body = response.json()
    assert response.status_code == 409
    assert body["error_code"] == "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH"
    assert "security-operations-expert" in body["detail"]
    assert TARGET_AGENT_ID in body["detail"]
    assert "系统不会改写包内身份" in body["detail"]
    assert body["field"] == "agent.yaml.agent.id"
    assert body["import_action"] == "create"
    assert body["expected_agent_id"] == TARGET_AGENT_ID
    assert body["actual_agent_id"] == "security-operations-expert"
    assert "完全一致" in body["remediation"]
    _assert_create_rejection_has_no_target_state(
        module,
        expected_code="WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
    )


def test_create_rejects_case_only_manifest_identity_mismatch(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {
            "CLAUDE.md": b"# rejected\n",
            "agent.yaml": b"agent:\n  id: Identity-Target\n",
        }
    )

    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    body = response.json()
    assert response.status_code == 409
    assert body["error_code"] == "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH"
    assert body["actual_agent_id"] == "Identity-Target"
    assert body["expected_agent_id"] == TARGET_AGENT_ID
    assert "完全一致" in body["detail"]
    _assert_create_rejection_has_no_target_state(
        module,
        expected_code="WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
    )


def test_create_rejects_url_agent_id_with_surrounding_whitespace_before_package_processing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {
            "agent.yaml": f"agent:\n  id: {TARGET_AGENT_ID}\n".encode(),
        }
    )

    with TestClient(module.app) as client:
        response = client.post(
            f"/api/agent-registry/%20{TARGET_AGENT_ID}%20/workspace/import",
            data={"name": "identity target"},
            files={
                "package": (
                    "workspace.tar.gz",
                    package,
                    "application/gzip",
                )
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert any(error.get("loc") == ["path", "agent_id"] and error.get("type") == "string_pattern_mismatch" for error in body["detail"])
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None
    assert not business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID).root.exists()
    assert _import_records(module, TARGET_AGENT_ID) == []


@pytest.mark.parametrize("agent_id_length", [129, 180])
def test_create_rejects_overlong_agent_id_before_any_target_filesystem_or_database_side_effect(
    monkeypatch,
    tmp_path: Path,
    agent_id_length: int,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    agent_id = "a" * agent_id_length
    target_root = business_agents_root(module.settings.data_dir) / agent_id
    target_lock = module.settings.data_dir / BUSINESS_AGENT_REPOSITORY_LOCKS_DIRNAME / f"{agent_id}.lock"

    with TestClient(module.app) as client:
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"name": "must never be processed"},
            files={"package": ("unparsed.tar.gz", b"not a workspace package", "application/gzip")},
        )

    assert response.status_code == 422
    assert any(error.get("type") == "string_too_long" for error in response.json()["detail"])
    assert not target_root.exists()
    assert not target_lock.exists()
    with module.agent_testing_service.store.Session() as db:
        assert db.get(AgentRegistryModel, agent_id) is None
        assert db.scalar(select(AgentWorkspaceImportRecordModel.import_id).where(AgentWorkspaceImportRecordModel.agent_id == agent_id).limit(1)) is None


def test_create_accepts_exact_manifest_identity_without_rewriting_package(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    source = Path(__file__).resolve().parents[1] / "docker" / "runtime-bootstrap" / "business-agents" / "security-operations-expert" / "workspace"
    manifest = f"agent:\n  id: {TARGET_AGENT_ID}\n  profile: security-operations-expert\n".encode()
    package = _package_from_workspace(source, overrides={"agent.yaml": manifest})

    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "created"
    assert body["agent"]["agent_id"] == TARGET_AGENT_ID
    assert body["test_suite_status"] == "ready"
    assert body["test_suite_diagnostics"] == []
    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 1
    assert records[0].import_id == body["import_record_id"]
    assert records[0].status == "accepted"
    assert records[0].suite_status == "ready"
    assert records[0].diagnostics_json == []
    target = Path(body["agent"]["workspace_dir"])
    assert (target / "agent.yaml").read_bytes() == manifest
    source_files = {path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()}
    target_files = {path.relative_to(target).as_posix(): path for path in target.rglob("*") if path.is_file() and ".git" not in path.relative_to(target).parts}
    assert set(target_files) == set(source_files)
    for relative, source_path in source_files.items():
        if relative == "agent.yaml":
            continue
        assert target_files[relative].read_bytes() == source_path.read_bytes()
        assert stat.S_IMODE(target_files[relative].stat().st_mode) & 0o111 == stat.S_IMODE(source_path.stat().st_mode) & 0o111


@pytest.mark.parametrize("injection_phase", ["before_init", "before_publish"])
def test_create_rejects_candidate_content_injected_before_publication(
    monkeypatch,
    tmp_path: Path,
    injection_phase: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    layout = business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID)
    private_value = "foreign-candidate-content"
    if injection_phase == "before_init":
        real_initialize = workspace_create_module.initialize_imported_repository

        def inject_before_init(store):
            store.repository_dir.joinpath("foreign-before-init.txt").write_text(private_value, encoding="utf-8")
            return real_initialize(store)

        monkeypatch.setattr(workspace_create_module, "initialize_imported_repository", inject_before_init)
        residue = layout.workspace / "foreign-before-init.txt"
    else:
        real_prepare = module.agent_testing_service.prepare_import

        def inject_before_publish(*args, **kwargs):
            prepared = real_prepare(*args, **kwargs)
            layout.workspace.joinpath("foreign-before-publish.txt").write_text(private_value, encoding="utf-8")
            return prepared

        monkeypatch.setattr(module.agent_testing_service, "prepare_import", inject_before_publish)
        residue = layout.workspace / "foreign-before-publish.txt"

    with TestClient(module.app) as client:
        response = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_CANDIDATE_INVALID"
    assert private_value not in response.text
    assert residue.read_text(encoding="utf-8") == private_value
    _assert_never_public_quarantine(module)


@pytest.mark.parametrize("symlink_location", ["git", "version"])
def test_create_initialization_rejects_symlink_without_touching_external_target(
    monkeypatch,
    tmp_path: Path,
    symlink_location: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / f"outside-{symlink_location}"
    outside.mkdir()
    sentinel = outside / "foreign-sentinel"
    sentinel.write_text("foreign-owner", encoding="utf-8")
    real_initialize = workspace_create_module.initialize_imported_repository

    def inject_symlink(store):
        target = store.repository_dir / ".git" if symlink_location == "git" else store.worktrees_dir.parent
        target.symlink_to(outside, target_is_directory=True)
        return real_initialize(store)

    monkeypatch.setattr(workspace_create_module, "initialize_imported_repository", inject_symlink)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        response = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_RESIDUE"
    assert sentinel.read_text(encoding="utf-8") == "foreign-owner"
    assert list(outside.iterdir()) == [sentinel]
    assert "foreign-owner" not in response.text
    _assert_never_public_quarantine(module)


def test_create_rejects_whole_layout_residue_with_stable_error_contract(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    layout = business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID)
    sentinel = layout.claude_root / "preexisting-private-state"
    with TestClient(module.app) as client:
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("foreign-owner", encoding="utf-8")
        response = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_RESIDUE"
    assert sentinel.read_text(encoding="utf-8") == "foreign-owner"
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None
    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 1 and records[0].status == "failed"
    assert records[0].error_json["error_code"] == "WORKSPACE_IMPORT_RESIDUE"


def test_create_apply_failure_is_not_misreported_as_permanent_id_reservation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    layout = business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID)

    def fail_apply(*_args, **_kwargs):
        raise business_agent_provisioning.WorkspaceProvisioningError(
            "forced safe apply failure",
            cleanup_complete=True,
        )

    monkeypatch.setattr(business_agent_provisioning, "apply_business_agent_workspace_plan", fail_apply)
    with TestClient(module.app) as client:
        response = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_IMPORT_PROVISIONING_FAILED"
    assert response.json()["error_code"] != "WORKSPACE_AGENT_ID_RESERVED"
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None
    assert not layout.root.exists()
    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 1 and records[0].status == "failed"


def test_create_keeps_activated_workspace_and_records_invalid_suite_when_post_activation_inspection_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    private_config = b'{"mcpServers":{"private":{"headers":{"Authorization":"Bearer test-only-value"}}}}\n'
    package = _workspace_package(
        {
            "CLAUDE.md": b"# activated create\n",
            ".mcp.json": private_config,
            ".claude/settings.json": b'{"permissions":{"ask":[]}}\n',
        },
        agent_id=TARGET_AGENT_ID,
    )

    def fail_inspection(*_args, **_kwargs):
        raise RuntimeError(_INSPECTION_PRIVATE_DETAIL)

    monkeypatch.setattr(module.agent_testing_service, "inspect_suite", fail_inspection)
    with TestClient(module.app) as client:
        response = _import_new_agent(
            client,
            agent_id=TARGET_AGENT_ID,
            name="identity target",
            package=package,
        )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "created"
    assert body["test_suite_status"] == "invalid"
    assert [item["code"] for item in body["test_suite_diagnostics"]] == [
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        _UNEXPECTED_INSPECTION_FAILURE_CODE,
    ]
    assert _UNEXPECTED_INSPECTION_FAILURE_CODE in body["test_suite_diagnostics"][0]["message"]
    assert _INSPECTION_PRIVATE_DETAIL not in response.text
    workspace = Path(body["agent"]["workspace_dir"])
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is not None
    assert workspace.joinpath(".mcp.json").read_bytes() == private_config
    assert _run_git(workspace, "rev-parse", "HEAD") == body["current_commit_sha"]

    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 1
    assert records[0].status == "accepted"
    assert records[0].action == "created"
    assert records[0].commit_sha == body["current_commit_sha"]
    assert records[0].error_json == {}
    assert records[0].suite_status == "invalid"
    assert [item["code"] for item in records[0].diagnostics_json] == [
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        _UNEXPECTED_INSPECTION_FAILURE_CODE,
    ]
    suite_json = records[0].suite_json
    assert suite_json is not None
    diagnostics = cast(list[dict[str, str]], suite_json["diagnostics"])
    assert {item["code"] for item in diagnostics} == {
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        _UNEXPECTED_INSPECTION_FAILURE_CODE,
    }
    assert _INSPECTION_PRIVATE_DETAIL not in str(suite_json)


def test_overwrite_keeps_new_head_and_records_invalid_suite_when_post_activation_inspection_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")
        assert created.status_code == 200
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_commit = _run_git(workspace, "rev-parse", "HEAD")
        private_config = b'{"mcpServers":{"private":{"env":{"MCP_TOKEN":"test-only-value"}}}}\n'

        def fail_inspection(*_args, **_kwargs):
            raise AgentTestingError(422, _INSPECTION_FAILURE_CODE, _INSPECTION_PRIVATE_DETAIL)

        monkeypatch.setattr(module.agent_testing_service, "inspect_suite", fail_inspection)
        response = client.post(
            f"/api/agent-registry/{TARGET_AGENT_ID}/workspace/import",
            data={"expected_current_commit_sha": baseline_commit},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package(
                        {
                            "CLAUDE.md": b"# activated overwrite\n",
                            ".mcp.json": private_config,
                            ".claude/settings.json": b'{"permissions":{"ask":[]}}\n',
                        },
                        agent_id=TARGET_AGENT_ID,
                    ),
                    "application/gzip",
                )
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "overwritten"
    assert body["previous_commit_sha"] == baseline_commit
    assert body["rollback_target_commit_sha"] == baseline_commit
    assert body["current_commit_sha"] != baseline_commit
    assert body["test_suite_status"] == "invalid"
    assert [item["code"] for item in body["test_suite_diagnostics"]] == [
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        _INSPECTION_FAILURE_CODE,
    ]
    assert _INSPECTION_FAILURE_CODE in body["test_suite_diagnostics"][0]["message"]
    assert _INSPECTION_PRIVATE_DETAIL not in response.text
    assert workspace.joinpath(".mcp.json").read_bytes() == private_config
    assert _run_git(workspace, "rev-parse", "HEAD") == body["current_commit_sha"]

    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 2
    overwritten = [record for record in records if record.action == "overwritten"]
    assert len(overwritten) == 1
    assert overwritten[0].status == "accepted"
    assert overwritten[0].commit_sha == body["current_commit_sha"]
    assert overwritten[0].error_json == {}
    assert overwritten[0].suite_status == "invalid"
    assert [item["code"] for item in overwritten[0].diagnostics_json] == [
        "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE",
        _INSPECTION_FAILURE_CODE,
    ]
    assert all(record.status == "accepted" for record in records)


def test_overwrite_rejects_manifest_mismatch_before_workspace_or_session_changes(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_commit = _run_git(workspace, "rev-parse", "HEAD")
        baseline_tree = _run_git(workspace, "rev-parse", "HEAD^{tree}")

        def fail_session_invalidation(*_args, **_kwargs):
            raise AssertionError("identity rejection must not invalidate sessions")

        monkeypatch.setattr(
            module.session_store,
            "clear_inactive_sdk_sessions_for_agent_in_transaction",
            fail_session_invalidation,
        )
        response = client.post(
            f"/api/agent-registry/{TARGET_AGENT_ID}/workspace/import",
            data={"expected_current_commit_sha": baseline_commit},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package(
                        {
                            "CLAUDE.md": b"# must not apply\n",
                            "agent.yaml": b"agent:\n  id: another-agent\n",
                        }
                    ),
                    "application/gzip",
                )
            },
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH"
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline_commit
    assert _run_git(workspace, "rev-parse", "HEAD^{tree}") == baseline_tree
    assert not workspace.joinpath("must-not-exist").exists()


def test_existing_target_without_expected_commit_reports_overwrite_remediation_first(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=TARGET_AGENT_ID, name="identity target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_commit = _run_git(workspace, "rev-parse", "HEAD")
        response = client.post(
            f"/api/agent-registry/{TARGET_AGENT_ID}/workspace/import",
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package({"agent.yaml": b"agent:\n  id: another-agent\n"}),
                    "application/gzip",
                )
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error_code"] == "WORKSPACE_IMPORT_CURRENT_REF_REQUIRED"
    assert "已经存在" in body["detail"]
    assert "覆盖导入" in body["detail"]
    assert body["field"] == "expected_current_commit_sha"
    assert body["import_action"] == "overwrite"
    assert body["expected_agent_id"] == TARGET_AGENT_ID
    assert "当前提交版本" in body["remediation"]
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline_commit


def _assert_create_rejection_has_no_target_state(module, *, expected_code: str) -> None:
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None
    layout = business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID)
    assert not layout.workspace.exists()
    assert not layout.version_base.exists()
    with module.agent_testing_service.store.Session() as db:
        records = list(
            db.scalars(
                select(AgentWorkspaceImportRecordModel)
                .where(AgentWorkspaceImportRecordModel.agent_id == TARGET_AGENT_ID)
                .order_by(AgentWorkspaceImportRecordModel.created_at.desc())
            )
        )
    assert len(records) == 1
    assert records[0].action == "create"
    assert records[0].status == "failed"
    assert records[0].package_sha256
    assert records[0].tree_sha256
    assert records[0].error_json["error_code"] == expected_code


def _assert_never_public_quarantine(module) -> None:
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None
    records = _import_records(module, TARGET_AGENT_ID)
    assert len(records) == 1
    assert records[0].status == "failed"
    with module.agent_testing_service.store.Session() as db:
        row = db.get(AgentRegistryModel, TARGET_AGENT_ID)
        assert row is not None and row.deleted_at
        assert row.provision_completed_token is None
        assert row.provision_previous_json == {
            "kind": "workspace_must_be_absent",
            "workspace_dir": str(business_agent_layout(module.settings.data_dir, TARGET_AGENT_ID).workspace),
        }


def _import_records(module, agent_id: str) -> list[AgentWorkspaceImportRecordModel]:
    with module.agent_testing_service.store.Session() as db:
        return list(
            db.scalars(
                select(AgentWorkspaceImportRecordModel)
                .where(AgentWorkspaceImportRecordModel.agent_id == agent_id)
                .order_by(AgentWorkspaceImportRecordModel.created_at)
            )
        )
