"""使用镜像只读资产准备 SDK gateway，保留每个 Workspace 的独立可写环境。"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import sys
from collections.abc import Awaitable
from contextlib import suppress
from pathlib import Path
from typing import TypeAlias

from agentscope.workspace import BubblewrapWorkspace

logger = logging.getLogger(__name__)
WHEELHOUSE = Path("/usr/local/share/agentgov/gateway-wheels")
PYTHON = Path("/usr/local/bin/python")
TOOLS = {"uv": Path("/usr/local/bin/uv"), "rg": Path("/usr/bin/rg")}
INITIALIZE_TIMEOUT_SECONDS = 60.0
MCP_TIMEOUT_SECONDS = 30.0
PREPARATION_TIMEOUT_SECONDS = 90.0
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GatewayEnvironment: TypeAlias = dict[str, str]


def offline_gateway_env() -> GatewayEnvironment:
    """仅使用 uv 公共配置；Python、依赖和工具均由镜像提供。"""

    return {
        "UV_OFFLINE": "1",
        "UV_NO_CACHE": "1",
        "UV_FIND_LINKS": WHEELHOUSE.as_uri(),
        "UV_PYTHON": str(PYTHON),
        "UV_PYTHON_DOWNLOADS": "never",
    }


def _open_real_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Gateway workdir must be an absolute real directory")
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory(parent: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode=0o700, dir_fd=parent)
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)


def _ensure_tool_link(directory: int, name: str, target: Path) -> None:
    # symlink 是原子新增；已存在项只验证，不覆盖，也不跟随其目标写入。
    try:
        os.symlink(str(target), name, dir_fd=directory)
    except FileExistsError:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISLNK(existing.st_mode) or os.readlink(name, dir_fd=directory) != str(target):
            raise ValueError("Gateway tool link does not match the immutable image") from None


def prepare_offline_gateway(host_workdir: Path) -> None:
    """为固定 SDK 的公开 Workspace 准备工具，不预造 gateway 脚本或共享 venv。"""

    if not WHEELHOUSE.is_dir() or not any(WHEELHOUSE.glob("*.whl")):
        raise RuntimeError("Offline gateway wheelhouse is unavailable")
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in (PYTHON, *TOOLS.values())):
        raise RuntimeError("Offline gateway executable is unavailable")
    descriptors: list[int] = []
    try:
        descriptors.append(_open_real_directory(host_workdir))
        descriptors.append(_open_child_directory(descriptors[-1], ".agentscope"))
        descriptors.append(_open_child_directory(descriptors[-1], "bin"))
        for name, target in TOOLS.items():
            _ensure_tool_link(descriptors[-1], name, target)
    except OSError:
        raise ValueError("Gateway workdir must contain only real directory ancestors") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_runtime_state_links(root: Path) -> None:
    """运行态仅放行 SDK 固定工具和 venv 链接；Harness 仍独立禁止所有链接。"""

    def reject_scan_error(error: OSError) -> None:
        raise error

    gateway = Path(".agentgov-runtime-state/.agentscope")
    venv = gateway / ".venv"
    allowed = {gateway / "bin" / name: {str(target)} for name, target in TOOLS.items()}
    allowed.update(
        {
            venv / "bin/python": {str(PYTHON), str(PYTHON.resolve())},
            venv / "bin/python3": {"python"},
            venv / f"bin/python{sys.version_info.major}.{sys.version_info.minor}": {"python"},
            venv / "lib64": {"lib"},
        },
    )
    try:
        os.close(_open_real_directory(root))
        for current, directories, files in os.walk(root, followlinks=False, onerror=reject_scan_error):
            for name in (*directories, *files):
                entry = Path(current) / name
                if entry.is_symlink() and os.readlink(entry) not in allowed.get(entry.relative_to(root), set()):
                    raise ValueError("Runtime state contains an unapproved symlink")
    except OSError:
        raise ValueError("Runtime state link validation failed") from None


class WorkspacePreparation:
    """初始化与 MCP roster 校验共用总预算；取消等待 SDK 原生资源清理。"""

    def __init__(self) -> None:
        self._deadline = asyncio.get_running_loop().time() + PREPARATION_TIMEOUT_SECONDS

    async def initialize(self, workspace: BubblewrapWorkspace) -> None:
        async def initialize() -> None:
            prepare_offline_gateway(Path(workspace.host_workdir))
            await workspace.initialize()

        await self._run("initialize", initialize(), INITIALIZE_TIMEOUT_SECONDS)

    async def validate_mcp(self, validation: Awaitable[None]) -> None:
        await self._run("mcp_roster", validation, MCP_TIMEOUT_SECONDS)

    async def _run(self, stage: str, operation: Awaitable[None], stage_timeout: float) -> None:
        remaining = max(0.0, self._deadline - asyncio.get_running_loop().time())
        logger.info("workspace_preparation stage=%s result=started", stage)
        try:
            await asyncio.wait_for(operation, timeout=min(remaining, stage_timeout))
        except BaseException as exc:
            logger.warning("workspace_preparation stage=%s result=failed error_type=%s", stage, type(exc).__name__)
            raise
        logger.info("workspace_preparation stage=%s result=completed", stage)
