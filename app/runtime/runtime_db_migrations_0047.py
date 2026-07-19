from __future__ import annotations

import json
from typing import cast

from sqlalchemy.engine import Connection

from .json_types import JsonObject
from .runtime_db_base import begin_sqlite_write_transaction

_OLD_VERSION_KEY = "main_agent_version_id"
_VERSION_KEY = "business_agent_version_id"
_OLD_COMPLETENESS_KEY = "has_main_agent_version"
_COMPLETENESS_KEY = "has_business_agent_version"
_OLD_FILE_NAME = "main_agent_version.json"
_FILE_NAME = "business_agent_version.json"


def _table_exists(connection: Connection, table_name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).first()
    return row is not None


def _json_object(value: object) -> JsonObject | None:
    if isinstance(value, dict):
        return cast(JsonObject, dict(value))
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return cast(JsonObject, parsed) if isinstance(parsed, dict) else None


def _rename_manifest_fields(manifest: JsonObject) -> bool:
    changed = False
    if _OLD_VERSION_KEY in manifest:
        manifest.setdefault(_VERSION_KEY, manifest[_OLD_VERSION_KEY])
        manifest.pop(_OLD_VERSION_KEY, None)
        changed = True

    completeness = manifest.get("completeness")
    if isinstance(completeness, dict) and _OLD_COMPLETENESS_KEY in completeness:
        completeness.setdefault(_COMPLETENESS_KEY, completeness[_OLD_COMPLETENESS_KEY])
        completeness.pop(_OLD_COMPLETENESS_KEY, None)
        changed = True

    included_files = manifest.get("included_files")
    if isinstance(included_files, list):
        for item in included_files:
            if isinstance(item, dict) and item.get("path") == _OLD_FILE_NAME:
                item["path"] = _FILE_NAME
                changed = True
    return changed


def migrate_0047_rename_business_agent_evidence_fields(connection: Connection) -> None:
    """一次性消除证据包中把任意业务 Agent 称为 main Agent 的旧字段。"""

    if not _table_exists(connection, "evidence_packages") and not _table_exists(connection, "evidence_files"):
        return
    begin_sqlite_write_transaction(connection)

    if _table_exists(connection, "evidence_packages"):
        rows = connection.exec_driver_sql("SELECT evidence_package_id, manifest_json FROM evidence_packages").fetchall()
        for evidence_package_id, raw_manifest in rows:
            manifest = _json_object(raw_manifest)
            if manifest is None or not _rename_manifest_fields(manifest):
                continue
            connection.exec_driver_sql(
                "UPDATE evidence_packages SET manifest_json = ? WHERE evidence_package_id = ?",
                (json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), evidence_package_id),
            )

    if not _table_exists(connection, "evidence_files"):
        return
    rows = connection.exec_driver_sql(
        "SELECT evidence_package_id, content_json FROM evidence_files WHERE file_name = ?",
        (_OLD_FILE_NAME,),
    ).fetchall()
    for evidence_package_id, raw_content in rows:
        content = _json_object(raw_content)
        if content is not None and _OLD_VERSION_KEY in content:
            content.setdefault(_VERSION_KEY, content[_OLD_VERSION_KEY])
            content.pop(_OLD_VERSION_KEY, None)
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")) if content is not None else raw_content
        connection.exec_driver_sql(
            "UPDATE evidence_files SET file_name = ?, content_json = ? WHERE evidence_package_id = ? AND file_name = ?",
            (_FILE_NAME, encoded, evidence_package_id, _OLD_FILE_NAME),
        )
