"""真实 SQLite 来源/关联契约；账本输入不冒充真实 Agent 对话验收。"""

import pytest
from app.runtime.errors import BusinessRuleViolation, FeedbackEventIdConflictError
from app.runtime.feedback_entities import parse_entities
from app.runtime.feedback_event_identity import FEEDBACK_EVENT_IMMUTABLE_FIELDS
from app.runtime.runtime_db import FeedbackEventModel
from app.runtime.schemas import FeedbackEventIngestRequest, FeedbackSignalCreateRequest
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from pydantic import ValidationError

from feedback_store_test_utils import _run_payload


def _event(event_id, **fields):
    payload = {
        "event_id": event_id,
        "source_system": "document-review",
        "event_type": "document.annotation.corrected",
        "timestamp": "2026-09-13T00:00:00Z",
        **fields,
    }
    return FeedbackEventIngestRequest.model_validate(payload)


def test_feedback_event_identity_covers_every_ingest_request_field():
    assert set(FEEDBACK_EVENT_IMMUTABLE_FIELDS) == set(FeedbackEventIngestRequest.model_fields)


def test_generic_entities_correlate_only_a_unique_run_and_preserve_case_identity(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    for run_id in ("first", "second"):
        store.record_run(_run_payload(run_id=run_id, session_id="shared", agent_id="documents"))
    pending = store.ingest_feedback_event(_event("uncertain", session_id="shared", entities={"document": ["guide-1"]}))
    assert pending.correlation_status == "pending_correlation"
    assert pending.event.matched_run_id is None
    assert pending.pending_correlation is not None
    with pytest.raises(BusinessRuleViolation, match="existing Agent run"):
        store.resolve_pending(pending.pending_correlation.pending_id, session_id="shared")
    resolved = store.resolve_pending(pending.pending_correlation.pending_id, run_id="first")
    assert resolved["resolved_run_id"] == "first"
    assert resolved["entities"] == {"document": ["guide-1"]}
    case = store.create_case(source_refs=[("pending_correlation", pending.pending_correlation.pending_id)])
    assert case["entities"] == {"document": ["guide-1"]}
    assert case["run_ids"] == ["first"]
    source = store.find_feedback_source("event", "uncertain")
    assert source["feedback_case_id"] == case["feedback_case_id"]
    assert "case" not in source["entities"]
    linked = store.ingest_feedback_event(_event("next", entities={"document": ["guide-1"]}))
    assert linked.event.matched_run_id == "first"
    assert linked.event.agent_id == "documents"
    store.record_run(_run_payload(run_id="foreign", agent_id="another-agent"))
    store.ingest_feedback_event(_event("another-owner", run_id="foreign", entities={"document": ["guide-1"]}))
    ambiguous = store.ingest_feedback_event(_event("ambiguous", entities={"document": ["guide-1"]}))
    assert ambiguous.correlation_status == "pending_correlation"


def test_feedback_event_exact_retry_normalizes_timestamp_and_entity_order(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    first = store.ingest_feedback_event(
        _event(
            "stable-event",
            timestamp="2026-09-13T02:00:00.1234567890+02:00",
            entities={"document": ["guide-b", "guide-a"]},
            metadata={"region": "north", "source": "review"},
            auto_captured=False,
        )
    )

    duplicate = store.ingest_feedback_event(
        _event(
            "stable-event",
            timestamp="2026-09-13T00:00:00.123456789Z",
            entities={"document": ["guide-a", "guide-b", "guide-a"]},
            metadata={"source": "review", "region": "north"},
        )
    )

    assert duplicate.correlation_status == "duplicate"
    assert duplicate.event == first.event
    assert duplicate.event.timestamp == "2026-09-13T00:00:00.123456789Z"
    assert "ingestion_request_sha256" not in duplicate.to_payload()["event"]
    assert len(store.list_pending(status="pending")) == 1

    with pytest.raises(FeedbackEventIdConflictError):
        store.ingest_feedback_event(
            _event(
                "stable-event",
                timestamp="2026-09-13T00:00:00.123456788Z",
                entities={"document": ["guide-a", "guide-b"]},
                metadata={"source": "review", "region": "north"},
            )
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"source_system": "another-source"},
        {"event_type": "document.annotation.deleted"},
        {"timestamp": "2026-09-13T00:00:01Z"},
        {"run_id": "another-run"},
        {"session_id": "another-session"},
        {"entities": {"document": ["another-document"]}},
        {"actor_id": "another-actor"},
        {"before": {"citation": "old"}},
        {"after": {"citation": "new"}},
        {"confidence": "high"},
        {"requires_review": False},
        {"comment": "changed"},
        {"metadata": {"source": "changed"}},
    ],
)
def test_feedback_event_id_cannot_be_reused_for_different_immutable_input(tmp_path, changed):
    store = FeedbackStore(data_dir=tmp_path)
    original = store.ingest_feedback_event(_event("immutable-event"))

    with pytest.raises(FeedbackEventIdConflictError) as caught:
        store.ingest_feedback_event(_event("immutable-event", **changed))

    assert caught.value.status_code == 409
    assert caught.value.error_code == "FEEDBACK_EVENT_ID_CONFLICT"
    assert caught.value.error_details == {"event_id": "immutable-event"}
    assert store.find_event("immutable-event") == original.event.to_payload()
    assert len(store.list_pending(status="pending")) == 1


