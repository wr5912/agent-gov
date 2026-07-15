from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from test_quality.policy import load_quality_policy, main_flow_bindings, validate_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_pytest_bindings(pytest_nodes: list[str], *, repo_root: Path) -> int:
    if not pytest_nodes:
        return 0
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *pytest_nodes],
        cwd=repo_root,
        check=False,
    ).returncode


def _artifact_name(script_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", script_name).strip("-")


def run_ui_bindings(ui_scripts: list[str], *, repo_root: Path, artifact_root: Path) -> int:
    artifact_root.mkdir(parents=True, exist_ok=True)
    for script_name in ui_scripts:
        script_artifacts = artifact_root / _artifact_name(script_name)
        script_artifacts.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["RETRIES"] = env.get("RETRIES", "1")
        env["VERIFY_SCREENSHOT_DIR"] = str(script_artifacts.resolve())
        result = subprocess.run(
            ["pnpm", "--dir", "frontend", "run", script_name],
            cwd=repo_root,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    return 0


def run_main_flow_tests(
    *,
    policy_path: Path,
    repo_root: Path,
    run_backend: bool = True,
    run_ui: bool = True,
    artifact_root: Path = Path("artifacts/test-quality/main-flow-ui"),
) -> int:
    policy = load_quality_policy(policy_path)
    validation = validate_quality_policy(policy, repo_root=repo_root)
    if validation.errors:
        for error in validation.errors:
            print(f"MAIN_FLOW_POLICY_FAIL: {error}")
        return 1
    pytest_nodes, ui_scripts = main_flow_bindings(policy)
    if run_backend:
        returncode = _run_pytest_bindings(pytest_nodes, repo_root=repo_root)
        if returncode != 0:
            return returncode
    if run_ui:
        returncode = run_ui_bindings(ui_scripts, repo_root=repo_root, artifact_root=artifact_root)
        if returncode != 0:
            return returncode
    print(f"main-flow tests OK: pytest={len(pytest_nodes) if run_backend else 0} ui={len(ui_scripts) if run_ui else 0}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run tests bound to required product main flows.")
    parser.add_argument("--policy", type=Path, default=Path("tests/quality_policy.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backend-only", action="store_true")
    mode.add_argument("--ui-only", action="store_true")
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/test-quality/main-flow-ui"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_main_flow_tests(
        policy_path=args.policy,
        repo_root=REPO_ROOT,
        run_backend=not args.ui_only,
        run_ui=not args.backend_only,
        artifact_root=args.artifact_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
