#!/usr/bin/env python3
"""仅供已封存部署验收 Node 子进程调用的只读身份门。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.selected_env_deployed_context import verify_context


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise ValueError("unexpected arguments")
        config = verify_context(dict(os.environ), require_node_parent=True)
    except Exception:
        print(json.dumps({"status": "failed", "code": "DEPLOYED_CONTEXT_INVALID"}))
        return 1
    print(json.dumps(config, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
