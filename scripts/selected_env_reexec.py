"""selected-env live trust anchor 到冻结 runner 的单向重执行契约。"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts import selected_env_source_snapshot as source_snapshot
from scripts.agentscope_atomic_cutover_bootstrap import freeze_deployable_source, source_artifact_sha256
from scripts.agentscope_atomic_cutover_env import verify_stable_env_file
from scripts.selected_env_operation_contract import DOCKER_BIND_OPERATIONS, OperationEnvironment, SelectedEnvError

STAGE_ENV = "AGENTGOV_SELECTED_ENV_FROZEN_STAGE"
ORIGINAL_ENV_PATH_ENV = "AGENTGOV_SELECTED_ENV_ORIGINAL_PATH"
ORIGINAL_ENV_IDENTITY_ENV = "AGENTGOV_SELECTED_ENV_ORIGINAL_IDENTITY"
ORIGINAL_ENV_DIGEST_ENV = "AGENTGOV_SELECTED_ENV_ORIGINAL_SHA256"
LIVE_REPO_ROOT_ENV = "AGENTGOV_SELECTED_ENV_LIVE_REPO_ROOT"
_STAGE_VALUE = "frozen-runner-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class FrozenStageState:
    original_env: Path
    original_identity: tuple[int, ...]
    original_digest: str
    live_repo_root: Path


def freeze_deployment_source(
    repo_root: Path,
    operation: str,
    operation_root: Path,
    env_snapshot: Path,
) -> tuple[source_snapshot.FrozenDeploymentSource, str]:
    try:
        frozen = source_snapshot.freeze_operation_source(
            repo_root,
            operation_root,
            env_snapshot,
            persistent_binds=operation in DOCKER_BIND_OPERATIONS,
            freeze_source=freeze_deployable_source,
            hash_source=source_artifact_sha256,
        )
        version = (frozen.root / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise SelectedEnvError("无法固定 deployable source") from exc
    if not version:
        raise SelectedEnvError("VERSION 不得为空")
    return frozen, version


def stage_environment(
    child_env: dict[str, str],
    *,
    original_env: Path,
    original_identity: tuple[int, ...],
    original_payload: bytes,
    live_repo_root: Path,
) -> OperationEnvironment:
    if len(original_identity) != 9 or not all(type(value) is int for value in original_identity):
        raise SelectedEnvError("原始 selected.env identity 结构无效")
    values = dict(child_env)
    values.update(
        {
            STAGE_ENV: _STAGE_VALUE,
            ORIGINAL_ENV_PATH_ENV: _absolute_real_path(original_env, "原始 selected.env").as_posix(),
            ORIGINAL_ENV_IDENTITY_ENV: ",".join(str(value) for value in original_identity),
            ORIGINAL_ENV_DIGEST_ENV: hashlib.sha256(original_payload).hexdigest(),
            LIVE_REPO_ROOT_ENV: _absolute_real_path(live_repo_root, "live repository").as_posix(),
        }
    )
    return values


def is_frozen_stage(environment: Mapping[str, str]) -> bool:
    return environment.get(STAGE_ENV) == _STAGE_VALUE


def load_frozen_stage(environment: Mapping[str, str]) -> FrozenStageState:
    if not is_frozen_stage(environment):
        raise SelectedEnvError("冻结 selected-env runner 缺少 stage marker")
    raw_identity = environment.get(ORIGINAL_ENV_IDENTITY_ENV, "")
    try:
        identity = tuple(int(value) for value in raw_identity.split(","))
    except ValueError as exc:
        raise SelectedEnvError("冻结 selected-env runner identity 无效") from exc
    digest = environment.get(ORIGINAL_ENV_DIGEST_ENV, "")
    if len(identity) != 9 or _SHA256.fullmatch(digest) is None:
        raise SelectedEnvError("冻结 selected-env runner state 无效")
    return FrozenStageState(
        original_env=_absolute_real_path(Path(environment.get(ORIGINAL_ENV_PATH_ENV, "")), "原始 selected.env"),
        original_identity=identity,
        original_digest=digest,
        live_repo_root=_absolute_real_path(Path(environment.get(LIVE_REPO_ROOT_ENV, "")), "live repository"),
    )


def frozen_command(
    source_root: Path,
    snapshot: Path,
    source_base: Path,
    operation: str,
    *,
    no_build: bool,
    force_recreate: bool,
    require_idle: bool = False,
) -> list[str]:
    command = [
        "python",
        (source_root / "scripts/run_selected_env_operation.py").as_posix(),
        "--env-file",
        snapshot.as_posix(),
        "--env-base-dir",
        source_base.as_posix(),
        "--operation",
        operation,
    ]
    if no_build:
        command.append("--no-build")
    if force_recreate:
        command.append("--force-recreate")
    if require_idle:
        command.append("--require-idle")
    return command


def verify_running_from_frozen_source(source_root: Path, runner_file: Path) -> None:
    expected = source_root / "scripts/run_selected_env_operation.py"
    try:
        if runner_file.resolve(strict=True) != expected.resolve(strict=True):
            raise SelectedEnvError("selected-env mutation runner 未从冻结 source 执行")
    except OSError as exc:
        raise SelectedEnvError("无法复验冻结 selected-env runner 路径") from exc


def verify_stage_postconditions(snapshot: Path, child_env: Mapping[str, str], digest: str) -> None:
    state = load_frozen_stage(child_env)
    source_snapshot.verify_command_source(child_env, hash_source=source_artifact_sha256)
    payload = snapshot.read_bytes()
    verify_stable_env_file(state.original_env, payload, state.original_identity, error_type=SelectedEnvError)
    if source_artifact_sha256(state.live_repo_root) != digest:
        raise SelectedEnvError("deployable source 在部署事务期间发生变化；镜像/容器结果拒绝放行")


def resolve_source_base(original_env: Path, configured: Path | None) -> Path:
    source_base = Path(os.path.abspath(configured or original_env.parent))
    try:
        resolved = source_base.resolve(strict=True)
    except OSError as exc:
        raise SelectedEnvError("所选 env 的原始基准目录无效") from exc
    if source_base.is_symlink() or resolved != source_base or not source_base.is_dir():
        raise SelectedEnvError("所选 env 的原始基准目录无效")
    return source_base


def _absolute_real_path(path: Path, boundary: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise SelectedEnvError(f"{boundary} 路径无效")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SelectedEnvError(f"{boundary} 路径无效") from exc
    if resolved != path:
        raise SelectedEnvError(f"{boundary} 路径无效")
    return path
