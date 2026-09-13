"""AgentScope 真实验收命令行契约。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from scripts.agentscope_live_acceptance_scenarios import (
    GENERIC_RUNTIME_CAPABILITY,
    RUNTIME_VERIFIED_CAPABILITIES,
)
from scripts.container_acceptance_inputs import TRUTHY


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 AgentGov 公共 API 验收真实 AgentScope 运行链路。")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--scenario-file", type=Path, required=True)
    parser.add_argument("--agent-id")
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument("--technical-integration-seed", action="store_true")
    seed_group.add_argument("--mcp-technical-seed", action="store_true")
    parser.add_argument("--runs", type=int, default=int(os.environ.get("LIVE_ACCEPTANCE_RUNS", "1")))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("LIVE_ACCEPTANCE_CONCURRENCY", "1")))
    parser.add_argument(
        "--capability",
        choices=tuple(sorted(RUNTIME_VERIFIED_CAPABILITIES)),
        default=GENERIC_RUNTIME_CAPABILITY,
        help="本轮精确能力配额；不同 capability 不混算。",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--require-trace-complete",
        action="store_true",
        default=os.environ.get("LIVE_ACCEPTANCE_REQUIRE_TRACE_COMPLETE", "").strip().lower() in TRUTHY,
    )
    return parser.parse_args(argv)
