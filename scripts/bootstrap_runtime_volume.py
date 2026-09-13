#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import MutableMapping
from contextlib import suppress
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime.protected_business_agents import BUILTIN_BUSINESS_AGENT_IDS  # noqa: E402

from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings  # noqa: E402
from scripts.container_acceptance_inputs import AcceptanceError  # noqa: E402
from scripts.container_acceptance_runtime_root import verify_runtime_bootstrap_authorization  # noqa: E402

DEFAULT_BOOTSTRAP_DIR = Path("docker/runtime-bootstrap")
DEFAULT_ENV_FILE = Path("docker/.env")
CONTAINER_RUNTIME_VOLUME_ROOT = Path(pwd.getpwuid(os.geteuid()).pw_dir) / "volume-agent-gov"
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-gov")
RUNTIME_VOLUME_MODES = {"container", "local-debug"}
_RUNTIME_ENV_FILE_MODES = {
    ".env": "container",
    ".env.example": "container",
    ".env.local-debug": "local-debug",
    ".env.local-debug.example": "local-debug",
}
RUNTIME_DATA_DIRS = (
    "data/business-agents",
    "data/uploads",
    "data/outputs",
    "data/outputs/reports",
    "data/.agent-testing/sessions",
    "data/.agent-testing/runs",
    "agentscope-runtime/data",
    "agentscope-runtime/workspaces",
    "agentscope-runtime/candidates",
    "langfuse/postgres",
    "langfuse/clickhouse/data",
    "langfuse/clickhouse/logs",
    "langfuse/redis",
    "langfuse/minio",
)


