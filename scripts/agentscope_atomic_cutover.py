#!/usr/bin/env python3
"""AgentScope fresh-epoch 的显式、可恢复、分阶段原子切换工具。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Final, NoReturn, NotRequired, TypeAlias, TypedDict, cast

from dotenv.parser import parse_stream

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime import sqlite_schema_contract as schema_contract  # noqa: E402

COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.yml"
LANGFUSE_COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.langfuse.yml"
SCHEMA_EPOCH: Final = schema_contract.CURRENT_SCHEMA_EPOCH
PREVIOUS_SCHEMA_EPOCH: Final = schema_contract.PREVIOUS_SCHEMA_EPOCH
LEGACY_SCHEMA_EPOCH: Final = schema_contract.LEGACY_SCHEMA_EPOCH
CURRENT_SCHEMA_CONTRACT_SHA256: Final = schema_contract.CURRENT_SCHEMA_CONTRACT_SHA256
PREVIOUS_SCHEMA_CONTRACT_SHA256: Final = schema_contract.PREVIOUS_SCHEMA_CONTRACT_SHA256
LEGACY_V1_SCHEMA_CONTRACT_SHA256: Final = schema_contract.LEGACY_V1_SCHEMA_CONTRACT_SHA256
HITL_FINGERPRINT_DATA_MIGRATION: Final = "agentscope-hitl-fingerprint-v1"
# 共享模块不导入 ORM；同一份规范化算法与 epoch 摘要同时约束应用启动和停服前分类。
PREPARE_CONFIRMATION: Final = "PREPARE-AGENTSCOPE-FRESH-EPOCH"
ACTIVE_RUN_STATUSES: Final = ("queued", "running", "waiting_human", "waiting_external", "finalizing")
MOUNT_KEYS: Final = (
    "HOST_DATA_MOUNT",
    "HOST_GOVERNOR_WORKSPACE_MOUNT",
    "HOST_AGENTSCOPE_RUNTIME_CANDIDATES_MOUNT",
    "HOST_AGENTSCOPE_RUNTIME_DATA_MOUNT",
    "HOST_AGENTSCOPE_RUNTIME_WORKSPACES_MOUNT",
    "LANGFUSE_POSTGRES_DATA_MOUNT",
    "LANGFUSE_CLICKHOUSE_DATA_MOUNT",
    "LANGFUSE_CLICKHOUSE_LOGS_MOUNT",
    "LANGFUSE_REDIS_DATA_MOUNT",
    "LANGFUSE_MINIO_DATA_MOUNT",
)


class CutoverError(RuntimeError):
    """切换前置、边界或证据不满足。"""


EnvValues: TypeAlias = dict[str, str]


class RuntimeEpoch(TypedDict):
    classification: str
    database: str
    tables: list[str]
    schema_versions: NotRequired[list[str]]
    legacy_tables: NotRequired[list[str]]
    physical_contract_sha256: NotRequired[str]


class ActiveWorkCounts(TypedDict):
    active_sessions: int
    active_runs: int
    hitl_waits: int
    active_tests: int
    active_agent_jobs: int
    active_publications: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bootstrap_module() -> ModuleType:
    module = _import_first(("scripts.agentscope_atomic_cutover_bootstrap", "agentscope_atomic_cutover_bootstrap"))
    if module is None:
        raise CutoverError("bootstrap cutover helper 缺失；拒绝 destructive cutover")
    return module


def _source_artifact_sha256() -> str:
    return cast(str, _bootstrap_module().source_artifact_sha256(REPO_ROOT))


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _operator_home() -> Path:
    module = _import_first(("scripts.agentscope_atomic_cutover_env", "agentscope_atomic_cutover_env"))
    if module is not None:
        return cast(Path, module.trusted_operator_identity().home)
    return Path(os.path.expanduser("~")).resolve()


def _require_privileged_mutation() -> None:
    if os.geteuid() != 0:
        raise CutoverError("atomic cutover mutating command 必须以 root 权限运行并保留 SUDO_UID")
    try:
        _bootstrap_module().trusted_operator_identity()
    except (KeyError, OSError, ValueError) as exc:
        raise CutoverError("无法建立可信 cutover operator identity") from exc


def _expand_env_value(value: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        host_capability = _operator_home().as_posix() if name == "HOME" else ""
        return values.get(name) or host_capability or default

    previous = value
    for _ in range(8):
        expanded = _ENV_REFERENCE.sub(replace, previous)
        if expanded == previous:
            break
        previous = expanded
    return os.path.expanduser(previous)


def load_env_file(path: Path) -> EnvValues:
    if not path.is_file():
        raise CutoverError(f"env 文件不存在: {path}")
    values: dict[str, str] = {}
    with path.open(encoding="utf-8") as stream:
        for binding in parse_stream(stream):
            if binding.error:
                raise CutoverError("env 格式无法安全解析")
            if binding.key is None:
                continue
            if binding.key in values:
                raise CutoverError(f"env 含重复配置: {binding.key}")
            values[binding.key] = _expand_env_value(binding.value or "", values)
    return values


def resolve_runtime_root(env_file: Path, explicit: Path | None = None, *, require_exists: bool) -> Path:
    env = load_env_file(env_file)
    configured = env.get("HOST_RUNTIME_VOLUME_ROOT", "").strip()
    if not configured:
        raise CutoverError("破坏性切换要求 env 显式设置 HOST_RUNTIME_VOLUME_ROOT")
    configured_path = Path(configured).expanduser()
    if not configured_path.is_absolute():
        raise CutoverError("HOST_RUNTIME_VOLUME_ROOT 必须是绝对路径")
    if configured_path.is_symlink():
        raise CutoverError("Runtime root 不得是符号链接")
    resolved = configured_path.resolve()
    if explicit is not None and explicit.expanduser().resolve() != resolved:
        raise CutoverError("--runtime-root 必须与所选 env 的 HOST_RUNTIME_VOLUME_ROOT 精确一致")
    expected_root = (_operator_home() / "volume-agent-gov").resolve()
    protected = (Path("/").resolve(), _operator_home(), REPO_ROOT.resolve(), Path.cwd().resolve(), env_file.resolve())
    if resolved != expected_root or any(item == resolved or item.is_relative_to(resolved) for item in protected):
        raise CutoverError(f"拒绝危险 Runtime root: {resolved}")
    if require_exists and (not resolved.is_dir() or resolved.is_symlink()):
        raise CutoverError(f"Runtime root 必须是已存在的真实目录: {resolved}")
    for key in MOUNT_KEYS:
        raw = env.get(key, "").strip()
        if not raw:
            continue
        mount = Path(_expand_env_value(raw, env)).expanduser().resolve()
        if not mount.is_relative_to(resolved):
            raise CutoverError(f"{key} 逃逸出 HOST_RUNTIME_VOLUME_ROOT，不能执行精确切换")
    return resolved


def _database_path(runtime_root: Path, env_file: Path) -> Path:
    env = load_env_file(env_file)
    data_mount = env.get("HOST_DATA_MOUNT", "").strip()
    data_root = Path(_expand_env_value(data_mount, env)).resolve() if data_mount else runtime_root / "data"
    if not data_root.is_relative_to(runtime_root):
        raise CutoverError("HOST_DATA_MOUNT 不在已校验 Runtime root 内")
    return data_root / "runtime.sqlite3"


def classify_runtime_epoch(db_path: Path) -> RuntimeEpoch:
    if not db_path.exists():
        return {"classification": "empty", "database": db_path.as_posix(), "tables": []}
    if not db_path.is_file() or db_path.is_symlink():
        raise CutoverError("Runtime database 必须是普通文件")
    try:
        with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as connection:
            inspection = schema_contract.inspect_sqlite_schema_epoch(
                connection,
                known_data_migration_markers=frozenset(
                    {HITL_FINGERPRINT_DATA_MIGRATION},
                ),
            )
            tables = list(inspection.tables)
            if not inspection.tables:
                return {"classification": "empty", "database": db_path.as_posix(), "tables": []}
            versions = list(inspection.schema_versions)
            physical_contract_sha256 = inspection.physical_contract_sha256
    except (IndexError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise CutoverError("Runtime database 无法只读分类") from exc
    legacy_names = [name for name in tables if "claude" in name.casefold() or re.search(r"(^|_)sdk(_|$)", name.casefold())]
    if legacy_names or inspection.classification is schema_contract.SqliteSchemaEpochClassification.LEGACY_UNSAFE_HISTORY:
        classification = "legacy-or-unknown"
    else:
        classification = inspection.classification.value
    return {
        "classification": classification,
        "database": db_path.as_posix(),
        "tables": tables,
        "schema_versions": versions,
        "legacy_tables": legacy_names,
        "physical_contract_sha256": physical_contract_sha256,
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _count_where(connection: sqlite3.Connection, table: str, clause: str, parameters: tuple[str, ...] = ()) -> int:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {clause}', parameters).fetchone()[0])


def active_work_counts(db_path: Path) -> ActiveWorkCounts:
    counters = ActiveWorkCounts(
        active_sessions=0,
        active_runs=0,
        hitl_waits=0,
        active_tests=0,
        active_agent_jobs=0,
        active_publications=0,
    )
    if not db_path.exists():
        return counters
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as connection:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" in tables and "active_run_id" in _table_columns(connection, "sessions"):
            counters["active_sessions"] += _count_where(connection, "sessions", "active_run_id IS NOT NULL")
        if "runtime_session_bindings" in tables and "active_run_id" in _table_columns(connection, "runtime_session_bindings"):
            counters["active_sessions"] += _count_where(connection, "runtime_session_bindings", "active_run_id IS NOT NULL")
        if "agent_runs" in tables and "status" in _table_columns(connection, "agent_runs"):
            counters["active_runs"] += _count_where(connection, "agent_runs", f"status IN ({placeholders})", ACTIVE_RUN_STATUSES)
        if "session_turn_intents" in tables:
            counters["active_runs"] += _count_where(connection, "session_turn_intents", "status = ?", ("running",))
        if "claude_user_input_requests" in tables:
            counters["hitl_waits"] += _count_where(connection, "claude_user_input_requests", "status = ?", ("waiting",))
        if "runtime_pending_actions" in tables:
            counters["hitl_waits"] += _count_where(connection, "runtime_pending_actions", "status = ?", ("pending",))
        if "agent_test_runs" in tables:
            counters["active_tests"] += _count_where(connection, "agent_test_runs", "status IN (?, ?)", ("queued", "running"))
        if "agent_jobs" in tables:
            counters["active_agent_jobs"] += _count_where(connection, "agent_jobs", "status IN (?, ?)", ("queued", "running"))
        if "agent_change_sets" in tables:
            counters["active_publications"] += _count_where(connection, "agent_change_sets", "status = ?", ("publishing",))
        if "agent_release_operations" in tables:
            counters["active_publications"] += _count_where(
                connection,
                "agent_release_operations",
                "status IN (?, ?)",
                ("reserved", "git_applied"),
            )
        if "execution_records" in tables:
            counters["active_publications"] += _count_where(connection, "execution_records", "status = ?", ("applying",))
    return counters


def _compose_base(env_file: Path, compose_file: Path = COMPOSE_FILE) -> list[str]:
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file)]
    if compose_file.resolve() == COMPOSE_FILE.resolve():
        command.extend(["-f", str(LANGFUSE_COMPOSE_FILE)])
    return command


def require_compose_project_stopped(env_file: Path) -> None:
    env = load_env_file(env_file)
    project = env.get("COMPOSE_PROJECT_NAME", "agent-gov")
    child_env = cast(dict[str, str], _bootstrap_module().selected_env_child_env(env_file))
    try:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=child_env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CutoverError("无法确认维护窗口中的 Compose 项目已停止") from exc
    if result.stdout.strip():
        raise CutoverError("维护闸未关闭：目标 Compose project 仍有容器；先显式 down 后再 prepare/execute")


def _import_first(module_names: tuple[str, ...]) -> ModuleType | None:
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            top_level = module_name.partition(".")[0]
            if exc.name not in {module_name, top_level}:
                raise
    return None


def command_inspect(args: argparse.Namespace) -> int:
    env_file = args.env_file.resolve()
    runtime_root = resolve_runtime_root(env_file, args.runtime_root, require_exists=False)
    result = classify_runtime_epoch(_database_path(runtime_root, env_file))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    allowed = {
        "empty",
        "agentscope",
        "agentscope-v2-migratable",
        "agentscope-v1-migratable",
    }
    if args.require_current_or_empty and result["classification"] not in allowed:
        raise CutoverError("检测到未知、漂移或不可安全迁移的 Runtime schema；普通 deploy 禁止启动")
    return 0


def command_maintenance_down(_args: argparse.Namespace) -> int:
    _reject_automatic_destructive_cutover("maintenance-down")


def command_prepare(_args: argparse.Namespace) -> int:
    _reject_automatic_destructive_cutover("prepare")


def _reject_automatic_destructive_cutover(command: str) -> NoReturn:
    raise CutoverError(
        f"{command} 已安全禁用：整根 Runtime 原子切换的 crash-resume、跨入口互斥与 root filesystem custody 尚未形成可证明闭环；请勿自动清空或恢复持久化数据。"
    )


def command_execute(_args: argparse.Namespace) -> int:
    _reject_automatic_destructive_cutover("execute")


def command_finalize(_args: argparse.Namespace) -> int:
    _reject_automatic_destructive_cutover("finalize/recover-finalize")


def command_restore(_args: argparse.Namespace) -> int:
    _reject_automatic_destructive_cutover("restore")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentScope fresh schema 的显式 atomic cutover 工具。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="只读判断 Runtime DB epoch；普通 deploy 使用。")
    inspect_parser.add_argument("--env-file", type=Path, required=True)
    inspect_parser.add_argument("--runtime-root", type=Path)
    inspect_parser.add_argument("--require-current-or-empty", action="store_true")
    inspect_parser.set_defaults(handler=command_inspect)

    maintenance = subparsers.add_parser("maintenance-down", help="已退役；使用 selected-env make down。")
    maintenance.set_defaults(handler=command_maintenance_down)

    prepare_parser = subparsers.add_parser("prepare", help="已退役；不再执行高权限冷备/旧栈 drill。")
    prepare_parser.set_defaults(handler=command_prepare)

    for name, handler in (
        ("execute", command_execute),
        ("finalize", command_finalize),
        ("recover-finalize", command_finalize),
        ("restore", command_restore),
    ):
        subparser = subparsers.add_parser(name, help="已退役并固定 fail closed。")
        subparser.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return int(args.handler(args))
        if args.command in {"maintenance-down", "prepare", "execute", "finalize", "recover-finalize", "restore"}:
            return int(args.handler(args))
        _require_privileged_mutation()
        try:
            from scripts.agentscope_atomic_cutover_lock import run_with_global_cutover_lock
        except ModuleNotFoundError:
            from agentscope_atomic_cutover_lock import run_with_global_cutover_lock
        return int(run_with_global_cutover_lock(CutoverError, lambda: args.handler(args)))
    except CutoverError as exc:
        print(f"AGENTSCOPE_CUTOVER_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
