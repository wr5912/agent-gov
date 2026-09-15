"""校验 pytest 全量采集与发布测试的服务端真实调用证据。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_PHASES = frozenset({"setup", "call", "teardown"})


def passed_report_errors(
    report: Mapping[str, object],
    *,
    actual_exit_code: int | None,
    release_check: bool,
    attested_invocations: Sequence[Mapping[str, object]],
    test_run_id: str,
    commit_sha: str,
) -> list[str]:
    """只有完整收集、三阶段通过的真实报告才能产生 passed。"""

    errors: list[str] = []
    reported_exit = report.get("exit_code")
    if isinstance(reported_exit, bool) or reported_exit != actual_exit_code or actual_exit_code != 0:
        errors.append("pytest report exit code does not match the completed process")

    collected = report.get("collected_nodeids")
    if not isinstance(collected, list) or not collected or any(not isinstance(item, str) or not item for item in collected):
        errors.append("pytest report has no complete collected leaf list")
        collected_ids: list[str] = []
    else:
        collected_ids = collected
        if len(set(collected_ids)) != len(collected_ids):
            errors.append("pytest report contains duplicate collected leaves")

    items = report.get("items")
    item_ids: list[str] = []
    if not isinstance(items, list) or not items:
        errors.append("pytest report has no leaf results")
    else:
        for item in items:
            if not isinstance(item, dict):
                errors.append("pytest report contains a non-object leaf result")
                continue
            nodeid = item.get("nodeid")
            if not isinstance(nodeid, str) or not nodeid:
                errors.append("pytest report contains a leaf without nodeid")
                continue
            item_ids.append(nodeid)
            phases = item.get("phase_outcomes")
            if item.get("outcome") != "passed" or item.get("phase") != "call":
                errors.append(f"pytest leaf did not pass: {nodeid}")
            if not isinstance(phases, dict) or set(phases) != _PHASES or any(value != "passed" for value in phases.values()):
                errors.append(f"pytest leaf has incomplete or non-passing phases: {nodeid}")
        if len(set(item_ids)) != len(item_ids):
            errors.append("pytest report contains duplicate leaf results")
    if set(item_ids) != set(collected_ids) or len(item_ids) != len(collected_ids):
        errors.append("pytest report leaf results do not cover the collected suite")

    if release_check:
        errors.extend(attested_release_invocation_errors(attested_invocations, test_run_id=test_run_id, commit_sha=commit_sha))
    return errors


def attested_release_invocation_errors(
    invocations: Sequence[Mapping[str, object]],
    *,
    test_run_id: str,
    commit_sha: str,
) -> list[str]:
    if not invocations:
        return ["release test has no server-attested Agent invocation"]
    errors: list[str] = []
    run_ids: set[str] = set()
    for invocation in invocations:
        run_id = invocation.get("run_id")
        if (
            invocation.get("test_run_id") != test_run_id
            or invocation.get("agent_version_id") != commit_sha
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(invocation.get("session_id"), str)
            or not invocation.get("session_id")
            or invocation.get("errors") != []
            or run_id in run_ids
        ):
            errors.append("release test has invalid server-attested Agent invocation identity or result")
            break
        run_ids.add(run_id)
    return errors
