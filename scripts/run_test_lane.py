#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_quality.collection import expand_selectors
from test_quality.coverage import evaluate_coverage, load_coverage
from test_quality.evidence import build_evidence, utc_now, validate_evidence, write_evidence
from test_quality.policy import PolicyValidation, load_quality_policy, selected_lane_nodes, validate_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one governed pytest lane and produce tamper-evident diagnostics.")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    parser.add_argument("--lane", default="main-full")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--scheduler", choices=("load", "worksteal"), default="load")
    parser.add_argument("--selection-file", type=Path)
    parser.add_argument("--selector", action="append", default=[])
    parser.add_argument("--skip-coverage-threshold", action="store_true")
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return (REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def _selection(args: argparse.Namespace, validation: PolicyValidation) -> tuple[str, ...]:
    selection = selected_lane_nodes(validation, args.lane)
    if args.selection_file and args.selector:
        raise ValueError("--selection-file and --selector are mutually exclusive")
    if args.selection_file:
        selection_path = _resolve(args.selection_file)
        raw_selection = json.loads(selection_path.read_text(encoding="utf-8"))
        requested = raw_selection.get("nodeids") if isinstance(raw_selection, dict) else None
        if not isinstance(requested, list) or not all(isinstance(nodeid, str) for nodeid in requested):
            raise ValueError("selection file must contain a string nodeids list")
        selection = tuple(sorted(set(requested)))
        unknown = sorted(set(selection) - set(validation.collection.nodeids))
        if unknown:
            raise ValueError(f"selection contains unknown pytest leaves: {unknown[:5]}")
    elif args.selector:
        selection = expand_selectors(args.selector, validation.collection.nodeids)
        missing = [selector for selector in args.selector if not expand_selectors([selector], validation.collection.nodeids)]
        if missing:
            raise ValueError(f"selectors expand to zero pytest leaves: {missing}")
    if not selection:
        raise ValueError(f"lane expands to zero pytest leaves: {args.lane}")
    return selection


def _pytest_command(args: argparse.Namespace, artifact_dir: Path, selection: tuple[str, ...]) -> list[str]:
    git_config = artifact_dir / "gitconfig"
    git_config.touch()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--durations=50",
        f"--junitxml={artifact_dir / 'junit.xml'}",
        "-o",
        "junit_family=xunit2",
        "-p",
        "scripts.test_quality.pytest_plugin",
        "--cov=app",
        "--cov=scripts",
        "--cov-branch",
        "--cov-report=term-missing:skip-covered",
        f"--cov-report=json:{artifact_dir / 'coverage.json'}",
    ]
    if args.workers:
        command.extend(["-n", str(args.workers), "--dist", args.scheduler])
    command.extend(selection)
    return command


def main() -> int:
    args = parse_args()
    policy_path = _resolve(args.policy)
    artifact_dir = _resolve(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    policy = load_quality_policy(policy_path)
    validation = validate_quality_policy(policy, repo_root=REPO_ROOT)
    if validation.errors:
        for error in validation.errors:
            print(f"TEST_QUALITY_POLICY_FAIL: {error}")
        return 1
    try:
        selection = _selection(args, validation)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"TEST_LANE_FAIL: {exc}")
        return 1
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = str(artifact_dir / "gitconfig")
    command = _pytest_command(args, artifact_dir, selection)
    started_at = utc_now()
    started = time.monotonic()
    result = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)
    wall_seconds = time.monotonic() - started
    completed_at = utc_now()
    required = (artifact_dir / "junit.xml", artifact_dir / "coverage.json")
    if not all(path.is_file() for path in required):
        print("TEST_LANE_FAIL: pytest did not produce JUnit and coverage artifacts")
        return result.returncode or 1
    try:
        evidence = build_evidence(
            repo_root=REPO_ROOT,
            policy_path=policy_path,
            artifact_dir=artifact_dir,
            lane=args.lane,
            global_collection=validation.collection,
            selection=selection,
            command=command,
            started_at=started_at,
            completed_at=completed_at,
            wall_seconds=wall_seconds,
            workers=args.workers,
            scheduler=args.scheduler if args.workers else "serial",
        )
        write_evidence(evidence, artifact_dir / "evidence.json")
    except ValueError as exc:
        print(f"TEST_EVIDENCE_FAIL: {exc}")
        return result.returncode or 1
    errors = validate_evidence(
        artifact_dir=artifact_dir,
        policy_path=policy_path,
        expected_selection=selection,
        expected_collection=validation.collection,
        require_all_passed=True,
    )
    if not args.skip_coverage_threshold:
        errors.extend(evaluate_coverage(load_coverage(artifact_dir / "coverage.json"), policy.coverage))
    if errors:
        for error in errors:
            print(f"TEST_EVIDENCE_FAIL: {error}")
        return result.returncode or 1
    if result.returncode:
        print(
            f"TEST_LANE_FAIL: lane={args.lane} leaves={len(selection)} workers={args.workers} "
            f"pytest_exit={result.returncode} seconds={wall_seconds:.2f} artifacts={artifact_dir}"
        )
        return result.returncode
    print(f"TEST_LANE_OK: lane={args.lane} leaves={len(selection)} workers={args.workers} seconds={wall_seconds:.2f} artifacts={artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
