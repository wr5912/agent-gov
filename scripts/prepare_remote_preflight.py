#!/usr/bin/env python3
"""组装远端只读预检的最小源码及已锁定的纯 Python dotenv 依赖。"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
from pathlib import Path

PREFLIGHT_SOURCE_FILES = (
    "scripts/__init__.py",
    "scripts/agentscope_atomic_cutover.py",
    "scripts/agentscope_atomic_cutover_bootstrap.py",
    "scripts/agentscope_atomic_cutover_images.py",
    "scripts/agentscope_atomic_cutover_env.py",
    "app/__init__.py",
    "app/runtime/__init__.py",
    "app/runtime/sqlite_schema_contract.py",
)


def prepare_bundle(source_root: Path, destination: Path) -> None:
    requirements = (source_root / "requirements-api.txt").read_text(encoding="utf-8")
    pins = re.findall(r"(?m)^python-dotenv==([0-9]+(?:\.[0-9]+)+)$", requirements)
    distribution = importlib.metadata.distribution("python-dotenv")
    if len(pins) != 1 or distribution.version != pins[0]:
        raise ValueError("预检依赖与 requirements-api.txt 的 python-dotenv 精确锁不一致")
    package_files = tuple(item for item in distribution.files or () if item.parts[0] == "dotenv" and item.suffix == ".py")
    required = {"dotenv/__init__.py", "dotenv/parser.py", "dotenv/main.py", "dotenv/variables.py"}
    if not required <= {item.as_posix() for item in package_files}:
        raise ValueError("已安装的 python-dotenv 缺少必要纯 Python 模块")
    destination.mkdir(mode=0o700)
    for relative in PREFLIGHT_SOURCE_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, target)
    for item in package_files:
        source = Path(distribution.locate_file(item))
        if source.is_symlink() or not source.is_file() or ".." in item.parts:
            raise ValueError("python-dotenv 包含非普通模块文件")
        target = destination / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare_bundle(args.source_root, args.output)
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
        parser.exit(1, "无法组装远端预检依赖；请使用项目已锁定的本机 Python 环境。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
