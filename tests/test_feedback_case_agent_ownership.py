from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from app.runtime.errors import BusinessRuleViolation, ConflictError
from app.runtime.runtime_db import FeedbackCaseModel, FeedbackCaseSourceModel, utc_now
from app.runtime.schemas import FeedbackCaseCreateRequest, FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.stores.feedback_store import FeedbackStore
from pydantic import ValidationError

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


def _store(tmp_path) -> FeedbackStore:
    return FeedbackStore(
        data_dir=tmp_path / "data",
        agent_exists=lambda agent_id: agent_id in {ORDINARY_TEST_AGENT_ID, "agent-a", "agent-b"},
    )


def _signal(store: FeedbackStore, *, run_id: str, agent_id: str, signal_id: str | None = None) -> dict:
    store.record_run(
        {
            "run_id": run_id,
            "session_id": f"session-{run_id}",
            "agent_id": agent_id,
            "created_at": "2026-07-13T00:00:00+00:00",
        }
    )
    return store.create_signal(FeedbackSignalCreateRequest(signal_id=signal_id, run_id=run_id, labels=["ownership-test"]))


def test_feedback_case_create_contract_rejects_legacy_untyped_source_ids() -> None:
    with pytest.raises(ValidationError):
        FeedbackCaseCreateRequest.model_validate({"source_ids": ["shared-source"]})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"source_refs": []},
        {"source_refs": [{"source_kind": "signal", "source_id": "   "}]},
        {"source_refs": [{"source_kind": "signal", "source_id": "sig-1"}], "run_id": "client-owned"},
        {"source_refs": [{"source_kind": "signal", "source_id": "sig-1", "run_id": "client-owned"}]},
    ],
)
def test_feedback_case_create_contract_requires_non_empty_typed_source_refs(payload: dict) -> None:
    with pytest.raises(ValidationError):
        FeedbackCaseCreateRequest.model_validate(payload)


def test_feedback_case_create_contract_accepts_supported_source_kinds() -> None:
    payload = {
        "source_refs": [
            {"source_kind": "signal", "source_id": "sig-1"},
            {"source_kind": "soc_event", "source_id": "evt-1"},
            {"source_kind": "pending_correlation", "source_id": "pc-1"},
        ]
    }

    assert FeedbackCaseCreateRequest.model_validate(payload).model_dump(mode="json") == {
        **payload,
        "title": None,
        "priority": "medium",
    }


def test_feedback_case_rejects_sources_owned_by_different_agents_without_writes(tmp_path) -> None:
    store = _store(tmp_path)
    left = _signal(store, run_id="run-a", agent_id="agent-a")
    right = _signal(store, run_id="run-b", agent_id="agent-b")

    with pytest.raises(BusinessRuleViolation, match="one business agent"):
        store.create_case(source_refs=[("signal", left["signal_id"]), ("signal", right["signal_id"])])

    assert store.list_cases() == []


def test_feedback_signal_reassignment_validates_target_and_preserves_case_provenance(tmp_path) -> None:
    store = _store(tmp_path)
    signal = _signal(store, run_id="run-a", agent_id="agent-a")

    with pytest.raises(BusinessRuleViolation, match="does not exist"):
        store.reassign_signal_agent(signal["signal_id"], agent_id="ghost", operator="reviewer")
    assert store.find_signal(signal["signal_id"])["agent_id"] == "agent-a"

    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"])])
    assert feedback_case is not None and feedback_case["agent_id"] == "agent-a"
    with pytest.raises(BusinessRuleViolation, match="immutable while it belongs to a FeedbackCase"):
        store.reassign_signal_agent(signal["signal_id"], agent_id="agent-b", operator="reviewer")

    assert store.find_signal(signal["signal_id"])["agent_id"] == "agent-a"
    assert store.find_case(feedback_case["feedback_case_id"])["agent_id"] == "agent-a"


def test_unmatched_signal_stays_unassigned_and_cannot_create_case(tmp_path) -> None:
    store = _store(tmp_path)

    signal = store.create_signal(FeedbackSignalCreateRequest(session_id="session-missing", comment="unmatched"))

    assert signal["agent_id"] is None
    assert signal["requires_review"] is True
    assert signal["metadata"]["attribution_status"] == "unassigned"
    with pytest.raises(BusinessRuleViolation, match="attributed to a business agent"):
        store.create_case(source_refs=[("signal", signal["signal_id"])])
    assert store.list_cases() == []