def test_enriched_legacy_event_binds_only_the_first_compatible_retry(tmp_path):
    """Pre-contract rows may contain correlation data absent from the old request."""
    store = FeedbackStore(data_dir=tmp_path)
    pending = store.ingest_feedback_event(_event("legacy-enriched", metadata={"source": "review"}))
    assert pending.pending_correlation is not None
    store.record_run(_run_payload(run_id="run-added-later", session_id="session-added-later"))
    store.resolve_pending(
        pending.pending_correlation.pending_id,
        run_id="run-added-later",
        entities={"document": ["guide-added-later"]},
    )
    with store.Session.begin() as db:
        row = db.get(FeedbackEventModel, "legacy-enriched")
        assert row is not None
        payload = dict(row.payload_json)
        payload.pop("ingestion_request_sha256")
        row.payload_json = payload

    with pytest.raises(FeedbackEventIdConflictError):
        store.ingest_feedback_event(
            _event(
                "legacy-enriched",
                session_id="another-session",
                metadata={"source": "review"},
            )
        )
    with store.Session() as db:
        rejected = db.get(FeedbackEventModel, "legacy-enriched")
        assert rejected is not None
        assert "ingestion_request_sha256" not in rejected.payload_json

    duplicate = store.ingest_feedback_event(_event("legacy-enriched", metadata={"source": "review"}))
    assert duplicate.correlation_status == "duplicate"
    assert duplicate.event.session_id == "session-added-later"
    assert duplicate.event.entities == {"document": ["guide-added-later"]}
    with store.Session() as db:
        bound = db.get(FeedbackEventModel, "legacy-enriched")
        assert bound is not None
        assert len(str(bound.payload_json["ingestion_request_sha256"])) == 64

    with pytest.raises(FeedbackEventIdConflictError):
        store.ingest_feedback_event(_event("legacy-enriched", comment="different", metadata={"source": "review"}))


