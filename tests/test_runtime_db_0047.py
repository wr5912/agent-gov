from __future__ import annotations

import json

from app.runtime.runtime_db import SchemaMigration, make_session_factory
from app.runtime.runtime_db_migrations_0047 import migrate_0047_rename_business_agent_evidence_fields
from sqlalchemy import create_engine


def test_0047_renames_existing_evidence_manifest_and_file(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    manifest = {
        "main_agent_version_id": "version-a",
        "completeness": {"has_main_agent_version": True},
        "included_files": [{"path": "main_agent_version.json", "sha256": "sha", "type": "agent_version"}],
    }
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE evidence_packages (evidence_package_id VARCHAR PRIMARY KEY, manifest_json JSON)")
        connection.exec_driver_sql(
            "CREATE TABLE evidence_files (evidence_package_id VARCHAR, file_name VARCHAR, content_json JSON, PRIMARY KEY (evidence_package_id, file_name))"
        )
        connection.exec_driver_sql(
            "INSERT INTO evidence_packages VALUES (?, ?)",
            ("evp-1", json.dumps(manifest)),
        )
        connection.exec_driver_sql(
            "INSERT INTO evidence_files VALUES (?, ?, ?)",
            ("evp-1", "main_agent_version.json", json.dumps({"main_agent_version_id": "version-a"})),
        )
        migrate_0047_rename_business_agent_evidence_fields(connection)
        migrate_0047_rename_business_agent_evidence_fields(connection)

        migrated = json.loads(connection.exec_driver_sql("SELECT manifest_json FROM evidence_packages WHERE evidence_package_id = 'evp-1'").scalar_one())
        file_name, content_raw = connection.exec_driver_sql("SELECT file_name, content_json FROM evidence_files WHERE evidence_package_id = 'evp-1'").one()

    assert migrated["business_agent_version_id"] == "version-a"
    assert migrated["completeness"]["has_business_agent_version"] is True
    assert migrated["included_files"][0]["path"] == "business_agent_version.json"
    assert "main_agent_version_id" not in migrated
    assert file_name == "business_agent_version.json"
    assert json.loads(content_raw) == {"business_agent_version_id": "version-a"}


def test_0047_is_registered_for_fresh_schema(tmp_path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")

    with factory() as db:
        assert db.get(SchemaMigration, "0047_rename_business_agent_evidence_fields") is not None
