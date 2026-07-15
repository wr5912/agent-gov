from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .models import CoveragePolicy, CoverageThreshold


@dataclass(frozen=True)
class CoverageSnapshot:
    num_statements: int
    num_branches: int
    excluded_lines: int
    line_percent: float
    branch_percent: float


def load_coverage(path: Path) -> Mapping[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"coverage JSON must be an object: {path}")
    return data


def _number(mapping: dict[str, object], key: str) -> float:
    value = mapping.get(key, 0)
    if not isinstance(value, int | float):
        raise ValueError(f"coverage field must be numeric: {key}")
    return float(value)


def _summary_errors(name: str, summary: dict[str, object], threshold: CoverageThreshold) -> list[str]:
    raw_line = summary.get("percent_statements_covered", summary.get("percent_covered", 0))
    if not isinstance(raw_line, int | float):
        raise ValueError(f"coverage line percentage must be numeric: {name}")
    branches = _number(summary, "num_branches")
    missing = _number(summary, "missing_branches")
    branch_percent = 100.0 if branches <= 0 else ((branches - missing) / branches) * 100
    errors: list[str] = []
    if float(raw_line) + 0.00001 < threshold.line_percent_min:
        errors.append(f"{name} line coverage {float(raw_line):.2f}% is below required {threshold.line_percent_min:.2f}%")
    if branch_percent + 0.00001 < threshold.branch_percent_min:
        errors.append(f"{name} branch coverage {branch_percent:.2f}% is below required {threshold.branch_percent_min:.2f}%")
    return errors


def coverage_snapshot(data: Mapping[str, object]) -> CoverageSnapshot:
    totals = data.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("coverage JSON is missing totals")
    raw_line = totals.get("percent_statements_covered", totals.get("percent_covered", 0))
    if not isinstance(raw_line, int | float):
        raise ValueError("coverage line percentage must be numeric")
    statements = _number(totals, "num_statements")
    branches = _number(totals, "num_branches")
    missing_branches = _number(totals, "missing_branches")
    excluded_lines = _number(totals, "excluded_lines")
    branch_percent = 100.0 if branches <= 0 else ((branches - missing_branches) / branches) * 100
    return CoverageSnapshot(
        num_statements=int(statements),
        num_branches=int(branches),
        excluded_lines=int(excluded_lines),
        line_percent=float(raw_line),
        branch_percent=branch_percent,
    )


def compare_coverage_snapshots(
    reference: CoverageSnapshot,
    candidate: CoverageSnapshot,
    *,
    max_delta_percentage_points: float,
) -> tuple[list[str], float, float]:
    errors: list[str] = []
    reference_universe = (reference.num_statements, reference.num_branches, reference.excluded_lines)
    candidate_universe = (candidate.num_statements, candidate.num_branches, candidate.excluded_lines)
    if candidate_universe != reference_universe:
        errors.append("coverage instrumentation universe mismatch")
    line_delta = candidate.line_percent - reference.line_percent
    branch_delta = candidate.branch_percent - reference.branch_percent
    if abs(line_delta) > max_delta_percentage_points or abs(branch_delta) > max_delta_percentage_points:
        errors.append(f"coverage delta exceeds {max_delta_percentage_points:.2f} percentage points: line={line_delta:+.4f}, branch={branch_delta:+.4f}")
    return errors, line_delta, branch_delta


def evaluate_coverage(data: Mapping[str, object], policy: CoveragePolicy) -> list[str]:
    totals = data.get("totals")
    if not isinstance(totals, dict):
        return ["coverage JSON is missing totals"]
    errors = _summary_errors("global", totals, policy.global_)
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    for threshold in policy.files:
        entry = files.get(threshold.path)
        if not isinstance(entry, dict) or not isinstance(entry.get("summary"), dict):
            errors.append(f"coverage file {threshold.path} is missing from coverage JSON")
            continue
        errors.extend(_summary_errors(threshold.path, entry["summary"], threshold))
    return errors