def test_session_locator_and_soc_event_persist_business_agent_owner(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_run(
        {
            "run_id": "run-session-owner",
            "session_id": "session-owner",
            "agent_id": "agent-a",
            "created_at": "2026-07-13T00:00:00+00:00",
        }
    )

    signal = store.create_signal(FeedbackSignalCreateRequest(session_id="session-owner"))
    event_result = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-session-owner",
            source_system="test",
            event_type="recommendation.accepted",
            timestamp="2026-07-13T00:00:01+00:00",
            session_id="session-owner",
        )
    )

    assert signal["matched_run_id"] == "run-session-owner"
    assert signal["agent_id"] == "agent-a"
    assert event_result["correlation_status"] == "matched"
    assert event_result["event"]["agent_id"] == "agent-a"
    feedback_case = store.create_case(source_refs=[("signal", signal["signal_id"]), ("soc_event", "event-session-owner")])
    assert feedback_case is not None
    assert feedback_case["agent_id"] == "agent-a"


def test_resolved_pending_updates_event_owner_before_case_creation(tmp_path) -> None:
    store = _store(tmp_path)
    ingested = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-pending",
            source_system="test",
            event_type="recommendation.rejected",
            timestamp="2026-07-13T00:00:00+00:00",
            session_id="session-later",
        )
    )
    pending_id = ingested["pending_correlation"]["pending_id"]
    store.record_run(
        {
            "run_id": "run-later",
            "session_id": "session-later",
            "agent_id": "agent-b",
            "created_at": "2026-07-13T00:00:01+00:00",
        }
    )

    resolved = store.resolve_pending(pending_id, run_id="run-later", comment="reviewed")

    assert resolved is not None and resolved["resolved_run_id"] == "run-later"
    assert store.find_event("event-pending")["agent_id"] == "agent-b"
    feedback_case = store.create_case(source_refs=[("pending_correlation", pending_id)])
    assert feedback_case is not None
    assert feedback_case["agent_id"] == "agent-b"
    assert feedback_case["event_ids"] == ["event-pending"]
    pending_source = store.find_feedback_source("pending_correlation", pending_id)
    event_source = store.find_feedback_source("soc_event", "event-pending")
    assert pending_source is not None and event_source is not None
    assert pending_source["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert event_source["feedback_case_id"] == feedback_case["feedback_case_id"]


def test_case_creation_and_reassignment_are_serialized_without_owner_split(tmp_path) -> None:
    store = _store(tmp_path)
    signal = _signal(store, run_id="run-race", agent_id="agent-a")
    start = Event()

    def create_case():
        start.wait(timeout=3)
        return store.create_case(source_refs=[("signal", signal["signal_id"])])

    def reassign():
        start.wait(timeout=3)
        try:
            return store.reassign_signal_agent(signal["signal_id"], agent_id="agent-b", operator="reviewer")
        except BusinessRuleViolation:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        case_future = pool.submit(create_case)
        reassign_future = pool.submit(reassign)
        start.set()
        feedback_case = case_future.result(timeout=5)
        reassign_future.result(timeout=5)

    assert feedback_case is not None
    persisted_signal = store.find_signal(signal["signal_id"])
    persisted_case = store.find_case(feedback_case["feedback_case_id"])
    assert persisted_signal is not None and persisted_case is not None
    assert persisted_signal["agent_id"] == persisted_case["agent_id"]


def test_feedback_source_can_belong_to_only_one_case(tmp_path) -> None:
    store = _store(tmp_path)
    signal = _signal(store, run_id="run-single-owner", agent_id="agent-a")
    first = store.create_case(source_refs=[("signal", signal["signal_id"])])

    with pytest.raises(ConflictError, match="already belongs"):
        store.create_case(source_refs=[("signal", signal["signal_id"])])

    assert first is not None
    assert [case["feedback_case_id"] for case in store.list_cases()] == [first["feedback_case_id"]]


def test_concurrent_ensure_case_for_source_is_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    signal = _signal(store, run_id="run-source-race", agent_id="agent-a")
    start = Event()

    def ensure_case():
        start.wait(timeout=3)
        return store.ensure_case_for_source("signal", signal["signal_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ensure_case) for _ in range(2)]
        start.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(result is not None for result in results)
    assert len({result["feedback_case_id"] for result in results if result}) == 1
    assert len(store.list_cases()) == 1


def test_case_rejects_source_owned_by_unregistered_agent(tmp_path) -> None:
    store = _store(tmp_path)
    signal = _signal(store, run_id="run-ghost-owner", agent_id="ghost")

    with pytest.raises(BusinessRuleViolation, match="does not exist"):
        store.create_case(source_refs=[("signal", signal["signal_id"])])

    assert store.list_cases() == []


def test_feedback_signal_create_is_idempotent_only_for_identical_content(tmp_path) -> None:
    store = _store(tmp_path)
    original = _signal(store, run_id="run-stable-id", agent_id="agent-a", signal_id="shared-signal")

    repeated = store.create_signal(
        FeedbackSignalCreateRequest(
            signal_id="shared-signal",
            run_id="run-stable-id",
            labels=["ownership-test"],
        )
    )

    assert repeated == original
    assert [item["signal_id"] for item in store.list_signals()] == ["shared-signal"]


def test_feedback_signal_create_cannot_overwrite_case_owned_identity(tmp_path) -> None:
    store = _store(tmp_path)
    original = _signal(store, run_id="run-owner-a", agent_id="agent-a", signal_id="claimed-signal")
    feedback_case = store.create_case(source_refs=[("signal", original["signal_id"])])
    _signal(store, run_id="run-owner-b", agent_id="agent-b")

    with pytest.raises(ConflictError, match="already owned by different content"):
        store.create_signal(
            FeedbackSignalCreateRequest(
                signal_id="claimed-signal",
                run_id="run-owner-b",
                labels=["ownership-test"],
            )
        )

    assert feedback_case is not None
    assert store.find_signal("claimed-signal")["agent_id"] == "agent-a"
    assert store.find_case(feedback_case["feedback_case_id"])["agent_id"] == "agent-a"


def test_concurrent_feedback_signal_id_collision_has_one_immutable_winner(tmp_path) -> None:
    store = _store(tmp_path)
    _signal(store, run_id="run-racer-a", agent_id="agent-a")
    _signal(store, run_id="run-racer-b", agent_id="agent-b")
    start = Event()

    def create(run_id: str):
        start.wait(timeout=3)
        try:
            return store.create_signal(
                FeedbackSignalCreateRequest(
                    signal_id="raced-signal",
                    run_id=run_id,
                    labels=["raced"],
                )
            )
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, run_id) for run_id in ("run-racer-a", "run-racer-b")]
        start.set()
        results = [future.result(timeout=5) for future in futures]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    persisted = store.find_signal("raced-signal")
    assert persisted is not None
    assert persisted == winners[0]


def test_case_source_kind_prevents_cross_table_id_collision(tmp_path) -> None:
    store = _store(tmp_path)
    _signal(store, run_id="run-collision-a", agent_id="agent-a", signal_id="shared-source")
    store.record_run(
        {
            "run_id": "run-collision-b",
            "session_id": "session-collision-b",
            "agent_id": "agent-b",
            "created_at": "2026-07-13T00:00:00+00:00",
        }
    )
    store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="shared-source",
            source_system="test",
            event_type="recommendation.accepted",
            timestamp="2026-07-13T00:00:01+00:00",
            run_id="run-collision-b",
        )
    )

    feedback_case = store.ensure_case_for_source("soc_event", "shared-source")

    assert feedback_case is not None
    assert feedback_case["agent_id"] == "agent-b"
    assert feedback_case["signal_ids"] == []
    assert feedback_case["event_ids"] == ["shared-source"]
    assert store.find_feedback_source("signal", "shared-source")["feedback_case_id"] is None
    assert store.find_feedback_source("soc_event", "shared-source")["feedback_case_id"] == feedback_case["feedback_case_id"]


