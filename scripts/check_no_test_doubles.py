#!/usr/bin/env python3
"""拒绝正式 live/e2e/容器验收资产中的 test double 与请求拦截。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.no_test_doubles_contract import (  # noqa: E402,F401
    DEFAULT_POLICY,
    JAVASCRIPT_RULES,
    REPO_ROOT,
    TEST_ONLY_JAVASCRIPT_RULES,
    Finding,
    FormalAcceptanceScope,
    FormalMakeInspection,
)
from scripts.no_test_doubles_make import inspect_make_targets, scan_make_targets  # noqa: E402,F401
from scripts.no_test_doubles_scan import (  # noqa: E402,F401
    PythonDoubleVisitor,
    expand_local_import_closure,
    explicit_files,
    load_formal_acceptance_scope,
    scan_javascript,
    scan_paths,
    scan_python,
    scan_shell,
    validate_formal_target_allowlist,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reject test doubles only in formal live/e2e/container acceptance assets.")
    parser.add_argument("paths", nargs="*", type=Path, help="Explicit assets to inspect instead of the quality-policy formal-live scope.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        scope = load_formal_acceptance_scope(args.policy)
        if not args.paths:
            validate_formal_target_allowlist(scope.make_targets)
    except ValueError as exc:
        print(f"NO_TEST_DOUBLES_FAIL: {exc}")
        return 1
    make_inspection = FormalMakeInspection(files=(), findings=(), targets=())
    selected = explicit_files(tuple(args.paths), repo_root=REPO_ROOT) if args.paths else scope.files
    if not args.paths:
        make_inspection = inspect_make_targets(REPO_ROOT / "Makefile", scope.make_targets)
        selected = tuple(sorted({*selected, *make_inspection.files}))
    try:
        files = expand_local_import_closure(selected)
    except ValueError as exc:
        print(f"NO_TEST_DOUBLES_FAIL: {exc}")
        return 1
    findings = scan_paths(files) + make_inspection.findings
    if findings:
        for finding in findings:
            print(f"NO_TEST_DOUBLES_FAIL: {finding.render()}")
        print(f"NO_TEST_DOUBLES_FAIL: findings={len(findings)} files={len({item.path for item in findings})}")
        return 1
    print("NO_TEST_DOUBLES_OK: findings=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
