from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from check_test_coverage_policy import load_json, main_flow_test_bindings, validate_main_flow_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_main_flow_tests(*, policy_path: Path, repo_root: Path) -> int:
    policy = load_json(policy_path)
    manifest_errors = validate_main_flow_manifest(policy, repo_root=repo_root)
    if manifest_errors:
        for error in manifest_errors:
            print(f"MAIN_FLOW_POLICY_FAIL: {error}")
        return 1
    pytest_nodes, ui_scripts = main_flow_test_bindings(policy)
    if pytest_nodes:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", *pytest_nodes], cwd=repo_root, check=False)
        if result.returncode != 0:
            return result.returncode
    for script_name in ui_scripts:
        result = subprocess.run(["pnpm", "--dir", "frontend", "run", script_name], cwd=repo_root, check=False)
        if result.returncode != 0:
            return result.returncode
    print(f"main-flow tests OK: pytest={len(pytest_nodes)} ui={len(ui_scripts)}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run tests bound to required product main flows.")
    parser.add_argument("--policy", type=Path, default=Path("tests/coverage_policy.json"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_main_flow_tests(policy_path=args.policy, repo_root=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
