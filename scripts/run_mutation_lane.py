#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

if __package__:
    from .test_quality.policy import load_quality_policy
else:
    from test_quality.policy import load_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the bounded high-risk mutation lane.")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts/test-quality/mutation"))
    return parser.parse_args()


def _run_with_timeout(command: list[str], timeout_seconds: int) -> int:
    process = subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return 124


def mutation_score(stats: Mapping[str, object]) -> tuple[int, int, float]:
    total = int(stats.get("total", 0))
    killed = int(stats.get("killed", 0))
    if total <= 0:
        raise ValueError("mutation run produced zero mutants")
    if killed < 0 or killed > total:
        raise ValueError(f"mutation statistics are inconsistent: killed={killed} total={total}")
    return total, killed, killed / total * 100


def main() -> int:
    args = parse_args()
    policy_path = (REPO_ROOT / args.policy).resolve() if not args.policy.is_absolute() else args.policy.resolve()
    artifact_dir = (REPO_ROOT / args.artifact_dir).resolve() if not args.artifact_dir.is_absolute() else args.artifact_dir.resolve()
    policy = load_quality_policy(policy_path)
    configured = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    policy_paths = sorted(target.path for target in policy.mutation.targets)
    if sorted(configured.get("only_mutate", [])) != policy_paths:
        print("MUTATION_FAIL: pyproject mutmut targets differ from tests/quality_policy.json")
        return 1
    configured_tests = sorted(configured.get("pytest_add_cli_args_test_selection", []))
    policy_tests = sorted({selector for target in policy.mutation.targets for selector in target.tests})
    if configured_tests != policy_tests:
        print("MUTATION_FAIL: pyproject mutmut test selection differs from tests/quality_policy.json")
        return 1
    mutants_dir = REPO_ROOT / "mutants"
    shutil.rmtree(mutants_dir, ignore_errors=True)
    returncode = _run_with_timeout(
        [str(REPO_ROOT / ".venv/bin/mutmut"), "run", "--max-children", "2"],
        policy.mutation.time_budget_seconds,
    )
    if returncode == 124:
        print(f"MUTATION_FAIL: exceeded {policy.mutation.time_budget_seconds}s time budget")
        return 1
    if returncode != 0:
        print(f"MUTATION_FAIL: mutmut run exited {returncode}")
        return returncode
    export = subprocess.run(
        [str(REPO_ROOT / ".venv/bin/mutmut"), "export-cicd-stats"],
        cwd=REPO_ROOT,
        check=False,
    )
    stats_path = mutants_dir / "mutmut-cicd-stats.json"
    if export.returncode != 0 or not stats_path.is_file():
        print("MUTATION_FAIL: mutmut did not export CI statistics")
        return export.returncode or 1
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stats_path, artifact_dir / "mutmut-cicd-stats.json")
    try:
        total, _killed, score = mutation_score(stats)
    except (TypeError, ValueError) as exc:
        print(f"MUTATION_FAIL: {exc}")
        return 1
    required_score = min(target.min_score for target in policy.mutation.targets)
    (artifact_dir / "summary.json").write_text(
        json.dumps(
            {
                "score": round(score, 2),
                "required_score": required_score,
                "targets": policy_paths,
                "tests": policy_tests,
                "stats": stats,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if score < required_score:
        print(f"MUTATION_FAIL: score {score:.2f}% < required {required_score:.2f}%")
        return 1
    print(f"MUTATION_OK: score={score:.2f}% mutants={total} artifacts={artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
