from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.runtime.errors import FeedbackStoreError

MAX_COMPRESSED_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_MULTIPART_REQUEST_BYTES = MAX_COMPRESSED_PACKAGE_BYTES + 1024 * 1024
MAX_SINGLE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 10_000
MAX_PACKAGE_PATH_BYTES = 4 * 1024
MAX_PACKAGE_DEPTH = 32
MAX_TAR_METADATA_BYTES = 64 * 1024
MAX_CONSECUTIVE_TAR_METADATA = 16
COPY_CHUNK_BYTES = 1024 * 1024
_TAR_BLOCK_BYTES = 512
_TAR_METADATA_TYPES = {b"x", b"g", b"L", b"K"}
_TAR_ALLOWED_MEMBER_TYPES = {b"\0", b"0", b"5"}
_MAX_GIT_STDERR_BYTES = 4 * 1024


class WorkspacePackageError(FeedbackStoreError):
    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class WorkspaceGitReadError(RuntimeError):
    """Raised when Git tree/blob streaming violates the expected batch protocol."""


@dataclass(frozen=True)
class ValidatedWorkspacePackage:
    entries: tuple[WorkspaceProvisionEntry, ...]
    package_sha256: str
    tree_sha256: str


@dataclass(frozen=True)
class _CommitBlobSpec:
    relative_path: PurePosixPath
    mode: int
    object_id: bytes
    size: int


@dataclass
class _TarStreamBudget:
    header_records: int = 0
    consecutive_metadata: int = 0
    payload_bytes: int = 0
    stream_bytes: int = 0


def read_workspace_package(
    source: BinaryIO,
    destination: Path,
    *,
    filename: str | None,
) -> ValidatedWorkspacePackage:
    if not (filename or "").lower().endswith(".tar.gz"):
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_TYPE_INVALID", "package filename must end with .tar.gz")
    package_sha256 = _copy_limited_package(source, destination)
    entries = _read_tar_entries(destination)
    return ValidatedWorkspacePackage(
        entries=entries,
        package_sha256=package_sha256,
        tree_sha256=tree_sha256(entries),
    )


def validate_workspace_config_entries(entries: tuple[WorkspaceProvisionEntry, ...]) -> None:
    required_objects = {".mcp.json", ".claude/settings.json"}
    for entry in entries:
        path = entry.relative_path.as_posix()
        if path not in required_objects:
            continue
        try:
            value = json.loads(entry.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_PACKAGE_CONFIG_INVALID",
                f"Workspace config must be a UTF-8 JSON object: {path}",
            ) from exc
        if not isinstance(value, dict):
            raise WorkspacePackageError(
                422,
                "WORKSPACE_PACKAGE_CONFIG_INVALID",
                f"Workspace config must be a JSON object: {path}",
            )


def validate_commit_path(raw_path: bytes) -> PurePosixPath:
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_PATH_INVALID", "Workspace Git path must be UTF-8") from exc
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_PATH_INVALID", f"Unsafe workspace Git path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ".git" in parts:
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_PATH_INVALID", f"Unsafe workspace Git path: {path!r}")
    if len(path.encode("utf-8")) + len("workspace/") > MAX_PACKAGE_PATH_BYTES or len(parts) > MAX_PACKAGE_DEPTH:
        raise WorkspacePackageError(413, "WORKSPACE_PACKAGE_PATH_TOO_LARGE", f"Workspace Git path exceeds limits: {path!r}")
    return PurePosixPath(*parts)


def read_commit_entries(
    repository: Path,
    commit_sha: str,
    *,
    run_git: Callable[[Path, list[str]], bytes],
) -> tuple[WorkspaceProvisionEntry, ...]:
    raw_tree = run_git(repository, ["ls-tree", "-r", "-z", "-l", "--full-tree", commit_sha])
    specs = _parse_commit_blob_specs(raw_tree)
    contents = _read_commit_blob_contents(repository, specs)
    entries = tuple(
        WorkspaceProvisionEntry(
            relative_path=spec.relative_path,
            content=content,
            mode=spec.mode,
        )
        for spec, content in zip(specs, contents, strict=True)
    )
    result = tuple(sorted(entries, key=lambda entry: entry.relative_path.as_posix()))
    validate_workspace_config_entries(result)
    return result


