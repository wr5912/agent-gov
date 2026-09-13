"""已部署浏览器验收的私有回执、身份复验与只读前置条件。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias, cast

from scripts import agentscope_atomic_cutover as cutover
from scripts.agentscope_atomic_cutover_types import DockerDaemonIdentity
from scripts.container_acceptance_identity import PinnedFileIdentity, verify_sealed_file, write_exclusive_file
from scripts.selected_env_browser_toolchain import BROWSER_TOOLCHAIN_ENV, verify_browser_toolchain
from scripts.selected_env_operation_contract import OperationEnvironment, SelectedEnvError, StackImageIds

OPERATION = "ui-playground-deployed-smoke"
SCOPE = "deployed_playground_two_turn_refresh"
CONTEXT_PATH_ENV = "AGENTGOV_DEPLOYED_CONTEXT_PATH"
CONTEXT_IDENTITY_ENV = "AGENTGOV_DEPLOYED_CONTEXT_IDENTITY"
API_KEY_ENV = "AGENTGOV_DEPLOYED_API_KEY"
BrowserMetadata: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class DeployedBrowserContext:
    acceptance_id: str
    source_sha256: str
    selected_env_sha256: str
    version: str
    ui_base: str
    api_base: str
    image_ids: StackImageIds
    container_ids: tuple[str, ...]
    runner_pid: int
    daemon_identity: DockerDaemonIdentity
    browser_toolchain_sha256: str


def require_live_opt_in(environ: Mapping[str, str]) -> None:
    if environ.get("REQUIRE_LIVE_RUNTIME") != "1":
        raise SelectedEnvError("部署浏览器验收必须显式 REQUIRE_LIVE_RUNTIME=1")


def deployment_urls(values: Mapping[str, str]) -> tuple[str, str]:
    ports = (values.get("FRONTEND_HOST_PORT", "50401"), values.get("HOST_PORT", "50400"))
    if any(re.fullmatch(r"504\d\d", value) is None for value in ports) or ports[0] == ports[1]:
        raise SelectedEnvError("部署浏览器验收要求独立的 50400–50499 宿主机端口")
    for key in ("FRONTEND_BIND_IP", "API_BIND_IP"):
        if values.get(key, "127.0.0.1") != "127.0.0.1":
            raise SelectedEnvError("部署浏览器验收仅允许本机 loopback")
    ui_base, api_base = (f"http://localhost:{port}" for port in ports)
    if values.get("FRONTEND_RUNTIME_API_BASE", api_base) != api_base:
        raise SelectedEnvError("前端 Runtime API 地址与所选本机 API 端口不一致")
    return ui_base, api_base


def require_no_active_work(snapshot: Path) -> None:
    """复用运维只读全量计数，避免列表 API 的分页上限漏掉活动任务。"""
    try:
        runtime_root = cutover.resolve_runtime_root(snapshot, require_exists=True)
        database = cutover._database_path(runtime_root, snapshot)
        require_idle_database(database)
    except (cutover.CutoverError, OSError, sqlite3.Error) as exc:
        raise SelectedEnvError("无法只读核验部署环境活动任务") from exc


def require_idle_database(database: Path) -> None:
    if database.is_symlink() or not database.is_file():
        raise SelectedEnvError("部署浏览器验收要求现有非符号链接 Runtime 数据库")
    require_idle_counts(cutover.active_work_counts(database))


def require_idle_counts(counts: cutover.ActiveWorkCounts) -> None:
    if any(value != 0 for value in counts.values()):
        raise SelectedEnvError("当前部署存在活动 run、会话、测试或发布；拒绝 force-recreate")


def seal_context(directory: Path, context: DeployedBrowserContext) -> OperationEnvironment:
    directory.mkdir(mode=0o700)
    path = directory / "context.json"
    identity = write_exclusive_file(path, json.dumps(asdict(context), sort_keys=True).encode(), error_type=SelectedEnvError, label="部署浏览器回执")
    path.chmod(0o400)
    directory.chmod(0o500)
    return {CONTEXT_PATH_ENV: str(path), CONTEXT_IDENTITY_ENV: json.dumps(identity, sort_keys=True)}


def load_context(environ: Mapping[str, str]) -> DeployedBrowserContext:
    require_live_opt_in(environ)
    try:
        path = Path(environ[CONTEXT_PATH_ENV])
        parent = path.parent.lstat()
        if not path.is_absolute() or not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o500:
            raise SelectedEnvError("部署浏览器回执目录未封存")
        if parent.st_uid != os.getuid():
            raise SelectedEnvError("部署浏览器回执不属于当前用户")
        identity = cast(PinnedFileIdentity, json.loads(environ[CONTEXT_IDENTITY_ENV]))
        verify_sealed_file(path, identity, mode=0o400, error_type=SelectedEnvError, label="部署浏览器回执")
        values = json.loads(path.read_bytes())
        context = DeployedBrowserContext(**values)
        if context.browser_toolchain_sha256 != hashlib.sha256(environ[BROWSER_TOOLCHAIN_ENV].encode()).hexdigest():
            raise SelectedEnvError("浏览器依赖身份脱离部署回执")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise SelectedEnvError("缺少有效的部署浏览器私有回执") from exc
    return context


def stack_container_ids(snapshot: Path, source_root: Path, environ: dict[str, str]) -> tuple[str, ...]:
    from scripts import run_selected_env_operation as runner

    command = [*runner._compose(snapshot, source_root, langfuse=True), "ps", "--all", "--quiet"]
    values = tuple(sorted(runner._run_output(command, environ).splitlines()))
    if not values or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
        raise SelectedEnvError("部署容器身份清单无效")
    return values


def _require_owned_process(context: DeployedBrowserContext, source_root: Path, node: str) -> None:
    """guard 必须由当前仍持有锁的 runner 的直属 Node 子进程调用。"""
    try:
        parent = os.getppid()
        node_command = Path(f"/proc/{parent}/cmdline").read_bytes().split(b"\0")
        parent_status = Path(f"/proc/{parent}/status").read_text()
        if f"PPid:\t{context.runner_pid}\n" not in parent_status:
            raise SelectedEnvError("部署浏览器回执不属于当前活动 runner")
        expected_node = [os.fsencode(node), os.fsencode(source_root / "scripts/verify_playground_deployed.mjs")]
        if node_command[:2] != expected_node:
            raise SelectedEnvError("部署浏览器调用入口不匹配冻结 Node/script")
        command = Path(f"/proc/{context.runner_pid}/cmdline").read_bytes().split(b"\0")
        expected_runner = os.fsencode(source_root / "scripts/run_selected_env_operation.py")
        if expected_runner not in command or os.fsencode(OPERATION) not in command:
            raise SelectedEnvError("部署浏览器回执所属 runner 已失效")
    except OSError as exc:
        raise SelectedEnvError("无法核验部署浏览器进程所有权") from exc


def verify_context(environ: dict[str, str], *, require_node_parent: bool) -> BrowserMetadata:
    from scripts import run_selected_env_operation as runner
    from scripts import selected_env_source_snapshot as source_snapshot

    context = load_context(environ)
    source_root = runner._verified_command_root(environ)
    snapshot = source_snapshot.verify_operation_input(environ)
    if snapshot is None or context.source_sha256 != environ.get("AGENTGOV_SOURCE_ARTIFACT_SHA256"):
        raise SelectedEnvError("浏览器回执与冻结源码/env 不一致")
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != context.selected_env_sha256:
        raise SelectedEnvError("浏览器回执与冻结 selected.env 摘要不一致")
    toolchain = verify_browser_toolchain(environ)
    if toolchain["source_root"] != str(source_root):
        raise SelectedEnvError("浏览器工具链未绑定同一冻结源码")
    if require_node_parent:
        _require_owned_process(context, source_root, toolchain["node"]["path"])
    if not require_node_parent:
        runner._local_daemon_support(environ).verify(environ, context.daemon_identity)
        runner._verify_stack_images(
            environ,
            snapshot,
            context.version,
            context.source_sha256,
            source_root=source_root,
            langfuse=True,
            running=True,
            expected_ids=context.image_ids,
        )
        if stack_container_ids(snapshot, source_root, environ) != tuple(context.container_ids):
            raise SelectedEnvError("浏览器验收期间部署容器被替换")
    runner._verify_frozen_stage_postconditions(snapshot, source_root, environ, context.source_sha256)
    package = toolchain["artifacts"]["playwright-package"]
    return {
        "scope": SCOPE,
        "ui_base": context.ui_base,
        "api_base": context.api_base,
        "agent_id": "security-operations-expert",
        "acceptance_id": context.acceptance_id,
        "source_sha256": context.source_sha256,
        "version": context.version,
        "browsers": ["chromium", "firefox"],
        "playwright_module_path": str(Path(package["path"]) / package["entrypoint"]),
    }
