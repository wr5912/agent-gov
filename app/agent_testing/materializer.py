from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, TypeAlias
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    require_governed_repository,
)

from .source_limits import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_COMPONENT_BYTES,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_PATH_BYTES,
    MAX_SOURCE_PATH_DEPTH,
)

GIT_BINARY = Path("/usr/bin/git")
RAW_GIT_ENV = MappingProxyType(
    {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
)
_MAX_TREE_OUTPUT_BYTES = MAX_SOURCE_FILES * (MAX_SOURCE_PATH_BYTES + 128)
_GIT_TIMEOUT_SECONDS = 30
_COPY_CHUNK_BYTES = 1024 * 1024
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE_NAMES = {
    ".claude.json",
    ".envrc",
    ".git-credentials",
    ".gitconfig",
    ".env",
    ".mcp.local.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "auth.json",
    "claude.local.md",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
    "settings.local.json",
}
_SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".direnv",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    ".terraform",
    "credential",
    "credentials",
    "private",
    "secret",
    "secrets",
}
_SENSITIVE_SUFFIXES = (".key", ".p12", ".pem", ".pfx", ".secret")
_BlobContents: TypeAlias = dict[bytes, bytes]
_JsonObject: TypeAlias = dict[str, object]
_ENV_PLACEHOLDER = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
_HEADER_PLACEHOLDER = re.compile(r"^(?:Bearer |Basic )?\$\{[A-Z][A-Z0-9_]*\}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|credential|authorization|cookie|signature|sig)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_COMPACT_KEY = re.compile(r"(?:apikey|accesstoken|refreshtoken|clientsecret|privatekey|authorization|password|passwd|credential|cookie|signature)")
_SENSITIVE_CLI_FLAG = re.compile(
    r"--(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|client[-_]?secret|secret|password|passwd|credential|authorization|private[-_]?key)"
)
_INLINE_CREDENTIAL = re.compile(
    r"(?:authorization|x-api-key|api[_-]?key|token|secret|password)\s*[:=]\s*([^,;\r\n]+)",
    re.IGNORECASE,
)
_SAFE_LITERAL_HEADERS = {"accept", "content-type", "user-agent"}
_MAX_MCP_CONFIG_BYTES = 1024 * 1024


class MaterializationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SourceFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


@dataclass(frozen=True)
class _BlobSpec:
    path: PurePosixPath
    mode: int
    object_id: bytes
    size: int


def materialize_git_commit(repository: Path, commit_sha: str, destination: Path) -> SourceFingerprint:
    """Materialize one exact commit without consulting the live worktree."""

    safe_repository = _validate_repository(repository)
    normalized_commit = _validate_commit_sha(commit_sha)
    resolved_commit = _resolve_object(safe_repository, normalized_commit, object_type="commit")
    if resolved_commit != normalized_commit:
        raise MaterializationError("AGENT_SOURCE_COMMIT_MISMATCH", "Git resolved a different commit than requested")
    tree_sha = _resolve_object(safe_repository, normalized_commit, object_type="tree")
    full_tree_specs = _read_tree(safe_repository, normalized_commit)
    specs = tuple(spec for spec in full_tree_specs if not _is_sensitive_path(spec.path.parts))
    contents = _read_blobs(safe_repository, specs)
    specs, contents = _project_safe_config_files(specs, contents)
    fingerprint = SourceFingerprint(
        commit_sha=normalized_commit,
        tree_sha=tree_sha,
        source_digest=_source_digest(specs, contents),
        file_count=len(specs),
        total_bytes=sum(item.size for item in specs),
    )
    _write_destination(destination, specs, contents)
    return fingerprint


def _validate_repository(repository: Path) -> Path:
    if not GIT_BINARY.is_absolute() or not GIT_BINARY.is_file() or not os.access(GIT_BINARY, os.X_OK):
        raise MaterializationError("AGENT_SOURCE_GIT_UNAVAILABLE", "The fixed Git binary is unavailable")
    candidate = repository.absolute()
    try:
        resolved = repository.resolve(strict=True)
    except OSError as exc:
        raise MaterializationError("AGENT_SOURCE_REPOSITORY_INVALID", "Agent Git repository is unavailable") from exc
    if resolved != candidate or repository.is_symlink() or not repository.is_dir():
        raise MaterializationError("AGENT_SOURCE_REPOSITORY_INVALID", "Agent Git repository must be a real directory")
    try:
        require_governed_repository(resolved)
    except GovernedGitEnvironmentError as exc:
        raise MaterializationError(
            "AGENT_SOURCE_REPOSITORY_INVALID",
            "Agent Git repository violates the governed source authority",
        ) from exc
    return resolved


def _validate_commit_sha(commit_sha: str) -> str:
    normalized = commit_sha.strip()
    if normalized != commit_sha or not _FULL_SHA.fullmatch(normalized):
        raise MaterializationError("AGENT_SOURCE_COMMIT_INVALID", "commit_sha must be a lowercase full 40-character Git SHA")
    return normalized


def _resolve_object(repository: Path, commit_sha: str, *, object_type: str) -> str:
    raw = _run_git_capture(
        repository,
        ["rev-parse", "--verify", f"{commit_sha}^{{{object_type}}}"],
        max_output_bytes=128,
        failure_code="AGENT_SOURCE_COMMIT_NOT_FOUND",
    )
    value = raw.rstrip(b"\n")
    if not _FULL_SHA.fullmatch(value.decode("ascii", errors="ignore")) or raw not in {value + b"\n", value}:
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git returned an invalid object identifier")
    return value.decode("ascii")


def _read_tree(repository: Path, commit_sha: str) -> tuple[_BlobSpec, ...]:
    raw_tree = _run_git_capture(
        repository,
        ["ls-tree", "-r", "-z", "-l", "--full-tree", commit_sha],
        max_output_bytes=_MAX_TREE_OUTPUT_BYTES,
        failure_code="AGENT_SOURCE_TREE_INVALID",
    )
    specs: list[_BlobSpec] = []
    total_bytes = 0
    seen_paths: set[str] = set()
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        spec = _parse_tree_record(record)
        normalized_path = spec.path.as_posix()
        if normalized_path in seen_paths:
            raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree contains a duplicate path")
        seen_paths.add(normalized_path)
        total_bytes += spec.size
        if len(specs) + 1 > MAX_SOURCE_FILES or total_bytes > MAX_SOURCE_BYTES:
            raise MaterializationError("AGENT_SOURCE_TOO_LARGE", "Agent Git tree exceeds the source materialization limits")
        specs.append(spec)
    return tuple(sorted(specs, key=lambda item: item.path.as_posix()))


def _parse_tree_record(record: bytes) -> _BlobSpec:
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.split()
    if not separator or len(fields) != 4:
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree contains an unreadable entry")
    raw_mode, object_type, object_id, raw_size = fields
    if raw_mode == b"120000":
        raise MaterializationError("AGENT_SOURCE_SYMLINK_FORBIDDEN", "Agent test source cannot contain symlinks")
    if raw_mode == b"160000" or object_type == b"commit":
        raise MaterializationError("AGENT_SOURCE_GITLINK_FORBIDDEN", "Agent test source cannot contain Git submodules")
    if raw_mode not in {b"100644", b"100755"} or object_type != b"blob":
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Agent test source only supports regular Git blobs")
    if not _FULL_SHA.fullmatch(object_id.decode("ascii", errors="ignore")):
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree contains an invalid object identifier")
    path = _validate_source_path(raw_path)
    size = _parse_blob_size(raw_size)
    return _BlobSpec(path=path, mode=0o755 if raw_mode == b"100755" else 0o644, object_id=object_id, size=size)


def _validate_source_path(raw_path: bytes) -> PurePosixPath:
    try:
        value = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterializationError("AGENT_SOURCE_PATH_INVALID", "Agent Git paths must be valid UTF-8") from exc
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MaterializationError("AGENT_SOURCE_PATH_INVALID", "Agent Git tree contains an unsafe path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() or part.endswith(".") for part in parts) or any(part.casefold() == ".git" for part in parts):
        raise MaterializationError("AGENT_SOURCE_PATH_INVALID", "Agent Git tree contains an unsafe path")
    encoded_parts = [part.encode("utf-8") for part in parts]
    if len(raw_path) > MAX_SOURCE_PATH_BYTES or len(parts) > MAX_SOURCE_PATH_DEPTH or any(len(part) > MAX_SOURCE_COMPONENT_BYTES for part in encoded_parts):
        raise MaterializationError("AGENT_SOURCE_PATH_TOO_LARGE", "Agent Git path exceeds the source materialization limits")
    return PurePosixPath(*parts)


def _is_sensitive_path(parts: tuple[str, ...]) -> bool:
    folded_parts = tuple(part.casefold() for part in parts)
    name = folded_parts[-1]
    if any(part in _SENSITIVE_DIRECTORIES for part in folded_parts[:-1]) or name in _SENSITIVE_NAMES:
        return True
    if name.startswith(".env.") and not name.endswith(".example"):
        return True
    if name.endswith(".example"):
        return False
    return (".local." in name) or any(name.endswith(suffix) or f"{suffix}." in name for suffix in _SENSITIVE_SUFFIXES)


def _parse_blob_size(raw_size: bytes) -> int:
    try:
        size = int(raw_size)
    except ValueError as exc:
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree contains an invalid blob size") from exc
    if size < 0:
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree contains a negative blob size")
    if size > MAX_SOURCE_FILE_BYTES:
        raise MaterializationError("AGENT_SOURCE_FILE_TOO_LARGE", "Agent Git blob exceeds the per-file source limit")
    return size


def _read_blobs(repository: Path, specs: tuple[_BlobSpec, ...]) -> tuple[bytes, ...]:
    if not specs:
        return ()
    unique_specs = _unique_blob_specs(specs)
    with tempfile.TemporaryFile() as stderr_output:
        try:
            process = subprocess.Popen(
                _materializer_git_command(repository, ["cat-file", "--batch"]),
                cwd=repository,
                env=RAW_GIT_ENV,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_output,
            )
        except OSError as exc:
            raise MaterializationError("AGENT_SOURCE_GIT_UNAVAILABLE", "Unable to start the fixed Git binary") from exc
        blobs = _consume_batch_process(process, unique_specs)
    return tuple(blobs[item.object_id] for item in specs)


def _consume_batch_process(process: subprocess.Popen[bytes], specs: tuple[_BlobSpec, ...]) -> _BlobContents:
    if process.stdin is None or process.stdout is None:  # pragma: no cover - subprocess contract
        _stop_process(process)
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git batch pipes are unavailable")
    blobs: _BlobContents = {}
    try:
        for spec in specs:
            process.stdin.write(spec.object_id + b"\n")
            process.stdin.flush()
            blobs[spec.object_id] = _read_one_blob(process.stdout, spec)
        process.stdin.close()
        if process.wait(timeout=_GIT_TIMEOUT_SECONDS) != 0:
            raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file batch failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _stop_process(process)
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file batch did not complete") from exc
    except Exception:
        _stop_process(process)
        raise
    finally:
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
    return blobs


def _read_one_blob(source: BinaryIO, spec: _BlobSpec) -> bytes:
    header = source.readline(256)
    fields = header.rstrip(b"\n").split()
    if len(fields) != 3 or fields[0] != spec.object_id or fields[1] != b"blob":
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file returned an invalid blob header")
    try:
        reported_size = int(fields[2])
    except ValueError as exc:
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file returned an invalid blob size") from exc
    if reported_size != spec.size:
        raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git blob size changed while materializing the commit")
    content = _read_exact(source, reported_size)
    if source.read(1) != b"\n":
        raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file returned a truncated blob delimiter")
    return content


def _read_exact(source: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, _COPY_CHUNK_BYTES))
        if not chunk:
            raise MaterializationError("AGENT_SOURCE_GIT_PROTOCOL_ERROR", "Git cat-file returned a truncated blob")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unique_blob_specs(specs: tuple[_BlobSpec, ...]) -> tuple[_BlobSpec, ...]:
    unique: list[_BlobSpec] = []
    sizes: dict[bytes, int] = {}
    seen: set[bytes] = set()
    for spec in specs:
        known_size = sizes.setdefault(spec.object_id, spec.size)
        if known_size != spec.size:
            raise MaterializationError("AGENT_SOURCE_TREE_INVALID", "Git tree reports inconsistent blob sizes")
        if spec.object_id not in seen:
            seen.add(spec.object_id)
            unique.append(spec)
    return tuple(unique)


def _source_digest(specs: tuple[_BlobSpec, ...], contents: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256(b"agentgov-source-v1\0")
    for spec, content in zip(specs, contents, strict=True):
        path = spec.path.as_posix().encode("utf-8")
        digest.update(f"{spec.mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(path)
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _project_safe_config_files(specs: tuple[_BlobSpec, ...], contents: tuple[bytes, ...]) -> tuple[tuple[_BlobSpec, ...], tuple[bytes, ...]]:
    projected_specs: list[_BlobSpec] = []
    projected_contents: list[bytes] = []
    for spec, content in zip(specs, contents, strict=True):
        config_kind = _projected_config_kind(spec.path.parts)
        if config_kind is not None:
            try:
                if config_kind == "mcp":
                    _validate_mcp_config(content)
                else:
                    _validate_settings_config(content)
            except MaterializationError as exc:
                if exc.code in {"AGENT_SOURCE_MCP_SECRET_LITERAL", "AGENT_SOURCE_SETTINGS_SECRET_LITERAL"}:
                    continue
                raise
        projected_specs.append(spec)
        projected_contents.append(content)
    return tuple(projected_specs), tuple(projected_contents)


def _projected_config_kind(parts: tuple[str, ...]) -> str | None:
    folded = tuple(part.casefold() for part in parts)
    if folded[-1] == ".mcp.json":
        return "mcp"
    if len(folded) >= 2 and folded[-2:] == (".claude", "settings.json"):
        return "settings"
    return None


def _validate_mcp_config(content: bytes) -> None:
    if len(content) > _MAX_MCP_CONFIG_BYTES:
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP config exceeds the static lane limit")
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP config must be an unambiguous UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP config must be a JSON object")
    _validate_mcp_value(payload)


def _validate_settings_config(content: bytes) -> None:
    if len(content) > _MAX_MCP_CONFIG_BYTES:
        raise MaterializationError("AGENT_SOURCE_SETTINGS_CONFIG_INVALID", "Claude settings exceed the static lane limit")
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MaterializationError(
            "AGENT_SOURCE_SETTINGS_CONFIG_INVALID",
            "Claude settings must be an unambiguous UTF-8 JSON object",
        ) from exc
    if not isinstance(payload, dict):
        raise MaterializationError("AGENT_SOURCE_SETTINGS_CONFIG_INVALID", "Claude settings must be a JSON object")
    try:
        _validate_mcp_value(payload)
    except MaterializationError as exc:
        if exc.code == "AGENT_SOURCE_MCP_SECRET_LITERAL":
            raise MaterializationError(
                "AGENT_SOURCE_SETTINGS_SECRET_LITERAL",
                "Claude settings contain a credential literal",
            ) from exc
        raise


def _unique_json_object(pairs: list[tuple[str, object]]) -> _JsonObject:
    result: _JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_mcp_value(value: object) -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):  # pragma: no cover - JSON object keys are strings
                raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP config contains an invalid key")
            normalized_key = child_key.casefold().replace("-", "_")
            compact_key = re.sub(r"[^a-z0-9]", "", normalized_key)
            if normalized_key == "headers":
                _validate_mcp_headers(child_value)
            elif normalized_key == "env":
                _validate_mcp_env(child_value)
            elif normalized_key == "args":
                _validate_mcp_args(child_value)
            elif normalized_key in {"url", "endpoint", "base_url", "baseurl"}:
                _validate_mcp_url(child_value)
            elif _SENSITIVE_KEY.search(normalized_key) or _SENSITIVE_COMPACT_KEY.search(compact_key):
                _validate_placeholder_value(child_value)
            else:
                _validate_mcp_value(child_value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_mcp_value(item)
        return
    if isinstance(value, str):
        match = _INLINE_CREDENTIAL.search(value)
        if match and not _HEADER_PLACEHOLDER.fullmatch(match.group(1).strip()):
            raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP config contains an inline credential literal")


def _validate_mcp_args(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP args must be a list of strings")
    for index, item in enumerate(value):
        if _SENSITIVE_CLI_FLAG.fullmatch(item):
            following = value[index + 1] if index + 1 < len(value) else ""
            if not _HEADER_PLACEHOLDER.fullmatch(following):
                raise MaterializationError(
                    "AGENT_SOURCE_MCP_SECRET_LITERAL",
                    "MCP credential arguments must use environment placeholders",
                )
        _validate_mcp_value(item)


def _validate_mcp_headers(value: object) -> None:
    if not isinstance(value, dict):
        raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP headers must be a JSON object")
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP header values must use safe strings")
        name = raw_name.casefold()
        if raw_value == "" or _HEADER_PLACEHOLDER.fullmatch(raw_value):
            continue
        if name in _SAFE_LITERAL_HEADERS and _safe_header_literal(raw_value):
            continue
        raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP credential headers must use environment placeholders")


def _validate_mcp_env(value: object) -> None:
    if not isinstance(value, dict):
        raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP env must be a JSON object")
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP env values must use environment placeholders")
        if raw_value and not _ENV_PLACEHOLDER.fullmatch(raw_value):
            raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP env values must use environment placeholders")


def _validate_mcp_url(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP URL must be a non-empty string")
    if _ENV_PLACEHOLDER.fullmatch(value):
        return
    if any(ord(character) < 32 or character.isspace() for character in value):
        raise MaterializationError("AGENT_SOURCE_MCP_CONFIG_INVALID", "MCP URL contains unsafe characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP URL contains userinfo or unsupported components")
    for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        compact_key = re.sub(r"[^a-z0-9]", "", query_key.casefold())
        if (_SENSITIVE_KEY.search(query_key) or _SENSITIVE_COMPACT_KEY.search(compact_key)) and query_value and not _ENV_PLACEHOLDER.fullmatch(query_value):
            raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP URL credential query values must use placeholders")


def _validate_placeholder_value(value: object) -> None:
    if not isinstance(value, str) or (value and not _HEADER_PLACEHOLDER.fullmatch(value)):
        raise MaterializationError("AGENT_SOURCE_MCP_SECRET_LITERAL", "MCP credential values must use environment placeholders")


def _safe_header_literal(value: str) -> bool:
    return len(value.encode("utf-8")) <= 256 and not any(ord(character) < 32 or ord(character) == 127 for character in value)


def _write_destination(destination: Path, specs: tuple[_BlobSpec, ...], contents: tuple[bytes, ...]) -> None:
    _validate_destination(destination)
    created = False
    try:
        destination.mkdir(mode=0o700)
        created = True
        for spec, content in zip(specs, contents, strict=True):
            target = destination.joinpath(*spec.path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(content)
            target.chmod(spec.mode)
    except OSError as exc:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise MaterializationError("AGENT_SOURCE_WRITE_FAILED", "Unable to materialize Agent Git source") from exc


def _validate_destination(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise MaterializationError("AGENT_SOURCE_DESTINATION_INVALID", "Materialization destination must not already exist")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise MaterializationError("AGENT_SOURCE_DESTINATION_INVALID", "Materialization parent is unavailable") from exc
    if parent != destination.parent.absolute() or not parent.is_dir():
        raise MaterializationError("AGENT_SOURCE_DESTINATION_INVALID", "Materialization parent must be a real directory")


def _run_git_capture(
    repository: Path,
    args: list[str],
    *,
    max_output_bytes: int,
    failure_code: str,
) -> bytes:
    _validate_git_command(args)
    with tempfile.TemporaryFile() as stdout_output, tempfile.TemporaryFile() as stderr_output:
        try:
            result = subprocess.run(
                _materializer_git_command(repository, args),
                cwd=repository,
                env=RAW_GIT_ENV,
                stdin=subprocess.DEVNULL,
                stdout=stdout_output,
                stderr=stderr_output,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaterializationError(failure_code, "Git object query did not complete") from exc
        if result.returncode != 0:
            raise MaterializationError(failure_code, "Git object query failed")
        if stdout_output.tell() > max_output_bytes:
            raise MaterializationError("AGENT_SOURCE_TOO_LARGE", "Git object metadata exceeds the source limits")
        stdout_output.seek(0)
        return stdout_output.read(max_output_bytes + 1)


def _validate_git_command(args: list[str]) -> None:
    if len(args) == 3 and args[:2] == ["rev-parse", "--verify"]:
        object_spec = args[2]
        if object_spec.endswith("^{commit}") or object_spec.endswith("^{tree}"):
            if _FULL_SHA.fullmatch(object_spec.split("^", maxsplit=1)[0]):
                return
    if len(args) == 6 and args[:5] == ["ls-tree", "-r", "-z", "-l", "--full-tree"] and _FULL_SHA.fullmatch(args[5]):
        return
    raise MaterializationError("AGENT_SOURCE_GIT_COMMAND_FORBIDDEN", "Raw Git materialization attempted an unsupported command")


def _materializer_git_command(repository: Path, args: list[str]) -> list[str]:
    try:
        scope = require_governed_repository(repository)
    except GovernedGitEnvironmentError as exc:
        raise MaterializationError(
            "AGENT_SOURCE_REPOSITORY_INVALID",
            "Agent Git repository violates the governed source authority",
        ) from exc
    return [
        str(GIT_BINARY),
        "--no-pager",
        f"--git-dir={scope.git_dir}",
        f"--work-tree={scope.work_tree}",
        *args,
    ]


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
