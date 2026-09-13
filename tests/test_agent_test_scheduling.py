from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.agent_testing.runner import FIXED_PYTEST_COMMAND
from app.agent_testing.schedule import validate_test_schedule
from app.agent_testing.schemas import AgentTestScheduleUpdateRequest
from app.agent_testing.service import AgentTestingError
from app.agent_testing.store import AgentTestingStore
from app.runtime.state_machines import StateTransitionError, validate_transition
from pydantic import ValidationError

from app_test_utils import load_test_app


def _service(tmp_path: Path, process_environment):
    module = load_test_app(
        process_environment,
        tmp_path,
        extra_agent_ids=("agent-a", "agent-missing"),
    )
    workspace = module.settings.data_dir / "business-agents" / "agent-a" / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    tests_dir.joinpath("README.md").write_text("# tests\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(
        "from pathlib import Path\n\n"
        "ROOT = Path(__file__).parents[1]\n\n"
        "class TestAgent:\n"
        "    def test_answer(self):\n"
        "        assert (ROOT / 'AGENT.md').is_file()\n\n"
        "    async def verify_async_answer_method(self):\n"
        "        assert (ROOT / 'agent.yaml').is_file()\n\n"
        "class Helper:\n"
        "    def test_not_collected(self):\n"
        "        assert (ROOT / 'tests' / 'README.md').is_file()\n\n"
        "async def verify_async_answer():\n"
        "    assert ROOT.is_dir()\n",
        encoding="utf-8",
    )
    git_store = module.agent_governance._store_for("agent-a")
    return (
        module.agent_testing_service,
        module.agent_test_schedule_service,
        module.agent_testing_store,
        git_store,
        module.agent_registry_store,
    )


def _await_terminal(store: AgentTestingStore, test_run_id: str) -> dict:
    deadline = time.monotonic() + 30
    run = store.get_run(test_run_id)
    while run and run["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.05)
        run = store.get_run(test_run_id)
    assert run is not None and run["status"] not in {"queued", "running"}, run
    return run


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


def test_schedule_crud_keeps_one_strategy_per_agent(tmp_path: Path, process_environment) -> None:
    service, schedules, _testing_store, _git_store, _registry = _service(tmp_path, process_environment)
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


def test_due_schedule_pins_current_commit_and_coalesces_missed_windows(tmp_path: Path, process_environment) -> None:
    service, schedules, testing_store, git_store, _registry = _service(tmp_path, process_environment)
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
        assert _await_terminal(testing_store, str(runs[0]["test_run_id"]))["status"] == "passed"
        assert schedules.tick(now=configured_at + timedelta(days=1)) == 0

        events = schedules.list_events("agent-a", limit=10)
        assert [event["status"] for event in events] == ["enqueued"]
        assert events[0]["test_run_id"] == runs[0]["test_run_id"]
    finally:
        service.close()


def test_schedule_coalesces_active_agent_commit_and_skips_inactive_agent(tmp_path: Path, process_environment) -> None:
    service, schedules, testing_store, git_store, registry = _service(tmp_path, process_environment)
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

        registry.transition_business_agent("agent-a", status="deprecated")
        assert schedules.tick(now=now + timedelta(hours=1, minutes=15)) == 1
        assert schedules.list_events("agent-a", limit=1)[0]["status"] == "skipped"
        assert schedules.read_schedule("agent-a")["enabled"] is True
        assert len(testing_store.list_runs(agent_id="agent-a")) == 1
    finally:
        service.close()


def test_scheduler_tick_drains_a_durable_pending_event_without_a_new_occurrence(tmp_path: Path, process_environment) -> None:
    service, schedules, testing_store, _git_store, _registry = _service(tmp_path, process_environment)
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
        assert _await_terminal(testing_store, str(event["test_run_id"]))["status"] == "passed"
    finally:
        service.close()


def test_missing_and_archived_agents_disable_future_schedule_windows(tmp_path: Path, process_environment) -> None:
    service, schedules, _testing_store, _git_store, registry = _service(tmp_path, process_environment)
    now = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    try:
        schedules.update_schedule(
            "agent-missing",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=now,
        )
        registry.delete_business_agent("agent-missing")
        assert schedules.tick(now=now + timedelta(minutes=15)) == 1
        missing_event = schedules.store.list_events(agent_id="agent-missing", limit=1)[0]
        assert missing_event["status"] == "skipped"
        assert missing_event["detail"]["schedule_disabled"] is True
        assert schedules.store.get_schedule("agent-missing")["enabled"] is False

        later = now + timedelta(hours=1)
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=later,
        )
        registry.transition_business_agent("agent-a", status="archived")
        assert schedules.tick(now=later + timedelta(minutes=15)) == 1
        archived_event = schedules.list_events("agent-a", limit=1)[0]
        assert archived_event["status"] == "skipped"
        assert archived_event["detail"]["schedule_disabled"] is True
        assert schedules.read_schedule("agent-a")["enabled"] is False

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


