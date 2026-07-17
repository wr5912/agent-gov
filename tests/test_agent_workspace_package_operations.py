from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from app.runtime.agent_admission import AgentMaintenanceClaimLost
from app.runtime.agent_git_raw_storage import RawGitStorageError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.session_store import LocalSession
from app.services import agent_version_maintenance
from app.services import agent_workspace_package_codec as workspace_codec
from app.services import agent_workspace_packages as workspace_packages
from app.services.agent_governance import AgentGovernanceError
from fastapi.testclient import TestClient

from test_agent_workspace_packages import (
    _load_app,
    _package_with_empty_pax_path,
    _package_with_large_reversed_conflict,
    _package_with_long_tar_metadata,
    _package_with_metadata_chain,
    _package_with_sparse_pax,
    _run_git,
    _workspace_package,
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
        created = client.post("/api/agent-registry", json={"name": "busy", "agent_id": "busy"})
        assert created.status_code == 201
        workspace = Path(created.json()["workspace_dir"])
        assert not (workspace / ".git").exists()
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
    assert not (workspace / ".git").exists()


def test_workspace_package_checks_all_open_change_sets_without_list_window(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/agent-registry", json={"name": "open-set", "agent_id": "open-set"}).status_code == 201
        module.agent_governance.create_change_set(agent_id="open-set", title="must block package operations")
        monkeypatch.setattr(module.agent_governance, "list_change_sets", lambda **_kwargs: [])
        response = client.post("/api/agent-registry/open-set/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_CHANGE_SET_ACTIVE"


def test_workspace_git_bootstrap_failure_is_structured(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/agent-registry", json={"name": "git-failure", "agent_id": "git-failure"}).status_code == 201

        def fail_bootstrap(_store: GitAgentVersionStore):
            raise AgentGitError(f"fatal: cannot read {tmp_path}/private-workspace")

        monkeypatch.setattr(GitAgentVersionStore, "ensure_bootstrap", fail_bootstrap)
        response = client.post("/api/agent-registry/git-failure/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert response.json()["detail"] == "Git workspace operation failed"
    assert str(tmp_path) not in response.text


def test_new_agent_import_compensates_git_and_registry_when_finalize_fails(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module.agent_registry_store,
        "finalize_business_agent",
        lambda _reservation: (_ for _ in ()).throw(RuntimeError("injected finalize failure")),
    )
    package = _workspace_package({"CLAUDE.md": b"# imported\n", ".mcp.json": b'{"mcpServers": {}}\n'})
    with TestClient(module.app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/agent-registry/finalize-failure/workspace/import",
            data={"name": "failure"},
            files={"package": ("failure.tar.gz", package, "application/gzip")},
        )

    layout = business_agent_layout(module.settings.data_dir, "finalize-failure")
    assert response.status_code == 500
    assert module.agent_registry_store.get_agent("finalize-failure") is None
    assert not layout.workspace.exists()
    assert not layout.version_base.exists()


def test_workspace_import_rejects_invalid_configs_and_size_limits_before_mutation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
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
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace_codec, "MAX_PACKAGE_MEMBERS", 2)
            at_member_limit = client.post(
                "/api/agent-registry/at-member-limit/workspace/import",
                data={"name": "at limit"},
                files={"package": ("at-limit.tar.gz", _workspace_package({"a": b"1", "b": b"2"}), "application/gzip")},
            )
            over_member_limit = client.post(
                "/api/agent-registry/over-member-limit/workspace/import",
                data={"name": "over limit"},
                files={"package": ("over-limit.tar.gz", _workspace_package({"a": b"1", "b": b"2", "c": b"3"}), "application/gzip")},
            )
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace_codec, "MAX_TAR_METADATA_BYTES", 1024)
            oversized_metadata = client.post(
                "/api/agent-registry/oversized-metadata/workspace/import",
                data={"name": "oversized metadata"},
                files={
                    "package": (
                        "oversized-metadata.tar.gz",
                        _package_with_long_tar_metadata(2048),
                        "application/gzip",
                    )
                },
            )
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace_codec, "MAX_CONSECUTIVE_TAR_METADATA", 4)
            metadata_chain = client.post(
                "/api/agent-registry/metadata-chain/workspace/import",
                data={"name": "metadata chain"},
                files={
                    "package": (
                        "metadata-chain.tar.gz",
                        _package_with_metadata_chain(5),
                        "application/gzip",
                    )
                },
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
        created = client.post("/api/agent-registry", json={"name": "export-guard", "agent_id": "export-guard"})
        workspace = Path(created.json()["workspace_dir"])
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
        created = client.post("/api/agent-registry", json={"name": "empty export", "agent_id": "empty-export"})
        workspace = Path(created.json()["workspace_dir"])
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
        created = client.post("/api/agent-registry", json={"name": "raw path", "agent_id": "raw-path"})
        workspace = Path(created.json()["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=raw-path").json()["commit_sha"]
        monkeypatch.setattr(
            workspace_packages,
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
    original_git = workspace_packages._git
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "commit failure", "agent_id": "commit-failure"})
        workspace = Path(created.json()["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=commit-failure").json()["commit_sha"]
        original_content = (workspace / "CLAUDE.md").read_bytes()
        changed_content = original_content + b"\n# dirty before failed export\n"
        (workspace / "CLAUDE.md").write_bytes(changed_content)

        def fail_snapshot_commit(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            if args[:3] == ["commit", "-m", "Snapshot live workspace before package operation"]:
                raise workspace_packages._GitCommandError(f"cannot commit {tmp_path}/private-workspace")
            return original_git(repository, args, check=check)

        monkeypatch.setattr(workspace_packages, "_git", fail_snapshot_commit)
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
    package = _workspace_package({"CLAUDE.md": b"# replacement\n"})
    calls = 0
    original_assert = agent_version_maintenance.AgentVersionMaintenanceLease.assert_active

    def fail_second_assert(lease) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AgentMaintenanceClaimLost("injected lease loss before merge")
        original_assert(lease)

    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "lease-target", "agent_id": "lease-target"})
        workspace = Path(created.json()["workspace_dir"])
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
    package = _workspace_package({"CLAUDE.md": b"# applied despite late release loss\n"})
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "late-loss", "agent_id": "late-loss"})
        workspace = Path(created.json()["workspace_dir"])
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


def test_workspace_import_does_not_activate_when_sdk_session_invalidation_fails(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package({"CLAUDE.md": b"# must not activate\n"})
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "invalidate-failure", "agent_id": "invalidate-failure"})
        workspace = Path(created.json()["workspace_dir"])
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
    package = _workspace_package({"CLAUDE.md": b"# must not activate across a dirty race\n"})
    original_invalidation = module.session_store.clear_inactive_sdk_sessions_for_agent_in_transaction
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "dirty race", "agent_id": "dirty-race"})
        workspace = Path(created.json()["workspace_dir"])
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
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

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_DIRTY_CONFLICT"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes
    assert concurrent_file.read_bytes() == b"preserve concurrent writer\n"


