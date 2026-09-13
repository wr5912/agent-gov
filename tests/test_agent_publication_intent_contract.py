from __future__ import annotations

import pytest
from app.services.agent_publication import PublicationIntent


def _intent() -> PublicationIntent:
    return PublicationIntent(
        release_id="agr-release",
        change_set_id="agcs-change",
        agent_id="security-operations-expert",
        commit_sha="a" * 40,
        diff_digest="b" * 64,
        test_run_id="agtr-run",
        suite_digest="c" * 64,
        tag_name="agent-release-agcs-change",
        operator="tester",
        note=None,
        force=False,
        force_publication_blocker=None,
        previous_status="candidate_committed",
        started_at="2026-09-12T00:00:00+00:00",
        previous_commit_sha="d" * 40,
    )


def test_publication_intent_round_trip_has_no_internal_schema_version() -> None:
    intent = _intent()
    record = intent.to_payload()

    assert "schema_version" not in record
    assert PublicationIntent.from_payload(record) == intent


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_publication_intent_requires_exact_fields(mutation: str) -> None:
    record = _intent().to_payload()
    if mutation == "extra":
        record["schema_version"] = "agent-publication-intent/v1"
    else:
        record.pop("diff_digest")

    with pytest.raises(ValueError, match="schema is invalid"):
        PublicationIntent.from_payload(record)
