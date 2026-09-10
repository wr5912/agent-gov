"""在固定版本 Runtime 启动扫描前，准备当前发布 Git 版本的不可变 Harness。"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import discover_business_agents
from app.runtime.settings import AppSettings, get_settings
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import agent_payload_from_workspace, session_settings_from_workspace
from app.runtime_gateway.store import RuntimeStateConflict, harness_digest


def _excluded_registry_agent_ids(db_path: Path) -> set[str]:
    """只读已有 tombstone；空卷不创建数据库，也不恢复未完成的 Agent 创建。"""

    if db_path.is_symlink():
        raise RuntimeStateConflict("Agent registry database must not be a symlink")
    if not db_path.exists():
        return set()
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_registry)")}
        if "agent_id" not in columns:
            return set()
        clauses = []
        if "deleted_at" in columns:
            clauses.append("deleted_at IS NOT NULL")
        if "provision_state" in columns:
            clauses.append("provision_state IS NOT NULL AND provision_state != 'ready'")
        if not clauses:
            return set()
        rows = connection.execute("SELECT agent_id FROM agent_registry WHERE " + " OR ".join(clauses))
        return {str(row[0]) for row in rows}


def _prepare_agent(settings: AppSettings, snapshots: PublishedHarnessSnapshotStore, agent_id: str) -> None:
    layout = business_agent_layout(settings.data_dir, agent_id)
    workspace = layout.workspace
    digest = harness_digest(workspace)
    agent_payload_from_workspace(workspace, display_name=agent_id)
    session_settings_from_workspace(workspace)
    if (workspace / ".git").is_symlink():
        raise RuntimeStateConflict("Business Agent Git metadata must not be a symlink")
    versions = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        repository_name=f"{agent_id}-config",
        git_user_name=settings.agent_git_user_name,
        git_user_email=settings.agent_git_user_email,
        create_directories=False,
    )
    versions.ensure_bootstrap()
    version_id, dirty = versions.inspect_clean_head()
    if dirty:
        raise RuntimeStateConflict("Business Agent Harness must be a clean governed Git version before preparation")
    snapshots.materialize(
        version_store=versions,
        agent_id=agent_id,
        agent_version_id=version_id,
        expected_digest=digest,
    )


def prepare_published_harnesses(settings: AppSettings) -> int:
    """幂等物化当前版本，不改 Workspace 字节、不注册 Runtime Agent 或 Session。"""

    excluded = _excluded_registry_agent_ids(settings.runtime_db_path)
    snapshots = PublishedHarnessSnapshotStore(settings.runtime_candidates_dir)
    prepared = 0
    for profile in discover_business_agents(settings):
        if profile.agent_id in excluded:
            continue
        _prepare_agent(settings, snapshots, profile.agent_id)
        prepared += 1
    return prepared


def main() -> int:
    try:
        prepared = prepare_published_harnesses(get_settings())
    except Exception as exc:  # CLI 只返回错误类型，禁止回显 Git/Harness 中的私有内容和路径。
        print(f"published_harness_preparation_failed={type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"published_harnesses_prepared={prepared}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
