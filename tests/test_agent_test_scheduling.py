from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.agent_testing.router import create_agent_testing_router
from app.agent_testing.runner import FIXED_PYTEST_COMMAND
from app.agent_testing.schedule import AgentTestScheduleService, AgentTestScheduleStore, validate_test_schedule
from app.agent_testing.service import AgentTestingError, AgentTestingService
from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatResponse
from app.runtime.state_machines import StateTransitionError, validate_transition
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _service(tmp_path: Path) -> tuple[AgentTestingService, AgentTestScheduleService, AgentTestingStore, GitAgentVersionStore, list[str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("CLAUDE.md").write_text("# scheduled Agent\n", encoding="utf-8")
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("README.md").write_text("# tests\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(
        "class TestAgent:\n"
        "    def helper(self):\n"
        "        return True\n\n"
        "    def test_answer(self):\n"
        "        assert self.helper()\n\n"
        "    async def test_async_answer_method(self):\n"
        "        assert True\n\n"
        "class Helper:\n"
        "    def test_not_collected(self):\n"
        "        assert True\n\n"
        "async def test_async_answer():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    git_store = GitAgentVersionStore(
        repository_dir=workspace,
        worktrees_dir=tmp_path / "worktrees",
        releases_dir=tmp_path / "releases",
    )
    git_store.ensure_bootstrap()
    session_factory = make_session_factory(tmp_path / "runtime.sqlite3")
    testing_store = AgentTestingStore(session_factory)
    schedule_store = AgentTestScheduleStore(session_factory)

    async def unused_run_candidate(*_args, **_kwargs) -> ChatResponse:
        raise AssertionError("must not invoke an Agent while scheduling pytest")

    service = AgentTestingService(
        store=testing_store,
        store_for=lambda agent_id: git_store if agent_id == "agent-a" else (_ for _ in ()).throw(AssertionError(agent_id)),
        agent_exists=lambda agent_id: agent_id == "agent-a",
        get_change_set=lambda _change_set_id: None,
        run_candidate=unused_run_candidate,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://127.0.0.1:8000",
        api_key=None,
        run_timeout_seconds=30,
        schedule_reader=schedule_store.get_schedule,
    )
    enqueued: list[str] = []
    service.runner.enqueue = enqueued.append  # type: ignore[method-assign]
    schedules = AgentTestScheduleService(
        store=schedule_store,
        testing=service,
        agent_exists=lambda agent_id: agent_id == "agent-a",
        agent_status=lambda _agent_id: "active",
    )
    return service, schedules, testing_store, git_store, enqueued


def test_schedule_validation_requires_five_fields_iana_timezone_and_fifteen_minutes() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    expression, timezone_name, next_run = validate_test_schedule("*/15 * * * *", "Asia/Shanghai", now=now)
    assert expression == "*/15 * * * *"
    assert timezone_name == "Asia/Shanghai"
    assert next_run > now

    with pytest.raises(ValueError, match="five-field"):
        validate_test_schedule("0 0 1 1 * 2027", "UTC", now=now)
    with pytest.raises(ValueError, match="15 minutes"):
        validate_test_schedule("*/10 * * * *", "UTC", now=now)
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        validate_test_schedule("0 2 * * *", "Mars/Olympus", now=now)


def test_schedule_crud_keeps_one_strategy_per_agent(tmp_path: Path) -> None:
    service, schedules, _testing_store, _git_store, _enqueued = _service(tmp_path)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        default = schedules.read_schedule("agent-a")
        assert default["schedule_id"] is None
        assert default["enabled"] is False

        created = schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="0 2 * * *",
            timezone_name="Asia/Shanghai",
            now=now,
        )
        updated = schedules.update_schedule(
            "agent-a",
            enabled=False,
            cron_expression="30 3 * * 1",
            timezone_name="UTC",
            now=now,
        )
        assert updated["schedule_id"] == created["schedule_id"]
        assert updated["next_run_at"] is None
        assert updated["cron_expression"] == "30 3 * * 1"
        schedule_items = schedules.store.schedules_for_agents(["agent-a"])
        assert schedule_items[0]["schedule_id"] == created["schedule_id"]
    finally:
        service.close()


def test_due_schedule_pins_current_commit_and_coalesces_missed_windows(tmp_path: Path) -> None:
    service, schedules, testing_store, git_store, enqueued = _service(tmp_path)
    configured_at = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    try:
        schedule = schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="15 * * * *",
            timezone_name="UTC",
            now=configured_at,
        )
        # Simulate a restart many windows later: exactly one old occurrence is emitted,
        # then next_run_at jumps to the first future window.
        assert schedules.tick(now=configured_at + timedelta(days=1)) == 1
        runs = testing_store.list_runs(agent_id="agent-a")
        assert len(runs) == 1
        assert runs[0]["commit_sha"] == git_store.current_commit_sha()
        assert runs[0]["change_set_id"] is None
        assert runs[0]["source"] == "scheduled"
        assert runs[0]["schedule_id"] == schedule["schedule_id"]
        assert runs[0]["scheduled_for"] == schedule["next_run_at"]
        assert enqueued == [runs[0]["test_run_id"]]
        assert schedules.tick(now=configured_at + timedelta(days=1)) == 0

        events = schedules.list_events("agent-a", limit=10)
        assert [event["status"] for event in events] == ["enqueued"]
        assert events[0]["test_run_id"] == runs[0]["test_run_id"]
    finally:
        service.close()


