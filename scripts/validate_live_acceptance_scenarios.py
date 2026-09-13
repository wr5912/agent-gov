#!/usr/bin/env python3
"""按仓库唯一 schema 校验并规范化外部真实验收场景。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_agentscope_live_acceptance import LiveAcceptanceError, Scenario, load_scenarios


def _scenario_payload(scenario: Scenario) -> dict[str, object]:
    payload: dict[str, object] = {
        "scenario_id": scenario.scenario_id,
        "purpose": scenario.purpose,
        "capability": scenario.capability,
        "input": scenario.input_text,
        "source_ref": scenario.source_ref,
        "reviewed_by": scenario.reviewed_by,
        "reviewed_at": scenario.reviewed_at,
    }
    if scenario.feedback_comment is not None:
        payload["feedback_comment"] = scenario.feedback_comment
    if scenario.acceptance is not None:
        payload["acceptance"] = {
            "allowed_target_paths": list(scenario.acceptance.allowed_target_paths),
            "required_test_literals": list(scenario.acceptance.required_test_literals),
            "required_code_fragments": list(scenario.acceptance.required_code_fragments),
        }
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate an operator-reviewed AgentGov live acceptance scenario file.")
    parser.add_argument("--scenario-file", type=Path, required=True)
    parser.add_argument("--expected-agent-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reviewed = load_scenarios(args.scenario_file, expected_agent_id=args.expected_agent_id)
    except LiveAcceptanceError as exc:
        print(f"LIVE_ACCEPTANCE_SCENARIO_FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "agent_id": reviewed.agent_id,
                "scenario_file_sha256": reviewed.sha256,
                "scenarios": [_scenario_payload(item) for item in reviewed.scenarios],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
