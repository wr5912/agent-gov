"""回归测试资产物化与同一 Agent 发布之间的稳定锁竞态。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest
from app.routers.error_handlers import register_error_handlers
from app.routers.improvement_execution import create_improvement_execution_router
from app.runtime.agent_git_store import AgentGitError
from app.runtime.errors import ConflictError
from app.services import agent_release_workflows
from app.services.agent_governance import AgentGovernanceError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from improvement_execution_test_support import _materialization_service


class _Lease:
    def __enter__(self) -> _Lease:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def assert_active(self) -> None:
        return None

    def check(self) -> None:
        return None


class _Maintenance:
    def lease(self, **_kwargs: object) -> _Lease:
        return _Lease()


def _enable_publication(gov: Any) -> None:
    gov.version_maintenance = _Maintenance()
    gov._normalize_agent_id = lambda agent_id: str(agent_id or "")


def _thread_call(
    action: Callable[[], Any],
    *,
    results: list[Any],
    errors: list[BaseException],
    done: threading.Event | None = None,
) -> None:
    try:
        results.append(action())
    except BaseException as exc:  # noqa: BLE001 - tests must surface worker-thread failures.
        errors.append(exc)
    finally:
        if done is not None:
            done.set()


def _publish(gov: Any) -> dict[str, Any]:
    return agent_release_workflows.publish_change_set(
        gov,
        "agc-tests",
        operator="release-test",
        tag_name=None,
        note=None,
        force=False,
    )


def test_publisher_first_blocks_materializer_then_fresh_terminal_fence_rejects(tmp_path, monkeypatch) -> None:
    worktree, gov, _content, service = _materialization_service(tmp_path)
    _enable_publication(gov)
    reserved = threading.Event()
    release = threading.Event()
    material_done = threading.Event()
    publish_results: list[Any] = []
    publish_errors: list[BaseException] = []
    material_results: list[Any] = []
    material_errors: list[BaseException] = []

    def reserve_while_locked(*_args: object, **_kwargs: object) -> dict[str, str]:
        assert gov.store.guard_depth == 1
        gov.change_sets["agc-tests"]["status"] = "publishing"
        reserved.set()
        assert release.wait(2)
        return {"status": "publishing"}

    monkeypatch.setattr(agent_release_workflows, "_publish_change_set_locked", reserve_while_locked)
    publisher = threading.Thread(target=_thread_call, args=(lambda: _publish(gov),), kwargs={"results": publish_results, "errors": publish_errors})
    publisher.start()
    assert reserved.wait(2)
    materializer = threading.Thread(
        target=_thread_call,
        args=(lambda: service.materialize_regression_tests("imp-1"),),
        kwargs={"results": material_results, "errors": material_errors, "done": material_done},
    )
    materializer.start()
    assert not material_done.wait(0.1)
    release.set()
    publisher.join(2)
    materializer.join(2)

    assert not publisher.is_alive() and not materializer.is_alive()
    assert publish_results == [{"status": "publishing"}] and not publish_errors
    assert not material_results and len(material_errors) == 1
    assert isinstance(material_errors[0], ConflictError)
    assert not (worktree / "tests").exists()


def test_publish_stable_guard_failure_precedes_reservation_and_maps_conflict(tmp_path, monkeypatch) -> None:
    _worktree, gov, _content, _service = _materialization_service(tmp_path)
    _enable_publication(gov)
    gov.store.guard_entry_error = AgentGitError("Business Agent repository is no longer mutable")
    locked_called = False

    def forbidden_locked(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal locked_called
        locked_called = True
        return {}

    monkeypatch.setattr(agent_release_workflows, "_publish_change_set_locked", forbidden_locked)
    with pytest.raises(AgentGovernanceError, match="no longer mutable") as raised:
        _publish(gov)

    assert raised.value.status_code == 409
    assert locked_called is False
    assert gov.change_sets["agc-tests"]["status"] == "candidate_committed"


def test_confirm_regression_projects_guard_failure_as_http_409_without_side_effects(tmp_path) -> None:
    worktree, gov, content, service = _materialization_service(tmp_path)
    gov.store.guard_entry_error = AgentGitError("Business Agent repository is no longer mutable")
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_improvement_execution_router(
            improvement_store=service._improvements,
            content_store=content,
            governor_service=None,
            execution_service=service,
            agent_testing=None,
            require_api_key=lambda: None,
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/improvements/imp-1/regression-test-design/confirm")

    execution = content.get_execution("imp-1")
    design = content.get_regression_test_design("imp-1")
    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"
    assert execution is not None and execution.applied_agent_version_id == "cand-sha"
    assert design is not None and design.status == "draft"
    assert gov.change_sets["agc-tests"]["candidate_commit_sha"] == "cand-sha"
    assert not (worktree / "tests").exists()


def test_materializer_first_holds_stable_lock_through_mark_and_rebind(tmp_path, monkeypatch) -> None:
    _worktree, gov, _content, service = _materialization_service(tmp_path)
    _enable_publication(gov)
    mark_entered = threading.Event()
    allow_mark = threading.Event()
    publisher_entered = threading.Event()
    observed_candidates: list[str] = []
    material_results: list[Any] = []
    material_errors: list[BaseException] = []
    publish_results: list[Any] = []
    publish_errors: list[BaseException] = []
    original_mark = gov.mark_candidate_committed

    def delayed_mark(*args: object, **kwargs: object) -> dict[str, Any]:
        assert gov.store.guard_depth == 1
        mark_entered.set()
        assert allow_mark.wait(2)
        return original_mark(*args, **kwargs)

    def observe_after_lock(*_args: object, **_kwargs: object) -> dict[str, str]:
        assert gov.store.guard_depth == 1
        observed_candidates.append(str(gov.change_sets["agc-tests"]["candidate_commit_sha"]))
        publisher_entered.set()
        return {"status": "reserved"}

    monkeypatch.setattr(gov, "mark_candidate_committed", delayed_mark)
    monkeypatch.setattr(agent_release_workflows, "_publish_change_set_locked", observe_after_lock)
    materializer = threading.Thread(
        target=_thread_call,
        args=(lambda: service.materialize_regression_tests("imp-1"),),
        kwargs={"results": material_results, "errors": material_errors},
    )
    materializer.start()
    assert mark_entered.wait(2)
    publisher = threading.Thread(target=_thread_call, args=(lambda: _publish(gov),), kwargs={"results": publish_results, "errors": publish_errors})
    publisher.start()
    assert not publisher_entered.wait(0.1)
    allow_mark.set()
    materializer.join(2)
    publisher.join(2)

    assert not materializer.is_alive() and not publisher.is_alive()
    assert not material_errors and not publish_errors
    assert material_results[0]["candidate_commit_sha"] == "cand-tests-sha"
    assert publish_results == [{"status": "reserved"}]
    assert observed_candidates == ["cand-tests-sha"]


def test_commit_then_mark_failure_compensates_under_lock_and_retry_succeeds(tmp_path, monkeypatch) -> None:
    worktree, gov, content, service = _materialization_service(tmp_path)
    original_mark = gov.mark_candidate_committed
    original_reset = gov.store.reset_worktree
    failures = 0
    reset_depths: list[int] = []

    def fail_new_candidate_once(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal failures
        candidate = str(kwargs.get("candidate_commit_sha") or "")
        if candidate == "cand-tests-sha" and failures == 0:
            failures += 1
            assert gov.store.guard_depth == 1
            raise RuntimeError("candidate mark interrupted")
        return original_mark(*args, **kwargs)

    def observe_reset(*args: object, **kwargs: object) -> None:
        reset_depths.append(gov.store.guard_depth)
        original_reset(*args, **kwargs)

    monkeypatch.setattr(gov, "mark_candidate_committed", fail_new_candidate_once)
    monkeypatch.setattr(gov.store, "reset_worktree", observe_reset)
    with pytest.raises(RuntimeError, match="candidate mark interrupted"):
        service.materialize_regression_tests("imp-1")

    execution = content.get_execution("imp-1")
    assert execution is not None and execution.applied_agent_version_id == "cand-sha"
    assert gov.change_sets["agc-tests"]["candidate_commit_sha"] == "cand-sha"
    assert gov.store.head == "cand-sha" and reset_depths == [1]
    assert not (worktree / "tests").exists()

    retried = service.materialize_regression_tests("imp-1")
    assert retried["candidate_commit_sha"] == "cand-tests-sha"
    assert (worktree / "tests" / "README.md").is_file()
