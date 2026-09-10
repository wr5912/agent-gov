from __future__ import annotations

import pytest
from scripts.codex_governance_legacy_feedback import legacy_feedback_active_refs


@pytest.mark.parametrize("path", ["app/runtime/new_formatter.py", "app/runtime/runtime_db_migrations.py"])
def test_removed_migration_path_cannot_exempt_legacy_output_schema(path: str) -> None:
    references = legacy_feedback_active_refs(path, 'output_schema_version = "legacy"')
    assert len(references) == 1
    assert "agent job output_schema_version reference" in next(iter(references))
