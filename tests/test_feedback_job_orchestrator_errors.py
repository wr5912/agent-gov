import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from app.runtime.agent_job_errors import AGENT_AUTH_REQUIRED, AgentAuthenticationRequiredError
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.json_types import JsonObject
from app.runtime.schemas import FeedbackSignalCreateRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.stores.feedback_store import FeedbackStore

from feedback_store_test_utils import (
    _attribution_output,
    _create_approved_task_for_target,
    _create_batch_with_completed_attribution,
    _record_run,
    _settings,
)


def _store(tmp_path) -> tuple[FeedbackStore, ClaudeRuntime]:
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    return store, runtime


def _feedback_case_with_attribution(store: FeedbackStore) -> JsonObject:
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    return feedback_case


async def _raise(exc: Exception, **_: Any) -> JsonObject:
    raise exc


def _auth_required() -> AgentAuthenticationRequiredError:
    return AgentAuthenticationRequiredError(
        profile_name="governor",
        runtime_volume_mode="local-debug",
        settings_env_file="docker/.env.local-debug",
        missing=["MODEL_PROVIDER_API_KEY", "ANTHROPIC_API_KEY"],
    )


@pytest.mark.parametrize(
    ("exc_factory", "error_code"),
    [
        (lambda: asyncio.TimeoutError("agent timed out"), "AGENT_TIMEOUT"),
        (lambda: RuntimeError("agent crashed"), "AGENT_RUNTIME_ERROR"),
        (_auth_required, AGENT_AUTH_REQUIRED),
    ],
)
def test_attribution_orchestrator_maps_agent_errors(tmp_path, monkeypatch, exc_factory: Callable[[], Exception], error_code: str):
    store, runtime = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    monkeypatch.setattr(runtime, "_run_profile_json", lambda **kwargs: _raise(exc_factory(), **kwargs))

    job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"], force=True))

    assert job.status == "failed"
    assert job.error_json is not None
    assert job.error_json.error_code == error_code


@pytest.mark.parametrize(
    ("exc_factory", "error_code"),
    [
        (lambda: asyncio.TimeoutError("agent timed out"), "AGENT_TIMEOUT"),
        (lambda: RuntimeError("agent crashed"), "AGENT_RUNTIME_ERROR"),
    ],
)
def test_single_feedback_plan_orchestrator_maps_agent_errors(tmp_path, monkeypatch, exc_factory: Callable[[], Exception], error_code: str):
    store, runtime = _store(tmp_path)
    feedback_case = _feedback_case_with_attribution(store)
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    monkeypatch.setattr(runtime, "_run_profile_json", lambda **kwargs: _raise(exc_factory(), **kwargs))

    updated = asyncio.run(runtime.run_batch_optimization_plan(batch["batch_id"], force=True))

    assert updated.status == "needs_human_review"
    assert updated.optimization_plan_job is not None
    assert updated.optimization_plan_job.status == "failed"
    assert updated.optimization_plan_job.error_json is not None
    assert updated.optimization_plan_job.error_json.error_code == error_code


@pytest.mark.parametrize(
    ("exc_factory", "error_code"),
    [
        (lambda: asyncio.TimeoutError("agent timed out"), "AGENT_TIMEOUT"),
        (lambda: RuntimeError("agent crashed"), "AGENT_RUNTIME_ERROR"),
    ],
)
def test_batch_plan_orchestrator_maps_agent_errors(tmp_path, monkeypatch, exc_factory: Callable[[], Exception], error_code: str):
    store, runtime = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    monkeypatch.setattr(runtime, "_run_profile_json", lambda **kwargs: _raise(exc_factory(), **kwargs))

    updated = asyncio.run(runtime.run_batch_optimization_plan(batch["batch_id"], force=True))

    assert updated.status == "needs_human_review"
    assert updated.optimization_plan_job is not None
    assert updated.optimization_plan_job.status == "failed"
    assert updated.optimization_plan_job.error_json is not None
    assert updated.optimization_plan_job.error_json.error_code == error_code


def test_batch_plan_orchestrator_preserves_formatter_raw_output(tmp_path, monkeypatch):
    store, runtime = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    raw_output = {
        "_formatter": {"name": "dspy", "status": "failed", "candidate_count": 0},
        "raw_text": "proposal-generator 输出了自然语言方案。",
    }

    class FormatterFailure(RuntimeError):
        raw_output_json = raw_output

    monkeypatch.setattr(runtime, "_run_profile_json", lambda **kwargs: _raise(FormatterFailure("formatter failed"), **kwargs))

    updated = asyncio.run(runtime.run_batch_optimization_plan(batch["batch_id"], force=True))

    assert updated.optimization_plan_job is not None
    assert updated.optimization_plan_job.status == "failed"
    assert updated.optimization_plan_job.raw_output_json == raw_output
    assert updated.optimization_plan_error is not None
    assert updated.optimization_plan_error.error_code == "AGENT_RUNTIME_ERROR"


@pytest.mark.parametrize(
    ("exc_factory", "error_code"),
    [
        (lambda: asyncio.TimeoutError("agent timed out"), "AGENT_TIMEOUT"),
        (lambda: RuntimeError("agent crashed"), "AGENT_RUNTIME_ERROR"),
    ],
)
def test_execution_orchestrator_maps_agent_errors(tmp_path, monkeypatch, exc_factory: Callable[[], Exception], error_code: str):
    store, runtime = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    monkeypatch.setattr(runtime, "_run_profile_json", lambda **kwargs: _raise(exc_factory(), **kwargs))

    job = asyncio.run(runtime.run_execution_job(task["optimization_task_id"], force=True))

    assert job.status == "failed"
    assert job.error_json is not None
    assert job.error_json.error_code == error_code
