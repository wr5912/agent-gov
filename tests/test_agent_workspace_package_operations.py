from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import AgentMaintenanceClaimLost, is_maintenance_active
from app.runtime.agent_git_raw_storage import RawGitStorageError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.session_store import LocalSession
from app.services import agent_version_maintenance
from app.services import agent_workspace_git_operations as workspace_git_operations
from app.services import agent_workspace_package_codec as workspace_codec
from app.services.agent_governance import AgentGovernanceError
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import (
    import_new_agent as _import_new_agent,
)
from workspace_package_test_utils import (
    package_from_workspace as _package_from_workspace,
)
from workspace_package_test_utils import (
    package_with_empty_pax_path as _package_with_empty_pax_path,
)
from workspace_package_test_utils import (
    package_with_large_reversed_conflict as _package_with_large_reversed_conflict,
)
from workspace_package_test_utils import (
    package_with_long_tar_metadata as _package_with_long_tar_metadata,
)
from workspace_package_test_utils import (
    package_with_metadata_chain as _package_with_metadata_chain,
)
from workspace_package_test_utils import (
    package_with_sparse_pax as _package_with_sparse_pax,
)
from workspace_package_test_utils import (
    run_git as _run_git,
)
from workspace_package_test_utils import (
    workspace_package as _workspace_package,
)


