from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, TypeAlias


class SqliteReader(Protocol):
    def execute(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> Iterable[Sequence[object]]: ...


ColumnContract: TypeAlias = tuple[int, str, str, bool, str | None, int, int]
ForeignKeyContract: TypeAlias = tuple[
    tuple[tuple[int, str, str], ...],
    str,
    str,
    str,
    str,
]
IndexColumnContract: TypeAlias = tuple[int, int, str | None, bool, str, bool]
IndexContract: TypeAlias = tuple[
    str,
    bool,
    str,
    bool,
    tuple[IndexColumnContract, ...],
    str | None,
]
TableContract: TypeAlias = tuple[
    str,
    tuple[ColumnContract, ...],
    tuple[ForeignKeyContract, ...],
    tuple[IndexContract, ...],
    tuple[str, ...],
    tuple[str, ...],
    bool,
    bool,
]
SchemaContract: TypeAlias = tuple[TableContract, ...]


_DDL_FEATURE_PATTERN: Final = re.compile(
    r"(?i)\b(AUTOINCREMENT|COLLATE|DEFERRABLE|GENERATED|INITIALLY|STORED|VIRTUAL)\b",
)

# 这些摘要是 epoch 的物理格式标识。只有显式 schema 迁移并同步负向测试时才能更新。
CURRENT_SCHEMA_CONTRACT_SHA256: Final = "6839624b48d7fccedfd8551295b3857df88e23bebc66aa4f72a1eaec3cf969ff"
PREVIOUS_SCHEMA_CONTRACT_SHA256: Final = frozenset(
    {"bca5c87f7ec8f6e77d0c3182e47540e53ec2e07556ff39a55fc86c69ea81eaaf"},
)
LEGACY_V1_SCHEMA_CONTRACT_SHA256: Final = frozenset(
    {
        "83a72b534a3044f6b8b1913b48d8c3607dffb3202545e47e79340a38e6ca34a3",
        "61d2f65e40f4d4945d1816aa438bc8605fb84b4a9220b24d8cb3b5877fdc7ca1",
    },
)

CURRENT_SCHEMA_EPOCH: Final = "agentscope-runtime-v3"
PREVIOUS_SCHEMA_EPOCH: Final = "agentscope-runtime-v2"
LEGACY_SCHEMA_EPOCH: Final = "agentscope-runtime-v1"
REMOVED_RELEASE_OPERATION_TABLE: Final = "agent_release_operations"


class SqliteSchemaEpochClassification(StrEnum):
    EMPTY = "empty"
    CURRENT = "agentscope"
    PREVIOUS_MIGRATABLE = "agentscope-v2-migratable"
    LEGACY_MIGRATABLE = "agentscope-v1-migratable"
    LEGACY_UNSAFE_HISTORY = "agentscope-v1-unsafe-history"
    UNKNOWN = "legacy-or-unknown"


@dataclass(frozen=True)
class SqliteSchemaEpochInspection:
    classification: SqliteSchemaEpochClassification
    tables: tuple[str, ...]
    schema_versions: tuple[str, ...]
    physical_contract_sha256: str | None


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _normalize_sql_fragment(sql: str) -> str:
    """Normalize insignificant SQL whitespace/case without changing literals."""

    normalized: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            normalized.append(char)
            if quote == "]":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    normalized.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            if pending_space and normalized:
                normalized.append(" ")
            pending_space = False
            quote = char
            normalized.append(char)
        elif char == "[":
            if pending_space and normalized:
                normalized.append(" ")
            pending_space = False
            quote = "]"
            normalized.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and normalized:
                normalized.append(" ")
            pending_space = False
            normalized.append(char.upper())
        index += 1
    return "".join(normalized).strip()


def _masked_quoted_content(sql: str) -> str:
    masked = list(sql)
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            masked[index] = " "
            if quote == "]":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    masked[index + 1] = " "
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            masked[index] = " "
        elif char == "[":
            quote = "]"
            masked[index] = " "
        index += 1
    return "".join(masked)


def _matching_parenthesis(sql: str, opening: int) -> int:
    quote: str | None = None
    depth = 0
    index = opening
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if quote == "]":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("SQLite schema contains an unbalanced CHECK expression")


def _check_contract(table_sql: str) -> tuple[str, ...]:
    masked = _masked_quoted_content(table_sql)
    checks: list[str] = []
    for match in re.finditer(r"(?i)\bCHECK\s*\(", masked):
        opening = masked.find("(", match.start())
        closing = _matching_parenthesis(table_sql, opening)
        checks.append(_normalize_sql_fragment(table_sql[opening + 1 : closing]))
    return tuple(checks)


def _ddl_feature_contract(table_sql: str) -> tuple[str, ...]:
    masked = _masked_quoted_content(table_sql)
    return tuple(match.group(1).upper() for match in _DDL_FEATURE_PATTERN.finditer(masked))


def _table_names(connection: SqliteReader) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
            )
        ),
    )