def test_schedule_coalesces_active_agent_commit_and_skips_inactive_agent(tmp_path: Path) -> None:
    service, schedules, testing_store, git_store, _enqueued = _service(tmp_path)
    now = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    try:
        active = testing_store.create_run(
            agent_id="agent-a",
            commit_sha=str(git_store.current_commit_sha()),
            change_set_id="agc-pending",
            source="release_check",
            command=FIXED_PYTEST_COMMAND,
            suite={},
            suite_digest=None,
        )
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="15 * * * *",
            timezone_name="UTC",
            now=now,
        )
        assert schedules.tick(now=now + timedelta(minutes=15)) == 1
        event = schedules.list_events("agent-a", limit=1)[0]
        assert event["status"] == "coalesced"
        assert event["test_run_id"] == active["test_run_id"]

        inactive = AgentTestScheduleService(
            store=schedules.store,
            testing=service,
            agent_exists=lambda agent_id: agent_id == "agent-a",
            agent_status=lambda _agent_id: "deprecated",
        )
        assert inactive.tick(now=now + timedelta(hours=1, minutes=15)) == 1
        assert inactive.list_events("agent-a", limit=1)[0]["status"] == "skipped"
        assert inactive.read_schedule("agent-a")["enabled"] is True
        assert len(testing_store.list_runs(agent_id="agent-a")) == 1
    finally:
        service.close()


def test_scheduler_tick_drains_a_durable_pending_event_without_a_new_occurrence(tmp_path: Path) -> None:
    service, schedules, testing_store, _git_store, enqueued = _service(tmp_path)
    now = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    try:
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=now,
        )
        claimed = schedules.store.claim_due_events(now=now + timedelta(minutes=15))
        assert len(claimed) == 1
        assert schedules.store.pending_events()[0]["status"] == "pending"

        assert schedules.tick(now=now + timedelta(minutes=16)) == 0
        event = schedules.list_events("agent-a", limit=1)[0]
        assert event["status"] == "enqueued"
        assert event["test_run_id"] == testing_store.list_runs(agent_id="agent-a")[0]["test_run_id"]
        assert enqueued == [event["test_run_id"]]
    finally:
        service.close()


def test_missing_and_archived_agents_disable_future_schedule_windows(tmp_path: Path) -> None:
    service, schedules, _testing_store, _git_store, _enqueued = _service(tmp_path)
    now = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    try:
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=now,
        )
        missing = AgentTestScheduleService(
            store=schedules.store,
            testing=service,
            agent_exists=lambda _agent_id: False,
            agent_status=lambda _agent_id: None,
        )
        assert missing.tick(now=now + timedelta(minutes=15)) == 1
        missing_event = schedules.store.list_events(agent_id="agent-a", limit=1)[0]
        assert missing_event["status"] == "skipped"
        assert missing_event["detail"]["schedule_disabled"] is True
        assert schedules.store.get_schedule("agent-a")["enabled"] is False

        later = now + timedelta(hours=1)
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=later,
        )
        archived = AgentTestScheduleService(
            store=schedules.store,
            testing=service,
            agent_exists=lambda _agent_id: True,
            agent_status=lambda _agent_id: "archived",
        )
        assert archived.tick(now=later + timedelta(minutes=15)) == 1
        archived_event = archived.list_events("agent-a", limit=1)[0]
        assert archived_event["status"] == "skipped"
        assert archived_event["detail"]["schedule_disabled"] is True
        assert archived.read_schedule("agent-a")["enabled"] is False

        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="0 2 * * *",
            timezone_name="UTC",
            now=later,
        )
        assert schedules.disable_agent_schedule("agent-a") is True
        assert schedules.read_schedule("agent-a")["enabled"] is False
    finally:
        service.close()