def test_unresolved_legacy_event_does_not_get_resolved_locator_compatibility(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    store.ingest_feedback_event(
        _event(
            "legacy-still-pending",
            session_id="caller-session",
            entities={"document": ["caller-document"]},
        )
    )
    with store.Session.begin() as db:
        row = db.get(FeedbackEventModel, "legacy-still-pending")
        assert row is not None
        payload = dict(row.payload_json)
        payload.pop("ingestion_request_sha256")
        row.payload_json = payload

    with pytest.raises(FeedbackEventIdConflictError):
        store.ingest_feedback_event(_event("legacy-still-pending"))
    exact = store.ingest_feedback_event(
        _event(
            "legacy-still-pending",
            session_id="caller-session",
            entities={"document": ["caller-document"]},
        )
    )
    assert exact.correlation_status == "duplicate"


def test_sensitive_entity_type_names_keep_shape_while_open_content_is_scrubbed(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    entities = {"access_token": ["business-object-1"], "credential_kind": ["kind-1"]}
    store.record_run(_run_payload(run_id="sensitive-entity-run", agent_id="documents"))
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            signal_id="sensitive-kind-signal",
            entities=entities,
            metadata={"api_key": "signal-secret"},
        )
    )
    event = store.ingest_feedback_event(
        _event(
            "sensitive-kind-event",
            entities=entities,
            before={"access_token": "before-secret"},
            metadata={"api_key": "event-secret"},
        )
    )

    assert signal["entities"] == entities
    assert signal["metadata"]["api_key"] == "[REDACTED]"
    assert signal["metadata"]["attribution_status"] == "unassigned"
    assert event.event.entities == entities
    assert event.event.before == {"access_token": "[REDACTED]"}
    assert event.event.metadata == {"api_key": "[REDACTED]"}
    duplicate = store.ingest_feedback_event(
        _event(
            "sensitive-kind-event",
            entities=entities,
            before={"access_token": "different-secret"},
            metadata={"api_key": "different-secret"},
        )
    )
    assert duplicate.correlation_status == "duplicate"
    attributed_signal = store.create_signal(
        FeedbackSignalCreateRequest(
            signal_id="sensitive-kind-attributed",
            run_id="sensitive-entity-run",
            entities=entities,
        )
    )
    feedback_case = store.create_case(source_refs=[("signal", attributed_signal["signal_id"])])
    assert feedback_case is not None
    assert feedback_case["entities"] == entities


def test_production_redacted_evidence_preserves_typed_entities_and_file_hashes(tmp_path):
    store = FeedbackStore(data_dir=tmp_path, enable_debug_evidence=False)
    entities = {"access_token": ["business-object-1"], "credential_kind": ["kind-1"]}
    store.record_run(
        _run_payload(
            run_id="redacted-evidence-run",
            agent_id="documents",
            entities=entities,
            metadata={"entities": "run-free-form", "api_key": "run-secret"},
        )
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            signal_id="redacted-evidence-signal",
            run_id="redacted-evidence-run",
            entities=entities,
            metadata={"entities": "signal-free-form", "api_key": "signal-secret"},
        )
    )
    event = store.ingest_feedback_event(
        _event(
            "redacted-evidence-event",
            run_id="redacted-evidence-run",
            entities=entities,
            metadata={"entities": "event-free-form", "api_key": "event-secret"},
        )
    )
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"]), ("event", event.event.event_id)])

    manifest = store.create_evidence_package(feedback_case["feedback_case_id"])
    assert manifest is not None
    included_files = manifest.get("included_files")
    assert isinstance(included_files, list)
    included = {item["path"]: item["sha256"] for item in included_files}
    expected_nested_entities = {
        "feedback.json": "signal-free-form",
        "runs.json": "run-free-form",
        "events.json": "event-free-form",
    }
    for file_name in ("feedback.json", "runs.json", "events.json"):
        evidence_file = store.get_evidence_package_file(manifest["evidence_package_id"], file_name)
        assert evidence_file is not None
        assert evidence_file["sha256"] == included[file_name]
        records = evidence_file["content"]
        assert isinstance(records, list) and records
        assert parse_entities(records[0]["entities"]) == entities
        assert records[0]["metadata"]["entities"] == expected_nested_entities[file_name]
        assert records[0]["metadata"]["api_key"] == "[REDACTED]"
    feedback_file = store.get_evidence_package_file(manifest["evidence_package_id"], "feedback.json")
    assert feedback_file is not None
    assert feedback_file["content"][0]["metadata"]["api_key"] == "[REDACTED]"


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-13T00:00:00",
        "2026-09-13 00:00:00Z",
        "2026-09-13T00:00:00-00:00",
        "2026-02-30T00:00:00Z",
    ],
)
def test_feedback_event_timestamp_rejects_non_rfc3339_or_unknown_offset(timestamp):
    with pytest.raises(ValidationError, match="timestamp"):
        _event("invalid-timestamp", timestamp=timestamp)


