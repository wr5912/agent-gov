from __future__ import annotations

from sqlalchemy.engine import Connection

from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.runtime.runtime_db_migrations_0055 import (
    refresh_0055_workspace_activation_authority,
)
from app.runtime.runtime_db_migrations_0057 import (
    refresh_0057_workspace_activation_recovery_authority,
)


def migrate_0058_workspace_activation_authority_hardening(
    connection: Connection,
) -> None:
    """Upgrade volumes that applied earlier Phase 7 activation triggers."""

    begin_sqlite_write_transaction(connection)
    refresh_0055_workspace_activation_authority(connection)
    refresh_0057_workspace_activation_recovery_authority(connection)
