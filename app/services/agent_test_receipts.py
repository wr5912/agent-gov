"""既有测试回执的矛盾检查，不补充历史未记录的报告字段。"""

from __future__ import annotations

from collections.abc import Sequence


def recorded_report_conflicts(
    report: object,
    *,
    nodeids: Sequence[str],
    test_run_id: str,
    commit_sha: str,
) -> bool:
    if not isinstance(report, dict):
        return report is not None
    if "exit_code" in report and report["exit_code"] is not None and (type(report["exit_code"]) is not int or report["exit_code"] != 0):
        return True
    if "items" in report:
        items = report["items"]
        if not isinstance(items, list) or any(not isinstance(item, dict) or item.get("outcome") != "passed" for item in items):
            return True
        reported_ids = [item.get("nodeid") for item in items]
        if any(not isinstance(nodeid, str) for nodeid in reported_ids) or sorted(reported_ids) != sorted(nodeids):
            return True
        for item in items:
            phases = item.get("phase_outcomes")
            if item.get("phase", "call") != "call" or (isinstance(phases, dict) and any(value != "passed" for value in phases.values())):
                return True
    invocations = report.get("invocations", [])
    if not isinstance(invocations, list):
        return True
    for invocation in invocations:
        if not isinstance(invocation, dict):
            return True
        if (
            invocation.get("errors", [])
            or invocation.get("test_run_id", test_run_id) != test_run_id
            or invocation.get("agent_version_id", commit_sha) != commit_sha
        ):
            return True
    return False