def test_workspace_import_rejects_missing_or_invalid_http_multipart_contract(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package({"CLAUDE.md": b"# multipart\n"})
    with TestClient(module.app) as client:
        missing_length_request = client.build_request(
            "POST",
            "/api/agent-registry/missing-length/workspace/import",
            content=b"body",
            headers={"Content-Type": "multipart/form-data; boundary=unused"},
        )
        del missing_length_request.headers["content-length"]
        missing_length = client.send(missing_length_request)
        unsupported_media = client.post(
            "/api/agent-registry/unsupported-media/workspace/import",
            content=b"body",
            headers={"Content-Type": "application/gzip"},
        )
        invalid_length_request = client.build_request(
            "POST",
            "/api/agent-registry/invalid-length/workspace/import",
            content=b"body",
            headers={
                "Content-Type": "multipart/form-data; boundary=unused",
                "Content-Length": "not-an-integer",
            },
        )
        invalid_length = client.send(invalid_length_request)
        unknown_field = client.post(
            "/api/agent-registry/unknown-field/workspace/import",
            files=[
                ("package", ("workspace.tar.gz", package, "application/gzip")),
                ("unexpected", (None, "value")),
            ],
        )
        repeated_field = client.post(
            "/api/agent-registry/repeated-field/workspace/import",
            files=[
                ("package", ("workspace.tar.gz", package, "application/gzip")),
                ("name", (None, "first")),
                ("name", (None, "second")),
            ],
        )

    assert missing_length.status_code == 411
    assert missing_length.json()["error_code"] == "WORKSPACE_CONTENT_LENGTH_REQUIRED"
    assert unsupported_media.status_code == 415
    assert unsupported_media.json()["error_code"] == "WORKSPACE_PACKAGE_INVALID"
    assert invalid_length.status_code == 422
    assert invalid_length.json()["error_code"] == "WORKSPACE_PACKAGE_INVALID"
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error_code"] == "WORKSPACE_PACKAGE_INVALID"
    assert repeated_field.status_code == 422
    assert repeated_field.json()["error_code"] == "WORKSPACE_PACKAGE_INVALID"


def test_workspace_package_operation_conflicts_with_active_agent_maintenance(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="busy", name="busy")
        assert created.status_code == 200
        workspace = Path(created.json()["agent"]["workspace_dir"])
        assert (workspace / ".git").is_dir()
        with module.agent_governance.version_maintenance.lease(
            agent_id="busy",
            kind="workspace_import",
            owner_id="test-suite",
        ):
            response = client.post("/api/agent-registry/busy/workspace/export")
            with pytest.raises(AgentGovernanceError) as conflict:
                module.agent_governance.create_change_set(agent_id="busy", title="blocked by package maintenance")

        with module.agent_governance.version_maintenance.lease(
            agent_id="busy",
            kind="change_set_create",
            owner_id="test-suite",
        ):
            reverse = client.post("/api/agent-registry/busy/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_MAINTENANCE_CONFLICT"
    assert conflict.value.status_code == 409
    assert reverse.status_code == 409
    assert (workspace / ".git").is_dir()


def test_workspace_package_checks_all_open_change_sets_without_list_window(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert _import_new_agent(client, agent_id="open-set", name="open-set").status_code == 200
        module.agent_governance.create_change_set(agent_id="open-set", title="must block package operations")
        monkeypatch.setattr(module.agent_governance, "list_change_sets", lambda **_kwargs: [])
        response = client.post("/api/agent-registry/open-set/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_CHANGE_SET_ACTIVE"


def test_workspace_git_export_failure_is_structured(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert _import_new_agent(client, agent_id="git-failure", name="git-failure").status_code == 200

        def fail_snapshot(_store: GitAgentVersionStore):
            raise AgentGitError(f"fatal: cannot read {tmp_path}/private-workspace")

        monkeypatch.setattr("app.services.agent_workspace_packages._snapshot_live_workspace", fail_snapshot)
        response = client.post("/api/agent-registry/git-failure/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert response.json()["detail"] == "Git workspace operation failed"
    assert str(tmp_path) not in response.text


def _post_package_size_limit_cases(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Response, Response, Response, Response]:
    invalid_json = client.post(
        "/api/agent-registry/invalid-json/workspace/import",
        data={"name": "invalid"},
        files={"package": ("invalid.tar.gz", _workspace_package({".mcp.json": b"[]"}), "application/gzip")},
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_codec, "MAX_SINGLE_MEMBER_BYTES", 4)
        oversized_member = client.post(
            "/api/agent-registry/oversized-member/workspace/import",
            data={"name": "oversized"},
            files={"package": ("oversized.tar.gz", _workspace_package({"five.bin": b"12345"}), "application/gzip")},
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_codec, "MAX_EXTRACTED_PACKAGE_BYTES", 4)
        oversized_total = client.post(
            "/api/agent-registry/oversized-total/workspace/import",
            data={"name": "oversized"},
            files={"package": ("oversized.tar.gz", _workspace_package({"a": b"123", "b": b"456"}), "application/gzip")},
        )
    rejected_before_parse = client.post(
        "/api/agent-registry/request-too-large/workspace/import",
        content=b"not-a-multipart-body",
        headers={
            "Content-Type": "multipart/form-data; boundary=unused",
            "Content-Length": str(workspace_codec.MAX_MULTIPART_REQUEST_BYTES + 1),
        },
    )
    return invalid_json, oversized_member, oversized_total, rejected_before_parse


def _post_package_member_limit_cases(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Response, Response]:
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_codec, "MAX_PACKAGE_MEMBERS", 2)
        at_member_limit = client.post(
            "/api/agent-registry/at-member-limit/workspace/import",
            data={"name": "at limit"},
            files={
                "package": (
                    "at-limit.tar.gz",
                    _workspace_package({"a": b"1"}, agent_id="at-member-limit"),
                    "application/gzip",
                )
            },
        )
        over_member_limit = client.post(
            "/api/agent-registry/over-member-limit/workspace/import",
            data={"name": "over limit"},
            files={
                "package": (
                    "over-limit.tar.gz",
                    _workspace_package({"a": b"1", "b": b"2"}, agent_id="over-member-limit"),
                    "application/gzip",
                )
            },
        )
    return at_member_limit, over_member_limit


def _post_package_metadata_limit_cases(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Response, Response, Response]:
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_codec, "MAX_TAR_METADATA_BYTES", 1024)
        oversized_metadata = client.post(
            "/api/agent-registry/oversized-metadata/workspace/import",
            data={"name": "oversized metadata"},
            files={"package": ("oversized-metadata.tar.gz", _package_with_long_tar_metadata(2048), "application/gzip")},
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_codec, "MAX_CONSECUTIVE_TAR_METADATA", 4)
        metadata_chain = client.post(
            "/api/agent-registry/metadata-chain/workspace/import",
            data={"name": "metadata chain"},
            files={"package": ("metadata-chain.tar.gz", _package_with_metadata_chain(5), "application/gzip")},
        )
        gnu_metadata_chain = client.post(
            "/api/agent-registry/gnu-metadata-chain/workspace/import",
            data={"name": "gnu metadata chain"},
            files={
                "package": (
                    "gnu-metadata-chain.tar.gz",
                    _package_with_metadata_chain(5, member_type=tarfile.GNUTYPE_LONGNAME),
                    "application/gzip",
                )
            },
        )
    return oversized_metadata, metadata_chain, gnu_metadata_chain


def test_workspace_import_rejects_invalid_configs_and_size_limits_before_mutation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        invalid_json, oversized_member, oversized_total, rejected_before_parse = _post_package_size_limit_cases(client, monkeypatch)
        at_member_limit, over_member_limit = _post_package_member_limit_cases(client, monkeypatch)
        oversized_metadata, metadata_chain, gnu_metadata_chain = _post_package_metadata_limit_cases(client, monkeypatch)
        sparse_pax = client.post(
            "/api/agent-registry/sparse-pax/workspace/import",
            data={"name": "sparse pax"},
            files={"package": ("sparse-pax.tar.gz", _package_with_sparse_pax(), "application/gzip")},
        )

    assert invalid_json.status_code == 422
    assert invalid_json.json()["error_code"] == "WORKSPACE_PACKAGE_CONFIG_INVALID"
    assert oversized_member.status_code == 413
    assert oversized_total.status_code == 413
    assert rejected_before_parse.status_code == 413
    assert at_member_limit.status_code == 200
    assert over_member_limit.status_code == 413
    assert over_member_limit.json()["error_code"] == "WORKSPACE_PACKAGE_TOO_MANY_MEMBERS"
    assert oversized_metadata.status_code == 413
    assert oversized_metadata.json()["error_code"] == "WORKSPACE_PACKAGE_METADATA_TOO_LARGE"
    assert metadata_chain.status_code == 413
    assert metadata_chain.json()["error_code"] == "WORKSPACE_PACKAGE_METADATA_TOO_LARGE"
    assert gnu_metadata_chain.status_code == 413
    assert gnu_metadata_chain.json()["error_code"] == "WORKSPACE_PACKAGE_METADATA_TOO_LARGE"
    assert sparse_pax.status_code == 422
    assert sparse_pax.json()["error_code"] == "WORKSPACE_PACKAGE_MEMBER_INVALID"
    assert module.agent_registry_store.get_agent("invalid-json") is None
    nul_member = tarfile.TarInfo("workspace/a\x00b")
    with pytest.raises(workspace_codec.WorkspacePackageError):
        workspace_codec._validate_member(nul_member, {}, set())


def test_workspace_import_rejects_empty_pax_path_and_large_reversed_path_conflict(tmp_path: Path) -> None:
    empty_path = _package_with_empty_pax_path()
    with pytest.raises(workspace_codec.WorkspacePackageError) as empty_exc:
        workspace_codec.read_workspace_package(
            io.BytesIO(empty_path),
            tmp_path / "empty-path.tar.gz",
            filename="empty-path.tar.gz",
        )
    assert empty_exc.value.error_code == "WORKSPACE_PACKAGE_PATH_INVALID"

    large_conflict = _package_with_large_reversed_conflict(workspace_codec.MAX_PACKAGE_MEMBERS)
    with pytest.raises(workspace_codec.WorkspacePackageError) as conflict_exc:
        workspace_codec.read_workspace_package(
            io.BytesIO(large_conflict),
            tmp_path / "large-conflict.tar.gz",
            filename="large-conflict.tar.gz",
        )
    assert conflict_exc.value.error_code == "WORKSPACE_PACKAGE_PATH_CONFLICT"


def test_workspace_import_maps_tarfile_recursion_error_to_invalid_package(monkeypatch, tmp_path: Path) -> None:
    package = _workspace_package({"CLAUDE.md": b"# valid preflight\n"})
    monkeypatch.setattr(workspace_codec.tarfile, "open", lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError("metadata chain")))

    with pytest.raises(workspace_codec.WorkspacePackageError) as exc_info:
        workspace_codec.read_workspace_package(
            io.BytesIO(package),
            tmp_path / "recursive.tar.gz",
            filename="recursive.tar.gz",
        )

    assert exc_info.value.error_code == "WORKSPACE_PACKAGE_INVALID"


def test_workspace_export_rejects_symlink_and_oversized_tree_without_advancing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="export-guard", name="export guard")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=export-guard").json()["commit_sha"]
        (workspace / "linked").symlink_to("CLAUDE.md")
        symlinked = client.post("/api/agent-registry/export-guard/workspace/export")
        after_symlink = client.get("/api/agent-repository/current?agent_id=export-guard").json()["commit_sha"]
        (workspace / "linked").unlink()
        (workspace / "oversized.bin").write_bytes(b"12345")
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace_codec, "MAX_SINGLE_MEMBER_BYTES", 4)
            oversized = client.post("/api/agent-registry/export-guard/workspace/export")

    assert symlinked.status_code == 422
    assert symlinked.json()["error_code"] == "WORKSPACE_EXPORT_TREE_INVALID"
    assert after_symlink == baseline
    assert oversized.status_code == 413


def test_workspace_export_snapshots_deletion_when_only_git_metadata_remains(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="empty-export", name="empty export")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=empty-export").json()["commit_sha"]
        for child in workspace.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

        exported = client.post("/api/agent-registry/empty-export/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=empty-export").json()["commit_sha"]

    assert exported.status_code == 200
    assert current != baseline
    assert _run_git(workspace, "ls-tree", "-r", "HEAD") == ""


def test_workspace_export_reports_unsafe_raw_attributes_path_without_advancing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="raw-path", name="raw path")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=raw-path").json()["commit_sha"]
        monkeypatch.setattr(
            workspace_git_operations,
            "configure_raw_git_storage",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RawGitStorageError("Git did not resolve info/attributes")),
        )
        response = client.post("/api/agent-registry/raw-path/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=raw-path").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert current == baseline
    assert _run_git(workspace, "status", "--porcelain") == ""


