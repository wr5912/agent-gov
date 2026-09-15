#!/usr/bin/env python3
"""首次初始化私有 Runtime 共享密钥；已有身份只保留，不执行轮换。"""

from __future__ import annotations

import argparse
import fcntl
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.agentscope_atomic_cutover_env import (  # noqa: E402
    parse_selected_env_payload,
    read_stable_env_file,
    verify_stable_env_file,
)
from scripts.bootstrap_runtime_volume import load_runtime_env, resolve_runtime_root  # noqa: E402

SECRET_KEY = "AGENTGOV_RUNTIME_SHARED_SECRET"
TEMPLATE_VALUES = frozenset({"replace-with-at-least-32-random-characters", "local-dev-insecure-change-me-32"})
INITIALIZING_OPERATIONS = frozenset(
    {
        "up",
        "all-up",
        "runtime-bootstrap",
        "runtime-recreate",
        "ui-playground-deployed-smoke",
        "ui-playground-deployed-recovery-smoke",
    }
)


def _has_runtime_data(env_file: Path) -> bool:
    values = load_runtime_env(env_file)
    root = resolve_runtime_root(None, env_file)
    local = env_file.name == ".env.local-debug" or values.get("RUNTIME_VOLUME_MODE") == "local-debug"
    data_root = Path(values.get("DATA_DIR" if local else "HOST_DATA_MOUNT") or root / "data")
    runtime_data = Path(values.get("HOST_AGENTSCOPE_RUNTIME_DATA_MOUNT") or root / "agentscope-runtime/data")
    runtime_workspaces = Path(values.get("HOST_AGENTSCOPE_RUNTIME_WORKSPACES_MOUNT") or root / "agentscope-runtime/workspaces")
    for path in (data_root, runtime_data, runtime_workspaces):
        if not path.is_absolute():
            raise ValueError("Runtime 存储位置必须明确为绝对路径，未生成密钥")
        if path.is_symlink():
            raise ValueError("Runtime 存储位置含符号链接，未生成密钥")
    database = data_root / "runtime.sqlite3"
    if database.exists() or database.is_symlink():
        return True
    # Bootstrap 仅创建空目录不表示旧实例；真实文件、native DB 或 Session 才阻止首次生成。
    for directory in (runtime_data, runtime_workspaces):
        if directory.exists() and not directory.is_dir():
            return True
        if directory.exists() and any(not child.is_dir() or child.is_symlink() for child in directory.rglob("*")):
            return True
    return False


def _replace_atomically(env_file: Path, original: bytes, identity: tuple[int, ...], updated: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".env.bak-runtime-secret-", dir=env_file.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        verify_stable_env_file(env_file, original, identity, error_type=ValueError)
        os.replace(temporary, env_file)
        directory_fd = os.open(env_file.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def initialize_shared_secret(env_file: Path) -> bool:
    if env_file.name.endswith(".example"):
        raise ValueError("只允许初始化私有 env，不修改示例")
    # 同目录的初始化串行化；锁不随 env 原子替换失效，也不在完成后删除造成锁分叉。
    if env_file.is_symlink() or env_file.resolve(strict=True) != Path(os.path.abspath(env_file)):
        raise ValueError("私有 env 不得通过符号链接访问")
    lock = env_file.with_name(".env.bak-runtime-secret.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise ValueError("Runtime 密钥初始化锁无效")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        original, identity = read_stable_env_file(env_file, error_type=ValueError)
        bindings = parse_selected_env_payload(original)
        values = {binding.key: binding.value or "" for binding in bindings if binding.key}
        existing = values.get(SECRET_KEY, "")
        if existing.strip() and existing not in TEMPLATE_VALUES:
            if len(existing) < 16:
                raise ValueError("已有 Runtime 密钥长度无效，请显式修正，不自动轮换")
            return False
        if _has_runtime_data(env_file):
            raise ValueError("检测到已有 Runtime 数据；请恢复原共享密钥，禁止自动生成替代值")
        value = secrets.token_hex(32)
        rendered = "".join(f"{SECRET_KEY}={value}\n" if binding.key == SECRET_KEY else binding.original.string for binding in bindings)
        if SECRET_KEY not in values:
            rendered += ("\n" if rendered and not rendered.endswith("\n") else "") + f"{SECRET_KEY}={value}\n"
        _replace_atomically(env_file, original, identity, rendered.encode("utf-8"))
        return True
    finally:
        os.close(descriptor)


def initialize_before_operation(env_file: Path, operation: str) -> None:
    if operation in INITIALIZING_OPERATIONS:
        initialize_shared_secret(env_file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        changed = initialize_shared_secret(args.env_file)
    except (OSError, ValueError):
        parser.exit(1, "无法初始化 Runtime 共享密钥：请检查所选 env；已有实例必须恢复原密钥。\n")
    print("Runtime 共享密钥已首次初始化" if changed else "Runtime 共享密钥已存在，保持原值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
