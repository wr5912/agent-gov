"""在 fixed Python fd 内仅加载候选源码与候选依赖快照。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypedDict, cast

PYTHON_AUTHORITY_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY"
PYTHON_AUTHORITY_SHA256_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_AUTHORITY_SHA256"
FRONTEND_DEPENDENCY_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT"
PYTHON_SITE_PACKAGES_ENV: Final = "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_SCRIPT_BYTES: Final = 8 * 1024 * 1024


class PythonRunnerAuthorityError(RuntimeError):
    """受管 Python child 未绑定候选依赖快照。"""


class ManagedPythonAuthorityRecord(TypedDict):
    command: str
    invocation_path: str
    resolved_path: str
    sha256: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    invocation_device: int
    invocation_inode: int
    invocation_mode: int
    invocation_uid: int
    invocation_gid: int
    invocation_mtime_ns: int
    invocation_ctime_ns: int
    leaf_link: str | None
    ancestor_authority_sha256: str


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _python_record(environ: Mapping[str, str]) -> ManagedPythonAuthorityRecord:
    raw = environ.get(PYTHON_AUTHORITY_ENV, "")
    digest = environ.get(PYTHON_AUTHORITY_SHA256_ENV, "")
    if not raw or len(raw.encode()) > 32 * 1024 or _SHA256.fullmatch(digest) is None:
        raise PythonRunnerAuthorityError("managed Python authority is invalid")
    try:
        record = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise PythonRunnerAuthorityError("managed Python authority is invalid") from exc
    if (
        not isinstance(record, dict)
        or record.get("command") != "python"
        or hashlib.sha256(_canonical_json(record)).hexdigest() != digest
        or Path(str(record.get("invocation_path"))) != Path(sys.executable)
    ):
        raise PythonRunnerAuthorityError("managed Python authority is invalid")
    return cast(ManagedPythonAuthorityRecord, record)


def _dependency_paths(repository: Path, environ: Mapping[str, str]) -> tuple[Path, Path]:
    frontend = Path(environ.get(FRONTEND_DEPENDENCY_ROOT_ENV, ""))
    python = Path(environ.get(PYTHON_SITE_PACKAGES_ENV, ""))
    valid = (
        repository.is_absolute()
        and frontend == repository / "frontend/node_modules"
        and python == repository.parent / "dependencies/python-site-packages"
        and frontend.is_dir()
        and python.is_dir()
    )
    for prefix in ("FRONTEND", "PYTHON"):
        digest = environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_SHA256", "")
        entries = environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_ENTRIES", "")
        regular_bytes = environ.get(f"AGENT_GOV_ACCEPTANCE_{prefix}_DEPENDENCIES_BYTES", "")
        valid = valid and _SHA256.fullmatch(digest) is not None and entries.isdecimal() and regular_bytes.isdecimal()
    if not valid:
        raise PythonRunnerAuthorityError("candidate Python dependency authority is invalid")
    return frontend, python


def _stdlib_paths(repository: Path, python_dependencies: Path) -> list[str]:
    accepted: list[str] = []
    for raw in sys.path:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute() or path in (repository, python_dependencies) or "site-packages" in path.parts:
            continue
        accepted.append(str(path))
    if not accepted:
        raise PythonRunnerAuthorityError("fixed Python stdlib authority is unavailable")
    return accepted


def _read_script(path: Path) -> bytes:
    descriptor = os.open(path, _FILE_FLAGS)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SCRIPT_BYTES:
            raise PythonRunnerAuthorityError("candidate Python script authority is invalid")
        chunks: list[bytes] = []
        remaining = _MAX_SCRIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        linked = os.stat(path, follow_symlinks=False)
        current = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        reopened = os.fstat(descriptor)
        linked_values = (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_size, linked.st_mtime_ns, linked.st_ctime_ns)
        reopened_values = (reopened.st_dev, reopened.st_ino, reopened.st_mode, reopened.st_size, reopened.st_mtime_ns, reopened.st_ctime_ns)
        if len(encoded) > _MAX_SCRIPT_BYTES or current != reopened_values or current != linked_values:
            raise PythonRunnerAuthorityError("candidate Python script authority drifted")
        return encoded
    finally:
        os.close(descriptor)


def run(arguments: list[str], repository: Path, environ: Mapping[str, str]) -> None:
    if not arguments or not sys.flags.isolated or not sys.flags.safe_path or not sys.flags.no_site:
        raise PythonRunnerAuthorityError("fixed Python isolation flags are missing")
    _python_record(environ)
    _frontend, python_dependencies = _dependency_paths(repository, environ)
    testkit = repository / "packages/agentgov-testkit/src"
    sys.path[:] = [str(repository), str(testkit), str(python_dependencies), *_stdlib_paths(repository, python_dependencies)]
    if arguments[0] == "-m":
        if len(arguments) < 2 or _MODULE.fullmatch(arguments[1]) is None:
            raise PythonRunnerAuthorityError("fixed acceptance Python module is invalid")
        sys.argv = arguments[1:]
        runpy.run_module(arguments[1], run_name="__main__", alter_sys=True)
        return
    script = Path(arguments[0]) if Path(arguments[0]).is_absolute() else repository / arguments[0]
    absolute = Path(os.path.abspath(script))
    try:
        absolute.relative_to(repository)
    except ValueError as exc:
        raise PythonRunnerAuthorityError("fixed acceptance Python script is outside the candidate") from exc
    encoded = _read_script(absolute)
    sys.argv = [str(absolute), *arguments[1:]]
    namespace = {"__name__": "__main__", "__file__": str(absolute), "__package__": None, "__spec__": None}
    exec(compile(encoded, str(absolute), "exec"), namespace)