def test_workspace_export_unstages_original_dirty_state_when_snapshot_commit_fails(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    original_git = workspace_git_operations.run_git
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="commit-failure", name="commit failure")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=commit-failure").json()["commit_sha"]
        original_content = (workspace / "CLAUDE.md").read_bytes()
        changed_content = original_content + b"\n# dirty before failed export\n"
        (workspace / "CLAUDE.md").write_bytes(changed_content)

        def fail_snapshot_commit(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            if args[:3] == ["commit", "-m", "Snapshot live workspace before package operation"]:
                raise workspace_git_operations.GitCommandError(f"cannot commit {tmp_path}/private-workspace")
            return original_git(repository, args, check=check)

        monkeypatch.setattr(workspace_git_operations, "run_git", fail_snapshot_commit)
        response = client.post("/api/agent-registry/commit-failure/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=commit-failure").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert response.json()["detail"] == "Git workspace operation failed"
    assert str(tmp_path) not in response.text
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == changed_content
    assert subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workspace, check=False).returncode == 0
    assert subprocess.run(["git", "diff", "--quiet"], cwd=workspace, check=False).returncode == 1


def test_workspace_import_rechecks_lease_immediately_before_activation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package({"CLAUDE.md": b"# replacement\n"}, agent_id="lease-target")
    calls = 0
    original_assert = agent_version_maintenance.AgentVersionMaintenanceLease.assert_active

    def fail_second_assert(lease) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AgentMaintenanceClaimLost("injected lease loss before merge")
        original_assert(lease)

    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="lease-target", name="lease target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=lease-target").json()["commit_sha"]
        monkeypatch.setattr(agent_version_maintenance.AgentVersionMaintenanceLease, "assert_active", fail_second_assert)
        response = client.post(
            "/api/agent-registry/lease-target/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=lease-target").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_MAINTENANCE_CONFLICT"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes


