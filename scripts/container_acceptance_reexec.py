"""State transfer into the frozen formal-acceptance runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NoReturn, TypeVar

from scripts.container_acceptance_environment import PROFILES, AcceptanceProfile, IsolatedEnvironment
from scripts.container_acceptance_inputs import ScenarioFileSnapshot
from scripts.container_acceptance_materialization import ExecutionMutationGuard
from scripts.container_acceptance_toolchain import FORMAL_SOURCE_ROOT_ENV, TOOL_PATH_ENV_KEYS

REEXEC_STATE_ENV = "AGENTGOV_ACCEPTANCE_FROZEN_REEXEC_STATE"
REEXEC_GUARD_FD_ENV = "AGENTGOV_ACCEPTANCE_FROZEN_GUARD_FD"
REEXEC_LOCK_FD_ENV = "AGENTGOV_ACCEPTANCE_FROZEN_LOCK_FD"
_E = TypeVar("_E", bound=Exception)
_STATE_KEYS = frozenset(
    {
        "profile",
        "source_env",
        "command",
        "isolation",
        "snapshots",
        "initial_source_fingerprint",
        "source_env_identity",
    }
)


def _fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def encode_state(
    profile: AcceptanceProfile,
    source_env: Path,
    command: list[str],
    isolation: IsolatedEnvironment,
    snapshots: tuple[ScenarioFileSnapshot, ...],
    initial_source_fingerprint: str,
    source_env_identity: tuple[int, ...],
) -> str:
    payload = {
        "profile": profile.name,
        "source_env": str(source_env),
        "command": command,
        "isolation": {
            "env_file": str(isolation.env_file),
            "runtime_root": str(isolation.runtime_root),
            "project_name": isolation.project_name,
            "container_prefix": isolation.container_prefix,
            "overrides": isolation.overrides,
            "source_root": str(isolation.source_root),
        },
        "snapshots": [
            {
                "environment_key": item.environment_key,
                "path": str(item.path),
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in snapshots
        ],
        "initial_source_fingerprint": initial_source_fingerprint,
        "source_env_identity": list(source_env_identity),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def exec_frozen_runner(
    state: str,
    environ: dict[str, str],
    guard: ExecutionMutationGuard,
    lock_fd: int,
    *,
    error_type: type[_E],
) -> NoReturn:
    try:
        os.set_inheritable(lock_fd, True)
        child = dict(environ)
        child[REEXEC_STATE_ENV] = state
        child[REEXEC_GUARD_FD_ENV] = str(guard.make_inheritable())
        child[REEXEC_LOCK_FD_ENV] = str(lock_fd)
        python = Path(child[TOOL_PATH_ENV_KEYS["python"]])
        formal_root = Path(child[FORMAL_SOURCE_ROOT_ENV])
        runner = formal_root / "scripts/run_container_acceptance.py"
        python.relative_to(Path(child["AGENTGOV_ACCEPTANCE_TOOLCHAIN_ROOT"]))
        runner.relative_to(formal_root)
        os.execve(python, [str(python), str(runner), "--frozen-resume"], child)
    except (OSError, KeyError, ValueError) as exc:
        _fail(error_type, "无法 re-exec 已物化的冻结验收 runner", exc)


def decode_state(
    environ: dict[str, str],
    *,
    error_type: type[_E],
) -> tuple[
    AcceptanceProfile,
    Path,
    list[str],
    IsolatedEnvironment,
    tuple[ScenarioFileSnapshot, ...],
    str,
    tuple[int, ...],
    ExecutionMutationGuard,
]:
    try:
        value = json.loads(environ[REEXEC_STATE_ENV])
        descriptor = int(environ[REEXEC_GUARD_FD_ENV])
        lock_fd = int(environ[REEXEC_LOCK_FD_ENV])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        _fail(error_type, "冻结 runner 缺少有效 resume state", exc)
    if not isinstance(value, dict) or set(value) != _STATE_KEYS:
        _fail(error_type, "冻结 runner resume state schema 不精确")
    profile_name = value.get("profile")
    source_env = value.get("source_env")
    command = value.get("command")
    isolation_value = value.get("isolation")
    snapshots_value = value.get("snapshots")
    fingerprint = value.get("initial_source_fingerprint")
    identity = value.get("source_env_identity")
    if (
        profile_name not in PROFILES
        or not isinstance(source_env, str)
        or not isinstance(command, list)
        or not all(isinstance(item, str) for item in command)
        or not isinstance(isolation_value, dict)
        or not isinstance(snapshots_value, list)
        or not isinstance(fingerprint, str)
        or not isinstance(identity, list)
        or not all(isinstance(item, int) for item in identity)
    ):
        _fail(error_type, "冻结 runner resume state 类型无效")
    isolation = _decode_isolation(isolation_value, error_type=error_type)
    snapshots = tuple(_decode_snapshot(item, error_type=error_type) for item in snapshots_value)
    try:
        os.set_inheritable(lock_fd, False)
    except OSError as exc:
        _fail(error_type, "冻结 runner 的全局锁未跨 exec 保留", exc)
    guard = ExecutionMutationGuard.from_inherited(descriptor, error_type=error_type)
    return PROFILES[profile_name], Path(source_env), command, isolation, snapshots, fingerprint, tuple(identity), guard


def _decode_isolation(value: dict[object, object], *, error_type: type[_E]) -> IsolatedEnvironment:
    expected = {"env_file", "runtime_root", "project_name", "container_prefix", "overrides", "source_root"}
    if set(value) != expected or not all(isinstance(value.get(key), str) for key in expected - {"overrides"}):
        _fail(error_type, "冻结 runner isolation schema 无效")
    overrides = value.get("overrides")
    if not isinstance(overrides, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in overrides.items()):
        _fail(error_type, "冻结 runner isolation overrides 无效")
    return IsolatedEnvironment(
        env_file=Path(str(value["env_file"])),
        runtime_root=Path(str(value["runtime_root"])),
        project_name=str(value["project_name"]),
        container_prefix=str(value["container_prefix"]),
        overrides=overrides,
        source_root=Path(str(value["source_root"])),
    )


def _decode_snapshot(value: object, *, error_type: type[_E]) -> ScenarioFileSnapshot:
    expected = {"environment_key", "path", "size_bytes", "sha256"}
    if not isinstance(value, dict) or set(value) != expected:
        _fail(error_type, "冻结 runner scenario schema 无效")
    if not isinstance(value["environment_key"], str) or not isinstance(value["path"], str):
        _fail(error_type, "冻结 runner scenario 路径无效")
    if not isinstance(value["size_bytes"], int) or not isinstance(value["sha256"], str):
        _fail(error_type, "冻结 runner scenario identity 无效")
    return ScenarioFileSnapshot(
        environment_key=value["environment_key"],
        path=Path(value["path"]),
        size_bytes=value["size_bytes"],
        sha256=value["sha256"],
    )
