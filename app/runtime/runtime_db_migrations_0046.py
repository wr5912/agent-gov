from __future__ import annotations

import json

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction


def migrate_0046_remove_agent_registry_origin(connection: Connection) -> None:
    """删除已退役的 seed/user 来源投影，并清理 provisioning 回滚快照。"""

    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_registry)")}
    if "origin" not in columns:
        return

    begin_sqlite_write_transaction(connection)
    rows = connection.exec_driver_sql("SELECT agent_id, provision_previous_json FROM agent_registry WHERE provision_previous_json IS NOT NULL").fetchall()
    for agent_id, raw_snapshot in rows:
        try:
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict) or "origin" not in snapshot:
            continue
        snapshot.pop("origin", None)
        connection.exec_driver_sql(
            "UPDATE agent_registry SET provision_previous_json = ? WHERE agent_id = ?",
            (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), agent_id),
        )

    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_agent_registry_origin")
    connection.exec_driver_sql("ALTER TABLE agent_registry DROP COLUMN origin")