def test_workspace_import_reports_success_after_merge_even_if_lease_release_is_lost(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {"CLAUDE.md": b"# applied despite late release loss\n"},
        agent_id="late-loss",
    )
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="late-loss", name="late loss")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=late-loss").json()["commit_sha"]
        monkeypatch.setattr(agent_version_maintenance, "release_maintenance", lambda *_args, **_kwargs: False)
        response = client.post(
            "/api/agent-registry/late-loss/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "overwritten"
    assert (workspace / "CLAUDE.md").read_bytes() == b"# applied despite late release loss\n"


def test_dirty_package_identical_import_reports_original_to_snapshot_version(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="dirty-unchanged", name="dirty unchanged")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        original = client.get("/api/agent-repository/current?agent_id=dirty-unchanged").json()["commit_sha"]
        workspace.joinpath("operator-note.txt").write_bytes(b"included dirty bytes\n")
        package = _package_from_workspace(workspace, overrides={})
        response = client.post(
            "/api/agent-registry/dirty-unchanged/workspace/import",
            data={"expected_current_commit_sha": original},
            files={"package": ("dirty-unchanged.tar.gz", package, "application/gzip")},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "unchanged"
    assert body["previous_commit_sha"] == original
    assert body["current_commit_sha"] != original
    assert body["rollback_target_commit_sha"] == original
    assert _run_git(workspace, "status", "--porcelain=v1", "--untracked-files=all", "--ignored") == ""
    with module.agent_testing_store.Session() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, body["import_record_id"])
        operation = db.scalar(
            select(AgentWorkspaceActivationOperationModel).where(AgentWorkspaceActivationOperationModel.import_id == body["import_record_id"])
        )
    assert audit is not None and (audit.action, audit.status, audit.commit_sha) == (
        "unchanged",
        "accepted",
        body["current_commit_sha"],
    )
    assert operation is not None and operation.state == "completed"
    assert operation.original_head_sha == original
    assert operation.base_commit_sha == operation.candidate_commit_sha == body["current_commit_sha"]


def test_workspace_import_does_not_activate_when_sdk_session_invalidation_fails(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package({"CLAUDE.md": b"# must not activate\n"}, agent_id="invalidate-failure")
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="invalidate-failure", name="invalidate failure")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=invalidate-failure").json()["commit_sha"]
        monkeypatch.setattr(
            module.session_store,
            "clear_inactive_sdk_sessions_for_agent_in_transaction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected invalidation failure")),
        )
        response = client.post(
            "/api/agent-registry/invalidate-failure/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=invalidate-failure").json()["commit_sha"]

    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_SESSION_INVALIDATION_FAILED"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes


