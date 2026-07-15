#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from test_quality.policy import load_quality_policy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate accumulated shadow reports against data-gated promotion rules.")
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_quality_policy(args.policy)
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    dates = [datetime.fromisoformat(report["started_at"]).date() for report in reports]
    span_days = (max(dates) - min(dates)).days + 1 if dates else 0
    required = policy.parallel.promotion_gate
    blockers: list[str] = []
    if len(reports) < required.min_paired_samples:
        blockers.append(f"paired samples {len(reports)} < {required.min_paired_samples}")
    if span_days < required.min_calendar_days:
        blockers.append(f"calendar span {span_days} < {required.min_calendar_days} days")
    mismatched = sum(bool(report.get("mismatches")) for report in reports)
    if mismatched > required.max_misses:
        blockers.append(f"mismatched samples {mismatched} > {required.max_misses}")
    configs: dict[str, list[dict[str, object]]] = {}
    for report in reports:
        for candidate in report.get("candidates", []):
            configs.setdefault(candidate["label"], []).append(candidate)
    eligible_configs: list[str] = []
    for label, samples in sorted(configs.items()):
        speedup = statistics.median(float(sample["speedup_percent"]) for sample in samples)
        cpu_increase = statistics.median(float(sample["cpu_increase_percent"]) for sample in samples)
        if speedup >= policy.parallel.min_p50_speedup_percent and cpu_increase <= policy.parallel.max_cpu_minutes_increase_percent:
            eligible_configs.append(label)
    if not eligible_configs:
        blockers.append("no worker/scheduler configuration meets speed and CPU budgets")
    tia_misses = sum(len((report.get("tia") or {}).get("misses", [])) for report in reports)
    if tia_misses > policy.impact.promotion_gate.max_misses:
        blockers.append(f"TIA misses {tia_misses} > {policy.impact.promotion_gate.max_misses}")
    print(
        json.dumps(
            {
                "promotion_eligible": not blockers,
                "paired_samples": len(reports),
                "calendar_days": span_days,
                "eligible_parallel_configs": eligible_configs,
                "blockers": blockers,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
