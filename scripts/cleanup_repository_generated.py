#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Literal, TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]

CleanupScope = Literal["generated", "evidence", "all"]

GENERATED_PATHS = (
    Path(".mypy_cache"),
    Path(".pytest_cache"),
    Path(".ruff_cache"),
    Path("frontend/dist"),
    Path("frontend/tsconfig.tsbuildinfo"),
    Path("mutants"),
)
CACHE_SEARCH_ROOTS = (
    Path(".codex"),
    Path("app"),
    Path("docker/e2e"),
    Path("scripts"),
    Path("tests"),
)
LEGACY_CACHE_ONLY_DIRS = (Path("app/worker"),)
EVIDENCE_PATHS = (Path("artifacts"),)


class CleanupResult(TypedDict):
    scope: CleanupScope
    dry_run: bool
    candidates: list[str]
    removed: list[str]
    skipped_protected: list[str]


def _relative(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _legacy_dir_is_cache_only(path: Path) -> bool:
    for descendant in path.rglob("*"):
        relative = descendant.relative_to(path)
        if descendant.is_symlink():
            return False
        if descendant.is_dir():
            if "__pycache__" not in relative.parts:
                return False
            continue
        if "__pycache__" not in relative.parts or descendant.suffix not in {".pyc", ".pyo"}:
            return False
    return True


def _validate_candidate(path: Path, repo_root: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to clean symlink: {_relative(path, repo_root)}")
    resolved = path.resolve(strict=False)
    if resolved == repo_root or repo_root not in resolved.parents:
        raise ValueError(f"cleanup candidate escapes repository: {path}")
    if not path.is_dir():
        return
    for descendant in path.rglob("*"):
        if descendant.is_symlink():
            raise ValueError(f"refusing to clean directory containing symlink: {_relative(descendant, repo_root)}")


def _collapse_nested_candidates(paths: set[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in sorted(paths, key=lambda item: (len(item.parts), item.as_posix())):
        if any(parent == path or parent in path.parents for parent in selected):
            continue
        selected.append(path)
    return selected


def _generated_candidates(repo_root: Path) -> tuple[set[Path], list[str]]:
    candidates = {path for relative in GENERATED_PATHS if (path := repo_root / relative).exists() or path.is_symlink()}
    candidates.update(path for path in repo_root.glob(".coverage*") if path.exists() or path.is_symlink())
    for relative_root in CACHE_SEARCH_ROOTS:
        search_root = repo_root / relative_root
        if not search_root.is_dir() or search_root.is_symlink():
            continue
        candidates.update(path for path in search_root.rglob("__pycache__") if path.exists() or path.is_symlink())

    skipped: list[str] = []
    for relative in LEGACY_CACHE_ONLY_DIRS:
        path = repo_root / relative
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink() and _legacy_dir_is_cache_only(path):
            candidates.add(path)
        else:
            skipped.append(relative.as_posix())
    return candidates, skipped


def cleanup_repository_generated(
    *,
    repo_root: Path = REPO_ROOT,
    scope: CleanupScope = "generated",
    dry_run: bool = False,
) -> CleanupResult:
    root = repo_root.resolve(strict=True)
    if scope not in {"generated", "evidence", "all"}:
        raise ValueError(f"unknown cleanup scope: {scope}")

    candidates: set[Path] = set()
    skipped: list[str] = []
    if scope in {"generated", "all"}:
        generated, skipped = _generated_candidates(root)
        candidates.update(generated)
    if scope in {"evidence", "all"}:
        candidates.update(path for relative in EVIDENCE_PATHS if (path := root / relative).exists() or path.is_symlink())

    selected = _collapse_nested_candidates(candidates)
    for path in selected:
        _validate_candidate(path, root)

    relative_candidates = [_relative(path, root) for path in selected]
    removed: list[str] = []
    if not dry_run:
        for path, relative in zip(selected, relative_candidates, strict=True):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(relative)
    return {
        "scope": scope,
        "dry_run": dry_run,
        "candidates": relative_candidates,
        "removed": removed,
        "skipped_protected": sorted(skipped),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean allowlisted repository-generated files without touching runtime data or dependencies.")
    parser.add_argument("--scope", choices=("generated", "evidence", "all"), default="generated")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = cleanup_repository_generated(scope=args.scope, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"REPOSITORY_CLEANUP_FAIL: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
