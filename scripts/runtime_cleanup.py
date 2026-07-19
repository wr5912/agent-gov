#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TypedDict

RUNTIME_BACKUP_DIR_NAME = ".runtime-bootstrap-backups"
BOOTSTRAP_BACKUP_DIR_NAME = ".runtime-bootstrap-backups"
BOOTSTRAP_STAGING_DIR_NAME = ".runtime-bootstrap-staging"
PROTECTED_RUNTIME_DIR_NAMES = {".git", "data", "langfuse"}


class CleanupResult(TypedDict):
    removed: list[str]
    skipped_protected: list[str]


def _is_dangerous_root(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == resolved.parent


def _is_protected_runtime_path(path: Path, *, runtime_root: Path) -> bool:
    try:
        rel_path = path.relative_to(runtime_root)
    except ValueError:
        return True
    return any(part in PROTECTED_RUNTIME_DIR_NAMES for part in rel_path.parts)


def _runtime_backup_candidates(runtime_root: Path) -> tuple[list[Path], list[str]]:
    candidates: list[Path] = []
    skipped: list[str] = []
    if not runtime_root.exists():
        return candidates, skipped
    backup_dir = runtime_root / RUNTIME_BACKUP_DIR_NAME
    if backup_dir.exists():
        candidates.append(backup_dir)
    for path in sorted(runtime_root.rglob("*")):
        try:
            rel_path = path.relative_to(runtime_root)
        except ValueError:
            skipped.append(path.as_posix())
            continue
        if rel_path.parts and rel_path.parts[0] == RUNTIME_BACKUP_DIR_NAME:
            continue
        if ".bak-" not in path.name:
            continue
        if _is_protected_runtime_path(path, runtime_root=runtime_root):
            skipped.append(path.as_posix())
            continue
        candidates.append(path)
    return candidates, skipped


def _bootstrap_artifact_candidates(bootstrap_dir: Path) -> list[Path]:
    parent = bootstrap_dir.parent
    bootstrap_name = bootstrap_dir.name
    candidates = [
        parent / BOOTSTRAP_BACKUP_DIR_NAME,
        parent / BOOTSTRAP_STAGING_DIR_NAME,
        parent / f".{bootstrap_name}.restore",
        parent / f".{bootstrap_name}.before-restore",
    ]
    candidates.extend(sorted(parent.glob(f".{bootstrap_name}.old-*")))
    return [path for path in candidates if path.exists()]


def _remove_candidate(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _dedupe_existing(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path.absolute()
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        deduped.append(path)
    return deduped


def cleanup_runtime_artifacts(
    *,
    runtime_root: Path | None = None,
    bootstrap_dir: Path | None = None,
    extra_paths: list[Path] | None = None,
    dry_run: bool = False,
) -> CleanupResult:
    candidates: list[Path] = []
    skipped: list[str] = []
    if runtime_root is not None:
        if _is_dangerous_root(runtime_root):
            raise ValueError(f"Refusing to clean dangerous runtime root: {runtime_root}")
        runtime_candidates, runtime_skipped = _runtime_backup_candidates(runtime_root)
        candidates.extend(runtime_candidates)
        skipped.extend(runtime_skipped)
    if bootstrap_dir is not None:
        candidates.extend(_bootstrap_artifact_candidates(bootstrap_dir))
    if extra_paths:
        candidates.extend(path for path in extra_paths if path.exists())

    removed: list[str] = []
    for path in _dedupe_existing(candidates):
        removed.append(path.as_posix())
        _remove_candidate(path, dry_run=dry_run)
    return {"removed": removed, "skipped_protected": skipped}
