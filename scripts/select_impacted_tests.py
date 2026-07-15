#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from test_quality.impact import select_impacted_nodes
from test_quality.policy import load_quality_policy, selected_lane_nodes, validate_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select impacted pytest leaves; unknown changes fail closed to main-full.")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def changed_paths(base_ref: str, head_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", f"{base_ref}...{head_ref}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    args = parse_args()
    policy_path = (REPO_ROOT / args.policy).resolve() if not args.policy.is_absolute() else args.policy.resolve()
    policy = load_quality_policy(policy_path)
    validation = validate_quality_policy(policy, repo_root=REPO_ROOT)
    if validation.errors:
        for error in validation.errors:
            print(f"TEST_IMPACT_FAIL: {error}")
        return 1
    paths = changed_paths(args.base_ref, args.head_ref)
    selection = select_impacted_nodes(
        changed_paths=paths,
        policy=policy.impact,
        collection=validation.collection,
        eligible_nodes=selected_lane_nodes(validation, policy.impact.unknown_change_lane),
    )
    output = (REPO_ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "mode": selection.mode,
                "changed_paths": paths,
                "matched_rules": selection.matched_rules,
                "reasons": selection.reasons,
                "nodeids": selection.nodeids,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"TEST_IMPACT_OK: mode={selection.mode} changed={len(paths)} rules={len(selection.matched_rules)} leaves={len(selection.nodeids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
