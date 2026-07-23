from __future__ import annotations

import asyncio
import io
import stat
import subprocess
import tarfile
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest
from app.runtime.runtime_db import SessionTurnIntentModel
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSession
from app.services import agent_workspace_package_codec as workspace_codec
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import package_with_agent_id as _package_with_agent_id


def _workspace_package(
    files: dict[str, bytes],
    *,
    executable: frozenset[str] = frozenset(),
    agent_id: str | None = None,
) -> bytes:
    package_files = dict(files)
    if agent_id is not None and "agent.yaml" not in package_files:
        package_files["agent.yaml"] = f"agent:\n  id: {agent_id}\n".encode()
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


def _import_new_agent(
    client: TestClient,
    *,
    agent_id: str,
    name: str,
    package: bytes | None = None,
):
    content = package or _workspace_package(
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


def test_workspace_export_import_round_trip_preserves_binary_endpoint_and_env(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="source", name="source")
        source = Path(created.json()["agent"]["workspace_dir"])
        binary = b"\x00\x01endpoint=http://real.internal:9080\n"
        (source / "payload.bin").write_bytes(binary)
        script = source / "hooks" / "raw-tool"
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
                "Origin": "http://localhost:55173",
                "Access-Control-Request-Method": "POST",
            },
        )
        exported = client.post(
            "/api/agent-registry/source/workspace/export",
            headers={"Origin": "http://localhost:55173"},
        )
        import_package = _package_with_agent_id(exported.content, "imported")
        imported = client.post(
            "/api/agent-registry/imported/workspace/import",
            data={"name": "imported"},
            files={"package": ("source-workspace.tar.gz", import_package, "application/gzip")},
        )

    assert exported.status_code == 200
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:55173"
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
    assert body["activation_mode"] == "next_turn"
    assert body["test_suite_status"] == "warning"
    assert body["test_file_count"] == 0
    assert {item["code"] for item in body["test_suite_warnings"]} == {"AGENT_TESTS_DIRECTORY_MISSING"}
    target = Path(body["agent"]["workspace_dir"])
    assert (target / "payload.bin").read_bytes() == binary
    assert (target / ".env").read_bytes() == b"REAL_ENDPOINT=http://real.internal:9080\nTOKEN=workspace-owned\n"
    assert (target / "ignored.secret").read_bytes() == b"ignored-but-workspace-owned\n"
    assert (target / "export-hidden.txt").read_bytes() == b"must-still-export\n"
    assert (target / "substituted.txt").read_bytes() == b"$Format:%H$\n"
    assert (target / "crlf.txt").read_bytes() == b"first\r\nsecond\r\n"
    assert (target / "hooks" / "raw-tool").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert stat.S_IMODE((target / "hooks" / "raw-tool").stat().st_mode) & 0o111
    assert (target / "agent.yaml").read_bytes() == b"agent:\n  id: imported\n"


def test_workspace_export_restores_exec_tracking_when_existing_git_disabled_filemode(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="filemode", name="filemode")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        client.get("/api/agent-repository/current?agent_id=filemode")
        script = workspace / "hooks" / "tracked-tool"
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
    imported_script = Path(imported.json()["agent"]["workspace_dir"]) / "hooks" / "tracked-tool"
    assert stat.S_IMODE(imported_script.stat().st_mode) & 0o111


def test_workspace_export_reads_many_large_blobs_with_one_batch_process(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    original_popen = workspace_codec.subprocess.Popen
    batch_calls = 0

    def counted_popen(*args, **kwargs):
        nonlocal batch_calls
        command = args[0] if args else kwargs.get("args")
        if command[:3] == ["git", "cat-file", "--batch"]:
            batch_calls += 1
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(workspace_codec.subprocess, "Popen", counted_popen)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="batch-export", name="batch export")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        for index in range(20):
            workspace.joinpath(f"blob-{index:02d}.bin").write_bytes(bytes([index]) * (96 * 1024 + index))
        exported = client.post("/api/agent-registry/batch-export/workspace/export")

    assert exported.status_code == 200
    assert batch_calls == 1


