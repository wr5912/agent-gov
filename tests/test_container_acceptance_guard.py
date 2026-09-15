from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from scripts import container_acceptance_compose_contract as compose_contract
from scripts import container_acceptance_daemon_monitor as daemon_monitor
from scripts import container_acceptance_inputs as inputs
from scripts import container_acceptance_materialization as materialization
from scripts import container_acceptance_python_runtime as python_runtime
from scripts import container_acceptance_toolchain as toolchain
from scripts import run_container_acceptance as acceptance
from scripts.agentscope_atomic_cutover_bootstrap import freeze_deployable_source
from scripts.container_acceptance_identity import capture_file_identity, verify_sealed_file, write_exclusive_file

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = REPO_ROOT / ".codex/hooks/container_acceptance_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("agentgov_container_acceptance_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_guard_blocks_every_private_make_acceptance_target() -> None:
    guard = _load_guard()

    for target in guard.PRIVATE_MAKE_TARGETS:
        assert guard.bypass_reason(f"make {target}") == "私有容器验收 Make 目标不能直接调用"


def test_guard_blocks_every_private_frontend_acceptance_script() -> None:
    guard = _load_guard()

    for script in guard.PRIVATE_FRONTEND_SCRIPTS:
        assert guard.bypass_reason(f"pnpm --dir frontend run {script}") == "真实容器前端 :impl 脚本不能直接调用"


def test_guard_blocks_every_direct_acceptance_script() -> None:
    guard = _load_guard()

    for script in guard.DIRECT_ACCEPTANCE_SCRIPTS:
        assert guard.bypass_reason(f"python3 {script}") == "真实容器验收脚本不能绕过公共 Make 入口"
        assert guard.bypass_reason(f"python3 -u {script}") == "真实容器验收脚本不能绕过公共 Make 入口"


def test_guard_allows_static_tools_to_inspect_acceptance_scripts() -> None:
    guard = _load_guard()
    script = guard.DIRECT_ACCEPTANCE_SCRIPTS[0]

    for command in (
        f"python3 -m ruff check {script}",
        f"python3 -m pyright {script}",
        f"python3 -m pytest -q tests/test_container_acceptance_guard.py {script}",
        f"python3 -m compileall {script}",
    ):
        assert guard.bypass_reason(command) is None


