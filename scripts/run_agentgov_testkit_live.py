#!/usr/bin/env python3
"""在真实容器与模型上验证 agentgov-testkit 公共调用链。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentgov_testkit import AgentInvocation, invoke_agent
from scripts.agentscope_live_acceptance_scenarios import (
    GENERIC_RUNTIME_CAPABILITY,
    ReviewedScenarioSet,
    Scenario,
)
from scripts.run_agentscope_live_acceptance import (
    TRACE_ID_PATTERN,
    LiveAcceptanceError,
    _read_env_file,
    _require_explicit_live_authorization,
    load_scenarios,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run agentgov-testkit against the real isolated AgentGov deployment.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--scenario-file", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    return parser.parse_args()


@dataclass(frozen=True)
class TestkitLiveReceipt:
    scenario_file_sha256: str
    scenario_id: str
    test_session_id: str
    commit_sha: str
    run_id: str
    session_id: str
    trace_id: str


def _success_scenario(reviewed: ReviewedScenarioSet) -> Scenario:
    scenario = next(
        (item for item in reviewed.scenarios if item.purpose == "success" and item.capability == GENERIC_RUNTIME_CAPABILITY),
        None,
    )
    if scenario is None:
        raise LiveAcceptanceError("真实 testkit 验收场景缺少 purpose=success 的 generic_runtime 输入")
    return scenario


def _create_test_session(*, api_base: str, api_key: str, agent_id: str) -> tuple[str, str]:
    with httpx.Client(trust_env=False) as client:
        response = client.post(
            f"{api_base}/api/agent-test-sessions",
            json={"agent_id": agent_id, "commit_sha": None, "change_set_id": None},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
    response.raise_for_status()
    payload = response.json()
    session_id = str(payload.get("test_session_id") or "")
    commit_sha = str(payload.get("commit_sha") or "")
    if not session_id or not commit_sha:
        raise LiveAcceptanceError("真实 testkit Session 缺少身份或发布提交")
    return session_id, commit_sha


def _validate_invocation(result: AgentInvocation, *, commit_sha: str) -> tuple[str, str, str]:
    if not result.text.strip() or result.agent_version_id != commit_sha:
        raise LiveAcceptanceError("真实 testkit 调用未返回当前发布版本的非空结果")
    trace_id = result.langfuse_trace_id or ""
    if result.run_id is None or result.session_id is None or not TRACE_ID_PATTERN.fullmatch(trace_id):
        raise LiveAcceptanceError("真实 testkit 调用缺少 run/session/trace 身份")
    return result.run_id, result.session_id, trace_id


def _delete_test_session(*, api_base: str, api_key: str, session_id: str) -> None:
    with httpx.Client(trust_env=False) as client:
        response = client.delete(
            f"{api_base}/api/agent-test-sessions/{session_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
    response.raise_for_status()


def _run_live(args: argparse.Namespace) -> TestkitLiveReceipt:
    env = _read_env_file(args.env_file.resolve())
    _require_explicit_live_authorization(env, require_trace_complete=True)
    reviewed = load_scenarios(args.scenario_file, expected_agent_id=args.agent_id)
    scenario = _success_scenario(reviewed)
    api_base = env["API_BASE"].rstrip("/")
    api_key = env["API_KEY"]
    session_id, commit_sha = _create_test_session(
        api_base=api_base,
        api_key=api_key,
        agent_id=args.agent_id,
    )
    try:
        result = invoke_agent(
            scenario.input_text,
            metadata={"scenario_id": scenario.scenario_id},
            api_base=api_base,
            api_key=api_key,
            test_session_id=session_id,
            timeout_seconds=args.timeout_seconds,
        )
        run_id, runtime_session_id, trace_id = _validate_invocation(result, commit_sha=commit_sha)
        return TestkitLiveReceipt(
            scenario_file_sha256=reviewed.sha256,
            scenario_id=scenario.scenario_id,
            test_session_id=session_id,
            commit_sha=commit_sha,
            run_id=run_id,
            session_id=runtime_session_id,
            trace_id=trace_id,
        )
    finally:
        _delete_test_session(api_base=api_base, api_key=api_key, session_id=session_id)


def main() -> int:
    try:
        receipt = _run_live(parse_args())
    except (KeyError, ValueError, httpx.HTTPError, LiveAcceptanceError) as exc:
        print(f"AGENTGOV_TESTKIT_LIVE_FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
