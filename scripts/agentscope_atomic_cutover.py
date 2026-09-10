#!/usr/bin/env python3
"""AgentScope fresh-epoch 的显式、可恢复、分阶段原子切换工具。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Final, NotRequired, TypeAlias, TypedDict, cast

if TYPE_CHECKING:
    from agentscope_atomic_cutover_evidence import CutoverEvidenceSupport
    from agentscope_atomic_cutover_recovery import CutoverRecoverySupport
    from agentscope_atomic_cutover_support import (
        CutoverSupport,
        RollbackBundle,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.yml"
LANGFUSE_COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.langfuse.yml"
SCHEMA_EPOCH: Final = "agentscope-runtime-v1"
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
FINAL_EVIDENCE_KEYS: Final = (
    "static_gates",
    "contract_tests",
    "container_acceptance",
    "browser_acceptance",
    "live_runtime",
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


def _source_artifact_sha256() -> str:
    """Hash deployable source without reading deployment env secrets."""

    candidates = (
        REPO_ROOT / "app",
        REPO_ROOT / "agentscope_runtime",
        REPO_ROOT / "scripts",
        REPO_ROOT / "frontend",
        REPO_ROOT / "packages/agentgov-testkit",
        REPO_ROOT / "docker/runtime-bootstrap",
        REPO_ROOT / "agentgov_harness_digest.py",
        REPO_ROOT / "Makefile",
        REPO_ROOT / "VERSION",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "requirements.txt",
        REPO_ROOT / "requirements-api.txt",
        REPO_ROOT / "docker/docker-compose.yml",
        REPO_ROOT / "docker/docker-compose.langfuse.yml",
        REPO_ROOT / "docker/api-gate/api-gate-state.json",
        REPO_ROOT / "docker/Dockerfile",
        REPO_ROOT / "docker/Dockerfile.dockerignore",
        REPO_ROOT / "docker/frontend.Dockerfile",
        REPO_ROOT / "docker/frontend.Dockerfile.dockerignore",
        REPO_ROOT / "docker/agentscope-runtime.Dockerfile",
        REPO_ROOT / "docker/agentscope-runtime.Dockerfile.dockerignore",
    )
    files: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir():
            files.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not {"__pycache__", "node_modules", "dist"}.intersection(path.parts)
                and path.suffix not in {".pyc", ".pyo", ".tsbuildinfo"}
                and path.relative_to(REPO_ROOT).as_posix() not in {"frontend/.env", "frontend/.env.local"}
            )
        elif candidate.is_file() and not candidate.is_symlink():
            files.append(candidate)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(REPO_ROOT).as_posix()):
        relative = path.relative_to(REPO_ROOT).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _expand_env_value(value: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        return values.get(name) or os.environ.get(name) or default

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
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _expand_env_value(value.strip().strip('"').strip("'"), values)
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
    forbidden = {Path("/").resolve(), Path.home().resolve(), REPO_ROOT.resolve()}
    if resolved in forbidden or len(resolved.parts) < 3:
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
            tables = sorted(str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            if not tables:
                return {"classification": "empty", "database": db_path.as_posix(), "tables": []}
            versions: list[str] = []
            if "schema_migrations" in tables:
                columns = {str(row[1]) for row in connection.execute('PRAGMA table_info("schema_migrations")')}
                if "version" in columns:
                    versions = [str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")]
    except sqlite3.DatabaseError as exc:
        raise CutoverError("Runtime database 无法只读分类") from exc
    legacy_names = [name for name in tables if "claude" in name.casefold() or re.search(r"(^|_)sdk(_|$)", name.casefold())]
    classification = "agentscope" if SCHEMA_EPOCH in versions and not legacy_names else "legacy-or-unknown"
    return {
        "classification": classification,
        "database": db_path.as_posix(),
        "tables": tables,
        "schema_versions": versions,
        "legacy_tables": legacy_names,
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
    try:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CutoverError("无法确认维护窗口中的 Compose 项目已停止") from exc
    if result.stdout.strip():
        raise CutoverError("维护闸未关闭：目标 Compose project 仍有容器；先显式 down 后再 prepare/execute")


_SUPPORT_MODULE_NAMES: Final = (
    "scripts.agentscope_atomic_cutover_support",
    "agentscope_atomic_cutover_support",
)
_EVIDENCE_MODULE_NAMES: Final = (
    "scripts.agentscope_atomic_cutover_evidence",
    "agentscope_atomic_cutover_evidence",
)
_RECOVERY_MODULE_NAMES: Final = (
    "scripts.agentscope_atomic_cutover_recovery",
    "agentscope_atomic_cutover_recovery",
)
_SUPPORT_EXPORTS: Final[Mapping[str, str]] = {
    "_tree_manifest": "tree_manifest",
    "_tree_content_projection": "tree_content_projection",
    "_safe_extract_regular_archive": "safe_extract_regular_archive",
    "create_snapshot_with_restore_drill": "create_snapshot_with_restore_drill",
    "_write_json": "write_json",
    "_atomic_write_gate_state": "atomic_write_gate_state",
    "_read_gate_state": "read_gate_state",
    "_require_restore_allowed": "require_restore_allowed",
    "_load_manifest": "load_manifest",
    "_verify_token": "verify_token",
    "_verify_manifest_target": "verify_manifest_target",
    "_clear_runtime_root": "clear_runtime_root",
    "_verify_rollback_bundle": "verify_rollback_bundle",
    "_append_env_overrides": "append_env_overrides",
    "_append_acceptance_overrides": "append_acceptance_overrides",
    "_write_production_drain_env": "write_production_drain_env",
    "_bootstrap_and_force_recreate": "bootstrap_and_force_recreate",
    "_stop_compose_project": "stop_compose_project",
    "_restore_rollback_images_and_start": "restore_rollback_images_and_start",
    "_wait_ready": "wait_ready",
    "_openapi_sha": "openapi_sha",
    "_agent_scope_version": "agent_scope_version",
    "_record_ledger": "record_ledger",
}
_EVIDENCE_EXPORTS: Final[Mapping[str, str]] = {
    "_evidence_binding_sha256": "evidence_binding_sha256",
    "_validate_final_evidence": "validate_final_evidence",
}
_SUPPORT: CutoverSupport | None = None
_EVIDENCE_SUPPORT: CutoverEvidenceSupport | None = None
_RECOVERY_SUPPORT: CutoverRecoverySupport | None = None


def _import_first(module_names: tuple[str, ...]) -> ModuleType | None:
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            top_level = module_name.partition(".")[0]
            if exc.name not in {module_name, top_level}:
                raise
    return None


def _import_support_module() -> ModuleType | None:
    return _import_first(_SUPPORT_MODULE_NAMES)


def _destructive_support() -> CutoverSupport:
    global _SUPPORT
    if _SUPPORT is not None:
        return _SUPPORT
    try:
        module = _import_support_module()
        if module is None:
            raise CutoverError("destructive/restore cutover helper 缺失；单文件 preflight 只允许 inspect")
        support = module.CutoverSupport(
            error_type=CutoverError,
            repo_root=REPO_ROOT,
            compose_file=COMPOSE_FILE,
            schema_epoch=SCHEMA_EPOCH,
            utc_now=_utc_now,
            sha256_file=_sha256_file,
            source_artifact_sha256=_source_artifact_sha256,
            load_env_file=load_env_file,
            resolve_runtime_root=resolve_runtime_root,
            compose_base=_compose_base,
            classify_runtime_epoch=classify_runtime_epoch,
            database_path=_database_path,
        )
    except CutoverError:
        raise
    except Exception as exc:  # noqa: BLE001 - import/constructor failure must remain a fail-closed CLI error.
        raise CutoverError("destructive/restore cutover helper 无法完整加载；未执行任何切换动作") from exc
    _SUPPORT = cast("CutoverSupport", support)
    return _SUPPORT


def _evidence_support() -> CutoverEvidenceSupport:
    global _EVIDENCE_SUPPORT
    if _EVIDENCE_SUPPORT is not None:
        return _EVIDENCE_SUPPORT
    module = _import_first(_EVIDENCE_MODULE_NAMES)
    if module is None:
        raise CutoverError("machine receipt evidence helper 缺失；拒绝 finalize")
    try:
        support = module.CutoverEvidenceSupport(
            error_type=CutoverError,
            final_evidence_keys=FINAL_EVIDENCE_KEYS,
            sha256_file=_sha256_file,
            write_json=_destructive_support().write_json,
        )
    except CutoverError:
        raise
    except Exception as exc:  # noqa: BLE001 - evidence validator construction must fail closed.
        raise CutoverError("machine receipt evidence helper 无法加载；拒绝 finalize") from exc
    _EVIDENCE_SUPPORT = cast("CutoverEvidenceSupport", support)
    return _EVIDENCE_SUPPORT


def _recovery_support() -> CutoverRecoverySupport:
    global _RECOVERY_SUPPORT
    if _RECOVERY_SUPPORT is not None:
        return _RECOVERY_SUPPORT
    module = _import_first(_RECOVERY_MODULE_NAMES)
    if module is None:
        raise CutoverError("irreversible recovery helper 缺失；拒绝 open gate")
    operations = _destructive_support()
    try:
        support = module.CutoverRecoverySupport(
            error_type=CutoverError,
            utc_now=_utc_now,
            sha256_file=_sha256_file,
            write_json=operations.write_json,
            read_gate_state=operations.read_gate_state,
            atomic_write_gate_state=operations.atomic_write_gate_state,
            record_ledger=operations.record_ledger,
            database_path=_database_path,
        )
    except CutoverError:
        raise
    except Exception as exc:  # noqa: BLE001 - recovery construction must fail closed.
        raise CutoverError("irreversible recovery helper 无法加载；拒绝 open gate") from exc
    _RECOVERY_SUPPORT = cast("CutoverRecoverySupport", support)
    return _RECOVERY_SUPPORT


def __getattr__(name: str) -> object:
    """Keep tested private helper names available after the internal split."""

    support_name = _SUPPORT_EXPORTS.get(name)
    if support_name is not None:
        return getattr(_destructive_support(), support_name)
    evidence_name = _EVIDENCE_EXPORTS.get(name)
    if evidence_name is not None:
        return getattr(_evidence_support(), evidence_name)
    raise AttributeError(name)


def _run(command: list[str], label: str, *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=True,
            capture_output=capture,
            text=capture,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CutoverError(f"{label}失败") from exc
    return result.stdout if capture else ""


def _capture_rollback_bundle(
    env_file: Path,
    backup_root: Path,
    rollback_compose_file: Path,
) -> RollbackBundle:
    return _destructive_support().capture_rollback_bundle(
        env_file,
        backup_root,
        rollback_compose_file,
        run_command=_run,
    )


def command_inspect(args: argparse.Namespace) -> int:
    env_file = args.env_file.resolve()
    runtime_root = resolve_runtime_root(env_file, args.runtime_root, require_exists=False)
    result = classify_runtime_epoch(_database_path(runtime_root, env_file))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.require_current_or_empty and result["classification"] not in {"empty", "agentscope"}:
        raise CutoverError("检测到旧或未知 Runtime schema；普通 deploy 禁止自动清空，请走显式 atomic cutover")
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    support = _destructive_support()
    if args.confirmation_token != PREPARE_CONFIRMATION:
        raise CutoverError(f"prepare 必须显式提供 --confirmation-token {PREPARE_CONFIRMATION}")
    env_file = args.env_file.resolve()
    runtime_root = resolve_runtime_root(env_file, args.runtime_root, require_exists=True)
    backup_parent = args.backup_dir.expanduser().resolve()
    rollback_compose_file = args.rollback_compose_file.expanduser()
    if backup_parent == runtime_root or backup_parent.is_relative_to(runtime_root):
        raise CutoverError("--backup-dir 必须位于 Runtime root 外")
    require_compose_project_stopped(env_file)
    db_path = _database_path(runtime_root, env_file)
    counters = active_work_counts(db_path)
    if any(counters.values()):
        raise CutoverError(f"active run/HITL/test/publish 未清零: {counters}")

    cutover_id = f"agentscope-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    backup_root = backup_parent / cutover_id
    archive, archive_sha, tree = support.create_snapshot_with_restore_drill(runtime_root, env_file, backup_root)
    rollback_bundle = _capture_rollback_bundle(env_file, backup_root, rollback_compose_file)
    env_snapshot = backup_root / "compose.env.snapshot"
    execute_token = f"EXECUTE-{cutover_id}-{secrets.token_urlsafe(18)}"
    finalize_token = f"FINALIZE-{cutover_id}-{secrets.token_urlsafe(18)}"
    root_stat = runtime_root.stat()
    manifest = {
        "schema_version": 1,
        "cutover_id": cutover_id,
        "state": "prepared",
        "prepared_at": _utc_now(),
        "runtime_root": runtime_root.as_posix(),
        "runtime_root_device": root_stat.st_dev,
        "runtime_root_inode": root_stat.st_ino,
        "source_env": env_file.as_posix(),
        "source_env_sha256": _sha256_file(env_file),
        "source_artifact_sha256": _source_artifact_sha256(),
        "env_snapshot": env_snapshot.as_posix(),
        "env_snapshot_sha256": _sha256_file(env_snapshot),
        "snapshot_archive": archive.as_posix(),
        "snapshot_sha256": archive_sha,
        "snapshot_entries": tree,
        "restore_drill": "passed",
        "active_counts": counters,
        **rollback_bundle,
        "execute_token_sha256": hashlib.sha256(execute_token.encode()).hexdigest(),
        "finalize_token_sha256": hashlib.sha256(finalize_token.encode()).hexdigest(),
        "irreversible": False,
    }
    manifest_path = backup_root / "cutover-manifest.json"
    support.write_json(manifest_path, manifest)
    _evidence_support().write_pending_template(
        backup_root / "final-evidence.json",
        cutover_id=cutover_id,
        source_artifact_sha256=str(manifest["source_artifact_sha256"]),
    )
    print(
        json.dumps(
            {
                "manifest": manifest_path.as_posix(),
                "execute_token": execute_token,
                "finalize_token": finalize_token,
                "restore_drill": "passed",
                "next": "execute",
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_execute(args: argparse.Namespace) -> int:
    support = _destructive_support()
    manifest_path = args.manifest.resolve()
    manifest = support.load_manifest(manifest_path)
    if manifest.get("state") != "prepared" or manifest.get("irreversible") is not False:
        raise CutoverError("只有 prepared 且可恢复的 manifest 可以 execute")
    support.verify_token(manifest, "execute_token_sha256", args.confirmation_token)
    env_file = Path(str(manifest["source_env"])).resolve()
    runtime_root = resolve_runtime_root(env_file, args.runtime_root, require_exists=True)
    require_compose_project_stopped(env_file)
    support.verify_manifest_target(manifest, runtime_root, env_file)
    counters = active_work_counts(_database_path(runtime_root, env_file))
    if any(counters.values()):
        raise CutoverError(f"execute 前 active run/HITL/test/publish 不再为零: {counters}")
    if support.tree_manifest(runtime_root) != manifest.get("snapshot_entries"):
        raise CutoverError("Runtime root 在 prepare 后发生变化，必须重新 prepare")

    acceptance_env = manifest_path.parent / "acceptance-only.env"
    gate_state_file = manifest_path.parent / "api-gate/api-gate-state.json"
    support.atomic_write_gate_state(
        gate_state_file,
        state="acceptance",
        cutover_id=str(manifest["cutover_id"]),
    )
    support.append_acceptance_overrides(
        env_file,
        acceptance_env,
        identity=str(manifest["cutover_id"]),
        gate_state_file=gate_state_file,
    )
    manifest.update(
        {
            "state": "acceptance_starting",
            "api_gate_state_file": gate_state_file.as_posix(),
            "acceptance_env": acceptance_env.as_posix(),
            "irreversible": False,
        }
    )
    support.write_json(manifest_path, manifest)
    print("CUTOVER_REVERSIBLE_RESET_BEGIN: 从此处清空活动 root，但仍可从外置快照恢复。")
    support.clear_runtime_root(runtime_root)
    try:
        support.bootstrap_and_force_recreate(runtime_root, acceptance_env)
        base_url, api_key = support.wait_ready(acceptance_env)
    except CutoverError as exc:
        manifest.update({"state": "acceptance_failed", "acceptance_failed_at": _utc_now()})
        support.write_json(manifest_path, manifest)
        raise CutoverError("验收栈启动失败；API gate 尚未 open，可使用 restore token 自动恢复旧栈") from exc
    db_path = _database_path(runtime_root, env_file)
    artifacts = support.acceptance_artifacts(runtime_root, base_url, api_key, acceptance_env, manifest)
    support.record_ledger(
        db_path,
        cutover_id=f"{manifest['cutover_id']}:acceptance-only",
        phase="acceptance_only",
        status="ready",
        detail="Fresh AgentScope epoch is loopback-bound with a one-time acceptance API key; rollback remains allowed.",
        artifacts=artifacts,
    )
    manifest.update(
        {
            "state": "acceptance_only",
            "executed_at": _utc_now(),
            "acceptance_artifacts": artifacts,
        }
    )
    support.write_json(manifest_path, manifest)
    _evidence_support().bind_final_evidence_template(
        manifest_path.parent / "final-evidence.json",
        manifest,
        artifacts,
    )
    print(json.dumps({"manifest": manifest_path.as_posix(), "state": "acceptance_only", "irreversible": False}, ensure_ascii=False))
    return 0


def command_finalize(args: argparse.Namespace) -> int:
    support = _destructive_support()
    manifest_path = args.manifest.resolve()
    manifest = support.load_manifest(manifest_path)
    support.verify_token(manifest, "finalize_token_sha256", args.confirmation_token)
    env_file = Path(str(manifest["source_env"])).resolve()
    runtime_root = resolve_runtime_root(env_file, args.runtime_root, require_exists=True)
    gate_state = support.read_gate_state(manifest)
    if gate_state is not None and (gate_state.get("state") == "open" or gate_state.get("irreversible_at")):
        _recovery_support().resume_irreversible_transition(
            manifest_path=manifest_path,
            manifest=manifest,
            runtime_root=runtime_root,
            env_file=env_file,
        )
        print(json.dumps({"manifest": manifest_path.as_posix(), "state": "irreversible", "recovered": True}))
        return 0
    resumable_states = {
        "acceptance_only",
        "production_drain_starting",
        "production_drain_failed",
        "production_drain_ready",
        "deletion_intent_ready",
    }
    if manifest.get("state") not in resumable_states or manifest.get("irreversible") is not False:
        raise CutoverError("只有可恢复的 acceptance/drain manifest 可以 finalize")
    evidence_file = getattr(args, "evidence_file", None)
    if not isinstance(evidence_file, Path):
        raise CutoverError("gate 尚未 open 时 recover-finalize 必须提供 --evidence-file")
    evidence, evidence_sha = _evidence_support().validate_final_evidence(evidence_file.resolve(), manifest)
    support.verify_manifest_target(manifest, runtime_root, env_file)
    db_path = _database_path(runtime_root, env_file)
    counters = active_work_counts(db_path)
    if any(counters.values()):
        raise CutoverError(f"finalize 前 active run/HITL/test/publish 不再为零: {counters}")

    if manifest.get("state") in {"production_drain_ready", "deletion_intent_ready"}:
        _recovery_support().resume_irreversible_transition(
            manifest_path=manifest_path,
            manifest=manifest,
            runtime_root=runtime_root,
            env_file=env_file,
            expected_evidence_sha256=evidence_sha,
        )
        print(json.dumps({"manifest": manifest_path.as_posix(), "state": "irreversible", "recovered": True}))
        return 0
    drain = support.start_production_drain(
        manifest_path=manifest_path,
        manifest=manifest,
        env_file=env_file,
        runtime_root=runtime_root,
        evidence=evidence,
        evidence_sha=evidence_sha,
    )
    counters = active_work_counts(db_path)
    if any(counters.values()):
        raise CutoverError(f"open gate 前 active run/HITL/test/publish 不再为零: {counters}")
    # open_production_gate performs the single fsync+replace mutation latch and
    # only then deletes the legacy rollback bundle.
    _recovery_support().open_production_gate(manifest_path=manifest_path, manifest=manifest, drain=drain)
    print(json.dumps({"manifest": manifest_path.as_posix(), "state": "irreversible", "legacy_restore": "forbidden"}))
    return 0


def command_restore(args: argparse.Namespace) -> int:
    support = _destructive_support()
    manifest_path = args.manifest.resolve()
    manifest = support.load_manifest(manifest_path)
    support.require_restore_allowed(manifest)
    expected = f"RESTORE-{manifest['cutover_id']}"
    if args.confirmation_token != expected:
        raise CutoverError(f"restore 必须显式提供 --confirmation-token {expected}")
    env_snapshot = Path(str(manifest["env_snapshot"])).resolve()
    runtime_root = resolve_runtime_root(env_snapshot, args.runtime_root, require_exists=True)
    support.verify_manifest_target(manifest, runtime_root, env_snapshot, verify_current_source=False)
    support.stop_compose_project(env_snapshot)
    support.clear_runtime_root(runtime_root)
    support.safe_extract_regular_archive(Path(str(manifest["snapshot_archive"])), runtime_root)
    if support.tree_content_projection(support.tree_manifest(runtime_root)) != support.tree_content_projection(manifest["snapshot_entries"]):
        raise CutoverError("恢复后的 Runtime root 与快照 manifest 不一致")
    manifest.update({"state": "restore_starting", "restore_started_at": _utc_now(), "irreversible": False})
    support.write_json(manifest_path, manifest)
    try:
        support.restore_rollback_images_and_start(manifest)
    except CutoverError as exc:
        manifest.update({"state": "restore_start_failed", "restore_start_failed_at": _utc_now()})
        support.write_json(manifest_path, manifest)
        raise CutoverError("旧数据已恢复，但精确旧 image 栈启动失败；保留 rollback bundle 供重试") from exc
    manifest.update({"state": "restored", "restored_at": _utc_now(), "rollback_bundle": "verified-and-started"})
    support.write_json(manifest_path, manifest)
    print(json.dumps({"manifest": manifest_path.as_posix(), "state": "restored"}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentScope fresh schema 的显式 atomic cutover 工具。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="只读判断 Runtime DB epoch；普通 deploy 使用。")
    inspect_parser.add_argument("--env-file", type=Path, required=True)
    inspect_parser.add_argument("--runtime-root", type=Path)
    inspect_parser.add_argument("--require-current-or-empty", action="store_true")
    inspect_parser.set_defaults(handler=command_inspect)

    prepare_parser = subparsers.add_parser("prepare", help="停机后做外置快照、SHA 和 restore drill；不清空。")
    prepare_parser.add_argument("--env-file", type=Path, required=True)
    prepare_parser.add_argument("--runtime-root", type=Path)
    prepare_parser.add_argument("--backup-dir", type=Path, required=True)
    prepare_parser.add_argument(
        "--rollback-compose-file",
        type=Path,
        required=True,
        help="切换前旧栈的 Compose 文件；必须显式指定，不得以新栈 Compose 推断。",
    )
    prepare_parser.add_argument("--confirmation-token", required=True)
    prepare_parser.set_defaults(handler=command_prepare)

    for name, handler in (
        ("execute", command_execute),
        ("finalize", command_finalize),
        ("recover-finalize", command_finalize),
        ("restore", command_restore),
    ):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--manifest", type=Path, required=True)
        subparser.add_argument("--runtime-root", type=Path)
        subparser.add_argument("--confirmation-token", required=True)
        if name == "finalize":
            subparser.add_argument("--evidence-file", type=Path, required=True)
        elif name == "recover-finalize":
            subparser.add_argument("--evidence-file", type=Path)
        subparser.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except CutoverError as exc:
        print(f"AGENTSCOPE_CUTOVER_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
