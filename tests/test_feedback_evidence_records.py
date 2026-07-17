from app.runtime.runtime_db import EvidencePackageModel
from pydantic import ValidationError

from feedback_store_test_utils import FeedbackSignalCreateRequest, _record_run, _store, pytest


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