def test_entity_filters_are_exact_and_support_non_soc_names(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    references = {"document": ["a", "ab"], 'object."[]': ["100%_'"]}
    store.record_run(_run_payload(run_id="one", entities=references))
    store.record_run(_run_payload(run_id="two", entities={"document": ["abc"]}))
    store.create_signal(FeedbackSignalCreateRequest(run_id="one", entities=references))
    store.ingest_feedback_event(_event("document-event", run_id="one", entities=references))
    for query in (store.list_runs, store.list_signals, store.list_events):
        assert len(query(entity_type="document", entity_id="a")) == 1
        assert len(query(entity_type=" document ", entity_id=" a ")) == 1
        assert query(entity_type="document", entity_id="b") == []
        assert len(query(entity_type='object."[]', entity_id="100%_'")) == 1
        with pytest.raises(BusinessRuleViolation, match="成对"):
            query(entity_type="document")


def test_entity_canonical_key_collision_merges_ids_without_breaking_query(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    event = store.ingest_feedback_event(
        _event(
            "canonical-collision",
            entities={"document": ["doc-1"], " document ": [" doc-2 ", "doc-1"]},
        )
    )

    assert event.event.entities == {"document": ["doc-1", "doc-2"]}
    assert [item["event_id"] for item in store.list_events(entity_type=" document ", entity_id=" doc-2 ")] == ["canonical-collision"]


@pytest.mark.parametrize(
    "entities",
    [
        {"document": "bad", " document ": [" doc-1 "]},
        {"document": [" doc-1 "], " document ": "bad"},
    ],
)
def test_entity_canonical_key_collision_rejects_invalid_value_regardless_of_order(entities):
    with pytest.raises(ValidationError):
        _event("invalid-collision", entities=entities)


def test_corrected_source_owner_cannot_attribute_new_entities_to_the_old_agent(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    registry = AgentRegistryStore(store.Session)
    for agent_id in ("agent-a", "agent-b"):
        registry.create_business_agent(name=agent_id, agent_id=agent_id, workspace_dir=str(tmp_path / agent_id))
    store.agent_exists = registry.has_agent
    store.record_run(_run_payload(run_id="run-a", agent_id="agent-a"))
    entities = {"document": ["guide-1"]}
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-a", entities=entities))
    matched = store.find_run_for_event({"entities": entities})
    assert matched is not None and matched["run_id"] == "run-a"

    corrected = store.reassign_signal_agent(signal["signal_id"], agent_id="agent-b", operator="reviewer")
    assert corrected.agent_id == "agent-b"
    assert corrected.run_id is None and corrected.matched_run_id is None and corrected.session_id is None
    correction = corrected.metadata["attribution_corrections"][-1]
    assert correction["detached_run_id"] == "run-a"
    assert correction["detached_session_id"] == "session-run-a"
    assert store.find_run_for_event({"entities": entities}) is None
    event = store.ingest_feedback_event(_event("after-correction", entities=entities))
    assert event.correlation_status == "pending_correlation"
    assert event.event.agent_id is None and event.event.matched_run_id is None
    next_signal = store.create_signal(FeedbackSignalCreateRequest(entities=entities))
    assert next_signal["agent_id"] is None and next_signal["matched_run_id"] is None
    assert store.find_signal(signal["signal_id"])["agent_id"] == "agent-b"


def test_explicit_run_reference_cannot_fall_back_to_another_session(tmp_path):
    store = FeedbackStore(data_dir=tmp_path)
    store.record_run(_run_payload(run_id="one", session_id="first"))
    for run_id, session_id in (("one", "other"), ("missing", "first")):
        with pytest.raises(BusinessRuleViolation, match="Session"):
            store.ingest_feedback_event(_event(f"{run_id}-{session_id}", run_id=run_id, session_id=session_id))
        with pytest.raises(BusinessRuleViolation, match="Session"):
            store.create_signal(FeedbackSignalCreateRequest(run_id=run_id, session_id=session_id))
    assert store.list_events() == []
    assert store.list_signals() == []


@pytest.mark.parametrize("entities", [{"document": "not-a-list"}, {" ": ["id"]}, {"document": [""]}])
def test_entity_inputs_reject_malformed_values(entities):
    with pytest.raises(ValidationError):
        _event("bad", entities=entities)


def test_legacy_business_fields_are_not_a_second_input_contract():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FeedbackSignalCreateRequest(alert_id="old")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _event("old", case_id="old")
    with pytest.raises(ValidationError):
        FeedbackEventIngestRequest(event_id="blank", source_system="docs", event_type=" ", timestamp="now")