def _parse_commit_blob_specs(raw_tree: bytes) -> tuple[_CommitBlobSpec, ...]:
    specs: list[_CommitBlobSpec] = []
    total_bytes = 0
    for raw_record in raw_tree.split(b"\0"):
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 4:
            raise WorkspacePackageError(422, "WORKSPACE_EXPORT_TREE_INVALID", "Git tree contains an unreadable entry")
        raw_mode, object_type, object_id, raw_size = fields
        if object_type != b"blob" or raw_mode not in {b"100644", b"100755"}:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_EXPORT_TREE_INVALID",
                "Workspace export only supports regular Git blobs; symlinks and submodules are not allowed",
            )
        relative_path = validate_commit_path(raw_path)
        size = _parse_blob_size(raw_size, relative_path)
        total_bytes += size
        if len(specs) + 1 > MAX_PACKAGE_MEMBERS or total_bytes > MAX_EXTRACTED_PACKAGE_BYTES:
            raise WorkspacePackageError(413, "WORKSPACE_PACKAGE_TOO_LARGE", "Workspace Git tree exceeds package limits")
        specs.append(
            _CommitBlobSpec(
                relative_path=relative_path,
                mode=0o755 if raw_mode == b"100755" else 0o644,
                object_id=object_id,
                size=size,
            )
        )
    return tuple(specs)


def _parse_blob_size(raw_size: bytes, relative_path: PurePosixPath) -> int:
    try:
        size = int(raw_size)
    except ValueError as exc:
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_TREE_INVALID", "Git tree blob has no valid size") from exc
    if size < 0:
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_TREE_INVALID", "Git tree blob has a negative size")
    if size > MAX_SINGLE_MEMBER_BYTES:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_MEMBER_TOO_LARGE",
            f"Workspace file exceeds {MAX_SINGLE_MEMBER_BYTES} bytes: {relative_path}",
        )
    return size


def _read_commit_blob_contents(
    repository: Path,
    specs: tuple[_CommitBlobSpec, ...],
) -> tuple[bytes, ...]:
    unique_specs = _unique_blob_specs(specs)
    if not unique_specs:
        return ()
    with tempfile.TemporaryFile() as stderr_output:
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=str(repository),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_output,
        )
        if process.stdin is None or process.stdout is None:  # pragma: no cover - subprocess contract
            process.kill()
            raise WorkspaceGitReadError("git cat-file --batch did not expose input/output pipes")
        blobs: dict[bytes, bytes] = {}
        try:
            for spec in unique_specs:
                blobs[spec.object_id] = _read_one_batch_blob(process, spec)
            process.stdin.close()
            return_code = process.wait()
            if return_code != 0:
                raise WorkspaceGitReadError(_batch_failure_message(stderr_output, return_code))
        except WorkspaceGitReadError:
            _stop_batch_process(process)
            raise
        except OSError as exc:
            _stop_batch_process(process)
            raise WorkspaceGitReadError(_batch_failure_message(stderr_output, process.returncode)) from exc
        except Exception:
            _stop_batch_process(process)
            raise
        finally:
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
    return tuple(blobs[spec.object_id] for spec in specs)


def _unique_blob_specs(specs: tuple[_CommitBlobSpec, ...]) -> tuple[_CommitBlobSpec, ...]:
    unique: list[_CommitBlobSpec] = []
    known_sizes: dict[bytes, int] = {}
    seen_objects: set[bytes] = set()
    for spec in specs:
        known_size = known_sizes.setdefault(spec.object_id, spec.size)
        if known_size != spec.size:
            raise WorkspacePackageError(422, "WORKSPACE_EXPORT_TREE_INVALID", "Git tree reports inconsistent blob sizes")
        if spec.object_id not in seen_objects:
            seen_objects.add(spec.object_id)
            unique.append(spec)
    return tuple(unique)


