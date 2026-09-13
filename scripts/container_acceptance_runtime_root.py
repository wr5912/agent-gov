"""隔离容器验收的一次性 Runtime root 授权回执。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict, cast

from scripts.agentscope_atomic_cutover_env import parse_selected_env_payload, read_stable_env_file
from scripts.container_acceptance_inputs import (
    ACCEPTANCE_ACTIVE_ENV,
    ACCEPTANCE_CONTEXT_ENV,
    ACCEPTANCE_PROFILE_ENV,
    ACCEPTANCE_RUN_ID_ENV,
    AcceptanceError,
)

BOOTSTRAP_AUTH_SECRET_ENV: Final = "AGENT_GOV_ACCEPTANCE_BOOTSTRAP_AUTH_SECRET"
_SCHEMA_VERSION: Final = 1
_RUN_ID_PATTERN: Final = re.compile(r"[1-9][0-9]*-(?P<token>[0-9a-f]{12})")


class _BootstrapAuthorizationBody(TypedDict):
    schema_version: int
    runner_pid: int
    run_id: str
    profile: str
    project_name: str
    runtime_root: str
    effective_env: str
    effective_env_sha256: str


class _BootstrapAuthorizationReceipt(_BootstrapAuthorizationBody):
    signature: str


@dataclass(frozen=True)
class _BoundAcceptancePaths:
    context_file: Path
    effective_env: Path
    run_id: str
    profile: str
    project_name: str


def _require_private_directory(path: Path, *, label: str, mode: int = 0o700) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AcceptanceError(f"{label}不可用") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or path.resolve(strict=True) != path
    ):
        raise AcceptanceError(f"{label}必须是当前用户持有的 {mode:04o} 真实目录")


def _absolute_path(raw: str, *, label: str) -> Path:
    path = Path(raw)
    absolute = Path(os.path.abspath(path))
    if not path.is_absolute() or path != absolute:
        raise AcceptanceError(f"{label}必须是无歧义绝对路径")
    return absolute


def _bind_acceptance_paths(runtime_root: Path, current: dict[str, str]) -> _BoundAcceptancePaths:
    if current.get(ACCEPTANCE_ACTIVE_ENV) != "1":
        raise AcceptanceError("隔离 Runtime root 只能由公共容器验收 runner 授权")
    run_id = current.get(ACCEPTANCE_RUN_ID_ENV, "")
    match = _RUN_ID_PATTERN.fullmatch(run_id)
    profile = current.get(ACCEPTANCE_PROFILE_ENV, "")
    if match is None or profile not in {"core", "langfuse"}:
        raise AcceptanceError("隔离容器验收的 run id 或 profile 无效")
    normalized_root = _absolute_path(str(runtime_root), label="隔离 Runtime root")
    parent = normalized_root.parent
    _require_private_directory(parent, label="隔离验收临时根")
    _require_private_directory(normalized_root, label="隔离 Runtime root")
    expected_prefix = f"agentgov-acceptance-{os.geteuid()}-"
    if parent.parent != Path(tempfile.gettempdir()).resolve() or not parent.name.startswith(expected_prefix):
        raise AcceptanceError("隔离 Runtime root 不属于 runner 临时目录")
    frozen_inputs_root = parent / "acceptance-inputs"
    context_root = parent / "acceptance-context"
    _require_private_directory(frozen_inputs_root, label="验收固定输入目录", mode=0o500)
    _require_private_directory(context_root, label="验收授权回执目录")
    context_file = _absolute_path(current.get(ACCEPTANCE_CONTEXT_ENV, ""), label="验收授权回执")
    effective_env = _absolute_path(current.get("COMPOSE_ENV_FILE", ""), label="验收有效 env")
    project_name = f"agv-acceptance-{os.geteuid()}-{match.group('token')}"
    expected_values = {
        "AGENT_GOV_COMPOSE_ENV_FILE": effective_env.as_posix(),
        "COMPOSE_PROJECT_NAME": project_name,
        "CONTAINER_NAME_PREFIX": project_name,
        "HOST_RUNTIME_VOLUME_ROOT": normalized_root.as_posix(),
    }
    if (
        normalized_root != parent / "runtime-root"
        or context_file != context_root / "acceptance-context.json"
        or effective_env != frozen_inputs_root / "compose.acceptance.env"
        or any(current.get(key) != value for key, value in expected_values.items())
    ):
        raise AcceptanceError("隔离验收路径或 project 绑定不精确")
    return _BoundAcceptancePaths(context_file, effective_env, run_id, profile, project_name)


def _effective_env_sha256(paths: _BoundAcceptancePaths, runtime_root: Path) -> str:
    raw, identity = read_stable_env_file(paths.effective_env, error_type=AcceptanceError)
    if identity[4] != os.geteuid() or stat.S_IMODE(identity[2]) != 0o400:
        raise AcceptanceError("验收有效 env 必须由当前用户持有且权限为 0400")
    try:
        values = {binding.key: binding.value or "" for binding in parse_selected_env_payload(raw) if binding.key}
    except ValueError as exc:
        raise AcceptanceError("验收有效 env 无法安全解析") from exc
    expected = {
        "COMPOSE_PROJECT_NAME": paths.project_name,
        "CONTAINER_NAME_PREFIX": paths.project_name,
        "HOST_RUNTIME_VOLUME_ROOT": runtime_root.as_posix(),
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise AcceptanceError("验收有效 env 与 runner 隔离上下文不一致")
    return hashlib.sha256(raw).hexdigest()


def _signature(body: _BootstrapAuthorizationBody, secret: bytes) -> str:
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, serialized, hashlib.sha256).hexdigest()


def _write_private_receipt(path: Path, receipt: _BootstrapAuthorizationReceipt) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        rendered = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(rendered)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def authorized_runtime_bootstrap_env(runtime_root: Path, environ: dict[str, str]) -> Iterator[dict[str, str]]:
    """为 runner 的单次 bootstrap 子进程生成最小能力环境。"""

    current = dict(environ)
    paths = _bind_acceptance_paths(runtime_root, current)
    secret = secrets.token_bytes(32)
    body = _BootstrapAuthorizationBody(
        schema_version=_SCHEMA_VERSION,
        runner_pid=os.getpid(),
        run_id=paths.run_id,
        profile=paths.profile,
        project_name=paths.project_name,
        runtime_root=runtime_root.as_posix(),
        effective_env=paths.effective_env.as_posix(),
        effective_env_sha256=_effective_env_sha256(paths, runtime_root),
    )
    receipt = _BootstrapAuthorizationReceipt(**body, signature=_signature(body, secret))
    _write_private_receipt(paths.context_file, receipt)
    current[BOOTSTRAP_AUTH_SECRET_ENV] = secret.hex()
    try:
        yield current
    finally:
        current.pop(BOOTSTRAP_AUTH_SECRET_ENV, None)
        paths.context_file.unlink(missing_ok=True)


def verify_runtime_bootstrap_authorization(runtime_root: Path, environ: dict[str, str] | None = None) -> None:
    """验证当前进程是 runner 直接启动的一次性 bootstrap 子进程。"""

    current = dict(os.environ if environ is None else environ)
    paths = _bind_acceptance_paths(runtime_root, current)
    raw_secret = current.get(BOOTSTRAP_AUTH_SECRET_ENV, "")
    if re.fullmatch(r"[0-9a-f]{64}", raw_secret) is None:
        raise AcceptanceError("隔离 Runtime root 缺少一次性授权能力")
    raw, identity = read_stable_env_file(paths.context_file, error_type=AcceptanceError)
    if identity[4] != os.geteuid() or stat.S_IMODE(identity[2]) != 0o600:
        raise AcceptanceError("验收授权回执必须由当前用户持有且权限为 0600")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("验收授权回执不是有效 JSON") from exc
    expected_keys = {*_BootstrapAuthorizationBody.__required_keys__, "signature"}
    if not isinstance(decoded, dict) or set(decoded) != expected_keys:
        raise AcceptanceError("验收授权回执 schema 不精确")
    receipt = cast(_BootstrapAuthorizationReceipt, decoded)
    body = _BootstrapAuthorizationBody(**{key: receipt[key] for key in _BootstrapAuthorizationBody.__required_keys__})
    expected_body = _BootstrapAuthorizationBody(
        schema_version=_SCHEMA_VERSION,
        runner_pid=os.getppid(),
        run_id=paths.run_id,
        profile=paths.profile,
        project_name=paths.project_name,
        runtime_root=runtime_root.as_posix(),
        effective_env=paths.effective_env.as_posix(),
        effective_env_sha256=_effective_env_sha256(paths, runtime_root),
    )
    supplied_signature = receipt.get("signature")
    if (
        body != expected_body
        or not isinstance(supplied_signature, str)
        or not hmac.compare_digest(
            supplied_signature,
            _signature(body, bytes.fromhex(raw_secret)),
        )
    ):
        raise AcceptanceError("隔离 Runtime root 的 runner 授权回执无效")