def test_case_and_evidence_projection_use_claims_and_exclude_unclaimed_loser(tmp_path) -> None:
    version_agent_ids: list[str | None] = []
    store = FeedbackStore(
        data_dir=tmp_path / "data",
        agent_exists=lambda agent_id: agent_id in {ORDINARY_TEST_AGENT_ID, "agent-a", "agent-b"},
        agent_version_provider=lambda agent_id: version_agent_ids.append(agent_id) or f"version-{agent_id}",
    )
    for run_id, agent_id in (("run-claimed", "agent-a"), ("run-stale", "agent-b")):
        store.record_run(
            {
                "run_id": run_id,
                "session_id": f"session-{run_id}",
                "agent_id": agent_id,
                "created_at": "2026-07-13T00:00:00+00:00",
            }
        )
        store.ingest_soc_event(
            SocEventIngestRequest(
                event_id=f"event-{run_id.removeprefix('run-')}",
                source_system="test",
                event_type="recommendation.accepted",
                timestamp="2026-07-13T00:00:01+00:00",
                run_id=run_id,
            )
        )

    now = utc_now()
    with store.Session.begin() as db:
        db.add_all(
            [
                FeedbackCaseModel(
                    feedback_case_id="case-claim-winner",
                    agent_id=ORDINARY_TEST_AGENT_ID,
                    created_at=now,
                    updated_at=now,
                    status="pending_evidence",
                    title="claim winner",
                    priority="medium",
                    source_ids_json=["event-stale"],
                    signal_ids_json=[],
                    event_ids_json=["event-stale"],
                    pending_correlation_ids_json=[],
                    run_ids_json=["run-stale"],
                    session_ids_json=["session-run-stale"],
                    alert_ids_json=[],
                    case_ids_json=[],
                ),
                FeedbackCaseModel(
                    feedback_case_id="case-unclaimed-loser",
                    agent_id="agent-b",
                    created_at=now,
                    updated_at=now,
                    status="pending_evidence",
                    title="unclaimed loser",
                    priority="medium",
                    source_ids_json=["event-claimed"],
                    signal_ids_json=[],
                    event_ids_json=["event-claimed"],
                    pending_correlation_ids_json=[],
                    run_ids_json=["run-claimed"],
                    session_ids_json=["session-run-claimed"],
                    alert_ids_json=[],
                    case_ids_json=[],
                ),
                FeedbackCaseSourceModel(
                    source_kind="soc_event",
                    source_id="event-claimed",
                    case_id="case-claim-winner",
                    agent_id="agent-a",
                    is_direct=True,
                    direct_position=0,
                    created_at=now,
                ),
            ]
        )

    projected = store.find_case("case-claim-winner")
    assert projected is not None
    assert projected["agent_id"] == "agent-a"
    assert projected["source_ids"] == ["event-claimed"]
    assert projected["event_ids"] == ["event-claimed"]
    assert projected["run_ids"] == ["run-claimed"]
    assert [case["feedback_case_id"] for case in store.list_cases(agent_id="agent-a")] == ["case-claim-winner"]
    assert store.list_cases(agent_id=ORDINARY_TEST_AGENT_ID) == []
    assert store.find_case("case-unclaimed-loser") is None
    assert store.create_evidence_package("case-unclaimed-loser") is None
    assert store.find_feedback_source("soc_event", "event-claimed")["feedback_case_id"] == "case-claim-winner"
    assert store.find_feedback_source("soc_event", "event-stale")["feedback_case_id"] is None

    evidence = store.create_evidence_package("case-claim-winner")
    assert evidence is not None
    assert evidence["business_agent_version_id"] == "version-agent-a"
    assert evidence["source_refs"]["event_ids"] == ["event-claimed"]
    assert evidence["source_refs"]["run_ids"] == ["run-claimed"]
    event_file = store.get_evidence_package_file(evidence["evidence_package_id"], "soc_events.json")
    assert [event["event_id"] for event in event_file["content"]] == ["event-claimed"]
    assert version_agent_ids == ["agent-a"]

    with store.Session() as db:
        repaired = db.get(FeedbackCaseModel, "case-claim-winner")
        assert repaired is not None
        assert repaired.agent_id == "agent-a"
        assert repaired.source_ids_json == ["event-claimed"]
        assert repaired.event_ids_json == ["event-claimed"]
        assert repaired.run_ids_json == ["run-claimed"]


@pytest.mark.parametrize(
    "request_type,payload",
    [
        (FeedbackSignalCreateRequest, {"comment": "hostile", "agent_id": "agent-b"}),
        (
            SocEventIngestRequest,
            {
                "event_id": "evt-hostile-agent",
                "source_system": "test",
                "event_type": "recommendation.accepted",
                "timestamp": "2026-07-13T00:00:01+00:00",
                "agent_id": "agent-b",
            },
        ),
    ],
)
def test_feedback_ingest_requests_forbid_backend_owned_agent_id(request_type, payload) -> None:
    with pytest.raises(ValidationError, match="agent_id"):
        request_type.model_validate(payload)