def test_workspace_import_rejects_file_created_during_session_invalidation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {"CLAUDE.md": b"# must not activate across a dirty race\n"},
        agent_id="dirty-race",
    )
    original_invalidation = module.session_store.clear_inactive_sdk_sessions_for_agent_in_transaction
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="dirty-race", name="dirty race")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        baseline = client.post("/api/agent-registry/dirty-race/workspace/export").headers["x-agent-commit-sha"]
        concurrent_file = workspace / "concurrent.secret"

        def invalidate_then_write(db, *, agent_id: str) -> int:
            cleared = original_invalidation(db, agent_id=agent_id)
            concurrent_file.write_bytes(b"preserve concurrent writer\n")
            return cleared

        monkeypatch.setattr(
            module.session_store,
            "clear_inactive_sdk_sessions_for_agent_in_transaction",
            invalidate_then_write,
        )
        response = client.post(
            "/api/agent-registry/dirty-race/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=dirty-race").json()["commit_sha"]

    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_ACTIVATION_RECOVERY_REQUIRED"
    assert current != baseline
    assert (workspace / "CLAUDE.md").read_bytes() == b"# must not activate across a dirty race\n"
    assert concurrent_file.read_bytes() == b"preserve concurrent writer\n"
    assert is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id="dirty-race",
    )


