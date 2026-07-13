#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

from export_runtime_template import DEFAULT_BACKUP_DIR, DEFAULT_TEMPLATE_DIR, _create_backup
from runtime_cleanup import cleanup_runtime_artifacts


class RestoreResult(TypedDict):
    ok: bool
    restored_from: str
    template_dir: str
    pre_restore_backup: str | None
    cleanup_removed: list[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def list_backups(backup_dir: Path) -> list[str]:
    if not backup_dir.exists():
        return []
    return [path.as_posix() for path in sorted(backup_dir.glob("*.tar.gz"))]


def _resolve_backup(value: str, backup_dir: Path) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate.resolve()
    candidate = backup_dir / value
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"backup not found: {value}")


def _safe_extract(archive: tarfile.TarFile, dest: Path) -> None:
    members: list[tuple[tarfile.TarInfo, Path]] = []
    for member in archive.getmembers():
        if not (member.isdir() or member.isreg()):
            raise ValueError(f"unsupported tar member type: {member.name}")

        member_path = PurePosixPath(member.name)
        if not member_path.parts or member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"unsafe tar member path: {member.name}")
        members.append((member, dest.joinpath(*member_path.parts)))

    directories: list[tuple[tarfile.TarInfo, Path]] = []
    for member, target in members:
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            directories.append((member, target))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"cannot read tar member: {member.name}")
        with source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        target.chmod(member.mode & 0o777)

    for member, target in reversed(directories):
        target.chmod(member.mode & 0o777)


def restore_backup(*, backup_path: Path, template_dir: Path, backup_dir: Path) -> RestoreResult:
    _create_backup(template_dir, backup_dir, prefix="pre-restore-runtime-volume-seeds")
    with tempfile.TemporaryDirectory(prefix="runtime-volume-seeds-restore-") as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(backup_path, "r:gz") as archive:
            _safe_extract(archive, tmp_path)
        extracted = tmp_path / template_dir.name
        if not extracted.exists():
            children = [child for child in tmp_path.iterdir() if child.is_dir()]
            if len(children) != 1:
                raise ValueError("backup archive does not contain a single template directory")
            extracted = children[0]
        replacement = template_dir.with_name(f".{template_dir.name}.restore")
        if replacement.exists():
            shutil.rmtree(replacement)
        shutil.copytree(extracted, replacement)
        old = template_dir.with_name(f".{template_dir.name}.before-restore")
        if old.exists():
            shutil.rmtree(old)
        if template_dir.exists():
            template_dir.rename(old)
        try:
            replacement.rename(template_dir)
        except Exception:
            if template_dir.exists():
                shutil.rmtree(template_dir)
            if old.exists():
                old.rename(template_dir)
            raise
        if old.exists():
            shutil.rmtree(old)
    cleanup_result = cleanup_runtime_artifacts(template_dir=template_dir, extra_paths=[backup_dir])
    return {
        "ok": True,
        "restored_from": backup_path.as_posix(),
        "template_dir": template_dir.as_posix(),
        "pre_restore_backup": None,
        "cleanup_removed": cleanup_result["removed"],
    }


def main() -> int:
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="List or restore docker/runtime-volume-seeds backups.")
    parser.add_argument("--backup-dir", type=Path, default=repo_root / DEFAULT_BACKUP_DIR)
    parser.add_argument("--template-dir", type=Path, default=repo_root / DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--backup", help="Backup tar.gz path or basename to restore.")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    backup_dir = args.backup_dir.resolve()
    if args.list:
        print(json.dumps({"backups": list_backups(backup_dir)}, ensure_ascii=False, indent=2))
        return 0
    if not args.backup:
        parser.error("--backup is required unless --list is used")
    result = restore_backup(
        backup_path=_resolve_backup(args.backup, backup_dir),
        template_dir=args.template_dir.resolve(),
        backup_dir=backup_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
