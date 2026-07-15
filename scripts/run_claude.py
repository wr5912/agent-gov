#!/usr/bin/env python3
"""从仓库根启动 Claude Code，确保项目 settings 与 hooks 被发现。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    os.chdir(REPO_ROOT)
    os.execvp("claude", ["claude", *args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
