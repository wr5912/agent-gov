from __future__ import annotations

import io
import shlex
import shutil
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import AgentAdmissionStateModel
from app.services import agent_workspace_package_codec as workspace_codec
from app.services.agent_governance import AgentGovernanceError
from fastapi.testclient import TestClient
from sqlalchemy import text

from app_test_utils import load_test_app as _load_app
from test_agent_workspace_packages import (
    _package_with_empty_pax_path,
    _package_with_large_reversed_conflict,
    _package_with_long_tar_metadata,
    _package_with_metadata_chain,
    _package_with_sparse_pax,
    _run_git,
    _seed_active_agent,
    _workspace_package,
)


def _install_sqlite_trigger(module, statement: str) -> None:
    with module.runtime_db_session_factory.begin() as db:
        db.execute(text(statement))


def _write_git_hook(workspace: Path, name: str, body: str) -> None:
    hook = workspace / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    hook.chmod(0o755)


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for filesystem signal: {path.name}")


def test_workspace_import_rejects_missing_or_invalid_http_multipart_contract(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    package = _workspace_package({"AGENT.md": b"# multipart\n"})
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


def test_workspace_package_operation_conflicts_with_active_agent_maintenance(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="busy", name="busy")
    with TestClient(module.app) as client:
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


def test_workspace_package_rejects_real_open_change_set(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    _seed_active_agent(module, agent_id="open-set", name="open-set")
    with TestClient(module.app) as client:
        module.agent_governance.create_change_set(agent_id="open-set", title="must block package operations")
        response = client.post("/api/agent-registry/open-set/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_CHANGE_SET_ACTIVE"


def test_workspace_git_bootstrap_failure_is_structured(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="git-failure", name="git-failure")
    with TestClient(module.app) as client:
        shutil.rmtree(workspace / ".git")
        (workspace / ".git").write_text(f"gitdir: {tmp_path}/private-workspace\n", encoding="utf-8")
        response = client.post("/api/agent-registry/git-failure/workspace/export")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert response.json()["detail"] == "Git workspace operation failed"
    assert str(tmp_path) not in response.text


def test_new_agent_import_compensates_git_and_registry_when_sqlite_finalize_fails(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    _install_sqlite_trigger(
        module,
        """
        CREATE TRIGGER reject_workspace_finalize
        BEFORE UPDATE OF provision_state ON agent_registry
        WHEN OLD.agent_id = 'finalize-failure' AND NEW.provision_state = 'ready'
        BEGIN
          SELECT RAISE(ABORT, 'finalize blocked by database');
        END
        """,
    )
    package = _workspace_package(
        {"AGENT.md": b"# imported\n"},
        agent_id="finalize-failure",
    )
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


def test_workspace_import_rejects_invalid_configs_and_real_size_limits_before_mutation(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    oversized_member_package = _workspace_package({"oversized.bin": b"x" * (workspace_codec.MAX_SINGLE_MEMBER_BYTES + 1)})
    shared_large_content = b"x" * workspace_codec.MAX_SINGLE_MEMBER_BYTES
    oversized_total_package = _workspace_package({f"part-{index}.bin": shared_large_content for index in range(5)})
    over_member_limit_package = _workspace_package({f"member-{index:05d}": b"" for index in range(workspace_codec.MAX_PACKAGE_MEMBERS + 1)})
    with TestClient(module.app) as client:
        invalid_json = client.post(
            "/api/agent-registry/invalid-json/workspace/import",
            data={"name": "invalid"},
            files={"package": ("invalid.tar.gz", _workspace_package({"mcp/invalid.json": b"[]"}), "application/gzip")},
        )
        oversized_member = client.post(
            "/api/agent-registry/oversized-member/workspace/import",
            data={"name": "oversized"},
            files={"package": ("oversized.tar.gz", oversized_member_package, "application/gzip")},
        )
        oversized_total = client.post(
            "/api/agent-registry/oversized-total/workspace/import",
            data={"name": "oversized"},
            files={"package": ("oversized.tar.gz", oversized_total_package, "application/gzip")},
        )
        rejected_before_parse = client.post(
            "/api/agent-registry/request-too-large/workspace/import",
            content=b"not-a-multipart-body",
            headers={
                "Content-Type": "multipart/form-data; boundary=unused",
                "Content-Length": str(workspace_codec.MAX_MULTIPART_REQUEST_BYTES + 1),
            },
        )
        over_member_limit = client.post(
            "/api/agent-registry/over-member-limit/workspace/import",
            data={"name": "over limit"},
            files={"package": ("over-limit.tar.gz", over_member_limit_package, "application/gzip")},
        )
        oversized_metadata = client.post(
            "/api/agent-registry/oversized-metadata/workspace/import",
            data={"name": "oversized metadata"},
            files={
                "package": (
                    "oversized-metadata.tar.gz",
                    _package_with_long_tar_metadata(workspace_codec.MAX_TAR_METADATA_BYTES + 1),
                    "application/gzip",
                )
            },
        )
        metadata_chain = client.post(
            "/api/agent-registry/metadata-chain/workspace/import",
            data={"name": "metadata chain"},
            files={
                "package": (
                    "metadata-chain.tar.gz",
                    _package_with_metadata_chain(workspace_codec.MAX_CONSECUTIVE_TAR_METADATA + 1),
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
                    _package_with_metadata_chain(
                        workspace_codec.MAX_CONSECUTIVE_TAR_METADATA + 1,
                        member_type=tarfile.GNUTYPE_LONGNAME,
                    ),
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


def test_workspace_export_rejects_symlink_and_actual_oversized_tree_without_advancing_head(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="export-guard", name="export guard")
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=export-guard").json()["commit_sha"]
        (workspace / "linked").symlink_to("AGENT.md")
        symlinked = client.post("/api/agent-registry/export-guard/workspace/export")
        after_symlink = client.get("/api/agent-repository/current?agent_id=export-guard").json()["commit_sha"]
        (workspace / "linked").unlink()
        with (workspace / "oversized.bin").open("wb") as oversized_file:
            oversized_file.seek(workspace_codec.MAX_SINGLE_MEMBER_BYTES)
            oversized_file.write(b"x")
        oversized = client.post("/api/agent-registry/export-guard/workspace/export")

    assert symlinked.status_code == 422
    assert symlinked.json()["error_code"] == "WORKSPACE_EXPORT_TREE_INVALID"
    assert after_symlink == baseline
    assert oversized.status_code == 413


def test_workspace_export_snapshots_deletion_when_only_git_metadata_remains(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="empty-export", name="empty export")
    with TestClient(module.app) as client:
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


def test_workspace_export_reports_real_invalid_attributes_path_without_advancing_head(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="raw-path", name="raw path")
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=raw-path").json()["commit_sha"]
        attributes = workspace / ".git" / "info" / "attributes"
        attributes.unlink()
        attributes.mkdir()
        response = client.post("/api/agent-registry/raw-path/workspace/export")
        current = _run_git(workspace, "rev-parse", "HEAD")

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert current == baseline
    assert _run_git(workspace, "status", "--porcelain") == ""


def test_workspace_export_unstages_original_dirty_state_when_real_git_hook_rejects_commit(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="commit-failure", name="commit failure")
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=commit-failure").json()["commit_sha"]
        original_content = (workspace / "AGENT.md").read_bytes()
        changed_content = original_content + b"\n# dirty before failed export\n"
        (workspace / "AGENT.md").write_bytes(changed_content)
        _write_git_hook(workspace, "pre-commit", "exit 23\n")
        response = client.post("/api/agent-registry/commit-failure/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=commit-failure").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_GIT_OPERATION_FAILED"
    assert response.json()["detail"] == "Git workspace operation failed"
    assert str(tmp_path) not in response.text
    assert current == baseline
    assert (workspace / "AGENT.md").read_bytes() == changed_content
    assert subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workspace, check=False).returncode == 0
    assert subprocess.run(["git", "diff", "--quiet"], cwd=workspace, check=False).returncode == 1


def test_workspace_import_rechecks_real_sqlite_lease_before_candidate_receipt(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="lease-target", name="lease target")
    package = _workspace_package({"AGENT.md": b"# replacement\n"}, agent_id="lease-target")
    with TestClient(module.app) as client:
        baseline_bytes = (workspace / "AGENT.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=lease-target").json()["commit_sha"]
        hook_entered = tmp_path / "lease-candidate-hook-entered"
        release_hook = tmp_path / "release-lease-candidate-hook"
        _write_git_hook(
            workspace,
            "pre-commit",
            (f"touch {shlex.quote(str(hook_entered))}\nwhile [ ! -f {shlex.quote(str(release_hook))} ]; do sleep 0.01; done\n"),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                client.post,
                "/api/agent-registry/lease-target/workspace/import",
                data={"expected_current_commit_sha": baseline},
                files={"package": ("replacement.tar.gz", package, "application/gzip")},
            )
            try:
                _wait_for_path(hook_entered)
                with module.runtime_db_session_factory.begin() as db:
                    state = db.get(AgentAdmissionStateModel, "lease-target")
                    assert state is not None and state.maintenance_token
                    state.maintenance_token = None
                    state.maintenance_kind = None
                    state.maintenance_owner_id = None
                    state.maintenance_expires_at = None
                release_hook.touch()
                response = request.result(timeout=5)
            finally:
                release_hook.touch()
        current = client.get("/api/agent-repository/current?agent_id=lease-target").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "CANDIDATE_WRITE_FAILED"
    assert current == baseline
    assert (workspace / "AGENT.md").read_bytes() == baseline_bytes
    change_sets = module.agent_governance.list_change_sets(agent_id="lease-target")
    assert len(change_sets) == 1
    assert change_sets[0]["status"] == "abandoned"


def test_workspace_import_reports_recovery_pending_when_candidate_lease_release_is_lost(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="late-loss", name="late loss")
    package = _workspace_package(
        {"AGENT.md": b"# applied despite late release loss\n"},
        agent_id="late-loss",
    )
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=late-loss").json()["commit_sha"]
        _install_sqlite_trigger(
            module,
            """
            CREATE TRIGGER ignore_late_workspace_release
            BEFORE UPDATE OF maintenance_token ON agent_admission_states
            WHEN OLD.agent_id = 'late-loss'
              AND OLD.maintenance_token IS NOT NULL
              AND NEW.maintenance_token IS NULL
            BEGIN
              SELECT RAISE(IGNORE);
            END
            """,
        )
        response = client.post(
            "/api/agent-registry/late-loss/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "CANDIDATE_RECOVERY_PENDING"
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline
    assert (workspace / "AGENT.md").read_bytes() != b"# applied despite late release loss\n"
    change_sets = module.agent_governance.list_change_sets(agent_id="late-loss")
    assert len(change_sets) == 1
    assert change_sets[0]["status"] == "draft"
    candidate = Path(str(change_sets[0]["worktree_path"]))
    assert (candidate / "AGENT.md").read_bytes() == (workspace / "AGENT.md").read_bytes()


def test_concurrent_workspace_import_creates_exactly_one_candidate(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="candidate-race", name="candidate race")
    baseline = _run_git(workspace, "rev-parse", "HEAD")
    package = _workspace_package({"AGENT.md": b"# one candidate\n"}, agent_id="candidate-race")

    with TestClient(module.app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        requests = [
            executor.submit(
                client.post,
                "/api/agent-registry/candidate-race/workspace/import",
                data={"expected_current_commit_sha": baseline},
                files={"package": ("replacement.tar.gz", package, "application/gzip")},
            )
            for _ in range(2)
        ]
        responses = [request.result(timeout=10) for request in requests]

    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error_code"] in {"CANDIDATE_CHANGE_SET_ACTIVE", "CANDIDATE_CHANGE_SET_FAILED"}
    change_sets = module.agent_governance.list_change_sets(agent_id="candidate-race")
    assert len(change_sets) == 1
    assert change_sets[0]["candidate_commit_sha"]
    assert _run_git(workspace, "rev-parse", "HEAD") == baseline


def test_workspace_export_cleans_artifact_and_restores_dirty_state_when_sqlite_release_is_lost(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="export-loss", name="export loss")
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=export-loss").json()["commit_sha"]
        (workspace / "dirty.txt").write_bytes(b"preserve me\n")
        _install_sqlite_trigger(
            module,
            """
            CREATE TRIGGER ignore_workspace_export_release
            BEFORE UPDATE OF maintenance_token ON agent_admission_states
            WHEN OLD.agent_id = 'export-loss'
              AND OLD.maintenance_token IS NOT NULL
              AND NEW.maintenance_token IS NULL
            BEGIN
              SELECT RAISE(IGNORE);
            END
            """,
        )
        response = client.post("/api/agent-registry/export-loss/workspace/export")
        current = client.get("/api/agent-repository/current?agent_id=export-loss").json()["commit_sha"]

    temporary_root = module.settings.data_dir / ".workspace-package-tmp"
    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_MAINTENANCE_CONFLICT"
    assert current == baseline
    assert (workspace / "dirty.txt").read_bytes() == b"preserve me\n"
    assert not temporary_root.exists() or not list(temporary_root.iterdir())