def test_guard_allows_public_acceptance_targets() -> None:
    guard = _load_guard()

    for target in (
        "container-core-smoke",
        "container-live-test",
        "container-technical-live-smoke",
        "container-mcp-technical-smoke",
        "container-release-candidate",
        "main-flow-live-test",
        "ui-agent-candidate-technical-smoke",
        "ui-playground-technical-smoke",
    ):
        assert guard.bypass_reason(f"make {target}") is None


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_hostile_path_node_is_rejected_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "node-executed"
    _executable(tmp_path / "node", f"#!/bin/sh\nprintf executed > '{marker}'\n")

    with pytest.raises(ValueError, match="Node 位于非可信路径"):
        toolchain.capture_acceptance_toolchain(
            {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
            "container-core-smoke",
        )

    assert not marker.exists()


def test_hostile_path_pnpm_curl_and_awk_are_never_selected(tmp_path: Path) -> None:
    marker = tmp_path / "hostile-tool-executed"
    for name in ("pnpm", "curl", "awk"):
        _executable(tmp_path / name, f"#!/bin/sh\nprintf {name} >> '{marker}'\n")
    captured = toolchain.capture_acceptance_toolchain(
        {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        "",
    )
    child_env = toolchain.toolchain_environment(captured)

    assert child_env["PATH"] == toolchain.TRUSTED_SYSTEM_PATH
    assert child_env[toolchain.TOOL_PATH_ENV_KEYS["curl"]] == "/usr/bin/curl"
    assert child_env[toolchain.TOOL_PATH_ENV_KEYS["awk"]] == "/usr/bin/awk"
    assert "pnpm" not in toolchain.TOOL_PATH_ENV_KEYS
    assert (
        acceptance._run_child(
            [child_env[toolchain.TOOL_PATH_ENV_KEYS["curl"]], "--version"],
            child_env,
        )
        == 0
    )
    assert not marker.exists()


def test_toolchain_verification_rejects_identity_and_path_tampering() -> None:
    captured = toolchain.capture_acceptance_toolchain(dict(os.environ), "")
    child_env = toolchain.toolchain_environment(captured)
    payload = json.loads(child_env[toolchain.TOOLCHAIN_ENV])
    payload["tools"][0]["sha256"] = "0" * 64
    child_env[toolchain.TOOLCHAIN_ENV] = json.dumps(payload)

    with pytest.raises(ValueError, match="身份已变化"):
        toolchain.verify_acceptance_toolchain(child_env)

    clean_env = toolchain.toolchain_environment(captured)
    clean_env["PATH"] = f"/tmp:{toolchain.TRUSTED_SYSTEM_PATH}"
    with pytest.raises(ValueError, match="PATH 不是可信确定值"):
        toolchain.verify_acceptance_toolchain(clean_env)


def test_acceptance_context_receipt_binds_exact_toolchain(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir(mode=0o700)
    buildx_state = runtime_root / "buildx-state"
    buildx_state.mkdir(mode=0o700)
    selected_env = tmp_path / "selected.env"
    selected_env.write_text("AGENTSCOPE_MODEL_NAME=local\n", encoding="utf-8")
    captured = toolchain.capture_acceptance_toolchain(dict(os.environ), "container-core-smoke")
    context_root = tmp_path / "acceptance-context"
    context_root.mkdir(mode=0o700)
    context_path = context_root / "acceptance-context.json"
    environment = {
        **toolchain.toolchain_environment(captured),
        inputs.ACCEPTANCE_ACTIVE_ENV: "1",
        inputs.ACCEPTANCE_RUN_ID_ENV: "receipt-run",
        inputs.ACCEPTANCE_PROFILE_ENV: "core",
        inputs.ACCEPTANCE_TARGET_ENV: "container-core-smoke",
        inputs.ACCEPTANCE_COMMAND_SHA256_ENV: "a" * 64,
        inputs.ACCEPTANCE_CONTEXT_ENV: str(context_path),
        "COMPOSE_PROJECT_NAME": "receipt-project",
        "COMPOSE_ENV_FILE": str(selected_env),
        "HOST_RUNTIME_VOLUME_ROOT": str(runtime_root),
        "BUILDX_CONFIG": str(buildx_state),
        "BUILDX_BUILDER": "default",
        "API_BASE": "http://127.0.0.1:50400",
        "FRONTEND_URL": "http://127.0.0.1:50401",
        inputs.LIVE_SOURCE_ROOT_ENV: str(REPO_ROOT),
        inputs.DEPLOYABLE_SOURCE_ROOT_ENV: str(REPO_ROOT),
        toolchain.FORMAL_SOURCE_ROOT_ENV: str(REPO_ROOT),
    }
    inputs.write_acceptance_context(
        context_path,
        source_env=selected_env,
        effective_env=selected_env,
        environ=environment,
        source_sha256="b" * 64,
        frozen_source_sha256="c" * 64,
        snapshots=(),
        containers=(),
    )
    with pytest.raises(inputs.AcceptanceError, match="上下文目录.*0500"):
        inputs.verify_acceptance_context(environment)
    context_root.chmod(0o500)
    with pytest.raises(inputs.AcceptanceError, match="封存为 0400"):
        inputs.verify_acceptance_context(environment)
    context_path.chmod(0o400)
    receipt = json.loads(context_path.read_text(encoding="utf-8"))

    assert receipt["schema_version"] == 4
    assert receipt["toolchain"] == captured
    for poisoned in (
        {"BUILDX_CONFIG": str(tmp_path / "hostile-buildx")},
        {"BUILDX_BUILDER": "remote"},
    ):
        with pytest.raises(inputs.AcceptanceError, match="Buildx state/builder"):
            inputs.verify_acceptance_context({**environment, **poisoned})
    receipt["toolchain"]["tools"][0]["sha256"] = "0" * 64
    context_path.chmod(0o600)
    context_path.write_text(json.dumps(receipt), encoding="utf-8")
    context_path.chmod(0o400)
    with pytest.raises(inputs.AcceptanceError, match="身份已变化"):
        inputs.verify_acceptance_context(environment)
    context_root.chmod(0o700)


def test_context_directory_sealing_does_not_relax_snapshot_write_permissions(tmp_path: Path) -> None:
    context_root = tmp_path / "context"
    context_root.mkdir(mode=0o700)
    context_path = context_root / "acceptance-context.json"
    inputs._validate_private_directory(context_root, label="验收输入目录")
    retained = write_exclusive_file(context_path, b"{}\n", error_type=inputs.AcceptanceError, label="回执")
    materialization.seal_materialized_input_tree(context_root, error_type=inputs.AcceptanceError)
    try:
        assert inputs._context_file({inputs.ACCEPTANCE_CONTEXT_ENV: str(context_path)}) == context_path
        verify_sealed_file(context_path, retained, mode=0o400, error_type=inputs.AcceptanceError, label="回执")
        with pytest.raises(inputs.AcceptanceError, match="验收输入目录.*0700"):
            inputs._validate_private_directory(context_root, label="验收输入目录")
    finally:
        materialization.make_materialized_tree_disposable(context_root)


def test_acceptance_make_recipes_use_only_bound_browser_and_http_tools() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    browser_recipes = tuple(
        makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
        for target in (
            "_ui-feedback-smoke",
            "_ui-playground-cancel-smoke",
            "_ui-agent-candidate-technical-smoke",
        )
    )

    assert all("AGENTGOV_ACCEPTANCE_NODE" in recipe for recipe in browser_recipes)
    assert all("pnpm --dir frontend" not in recipe for recipe in browser_recipes)
    assert "AGENTGOV_ACCEPTANCE_CURL" in makefile.split("\n_ui-smoke:", 1)[1].split("\n\n", 1)[0]
    for target in ("_ui-smoke", "_ui-feedback-smoke", "_ui-playground-cancel-smoke", "_ui-agent-candidate-technical-smoke"):
        assert "AGENTGOV_ACCEPTANCE_AWK" in makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]


def test_explicit_docker_config_prevents_hostile_home_compose_plugin(tmp_path: Path) -> None:
    marker = tmp_path / "hostile-compose-executed"
    hostile_home = tmp_path / "home"
    hostile_plugins = hostile_home / ".docker/cli-plugins"
    hostile_plugins.mkdir(parents=True)
    _executable(
        hostile_plugins / "docker-compose",
        f"#!/bin/sh\nprintf executed > '{marker}'\n",
    )
    isolated_config = tmp_path / "isolated-docker-config"
    isolated_plugins = isolated_config / "cli-plugins"
    isolated_plugins.mkdir(parents=True)
    shutil.copy2(toolchain.SYSTEM_TOOL_PATHS["docker-compose"], isolated_plugins / "docker-compose")
    result = subprocess.run(
        [str(toolchain.SYSTEM_TOOL_PATHS["docker"]), "--config", str(isolated_config), "compose", "version", "--short"],
        env={"HOME": str(hostile_home), "PATH": f"{tmp_path}:{toolchain.TRUSTED_SYSTEM_PATH}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert not marker.exists()


def test_compose_command_executes_the_bound_docker_copy(tmp_path: Path) -> None:
    command = acceptance.compose_command(
        acceptance.PROFILES["core"],
        tmp_path / "selected.env",
        acceptance.REPO_ROOT,
        docker_path=tmp_path / "private-tools/docker",
    )

    assert command[0] == str(tmp_path / "private-tools/docker")


def test_materialized_docker_config_blocks_caches_without_changing_plugin_receipts(tmp_path: Path) -> None:
    execution_root = tmp_path / "execution"
    execution_root.mkdir(mode=0o700)
    receipts = []
    for name in ("docker-compose", "docker-buildx"):
        source = capture_file_identity(name, toolchain.SYSTEM_TOOL_PATHS[name], kind="executable", error_type=ValueError, executable=True)
        receipts.append(
            materialization._copy_file_exact(source, toolchain.materialized_tool_path(execution_root, name), error_type=ValueError, executable=True)
        )
    materialization._seal_docker_plugin_directories(execution_root, error_type=acceptance.AcceptanceError)
    docker_config = execution_root / "docker-config"
    guard = materialization.ExecutionMutationGuard((execution_root,), error_type=acceptance.AcceptanceError)
    try:
        assert execution_root.stat().st_mode & 0o777 == 0o700
        assert docker_config.stat().st_mode & 0o777 == 0o500
        assert (docker_config / "cli-plugins").stat().st_mode & 0o777 == 0o500
        for receipt in receipts:
            assert capture_file_identity(receipt["name"], Path(receipt["path"]), kind="executable", error_type=ValueError, executable=True) == receipt
        for name in (".token_seed", ".token_seed.lock"):
            with pytest.raises(PermissionError):
                (docker_config / name).touch()
            assert not (docker_config / name).exists()
        result = subprocess.run(
            [str(toolchain.SYSTEM_TOOL_PATHS["docker"]), "--config", str(docker_config), "compose", "version", "--short"],
            env={"HOME": str(tmp_path), "PATH": toolchain.TRUSTED_SYSTEM_PATH},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()
        guard.check()
        docker_config.chmod(0o700)
        (docker_config / ".token_seed.lock").touch()
        with pytest.raises(acceptance.AcceptanceError, match="执行期间.*发生变化"):
            guard.check()
    finally:
        guard.close()
        materialization.make_materialized_tree_disposable(execution_root)


def test_materialized_docker_config_sealing_reports_incomplete_layout(tmp_path: Path) -> None:
    (tmp_path / "docker-config").mkdir()

    with pytest.raises(acceptance.AcceptanceError, match="无法收紧验收 Docker 配置目录权限") as error:
        materialization._seal_docker_plugin_directories(tmp_path, error_type=acceptance.AcceptanceError)

    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_running_child_cannot_hide_modify_then_restore_of_formal_inputs(tmp_path: Path) -> None:
    watched_root = tmp_path / "watched"
    watched_root.mkdir()
    watched_file = watched_root / "tool"
    watched_file.write_text("trusted\n", encoding="utf-8")
    watched_file.chmod(0o400)
    guard = materialization.ExecutionMutationGuard((watched_root,), error_type=acceptance.AcceptanceError)

    def mutate_and_restore() -> None:
        threading.Event().wait(0.1)
        watched_file.chmod(0o600)
        watched_file.write_text("hostile\n", encoding="utf-8")
        watched_file.write_text("trusted\n", encoding="utf-8")
        watched_file.chmod(0o400)

    worker = threading.Thread(target=mutate_and_restore)
    acceptance._ACTIVE_INPUT_GUARD = guard
    worker.start()
    try:
        with pytest.raises(acceptance.AcceptanceError, match="执行期间.*发生变化"):
            acceptance._run_checked(
                [sys.executable, "-c", "import time; time.sleep(0.3); print('false success')"],
                env=dict(os.environ),
                label="篡改回归",
                capture=True,
            )
    finally:
        worker.join()
        acceptance._ACTIVE_INPUT_GUARD = None
        guard.close()


def test_materialized_python_paths_never_reference_live_venv_or_user_base(tmp_path: Path) -> None:
    execution_root = tmp_path / "execution"
    formal_root = execution_root / "formal-source"

    python_home, python_paths = python_runtime.materialized_python_paths(execution_root, formal_root)

    assert python_home == execution_root / "python-base"
    assert all(path.is_relative_to(execution_root) for path in python_paths)
    assert REPO_ROOT / ".venv" not in python_paths


def test_cleanup_child_cannot_hide_transient_toolchain_mutation(tmp_path: Path, monkeypatch) -> None:
    temp_root = tmp_path / "acceptance"
    watched_root = temp_root / "execution-toolchain"
    watched_root.mkdir(parents=True)
    watched_file = watched_root / "docker"
    watched_file.write_text("trusted\n", encoding="utf-8")
    watched_file.chmod(0o400)
    runtime_root = temp_root / "runtime-root"
    runtime_root.mkdir()
    isolation = acceptance.IsolatedEnvironment(
        temp_root / "compose.acceptance.env",
        runtime_root,
        "project",
        "container",
        {},
    )
    guard = materialization.ExecutionMutationGuard((watched_root,), error_type=acceptance.AcceptanceError)

    def cleanup_with_running_child(*_args, **_kwargs) -> None:
        acceptance._run_checked(
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
            env=dict(os.environ),
            label="cleanup mutation regression",
        )

    def mutate_and_restore() -> None:
        threading.Event().wait(0.1)
        watched_file.chmod(0o600)
        watched_file.write_text("hostile\n", encoding="utf-8")
        watched_file.write_text("trusted\n", encoding="utf-8")
        watched_file.chmod(0o400)

    monkeypatch.setattr(acceptance, "cleanup_profile", cleanup_with_running_child)
    monkeypatch.setattr(acceptance, "verify_acceptance_toolchain", lambda *_args, **_kwargs: None)
    acceptance._ACTIVE_INPUT_GUARD = guard
    worker = threading.Thread(target=mutate_and_restore)
    worker.start()
    try:
        with pytest.raises(acceptance.AcceptanceError, match="执行期间.*发生变化"):
            acceptance._cleanup_acceptance_run(
                acceptance.PROFILES["core"],
                temp_root,
                isolation,
                dict(os.environ),
                {},
                compose_started=True,
                input_guard=guard,
                operation_error=None,
            )
    finally:
        worker.join()
        acceptance._ACTIVE_INPUT_GUARD = None
        guard.close()


def test_sealed_effective_env_and_scenario_detect_modify_then_restore(tmp_path: Path) -> None:
    frozen_inputs = tmp_path / "acceptance-inputs"
    frozen_inputs.mkdir()
    effective_env = frozen_inputs / "compose.acceptance.env"
    scenario = frozen_inputs / "real-scenarios.json"
    effective_env.write_text("HOST_PORT=50400\n", encoding="utf-8")
    scenario.write_text('{"scenario":"real"}\n', encoding="utf-8")
    materialization.seal_materialized_input_tree(frozen_inputs, error_type=acceptance.AcceptanceError)
    guard = materialization.ExecutionMutationGuard((frozen_inputs,), error_type=acceptance.AcceptanceError)

    effective_env.chmod(0o600)
    effective_env.write_text("HOST_PORT=50499\n", encoding="utf-8")
    effective_env.write_text("HOST_PORT=50400\n", encoding="utf-8")
    effective_env.chmod(0o400)

    try:
        with pytest.raises(acceptance.AcceptanceError, match="执行期间.*发生变化"):
            guard.check()
    finally:
        guard.close()
        materialization.make_materialized_tree_disposable(frozen_inputs)


def test_context_changed_before_watch_cannot_self_attest(tmp_path: Path) -> None:
    context = tmp_path / "acceptance-context.json"
    retained = write_exclusive_file(
        context,
        b'{"profile":"core","trusted":true}\n',
        error_type=acceptance.AcceptanceError,
        label="验收上下文回执",
    )
    context.write_text('{"profile":"core","trusted":false}\n', encoding="utf-8")
    context.chmod(0o400)

    with pytest.raises(acceptance.AcceptanceError, match="同一 inode 与字节"):
        verify_sealed_file(
            context,
            retained,
            mode=0o400,
            error_type=acceptance.AcceptanceError,
            label="验收上下文回执",
        )


@pytest.mark.parametrize(
    "event",
    [
        {"Type": "container", "Action": "update", "Actor": {"Attributes": {"com.docker.compose.project": "formal"}}},
        {"Type": "container", "Action": "health_status: unhealthy", "Actor": {"ID": "a" * 64, "Attributes": {}}},
        {"Type": "network", "Action": "disconnect", "Actor": {"ID": "b" * 64, "Attributes": {}}},
    ],
)
def test_docker_event_monitor_rejects_project_health_and_network_mutation(event: object, tmp_path: Path) -> None:
    monitor = daemon_monitor.DockerMutationMonitor(
        docker_path="/usr/bin/docker",
        project_name="formal",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
        environ={},
        run_output=lambda *_args: "",
        error_type=acceptance.AcceptanceError,
    )
    monitor._container_ids = frozenset({"a" * 64})
    monitor._network_ids = frozenset({"b" * 64})
    monitor._events = [json.dumps(event)]

    with pytest.raises(acceptance.AcceptanceError, match="Docker 状态发生变化"):
        monitor._reject_events()


@pytest.mark.parametrize("action", ["exec_create", "exec_create: /bin/true", "exec_start", "exec_start: /bin/true", "exec_die", "exec_detach"])
def test_docker_event_classification_distinguishes_exec_from_lifecycle(action: str, tmp_path: Path) -> None:
    monitor = daemon_monitor.DockerMutationMonitor(
        docker_path="/usr/bin/docker",
        project_name="formal",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
        environ={},
        run_output=acceptance._daemon_command,
        error_type=acceptance.AcceptanceError,
    )
    event = {"Type": "container", "Action": action, "Actor": {"Attributes": {"com.docker.compose.project": "formal"}}}

    assert monitor._relevant(event) is False
    for kind in ("network", "volume", "daemon"):
        assert monitor._relevant({**event, "Type": kind}) is True
    assert monitor._relevant({**event, "Action": "exec_unknown"}) is True


def test_docker_event_stream_and_ready_gap_share_fixed_since_boundary(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    class FakeProcess:
        pid = 12345
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: int) -> int:
            del timeout
            return self.returncode or 0

        def kill(self) -> None:
            self.returncode = -9

    def popen(command: list[str], **_kwargs: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    def output(command: list[str], _label: str) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(daemon_monitor.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon_monitor, "_has_socket_descriptor", lambda _pid: True)
    monitor = daemon_monitor.DockerMutationMonitor(
        docker_path="/bound/docker",
        project_name="formal",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
        environ={},
        run_output=output,
        error_type=acceptance.AcceptanceError,
    )

    monitor.start()
    monitor.close()

    stream, ready_gap = commands
    stream_since = stream[stream.index("--since") + 1]
    gap_since = ready_gap[ready_gap.index("--since") + 1]
    assert stream[:2] == ["/bound/docker", "events"]
    assert ready_gap[:2] == ["/bound/docker", "events"]
    assert stream_since == gap_since
    assert "--until" not in stream
    assert "--until" in ready_gap


@pytest.mark.parametrize("action", ["create", "start", "die", "destroy"])
def test_docker_event_monitor_rejects_transient_unlabelled_sidecar(action: str, tmp_path: Path) -> None:
    monitor = daemon_monitor.DockerMutationMonitor(
        docker_path="/usr/bin/docker",
        project_name="formal",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
        environ={},
        run_output=lambda *_args: "",
        error_type=acceptance.AcceptanceError,
    )
    monitor._events = [
        json.dumps(
            {
                "Type": "container",
                "Action": action,
                "Actor": {"ID": "c" * 64, "Attributes": {"image": "hostile-sidecar"}},
            }
        )
    ]

    with pytest.raises(acceptance.AcceptanceError, match=f"Docker 状态发生变化: {action}"):
        monitor._reject_events()


def test_docker_inventory_rejects_unreceipted_network_sidecar(tmp_path: Path) -> None:
    target = "a" * 64

    def output(command: list[str], _label: str) -> str:
        if command[1:3] == ["network", "ls"]:
            return "b" * 64
        if command[1:3] == ["network", "inspect"]:
            return json.dumps({target: {}, "c" * 64: {}})
        if command[1:3] == ["ps", "--no-trunc"] and "--filter" in command:
            return target
        return ""

    monitor = daemon_monitor.DockerMutationMonitor(
        docker_path="/usr/bin/docker",
        project_name="formal",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
        environ={},
        run_output=output,
        error_type=acceptance.AcceptanceError,
    )
    monitor._container_ids = frozenset({target})

    with pytest.raises(acceptance.AcceptanceError, match="非回执 sidecar"):
        monitor._capture_inventory()


@pytest.mark.parametrize(
    ("boundary", "replacement"),
    [
        ("Init", False),
        ("ExtraHosts", []),
        ("Healthcheck", {"Test": ["CMD", "true"], "Interval": 1_000_000_000, "Timeout": 9, "Retries": 2}),
        ("Healthcheck", {"Test": ["CMD", "false"], "Interval": 1_000_000_000, "Timeout": 2_000_000_000, "Retries": 2}),
        ("RestartPolicy", {"Name": "no", "MaximumRetryCount": 0}),
    ],
)
def test_rendered_compose_supplemental_contract_rejects_single_field_drift(
    boundary: str,
    replacement: object,
) -> None:
    service = {
        "container_name": "formal-service",
        "init": True,
        "extra_hosts": ["host.docker.internal=host-gateway"],
        "healthcheck": {"test": ["CMD", "true"], "interval": "1s", "timeout": "2s", "retries": 2},
        "logging": {"driver": "json-file", "options": {"max-size": "50m"}},
        "networks": {"default": None},
        "restart": "unless-stopped",
        "expose": ["8090"],
        "tmpfs": ["/tmp"],
        "ports": [],
    }
    image = {"ExposedPorts": {"8080/tcp": {}}}
    container = {
        "Name": "/formal-service",
        "Config": {
            "ExposedPorts": {"8080/tcp": {}, "8090/tcp": {}},
            "Healthcheck": {
                "Test": ["CMD", "true"],
                "Interval": 1_000_000_000,
                "Timeout": 2_000_000_000,
                "Retries": 2,
            },
        },
        "HostConfig": {
            "Init": True,
            "ExtraHosts": ["host.docker.internal:host-gateway"],
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "50m"}},
            "Tmpfs": {"/tmp": ""},
        },
        "NetworkSettings": {"Networks": {"formal-project_default": {}}},
    }
    compose_contract._verify_supplemental(service, image, container, "formal-project", "formal-service")
    target = container["Config"] if boundary == "Healthcheck" else container["HostConfig"]
    assert isinstance(target, dict)
    target[boundary] = replacement

    with pytest.raises(ValueError, match="supplemental container config mismatch"):
        compose_contract._verify_supplemental(service, image, container, "formal-project", "formal-service")


def test_healthcheck_explicit_zero_and_empty_values_override_image_defaults() -> None:
    image = {
        "Test": ["CMD", "image-probe"],
        "Interval": 5_000_000_000,
        "Timeout": 4_000_000_000,
        "Retries": 3,
    }
    service = {"test": [], "interval": "0s", "retries": 0}

    assert compose_contract._expected_healthcheck(service, image) == {
        "test": (),
        "interval": 0,
        "timeout": 4_000_000_000,
        "start_period": 0,
        "start_interval": 0,
        "retries": 0,
    }


@pytest.mark.parametrize(
    ("compose_command", "native_command"),
    [
        ('pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"', 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'),
        ('redis-cli -a "$$REDIS_AUTH" ping | grep PONG', 'redis-cli -a "$REDIS_AUTH" ping | grep PONG'),
        ('printf "$$$$"', 'printf "$$"'),
    ],
)
def test_healthcheck_unescapes_only_explicit_compose_command_once(compose_command: str, native_command: str) -> None:
    image = {"Test": ["CMD-SHELL", 'printf "$$"']}
    expected = compose_contract._expected_healthcheck({"test": ["CMD-SHELL", compose_command]}, image)

    assert expected == compose_contract._actual_healthcheck({"Test": ["CMD-SHELL", native_command]})
    assert expected != compose_contract._actual_healthcheck({"Test": ["CMD-SHELL", native_command + "; unexpected"]})
    for service in (None, {"interval": "1s"}):
        assert compose_contract._expected_healthcheck(service, image)["test"] == ("CMD-SHELL", 'printf "$$"')


def test_healthcheck_rejects_unmodeled_fields() -> None:
    with pytest.raises(ValueError, match="未建模字段"):
        compose_contract._expected_healthcheck({"x-hidden": "accepted"}, None)


def test_reviewed_scenarios_rejects_hostile_python_not_bound_by_receipt(tmp_path: Path) -> None:
    marker = tmp_path / "hostile-python-executed"
    hostile_python = tmp_path / "python"
    _executable(hostile_python, f"#!/bin/sh\nprintf executed > '{marker}'\n")
    captured = toolchain.capture_acceptance_toolchain(dict(os.environ), "")
    node = next(item["path"] for item in captured["tools"] if item["name"] == "node")
    environment = {
        **os.environ,
        "AGENTGOV_ACCEPTANCE_PYTHON": str(hostile_python),
        toolchain.TOOLCHAIN_ENV: json.dumps(captured),
    }
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "import {loadReviewedScenarios} from './scripts/improvement_ui_e2e/reviewed_scenarios.mjs'; loadReviewedScenarios('x.json','agent');",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match the materialized toolchain receipt" in result.stderr
    assert not marker.exists()


def test_frozen_formal_bundle_contains_policy_and_executes_scanner(tmp_path: Path) -> None:
    deployable = tmp_path / "deployable"
    freeze_deployable_source(REPO_ROOT, deployable)
    policy = capture_file_identity(
        "formal-quality-policy",
        REPO_ROOT / "tests/quality_policy.json",
        kind="file",
        error_type=ValueError,
    )
    formal = materialization._freeze_formal_source(
        deployable,
        tmp_path / "formal",
        policy,
        error_type=ValueError,
    )
    formal_root = Path(formal["path"])

    assert (formal_root / "tests/quality_policy.json").is_file()
    result = subprocess.run(
        [sys.executable, str(formal_root / "scripts/check_no_test_doubles.py")],
        cwd=formal_root,
        env={
            "PATH": toolchain.TRUSTED_SYSTEM_PATH,
            "PYTHONPATH": str(formal_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "NO_TEST_DOUBLES_OK" in result.stdout
