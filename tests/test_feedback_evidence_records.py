from app.runtime.errors import BusinessRuleViolation
from app.runtime.records.source_records import upsert_agent_run_record
from app.runtime.runtime_db import EvidencePackageModel
from pydantic import ValidationError

from feedback_store_test_utils import FeedbackSignalCreateRequest, _record_run, _run_payload, _store, pytest


def test_evidence_package_projection_rejects_invalid_persisted_file_path(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"])])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    with store.Session.begin() as db:
        row = db.get(EvidencePackageModel, evidence["evidence_package_id"])
        manifest = dict(row.manifest_json or {})
        manifest["included_files"] = [
            dict(manifest["included_files"][0]),
            {"path": "../feedback.json", "sha256": "0" * 64, "type": "feedback"},
        ]
        row.manifest_json = manifest

    with pytest.raises(ValidationError):
        store.get_evidence_package(evidence["evidence_package_id"])


def test_run_history_cursor_covers_same_timestamp_and_concurrent_new_rows(tmp_path):
    """真实 SQLite 查询契约；账本排序输入不作为 Runtime/模型验收证据。"""
    store, _ = _store(tmp_path)
    records = [store.prepare_run_record(_run_payload(run_id=f"history-{index:04d}", session_id="history-session")) for index in range(501)]
    with store.Session.begin() as db:
        for record in records:
            upsert_agent_run_record(db, record)
    first = store.list_runs(session_id="history-session", limit=500)
    assert len(first) == 500
    store.record_run(_run_payload(run_id="new-after-first-page", session_id="history-session", created_at="2026-09-13T00:00:00+00:00"))
    second = store.list_runs(
        session_id="history-session",
        limit=500,
        before_created_at=first[-1]["created_at"],
        before_run_id=first[-1]["run_id"],
    )
    assert len(second) == 1
    assert [row["run_id"] for row in first + second] == [record.run_id for record in reversed(records)]
    assert store.list_runs(session_id="another-session", limit=500) == []
    with pytest.raises(BusinessRuleViolation):
        store.list_runs(before_run_id="history-0001")
