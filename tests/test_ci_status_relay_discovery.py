from __future__ import annotations

import json
import sqlite3
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tests.ci_status_relay_support import (
    PR_SHA,
    PUSH_SHA,
    FakeGitHub,
    _config,
    _pull,
    _responses,
    _run,
    relay,
)


def test_workflow_path_accepts_ref_suffix_but_rejects_another_workflow(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    suffixed = {
        **_run(
            100,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=6,
        ),
        "path": ".github/workflows/governance.yml@refs/heads/master",
    }

    parsed = relay.parse_workflow_run(
        config,
        suffixed,
        expected_event="pull_request",
    )

    assert parsed is not None
    assert parsed.run_id == 100
    assert (
        relay.parse_workflow_run(
            config,
            {
                **suffixed,
                "path": ".github/workflows/another.yml@refs/heads/master",
            },
            expected_event="pull_request",
        )
        is None
    )


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_terminal_workflow_conclusion_is_preserved(
    tmp_path: Path,
    conclusion: str,
) -> None:
    config = _config(tmp_path)
    parsed = relay.parse_workflow_run(
        config,
        _run(
            100,
            event="pull_request",
            sha=PR_SHA,
            conclusion=conclusion,
            pull_number=6,
        ),
        expected_event="pull_request",
    )

    assert parsed is not None
    assert parsed.conclusion == conclusion
    replayed = relay._replayed_workflow_run(  # noqa: SLF001 - replay contract
        relay._run_replay_payload(parsed)  # noqa: SLF001 - replay contract
    )
    assert replayed.conclusion == conclusion


@pytest.mark.parametrize("conclusion", [None, "unknown"])
def test_invalid_terminal_conclusion_fails_stream_and_persists_evidence(
    tmp_path: Path,
    conclusion: object,
) -> None:
    config = _config(tmp_path)
    invalid = {
        **_run(
            100,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=6,
        ),
        "conclusion": conclusion,
    }
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        with pytest.raises(relay.RelayError, match="invalid workflow conclusion"):
            relay.discover_notifications(
                config,
                FakeGitHub(
                    _responses(
                        config,
                        pull_runs=[invalid],
                        push_runs=[],
                    )
                ),
                store,
            )
        snapshot = store.snapshot()
    finally:
        store.close()

    assert snapshot["pending"] == 0
    assert snapshot["discovery_failures"] == 1
    assert snapshot["failure_items"][0]["category"] == "github_payload"
    assert snapshot["watermarks"] == []


def test_discovers_pr_and_push_terminal_runs_with_one_aid_and_failure_jobs(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    responses = _responses(
        config,
        pull_runs=[
            _run(
                101,
                event="pull_request",
                sha=PR_SHA,
                conclusion="failure",
                attempt=3,
                pull_number=7,
            ),
            {
                **_run(
                    999,
                    event="pull_request",
                    sha=PR_SHA,
                    conclusion="success",
                    pull_number=7,
                ),
                "status": "in_progress",
            },
        ],
        push_runs=[_run(102, event="push", sha=PUSH_SHA, conclusion="success", attempt=2)],
    )
    responses.update(
        {
            relay.github_path(config, "/pulls/7"): _pull(7),
            relay.github_path(config, f"/commits/{PUSH_SHA}/pulls"): [_pull(8, sha=PUSH_SHA)],
            relay.github_path(
                config,
                "/actions/runs/101/attempts/3/jobs?per_page=100",
            ): {
                "jobs": [
                    {"name": "backend-main-full", "conclusion": "failure"},
                    {"name": "quality-gate", "conclusion": "cancelled"},
                    {"name": "frontend-ui", "conclusion": "success"},
                ]
            },
        }
    )
    github = FakeGitHub(responses)
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, github, store) == (2, 0)
        pending = store.pending()
    finally:
        store.close()

    assert len(pending) == 2
    payloads = [json.loads(str(row["payload"])) for row in pending]
    pr_marker = "agent-gov-ci:wr5912/agent-gov:101:3:failure"
    push_marker = "agent-gov-ci:wr5912/agent-gov:102:2:success"
    pr_comment = next(item["content"] for item in payloads if pr_marker in item["content"])
    push_comment = next(item["content"] for item in payloads if push_marker in item["content"])
    assert pr_marker in pr_comment
    assert "Repository：`wr5912/agent-gov`" in pr_comment
    assert "Branch：`master`" in pr_comment
    assert "状态：`failure`" in pr_comment
    assert "事件：`pull_request`" in pr_comment
    assert "PR：`#7`" in pr_comment
    assert "Run ID：`101`" in pr_comment
    assert "backend-main-full, quality-gate" in pr_comment
    assert push_marker in push_comment
    assert "状态：`success`" in push_comment
    assert "PR：`#8`" in push_comment
    assert "该评论由 228 CI status relay 写入 Multica" in push_comment
    assert (
        relay.github_path(
            config,
            "/actions/runs/102/attempts/2/jobs?per_page=100",
        )
        not in github.requests
    )


def test_conflicting_pr_aid_is_persisted_without_fallback_or_outbox(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    responses = _responses(
        config,
        pull_runs=[
            _run(
                201,
                event="pull_request",
                sha=PR_SHA,
                conclusion="failure",
                pull_number=9,
            )
        ],
        push_runs=[],
    )
    responses[relay.github_path(config, "/pulls/9")] = _pull(
        9,
        body="also references AID-17",
    )
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (
            0,
            1,
        )
        assert store.snapshot()["pending"] == 0
        failures = store.failure_evidence()
    finally:
        store.close()

    assert "must match" in capsys.readouterr().err
    assert len(failures) == 1
    assert failures[0]["category"] == "trace_resolution"
    assert failures[0]["run_id"] == 201
    assert failures[0]["attempt"] == 1
    assert "must match" in failures[0]["detail"]


def test_duplicate_poll_is_idempotent_and_rerun_attempt_gets_a_new_key(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    run = _run(
        301,
        event="pull_request",
        sha=PR_SHA,
        conclusion="success",
        pull_number=10,
    )
    responses = _responses(config, pull_runs=[run], push_runs=[])
    responses[relay.github_path(config, "/pulls/10")] = _pull(10)
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (
            1,
            0,
        )
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (
            0,
            0,
        )
        rerun_responses = _responses(
            config,
            pull_runs=[{**run, "run_attempt": 2}],
            push_runs=[],
        )
        rerun_responses[relay.github_path(config, "/pulls/10")] = _pull(10)
        assert relay.discover_notifications(
            config,
            FakeGitHub(rerun_responses),
            store,
        ) == (1, 0)
        keys = [str(row["dedupe_key"]) for row in store.pending()]
    finally:
        store.close()

    assert keys == [
        "github-run:wr5912/agent-gov:301:1:success",
        "github-run:wr5912/agent-gov:301:2:success",
    ]


def test_pr_metadata_edit_cannot_rekey_the_same_workflow_run(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    run = _run(
        302,
        event="pull_request",
        sha=PR_SHA,
        conclusion="success",
        pull_number=10,
    )
    first = _responses(config, pull_runs=[run], push_runs=[])
    first[relay.github_path(config, "/pulls/10")] = _pull(10)
    edited = _responses(config, pull_runs=[run], push_runs=[])
    edited[relay.github_path(config, "/pulls/10")] = _pull(
        10,
        title="AID-17: conflicting edit",
    )
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, FakeGitHub(first), store) == (
            1,
            0,
        )
        assert relay.discover_notifications(config, FakeGitHub(edited), store) == (
            0,
            0,
        )
        keys = [str(row["dedupe_key"]) for row in store.pending()]
    finally:
        store.close()

    assert keys == ["github-run:wr5912/agent-gov:302:1:success"]


def test_existing_aid_key_bootstraps_run_identity_without_duplicate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    run = _run(
        303,
        event="pull_request",
        sha=PR_SHA,
        conclusion="success",
        pull_number=10,
    )
    responses = _responses(config, pull_runs=[run], push_runs=[])
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    assert store.enqueue(
        "github-run:303:1:success:AID-16",
        {
            "aid": "AID-16",
            "marker": "agent-gov-ci:303:1:AID-16",
            "content": "already delivered by the previous relay version",
        },
    )
    store.mark_delivered(int(store.pending()[0]["id"]))
    try:
        assert relay.discover_notifications(
            config,
            FakeGitHub(responses),
            store,
        ) == (0, 0)
        assert store.snapshot()["delivered"] == 1
        assert store.snapshot()["pending"] == 0
    finally:
        store.close()


def test_same_run_attempt_in_another_repository_is_not_treated_as_duplicate(
    tmp_path: Path,
) -> None:
    first_config = _config(tmp_path)
    second_config = replace(first_config, repository="wr5912/another-agent-gov")
    run = _run(
        304,
        event="pull_request",
        sha=PR_SHA,
        conclusion="success",
        pull_number=10,
    )
    responses = _responses(second_config, pull_runs=[run], push_runs=[])
    responses[relay.github_path(second_config, "/pulls/10")] = _pull(10)
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    assert store.enqueue(
        "github-run:wr5912/agent-gov:304:1:success",
        {
            "aid": "AID-16",
            "marker": "agent-gov-ci:wr5912/agent-gov:304:1:success",
            "content": "first repository result",
        },
    )
    try:
        assert relay.discover_notifications(
            second_config,
            FakeGitHub(responses),
            store,
        ) == (1, 0)
        keys = [str(row["dedupe_key"]) for row in store.pending()]
    finally:
        store.close()

    assert keys == [
        "github-run:wr5912/agent-gov:304:1:success",
        "github-run:wr5912/another-agent-gov:304:1:success",
    ]


def test_resolving_run_failure_does_not_resolve_another_repository(
    tmp_path: Path,
) -> None:
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    for repository in ("wr5912/agent-gov", "wr5912/another-agent-gov"):
        store.record_failure(
            failure_key=f"github-run:{repository}:305:1:trace_resolution",
            category="trace_resolution",
            run_id=305,
            attempt=1,
            detail="temporary linkage failure",
            replay_payload='{"run_id": 305}',
        )
    try:
        retryable = [str(row["failure_key"]) for row in store.retryable_failures("wr5912/agent-gov")]
        store.resolve_run_failures("wr5912/agent-gov", 305, 1)
        evidence = {item["failure_key"]: item["resolved_at"] for item in store.failure_evidence()}
    finally:
        store.close()

    assert retryable == ["github-run:wr5912/agent-gov:305:1:trace_resolution"]
    assert evidence["github-run:wr5912/agent-gov:305:1:trace_resolution"] is not None
    assert evidence["github-run:wr5912/another-agent-gov:305:1:trace_resolution"] is None


def test_push_requires_exactly_one_final_merged_pr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    responses = _responses(
        config,
        pull_runs=[],
        push_runs=[_run(401, event="push", sha=PUSH_SHA, conclusion="success")],
    )
    responses[relay.github_path(config, f"/commits/{PUSH_SHA}/pulls")] = [
        _pull(11, sha=PUSH_SHA),
        _pull(12, sha=PUSH_SHA),
    ]
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (
            0,
            1,
        )
        assert store.snapshot()["pending"] == 0
    finally:
        store.close()

    assert "exactly one merged PR" in capsys.readouterr().err


def test_push_trace_mapping_failure_replays_after_github_linkage_recovers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    run = _run(
        450,
        event="push",
        sha=PUSH_SHA,
        conclusion="success",
    )
    first_responses = _responses(config, pull_runs=[], push_runs=[run])
    first_responses[relay.github_path(config, f"/commits/{PUSH_SHA}/pulls")] = []
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(
            config,
            FakeGitHub(first_responses),
            store,
        ) == (0, 1)
        watermark = store.get_watermark(config, "push")
        assert watermark is not None
        assert watermark.run_id == 450

        second_responses = _responses(config, pull_runs=[], push_runs=[run])
        second_responses[relay.github_path(config, f"/commits/{PUSH_SHA}/pulls")] = [_pull(18, sha=PUSH_SHA)]
        assert relay.discover_notifications(
            config,
            FakeGitHub(second_responses),
            store,
        ) == (1, 0)
        recovered = store.snapshot()
        assert recovered["pending"] == 1
        assert recovered["discovery_failures"] == 0
        assert recovered["failure_items"][0]["resolved_at"] is not None

        third_responses = _responses(config, pull_runs=[], push_runs=[run])
        assert relay.discover_notifications(
            config,
            FakeGitHub(third_responses),
            store,
        ) == (0, 0)
        assert store.snapshot()["pending"] == 1
    finally:
        store.close()


def test_failed_job_payload_failure_replays_after_github_recovers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    run = _run(
        451,
        event="pull_request",
        sha=PR_SHA,
        conclusion="failure",
        pull_number=19,
    )
    jobs_path = relay.github_path(
        config,
        "/actions/runs/451/attempts/1/jobs?per_page=100",
    )
    first_responses = _responses(config, pull_runs=[run], push_runs=[])
    first_responses[relay.github_path(config, "/pulls/19")] = _pull(19)
    first_responses[jobs_path] = {"jobs": "temporarily unavailable"}
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(
            config,
            FakeGitHub(first_responses),
            store,
        ) == (0, 1)

        second_responses = _responses(config, pull_runs=[run], push_runs=[])
        second_responses[relay.github_path(config, "/pulls/19")] = _pull(19)
        second_responses[jobs_path] = {"jobs": [{"name": "backend-main-full", "conclusion": "failure"}]}
        assert relay.discover_notifications(
            config,
            FakeGitHub(second_responses),
            store,
        ) == (1, 0)
        pending = store.pending()
        assert len(pending) == 1
        assert "backend-main-full" in str(pending[0]["payload"])
    finally:
        store.close()


def test_relay_not_before_avoids_historical_comment_backfill(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        not_before=datetime(2026, 7, 16, 9, tzinfo=timezone.utc),
    )
    responses = _responses(
        config,
        pull_runs=[
            _run(
                701,
                event="pull_request",
                sha=PR_SHA,
                conclusion="success",
                pull_number=13,
            )
        ],
        push_runs=[],
    )
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (
            0,
            0,
        )
        assert store.snapshot()["pending"] == 0
    finally:
        store.close()


def test_workflow_query_bounds_full_pagination_to_runs_created_after_install(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        not_before=datetime(2026, 7, 16, 9, tzinfo=timezone.utc),
    )

    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(
            relay.workflow_runs_path(config, "pull_request"),
        ).query
    )

    assert query["created"] == [">=2026-07-16T09:00:00Z"]


def test_discovery_paginates_and_persists_event_watermarks(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), run_limit=2)
    run_801 = _run(
        801,
        event="pull_request",
        sha=PR_SHA,
        conclusion="success",
        pull_number=21,
    )
    run_802 = {
        **_run(
            802,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=22,
        ),
        "updated_at": "2026-07-16T08:01:00Z",
    }
    run_803 = {
        **_run(
            803,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=23,
        ),
        "updated_at": "2026-07-16T08:02:00Z",
    }
    responses = {
        relay.workflow_runs_path(config, "pull_request", page=1): {"workflow_runs": [run_803, run_802]},
        relay.workflow_runs_path(config, "pull_request", page=2): {"workflow_runs": [run_801]},
        relay.workflow_runs_path(config, "push", page=1): {"workflow_runs": []},
        relay.github_path(config, "/pulls/21"): _pull(21),
        relay.github_path(config, "/pulls/22"): _pull(22),
        relay.github_path(config, "/pulls/23"): _pull(23),
    }
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        first_github = FakeGitHub(responses)
        assert relay.discover_notifications(config, first_github, store) == (3, 0)
        assert relay.workflow_runs_path(config, "pull_request", page=2) in first_github.requests
        watermark = store.get_watermark(config, "pull_request")
        assert watermark is not None
        assert watermark.run_id == 803

        second_responses = {
            relay.workflow_runs_path(config, "pull_request", page=1): {"workflow_runs": [run_803, run_802]},
            relay.workflow_runs_path(config, "pull_request", page=2): {"workflow_runs": [run_801]},
            relay.workflow_runs_path(config, "push", page=1): {"workflow_runs": []},
        }
        second_github = FakeGitHub(second_responses)
        assert relay.discover_notifications(config, second_github, store) == (0, 0)
        assert relay.workflow_runs_path(config, "pull_request", page=2) in second_github.requests
    finally:
        store.close()


def test_discovery_scans_past_a_watermarked_page_for_a_late_completed_run(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), run_limit=2)
    page_one = [
        {
            **_run(
                903,
                event="pull_request",
                sha=PR_SHA,
                conclusion="success",
                pull_number=31,
            ),
            "updated_at": "2026-07-16T08:09:00Z",
        },
        {
            **_run(
                902,
                event="pull_request",
                sha=PR_SHA,
                conclusion="success",
                pull_number=30,
            ),
            "updated_at": "2026-07-16T08:08:00Z",
        },
    ]
    late_completed = {
        **_run(
            801,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=24,
        ),
        "updated_at": "2026-07-16T08:11:00Z",
    }
    responses = {
        relay.workflow_runs_path(config, "pull_request", page=1): {"workflow_runs": page_one},
        relay.workflow_runs_path(config, "pull_request", page=2): {"workflow_runs": [late_completed]},
        relay.workflow_runs_path(config, "push", page=1): {"workflow_runs": []},
        relay.github_path(config, "/pulls/24"): _pull(24),
    }
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    store.set_watermark(
        config,
        "pull_request",
        relay.RunWatermark(
            updated_at=datetime(2026, 7, 16, 8, 10, tzinfo=timezone.utc),
            run_id=900,
            attempt=1,
        ),
    )
    try:
        github = FakeGitHub(responses)
        assert relay.discover_notifications(config, github, store) == (1, 0)
        assert relay.workflow_runs_path(config, "pull_request", page=2) in github.requests
        assert [str(row["dedupe_key"]) for row in store.pending()] == ["github-run:wr5912/agent-gov:801:1:success"]
        watermark = store.get_watermark(config, "pull_request")
        assert watermark is not None
        assert watermark.updated_at == datetime(
            2026,
            7,
            16,
            8,
            11,
            tzinfo=timezone.utc,
        )
        assert watermark.run_id == 801
    finally:
        store.close()


