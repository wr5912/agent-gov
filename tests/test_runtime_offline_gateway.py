"""离线 gateway 的镜像资产、工作区隔离及初始化预算契约。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from agentscope.workspace import BubblewrapWorkspace
from agentscope_runtime import offline_gateway
from agentscope_runtime.workspace_manager import AgentGovLocalWorkspace, AgentGovWorkspaceManager, harness_digest


@pytest.fixture
def image_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    image = tmp_path / "image"
    image.mkdir()
    wheels = image / "wheels"
    wheels.mkdir()
    (wheels / "agentscope-2.0.8-py3-none-any.whl").write_bytes(b"unit-test-asset")
    for name in ("python", "uv", "rg"):
        binary = image / name
        binary.write_bytes(b"unit-test-binary")
        binary.chmod(0o755)
    monkeypatch.setattr(offline_gateway, "WHEELHOUSE", wheels)
    monkeypatch.setattr(offline_gateway, "PYTHON", image / "python")
    monkeypatch.setattr(offline_gateway, "TOOLS", {"uv": image / "uv", "rg": image / "rg"})
    return image


def test_image_tools_seed_atomically_and_idempotently_without_shared_state(tmp_path: Path, image_assets: Path) -> None:
    workdirs = [tmp_path / name for name in ("first", "second")]
    for workdir in workdirs:
        workdir.mkdir()
        (workdir / "user-data").write_bytes(b"unchanged")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(offline_gateway.prepare_offline_gateway, [workdir] * 8))
        for name in ("uv", "rg"):
            link = workdir / ".agentscope" / "bin" / name
            assert link.is_symlink()
            assert link.readlink() == image_assets / name
            inode = link.lstat().st_ino
            offline_gateway.prepare_offline_gateway(workdir)
            assert link.lstat().st_ino == inode
        assert (workdir / "user-data").read_bytes() == b"unchanged"
        assert not (workdir / ".agentscope" / ".venv").exists()
        assert {path.name for path in (workdir / ".agentscope").iterdir()} == {"bin"}
    assert workdirs[0].stat().st_ino != workdirs[1].stat().st_ino


@pytest.mark.parametrize("ancestor", ["parent", "workdir", ".agentscope", "bin"])
def test_seed_refuses_symlink_ancestors_without_touching_target(tmp_path: Path, image_assets: Path, ancestor: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "private-value"
    marker.write_bytes(b"untouched")
    parent = tmp_path / "parent"
    parent.mkdir()
    workdir = parent / "workdir"
    workdir.mkdir()
    if ancestor == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        workdir = alias / "workdir"
    elif ancestor == "workdir":
        workdir.rmdir()
        workdir.symlink_to(outside, target_is_directory=True)
    elif ancestor == ".agentscope":
        (workdir / ".agentscope").symlink_to(outside, target_is_directory=True)
    else:
        (workdir / ".agentscope").mkdir()
        (workdir / ".agentscope" / "bin").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory ancestors") as error:
        offline_gateway.prepare_offline_gateway(workdir)
    assert str(tmp_path) not in str(error.value)
    assert marker.read_bytes() == b"untouched"
    assert {path.name for path in outside.iterdir()} == {"private-value"}


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_seed_rejects_existing_non_image_tool(tmp_path: Path, image_assets: Path, kind: str) -> None:
    workdir = tmp_path / "workspace"
    bin_dir = workdir / ".agentscope" / "bin"
    bin_dir.mkdir(parents=True)
    target = bin_dir / "uv"
    if kind == "file":
        target.write_bytes(b"do-not-overwrite")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(image_assets / "rg")
    original = target.lstat()
    with pytest.raises(ValueError, match="immutable image"):
        offline_gateway.prepare_offline_gateway(workdir)
    assert target.lstat() == original


@pytest.mark.parametrize("asset", ["wheels", "uv", "rg", "python"])
def test_missing_image_asset_fails_before_workspace_mutation(tmp_path: Path, image_assets: Path, asset: str) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    if asset == "wheels":
        next((image_assets / "wheels").iterdir()).unlink()
    else:
        (image_assets / asset).chmod(0o644)
    with pytest.raises(RuntimeError, match="unavailable"):
        offline_gateway.prepare_offline_gateway(workdir)
    assert list(workdir.iterdir()) == []


def test_ancestor_swap_between_mkdir_and_open_is_not_followed(tmp_path: Path, image_assets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    real_mkdir = os.mkdir

    def racing_mkdir(path, mode=0o777, *, dir_fd=None):
        if path == ".agentscope":
            os.symlink(outside, path, dir_fd=dir_fd, target_is_directory=True)
            raise FileExistsError
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    with pytest.raises(ValueError, match="real directory ancestors"):
        offline_gateway.prepare_offline_gateway(workdir)
    assert list(outside.iterdir()) == []


def test_workspace_public_env_enforces_offline_without_shared_cache(tmp_path: Path) -> None:
    workspace = AgentGovLocalWorkspace(
        harness_root=tmp_path,
        expected_digest="a" * 64,
        host_workdir=str(tmp_path / "state"),
        host_cache_dir=str(tmp_path / "private-cache"),
        sandbox_env={"UV_OFFLINE": "0", "UV_NO_CACHE": "0", "HTTPS_PROXY": "http://proxy.test:8080"},
    )
    assert workspace.env == {
        "HTTPS_PROXY": "http://proxy.test:8080",
        "UV_OFFLINE": "1",
        "UV_NO_CACHE": "1",
        "UV_FIND_LINKS": "file:///usr/local/share/agentgov/gateway-wheels",
        "UV_PYTHON": "/usr/local/bin/python",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    assert workspace.extra_pip == ["agentscope==2.0.8"]
    assert workspace.share_net is True


@pytest.mark.parametrize("stage", ["initialize", "mcp_roster"])
def test_timeout_waits_for_native_operation_cleanup(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str) -> None:
    monkeypatch.setattr(offline_gateway, "INITIALIZE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(offline_gateway, "MCP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(offline_gateway, "prepare_offline_gateway", lambda _: None)
    finished: list[str] = []

    async def operation() -> None:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            finished.append("native_cleanup")

    class Workspace:
        host_workdir = "/not-used"
        initialize = staticmethod(operation)

    async def exercise() -> None:
        preparation = offline_gateway.WorkspacePreparation()
        with pytest.raises(TimeoutError):
            if stage == "initialize":
                await preparation.initialize(cast(BubblewrapWorkspace, Workspace()))
            else:
                await preparation.validate_mcp(operation())
        assert finished == ["native_cleanup"]

    with caplog.at_level(logging.INFO, logger=offline_gateway.__name__):
        asyncio.run(exercise())
    assert f"stage={stage} result=started" in caplog.text
    assert f"stage={stage} result=failed error_type=TimeoutError" in caplog.text


def test_mcp_stage_uses_remaining_total_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offline_gateway, "PREPARATION_TIMEOUT_SECONDS", 0.01)
    started = False

    async def validation() -> None:
        nonlocal started
        started = True

    async def exercise() -> None:
        preparation = offline_gateway.WorkspacePreparation()
        await asyncio.sleep(0.02)
        with pytest.raises(TimeoutError):
            await preparation.validate_mcp(validation())

    asyncio.run(exercise())
    assert started is False


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_is_propagated_without_logging_private_details(caplog: pytest.LogCaptureFixture, cancel: bool) -> None:
    failure = asyncio.CancelledError("private-token") if cancel else RuntimeError("private-token")

    async def validation() -> None:
        raise failure

    async def exercise() -> None:
        with pytest.raises(type(failure)) as error:
            await offline_gateway.WorkspacePreparation().validate_mcp(validation())
        assert error.value is failure

    with caplog.at_level(logging.INFO, logger=offline_gateway.__name__):
        asyncio.run(exercise())
    assert f"error_type={type(failure).__name__}" in caplog.text
    assert "private-token" not in caplog.text


def test_success_preserves_native_initialize_before_mcp_validation(tmp_path: Path, image_assets: Path, caplog: pytest.LogCaptureFixture) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    stages: list[str] = []

    class Workspace:
        host_workdir = str(workdir)

        async def initialize(self) -> None:
            assert (workdir / ".agentscope" / "bin" / "uv").readlink() == image_assets / "uv"
            stages.append("native_initialize")

    async def validation() -> None:
        stages.append("native_mcp")

    async def exercise() -> None:
        preparation = offline_gateway.WorkspacePreparation()
        await preparation.initialize(cast(BubblewrapWorkspace, Workspace()))
        await preparation.validate_mcp(validation())

    with caplog.at_level(logging.INFO, logger=offline_gateway.__name__):
        asyncio.run(exercise())
    assert stages == ["native_initialize", "native_mcp"]
    assert "stage=initialize result=completed" in caplog.text
    assert "stage=mcp_roster result=completed" in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.fixture
def manager_binding(tmp_path: Path, image_assets: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgentGovWorkspaceManager, str]:
    source = tmp_path / "candidates" / "candidate-test" / "workspace"
    source.mkdir(parents=True)
    (source / "agent.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    state = tmp_path / "workspaces"
    state.mkdir()
    manager = AgentGovWorkspaceManager(business_agents_root=tmp_path / "business", candidates_root=source.parent.parent, workspaces_root=state)

    async def initialize(workspace: AgentGovLocalWorkspace) -> None:
        venv = Path(workspace.host_workdir) / ".agentscope" / ".venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "lib").mkdir(exist_ok=True)
        links = {
            "bin/python": str(image_assets / "python"),
            "bin/python3": "python",
            f"bin/python{sys.version_info.major}.{sys.version_info.minor}": "python",
            "lib64": "lib",
        }
        for path, target in links.items():
            if not (venv / path).is_symlink():
                (venv / path).symlink_to(target)

    async def close(_: AgentGovLocalWorkspace) -> None:
        return None

    async def validate_mcp(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(AgentGovLocalWorkspace, "initialize", initialize)
    monkeypatch.setattr(AgentGovLocalWorkspace, "close", close)
    monkeypatch.setattr(AgentGovWorkspaceManager, "_validate_live_mcp_tools", validate_mcp)
    return manager, f"candidate-test--v-{harness_digest(source)}"


def test_seeded_workspace_reuses_sdk_links_for_warm_session_and_restart(manager_binding: tuple[AgentGovWorkspaceManager, str]) -> None:
    manager, workspace_id = manager_binding

    async def exercise() -> None:
        first = await manager.get_workspace("user", "agent", "first", workspace_id)
        second = await manager.get_workspace("user", "agent", "second", workspace_id)
        assert second is first
        await manager.close_all()
        restarted = await manager.get_workspace("user", "agent", "third", workspace_id)
        assert restarted is not first
        await manager.close_all()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "path,target", [(".agentscope/bin/uv", "/tmp/unapproved"), (".agentscope/.venv/bin/python", "/tmp/unapproved"), ("user-link", "/etc/passwd")]
)
def test_warm_workspace_still_rejects_non_sdk_links(manager_binding: tuple[AgentGovWorkspaceManager, str], path: str, target: str) -> None:
    manager, workspace_id = manager_binding

    async def exercise() -> None:
        first = await manager.get_workspace("user", "agent", "first", workspace_id)
        link = Path(first.host_workdir) / path
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        with pytest.raises(ValueError, match="unapproved symlink"):
            await manager.get_workspace("user", "agent", "second", workspace_id)
        await manager.close_all()

    asyncio.run(exercise())


def test_mcp_cancel_closes_new_workspace_before_propagation(manager_binding: tuple[AgentGovWorkspaceManager, str], monkeypatch: pytest.MonkeyPatch) -> None:
    manager, workspace_id = manager_binding
    closed: list[str] = []

    async def close(workspace: AgentGovLocalWorkspace) -> None:
        closed.append(workspace.workspace_id)

    async def cancelled(*args, **kwargs) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(AgentGovLocalWorkspace, "close", close)
    monkeypatch.setattr(AgentGovWorkspaceManager, "_validate_live_mcp_tools", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.get_workspace("user", "agent", "first", workspace_id))
    assert closed == [workspace_id]


def test_native_sdk_initialize_timeout_terminates_process_before_close(tmp_path: Path, image_assets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """仅替换操作系统启动边界；SDK 的 initialize、取消进程组、close 均真实执行。"""

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    workspace = BubblewrapWorkspace(host_workdir=str(workdir), host_cache_dir=str(tmp_path / "cache"), gateway_port=None)
    spawn = asyncio.create_subprocess_exec
    which = shutil.which
    native_close = workspace.close
    processes: list[asyncio.subprocess.Process] = []
    closed: list[bool] = []

    async def launch_local_probe(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    async def close() -> None:
        assert processes and all(process.returncode is not None for process in processes)
        await native_close()
        closed.append(True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch_local_probe)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else which(name))
    monkeypatch.setattr(workspace, "close", close)
    monkeypatch.setattr(offline_gateway, "INITIALIZE_TIMEOUT_SECONDS", 0.1)

    async def exercise() -> None:
        with pytest.raises(TimeoutError):
            await offline_gateway.WorkspacePreparation().initialize(workspace)
        assert closed == [True]
        assert workspace.is_alive is False
        with pytest.raises(RuntimeError, match="no active backend"):
            workspace.get_backend()

    asyncio.run(exercise())


def test_runtime_link_scan_rejects_unreadable_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = tmp_path / ".agentgov-runtime-state" / "unreadable"
    blocked.mkdir(parents=True)
    (blocked / "unsafe-link").symlink_to("/etc/passwd")
    scandir = os.scandir

    def unreadable_scan(path):
        if os.fspath(path) == str(blocked):
            raise PermissionError("private-directory-detail")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable_scan)
    with pytest.raises(ValueError, match="Runtime state link validation failed") as error:
        offline_gateway.validate_runtime_state_links(tmp_path)
    assert "private-directory-detail" not in str(error.value)
