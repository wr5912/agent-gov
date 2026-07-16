#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .test_quality.models import QualityPolicy
    from .test_quality.policy import load_quality_policy
else:
    from test_quality.models import QualityPolicy
    from test_quality.policy import load_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATION_ARTIFACTS = ("mutmut-cicd-stats.json", "results.txt", "survivors.diff", "summary.json")
SURVIVED_RESULT_RE = re.compile(r"^\s*(\S+): survived\s*$", re.MULTILINE)


@dataclass(frozen=True)
class MutationDetailEvidence:
    survivors: tuple[str, ...]
    results_sha256: str
    survivors_sha256: str


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_mutation_artifact_dir(artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    owned = {path for name in MUTATION_ARTIFACTS if (path := artifact_dir / name).exists() or path.is_symlink()}
    for path in owned:
        if path.is_symlink():
            raise ValueError(f"mutation artifact must not be a symlink: {path.name}")
        if not path.is_file():
            raise ValueError(f"mutation artifact path must be a file: {path.name}")
    for path in owned:
        path.unlink()


def reset_mutants_dir(mutants_dir: Path) -> None:
    if mutants_dir.is_symlink():
        raise ValueError("mutants work directory must not be a symlink")
    if mutants_dir.exists():
        shutil.rmtree(mutants_dir)


def finalize_mutants_dir(mutants_dir: Path, *, score: float, required_score: float) -> bool:
    if score < required_score:
        return False
    reset_mutants_dir(mutants_dir)
    return True


def mutation_configuration(policy: QualityPolicy) -> tuple[list[str], list[str]]:
    configured = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    policy_paths = sorted(target.path for target in policy.mutation.targets)
    if sorted(configured.get("only_mutate", [])) != policy_paths:
        raise ValueError("pyproject mutmut targets differ from tests/quality_policy.json")
    configured_tests = sorted(configured.get("pytest_add_cli_args_test_selection", []))
    policy_tests = sorted({selector for target in policy.mutation.targets for selector in target.tests})
    if configured_tests != policy_tests:
        raise ValueError("pyproject mutmut test selection differs from tests/quality_policy.json")
    return policy_paths, policy_tests


def collect_mutation_details(
    *,
    mutmut_bin: Path,
    repo_root: Path,
    artifact_dir: Path,
    stats: Mapping[str, object],
) -> MutationDetailEvidence:
    results = subprocess.run(
        [str(mutmut_bin), "results"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if results.returncode != 0:
        raise ValueError(f"mutmut results exited {results.returncode}")
    results_text = results.stdout
    if results.stderr:
        results_text += ("\n" if results_text and not results_text.endswith("\n") else "") + results.stderr
    if results_text and not results_text.endswith("\n"):
        results_text += "\n"
    results_path = artifact_dir / "results.txt"
    results_path.write_text(results_text, encoding="utf-8")

    survivors = SURVIVED_RESULT_RE.findall(results_text)
    expected_survivors = int(stats.get("survived", 0))
    if expected_survivors < 0 or len(survivors) != expected_survivors or len(survivors) != len(set(survivors)):
        raise ValueError(f"mutation survivor evidence mismatch: stats={expected_survivors} results={len(survivors)}")

    diffs: list[str] = []
    for survivor in survivors:
        shown = subprocess.run(
            [str(mutmut_bin), "show", survivor],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if shown.returncode != 0 or not shown.stdout.strip():
            raise ValueError(f"mutmut show failed for survivor: {survivor}")
        diffs.append(shown.stdout.rstrip())
    survivor_path = artifact_dir / "survivors.diff"
    survivor_path.write_text(("\n\n".join(diffs) + "\n") if diffs else "", encoding="utf-8")
    return MutationDetailEvidence(
        survivors=tuple(survivors),
        results_sha256=_sha256(results_path),
        survivors_sha256=_sha256(survivor_path),
    )


def write_mutation_summary(
    artifact_dir: Path,
    *,
    score: float,
    required_score: float,
    policy_paths: list[str],
    policy_tests: list[str],
    stats: Mapping[str, object],
    details: MutationDetailEvidence,
    copied_stats_path: Path,
) -> None:
    payload = {
        "score": round(score, 2),
        "required_score": required_score,
        "targets": policy_paths,
        "tests": policy_tests,
        "stats": stats,
        "survivors": details.survivors,
        "artifact_hashes": {
            "results.txt": details.results_sha256,
            "survivors.diff": details.survivors_sha256,
            copied_stats_path.name: _sha256(copied_stats_path),
        },
    }
    (artifact_dir / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    policy_path = (REPO_ROOT / args.policy).resolve() if not args.policy.is_absolute() else args.policy.resolve()
    artifact_dir = (REPO_ROOT / args.artifact_dir).resolve() if not args.artifact_dir.is_absolute() else args.artifact_dir.resolve()
    policy = load_quality_policy(policy_path)
    try:
        policy_paths, policy_tests = mutation_configuration(policy)
    except ValueError as exc:
        print(f"MUTATION_FAIL: {exc}")
        return 1
    mutants_dir = REPO_ROOT / "mutants"
    mutmut_bin = REPO_ROOT / ".venv/bin/mutmut"
    try:
        prepare_mutation_artifact_dir(artifact_dir)
        reset_mutants_dir(mutants_dir)
    except (OSError, ValueError) as exc:
        print(f"MUTATION_FAIL: {exc}")
        return 1
    returncode = _run_with_timeout(
        [str(mutmut_bin), "run", "--max-children", "2"],
        policy.mutation.time_budget_seconds,
    )
    if returncode == 124:
        print(f"MUTATION_FAIL: exceeded {policy.mutation.time_budget_seconds}s time budget")
        return 1
    if returncode != 0:
        print(f"MUTATION_FAIL: mutmut run exited {returncode}")
        return returncode
    export = subprocess.run(
        [str(mutmut_bin), "export-cicd-stats"],
        cwd=REPO_ROOT,
        check=False,
    )
    stats_path = mutants_dir / "mutmut-cicd-stats.json"
    if export.returncode != 0 or not stats_path.is_file():
        print("MUTATION_FAIL: mutmut did not export CI statistics")
        return export.returncode or 1
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    copied_stats_path = artifact_dir / "mutmut-cicd-stats.json"
    shutil.copy2(stats_path, copied_stats_path)
    try:
        total, _killed, score = mutation_score(stats)
        details = collect_mutation_details(
            mutmut_bin=mutmut_bin,
            repo_root=REPO_ROOT,
            artifact_dir=artifact_dir,
            stats=stats,
        )
    except (TypeError, ValueError) as exc:
        print(f"MUTATION_FAIL: {exc}")
        return 1
    required_score = min(target.min_score for target in policy.mutation.targets)
    write_mutation_summary(
        artifact_dir,
        score=score,
        required_score=required_score,
        policy_paths=policy_paths,
        policy_tests=policy_tests,
        stats=stats,
        details=details,
        copied_stats_path=copied_stats_path,
    )
    try:
        finalized = finalize_mutants_dir(mutants_dir, score=score, required_score=required_score)
    except (OSError, ValueError) as exc:
        print(f"MUTATION_FAIL: evidence saved but work directory cleanup failed: {exc}")
        return 1
    if not finalized:
        print(f"MUTATION_FAIL: score {score:.2f}% < required {required_score:.2f}%")
        return 1
    print(f"MUTATION_OK: score={score:.2f}% mutants={total} artifacts={artifact_dir} workdir=removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