def test_discovery_keeps_same_timestamp_boundary_for_late_lower_run_id(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    late_visible = {
        **_run(
            999,
            event="pull_request",
            sha=PR_SHA,
            conclusion="success",
            pull_number=25,
        ),
        "updated_at": "2026-07-16T08:10:00Z",
    }
    responses = {
        relay.workflow_runs_path(config, "pull_request", page=1): {
            "workflow_runs": [late_visible],
        },
        relay.workflow_runs_path(config, "push", page=1): {"workflow_runs": []},
        relay.github_path(config, "/pulls/25"): _pull(25),
    }
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    store.set_watermark(
        config,
        "pull_request",
        relay.RunWatermark(
            updated_at=datetime(2026, 7, 16, 8, 10, tzinfo=timezone.utc),
            run_id=1000,
            attempt=1,
        ),
    )
    try:
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (1, 0)
        assert [str(row["dedupe_key"]) for row in store.pending()] == ["github-run:wr5912/agent-gov:999:1:success"]
        assert relay.discover_notifications(config, FakeGitHub(responses), store) == (0, 0)
    finally:
        store.close()


def test_outbox_schema_has_only_pending_and_delivered_lifecycle_states(
    tmp_path: Path,
) -> None:
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            with store._connection:  # noqa: SLF001 - contract-level schema assertion
                store._connection.execute(  # noqa: SLF001
                    "INSERT INTO outbox(dedupe_key, payload, status, created_at, updated_at) VALUES('bad', '{}', 'failed', ?, ?)",
                    (relay.utc_now(), relay.utc_now()),
                )
    finally:
        store.close()
