"""手工维护的保守 Workspace 清单与可恢复隔离，不回收普通业务 Session。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypeAlias, TypedDict, cast

from agentgov_agentscope_contract import version_workspace_id
from app.runtime.sqlite_schema_contract import SqliteSchemaEpochClassification

from scripts import agentscope_atomic_cutover as cutover
from scripts.selected_env_operation_contract import SelectedEnvError

if TYPE_CHECKING:
    from scripts.runtime_workspace_gc_inventory import NativeAgentReference

    NativeAgentIndex: TypeAlias = dict[str, NativeAgentReference]

_MARKER = ".agentgov-runtime-workspace.json"
_WORKSPACE = re.compile(r"(?:candidate|published)-[A-Za-z0-9._-]+--v-[0-9a-f]{64}")
_SESSION_TERMINALS = frozenset({"bound", "failed", "failed_cleaned"})


@dataclass(frozen=True)
class GovernanceReferences:
    agent_ids: frozenset[str]
    protected_agents: frozenset[str]
    protected_workspaces: frozenset[str]
    eligible: tuple[tuple[str, str], ...]


class WorkspaceItem(TypedDict):
    workspace_id: str
    action: Literal["quarantine", "retain"]
    reason: str


class QuarantineResult(TypedDict):
    quarantine_id: str | None
    moved: list[str]


class WorkspacePlan(TypedDict):
    scope: Literal["cleanup_complete_ephemeral_only"]
    native_global_complete: Literal[False]
    items: list[WorkspaceItem]
    result: NotRequired[QuarantineResult]


def read_governance_references(database: Path) -> GovernanceReferences:
    if database.is_symlink() or not database.is_file():
        raise SelectedEnvError("维护要求真实 AgentGov 数据库")
    if cutover.classify_runtime_epoch(database)["classification"] != SqliteSchemaEpochClassification.CURRENT.value:
        raise SelectedEnvError("维护要求当前且完整的 AgentGov schema")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        bindings = connection.execute("SELECT runtime_agent_id FROM runtime_session_bindings").fetchall()
        versions = connection.execute("SELECT runtime_agent_id FROM runtime_agent_versions").fetchall()
        intents = connection.execute("SELECT runtime_agent_id, workspace_id, status FROM runtime_session_creation_intents").fetchall()
        resources = connection.execute("SELECT runtime_agent_id, workspace_id, status, source_kind FROM runtime_ephemeral_resources").fetchall()
    protected_agents = {row["runtime_agent_id"] for row in [*bindings, *versions]}
    protected_workspaces = {row["workspace_id"] for row in intents if row["status"] not in _SESSION_TERMINALS}
    protected_workspaces.update(row["workspace_id"] for row in resources if row["status"] != "cleanup_complete")
    protected_agents.update(row["runtime_agent_id"] for row in intents if row["status"] not in _SESSION_TERMINALS)
    agent_ids = protected_agents | {row["runtime_agent_id"] for row in [*intents, *resources] if row["runtime_agent_id"]}
    eligible = tuple(
        (row["workspace_id"], row["runtime_agent_id"])
        for row in resources
        if row["status"] == "cleanup_complete" and row["source_kind"] in {"candidate_snapshot", "staged"} and row["runtime_agent_id"]
    )
    return GovernanceReferences(frozenset(agent_ids), frozenset(protected_agents), frozenset(protected_workspaces), eligible)


def _native_references(payload: object) -> tuple[NativeAgentIndex, set[str]]:
    if not isinstance(payload, dict) or payload.get("scope") != "reachable_and_explicit_agents" or payload.get("global_complete") is not False:
        raise SelectedEnvError("Runtime 引用清单范围不明，未批准回收")
    agents = payload.get("agents")
    if not isinstance(agents, list):
        raise SelectedEnvError("Runtime 引用清单不完整")
    by_id: NativeAgentIndex = {}
    workspaces: set[str] = set()
    for row in agents:
        if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str) or not isinstance(row.get("present"), bool):
            raise SelectedEnvError("Runtime 引用行无效")
        agent_id = row["agent_id"]
        if agent_id in by_id or not isinstance(row.get("sessions"), list):
            raise SelectedEnvError("Runtime 引用清单含重复或缺失条目")
        by_id[agent_id] = cast("NativeAgentReference", row)
        for session in row["sessions"]:
            if not isinstance(session, dict) or not isinstance(session.get("session_id"), str) or not isinstance(session.get("workspace_id"), str):
                raise SelectedEnvError("Runtime Session workspace 引用不明")
            workspaces.add(session["workspace_id"])
    return by_id, workspaces


def known_workspace(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        version_id = version_workspace_id(path.name)
        if _WORKSPACE.fullmatch(version_id) is None:
            return False
        marker = path / _MARKER
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            return False
        payload = json.loads(marker.read_bytes())
        return payload == {"workspace_id": path.name, "harness_digest": version_id.rsplit("--v-", 1)[1]}
    except (OSError, ValueError):
        return False


def plan_workspaces(root: Path, references: GovernanceReferences, native: object) -> WorkspacePlan:
    if not root.is_absolute() or root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise SelectedEnvError("Runtime Workspace 根必须是精确的真实目录")
    by_agent, native_workspaces = _native_references(native)
    if not references.agent_ids <= by_agent.keys():
        raise SelectedEnvError("Runtime 引用清单未覆盖所有 AgentGov 定位符")
    eligible: dict[str, list[str]] = {}
    for workspace_id, agent_id in references.eligible:
        eligible.setdefault(workspace_id, []).append(agent_id)
    items: list[WorkspaceItem] = []
    for path in sorted(root.iterdir()):
        if path.name == ".quarantine":
            continue
        owners = eligible.get(path.name, [])
        if not known_workspace(path):
            reason = "unknown_workspace"
        elif path.name in references.protected_workspaces or path.name in native_workspaces:
            reason = "referenced_workspace"
        elif not owners:
            reason = "no_completed_ephemeral_deletion"
        elif any(owner in references.protected_agents or by_agent[owner]["present"] or by_agent[owner]["sessions"] for owner in owners):
            reason = "referenced_agent"
        else:
            reason = "completed_ephemeral_unreferenced"
        items.append({"workspace_id": path.name, "action": "quarantine" if reason == "completed_ephemeral_unreferenced" else "retain", "reason": reason})
    return {"scope": "cleanup_complete_ephemeral_only", "native_global_complete": False, "items": items}


def quarantine_workspaces(root: Path, plan: WorkspacePlan) -> QuarantineResult:
    """仅调用方已停服且重建引用清单后使用；整体 rename 保留 Workspace 内 venv 与状态。"""
    selected = [item["workspace_id"] for item in plan["items"] if item["action"] == "quarantine"]
    if root.is_symlink() or root.resolve() != root or not root.is_dir() or len(selected) != len(set(selected)):
        raise SelectedEnvError("Workspace 根或隔离清单无效")
    if not selected:
        return {"quarantine_id": None, "moved": []}
    for name in selected:
        if Path(name).name != name or not known_workspace(root / name):
            raise SelectedEnvError("Workspace 在隔离前改变，未继续回收")
    quarantine_root = root / ".quarantine"
    quarantine_root.mkdir(mode=0o700, exist_ok=True)
    metadata = quarantine_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SelectedEnvError("Workspace 隔离目录 owner/type/mode 无效")
    quarantine_id = uuid.uuid4().hex
    destination = quarantine_root / quarantine_id
    destination.mkdir(mode=0o700)
    # 先写完整移动意图；进程崩溃时可用原/目标存在性核对，不丢失恢复定位符。
    manifest = destination / "manifest.json"
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"workspace_ids": selected}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(destination)
    moved = []
    try:
        for name in selected:
            os.rename(root / name, destination / name)
            moved.append(name)
        _sync_directory(root)
        _sync_directory(destination)
    except BaseException:
        # 不覆盖运行态新内容；即使无法回滚，已写 manifest 与原字节仍留在隔离区。
        for name in reversed(moved):
            if not (root / name).exists() and not (root / name).is_symlink():
                os.rename(destination / name, root / name)
        raise
    return {"quarantine_id": quarantine_id, "moved": moved}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
