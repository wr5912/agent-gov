"""v3 控制账本到原生输入身份的单次迁移；旧请求仅保留历史。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from app.runtime_gateway.contracts import ACTIVE_RUN_STATUSES
from app.runtime_gateway.models import RuntimeChatOperationModel

from .runtime_db_base import Base, begin_sqlite_write_transaction


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_before_v4_migration(connection: Connection) -> None:
    """在迁移写锁内由独立只读连接生成内容寻址的一致副本。"""

    driver_connection = connection.connection.driver_connection
    if not bool(getattr(driver_connection, "in_transaction", False)):
        raise RuntimeError("Runtime migration backup requires an active SQLite write transaction")
    database = connection.engine.url.database
    if not database or database == ":memory:":
        return
    source = Path(database).resolve()
    descriptor, name = tempfile.mkstemp(prefix=f".{source.name}.pre-v4-", suffix=".pending", dir=source.parent)
    os.close(descriptor)
    pending = Path(name)
    try:
        with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as original, sqlite3.connect(pending) as backup:
            original.backup(backup)
        _fsync_file(pending)
        digest = _file_sha256(pending)
        target = source.with_name(f"{source.name}.pre-v4-{digest}.bak")
        with suppress(FileExistsError):
            os.link(pending, target, follow_symlinks=False)
        mode = target.lstat().st_mode
        if stat.S_IMODE(mode) != 0o600 or not stat.S_ISREG(mode) or _file_sha256(target) != digest:
            raise RuntimeError("Runtime migration backup identity conflicts with an existing file") from None
        # 首次 link 后目录 fsync 失败会留下完整 target；重试复用同一 target
        # 时仍必须再次持久化目录项，不能仅凭摘要相同就继续迁移。
        _fsync_directory(source.parent)
    finally:
        pending.unlink(missing_ok=True)


def require_v3_migration_ready(connection: Connection) -> None:
    """拒绝仍有活动 run 的 v3 库；可在创建整库备份前只读调用。"""

    active_states = tuple(status.value for status in ACTIVE_RUN_STATUSES)
    placeholders = ",".join("?" for _ in active_states)
    active = connection.exec_driver_sql(f"SELECT COUNT(*) FROM agent_runs WHERE status IN ({placeholders})", active_states).scalar_one()
    if active:
        raise RuntimeError("请先收尾旧版本活动 run，再切换原生输入身份。")


@contextmanager
def v4_migration_transaction(engine: Engine):
    """仅迁移连接临时关闭外键，事务内完整重建并验引用，退出总恢复原配置。"""
    with engine.connect() as connection:
        foreign_keys = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        legacy_alter = int(connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one())
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            with connection.begin():
                begin_sqlite_write_transaction(connection)
                yield connection
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("Runtime migration produced an invalid reference")
        finally:
            connection.rollback()
            connection.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")
            connection.exec_driver_sql(f"PRAGMA legacy_alter_table={legacy_alter}")
            connection.commit()


def migrate_v3_control_schema(connection: Connection) -> None:
    """同一事务保留全部旧操作/响应字节，并移除旧全局 client ID 约束。"""
    require_v3_migration_ready(connection)
    table = Base.metadata.tables[RuntimeChatOperationModel.__tablename__]
    backup_table = f"{table.name}__migration_source"
    old_count = connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{table.name}"').scalar_one()
    connection.exec_driver_sql(f'ALTER TABLE "{table.name}" RENAME TO "{backup_table}"')
    quote = connection.dialect.identifier_preparer.quote
    for index in inspect(connection).get_indexes(backup_table):
        connection.exec_driver_sql(f"DROP INDEX {quote(index['name'])}")
    table.create(connection)
    columns = ", ".join(quote(column.name) for column in table.columns)
    connection.exec_driver_sql(f'INSERT INTO "{table.name}" ({columns}) SELECT {columns} FROM "{backup_table}"')
    new_count = connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{table.name}"').scalar_one()
    if old_count != new_count:
        raise RuntimeError("Runtime operation migration did not preserve every row")
    connection.exec_driver_sql(f'DROP TABLE "{backup_table}"')
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Runtime operation migration produced an invalid reference")
