#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

try:
    from scripts.test_quality.coverage import CoverageSnapshot, compare_coverage_snapshots, coverage_snapshot, evaluate_coverage
    from scripts.test_quality.evidence import TestEvidence, sha256_file, validate_evidence
    from scripts.test_quality.models import QualityPolicy
    from scripts.test_quality.policy import load_quality_policy
except ModuleNotFoundError:
    from test_quality.coverage import CoverageSnapshot, compare_coverage_snapshots, coverage_snapshot, evaluate_coverage
    from test_quality.evidence import TestEvidence, sha256_file, validate_evidence
    from test_quality.models import QualityPolicy
    from test_quality.policy import load_quality_policy


class SourceArtifact(TypedDict):
    directory: str
    evidence_sha256: str


class SourceReferences(TypedDict):
    serial: SourceArtifact
    candidates: list[SourceArtifact]
    tia: SourceArtifact | None


class CandidateComparison(TypedDict):
    label: str
    workers: int
    scheduler: str
    wall_seconds: float
    speedup_percent: float
    cpu_increase_percent: float
    coverage_line_delta_percentage_points: float
    coverage_branch_delta_percentage_points: float
    mismatches: list[str]


class TiaComparison(TypedDict):
    selected_count: int
    full_count: int
    misses: list[str]
    mismatches: list[str]


class ShadowReport(TypedDict):
    sample_id: str
    sources: SourceReferences
    commit_sha: str
    started_at: str
    serial_wall_seconds: float
    candidates: list[CandidateComparison]
    tia: TiaComparison | None
    mismatches: list[str]
    sample_passed: bool
    promotion_eligible: bool
    promotion_reason: str


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
        require_all_passed=False,
    )


def _source(directory: Path) -> SourceArtifact:
    return {"directory": str(directory.resolve()), "evidence_sha256": sha256_file(directory / "evidence.json")}


def _serial_errors(serial: TestEvidence, coverage: Mapping[str, object], directory: Path, policy: QualityPolicy, policy_path: Path) -> list[str]:
    errors = [f"serial evidence: {error}" for error in _artifact_errors(directory, policy_path)]
    errors.extend(f"serial coverage: {error}" for error in evaluate_coverage(coverage, policy.coverage))
    if serial.dirty:
        errors.append("serial evidence has a dirty source worktree")
    if serial.lane != "main-full" or serial.timing.workers != 0 or serial.timing.scheduler != "serial":
        errors.append("serial evidence is not a main-full serial run")
    if serial.collection.global_count != serial.collection.selected_count:
        errors.append("serial evidence does not cover the global collection")
    if serial.timing.wall_seconds <= 0:
        errors.append("serial wall time must be positive")
    failed_leaves = sorted(nodeid for nodeid, outcome in serial.outcomes.items() if outcome != "passed")
    if failed_leaves:
        errors.append(f"serial evidence contains non-passing leaves: {failed_leaves[:5]}")
    return errors


def _compare_candidate(
    *, directory: Path, serial: TestEvidence, serial_snapshot: CoverageSnapshot, policy: QualityPolicy, policy_path: Path
) -> CandidateComparison:
    candidate, coverage = _load(directory)
    errors = _artifact_errors(directory, policy_path)
    errors.extend(_identity_errors(serial, candidate))
    if candidate.dirty:
        errors.append("candidate evidence has a dirty source worktree")
    if candidate.lane != serial.lane or candidate.collection.global_count != serial.collection.global_count:
        errors.append("candidate lane or global collection count differs from serial")
    if candidate.timing.workers <= 0 or candidate.timing.scheduler == "serial":
        errors.append("candidate evidence is not a parallel run")
    if candidate.selection != serial.selection:
        errors.append("selection mismatch")
    if candidate.outcomes != serial.outcomes:
        errors.append("outcomes mismatch")
    errors.extend(f"coverage: {error}" for error in evaluate_coverage(coverage, policy.coverage))
    coverage_errors, line_delta, branch_delta = compare_coverage_snapshots(
        serial_snapshot,
        coverage_snapshot(coverage),
        max_delta_percentage_points=policy.parallel.max_coverage_delta_percentage_points,
    )
    errors.extend(coverage_errors)
    serial_seconds = max(serial.timing.wall_seconds, 1e-9)
    speedup = 100 * (1 - candidate.timing.wall_seconds / serial_seconds)
    cpu_increase = 100 * (candidate.timing.wall_seconds * max(candidate.timing.workers, 1) / serial_seconds - 1)
    return {
        "label": f"n{candidate.timing.workers}-{candidate.timing.scheduler}",
        "workers": candidate.timing.workers,
        "scheduler": candidate.timing.scheduler,
        "wall_seconds": candidate.timing.wall_seconds,
        "speedup_percent": round(speedup, 2),
        "cpu_increase_percent": round(cpu_increase, 2),
        "coverage_line_delta_percentage_points": round(line_delta, 4),
        "coverage_branch_delta_percentage_points": round(branch_delta, 4),
        "mismatches": errors,
    }