def _read_one_batch_blob(
    process: subprocess.Popen[bytes],
    spec: _CommitBlobSpec,
) -> bytes:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(spec.object_id + b"\n")
    process.stdin.flush()
    header = process.stdout.readline(256)
    fields = header.rstrip(b"\n").split()
    if len(fields) != 3 or fields[0] != spec.object_id or fields[1] != b"blob":
        raise WorkspaceGitReadError("git cat-file --batch returned an invalid blob header")
    try:
        reported_size = int(fields[2])
    except ValueError as exc:
        raise WorkspaceGitReadError("git cat-file --batch returned an invalid blob size") from exc
    if reported_size != spec.size:
        raise WorkspacePackageError(422, "WORKSPACE_EXPORT_TREE_INVALID", "Git blob size changed during export")
    content = _read_process_bytes(process.stdout, reported_size)
    if process.stdout.read(1) != b"\n":
        raise WorkspaceGitReadError("git cat-file --batch returned a truncated blob delimiter")
    return content


def _read_process_bytes(source: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, COPY_CHUNK_BYTES))
        if not chunk:
            raise WorkspaceGitReadError("git cat-file --batch returned a truncated blob")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stop_batch_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _batch_failure_message(stderr_output: BinaryIO, return_code: int | None) -> str:
    stderr_output.seek(0)
    captured = stderr_output.read(_MAX_GIT_STDERR_BYTES + 1)
    suffix = " (stderr truncated)" if len(captured) > _MAX_GIT_STDERR_BYTES else ""
    code = "unknown" if return_code is None else str(return_code)
    return f"git cat-file --batch failed with exit code {code}{suffix}"