class BootstrapResult(TypedDict):
    created_dirs: list[str]
    copied: list[str]
    skipped_existing: list[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_env_value(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return env.get(match.group(1), "")

    expanded = re.sub(r"\$\{([^}]+)\}", replace, value.strip())
    if expanded == "~" or expanded.startswith("~/"):
        expanded = env["HOME"] + expanded[1:]
    return expanded


def _load_env_file(path: Path) -> MutableMapping[str, str]:
    env: dict[str, str] = {"HOME": Path(pwd.getpwuid(os.geteuid()).pw_dir).as_posix()}
    if not path.exists():
        return env
    for binding in parse_selected_env_bindings(path):
        if binding.key is None:
            continue
        env[binding.key] = _expand_env_value(binding.value or "", env)
    return env


def load_runtime_env(path: Path) -> MutableMapping[str, str]:
    """Load the selected runtime env file without mutating process environment."""

    return _load_env_file(path)


def _runtime_root_for_mode(mode: str | None) -> Path:
    normalized = (mode or "container").strip()
    if normalized not in RUNTIME_VOLUME_MODES:
        raise ValueError(f"Unsupported runtime volume mode={normalized!r}; expected container or local-debug")
    if normalized == "local-debug":
        return LOCAL_DEBUG_RUNTIME_VOLUME_ROOT
    return CONTAINER_RUNTIME_VOLUME_ROOT


def _runtime_volume_mode_for_env_file(env_file: Path) -> str | None:
    return _RUNTIME_ENV_FILE_MODES.get(env_file.name)


def resolve_runtime_volume_mode(env_file: Path, runtime_root: Path, runtime_volume_mode: str | None = None) -> str:
    if runtime_volume_mode:
        return runtime_volume_mode
    mode = _runtime_volume_mode_for_env_file(env_file)
    if mode:
        return mode
    if runtime_root.resolve() == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT.resolve():
        return "local-debug"
    return "container"


def resolve_runtime_root(cli_value: str | None, env_file: Path, runtime_volume_mode: str | None = None) -> Path:
    env = _load_env_file(env_file)
    if cli_value:
        value = _expand_env_value(cli_value, dict(env))
        return Path(value).resolve()
    value = env.get("HOST_RUNTIME_VOLUME_ROOT")
    if value:
        return Path(value).expanduser().resolve()
    # Legacy compatibility only; official env files derive the mode from their filename.
    mode = runtime_volume_mode or env.get("RUNTIME_VOLUME_MODE") or _runtime_volume_mode_for_env_file(env_file)
    return _runtime_root_for_mode(mode).resolve()


def resolve_bootstrap_dir(cli_value: Path | None, env_file: Path, env_base_dir: Path | None = None) -> Path:
    if cli_value is not None:
        return cli_value.expanduser().resolve()
    env = _load_env_file(env_file)
    configured = str(env.get("RUNTIME_BOOTSTRAP_HOST_DIR") or "").strip()
    if not configured:
        return (_repo_root() / DEFAULT_BOOTSTRAP_DIR).resolve()
    candidate = Path(_expand_env_value(configured, dict(env)))
    if not candidate.is_absolute():
        candidate = (env_base_dir or env_file.expanduser().resolve().parent) / candidate
    return candidate.resolve()


def require_authorized_runtime_root(
    runtime_root: Path,
    runtime_volume_mode: str,
    environ: dict[str, str] | None = None,
) -> None:
    expected = _runtime_root_for_mode(runtime_volume_mode).resolve()
    if runtime_root != expected:
        if runtime_volume_mode != "container":
            raise ValueError(f"Runtime root 必须精确为当前模式专用目录: {expected}")
        try:
            verify_runtime_bootstrap_authorization(runtime_root, environ)
        except AcceptanceError as exc:
            raise ValueError(f"Runtime root 必须精确为当前模式专用目录: {expected}；隔离验收授权无效") from exc
        return
    current = expected
    while not current.exists():
        if current.parent == current:
            raise ValueError("Runtime root 缺少可验证父目录")
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise ValueError("Runtime root 父链不得含符号链接或非目录")


def _initialize_builtin_business_agents(
    *,
    runtime_root: Path,
    bootstrap_dir: Path,
    dry_run: bool,
    copied: list[str],
    skipped: list[str],
) -> None:
    """只初始化显式内置业务 Agent；整个运行态 Workspace 已存在时绝不回灌。"""

    builtins_root = bootstrap_dir / "business-agents"
    if builtins_root.is_symlink() or not builtins_root.is_dir():
        raise ValueError(f"Runtime bootstrap business-agents root must be a real directory: {builtins_root}")
    actual_ids = {entry.name for entry in builtins_root.iterdir() if entry.is_dir() and not entry.is_symlink()}
    if actual_ids != set(BUILTIN_BUSINESS_AGENT_IDS):
        raise ValueError(
            "Runtime bootstrap built-in business Agents do not match the declared set: "
            f"expected={sorted(BUILTIN_BUSINESS_AGENT_IDS)}, actual={sorted(actual_ids)}"
        )
    for agent_id in sorted(BUILTIN_BUSINESS_AGENT_IDS):
        workspace = builtins_root / agent_id / "workspace"
        if workspace.is_symlink() or not workspace.is_dir() or not any(workspace.iterdir()):
            raise ValueError(f"Built-in business Agent Workspace is missing, unsafe, or empty: {agent_id}")
        rel = Path("data") / "business-agents" / agent_id / "workspace"
        _copy_missing(
            workspace,
            runtime_root / rel,
            rel_path=rel,
            dry_run=dry_run,
            copied=copied,
            skipped=skipped,
        )


def _same_file_bytes(src: Path, dest: Path) -> bool:
    with src.open("rb") as source, dest.open("rb") as target:
        while True:
            source_chunk = source.read(1024 * 1024)
            if source_chunk != target.read(1024 * 1024):
                return False
            if not source_chunk:
                return True


def _replace_governor_file(src: Path, dest: Path, existing: os.stat_result | None, source_mode: int) -> None:
    """在同目录原子更新平台配置；已有文件的 owner/mode 是卷侧权威。"""

    temporary: Path | None = None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{dest.name}.bootstrap-", dir=dest.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, src.open("rb") as source:
            shutil.copyfileobj(source, output)
            if existing is not None:
                temporary_stat = os.fstat(output.fileno())
                if (temporary_stat.st_uid, temporary_stat.st_gid) != (existing.st_uid, existing.st_gid):
                    os.fchown(output.fileno(), existing.st_uid, existing.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(existing.st_mode if existing is not None else source_mode))
            output.flush()
            os.fsync(output.fileno())
        try:
            current = dest.lstat()
        except FileNotFoundError:
            current = None
        if (existing is None) != (current is None) or (
            existing is not None
            and current is not None
            and (existing.st_dev, existing.st_ino, existing.st_mode, existing.st_uid, existing.st_gid)
            != (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid)
        ):
            raise ValueError(f"Runtime bootstrap target changed during copy: {dest}")
        os.replace(temporary, dest)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_missing(
    src: Path,
    dest: Path,
    *,
    rel_path: Path,
    dry_run: bool,
    copied: list[str],
    skipped: list[str],
) -> None:
    source_mode = src.lstat().st_mode
    if stat.S_ISLNK(source_mode) or not (stat.S_ISDIR(source_mode) or stat.S_ISREG(source_mode)):
        raise ValueError(f"Runtime bootstrap entry must be a regular file or directory: {rel_path.as_posix()}")
    # 业务 Agent workspace 是 Git 版本源。只有整个 workspace 不存在时才播种出生配置；
    # 已存在 workspace 不逐文件 fill-missing，避免把版本中有意删除的文件复活。
    parts = rel_path.parts
    is_business_workspace_root = len(parts) == 4 and parts[0] == "data" and parts[1] == "business-agents" and parts[3] == "workspace"
    if is_business_workspace_root and stat.S_ISDIR(source_mode) and dest.exists():
        skipped.append(dest.as_posix())
        return
    # governor-workspace 是平台治理配置，不是用户在卷里积累的业务优化态；每次初始化都覆盖
    # 初始化源中存在的文件，但不移除卷内私有文件。业务 Agent Workspace 走上面的整体跳过。
    overwrite = bool(parts and parts[0] == "governor-workspace")
    if stat.S_ISDIR(source_mode):
        if overwrite and (dest.exists() or dest.is_symlink()):
            if not stat.S_ISDIR(dest.lstat().st_mode):
                raise ValueError(f"Runtime bootstrap governor target must be a real directory: {rel_path.as_posix()}")
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_missing(
                child,
                dest / child.name,
                rel_path=rel_path / child.name,
                dry_run=dry_run,
                copied=copied,
                skipped=skipped,
            )
        return
    existing = None
    if overwrite:
        with suppress(FileNotFoundError):
            existing = dest.lstat()
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"Runtime bootstrap governor target must be a regular file: {rel_path.as_posix()}")
        if existing is not None and _same_file_bytes(src, dest):
            skipped.append(dest.as_posix())
            return
    if dest.exists() and not overwrite:
        skipped.append(dest.as_posix())
        return
    copied.append(dest.as_posix())
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        _replace_governor_file(src, dest, existing, source_mode)
    else:
        shutil.copy2(src, dest)


def bootstrap_runtime_volume(
    *,
    runtime_root: Path,
    bootstrap_dir: Path,
    runtime_volume_mode: str = "container",
    env: MutableMapping[str, str] | None = None,
    dry_run: bool = False,
) -> BootstrapResult:
    del runtime_volume_mode
    copied: list[str] = []
    skipped: list[str] = []
    created_dirs: list[str] = []
    for rel in RUNTIME_DATA_DIRS:
        path = runtime_root / rel
        created_dirs.append(path.as_posix())
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
    if not dry_run and os.geteuid() == 0:
        values = env or {}
        raw_uid = str(values.get("AGENT_GOV_RUNTIME_UID", "1000"))
        raw_gid = str(values.get("AGENT_GOV_RUNTIME_GID", "1000"))
        if not raw_uid.isdecimal() or not raw_gid.isdecimal():
            raise ValueError("AGENT_GOV_RUNTIME_UID/GID must be decimal integers")
        runtime_uid, runtime_gid = int(raw_uid), int(raw_gid)
        for relative in ("agentscope-runtime", "agentscope-runtime/data", "agentscope-runtime/workspaces", "agentscope-runtime/candidates"):
            os.chown(runtime_root / relative, runtime_uid, runtime_gid, follow_symlinks=False)
    if bootstrap_dir.is_symlink() or not bootstrap_dir.is_dir():
        raise ValueError(f"Runtime bootstrap source must be a real directory: {bootstrap_dir}")
    governor_workspace = bootstrap_dir / "governor-workspace"
    if governor_workspace.is_symlink() or not governor_workspace.is_dir() or not any(governor_workspace.iterdir()):
        raise ValueError("Runtime bootstrap governor Workspace is missing, unsafe, or empty")
    _copy_missing(
        governor_workspace,
        runtime_root / "governor-workspace",
        rel_path=Path("governor-workspace"),
        dry_run=dry_run,
        copied=copied,
        skipped=skipped,
    )
    _initialize_builtin_business_agents(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap_dir,
        dry_run=dry_run,
        copied=copied,
        skipped=skipped,
    )

    return {
        "created_dirs": created_dirs,
        "copied": copied,
        "skipped_existing": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap runtime volume from docker/runtime-bootstrap.")
    parser.add_argument("--runtime-root", help="Host runtime root. Defaults to HOST_RUNTIME_VOLUME_ROOT or the selected runtime volume mode.")
    parser.add_argument(
        "--runtime-volume-mode",
        choices=sorted(RUNTIME_VOLUME_MODES),
        help="Default runtime root mode when HOST_RUNTIME_VOLUME_ROOT is not set: container=~/volume-agent-gov, local-debug=/tmp/local-debug-volume-agent-gov.",
    )
    parser.add_argument("--bootstrap-dir", type=Path)
    parser.add_argument("--env-base-dir", type=Path)
    parser.add_argument("--env-file", type=Path, default=_repo_root() / DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    runtime_volume_mode = resolve_runtime_volume_mode(args.env_file, runtime_root, args.runtime_volume_mode)
    require_authorized_runtime_root(runtime_root, runtime_volume_mode)
    env = _load_env_file(args.env_file)
    bootstrap_dir = resolve_bootstrap_dir(args.bootstrap_dir, args.env_file, args.env_base_dir)
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        bootstrap_dir=bootstrap_dir,
        runtime_volume_mode=runtime_volume_mode,
        env=env,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(
            json.dumps(
                {
                    "runtime_root": runtime_root.as_posix(),
                    "runtime_volume_mode": runtime_volume_mode,
                    "bootstrap_dir": bootstrap_dir.as_posix(),
                    **result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
