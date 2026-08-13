from __future__ import annotations

import io
import stat
import subprocess
import tarfile
from collections.abc import Iterable
from pathlib import Path

from fastapi.testclient import TestClient


def workspace_package(
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


def import_new_agent(
    client: TestClient,
    *,
    agent_id: str,
    name: str,
    package: bytes | None = None,
    requires_web_hitl: bool = True,
):
    content = package or workspace_package(
        {
            "CLAUDE.md": f"# {name}\n".encode(),
            ".mcp.json": b'{"mcpServers": {}}\n',
            ".claude/settings.json": (b'{"permissions":{"ask":["Bash(*)"]}}\n' if requires_web_hitl else b'{"permissions":{"ask":[]}}\n'),
        },
        agent_id=agent_id,
    )
    return client.post(
        f"/api/agent-registry/{agent_id}/workspace/import",
        data={"name": name},
        files={"package": (f"{agent_id}.tar.gz", content, "application/gzip")},
    )


def package_from_workspace(workspace: Path, *, overrides: dict[str, bytes]) -> bytes:
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
    return workspace_package(files, executable=frozenset(executable))


def run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def invalid_package(kind: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if kind == "traversal":
            member = tarfile.TarInfo("workspace/../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        elif kind == "symlink":
            member = tarfile.TarInfo("workspace/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside/workspace-boundary"
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


def package_with_long_tar_metadata(path_bytes: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        member = tarfile.TarInfo(f"workspace/{'a' * path_bytes}")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def package_with_metadata_chain(count: int, *, member_type: bytes = tarfile.XGLTYPE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(count):
            payload = pax_record(str(index), "x") if member_type == tarfile.XGLTYPE else f"workspace/long-{index}\0".encode()
            member = tarfile.TarInfo(f"metadata-{index}")
            member.type = member_type
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
    return buffer.getvalue()


def pax_record(key: str, value: str) -> bytes:
    body = f"{key}={value}\n".encode()
    length = len(body) + 3
    while True:
        record = str(length).encode() + b" " + body
        if len(record) == length:
            return record
        length = len(record)


def package_with_empty_pax_path() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = pax_record("path", "")
        metadata = tarfile.TarInfo("empty-path-metadata")
        metadata.type = tarfile.XHDTYPE
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))
        member = tarfile.TarInfo("workspace/fallback")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def package_with_large_reversed_conflict(member_count: int) -> bytes:
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


def git_bytes(repository: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def make_shared_blob_commit(repository: Path, paths: Iterable[str], content: bytes) -> str:
    repository.mkdir()
    git_bytes(repository, ["init", "-q"])
    git_bytes(repository, ["config", "user.name", "AgentGov Test"])
    git_bytes(repository, ["config", "user.email", "agentgov-test@example.local"])
    object_id = git_bytes(repository, ["hash-object", "-w", "--stdin"], input_bytes=content).strip()
    tree_input = b"".join(f"100644 blob {object_id.decode()}\t{path}\n".encode() for path in paths)
    tree_id = git_bytes(repository, ["mktree"], input_bytes=tree_input).strip()
    return git_bytes(repository, ["commit-tree", tree_id.decode(), "-m", "scale tree"]).decode().strip()


def package_with_sparse_pax() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("workspace/sparse.bin")
        member.size = 1
        member.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
        archive.addfile(member, io.BytesIO(b"x"))
    return buffer.getvalue()


def package_with_agent_id(package: bytes, agent_id: str) -> bytes:
    """重打测试包并显式声明目标 ID，模拟包所有者修改而非平台改写。"""
    files: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as source_archive:
        for member in source_archive:
            if not member.isfile() or not member.name.startswith("workspace/"):
                continue
            relative = member.name.removeprefix("workspace/")
            source = source_archive.extractfile(member)
            assert source is not None
            files[relative] = (source.read(), member.mode)
    files["agent.yaml"] = (f"agent:\n  id: {agent_id}\n".encode(), 0o644)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as target_archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        target_archive.addfile(root)
        for relative, (content, mode) in sorted(files.items()):
            member = tarfile.TarInfo(f"workspace/{relative}")
            member.size = len(content)
            member.mode = mode
            target_archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()