@pytest.mark.parametrize(
    ("concurrent_name", "gitignore"),
    [
        ("collision.secret", b"*.secret\n"),
        ("late-untracked.txt", None),
    ],
)
def test_workspace_import_does_not_overwrite_ignored_file_created_at_merge(
    monkeypatch,
    tmp_path: Path,
    concurrent_name: str,
    gitignore: bytes | None,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    entries = {"CLAUDE.md": b"# candidate must not overwrite the concurrent file\n"}
    if gitignore is not None:
        entries[".gitignore"] = gitignore
    package = _workspace_package(entries, agent_id="merge-race")
    original_git = workspace_git_operations.run_git
    injected = False
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="merge-race", name="merge race")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        if gitignore is not None:
            (workspace / ".gitignore").write_bytes(gitignore)
        baseline = client.post("/api/agent-registry/merge-race/workspace/export").headers["x-agent-commit-sha"]
        concurrent_file = workspace / concurrent_name
        session = LocalSession(
            session_id="merge-race-session",
            sdk_session_id="merge-race-sdk",
            agent_id="merge-race",
            turns=1,
        )
        module.session_store.save(session)

        def inject_ignored_file_after_merge(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            nonlocal injected
            if args[:3] == ["merge", "--ff-only", "--no-overwrite-ignore"]:
                result = original_git(repository, args, check=check)
                injected = True
                concurrent_file.write_bytes(b"concurrent writer wins\n")
                return result
            return original_git(repository, args, check=check)

        monkeypatch.setattr(workspace_git_operations, "run_git", inject_ignored_file_after_merge)
        response = client.post(
            "/api/agent-registry/merge-race/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=merge-race").json()["commit_sha"]

    saved = module.session_store.get(session.session_id)
    assert injected
    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_ACTIVATION_RECOVERY_REQUIRED"
    assert current != baseline
    assert (workspace / "CLAUDE.md").read_bytes() == b"# candidate must not overwrite the concurrent file\n"
    assert concurrent_file.read_bytes() == b"concurrent writer wins\n"
    assert saved is not None and saved.sdk_session_id == "merge-race-sdk"
    assert is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id="merge-race",
    )


def test_workspace_import_compensates_git_and_session_mapping_when_activation_commit_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {
            ".gitignore": b"*.secret\n",
            "CLAUDE.md": b"# candidate whose DB commit will fail\n",
        },
        agent_id="commit-race",
    )
    commit_failed = False
    with TestClient(module.app, raise_server_exceptions=False) as client:
        created = _import_new_agent(client, agent_id="commit-race", name="commit race")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        baseline = client.post("/api/agent-registry/commit-race/workspace/export").headers["x-agent-commit-sha"]
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        session = LocalSession(
            session_id="commit-race-session",
            sdk_session_id="commit-race-sdk",
            agent_id="commit-race",
            turns=1,
        )
        module.session_store.save(session)
        session_class = module.agent_governance.version_maintenance.session_factory.class_
        original_commit = session_class.commit

        def fail_commit_after_git_activation(db_session) -> None:
            nonlocal commit_failed
            current = _run_git(workspace, "rev-parse", "HEAD")
            if not commit_failed and current != baseline:
                commit_failed = True
                raise RuntimeError("injected activation commit failure")
            original_commit(db_session)

        monkeypatch.setattr(session_class, "commit", fail_commit_after_git_activation)
        response = client.post(
            "/api/agent-registry/commit-race/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=commit-race").json()["commit_sha"]

    saved = module.session_store.get(session.session_id)
    assert commit_failed
    assert response.status_code == 503
    assert response.json()["error_code"] == "WORKSPACE_ACTIVATION_METADATA_FAILED"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes
    assert saved is not None and saved.sdk_session_id == "commit-race-sdk"
    with module.agent_testing_store.Session() as db:
        records = list(db.scalars(select(AgentWorkspaceImportRecordModel).where(AgentWorkspaceImportRecordModel.agent_id == "commit-race")).all())
        operation = db.scalar(select(AgentWorkspaceActivationOperationModel).where(AgentWorkspaceActivationOperationModel.agent_id == "commit-race"))
    assert [(record.action, record.status) for record in records] == [
        ("created", "accepted"),
        ("overwrite", "failed"),
    ]
    assert operation is not None and operation.state == "rejected"
    assert operation.import_id == records[-1].import_id


def test_workspace_export_cleans_artifact_and_restores_dirty_state_when_release_is_lost(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="export-loss", name="export loss")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=export-loss").json()["commit_sha"]
        (workspace / "dirty.txt").write_bytes(b"preserve me\n")
        monkeypatch.setattr(agent_version_maintenance, "release_maintenance", lambda *_args, **_kwargs: False)
        response = client.post("/api/agent-registry/export-loss/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=export-loss").json()["commit_sha"]

    temporary_root = module.settings.data_dir / ".workspace-package-tmp"
    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_MAINTENANCE_CONFLICT"
    assert current == baseline
    assert (workspace / "dirty.txt").read_bytes() == b"preserve me\n"
    assert not temporary_root.exists() or not list(temporary_root.iterdir())