def test_workspace_commit_reader_scales_to_ten_thousand_paths_with_one_batch_process(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "scale-repository"
    commit_sha = _make_shared_blob_commit(
        repository,
        (f"file-{index:05d}.txt" for index in range(workspace_codec.MAX_PACKAGE_MEMBERS)),
        b"shared\n",
    )
    original_popen = workspace_codec.subprocess.Popen
    batch_calls = 0

    def counted_popen(*args, **kwargs):
        nonlocal batch_calls
        command = args[0] if args else kwargs.get("args")
        if command[:3] == ["git", "cat-file", "--batch"]:
            batch_calls += 1
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(workspace_codec.subprocess, "Popen", counted_popen)
    entries = workspace_codec.read_commit_entries(repository, commit_sha, run_git=_git_bytes)

    assert len(entries) == workspace_codec.MAX_PACKAGE_MEMBERS
    assert entries[0].content == b"shared\n"
    assert entries[-1].relative_path.as_posix() == "file-09999.txt"
    assert batch_calls == 1


def test_workspace_batch_reader_spools_stderr_without_exposing_repository_path(monkeypatch, tmp_path: Path) -> None:
    object_id = b"a" * 40

    class FailedBatchProcess:
        def __init__(self, stderr) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(object_id + b" blob 1\nx\n")
            self.returncode = None
            stderr.write(f"fatal: cannot read {tmp_path}/private-object\n".encode())

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 7
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(workspace_codec.subprocess, "Popen", lambda *args, **kwargs: FailedBatchProcess(kwargs["stderr"]))
    spec = workspace_codec._CommitBlobSpec(
        relative_path=PurePosixPath("file.txt"),
        mode=0o644,
        object_id=object_id,
        size=1,
    )

    with pytest.raises(workspace_codec.WorkspaceGitReadError) as exc_info:
        workspace_codec._read_commit_blob_contents(tmp_path, (spec,))

    assert "exit code 7" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)


def test_workspace_commit_reader_rejects_empty_raw_tree_path_before_blob_read(tmp_path: Path) -> None:
    raw_tree = b"100644 blob " + b"a" * 40 + b" 1\t\0"

    with pytest.raises(workspace_codec.WorkspacePackageError) as exc_info:
        workspace_codec.read_commit_entries(tmp_path, "a" * 40, run_git=lambda _repository, _args: raw_tree)

    assert exc_info.value.error_code == "WORKSPACE_EXPORT_PATH_INVALID"


