from __future__ import annotations

import io
import json
import stat
import subprocess
import tarfile
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml
from agentscope.app import create_app as create_agentscope_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from app.runtime.agent_paths import business_agent_layout
from app.services import agent_workspace_package_codec as workspace_codec
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from business_agent_test_utils import create_test_business_agent_workspace
from runtime_loopback import serve_loopback
from workspace_package_test_utils import package_with_agent_id as _package_with_agent_id


def _workspace_package(
    files: dict[str, bytes],
    *,
    executable: frozenset[str] = frozenset(),
    agent_id: str | None = None,
) -> bytes:
    package_files = dict(files)
    if agent_id is not None and "agent.yaml" not in package_files:
        package_files["agent.yaml"] = _agent_manifest(agent_id, requires_web_hitl=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for path, content in sorted(package_files.items()):
            member = tarfile.TarInfo(f"workspace/{path}")
            member.size = len(content)
            member.mode = 0o755 if path in executable else 0o644
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _agent_manifest(agent_id: str, *, requires_web_hitl: bool) -> bytes:
    permission_mode = "default" if requires_web_hitl else "dont_ask"
    return (
        "schema_version: 1\n"
        "agent:\n"
        f"  id: {agent_id}\n"
        "  runtime: agentscope\n"
        "  runtime_contract: agentscope-app/2.0.8\n"
        "  system_prompt: AGENT.md\n"
        "session:\n"
        f"  permission_mode: {permission_mode}\n"
        "  cwd: .\n"
        "  model_profile: default\n"
        "workspace_policy:\n"
        "  fail_closed: true\n"
        "  immutable_harness: true\n"
        "  allow_for_run: false\n"
        f"  ask_tools: {['ReviewAction'] if requires_web_hitl else []}\n"
    ).encode()


def _import_new_agent(
    client: TestClient,
    *,
    agent_id: str,
    name: str,
    package: bytes | None = None,
    requires_web_hitl: bool = True,
):
    content = package or _workspace_package(
        {
            "AGENT.md": f"# {name}\n".encode(),
            "agent.yaml": _agent_manifest(agent_id, requires_web_hitl=requires_web_hitl),
        },
    )
    return client.post(
        f"/api/agent-registry/{agent_id}/workspace/import",
        data={"name": name},
        files={"package": (f"{agent_id}.tar.gz", content, "application/gzip")},
    )


def _seed_active_agent(
    module,
    *,
    agent_id: str,
    name: str,
    requires_web_hitl: bool = True,
) -> Path:
    """Create a real active Harness fixture without exercising package import."""

    layout = business_agent_layout(module.settings.data_dir, agent_id)
    create_test_business_agent_workspace(
        layout.workspace,
        agent_id=agent_id,
        name=name,
        requires_web_hitl=requires_web_hitl,
    )
    module.agent_registry_store.create_business_agent(
        name=name,
        agent_id=agent_id,
        workspace_dir=str(layout.workspace),
    )
    module.agent_governance._store_for(agent_id).ensure_bootstrap()
    return layout.workspace


def _candidate_workspace(module, response) -> Path:
    body = response.json()
    change_set = module.agent_governance.get_change_set(body["change_set_id"])
    assert change_set is not None
    assert change_set["candidate_commit_sha"] == body["candidate_commit_sha"]
    return Path(str(change_set["worktree_path"]))


def test_new_draft_create_change_set_failure_compensates_exact_registry_and_storage(
    process_environment,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services.agent_governance import AgentGovernanceError

    module = _load_app(process_environment, tmp_path)
    original = module.agent_governance.create_change_set

    def fail_create(*_args, **_kwargs):
        raise AgentGovernanceError(409, "injected change-set failure")

    monkeypatch.setattr(module.agent_governance, "create_change_set", fail_create)
    with TestClient(module.app) as client:
        failed = _import_new_agent(client, agent_id="create-failure-agent", name="创建失败 Agent")
        assert failed.status_code == 409, failed.text
        assert module.agent_registry_store.get_agent("create-failure-agent") is None
        assert not business_agent_layout(module.settings.data_dir, "create-failure-agent").root.exists()

        monkeypatch.setattr(module.agent_governance, "create_change_set", original)
        retried = _import_new_agent(client, agent_id="create-failure-agent", name="重试 Agent")
        assert retried.status_code == 200, retried.text


def test_new_draft_candidate_write_failure_abandons_change_set_and_compensates_storage(
    process_environment,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services.agent_candidate_writer import AgentCandidateWriteError, AgentCandidateWriter

    module = _load_app(process_environment, tmp_path)
    original = AgentCandidateWriter.write_entries

    def fail_write(self, **_kwargs):
        del self
        raise AgentCandidateWriteError(409, "injected candidate write failure")

    monkeypatch.setattr(AgentCandidateWriter, "write_entries", fail_write)
    with TestClient(module.app) as client:
        failed = _import_new_agent(client, agent_id="write-failure-agent", name="写入失败 Agent")
        assert failed.status_code == 409, failed.text
        assert module.agent_registry_store.get_agent("write-failure-agent") is None
        assert not business_agent_layout(module.settings.data_dir, "write-failure-agent").root.exists()
        change_sets = module.agent_governance.list_change_sets(agent_id="write-failure-agent")
        assert len(change_sets) == 1
        assert change_sets[0]["status"] == "abandoned"

        monkeypatch.setattr(AgentCandidateWriter, "write_entries", original)
        retried = _import_new_agent(client, agent_id="write-failure-agent", name="重试 Agent")
        assert retried.status_code == 200, retried.text


def _native_schema_runtime(tmp_path: Path):
    return create_agentscope_app(
        storage=AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:"),
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(tmp_path / "agentscope-workspaces"),
        knowledge_base_manager=None,
        enable_index_worker=False,
        enable_channel_worker=False,
        enable_scheduler=False,
        channels=[],
        mcp_hubs=[],
        skill_hubs=[],
    )


def _bind_runtime_client(module, runtime_url: str) -> None:
    module.runtime_client.base_url = runtime_url
    module.runtime_client._client.base_url = runtime_url


def _package_from_workspace(workspace: Path, *, overrides: dict[str, bytes]) -> bytes:
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if ".git" in relative.parts or not path.is_file():
            continue
        key = relative.as_posix()
        files[key] = path.read_bytes()
        if stat.S_IMODE(path.stat().st_mode) & 0o111:
            executable.add(key)
    files.update(overrides)
    return _workspace_package(files, executable=frozenset(executable))


def _run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _invalid_package(kind: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if kind == "traversal":
            member = tarfile.TarInfo("workspace/../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        elif kind == "symlink":
            member = tarfile.TarInfo("workspace/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
        elif kind == "directory-size":
            member = tarfile.TarInfo("workspace/non-empty-directory/")
            member.type = tarfile.DIRTYPE
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        elif kind == "file-prefix":
            parent = tarfile.TarInfo("workspace/a")
            parent.size = 1
            archive.addfile(parent, io.BytesIO(b"x"))
            child = tarfile.TarInfo("workspace/a/b")
            child.size = 1
            archive.addfile(child, io.BytesIO(b"y"))
        elif kind == "file-prefix-reversed":
            child = tarfile.TarInfo("workspace/a/b")
            child.size = 1
            archive.addfile(child, io.BytesIO(b"y"))
            parent = tarfile.TarInfo("workspace/a")
            parent.size = 1
            archive.addfile(parent, io.BytesIO(b"x"))
        elif kind == "surrogate":
            member = tarfile.TarInfo("workspace/\udcff")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        else:
            for content in (b"a", b"b"):
                member = tarfile.TarInfo("workspace/duplicate")
                member.size = 1
                archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _package_with_long_tar_metadata(path_bytes: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        member = tarfile.TarInfo(f"workspace/{'a' * path_bytes}")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def _package_with_metadata_chain(count: int, *, member_type: bytes = tarfile.XGLTYPE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(count):
            payload = _pax_record(str(index), "x") if member_type == tarfile.XGLTYPE else f"workspace/long-{index}\0".encode()
            member = tarfile.TarInfo(f"metadata-{index}")
            member.type = member_type
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
    return buffer.getvalue()


def _pax_record(key: str, value: str) -> bytes:
    body = f"{key}={value}\n".encode()
    length = len(body) + 3
    while True:
        record = str(length).encode() + b" " + body
        if len(record) == length:
            return record
        length = len(record)


def _package_with_empty_pax_path() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = _pax_record("path", "")
        metadata = tarfile.TarInfo("empty-path-metadata")
        metadata.type = tarfile.XHDTYPE
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))
        member = tarfile.TarInfo("workspace/fallback")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def _package_with_large_reversed_conflict(member_count: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for index in range(member_count - 2):
            member = tarfile.TarInfo(f"workspace/sibling-{index:05d}")
            member.size = 0
            archive.addfile(member, io.BytesIO())
        child = tarfile.TarInfo("workspace/conflict/child")
        child.size = 0
        archive.addfile(child, io.BytesIO())
        parent = tarfile.TarInfo("workspace/conflict")
        parent.size = 0
        archive.addfile(parent, io.BytesIO())
    return buffer.getvalue()


def _git_bytes(repository: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def _make_shared_blob_commit(repository: Path, paths: Iterable[str], content: bytes) -> str:
    repository.mkdir()
    _git_bytes(repository, ["init", "-q"])
    _git_bytes(repository, ["config", "user.name", "AgentGov Test"])
    _git_bytes(repository, ["config", "user.email", "agentgov-test@example.local"])
    object_id = _git_bytes(repository, ["hash-object", "-w", "--stdin"], input_bytes=content).strip()
    tree_input = b"".join(f"100644 blob {object_id.decode()}\t{path}\n".encode() for path in paths)
    tree_id = _git_bytes(repository, ["mktree"], input_bytes=tree_input).strip()
    return _git_bytes(repository, ["commit-tree", tree_id.decode(), "-m", "scale tree"]).decode().strip()


def _package_with_sparse_pax() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("workspace/sparse.bin")
        member.size = 1
        member.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def test_workspace_export_import_round_trip_preserves_binary_endpoint_and_env(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    source = _seed_active_agent(module, agent_id="source", name="source")
    with TestClient(module.app) as client:
        binary = b"\x00\x01endpoint=http://real.internal:9080\n"
        (source / "payload.bin").write_bytes(binary)
        script = source / "tools" / "raw-tool"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(b"#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        (source / ".gitignore").write_bytes(b".env\n*.secret\n")
        (source / ".gitattributes").write_bytes(b"export-hidden.txt export-ignore\nsubstituted.txt export-subst\n*.txt text eol=lf\n")
        (source / ".env").write_bytes(b"REAL_ENDPOINT=http://real.internal:9080\nTOKEN=workspace-owned\n")
        (source / "ignored.secret").write_bytes(b"ignored-but-workspace-owned\n")
        (source / "export-hidden.txt").write_bytes(b"must-still-export\n")
        (source / "substituted.txt").write_bytes(b"$Format:%H$\n")
        (source / "crlf.txt").write_bytes(b"first\r\nsecond\r\n")

        preflight = client.options(
            "/api/agent-registry/source/workspace/export",
            headers={
                "Origin": "http://localhost:50401",
                "Access-Control-Request-Method": "POST",
            },
        )
        exported = client.post(
            "/api/agent-registry/source/workspace/export",
            headers={"Origin": "http://localhost:50401"},
        )
        import_package = _package_with_agent_id(exported.content, "imported")
        imported = client.post(
            "/api/agent-registry/imported/workspace/import",
            data={"name": "imported"},
            files={"package": ("source-workspace.tar.gz", import_package, "application/gzip")},
        )

    assert exported.status_code == 200
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:50401"
    exposed = exported.headers["access-control-expose-headers"].lower()
    assert "content-disposition" in exposed
    assert "x-agent-commit-sha" in exposed
    assert "x-workspace-package-sha256" in exposed
    assert "x-workspace-tree-sha256" in exposed
    assert exported.headers["content-type"].startswith("application/gzip")
    assert len(exported.headers["x-agent-commit-sha"]) == 40
    assert len(exported.headers["x-workspace-package-sha256"]) == 64
    assert len(exported.headers["x-workspace-tree-sha256"]) == 64
    assert imported.status_code == 200
    body = imported.json()
    assert body["action"] == "created"
    assert body["published"] is False
    assert body["agent"]["status"] == "draft"
    assert body["candidate_commit_sha"] != body["base_commit_sha"]
    assert body["test_suite_status"] == "warning"
    assert body["test_file_count"] == 0
    assert {item["code"] for item in body["test_suite_warnings"]} == {"AGENT_TESTS_DIRECTORY_MISSING"}
    live_target = Path(body["agent"]["workspace_dir"])
    target = _candidate_workspace(module, imported)
    assert _run_git(live_target, "rev-parse", "HEAD") == body["base_commit_sha"]
    assert not (live_target / "payload.bin").exists()
    assert (target / "payload.bin").read_bytes() == binary
    assert (target / ".env").read_bytes() == b"REAL_ENDPOINT=http://real.internal:9080\nTOKEN=workspace-owned\n"
    assert (target / "ignored.secret").read_bytes() == b"ignored-but-workspace-owned\n"
    assert (target / "export-hidden.txt").read_bytes() == b"must-still-export\n"
    assert (target / "substituted.txt").read_bytes() == b"$Format:%H$\n"
    assert (target / "crlf.txt").read_bytes() == b"first\r\nsecond\r\n"
    assert (target / "tools" / "raw-tool").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert stat.S_IMODE((target / "tools" / "raw-tool").stat().st_mode) & 0o111
    assert "id: imported" in (target / "agent.yaml").read_text(encoding="utf-8")


def test_workspace_export_restores_exec_tracking_when_existing_git_disabled_filemode(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="filemode", name="filemode")
    with TestClient(module.app) as client:
        client.get("/api/agent-repository/current?agent_id=filemode")
        script = workspace / "tools" / "tracked-tool"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(b"#!/bin/sh\nexit 0\n")
        script.chmod(0o644)
        _run_git(workspace, "add", "-A", "-f", "--", ".")
        _run_git(workspace, "commit", "-m", "Track non-executable tool")
        previous = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "config", "core.fileMode", "false")
        script.chmod(0o755)

        exported = client.post("/api/agent-registry/filemode/workspace/export")
        import_package = _package_with_agent_id(exported.content, "filemode-copy")
        imported = client.post(
            "/api/agent-registry/filemode-copy/workspace/import",
            data={"name": "filemode copy"},
            files={"package": ("filemode.tar.gz", import_package, "application/gzip")},
        )

    assert exported.status_code == 200
    assert exported.headers["x-agent-commit-sha"] != previous
    assert _run_git(workspace, "config", "--bool", "core.fileMode") == "true"
    assert imported.json()["published"] is False
    imported_script = _candidate_workspace(module, imported) / "tools" / "tracked-tool"
    assert stat.S_IMODE(imported_script.stat().st_mode) & 0o111


def test_workspace_export_reads_many_large_blobs_through_real_git(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="batch-export", name="batch export")
    with TestClient(module.app) as client:
        for index in range(20):
            workspace.joinpath(f"blob-{index:02d}.bin").write_bytes(bytes([index]) * (96 * 1024 + index))
        exported = client.post("/api/agent-registry/batch-export/workspace/export")
        imported_package = _package_with_agent_id(exported.content, "batch-export-copy")
        imported = _import_new_agent(
            client,
            agent_id="batch-export-copy",
            name="batch export copy",
            package=imported_package,
        )

    assert exported.status_code == 200
    assert imported.status_code == 200
    imported_workspace = _candidate_workspace(module, imported)
    for index in range(20):
        assert imported_workspace.joinpath(f"blob-{index:02d}.bin").read_bytes() == bytes([index]) * (96 * 1024 + index)


def test_workspace_commit_reader_scales_to_ten_thousand_paths_with_one_real_git_batch_process(
    process_environment,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "scale-repository"
    commit_sha = _make_shared_blob_commit(
        repository,
        (f"file-{index:05d}.txt" for index in range(workspace_codec.MAX_PACKAGE_MEMBERS)),
        b"shared\n",
    )
    trace_path = tmp_path / "git-trace.jsonl"
    process_environment.set("GIT_TRACE2_EVENT", str(trace_path))
    entries = workspace_codec.read_commit_entries(repository, commit_sha, run_git=_git_bytes)
    starts = [json.loads(line)["argv"] for line in trace_path.read_text(encoding="utf-8").splitlines() if json.loads(line).get("event") == "start"]

    assert len(entries) == workspace_codec.MAX_PACKAGE_MEMBERS
    assert entries[0].content == b"shared\n"
    assert entries[-1].relative_path.as_posix() == "file-09999.txt"
    assert sum(arguments[:3] == ["git", "cat-file", "--batch"] for arguments in starts) == 1


def test_workspace_overwrite_requires_expected_commit_and_restore_creates_new_commit(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="target", name="target")
    package = _workspace_package(
        {"AGENT.md": b"# replacement\n", "binary.bin": b"\x00raw"},
        agent_id="target",
    )
    with TestClient(module.app) as client:
        baseline_text = (workspace / "AGENT.md").read_bytes()
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        (workspace / "stale.secret").write_bytes(b"must-be-deleted-by-replacement\n")
        _run_git(workspace, "add", "-A", "-f", "--", ".")
        _run_git(workspace, "commit", "-m", "Prepare live replacement baseline")
        baseline = client.get("/api/agent-repository/current?agent_id=target").json()["commit_sha"]

        missing_current = client.post(
            "/api/agent-registry/target/workspace/import",
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        stale_current = client.post(
            "/api/agent-registry/target/workspace/import",
            data={"expected_current_commit_sha": "0" * 40},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        overwritten = client.post(
            "/api/agent-registry/target/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("replacement.tar.gz", package, "application/gzip")},
        )
        overwrite_body = overwritten.json()
        candidate = _candidate_workspace(module, overwritten)
        candidate_replaced_tree = not (candidate / "stale.secret").exists()
        module.agent_governance.abandon_change_set(overwrite_body["change_set_id"], operator="test")
        historical = baseline
        (workspace / "AGENT.md").write_bytes(b"# current live\n")
        _run_git(workspace, "add", "-A", "-f", "--", ".")
        _run_git(workspace, "commit", "-m", "Advance live tree before restore candidate")
        current_before_restore = _run_git(workspace, "rev-parse", "HEAD")
        restored = client.post(
            "/api/agent-registry/target/workspace/restore",
            json={
                "target_commit_sha": historical,
                "expected_current_commit_sha": current_before_restore,
                "reason": "restore test baseline",
            },
        )

    assert missing_current.status_code == 422
    assert missing_current.json()["error_code"] == "WORKSPACE_IMPORT_CURRENT_REF_REQUIRED"
    assert stale_current.status_code == 409
    assert stale_current.json()["error_code"] == "CANDIDATE_BASE_CONFLICT"
    assert overwritten.status_code == 200
    assert overwrite_body["action"] == "candidate_committed"
    assert overwrite_body["published"] is False
    assert candidate_replaced_tree
    assert overwrite_body["base_commit_sha"] == baseline
    assert overwrite_body["candidate_commit_sha"] != baseline
    assert _run_git(workspace, "rev-parse", "HEAD") == current_before_restore
    assert (workspace / "AGENT.md").read_bytes() == b"# current live\n"
    assert (workspace / "stale.secret").read_bytes() == b"must-be-deleted-by-replacement\n"
    restore_body = restored.json()
    assert restored.status_code == 200
    assert restore_body["action"] == "candidate_committed"
    assert restore_body["published"] is False
    assert restore_body["restored_tree_commit_sha"] == historical
    assert restore_body["base_commit_sha"] == current_before_restore
    assert restore_body["candidate_commit_sha"] != current_before_restore
    restore_candidate = _candidate_workspace(module, restored)
    assert (restore_candidate / "AGENT.md").read_bytes() == baseline_text
    assert _run_git(workspace, "rev-parse", "HEAD") == current_before_restore


def test_workspace_restore_rejects_historical_non_regular_tree_without_changing_head(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="restore-guard", name="restore guard")
    with TestClient(module.app) as client:
        baseline = client.get("/api/agent-repository/current?agent_id=restore-guard").json()["commit_sha"]
        (workspace / "unsafe-link").symlink_to("AGENT.md")
        _run_git(workspace, "add", "-A", "--", ".")
        _run_git(workspace, "commit", "-m", "Historical unsafe symlink")
        unsafe_commit = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "reset", "--hard", baseline)

        response = client.post(
            "/api/agent-registry/restore-guard/workspace/restore",
            json={
                "target_commit_sha": unsafe_commit,
                "expected_current_commit_sha": baseline,
                "reason": "must reject unsafe historical tree",
            },
        )
        current = client.get("/api/agent-repository/current?agent_id=restore-guard").json()["commit_sha"]

    assert response.status_code == 422
    assert response.json()["error_code"] == "WORKSPACE_RESTORE_TARGET_INVALID"
    assert current == baseline
    assert not (workspace / "unsafe-link").exists()


def test_workspace_restore_rejects_actual_oversized_tree_without_changing_head(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="restore-failures", name="restore failures")
    with TestClient(module.app) as client:
        baseline_bytes = (workspace / "AGENT.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=restore-failures").json()["commit_sha"]

        with (workspace / "oversized.bin").open("wb") as oversized_file:
            oversized_file.seek(workspace_codec.MAX_SINGLE_MEMBER_BYTES)
            oversized_file.write(b"x")
        _run_git(workspace, "add", "-A", "--", ".")
        _run_git(workspace, "commit", "-m", "Historical oversized tree")
        oversized_commit = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "reset", "--hard", baseline)
        oversized = client.post(
            "/api/agent-registry/restore-failures/workspace/restore",
            json={
                "target_commit_sha": oversized_commit,
                "expected_current_commit_sha": baseline,
                "reason": "must reject oversized historical tree",
            },
        )

        current = client.get("/api/agent-repository/current?agent_id=restore-failures").json()["commit_sha"]

    assert oversized.status_code == 413
    assert oversized.json()["error_code"] == "WORKSPACE_RESTORE_TARGET_INVALID"
    assert current == baseline
    assert (workspace / "AGENT.md").read_bytes() == baseline_bytes


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [
        ("traversal", "WORKSPACE_PACKAGE_PATH_INVALID"),
        ("symlink", "WORKSPACE_PACKAGE_MEMBER_INVALID"),
        ("duplicate", "WORKSPACE_PACKAGE_DUPLICATE_MEMBER"),
        ("directory-size", "WORKSPACE_PACKAGE_MEMBER_INVALID"),
        ("file-prefix", "WORKSPACE_PACKAGE_PATH_CONFLICT"),
        ("file-prefix-reversed", "WORKSPACE_PACKAGE_PATH_CONFLICT"),
        ("surrogate", "WORKSPACE_PACKAGE_PATH_INVALID"),
    ],
)
def test_workspace_import_rejects_unsafe_tar_members_without_registering_agent(
    process_environment,
    tmp_path: Path,
    kind: str,
    error_code: str,
) -> None:
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        response = client.post(
            f"/api/agent-registry/unsafe-{kind}/workspace/import",
            data={"name": "unsafe"},
            files={"package": ("unsafe.tar.gz", _invalid_package(kind), "application/gzip")},
        )
        registered = {item["agent_id"] for item in client.get("/api/agent-registry").json()}

    assert response.status_code == 422
    assert response.json()["error_code"] == error_code
    assert f"unsafe-{kind}" not in registered


def test_native_candidate_source_projects_only_safe_live_git_fields(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="native-source", name="Native Source")
    manifest_path = workspace / "agent.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["agent"]["name"] = "Native Source"
    manifest["agent"]["provider_secret"] = "must-not-leak"
    manifest["context_config"] = {"trigger_ratio": 0.7}
    manifest["react_config"] = {"max_iters": 12}
    manifest["invite_config"] = {"invitable": False}
    manifest["mcp"] = {"headers": {"Authorization": "must-not-leak"}}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (workspace / ".env").write_text("PRIVATE_TOKEN=must-not-leak\n", encoding="utf-8")
    _run_git(workspace, "add", "-A", "-f", "--", ".")
    _run_git(workspace, "commit", "-m", "Prepare native source")
    expected_commit = _run_git(workspace, "rev-parse", "HEAD")

    with serve_loopback(_native_schema_runtime(tmp_path)) as runtime_url:
        _bind_runtime_client(module, runtime_url)
        with TestClient(module.app) as client:
            response = client.get("/api/agent-registry/native-source/native-candidate-source")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"agent_data", "change_set_id", "current_commit_sha"}
    assert body["change_set_id"] is None
    assert body["current_commit_sha"] == expected_commit
    agent_data = body["agent_data"]
    assert set(agent_data) == {"name", "system_prompt", "context_config", "react_config", "invite_config"}
    assert agent_data["name"] == "Native Source"
    assert agent_data["system_prompt"] == (workspace / "AGENT.md").read_text(encoding="utf-8")
    assert agent_data["context_config"]["trigger_ratio"] == 0.7
    assert agent_data["context_config"]["tool_result_limit"] == 50_000
    assert agent_data["react_config"]["max_iters"] == 12
    assert agent_data["react_config"]["structured_output_grace_iters"] == 5
    assert agent_data["invite_config"] == {"invitable": False, "invite_description": None}
    assert "must-not-leak" not in response.text
    assert "PRIVATE_TOKEN" not in response.text


def test_native_candidate_source_rejects_schema_drift_in_live_and_open_candidate(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace = _seed_active_agent(module, agent_id="native-drift", name="Native Drift")
    manifest_path = workspace / "agent.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["agent"]["name"] = "Native Drift"
    manifest["context_config"] = {"summary_schema": {"type": "object"}}
    manifest["react_config"] = {}
    manifest["invite_config"] = {}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    _run_git(workspace, "add", "-A", "--", ".")
    _run_git(workspace, "commit", "-m", "Introduce native schema drift")

    with serve_loopback(_native_schema_runtime(tmp_path)) as runtime_url:
        _bind_runtime_client(module, runtime_url)
        with TestClient(module.app) as client:
            drift = client.get("/api/agent-registry/native-drift/native-candidate-source")
            created = _import_new_agent(client, agent_id="native-draft", name="Native Draft")
            draft = client.get("/api/agent-registry/native-draft/native-candidate-source")

    assert drift.status_code == 409
    assert drift.json()["error_code"] == "NATIVE_AGENT_SOURCE_INVALID"
    assert created.status_code == 200
    assert created.json()["agent"]["status"] == "draft"
    assert draft.status_code == 409
    assert draft.json()["error_code"] == "NATIVE_AGENT_SOURCE_INVALID"


def test_native_agent_form_creates_only_a_draft_candidate_and_rejects_backend_fields(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    request = {
        "agent_data": {
            "name": "Native Form",
            "system_prompt": "Review governed evidence.",
            "context_config": {},
            "react_config": {},
            "invite_config": {"invitable": False},
        }
    }
    polluted = {
        "agent_data": {
            **request["agent_data"],
            "id": "backend-owned",
        }
    }

    with serve_loopback(_native_schema_runtime(tmp_path)) as runtime_url:
        _bind_runtime_client(module, runtime_url)
        with TestClient(module.app) as client:
            response = client.post("/api/agent-registry/native-form/native-candidate", json=request)
            rejected = client.post("/api/agent-registry/native-polluted/native-candidate", json=polluted)

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "created"
    assert body["agent"]["status"] == "draft"
    assert body["published"] is False
    candidate = _candidate_workspace(module, response)
    live = Path(body["agent"]["workspace_dir"])
    generated_test = "tests/test_native_agent_harness_contract.py"
    assert {"tests/README.md", generated_test} <= set(body["changed_paths"])
    assert (candidate / "AGENT.md").read_text(encoding="utf-8") == "Review governed evidence."
    assert (candidate / "agent.yaml").is_file()
    assert (candidate / generated_test).is_file()
    assert "def test_" in _run_git(live, "show", f"{body['candidate_commit_sha']}:{generated_test}")
    assert not (live / "AGENT.md").exists()
    assert _run_git(live, "rev-parse", "HEAD") == body["base_commit_sha"]
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{body['base_commit_sha']}:{generated_test}"],
            cwd=live,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    assert rejected.status_code == 422
    assert module.agent_registry_store.get_agent("native-polluted") is None


def test_workspace_package_openapi_documents_binary_multipart_and_export_receipt_headers(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    schema = module.app.openapi()
    import_operation = schema["paths"]["/api/agent-registry/{agent_id}/workspace/import"]["post"]
    request_body = import_operation["requestBody"]
    assert request_body["required"] is True
    assert set(request_body["content"]) == {"multipart/form-data"}
    multipart_schema = request_body["content"]["multipart/form-data"]["schema"]
    assert multipart_schema["type"] == "object"
    assert multipart_schema["required"] == ["package"]
    assert set(multipart_schema["properties"]) == {"package", "name", "expected_current_commit_sha", "reason"}
    assert multipart_schema["properties"]["package"]["type"] == "string"
    assert multipart_schema["properties"]["package"]["format"] == "binary"
    assert {"200", "400", "404", "409", "422"} <= set(import_operation["responses"])

    export_operation = schema["paths"]["/api/agent-registry/{agent_id}/workspace/export"]["post"]
    assert {"200", "400", "404", "409"} <= set(export_operation["responses"])
    response_headers = export_operation["responses"]["200"]["headers"]
    assert set(response_headers) == {
        "Content-Disposition",
        "X-Agent-Commit-SHA",
        "X-Workspace-Package-SHA256",
        "X-Workspace-Tree-SHA256",
    }
    restore_operation = schema["paths"]["/api/agent-registry/{agent_id}/workspace/restore"]["post"]
    assert {"200", "400", "404", "409", "422"} <= set(restore_operation["responses"])
    source_operation = schema["paths"]["/api/agent-registry/{agent_id}/native-candidate-source"]["get"]
    source_schema = source_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert source_schema == {"$ref": "#/components/schemas/NativeAgentCandidateSourceResponse"}