def test_test_asset_file_and_paginated_history_are_read_only_projections(tmp_path: Path) -> None:
    service, _schedules, testing_store, git_store, _enqueued = _service(tmp_path)
    try:
        source = service.get_suite_file("agent-a", path="tests/test_agent.py")
        assert source["commit_sha"] == git_store.current_commit_sha()
        assert source["line_count"] == 16
        symbols = source["symbols"]
        assert isinstance(symbols, list)
        assert [(item["kind"], item["name"], item["qualified_name"], item["line"]) for item in symbols if isinstance(item, dict)] == [
            ("class", "TestAgent", "TestAgent", 1),
            ("function", "test_answer", "TestAgent.test_answer", 5),
            ("async_function", "test_async_answer_method", "TestAgent.test_async_answer_method", 8),
            ("class", "Helper", "Helper", 11),
            ("async_function", "test_async_answer", "test_async_answer", 15),
        ]
        with pytest.raises(AgentTestingError) as traversal:
            service.get_suite_file("agent-a", path="tests/../.env")
        assert traversal.value.error_code == "AGENT_TEST_FILE_PATH_INVALID"

        for index, run_source in enumerate(("manual", "scheduled", "manual")):
            run = testing_store.create_run(
                agent_id="agent-a",
                commit_sha=f"{index + 1:040x}",
                change_set_id=None,
                source=run_source,
                command=FIXED_PYTEST_COMMAND,
                suite={},
                suite_digest=None,
            )
            assert testing_store.claim_run(str(run["test_run_id"])) is not None
            testing_store.finish_run(str(run["test_run_id"]), status="passed", report={"exit_code": 0}, items=[], stdout="", stderr="")

        first = service.list_run_history(
            agent_id="agent-a",
            status=None,
            source=None,
            commit_sha=None,
            cursor=None,
            limit=2,
        )
        second = service.list_run_history(
            agent_id="agent-a",
            status=None,
            source=None,
            commit_sha=None,
            cursor=str(first["next_cursor"]),
            limit=2,
        )
        assert len(first["items"]) == 2
        assert len(second["items"]) == 1
        scheduled = service.list_run_history(
            agent_id="agent-a",
            status="passed",
            source="scheduled",
            commit_sha=None,
            cursor=None,
            limit=10,
        )
        scheduled_items = scheduled["items"]
        assert isinstance(scheduled_items, list)
        assert [item["source"] for item in scheduled_items if isinstance(item, dict)] == ["scheduled"]
        latest = testing_store.latest_run_summaries(["agent-a"])
        assert latest[0]["source"] == "manual"
    finally:
        service.close()


def test_schedule_event_state_machine_rejects_terminal_reopen() -> None:
    validate_transition("agent_test_schedule_event", "pending", "enqueued")
    with pytest.raises(StateTransitionError):
        validate_transition("agent_test_schedule_event", "enqueued", "pending")


def test_history_and_schedule_routes_keep_backend_owned_fields_out_of_requests() -> None:
    class FakeTesting:
        def list_run_history(self, **kwargs):
            assert kwargs["agent_id"] == "agent-a"
            return {"items": [], "next_cursor": None}

    class FakeSchedules:
        def update_schedule(self, agent_id: str, **kwargs):
            assert agent_id == "agent-a"
            return {
                "schedule_id": "atsc-a",
                "agent_id": agent_id,
                "enabled": kwargs["enabled"],
                "cron_expression": kwargs["cron_expression"],
                "timezone": kwargs["timezone_name"],
                "next_run_at": "2026-07-21T02:00:00+00:00",
            }

    app = FastAPI()
    app.include_router(
        create_agent_testing_router(
            service=FakeTesting(),  # type: ignore[arg-type]
            schedule_service=FakeSchedules(),  # type: ignore[arg-type]
            require_api_key=lambda: None,
        )
    )
    with TestClient(app) as client:
        history = client.get("/api/agent-test-runs/history", params={"agent_id": "agent-a"})
        updated = client.put(
            "/api/agent-registry/agent-a/test-schedule",
            json={"enabled": True, "cron_expression": "0 2 * * *", "timezone": "UTC"},
        )
        hostile = client.put(
            "/api/agent-registry/agent-a/test-schedule",
            json={
                "enabled": True,
                "cron_expression": "0 2 * * *",
                "timezone": "UTC",
                "next_run_at": "2000-01-01T00:00:00Z",
                "test_run_id": "forged",
            },
        )

    assert history.status_code == 200
    assert history.json() == {"items": [], "next_cursor": None}
    assert updated.status_code == 200
    assert updated.json()["schedule_id"] == "atsc-a"
    assert hostile.status_code == 422
