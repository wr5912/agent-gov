"""Migration 0043: repair indexes for intermediate response-disposition ledgers."""

from sqlalchemy.engine import Connection


def migrate_0043_response_disposition_claims(connection: Connection) -> None:
    """Leave table ownership to the ORM and only repair an already-present table."""

    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(response_disposition_claims)")}
    required = {"approval_request_id", "case_id", "execution_run_id", "response_id", "agent_run_id", "status", "created_at"}
    if not required <= columns:
        return
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_response_disposition_claim_status ON response_disposition_claims (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_response_disposition_claim_case ON response_disposition_claims (case_id, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_response_disposition_claims_execution_run_id ON response_disposition_claims (execution_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_response_disposition_claims_response_id ON response_disposition_claims (response_id)",
        "CREATE INDEX IF NOT EXISTS ix_response_disposition_claims_agent_run_id ON response_disposition_claims (agent_run_id)",
    ):
        connection.exec_driver_sql(statement)