def test_workspace_overwrite_requires_expected_commit_and_restore_creates_new_commit(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    package = _workspace_package(
        {"CLAUDE.md": b"# replacement\n", "binary.bin": b"\x00raw"},
        agent_id="target",
    )
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="target", name="target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_text = (workspace / "CLAUDE.md").read_bytes()
        (workspace / ".gitignore").write_bytes(b"*.secret\n")
        (workspace / "stale.secret").write_bytes(b"must-be-deleted-by-replacement\n")
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
        stale_deleted = not (workspace / "stale.secret").exists()
        restored = client.post(
            "/api/agent-registry/target/workspace/restore",
            json={
                "target_commit_sha": overwrite_body["rollback_target_commit_sha"],
                "expected_current_commit_sha": overwrite_body["current_commit_sha"],
                "reason": "restore test baseline",
            },
        )

    assert missing_current.status_code == 422
    assert missing_current.json()["error_code"] == "WORKSPACE_IMPORT_CURRENT_REF_REQUIRED"
    assert stale_current.status_code == 409
    assert stale_current.json()["error_code"] == "WORKSPACE_HEAD_CONFLICT"
    assert overwritten.status_code == 200
    assert overwrite_body["action"] == "overwritten"
    assert overwrite_body["rollback_target_commit_sha"]
    assert stale_deleted
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_text
    assert (workspace / "stale.secret").read_bytes() == b"must-be-deleted-by-replacement\n"
    restore_body = restored.json()
    assert restored.status_code == 200
    assert restore_body["action"] == "restored"
    assert restore_body["restored_tree_commit_sha"] == overwrite_body["rollback_target_commit_sha"]
    assert restore_body["current_commit_sha"] not in {baseline, overwrite_body["current_commit_sha"]}


def test_workspace_import_invalidates_sdk_resume_and_next_turn_reads_applied_commit(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="session-target", name="session target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=session-target").json()["commit_sha"]
        session = LocalSession(
            session_id="existing-api-session",
            sdk_session_id="existing-sdk-session",
            agent_id="session-target",
            turns=1,
        )
        module.session_store.save(session)
        request = ChatRequest(message="before import", session_id=session.session_id, agent_id="session-target")
        profile = module.runtime._resolve_runtime_profile(request, None)
        before_session = module.session_store.get(session.session_id)
        assert before_session is not None
        before_options = module.runtime._build_options(request, before_session, profile=profile)
        package = _package_from_workspace(workspace, overrides={"CLAUDE.md": b"# applied package workspace\n"})

        imported = client.post(
            "/api/agent-registry/session-target/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("session-target.tar.gz", package, "application/gzip")},
        )

    saved = module.session_store.get(session.session_id)
    assert imported.status_code == 200
    assert getattr(before_options, "resume", None) == "existing-sdk-session"
    assert saved is not None
    assert saved.session_id == session.session_id
    assert saved.turns == 1
    assert saved.sdk_session_id is None
    after_request = ChatRequest(message="after import", session_id=session.session_id, agent_id="session-target")
    after_profile = module.runtime._resolve_runtime_profile(after_request, None)
    after_context = asyncio.run(
        module.runtime._new_runtime_request_context(
            after_request,
            profile=after_profile,
            agent_id="session-target",
        )
    )
    after_options = module.runtime._build_options(
        after_request,
        after_context.session,
        context=after_context,
        profile=after_profile,
    )
    assert getattr(after_options, "resume", None) is None
    assert getattr(after_options, "session_id", None) == after_context.attempted_sdk_session_id
    assert Path(str(after_options.cwd)) == workspace
    assert workspace.joinpath("CLAUDE.md").read_bytes() == b"# applied package workspace\n"
    assert after_context.session.session_id == session.session_id
    assert after_context.agent_version_id == imported.json()["current_commit_sha"]


def test_runtime_admission_holds_version_snapshot_stable_against_workspace_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    version_resolver_entered = threading.Event()
    allow_version_resolver = threading.Event()
    import_lease_requested = threading.Event()
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="admission-race", name="admission race")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=admission-race").json()["commit_sha"]
        package = _package_from_workspace(
            workspace,
            overrides={"CLAUDE.md": b"# import must wait for active turn\n"},
        )
        request = ChatRequest(
            message="hold the runtime admission",
            session_id="admission-race-session",
            agent_id="admission-race",
        )
        profile = module.runtime._resolve_runtime_profile(request, None)
        original_version_resolver = module.runtime._current_agent_version_id
        original_lease = module.agent_governance.version_maintenance.lease

        def blocking_version_resolver(agent_id: str | None = None) -> str | None:
            version = original_version_resolver(agent_id)
            assert version == baseline
            version_resolver_entered.set()
            assert allow_version_resolver.wait(timeout=5)
            return version

        monkeypatch.setattr(
            module.runtime,
            "_current_agent_version_id",
            blocking_version_resolver,
        )

        def signaling_lease(**kwargs):
            if kwargs.get("agent_id") == "admission-race":
                import_lease_requested.set()
            return original_lease(**kwargs)

        monkeypatch.setattr(
            module.agent_governance.version_maintenance,
            "lease",
            signaling_lease,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            context_future = executor.submit(
                asyncio.run,
                module.runtime._new_runtime_request_context(
                    request,
                    profile=profile,
                    agent_id="admission-race",
                ),
            )
            assert version_resolver_entered.wait(timeout=5)
            import_future = executor.submit(
                client.post,
                "/api/agent-registry/admission-race/workspace/import",
                data={"expected_current_commit_sha": baseline},
                files={
                    "package": (
                        "admission-race.tar.gz",
                        package,
                        "application/gzip",
                    )
                },
            )
            assert import_lease_requested.wait(timeout=5)
            assert not import_future.done()
            allow_version_resolver.set()
            context = context_future.result(timeout=5)
            imported = import_future.result(timeout=5)

        current = client.get("/api/agent-repository/current?agent_id=admission-race").json()["commit_sha"]

    with module.session_store.Session() as db:
        intent = db.get(SessionTurnIntentModel, context.run_id)
        assert intent is not None
        intent_version = intent.request_json["agent_version_id"]
    assert imported.status_code == 409
    assert imported.json()["error_code"] == "WORKSPACE_SESSION_INVALIDATION_CONFLICT"
    assert current == baseline
    assert context.agent_version_id == baseline
    assert intent_version == baseline


def test_workspace_import_rejects_active_first_turn_without_changing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="active-turn", name="active turn")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=active-turn").json()["commit_sha"]
        session = module.session_store.get_or_create_owned("active-first-turn", agent_id="active-turn")
        module.session_store.begin_persisted_turn(
            session,
            run_id="active-workspace-turn",
            agent_id="active-turn",
            new_sdk_session_id="attempted-first-sdk-session",
            sdk_project_key="active-turn-project",
            resolve_agent_version_id=lambda: baseline,
            request={"message": "still running"},
            created_at="2026-07-16T00:00:00+00:00",
        )
        package = _package_from_workspace(workspace, overrides={"CLAUDE.md": b"# must not activate\n"})

        response = client.post(
            "/api/agent-registry/active-turn/workspace/import",
            data={"expected_current_commit_sha": baseline},
            files={"package": ("active-turn.tar.gz", package, "application/gzip")},
        )
        current = client.get("/api/agent-repository/current?agent_id=active-turn").json()["commit_sha"]

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKSPACE_SESSION_INVALIDATION_CONFLICT"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes


def test_workspace_restore_rejects_historical_non_regular_tree_without_changing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="restore-guard", name="restore guard")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = client.get("/api/agent-repository/current?agent_id=restore-guard").json()["commit_sha"]
        (workspace / "unsafe-link").symlink_to("CLAUDE.md")
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


def test_workspace_restore_projects_size_and_session_invalidation_failures_without_changing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="restore-failures", name="restore failures")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_bytes = (workspace / "CLAUDE.md").read_bytes()
        baseline = client.get("/api/agent-repository/current?agent_id=restore-failures").json()["commit_sha"]

        (workspace / "oversized.bin").write_bytes(b"12345")
        _run_git(workspace, "add", "-A", "--", ".")
        _run_git(workspace, "commit", "-m", "Historical oversized tree")
        oversized_commit = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "reset", "--hard", baseline)
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace_codec, "MAX_SINGLE_MEMBER_BYTES", 4)
            oversized = client.post(
                "/api/agent-registry/restore-failures/workspace/restore",
                json={
                    "target_commit_sha": oversized_commit,
                    "expected_current_commit_sha": baseline,
                    "reason": "must reject oversized historical tree",
                },
            )

        (workspace / "CLAUDE.md").write_bytes(b"# historical valid tree\n")
        _run_git(workspace, "add", "-A", "--", ".")
        _run_git(workspace, "commit", "-m", "Historical valid tree")
        valid_commit = _run_git(workspace, "rev-parse", "HEAD")
        _run_git(workspace, "reset", "--hard", baseline)
        monkeypatch.setattr(
            module.session_store,
            "clear_inactive_sdk_sessions_for_agent_in_transaction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected invalidation failure")),
        )
        invalidation_failed = client.post(
            "/api/agent-registry/restore-failures/workspace/restore",
            json={
                "target_commit_sha": valid_commit,
                "expected_current_commit_sha": baseline,
                "reason": "must not activate when invalidation fails",
            },
        )
        current = client.get("/api/agent-repository/current?agent_id=restore-failures").json()["commit_sha"]

    assert oversized.status_code == 413
    assert oversized.json()["error_code"] == "WORKSPACE_RESTORE_TARGET_INVALID"
    assert invalidation_failed.status_code == 503
    assert invalidation_failed.json()["error_code"] == "WORKSPACE_SESSION_INVALIDATION_FAILED"
    assert current == baseline
    assert (workspace / "CLAUDE.md").read_bytes() == baseline_bytes


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
    monkeypatch,
    tmp_path: Path,
    kind: str,
    error_code: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
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


def test_workspace_package_openapi_documents_binary_multipart_and_export_receipt_headers(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
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
    assert {"411", "413", "415", "503"} <= set(import_operation["responses"])

    export_operation = schema["paths"]["/api/agent-registry/{agent_id}/workspace/export"]["post"]
    assert "413" in export_operation["responses"]
    response_headers = export_operation["responses"]["200"]["headers"]
    assert set(response_headers) == {
        "Content-Disposition",
        "X-Agent-Commit-SHA",
        "X-Workspace-Package-SHA256",
        "X-Workspace-Tree-SHA256",
    }
    restore_operation = schema["paths"]["/api/agent-registry/{agent_id}/workspace/restore"]["post"]
    assert {"413", "503"} <= set(restore_operation["responses"])