def test_test_asset_file_and_paginated_history_are_read_only_projections(tmp_path: Path, process_environment) -> None:
    service, schedules, testing_store, git_store, _registry = _service(tmp_path, process_environment)
    try:
        source = service.get_suite_file("agent-a", path="tests/test_agent.py")
        assert source["commit_sha"] == git_store.current_commit_sha()
        assert source["line_count"] == 17
        symbols = source["symbols"]
        assert isinstance(symbols, list)
        assert [(item["kind"], item["name"], item["qualified_name"], item["line"]) for item in symbols if isinstance(item, dict)] == [
            ("class", "TestAgent", "TestAgent", 5),
            ("function", "test_answer", "TestAgent.test_answer", 6),
            ("class", "Helper", "Helper", 12),
            ("async_function", "verify_async_answer", "verify_async_answer", 16),
        ]
        with pytest.raises(AgentTestingError) as traversal:
            service.get_suite_file("agent-a", path="tests/../.env")
        assert traversal.value.error_code == "AGENT_TEST_FILE_PATH_INVALID"

        first_manual = service.create_run(
            agent_id="agent-a",
            commit_sha=None,
            change_set_id=None,
            source="manual",
        )
        assert _await_terminal(testing_store, str(first_manual["test_run_id"]))["status"] == "passed"
        scheduled_at = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
        schedules.update_schedule(
            "agent-a",
            enabled=True,
            cron_expression="*/15 * * * *",
            timezone_name="UTC",
            now=scheduled_at,
        )
        assert schedules.tick(now=scheduled_at + timedelta(minutes=15)) == 1
        scheduled_run = testing_store.list_runs(agent_id="agent-a")[0]
        assert scheduled_run["source"] == "scheduled"
        assert _await_terminal(testing_store, str(scheduled_run["test_run_id"]))["status"] == "passed"
        last_manual = service.create_run(
            agent_id="agent-a",
            commit_sha=None,
            change_set_id=None,
            source="manual",
        )
        assert _await_terminal(testing_store, str(last_manual["test_run_id"]))["status"] == "passed"

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


def test_schedule_request_keeps_backend_owned_fields_out_of_writable_contract() -> None:
    request = AgentTestScheduleUpdateRequest.model_validate({"enabled": True, "cron_expression": "0 2 * * *", "timezone": "UTC"})
    assert request.model_dump(mode="json") == {
        "enabled": True,
        "cron_expression": "0 2 * * *",
        "timezone": "UTC",
    }

    with pytest.raises(ValidationError):
        AgentTestScheduleUpdateRequest.model_validate(
            {
                "enabled": True,
                "cron_expression": "0 2 * * *",
                "timezone": "UTC",
                "next_run_at": "2000-01-01T00:00:00Z",
                "test_run_id": "forged",
            }
        )
