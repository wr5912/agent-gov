#!/usr/bin/env python3
"""重新核对公共容器验收 runner 生成的运行上下文。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.container_acceptance_inputs import AcceptanceError, verify_acceptance_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-trace-complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        verify_acceptance_context(
            dict(os.environ),
            require_trace_complete=args.require_trace_complete,
        )
    except AcceptanceError as exc:
        print(f"CONTAINER_ACCEPTANCE_CONTEXT_FAIL: {exc}", file=sys.stderr)
        return 1
    print("CONTAINER_ACCEPTANCE_CONTEXT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