def write_workspace_archive(destination: Path, entries: tuple[WorkspaceProvisionEntry, ...]) -> None:
    with destination.open("wb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as gzip_output:
            with tarfile.open(fileobj=gzip_output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                root = tarfile.TarInfo("workspace/")
                root.type = tarfile.DIRTYPE
                root.mode = 0o755
                root.uid = root.gid = 0
                root.uname = root.gname = ""
                root.mtime = 0
                archive.addfile(root)
                for entry in entries:
                    member = tarfile.TarInfo(f"workspace/{entry.relative_path.as_posix()}")
                    member.size = len(entry.content)
                    member.mode = entry.mode
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(entry.content))


def tree_sha256(entries: tuple[WorkspaceProvisionEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        path = entry.relative_path.as_posix().encode("utf-8")
        digest.update(f"{entry.mode:o} ".encode("ascii"))
        digest.update(path)
        digest.update(b"\0")
        digest.update(str(len(entry.content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.content)
        digest.update(b"\0")
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_limited_package(source: BinaryIO, destination: Path) -> str:
    digest = hashlib.sha256()
    written = 0
    with destination.open("wb") as target:
        while chunk := source.read(COPY_CHUNK_BYTES):
            written += len(chunk)
            if written > MAX_COMPRESSED_PACKAGE_BYTES:
                raise WorkspacePackageError(
                    413,
                    "WORKSPACE_PACKAGE_TOO_LARGE",
                    f"Compressed workspace package exceeds {MAX_COMPRESSED_PACKAGE_BYTES} bytes",
                )
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    if written == 0:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_EMPTY", "Workspace package is empty")
    return digest.hexdigest()


def _read_tar_entries(path: Path) -> tuple[WorkspaceProvisionEntry, ...]:
    _preflight_tar_stream(path)
    entries: list[WorkspaceProvisionEntry] = []
    seen: dict[str, bool] = {}
    descendant_prefixes: set[str] = set()
    member_count = 0
    extracted_bytes = 0
    saw_workspace = False
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            while member := archive.next():
                relative_path = _validate_member(member, seen, descendant_prefixes)
                saw_workspace = True
                if relative_path != PurePosixPath("."):
                    member_count += 1
                    if member_count > MAX_PACKAGE_MEMBERS:
                        raise WorkspacePackageError(
                            413,
                            "WORKSPACE_PACKAGE_TOO_MANY_MEMBERS",
                            "Workspace package has too many members",
                        )
                if member.isdir():
                    continue
                if member.size > MAX_SINGLE_MEMBER_BYTES:
                    raise WorkspacePackageError(
                        413,
                        "WORKSPACE_PACKAGE_MEMBER_TOO_LARGE",
                        f"Workspace package member exceeds {MAX_SINGLE_MEMBER_BYTES} bytes: {member.name}",
                    )
                extracted_bytes += member.size
                if extracted_bytes > MAX_EXTRACTED_PACKAGE_BYTES:
                    raise WorkspacePackageError(
                        413,
                        "WORKSPACE_PACKAGE_EXPANDED_TOO_LARGE",
                        f"Expanded workspace package exceeds {MAX_EXTRACTED_PACKAGE_BYTES} bytes",
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_MEMBER_INVALID", f"Cannot read package member: {member.name}")
                content = source.read(member.size + 1)
                if len(content) != member.size:
                    raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_MEMBER_INVALID", f"Package member size mismatch: {member.name}")
                entries.append(
                    WorkspaceProvisionEntry(
                        relative_path=relative_path,
                        content=content,
                        mode=0o755 if member.mode & 0o111 else 0o644,
                    )
                )
    except (tarfile.TarError, OSError, EOFError, RecursionError) as exc:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", "Workspace package is not a valid .tar.gz archive") from exc
    if not saw_workspace:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_ROOT_INVALID", "Workspace package must contain a workspace/ root")
    entries.sort(key=lambda entry: entry.relative_path.as_posix())
    result = tuple(entries)
    validate_workspace_config_entries(result)
    return result


def _preflight_tar_stream(path: Path) -> None:
    """Bound tar metadata before ``tarfile`` materializes PAX/GNU headers."""
    budget = _TarStreamBudget()
    try:
        with gzip.open(path, mode="rb") as source:
            while header := _read_tar_block(source):
                if not any(header):
                    return
                size = _validated_tar_record_size(header, budget)
                member_type = header[156:157]
                consumed_size = _preflight_tar_record(source, member_type, size, budget)
                padded_size = ((size + _TAR_BLOCK_BYTES - 1) // _TAR_BLOCK_BYTES) * _TAR_BLOCK_BYTES
                _record_tar_stream_bytes(budget, padded_size)
                _discard_tar_bytes(source, padded_size - consumed_size)
    except WorkspacePackageError:
        raise
    except (gzip.BadGzipFile, tarfile.TarError, ValueError, OSError, EOFError, RecursionError, zlib.error) as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_INVALID",
            "Workspace package is not a valid .tar.gz archive",
        ) from exc


def _validated_tar_record_size(header: bytes, budget: _TarStreamBudget) -> int:
    budget.header_records += 1
    if budget.header_records > MAX_PACKAGE_MEMBERS * 3 + 32:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_TOO_MANY_MEMBERS",
            "Workspace package has too many tar header records",
        )
    size = tarfile.nti(header[124:136])
    if size < 0:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_MEMBER_INVALID",
            "Workspace package contains a negative tar member size",
        )
    return size


def _preflight_tar_record(
    source: BinaryIO,
    member_type: bytes,
    size: int,
    budget: _TarStreamBudget,
) -> int:
    if member_type in _TAR_METADATA_TYPES:
        return _preflight_tar_metadata(source, member_type, size, budget)
    budget.consecutive_metadata = 0
    if member_type not in _TAR_ALLOWED_MEMBER_TYPES:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_MEMBER_INVALID",
            "Special package member is not allowed",
        )
    if size > MAX_SINGLE_MEMBER_BYTES:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_MEMBER_TOO_LARGE",
            f"Workspace package member exceeds {MAX_SINGLE_MEMBER_BYTES} bytes",
        )
    budget.payload_bytes += size
    if budget.payload_bytes > MAX_EXTRACTED_PACKAGE_BYTES:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_EXPANDED_TOO_LARGE",
            f"Expanded workspace package exceeds {MAX_EXTRACTED_PACKAGE_BYTES} bytes",
        )
    return 0


def _preflight_tar_metadata(
    source: BinaryIO,
    member_type: bytes,
    size: int,
    budget: _TarStreamBudget,
) -> int:
    budget.consecutive_metadata += 1
    if budget.consecutive_metadata > MAX_CONSECUTIVE_TAR_METADATA:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_METADATA_TOO_LARGE",
            "Workspace package contains too many consecutive tar metadata records",
        )
    if size > MAX_TAR_METADATA_BYTES:
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_METADATA_TOO_LARGE",
            f"Tar metadata record exceeds {MAX_TAR_METADATA_BYTES} bytes",
        )
    if member_type not in {b"x", b"g"}:
        return 0
    metadata = _read_tar_bytes(source, size)
    if b"GNU.sparse." in metadata:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_MEMBER_INVALID",
            "GNU sparse tar members are not allowed",
        )
    return size


