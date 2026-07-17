from __future__ import annotations

from sqlalchemy.engine import Connection


def migrate_0045_drop_response_disposition_claims(connection: Connection) -> None:
    """Retire the response-disposition claim store without retaining a shadow archive."""

    connection.exec_driver_sql("DROP TABLE IF EXISTS response_disposition_claims")
