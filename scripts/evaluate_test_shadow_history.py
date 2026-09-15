#!/usr/bin/env python3
"""只读复核 shadow 原始证据；汇总结果仍不改变 policy 的 shadow 模式。"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import TypedDict

try:
    from scripts.compare_test_shadow_evidence import CandidateComparison, ShadowReport, compare_evidence
    from scripts.test_quality.policy import load_quality_policy
except ModuleNotFoundError:
    from compare_test_shadow_evidence import CandidateComparison, ShadowReport, compare_evidence
    from test_quality.policy import load_quality_policy


class HistoryEvaluation(TypedDict):
    promotion_eligible: bool
    paired_samples: int
    calendar_days: int
    eligible_parallel_configs: list[str]
    blockers: list[str]
    mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate accumulated shadow reports against data-gated promotion rules.")
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    return parser.parse_args()


def _source_directory(source: object) -> Path:
    if not isinstance(source, dict) or not isinstance(source.get("directory"), str) or not isinstance(source.get("evidence_sha256"), str):
        raise ValueError("shadow report has no exact source artifact reference")
    return Path(source["directory"])


def _verified_report(path: Path, policy_path: Path) -> ShadowReport:
    stored = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stored, dict):
        raise ValueError("shadow report must be an object")
    sources = stored.get("sources")
    if not isinstance(sources, dict) or not isinstance(sources.get("candidates"), list):
        raise ValueError("shadow report has no original source artifact references")
    serial_dir = _source_directory(sources.get("serial"))
    candidate_dirs = [_source_directory(source) for source in sources["candidates"]]
    tia_dir = _source_directory(sources["tia"]) if sources.get("tia") is not None else None
    actual = compare_evidence(serial_dir=serial_dir, candidate_dirs=candidate_dirs, tia_dir=tia_dir, policy_path=policy_path)
    if actual != stored:
        raise ValueError("shadow report differs from its revalidated source artifacts")
    return actual


def _span_days(reports: list[ShadowReport]) -> int:
    if not reports:
        return 0
    dates = [datetime.fromisoformat(str(report["started_at"])).date() for report in reports]
    return (max(dates) - min(dates)).days + 1


def evaluate_history(report_paths: list[Path], *, policy_path: Path) -> HistoryEvaluation:
    policy = load_quality_policy(policy_path)
    reports: list[ShadowReport] = []
    blockers: list[str] = []
    sample_ids: set[str] = set()
    identity: tuple[object, ...] | None = None
    for path in report_paths:
        try:
            report = _verified_report(path, policy_path)
            source = report["sources"]["serial"]
            serial_evidence = json.loads((Path(source["directory"]) / "evidence.json").read_text(encoding="utf-8"))
            current_identity = (
                report["commit_sha"],
                serial_evidence["policy_sha256"],
                tuple(sorted(serial_evidence["dependency_hashes"].items())),
                serial_evidence["collection"]["global_digest"],
                serial_evidence["collection"]["global_count"],
            )
            if identity is None:
                identity = current_identity
            elif current_identity != identity:
                blockers.append(f"{path}: shadow reports do not share one source SHA, policy, dependencies, and global collection")
                continue
            sample_id = str(report["sample_id"])
            if sample_id in sample_ids:
                blockers.append(f"{path}: duplicate serial evidence sample")
                continue
            sample_ids.add(sample_id)
            if not report["sample_passed"]:
                blockers.append(f"{path}: source pair failed validation or contains failing pytest leaves")
            reports.append(report)
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            blockers.append(f"{path}: invalid shadow report or source evidence: {exc}")

    parallel_gate = policy.parallel.promotion_gate
    impact_gate = policy.impact.promotion_gate
    span_days = _span_days(reports)
    if len(reports) < parallel_gate.min_paired_samples or len(reports) < impact_gate.min_paired_samples:
        blockers.append(f"paired samples {len(reports)} < required {max(parallel_gate.min_paired_samples, impact_gate.min_paired_samples)}")
    if span_days < parallel_gate.min_calendar_days or span_days < impact_gate.min_calendar_days:
        blockers.append(f"calendar span {span_days} < required {max(parallel_gate.min_calendar_days, impact_gate.min_calendar_days)} days")

    configs: dict[str, list[tuple[ShadowReport, CandidateComparison]]] = {}
    tia_misses = 0
    for report in reports:
        tia = report.get("tia")
        if not isinstance(tia, dict):
            blockers.append("sample lacks paired TIA evidence")
        else:
            tia_misses += len(tia["misses"])
        for candidate in report["candidates"]:
            configs.setdefault(str(candidate["label"]), []).append((report, candidate))
    if tia_misses > impact_gate.max_misses:
        blockers.append(f"TIA misses {tia_misses} > {impact_gate.max_misses}")
    mismatched = sum(bool(report["mismatches"]) for report in reports)
    if mismatched > parallel_gate.max_misses:
        blockers.append(f"mismatched samples {mismatched} > {parallel_gate.max_misses}")

    eligible_configs: list[str] = []
    for label, samples in sorted(configs.items()):
        if len(samples) < parallel_gate.min_paired_samples or _span_days([report for report, _ in samples]) < parallel_gate.min_calendar_days:
            continue
        speedup = statistics.median(float(candidate["speedup_percent"]) for _, candidate in samples)
        cpu_increase = statistics.median(float(candidate["cpu_increase_percent"]) for _, candidate in samples)
        if speedup >= policy.parallel.min_p50_speedup_percent and cpu_increase <= policy.parallel.max_cpu_minutes_increase_percent:
            eligible_configs.append(label)
    if not eligible_configs:
        blockers.append("no worker/scheduler configuration has sufficient distinct paired samples, calendar span, speed, and CPU budget")
    return {
        "promotion_eligible": not blockers,
        "paired_samples": len(reports),
        "calendar_days": span_days,
        "eligible_parallel_configs": eligible_configs,
        "blockers": blockers,
        "mode": "shadow",
    }


def main() -> int:
    args = parse_args()
    result = evaluate_history(args.reports, policy_path=args.policy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["promotion_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
