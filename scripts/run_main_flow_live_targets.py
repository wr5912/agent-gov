#!/usr/bin/env python3
"""在同一次隔离 Compose 刷新内执行 policy 声明的真实 UI 目标。"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from test_quality.policy import REAL_CONTAINER_UI_TARGETS, load_quality_policy, real_container_ui_targets

PRIVATE_TARGETS = {target: f"_{target}" for target in REAL_CONTAINER_UI_TARGETS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run allowlisted real-container UI targets from the quality policy.")
    parser.add_argument("--policy", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE") != "1" or not os.environ.get("AGENT_GOV_ACCEPTANCE_RUN_ID", "").strip():
        raise SystemExit("必须通过 make main-flow-live-test 的隔离容器 runner 执行")
    policy = load_quality_policy(args.policy.resolve())
    targets = real_container_ui_targets(policy)
    if not targets:
        raise SystemExit("质量策略未声明真实容器 UI 目标")
    for target in targets:
        private_target = PRIVATE_TARGETS.get(target)
        if private_target is None:
            raise SystemExit(f"真实容器 UI 目标未在固定 allowlist 中: {target}")
        result = subprocess.run(["make", "--no-print-directory", private_target], check=False, env=os.environ.copy())
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