def test_workspace_import_does_not_overwrite_ignored_file_created_at_merge(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {
            ".gitignore": b"*.secret\n",
            "CLAUDE.md": b"# candidate must not overwrite the concurrent file\n",
            "collision.secret": b"candidate bytes\n",
        }
    )
    original_git = workspace_packages._git
    injected = False
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "merge race", "agent_id": "merge-race"})
        workspace = Path(created.json()["workspace_dir"])
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        baseline = client.post("/api/agent-registry/merge-race/workspace/export").headers["x-agent-commit-sha"]
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        concurrent_file = workspace / "collision.secret"
        session = LocalSession(
            session_id="merge-race-session",
            sdk_session_id="merge-race-sdk",
            agent_id="merge-race",
            turns=1,
        )
        module.session_store.save(session)

        def inject_ignored_file_before_merge(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            nonlocal injected
            if args[:3] == ["merge", "--ff-only", "--no-overwrite-ignore"]:
                injected = True
                concurrent_file.write_bytes(b"concurrent writer wins\n")
            return original_git(repository, args, check=check)

        monkeypatch.setattr(workspace_packages, "_git", inject_ignored_file_before_merge)
        response = client.post(
            "/api/agent-registry/merge-race/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=merge-race").json()["commit_sha"]

    saved = module.session_store.get(session.session_id)
    assert injected
    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes
    assert concurrent_file.read_bytes() == b"concurrent writer wins\n"
    assert saved is not None and saved.sdk_session_id == "merge-race-sdk"


def test_workspace_import_compensates_git_and_session_mapping_when_activation_commit_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {
            ".gitignore": b"*.secret\n",
            "CLAUDE.md": b"# candidate whose DB commit will fail\n",
        }
    )
    commit_failed = False
    with TestClient(module.app, raise_server_exceptions=False) as client:
        created = client.post("/api/agent-registry", json={"name": "commit race", "agent_id": "commit-race"})
        workspace = Path(created.json()["workspace_dir"])
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        baseline = client.post("/api/agent-registry/commit-race/workspace/export").headers["x-agent-commit-sha"]
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        concurrent_file = workspace / "preserve.secret"
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
                concurrent_file.write_bytes(b"preserve across compensation\n")
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
    assert response.status_code == 500
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes
    assert concurrent_file.read_bytes() == b"preserve across compensation\n"
    assert saved is not None and saved.sdk_session_id == "commit-race-sdk"


def test_workspace_export_cleans_artifact_and_restores_dirty_state_when_release_is_lost(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "export-loss", "agent_id": "export-loss"})
        workspace = Path(created.json()["workspace_dir"])
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