def _table_options(connection: SqliteReader, table_name: str) -> tuple[bool, bool]:
    rows = tuple(
        connection.execute(
            f"PRAGMA table_list({_quote_identifier(table_name)})",
        ),
    )
    if len(rows) != 1 or str(rows[0][1]) != table_name or str(rows[0][2]) != "table":
        raise ValueError(f"SQLite table_list contract is unavailable for {table_name}")
    return bool(rows[0][4]), bool(rows[0][5])


def _column_contract(connection: SqliteReader, table_name: str) -> tuple[ColumnContract, ...]:
    return tuple(
        (
            int(row[0]),
            str(row[1]),
            _normalize_sql_fragment(str(row[2])),
            bool(row[3]),
            None if row[4] is None else _normalize_sql_fragment(str(row[4])),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute(
            f"PRAGMA table_xinfo({_quote_identifier(table_name)})",
        )
    )


def _foreign_key_contract(
    connection: SqliteReader,
    table_name: str,
) -> tuple[ForeignKeyContract, ...]:
    grouped: dict[int, tuple[str, str, str, str, list[tuple[int, str, str]]]] = {}
    for row in connection.execute(
        f"PRAGMA foreign_key_list({_quote_identifier(table_name)})",
    ):
        identifier = int(row[0])
        entry = grouped.setdefault(
            identifier,
            (str(row[2]), str(row[5]), str(row[6]), str(row[7]), []),
        )
        entry[4].append((int(row[1]), str(row[3]), str(row[4])))
    return tuple(
        sorted(
            (
                tuple(sorted(columns)),
                referred_table,
                on_update.upper(),
                on_delete.upper(),
                match.upper(),
            )
            for referred_table, on_update, on_delete, match, columns in grouped.values()
        ),
    )


def _index_contract(
    connection: SqliteReader,
    table_name: str,
) -> tuple[IndexContract, ...]:
    indexes: list[IndexContract] = []
    for row in connection.execute(
        f"PRAGMA index_list({_quote_identifier(table_name)})",
    ):
        index_name = str(row[1])
        index_columns = tuple(
            (
                int(column[0]),
                int(column[1]),
                None if column[2] is None else str(column[2]),
                bool(column[3]),
                str(column[4]).upper(),
                bool(column[5]),
            )
            for column in connection.execute(
                f"PRAGMA index_xinfo({_quote_identifier(index_name)})",
            )
        )
        sql_rows = tuple(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
                (index_name,),
            ),
        )
        if len(sql_rows) != 1:
            raise ValueError(f"SQLite index metadata is unavailable for {index_name}")
        index_sql = sql_rows[0][0]
        indexes.append(
            (
                index_name,
                bool(row[2]),
                str(row[3]),
                bool(row[4]),
                index_columns,
                None if index_sql is None else _normalize_sql_fragment(str(index_sql)),
            ),
        )
    return tuple(sorted(indexes))


