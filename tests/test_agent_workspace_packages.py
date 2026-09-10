from __future__ import annotations

import io
import stat
import subprocess
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import pytest
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
    assert (target / "tools" / "raw-tool").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert stat.S_IMODE((target / "tools" / "raw-tool").stat().st_mode) & 0o111
    assert "id: imported" in (target / "agent.yaml").read_text(encoding="utf-8")


def test_workspace_export_restores_exec_tracking_when_existing_git_disabled_filemode(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="filemode", name="filemode")
        workspace = Path(created.json()["agent"]["workspace_dir"])
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
    imported_script = Path(imported.json()["agent"]["workspace_dir"]) / "tools" / "tracked-tool"
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
        {"AGENT.md": b"# replacement\n", "binary.bin": b"\x00raw"},
        agent_id="target",
    )
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="target", name="target")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_text = (workspace / "AGENT.md").read_bytes()
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
    assert (workspace / "AGENT.md").read_bytes() == baseline_text
    assert (workspace / "stale.secret").read_bytes() == b"must-be-deleted-by-replacement\n"
    restore_body = restored.json()
    assert restored.status_code == 200
    assert restore_body["action"] == "restored"
    assert restore_body["restored_tree_commit_sha"] == overwrite_body["rollback_target_commit_sha"]
    assert restore_body["current_commit_sha"] not in {baseline, overwrite_body["current_commit_sha"]}


def test_workspace_restore_rejects_historical_non_regular_tree_without_changing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="restore-guard", name="restore guard")
        workspace = Path(created.json()["agent"]["workspace_dir"])
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


def test_workspace_restore_rejects_oversized_tree_without_changing_head(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="restore-failures", name="restore failures")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline_bytes = (workspace / "AGENT.md").read_bytes()
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
