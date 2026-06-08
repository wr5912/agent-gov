#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, resolve_runtime_root
from runtime_cleanup import cleanup_runtime_artifacts


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="Clean runtime backup and runtime-template transient artifacts.")
    parser.add_argument("--runtime-root", help="Runtime root to clean. Defaults to the selected env file/mode root.")
    parser.add_argument("--runtime-volume-mode", choices=["container", "local-debug"])
    parser.add_argument("--env-file", type=Path, default=repo_root / DEFAULT_ENV_FILE)
    parser.add_argument("--template-dir", type=Path, default=repo_root / DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--template-artifacts", action="store_true", help="Also clean docker runtime-template transient artifacts.")
    parser.add_argument("--runtime-artifacts", action="store_true", help="Clean runtime root backup artifacts.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    clean_runtime = args.runtime_artifacts or not args.template_artifacts
    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode) if clean_runtime else None
    result = cleanup_runtime_artifacts(
        runtime_root=runtime_root,
        template_dir=args.template_dir.resolve() if args.template_artifacts else None,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "runtime_root": runtime_root.as_posix() if runtime_root else None,
                "template_dir": args.template_dir.resolve().as_posix() if args.template_artifacts else None,
                "dry_run": args.dry_run,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
