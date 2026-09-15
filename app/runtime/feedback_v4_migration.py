"""一次性迁移旧 SOC 关联字段；不改历史证据、发布和模型输出正文。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TypeAlias

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from .feedback_entities import FeedbackEntities, merge_entities, parse_entities
from .runtime_db_base import Base

MigrationRow: TypeAlias = dict[str, object]
MigrationProjection: TypeAlias = Callable[[MigrationRow], MigrationRow]


def _object(value: object) -> MigrationRow:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise RuntimeError("Historical feedback JSON must be an object")
    return dict(parsed)


def _entities(payload: Mapping[str, object], *, lists: bool = False) -> FeedbackEntities:
    references: FeedbackEntities = {}
    for kind in ("alert", "case"):
        value = payload.get(f"{kind}_ids_json" if lists else f"{kind}_id")
        if lists and isinstance(value, str):
            value = json.loads(value)
        if value:
            references[kind] = list(value) if isinstance(value, list) else [str(value)]
    return merge_entities([parse_entities(payload.get("entities")), references])


def _source_row(row: MigrationRow) -> MigrationRow:
    payload = _object(row["payload_json"])
    payload["entities"] = merge_entities([_entities(payload), _entities(row)])
    payload.pop("alert_id", None)
    payload.pop("case_id", None)
    return {**row, "payload_json": json.dumps(payload, ensure_ascii=False)}


def _run_row(row: MigrationRow) -> MigrationRow:
    return {**row, "entities_json": json.dumps(_entities(row), ensure_ascii=False)}


def _case_row(row: MigrationRow) -> MigrationRow:
    return {**row, "entities_json": json.dumps(_entities(row, lists=True), ensure_ascii=False)}


def rebuild_v4_table(connection: Connection, source_name: str, target_name: str, project: MigrationProjection) -> None:
    """原样复制未变列（包括 BLOB/JSON 字节），只转换显式列，核对主键和行数。"""
    quote = connection.dialect.identifier_preparer.quote
    table = Base.metadata.tables[target_name]
    backup_name = f"{source_name}__migration_source"
    rows = [project(dict(row)) for row in connection.exec_driver_sql(f"SELECT * FROM {quote(source_name)}").mappings()]
    connection.exec_driver_sql(f"ALTER TABLE {quote(source_name)} RENAME TO {quote(backup_name)}")
    for index in inspect(connection).get_indexes(backup_name):
        connection.exec_driver_sql(f"DROP INDEX {quote(index['name'])}")
    table.create(connection)
    names = [column.name for column in table.columns]
    columns = ", ".join(quote(name) for name in names)
    placeholders = ", ".join("?" for _ in names)
    if rows:
        connection.exec_driver_sql(
            f"INSERT INTO {quote(target_name)} ({columns}) VALUES ({placeholders})", [tuple(row[name] for name in names) for row in rows]
        )
    keys = [column.name for column in table.primary_key.columns]
    expected = {tuple(row[key] for key in keys) for row in rows}
    actual = {tuple(row) for row in connection.exec_driver_sql(f"SELECT {', '.join(quote(key) for key in keys)} FROM {quote(target_name)}")}
    if actual != expected or len(actual) != len(rows):
        raise RuntimeError("Feedback migration did not preserve every source identity")
    connection.exec_driver_sql(f"DROP TABLE {quote(backup_name)}")


def migrate_feedback_schema(connection: Connection) -> None:
    rebuild_v4_table(connection, "agent_runs", "agent_runs", _run_row)
    rebuild_v4_table(connection, "feedback_signals", "feedback_signals", _source_row)
    rebuild_v4_table(connection, "soc_events", "feedback_events", _source_row)
    rebuild_v4_table(connection, "feedback_cases", "feedback_cases", _case_row)
    _migrate_improvement_entities(connection)
    for pending_id, encoded in connection.exec_driver_sql("SELECT pending_id, payload_json FROM pending_correlations").all():
        payload = _source_row({"payload_json": encoded})["payload_json"]
        connection.exec_driver_sql("UPDATE pending_correlations SET payload_json = ? WHERE pending_id = ?", (payload, pending_id))
    _migrate_source_kinds(connection)


def _migrate_improvement_entities(connection: Connection) -> None:
    assignments = dict(connection.exec_driver_sql("SELECT feedback_id, feedback_case_id FROM improvement_feedback_case_assignments").all())
    cases = dict(connection.exec_driver_sql("SELECT feedback_case_id, entities_json FROM feedback_cases").all())

    def project(row: MigrationRow) -> MigrationRow:
        references = dict(row)
        assigned = assignments.get(row["feedback_id"])
        if assigned is not None:
            # 旧 case_id 混装了治理 ID；只通过真实 assignment 识别，不按 fbc- 前缀猜测。
            if references.get("case_id") == assigned:
                references.pop("case_id")
            if assigned not in cases:
                raise RuntimeError("Historical feedback assignment references a missing Case")
        elif row.get("source") == "feedback_inbox" and row.get("case_id"):
            raise RuntimeError("Historical inbox feedback lacks its Case assignment; restore the relation before migrating")
        inherited = _object(cases[assigned]) if assigned is not None else {}
        return {**row, "entities_json": json.dumps(merge_entities([_entities(references), parse_entities(inherited)]), ensure_ascii=False)}

    rebuild_v4_table(connection, "improvement_feedbacks", "improvement_feedbacks", project)


def _migrate_source_kinds(connection: Connection) -> None:
    annotations = connection.exec_driver_sql(
        "SELECT annotation_id, source_id, payload_json FROM feedback_source_annotations WHERE source_kind = 'soc_event'"
    ).all()
    for old_id, source_id, encoded in annotations:
        new_id = f"event:{source_id}"
        payload = _object(encoded)
        payload.update(source_kind="event", annotation_id=new_id)
        connection.exec_driver_sql(
            "UPDATE feedback_source_annotations SET annotation_id = ?, source_kind = 'event', payload_json = ? WHERE annotation_id = ?",
            (new_id, json.dumps(payload, ensure_ascii=False), old_id),
        )
    connection.exec_driver_sql("UPDATE feedback_case_sources SET source_kind = 'event' WHERE source_kind = 'soc_event'")
    # source_ids_json 是裸 ID，不带 source_kind 前缀；它以及 assignment/release/evidence 均不重写。
