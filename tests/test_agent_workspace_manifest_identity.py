from __future__ import annotations

import stat
from pathlib import Path

import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_paths import business_agent_layout
from fastapi.testclient import TestClient
from sqlalchemy import select

from app_test_utils import load_test_app as _load_app
from test_agent_workspace_packages import (
    _candidate_workspace,
    _import_new_agent,
    _package_from_workspace,
    _run_git,
    _seed_active_agent,
    _workspace_package,
)

TARGET_AGENT_ID = "identity-target"


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
        (
            b"agent:\n  id: identity-target\n  id: identity-target\n",
            "WORKSPACE_MANIFEST_INVALID",
            "重复的 agent.yaml.agent.id",
            None,
        ),
    ],
)
def test_create_rejects_missing_or_invalid_manifest_identity_before_mutation(
    process_environment,
    tmp_path: Path,
    manifest: bytes | None,
    expected_code: str,
    detail_fragment: str,
    forbidden_fragment: str | None,
) -> None:
    module = _load_app(process_environment, tmp_path)
    files = {"AGENT.md": b"# rejected\n"}
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


def test_create_rejects_deep_manifest_without_partial_state(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    lines = ["agent:", f"  id: {TARGET_AGENT_ID}", "nested:"]
    for depth in range(350):
        lines.append(f"{'  ' * (depth + 1)}level_{depth}:")
    lines.append(f"{'  ' * 351}value")
    package = _workspace_package(
        {
            "AGENT.md": b"# rejected\n",
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


def test_create_rejects_source_identity_mismatch_with_actionable_error_and_audit(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
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


def test_create_rejects_case_only_manifest_identity_mismatch(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    package = _workspace_package(
        {
            "AGENT.md": b"# rejected\n",
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
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
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
    assert body["error_code"] == "WORKSPACE_AGENT_ID_INVALID"
    assert body["field"] == "url.agent_id"
    assert "首尾空白" in body["detail"]
    assert body["remediation"]
    assert module.agent_registry_store.get_agent(TARGET_AGENT_ID) is None


def test_create_accepts_exact_manifest_identity_without_rewriting_package(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    source = Path(__file__).resolve().parents[1] / "docker" / "runtime-bootstrap" / "business-agents" / "security-operations-expert" / "workspace"
    manifest = (
        (source / "agent.yaml")
        .read_bytes()
        .replace(
            b"id: security-operations-expert",
            f"id: {TARGET_AGENT_ID}".encode(),
            1,
        )
    )
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
    assert body["agent"]["status"] == "draft"
    assert body["published"] is False
    assert body["test_suite_status"] == "ready"
    assert body["test_suite_warnings"] == []
    live = Path(body["agent"]["workspace_dir"])
    target = _candidate_workspace(module, response)
    assert _run_git(live, "rev-parse", "HEAD") == body["base_commit_sha"]
    assert not (live / "agent.yaml").exists()
    assert (target / "agent.yaml").read_bytes() == manifest
    source_files = {path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()}
    target_files = {path.relative_to(target).as_posix(): path for path in target.rglob("*") if path.is_file() and ".git" not in path.relative_to(target).parts}
    assert set(target_files) == set(source_files)
    for relative, source_path in source_files.items():
        if relative == "agent.yaml":
            continue
        assert target_files[relative].read_bytes() == source_path.read_bytes()
        assert stat.S_IMODE(target_files[relative].stat().st_mode) & 0o111 == stat.S_IMODE(source_path.stat().st_mode) & 0o111


def test_overwrite_rejects_manifest_mismatch_before_workspace_changes(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id=TARGET_AGENT_ID, name="identity target")
    with TestClient(module.app) as client:
        baseline_commit = _run_git(workspace, "rev-parse", "HEAD")
        baseline_tree = _run_git(workspace, "rev-parse", "HEAD^{tree}")

        response = client.post(
            f"/api/agent-registry/{TARGET_AGENT_ID}/workspace/import",
            data={"expected_current_commit_sha": baseline_commit},
            files={
                "package": (
                    "replacement.tar.gz",
                    _workspace_package(
                        {
                            "AGENT.md": b"# must not apply\n",
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


def test_existing_target_without_expected_commit_reports_overwrite_remediation_first(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id=TARGET_AGENT_ID, name="identity target")
    with TestClient(module.app) as client:
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
