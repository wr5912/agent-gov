"""复用 selected-env 冻结边界，执行显式停服的 Workspace 手工维护。"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from scripts import agentscope_atomic_cutover as cutover
from scripts import selected_env_source_snapshot as source_snapshot
from scripts.agentscope_atomic_cutover_daemon import CutoverDaemonSupport
from scripts.agentscope_atomic_cutover_types import DockerDaemonIdentity
from scripts.runtime_workspace_gc import GovernanceReferences, WorkspacePlan, plan_workspaces, quarantine_workspaces, read_governance_references
from scripts.selected_env_deployed_context import require_idle_database
from scripts.selected_env_operation_contract import WORKSPACE_GC_APPLY_OPERATION, OperationEnvironment, SelectedEnvError

if TYPE_CHECKING:
    from scripts.runtime_workspace_gc_inventory import NativeInventory


class ContainerHealth(TypedDict):
    Status: str


class ContainerState(TypedDict):
    Running: bool
    Paused: bool
    Restarting: bool
    Health: NotRequired[ContainerHealth]


class ContainerMount(TypedDict):
    Type: str
    Source: str
    Destination: str


class MaintenanceContainer(TypedDict):
    id: str
    image: str
    user: str
    project: str
    service: str
    state: ContainerState
    mounts: list[ContainerMount]
    database: list[str]


_INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},"state":{{json .State}},'
    '"user":{{json .Config.User}},"mounts":{{json .Mounts}},'
    '"database":[{{range .Config.Env}}{{if eq (index (split . "=") 0) "AGENTSCOPE_RUNTIME_DATABASE_URL"}}{{json .}}{{end}}{{end}}],'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)


@dataclass(frozen=True)
class RecoveryCommands:
    """只保留维护前已核验的目标与恢复动作，不接受 Compose 选择或删除命令。"""

    api_id: str
    runtime_id: str
    api_was_running: bool
    runtime_was_running: bool

    def validate(self, command: list[str]) -> None:
        if self.api_id == self.runtime_id or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (self.api_id, self.runtime_id)):
            raise SelectedEnvError("维护恢复缺少两个精确且独立的原容器 ID")
        allowed = {("docker", "info", "--format", "{{json .ID}}")}
        allowed.update(("docker", "inspect", "--format", "{{json .State}}", target) for target in (self.api_id, self.runtime_id))
        if self.api_was_running:
            allowed.add(("docker", "unpause", self.api_id))
        if self.runtime_was_running:
            allowed.add(("docker", "start", self.runtime_id))
        if tuple(command) not in allowed:
            raise SelectedEnvError("维护恢复仅允许原容器的状态读取及预绑定恢复动作")


@dataclass(frozen=True)
class RecoveryBoundary:
    commands: RecoveryCommands
    binary: Path
    binary_digest: str
    docker_config: Path
    daemon_identity: DockerDaemonIdentity

    def environment(self) -> OperationEnvironment:
        return {"DOCKER_HOST": self.daemon_identity["endpoint"], "DOCKER_CONFIG": str(self.docker_config)}

    def _raw_output(self, command: list[str], _label: str, *, capture: bool = False, child_env: Mapping[str, str] | None = None) -> str:
        self.commands.validate(command)
        if not capture or child_env != self.environment():
            raise SelectedEnvError("维护恢复命令环境脱离原 daemon")
        verify_recovery_execution_boundary(self.binary, self.binary_digest, self.docker_config)
        bound = [str(self.binary), "--host", self.daemon_identity["endpoint"], "--config", str(self.docker_config), *command[1:]]
        try:
            return subprocess.run(bound, env=self.environment(), cwd=self.docker_config, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise SelectedEnvError("原 Docker CLI/daemon 恢复命令不可用，请人工核验原服务状态") from exc
        finally:
            verify_recovery_execution_boundary(self.binary, self.binary_digest, self.docker_config)

    def output(self, command: list[str]) -> str:
        self.commands.validate(command)
        daemon = CutoverDaemonSupport(error_type=SelectedEnvError, run_command=self._raw_output)
        try:
            daemon.verify(self.environment(), self.daemon_identity)
            try:
                return self._raw_output(command, "恢复原维护服务", capture=True, child_env=self.environment())
            finally:
                daemon.verify(self.environment(), self.daemon_identity)
        except (OSError, SelectedEnvError) as exc:
            raise SelectedEnvError("原 Docker CLI/daemon 身份不可核验或执行失败，请人工恢复原维护服务") from exc


def verify_recovery_execution_boundary(binary: Path, digest: str, docker_config: Path) -> None:
    # 复用原 binary 校验；不让已损坏的 source/env/Python/Compose 插件阻止恢复。
    try:
        source_snapshot._require_readonly_binary(binary, digest, "维护恢复 Docker CLI")
        source_snapshot._require_private_directory(docker_config, mode=0o500, label="维护恢复 Docker config")
        config_file = docker_config / "config.json"
        if config_file.exists() or config_file.is_symlink():
            raise SelectedEnvError("维护恢复 Docker config 已改变")
    except (OSError, SelectedEnvError) as exc:
        raise SelectedEnvError("维护恢复 Docker 执行边界不可用，请人工核验原服务状态") from exc


def _prepare_recovery(api: MaintenanceContainer, runtime: MaintenanceContainer, child_env: dict[str, str]) -> RecoveryBoundary:
    from scripts import run_selected_env_operation as runner

    runner._verified_command_root(child_env)
    commands = RecoveryCommands(api["id"], runtime["id"], api["state"]["Running"], runtime["state"]["Running"])
    commands.validate(["docker", "info", "--format", "{{json .ID}}"])
    return RecoveryBoundary(
        commands,
        Path(child_env[source_snapshot.DOCKER_CLI_ENV]),
        child_env[source_snapshot.DOCKER_CLI_DIGEST_ENV],
        Path(child_env["DOCKER_CONFIG"]),
        runner._capture_local_daemon(child_env),
    )


def _container(base: list[str], service: str, child_env: dict[str, str], project: str) -> MaintenanceContainer:
    from scripts import run_selected_env_operation as runner

    ids = runner._run_output([*base, "ps", "--all", "--quiet", service], child_env).splitlines()
    if len(ids) != 1 or re.fullmatch(r"[0-9a-f]{64}", ids[0]) is None:
        raise SelectedEnvError("维护要求所选 Compose service 恰有一个确定容器")
    payload = json.loads(runner._run_output(["docker", "inspect", "--format", _INSPECT, ids[0]], child_env))
    if payload.get("project") != project or payload.get("service") != service or payload.get("id") != ids[0]:
        raise SelectedEnvError("维护容器不属于所选 Compose project/service")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", payload.get("image", "")) is None:
        raise SelectedEnvError("维护容器缺少精确 image ID")
    if payload.get("state", {}).get("Restarting"):
        raise SelectedEnvError("维护期间容器正在重启，请等待稳定")
    return cast(MaintenanceContainer, payload)


def _mount(container: MaintenanceContainer, target: str, runtime_root: Path) -> Path:
    mounts = [entry for entry in container["mounts"] if entry.get("Destination") == target and entry.get("Type") == "bind"]
    if len(mounts) != 1:
        raise SelectedEnvError("Runtime bind mount 无法精确定位")
    source = Path(mounts[0]["Source"])
    if not source.is_absolute() or source.resolve() != source or not source.is_dir() or not source.is_relative_to(runtime_root):
        raise SelectedEnvError("Runtime bind mount 不在所选真实 Runtime 根内")
    return source


def _native_inventory(
    runtime: MaintenanceContainer,
    native_data: Path,
    source_root: Path,
    references: GovernanceReferences,
    child_env: dict[str, str],
) -> NativeInventory:
    from scripts import run_selected_env_operation as runner

    user = runtime["user"]
    if re.fullmatch(r"[0-9]+:[0-9]+", user) is None:
        raise SelectedEnvError("Runtime 容器必须使用明确 UID:GID")
    script = source_root / "scripts/runtime_workspace_gc_inventory.py"
    reader_id = uuid.uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--label",
        f"io.agentgov.workspace-gc-reader={reader_id}",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        user,
        "--mount",
        f"type=bind,src={native_data},dst=/runtime-data,readonly",
        "--entrypoint",
        "/usr/local/bin/python",
        runtime["image"],
        "-I",
        "-",
        "--database",
        "/runtime-data/agentscope.db",
        "--agent-ids",
        *sorted(references.agent_ids),
    ]
    try:
        with runner._command_monitor(child_env):
            result = subprocess.run(
                runner._bind_command(command, child_env),
                input=script.read_text(encoding="utf-8"),
                env=child_env,
                cwd=source_root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SelectedEnvError("只读 native 库存进程未完成；保留 Workspace") from exc
    finally:
        _remove_inventory_reader(reader_id, child_env)
    if result.returncode != 0 or len(result.stdout) > 5_000_000:
        raise SelectedEnvError("只读 native 库存进程失败；引用未知，保留 Workspace")
    return cast("NativeInventory", json.loads(result.stdout))


def _remove_inventory_reader(reader_id: str, child_env: dict[str, str]) -> None:
    from scripts import run_selected_env_operation as runner

    command = ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"label=io.agentgov.workspace-gc-reader={reader_id}"]
    ids = runner._run_output(command, child_env).splitlines()
    if len(ids) > 1 or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in ids):
        raise SelectedEnvError("本轮只读库存容器归属不明，未做额外清理")
    if ids:
        runner._run(["docker", "rm", "--force", ids[0]], child_env)


def _state(container_id: str, child_env: dict[str, str]) -> ContainerState:
    from scripts import run_selected_env_operation as runner

    return cast(ContainerState, json.loads(runner._run_output(["docker", "inspect", "--format", "{{json .State}}", container_id], child_env)))


def _recovery_state(container_id: str, recovery: RecoveryBoundary) -> ContainerState:
    return cast(ContainerState, json.loads(recovery.output(["docker", "inspect", "--format", "{{json .State}}", container_id])))


def _wait_healthy(container_id: str, recovery: RecoveryBoundary) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = _recovery_state(container_id, recovery)
        if state.get("Running") and state.get("Health", {}).get("Status") == "healthy":
            return
        if not state.get("Running") or state.get("Health", {}).get("Status") == "unhealthy":
            break
        time.sleep(1)
    raise SelectedEnvError("Runtime 已恢复启动，但尚未确认健康；请检查服务状态")


@contextmanager
def _restore_services(api: MaintenanceContainer, runtime: MaintenanceContainer, child_env: dict[str, str]) -> Iterator[None]:
    if api["state"].get("Paused") or runtime["state"].get("Paused"):
        raise SelectedEnvError("服务已由其他维护操作暂停；保持原状，请先完成该维护")
    recovery = _prepare_recovery(api, runtime, child_env)

    def interrupted(_signum, _frame):
        raise InterruptedError("Workspace maintenance interrupted")

    old_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        # Runtime boot 回执需要 API 可响应；先恢复 API，再启动原本运行的 Runtime。
        try:
            if recovery.commands.api_was_running and _recovery_state(recovery.commands.api_id, recovery).get("Paused"):
                recovery.output(["docker", "unpause", recovery.commands.api_id])
        finally:
            if recovery.commands.runtime_was_running and not _recovery_state(recovery.commands.runtime_id, recovery).get("Running"):
                recovery.output(["docker", "start", recovery.commands.runtime_id])
                _wait_healthy(recovery.commands.runtime_id, recovery)


def _apply(
    api: MaintenanceContainer,
    runtime: MaintenanceContainer,
    database: Path,
    native_data: Path,
    workspaces: Path,
    source_root: Path,
    child_env: dict[str, str],
) -> WorkspacePlan:
    from scripts import run_selected_env_operation as runner

    require_idle_database(database)
    with _restore_services(api, runtime, child_env):
        if api["state"].get("Running"):
            runner._run(["docker", "pause", api["id"]], child_env)
        require_idle_database(database)
        if runtime["state"].get("Running"):
            runner._run(["docker", "stop", "--time", "30", runtime["id"]], child_env)
        if _state(runtime["id"], child_env).get("Running"):
            raise SelectedEnvError("Runtime 停服未确认，拒绝移动 Workspace")
        # 停服之后，使用同一个实际 image 与只读数据卷重新建立 native 引用证据。
        references = read_governance_references(database)
        native = _native_inventory(runtime, native_data, source_root, references, child_env)
        plan = plan_workspaces(workspaces, references, native)
        plan["result"] = quarantine_workspaces(workspaces, plan)
    return plan


def run_workspace_gc(operation: str, snapshot: Path, source_root: Path, child_env: dict[str, str]) -> int:
    from scripts import run_selected_env_operation as runner

    runtime_root = cutover.resolve_runtime_root(snapshot, require_exists=True)
    values = cutover.load_env_file(snapshot)
    project = values.get("COMPOSE_PROJECT_NAME", "")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) is None:
        raise SelectedEnvError("维护要求所选 env 明确 Compose project")
    base = runner._compose(snapshot, source_root)
    api = _container(base, "agent-gov-api", child_env, project)
    runtime = _container(base, "agentscope-runtime", child_env, project)
    database = _mount(api, values.get("DATA_DIR", "/data"), runtime_root) / "runtime.sqlite3"
    native_data = _mount(runtime, "/runtime-data", runtime_root)
    workspaces = _mount(runtime, "/runtime-workspaces", runtime_root)
    if runtime["database"] != ["AGENTSCOPE_RUNTIME_DATABASE_URL=sqlite+aiosqlite:////runtime-data/agentscope.db"]:
        raise SelectedEnvError("实际 Runtime 数据库地址不满足固定维护契约")
    expected_native = Path(values.get("HOST_AGENTSCOPE_RUNTIME_DATA_MOUNT") or runtime_root / "agentscope-runtime/data")
    expected_workspaces = Path(values.get("HOST_AGENTSCOPE_RUNTIME_WORKSPACES_MOUNT") or runtime_root / "agentscope-runtime/workspaces")
    if native_data != expected_native or workspaces != expected_workspaces:
        raise SelectedEnvError("实际 Runtime 持久目录与所选 env 不一致")
    if database != cutover._database_path(runtime_root, snapshot):
        raise SelectedEnvError("实际 API 数据卷与所选 env 不一致")
    if operation == WORKSPACE_GC_APPLY_OPERATION:
        plan = _apply(api, runtime, database, native_data, workspaces, source_root, child_env)
    else:
        references = read_governance_references(database)
        native = _native_inventory(runtime, native_data, source_root, references, child_env)
        plan = plan_workspaces(workspaces, references, native)
    print(json.dumps({"operation": operation, **plan}, ensure_ascii=False))
    return 0