def physical_schema_contract(connection: SqliteReader) -> SchemaContract:
    contract: list[TableContract] = []
    for table_name in _table_names(connection):
        sql_rows = tuple(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table_name,),
            ),
        )
        if len(sql_rows) != 1 or sql_rows[0][0] is None:
            raise ValueError(f"SQLite table metadata is unavailable for {table_name}")
        without_rowid, strict = _table_options(connection, table_name)
        contract.append(
            (
                table_name,
                _column_contract(connection, table_name),
                _foreign_key_contract(connection, table_name),
                _index_contract(connection, table_name),
                _check_contract(str(sql_rows[0][0])),
                _ddl_feature_contract(str(sql_rows[0][0])),
                without_rowid,
                strict,
            ),
        )
    return tuple(contract)


def physical_schema_contract_sha256(connection: SqliteReader) -> str:
    payload = json.dumps(
        physical_schema_contract(connection),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def inspect_sqlite_schema_epoch(
    connection: SqliteReader,
    *,
    known_data_migration_markers: frozenset[str] = frozenset(),
) -> SqliteSchemaEpochInspection:
    """Classify only an exact, frozen Runtime schema and its known markers."""

    tables = _table_names(connection)
    if not tables:
        return SqliteSchemaEpochInspection(
            classification=SqliteSchemaEpochClassification.EMPTY,
            tables=(),
            schema_versions=(),
            physical_contract_sha256=None,
        )
    versions = _schema_versions(connection, tables)
    contract_sha256 = physical_schema_contract_sha256(connection)
    if _markers_match(versions, CURRENT_SCHEMA_EPOCH, known_data_migration_markers) and contract_sha256 == CURRENT_SCHEMA_CONTRACT_SHA256:
        classification = SqliteSchemaEpochClassification.CURRENT
    elif _markers_match(versions, PREVIOUS_SCHEMA_EPOCH, known_data_migration_markers) and contract_sha256 in PREVIOUS_SCHEMA_CONTRACT_SHA256:
        classification = SqliteSchemaEpochClassification.PREVIOUS_MIGRATABLE
    elif _markers_match(versions, LEGACY_SCHEMA_EPOCH, known_data_migration_markers) and contract_sha256 in LEGACY_V1_SCHEMA_CONTRACT_SHA256:
        classification = (
            SqliteSchemaEpochClassification.LEGACY_MIGRATABLE
            if _removed_release_history_is_empty(connection, tables)
            else SqliteSchemaEpochClassification.LEGACY_UNSAFE_HISTORY
        )
    else:
        classification = SqliteSchemaEpochClassification.UNKNOWN
    return SqliteSchemaEpochInspection(
        classification=classification,
        tables=tables,
        schema_versions=versions,
        physical_contract_sha256=contract_sha256,
    )


def _schema_versions(
    connection: SqliteReader,
    tables: tuple[str, ...],
) -> tuple[str, ...]:
    if "schema_migrations" not in tables:
        return ()
    columns = {str(row[1]) for row in connection.execute('PRAGMA table_info("schema_migrations")')}
    if "version" not in columns:
        return ()
    return tuple(
        sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations",
            )
        ),
    )


def _markers_match(
    versions: tuple[str, ...],
    epoch: str,
    known_data_migration_markers: frozenset[str],
) -> bool:
    version_set = frozenset(versions)
    return epoch in version_set and version_set - {epoch} <= known_data_migration_markers


def _removed_release_history_is_empty(
    connection: SqliteReader,
    tables: tuple[str, ...],
) -> bool:
    if REMOVED_RELEASE_OPERATION_TABLE not in tables:
        return True
    rows = tuple(
        connection.execute(
            f'SELECT COUNT(*) FROM "{REMOVED_RELEASE_OPERATION_TABLE}"',
        ),
    )
    return len(rows) == 1 and int(rows[0][0]) == 0