def _compare_tia(*, directory: Path, serial: TestEvidence, policy_path: Path) -> TiaComparison:
    impacted, _ = _load(directory)
    errors = _artifact_errors(directory, policy_path)
    errors.extend(_identity_errors(serial, impacted))
    if impacted.dirty:
        errors.append("TIA evidence has a dirty source worktree")
    if impacted.collection.global_count != serial.collection.global_count:
        errors.append("TIA global collection count differs from serial")
    if impacted.timing.workers != 0 or impacted.timing.scheduler != "serial":
        errors.append("TIA evidence is not a serial run")
    selected = set(impacted.selection)
    if not selected <= set(serial.selection):
        errors.append("TIA selection is not a subset of main-full")
    elif {nodeid: serial.outcomes[nodeid] for nodeid in selected} != impacted.outcomes:
        errors.append("TIA outcomes differ from main-full for selected leaves")
    misses = sorted(nodeid for nodeid, outcome in serial.outcomes.items() if outcome == "failed" and nodeid not in selected)
    if misses:
        errors.append(f"TIA missed failing leaves: {misses[:5]}")
    return {"selected_count": len(selected), "full_count": len(serial.selection), "misses": misses, "mismatches": errors}


def compare_evidence(*, serial_dir: Path, candidate_dirs: list[Path], tia_dir: Path | None, policy_path: Path) -> ShadowReport:
    policy = load_quality_policy(policy_path)
    serial, serial_coverage = _load(serial_dir)
    mismatches = _serial_errors(serial, serial_coverage, serial_dir, policy, policy_path)
    sources: SourceReferences = {"serial": _source(serial_dir), "candidates": [], "tia": None}
    source_directories = {str(serial_dir.resolve())}
    candidate_labels: set[str] = set()
    candidates: list[CandidateComparison] = []
    for directory in candidate_dirs:
        resolved_directory = str(directory.resolve())
        if resolved_directory in source_directories:
            mismatches.append("duplicate shadow source directory")
        source_directories.add(resolved_directory)
        sources["candidates"].append(_source(directory))
        comparison = _compare_candidate(
            directory=directory, serial=serial, serial_snapshot=coverage_snapshot(serial_coverage), policy=policy, policy_path=policy_path
        )
        if comparison["label"] in candidate_labels:
            comparison["mismatches"].append("duplicate worker/scheduler configuration in one pair")
        candidate_labels.add(comparison["label"])
        mismatches.extend(f"{comparison['label']}: {error}" for error in comparison["mismatches"])
        candidates.append(comparison)
    tia: TiaComparison | None = None
    if tia_dir:
        resolved_directory = str(tia_dir.resolve())
        if resolved_directory in source_directories:
            mismatches.append("duplicate shadow source directory")
        sources["tia"] = _source(tia_dir)
        tia = _compare_tia(directory=tia_dir, serial=serial, policy_path=policy_path)
        mismatches.extend(f"tia: {error}" for error in tia["mismatches"])
    return {
        "sample_id": sources["serial"]["evidence_sha256"],
        "sources": sources,
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


def main() -> int:
    args = parse_args()
    report = compare_evidence(serial_dir=args.serial_dir, candidate_dirs=args.candidate_dir, tia_dir=args.tia_dir, policy_path=args.policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["mismatches"]:
        for error in report["mismatches"]:
            print(f"TEST_SHADOW_MISMATCH: {error}")
        return 1
    print(f"TEST_SHADOW_OK: candidates={len(report['candidates'])} tia={'yes' if report['tia'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
