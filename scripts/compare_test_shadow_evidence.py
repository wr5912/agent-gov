#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from test_quality.coverage import compare_coverage_snapshots, coverage_snapshot, evaluate_coverage
from test_quality.evidence import TestEvidence, validate_evidence
from test_quality.policy import load_quality_policy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare serial, xdist, and TIA evidence produced for one commit.")
    parser.add_argument("--serial-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, action="append", default=[])
    parser.add_argument("--tia-dir", type=Path)
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load(directory: Path) -> tuple[TestEvidence, Mapping[str, object]]:
    evidence = TestEvidence.model_validate_json((directory / "evidence.json").read_text(encoding="utf-8"))
    coverage = json.loads((directory / "coverage.json").read_text(encoding="utf-8"))
    if not isinstance(coverage, dict):
        raise ValueError(f"coverage JSON must be an object: {directory}")
    coverage.pop("meta", None)
    return evidence, coverage


def _identity_errors(reference: TestEvidence, candidate: TestEvidence) -> list[str]:
    errors = []
    for field in ("commit_sha", "policy_sha256"):
        if getattr(reference, field) != getattr(candidate, field):
            errors.append(f"{field} mismatch")
    if reference.collection.global_digest != candidate.collection.global_digest:
        errors.append("global collection digest mismatch")
    return errors


def _artifact_errors(directory: Path, policy_path: Path) -> list[str]:
    return validate_evidence(
        artifact_dir=directory,
        policy_path=policy_path,
        require_clean=os.environ.get("CI", "").lower() == "true",
        expected_sha=os.environ.get("GITHUB_SHA"),
        expected_run_id=os.environ.get("GITHUB_RUN_ID"),
        expected_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
        require_all_passed=True,
    )


def main() -> int:
    args = parse_args()
    policy = load_quality_policy(args.policy)
    serial, serial_coverage = _load(args.serial_dir)
    serial_snapshot = coverage_snapshot(serial_coverage)
    mismatches = [f"serial evidence: {error}" for error in _artifact_errors(args.serial_dir, args.policy)]
    mismatches.extend(f"serial coverage: {error}" for error in evaluate_coverage(serial_coverage, policy.coverage))
    candidates: list[dict[str, object]] = []
    for directory in args.candidate_dir:
        candidate, coverage = _load(directory)
        candidate_snapshot = coverage_snapshot(coverage)
        errors = _artifact_errors(directory, args.policy)
        errors.extend(_identity_errors(serial, candidate))
        if candidate.selection != serial.selection:
            errors.append("selection mismatch")
        if candidate.outcomes != serial.outcomes:
            errors.append("outcomes mismatch")
        errors.extend(f"coverage: {error}" for error in evaluate_coverage(coverage, policy.coverage))
        coverage_errors, line_delta, branch_delta = compare_coverage_snapshots(
            serial_snapshot,
            candidate_snapshot,
            max_delta_percentage_points=policy.parallel.max_coverage_delta_percentage_points,
        )
        errors.extend(coverage_errors)
        label = f"n{candidate.timing.workers}-{candidate.timing.scheduler}"
        mismatches.extend(f"{label}: {error}" for error in errors)
        speedup = 100 * (1 - candidate.timing.wall_seconds / serial.timing.wall_seconds)
        cpu_increase = 100 * (candidate.timing.wall_seconds * max(candidate.timing.workers, 1) / serial.timing.wall_seconds - 1)
        candidates.append(
            {
                "label": label,
                "workers": candidate.timing.workers,
                "scheduler": candidate.timing.scheduler,
                "wall_seconds": candidate.timing.wall_seconds,
                "speedup_percent": round(speedup, 2),
                "cpu_increase_percent": round(cpu_increase, 2),
                "coverage_line_delta_percentage_points": round(line_delta, 4),
                "coverage_branch_delta_percentage_points": round(branch_delta, 4),
                "mismatches": errors,
            }
        )
    tia: dict[str, object] | None = None
    if args.tia_dir:
        impacted, _ = _load(args.tia_dir)
        errors = _artifact_errors(args.tia_dir, args.policy)
        errors.extend(_identity_errors(serial, impacted))
        selected = set(impacted.selection)
        if not selected <= set(serial.selection):
            errors.append("TIA selection is not a subset of main-full")
        if {nodeid: serial.outcomes[nodeid] for nodeid in selected} != impacted.outcomes:
            errors.append("TIA outcomes differ from main-full for selected leaves")
        misses = sorted(nodeid for nodeid, outcome in serial.outcomes.items() if outcome == "failed" and nodeid not in selected)
        if misses:
            errors.append(f"TIA missed failing leaves: {misses[:5]}")
        mismatches.extend(f"tia: {error}" for error in errors)
        tia = {
            "selected_count": len(selected),
            "full_count": len(serial.selection),
            "misses": misses,
            "mismatches": errors,
        }
    report = {
        "commit_sha": serial.commit_sha,
        "started_at": serial.timing.started_at.isoformat(),
        "serial_wall_seconds": serial.timing.wall_seconds,
        "candidates": candidates,
        "tia": tia,
        "mismatches": mismatches,
        "sample_passed": not mismatches,
        "promotion_eligible": False,
        "promotion_reason": "单次样本只用于配对校验；晋级需聚合至少 20 组且跨越 14 天",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mismatches:
        for error in mismatches:
            print(f"TEST_SHADOW_MISMATCH: {error}")
        return 1
    print(f"TEST_SHADOW_OK: candidates={len(candidates)} tia={'yes' if tia else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
