#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from test_quality.coverage import evaluate_coverage, load_coverage
from test_quality.evidence import validate_evidence
from test_quality.policy import load_quality_policy, selected_lane_nodes, validate_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the strict test portfolio policy and trusted test evidence.")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--coverage-json", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--lane", default="main-full")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--expected-sha")
    parser.add_argument("--skip-collection", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy_path = (REPO_ROOT / args.policy).resolve() if not args.policy.is_absolute() else args.policy.resolve()
    policy = load_quality_policy(policy_path)
    validation = None if args.skip_collection else validate_quality_policy(policy, repo_root=REPO_ROOT)
    errors = [] if validation is None else list(validation.errors)
    if args.coverage_json:
        errors.extend(evaluate_coverage(load_coverage(args.coverage_json), policy.coverage))
    if args.evidence_dir:
        expected_selection = None if validation is None else selected_lane_nodes(validation, args.lane)
        errors.extend(
            validate_evidence(
                artifact_dir=args.evidence_dir,
                policy_path=policy_path,
                expected_selection=expected_selection,
                expected_collection=None if validation is None else validation.collection,
                require_clean=args.require_clean,
                expected_sha=args.expected_sha,
                require_all_passed=True,
            )
        )
    if not args.manifest_only and not args.coverage_json and not args.evidence_dir:
        errors.append("one of --manifest-only, --coverage-json, or --evidence-dir is required")
    if errors:
        for error in sorted(set(errors)):
            print(f"TEST_QUALITY_POLICY_FAIL: {error}")
        return 1
    node_count = 0 if validation is None else len(validation.collection.nodeids)
    print(f"TEST_QUALITY_POLICY_OK: pytest_leaves={node_count} evidence={'yes' if args.evidence_dir else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