def _record_tar_stream_bytes(budget: _TarStreamBudget, padded_size: int) -> None:
    budget.stream_bytes += _TAR_BLOCK_BYTES + padded_size
    if budget.stream_bytes > _max_tar_stream_bytes():
        raise WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_EXPANDED_TOO_LARGE",
            "Expanded tar stream exceeds workspace package limits",
        )


def _read_tar_block(source: BinaryIO) -> bytes:
    block = source.read(_TAR_BLOCK_BYTES)
    if not block:
        return b""
    if len(block) != _TAR_BLOCK_BYTES:
        raise ValueError("Truncated tar header")
    return block


def _discard_tar_bytes(source: BinaryIO, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, COPY_CHUNK_BYTES))
        if not chunk:
            raise ValueError("Truncated tar member")
        remaining -= len(chunk)


def _read_tar_bytes(source: BinaryIO, size: int) -> bytes:
    content = source.read(size)
    if len(content) != size:
        raise ValueError("Truncated tar metadata")
    return content


def _max_tar_stream_bytes() -> int:
    per_member_overhead = MAX_PACKAGE_PATH_BYTES + 2 * _TAR_BLOCK_BYTES
    return MAX_EXTRACTED_PACKAGE_BYTES + (MAX_PACKAGE_MEMBERS + 1) * per_member_overhead + 2 * _TAR_BLOCK_BYTES


def _validate_member(
    member: tarfile.TarInfo,
    seen: dict[str, bool],
    descendant_prefixes: set[str],
) -> PurePosixPath:
    raw = member.name
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_PATH_INVALID", f"Unsafe package path: {raw!r}")
    try:
        raw_bytes = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_PATH_INVALID",
            f"Package path must be valid UTF-8: {raw!r}",
        ) from exc
    normalized = raw.rstrip("/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_PATH_INVALID", f"Unsafe package path: {raw!r}")
    if len(raw_bytes) > MAX_PACKAGE_PATH_BYTES or len(parts) - 1 > MAX_PACKAGE_DEPTH:
        raise WorkspacePackageError(413, "WORKSPACE_PACKAGE_PATH_TOO_LARGE", f"Package path exceeds limits: {raw!r}")
    if parts[0] != "workspace":
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_ROOT_INVALID", "Package entries must be under workspace/")
    if ".git" in parts[1:]:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_GIT_FORBIDDEN", "Workspace package must not contain .git")
    if normalized in seen:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_DUPLICATE_MEMBER", f"Duplicate package member: {normalized}")
    if not (member.isdir() or member.isreg()):
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_MEMBER_INVALID", f"Special package member is not allowed: {raw}")
    if member.isdir() and member.size != 0:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_MEMBER_INVALID", f"Directory package member must have zero size: {raw}")
    if len(parts) == 1:
        if not member.isdir():
            raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_ROOT_INVALID", "workspace package root must be a directory")
        seen[normalized] = True
        return PurePosixPath(".")
    if member.size < 0:
        raise WorkspacePackageError(422, "WORKSPACE_PACKAGE_MEMBER_INVALID", f"Invalid package member size: {raw}")
    _reject_member_path_conflict(
        normalized,
        parts,
        is_directory=member.isdir(),
        seen=seen,
        descendant_prefixes=descendant_prefixes,
    )
    for index in range(1, len(parts)):
        descendant_prefixes.add("/".join(parts[:index]))
    seen[normalized] = member.isdir()
    return PurePosixPath(*parts[1:])


def _reject_member_path_conflict(
    normalized: str,
    parts: list[str],
    *,
    is_directory: bool,
    seen: dict[str, bool],
    descendant_prefixes: set[str],
) -> None:
    for index in range(1, len(parts)):
        prefix = "/".join(parts[:index])
        if seen.get(prefix) is False:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_PACKAGE_PATH_CONFLICT",
                f"Package file conflicts with descendant path: {prefix}",
            )
    if is_directory:
        return
    if normalized in descendant_prefixes:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_PACKAGE_PATH_CONFLICT",
            f"Package file conflicts with existing descendant path: {normalized}",
        )
