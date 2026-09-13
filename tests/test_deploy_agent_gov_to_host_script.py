from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"
CUTOVER_SCRIPT = REPO_ROOT / "scripts" / "agentscope_atomic_cutover.py"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _load_cutover() -> ModuleType:
    module_name = "_agentgov_atomic_cutover_test"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CUTOVER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_deploy_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_script_defaults_and_preserves_private_remote_env() -> None:
    text = _script_text()

    assert 'DEFAULT_HOST="172.16.112.232"' in text
    assert 'DEFAULT_REMOTE_DIR="~/work/agent-gov"' in text
    assert 'DEPLOY_USER="${DEPLOY_USER:-root}"' in text
    assert 'REMOTE_DIR="${REMOTE_DIR:-$DEFAULT_REMOTE_DIR}"' in text
    assert "cp -n docker/.env.example docker/.env" in text

    for excluded in (
        "--exclude='/images/'",
        "--exclude='/docker/.env'",
        "--exclude='/docker/.env.bak-*'",
        "--exclude='/docker/.env.local-debug'",
        "--exclude='/frontend/.env.local'",
    ):
        assert excluded in text


def test_deploy_fails_before_sync_when_remote_python_is_older_than_311() -> None:
    text = _script_text()

    assert "sys.version_info < (3, 11)" in text
    assert "remote Python >=3.11 is required" in text
    assert text.index("sys.version_info < (3, 11)") < text.index("Syncing ${DEPLOY_REF} tracked code")


def test_deploy_rejects_legacy_epoch_before_overwriting_remote_source() -> None:
    text = _script_text()

    preflight = "Preflighting remote Runtime epoch before source sync"
    sync = "Syncing ${DEPLOY_REF} tracked code"
    assert preflight in text
    assert ".agentscope-cutover-preflight.XXXXXX" in text
    assert "./app/runtime/sqlite_schema_contract.py" in text
    assert "--require-current-or-empty" in text
    assert "trap 'rm -rf -- \"$preflight_root\"' EXIT" in text
    assert text.index(preflight) < text.index(sync)


def test_deploy_script_packages_project_and_langfuse_dependency_images() -> None:
    text = _script_text()

    for image in (
        "agent-gov-agentscope-runtime:${VERSION}",
        "agent-gov-api:${VERSION}",
        "agent-gov-ui:${VERSION}",
    ):
        assert image in text

    for env_key in (
        "LANGFUSE_WORKER_IMAGE",
        "LANGFUSE_WEB_IMAGE",
        "LANGFUSE_POSTGRES_IMAGE",
        "LANGFUSE_CLICKHOUSE_IMAGE",
        "LANGFUSE_REDIS_IMAGE",
        "LANGFUSE_MINIO_IMAGE",
    ):
        assert env_key in text

    assert "docker save" in text
    assert '"${docker_cmd[@]}" load' in text
    assert "sha256sum" in text
    assert "agent-gov-${VERSION}-images.tar.gz" in text
    assert "agent-gov-${VERSION}-langfuse-deps-images.tar.gz" in text


def test_deploy_validates_every_remote_archive_before_first_docker_load() -> None:
    text = _script_text()
    validation = "python3 scripts/agentscope_atomic_cutover_archive.py"
    load = '"${docker_cmd[@]}" load'

    assert text.count(validation) == 2
    assert text.rindex(validation) < text.index(load)
    assert "LOCAL_DOCKER=(env -i" in text
    assert 'DOCKER_HOST=unix:///var/run/docker.sock "$LOCAL_DOCKER_BIN")' in text
    assert 'DOCKER_HOST=unix:///var/run/docker.sock "$docker_boundary/docker")' in text
    assert "prepare_docker_toolchain" in text
    assert 'install -m 0500 "$source_cli" "$docker_boundary/docker"' in text


def test_deploy_script_uses_loaded_images_for_full_compose_stack() -> None:
    text = _script_text()

    assert "git fetch origin master" in text
    assert 'DEPLOY_REF="${DEPLOY_REF:-origin/master}"' in text
    assert 'git show "${TARGET_COMMIT}:VERSION"' in text
    assert 'git archive "$TARGET_COMMIT"' in text
    assert "the running deploy script differs from DEPLOY_REF" in text
    assert "working tree must be clean" not in text
    assert "scripts/run_selected_env_operation.py" in text
    assert "--operation all-up --no-build --force-recreate" in text
    assert "--operation up --no-build --force-recreate" in text
    assert "compose=(" not in text
    assert '"$docker_boundary/docker"' in text
    assert "COMPOSE_UP_FLAGS" not in text
    assert "make --no-print-directory all-up" not in text
    assert 'docker ps -aq --filter "name=agent-gov"' not in text
    assert "--profile langfuse up -d --no-build --pull never" not in text
    assert "runtime_root=$(expand_remote_value" not in text
    assert "chmod a+rwx" not in text
    assert "rm -rf '${HOME}'" not in text


def test_deploy_script_rejects_cross_architecture_image_archives_before_sync_or_stop() -> None:
    text = _script_text()
    mismatch = '[[ "$LOCAL_DOCKER_ARCH" = "$REMOTE_DOCKER_ARCH" ]]'

    assert "normalize_architecture" in text
    assert mismatch in text
    assert text.index(mismatch) < text.index('log "Syncing ${DEPLOY_REF} tracked code"')
    assert "image platform mismatch before transfer" in text
    assert '--architecture "$remote_arch"' in text
    assert text.index('--architecture "$remote_arch"') < text.index("--operation all-up --no-build --force-recreate")


def test_normal_deploy_checks_fresh_epoch_before_sync_and_activation() -> None:
    text = _script_text()
    inspect = 'python3 "$preflight_script" inspect'
    sync = 'log "Syncing ${DEPLOY_REF} tracked code"'
    activation = "--operation all-up --no-build --force-recreate"

    assert inspect in text
    assert "--require-current-or-empty" in text
    assert text.index(inspect) < text.index(sync) < text.index(activation)
    assert "普通部署" in text
    assert "destructive cutover" in text
    assert "agentscope_atomic_cutover.py execute" not in text


def test_deploy_script_uses_python_health_checks_without_remote_curl_dependency() -> None:
    text = _script_text()

    assert "from urllib.request import ProxyHandler, Request, build_opener" in text
    assert "direct_http = build_opener(ProxyHandler({}))" in text
    assert "direct_http.open(request" in text
    assert '("API and AgentScope Runtime readiness", "http://127.0.0.1:${host_port}/health/ready", 60, True)' in text
    assert '("UI", "http://127.0.0.1:${frontend_port}", 60, False)' in text
    assert '("Langfuse", "http://127.0.0.1:${langfuse_port}", 90, False)' in text
    assert 'last_error = RuntimeError(f"HTTP {exc.code}' in text
    assert 'print(f"{name} OK: {url} status={exc.code}")' not in text
    assert "curl " not in text


def test_deploy_script_preflights_agentscope_secrets_and_service_set_before_cutover() -> None:
    text = _script_text()

    for key in ("API_KEY", "AGENTGOV_RUNTIME_SHARED_SECRET", "MODEL_PROVIDER_API_KEY"):
        assert key in text
    assert "placeholder private env value is forbidden" in text
    assert "change-me|replace-with-*" in text
    for key in (
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_POSTGRES_PASSWORD",
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_ROOT_PASSWORD",
    ):
        assert key in text
    assert "64 nonzero lowercase hex characters" in text
    assert "LANGFUSE_ALLOW_PUBLIC_BIND=1" in text
    assert 'require_public_bind_opt_in "AgentGov API" API_BIND_IP API_ALLOW_PUBLIC_BIND' in text
    assert 'require_public_bind_opt_in "AgentGov UI" FRONTEND_BIND_IP FRONTEND_ALLOW_PUBLIC_BIND' in text
    assert "single-tenant operator control plane without cross-user isolation" in text
    assert "scripts/run_selected_env_operation.py" in text
    assert "--operation all-up --no-build --force-recreate" in text
    assert "compose=(" not in text


def test_cutover_epoch_inspection_refuses_legacy_database_without_mutation(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sdk_sessions (id TEXT PRIMARY KEY)")
    before = db_path.read_bytes()

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "legacy-or-unknown"
    assert result["legacy_tables"] == ["sdk_sessions"]
    assert db_path.read_bytes() == before


def _load_selected_env_runner() -> ModuleType:
    return importlib.import_module("scripts.run_selected_env_operation")


def _image_contract_fixture() -> tuple[dict[str, object], dict[str, str], dict[str, str]]:
    runner = _load_selected_env_runner()
    names = (*runner._LOCAL_IMAGES, *runner._THIRD_PARTY_SERVICES)
    references: dict[str, str] = {}
    image_ids: dict[str, str] = {}
    services: dict[str, object] = {}
    for index, service in enumerate(names, start=1):
        reference = f"agent-gov-local:{index}" if service in runner._LOCAL_IMAGES else f"registry.example/{service}@sha256:{index:064x}"
        references[reference] = service
        image_ids[service] = f"sha256:{(index + 20):064x}"
        services[service] = {"image": reference}
    return {"services": services}, references, image_ids


def _write_digest_pinned_build_dockerfiles(root: Path) -> None:
    references = {
        "docker/Dockerfile": "python:3.11-slim@sha256:" + "a" * 64,
        "docker/agentscope-runtime.Dockerfile": "python:3.11-slim@sha256:" + "a" * 64,
        "docker/frontend.Dockerfile": "node:22-alpine@sha256:" + "b" * 64,
    }
    for relative, reference in references.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"FROM {reference}\n", encoding="utf-8")


def test_remote_preflight_bundle_inspect_remains_read_only_and_self_contained(tmp_path) -> None:
    operator_home = tmp_path / "operator"
    runtime_root = operator_home / "volume-agent-gov"
    runtime_root.mkdir(parents=True)
    preflight_root = tmp_path / "remote/images/.agentscope-cutover-preflight.fixture"
    scripts = preflight_root / "scripts"
    runtime_package = preflight_root / "app/runtime"
    scripts.mkdir(parents=True)
    runtime_package.mkdir(parents=True)
    standalone = scripts / "agentscope_atomic_cutover.py"
    shutil.copyfile(CUTOVER_SCRIPT, standalone)
    shutil.copyfile(REPO_ROOT / "app/__init__.py", preflight_root / "app/__init__.py")
    shutil.copyfile(REPO_ROOT / "app/runtime/__init__.py", runtime_package / "__init__.py")
    shutil.copyfile(REPO_ROOT / "app/runtime/sqlite_schema_contract.py", runtime_package / "sqlite_schema_contract.py")
    env_file = tmp_path / "remote/docker.env"
    env_file.write_text(f"HOST_RUNTIME_VOLUME_ROOT={runtime_root}\n", encoding="utf-8")
    env = {
        "HOME": operator_home.as_posix(),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": preflight_root.as_posix(),
    }

    result = subprocess.run(
        [sys.executable, str(standalone), "inspect", "--env-file", str(env_file), "--require-current-or-empty"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["classification"] == "empty"
    assert list(runtime_root.iterdir()) == []


@pytest.mark.parametrize(
    "command",
    ["maintenance-down", "prepare", "execute", "finalize", "recover-finalize", "restore"],
)
def test_retired_atomic_commands_fail_closed_without_touching_files(tmp_path, command: str) -> None:
    sentinel = tmp_path / "must-survive"
    sentinel.write_text("safe", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(CUTOVER_SCRIPT), command],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    label = command if command != "recover-finalize" else "finalize/recover-finalize"
    assert result.returncode == 1
    assert f"{label} 已安全禁用" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


@pytest.mark.parametrize("command", ["prepare", "execute", "finalize", "recover-finalize", "restore"])
def test_retired_atomic_commands_do_not_accept_legacy_activation_arguments(tmp_path, command: str) -> None:
    sentinel = tmp_path / "must-survive"
    sentinel.write_text("safe", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(CUTOVER_SCRIPT), command, "--confirmation-token", "hostile"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_atomic_main_exposes_no_private_destructive_aliases() -> None:
    cutover = _load_cutover()

    for name in (
        "__getattr__",
        "_capture_rollback_bundle",
        "_destructive_support",
        "_recovery_support",
        "_bootstrap_and_force_recreate",
        "_restore_rollback_images_and_start",
        "_safe_extract_regular_archive",
        "_write_production_gate_state_atomic",
    ):
        assert not hasattr(cutover, name)


def test_retired_support_modules_fail_before_any_injected_mutation() -> None:
    support = importlib.import_module("scripts.agentscope_atomic_cutover_support")
    images = importlib.import_module("scripts.agentscope_atomic_cutover_images")
    rollback = importlib.import_module("scripts.agentscope_atomic_cutover_rollback")
    recovery = importlib.import_module("scripts.agentscope_atomic_cutover_recovery")
    mutations: list[str] = []

    injected = {
        "run_command": lambda *_args, **_kwargs: mutations.append("run") or "",
        "write_json": lambda *_args, **_kwargs: mutations.append("write"),
        "atomic_write_gate": lambda *_args, **_kwargs: mutations.append("gate"),
    }
    for constructor in (
        support.CutoverSupport,
        images.CutoverImageSupport,
        rollback.CutoverRollbackSupport,
    ):
        with pytest.raises(RuntimeError, match="退役"):
            constructor(error_type=RuntimeError, **injected)

    recovery_support = recovery.CutoverRecoverySupport(error_type=RuntimeError, **injected)
    with pytest.raises(RuntimeError, match="安全禁用"):
        recovery_support.open_production_gate()
    with pytest.raises(RuntimeError, match="安全禁用"):
        recovery_support.resume_irreversible_transition()

    assert mutations == []


def test_cutover_epoch_inspection_accepts_exact_current_agentscope_schema(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    from app.runtime.runtime_db import make_session_factory

    factory = make_session_factory(db_path)
    factory.kw["bind"].dispose()

    assert cutover.SCHEMA_EPOCH == "agentscope-runtime-v3"
    assert cutover.classify_runtime_epoch(db_path)["classification"] == "agentscope"


def test_cutover_quiescence_gate_counts_old_and_new_runtime_work(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, active_run_id TEXT);
            CREATE TABLE runtime_session_bindings (id TEXT PRIMARY KEY, active_run_id TEXT);
            CREATE TABLE agent_runs (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE session_turn_intents (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE claude_user_input_requests (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE runtime_pending_actions (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_test_runs (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_jobs (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_change_sets (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_release_operations (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE execution_records (id TEXT PRIMARY KEY, status TEXT);
            INSERT INTO sessions VALUES ('s1', 'run-1');
            INSERT INTO runtime_session_bindings VALUES ('b1', 'run-2');
            INSERT INTO agent_runs VALUES ('r1', 'finalizing');
            INSERT INTO session_turn_intents VALUES ('t1', 'running');
            INSERT INTO claude_user_input_requests VALUES ('h1', 'waiting');
            INSERT INTO runtime_pending_actions VALUES ('h2', 'pending');
            INSERT INTO agent_test_runs VALUES ('test1', 'queued');
            INSERT INTO agent_jobs VALUES ('job1', 'running');
            INSERT INTO agent_change_sets VALUES ('c1', 'publishing');
            INSERT INTO agent_release_operations VALUES ('o1', 'git_applied');
            INSERT INTO execution_records VALUES ('e1', 'applying');
            """
        )

    counts = cutover.active_work_counts(db_path)

    assert counts == {
        "active_sessions": 2,
        "active_runs": 2,
        "hitl_waits": 2,
        "active_tests": 1,
        "active_agent_jobs": 1,
        "active_publications": 3,
    }


def test_runtime_root_guard_rejects_broad_and_unrelated_paths(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    monkeypatch.setattr(cutover, "_operator_home", lambda: operator_home.resolve())
    env_file = tmp_path / "selected.env"

    for candidate in (Path("/"), operator_home, tmp_path):
        env_file.write_text(f"HOST_RUNTIME_VOLUME_ROOT={candidate}\n", encoding="utf-8")
        with pytest.raises(cutover.CutoverError, match="拒绝危险"):
            cutover.resolve_runtime_root(env_file, require_exists=False)


def test_runtime_validate_dispatches_before_generic_runtime_branch(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    calls: list[list[str]] = []
    snapshot = tmp_path / "selected.env"
    snapshot.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_preflight", lambda *_args: calls.append(["preflight"]))
    monkeypatch.setattr(runner, "_run", lambda command, _env, **_kwargs: calls.append(command) or 0)
    monkeypatch.setattr(
        runner,
        "_execute_runtime_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("generic runtime dispatch reached")),
    )

    result = runner._execute_operation(
        "runtime-validate",
        snapshot,
        runner.REPO_ROOT,
        tmp_path,
        {},
        no_build=False,
        force_recreate=False,
    )

    assert result == 0
    assert calls[0] == ["preflight"]
    assert any(any(item.endswith("bootstrap_runtime_volume.py") for item in command) for command in calls[1:])
    assert any(any(item.endswith("check_agentscope_cutover.py") for item in command) for command in calls[1:])


@pytest.mark.parametrize(
    ("child_env", "socket_available", "message"),
    [
        ({"DOCKER_HOST": "tcp://remote.invalid:2376"}, True, "固定本机"),
        ({"DOCKER_HOST": "unix:///var/run/docker.sock"}, False, "Unix socket 不可用"),
    ],
)
def test_selected_env_daemon_gate_rejects_remote_or_missing_socket_before_mutation(
    tmp_path,
    monkeypatch,
    child_env: dict[str, str],
    socket_available: bool,
    message: str,
) -> None:
    runner = _load_selected_env_runner()
    mutations: list[str] = []
    real_stat = os.stat

    def guarded_stat(path, *args, **kwargs):
        if os.fspath(path) == "/var/run/docker.sock":
            if not socket_available:
                raise FileNotFoundError(path)
            return SimpleNamespace(st_mode=stat.S_IFSOCK)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(runner.os, "stat", guarded_stat)
    with pytest.raises(runner.SelectedEnvError, match=message):
        with runner._daemon_mutation_lock("down", child_env):
            mutations.append("execute")

    assert mutations == []


def test_source_digest_binds_mode_and_rejects_symlinks(tmp_path, monkeypatch) -> None:
    bootstrap = importlib.import_module("scripts.agentscope_atomic_cutover_bootstrap")
    app = tmp_path / "app"
    app.mkdir()
    entrypoint = app / "entrypoint.py"
    entrypoint.write_text("print('safe')\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_source_candidates", lambda root: (root / "app",))

    initial = bootstrap.source_artifact_sha256(tmp_path)
    entrypoint.chmod(0o755)
    assert bootstrap.source_artifact_sha256(tmp_path) != initial

    symlink = app / "active.py"
    symlink.symlink_to(entrypoint.name)
    with pytest.raises(ValueError, match="symlink/special"):
        bootstrap.source_artifact_sha256(tmp_path)

    symlink.unlink()
    snapshot = tmp_path / "source-snapshot"
    frozen_digest = bootstrap.freeze_deployable_source(tmp_path, snapshot)
    entrypoint.write_text("print('changed after freeze')\n", encoding="utf-8")

    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "app/entrypoint.py").read_text(encoding="utf-8") == "print('safe')\n"
    assert bootstrap.source_artifact_sha256(snapshot) == frozen_digest
    assert bootstrap.source_artifact_sha256(tmp_path) != frozen_digest


def test_stack_image_contract_accepts_null_labels_only_for_digest_pinned_third_party(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _load_selected_env_runner()
    config, references, image_ids = _image_contract_fixture()
    source_digest = "a" * 64

    def output(command, _child_env):
        if command[-3:] == ["config", "--format", "json"]:
            return json.dumps(config)
        if command[:3] == ["docker", "image", "inspect"]:
            service = references[command[3]]
            labels = {"io.agentgov.source-artifact-sha256": source_digest} if service in runner._LOCAL_IMAGES else None
            return json.dumps([{"Id": image_ids[service], "Config": {"Labels": labels}}])
        raise AssertionError(command)

    monkeypatch.setattr(runner, "_run_output", output)

    actual = runner._verify_stack_images(
        {},
        tmp_path / "selected.env",
        "4.0.0",
        source_digest,
        langfuse=True,
        running=False,
    )

    assert actual == image_ids


def test_build_base_image_contract_rejects_mutable_missing_and_symlinked_from(tmp_path) -> None:
    inventory = importlib.import_module("scripts.selected_env_image_inventory")
    _write_digest_pinned_build_dockerfiles(tmp_path)

    references = inventory.build_base_image_references(tmp_path)

    assert len(references) == 3
    assert references["build-base:docker/Dockerfile:0"] == references["build-base:docker/agentscope-runtime.Dockerfile:0"]

    frontend = tmp_path / "docker/frontend.Dockerfile"
    frontend.write_text("FROM node:22-alpine\n", encoding="utf-8")
    with pytest.raises(inventory.SelectedEnvError, match="digest pin"):
        inventory.build_base_image_references(tmp_path)

    frontend.write_text("RUN true\n", encoding="utf-8")
    with pytest.raises(inventory.SelectedEnvError, match="缺少 FROM"):
        inventory.build_base_image_references(tmp_path)

    target = tmp_path / "frontend.real.Dockerfile"
    target.write_text("FROM node:22-alpine@sha256:" + "b" * 64 + "\n", encoding="utf-8")
    frontend.unlink()
    frontend.symlink_to(target)
    with pytest.raises(inventory.SelectedEnvError, match="普通非符号链接"):
        inventory.build_base_image_references(tmp_path)


def test_images_prepare_pulls_only_missing_digest_pinned_images(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    _config, references, _image_ids = _image_contract_fixture()
    third_party_references = {service: reference for reference, service in references.items() if service in runner._THIRD_PARTY_SERVICES}
    python_base = "python:3.11-slim@sha256:" + "a" * 64
    node_base = "node:22-alpine@sha256:" + "b" * 64
    required_references = {
        **third_party_references,
        "build-base:docker/Dockerfile:0": python_base,
        "build-base:docker/agentscope-runtime.Dockerfile:0": python_base,
        "build-base:docker/frontend.Dockerfile:0": node_base,
    }
    reference_ids = {reference: f"sha256:{index:064x}" for index, reference in enumerate(dict.fromkeys(required_references.values()), start=40)}
    expected_ids = {service: reference_ids[reference] for service, reference in required_references.items()}
    available = set(required_references.values()) - {python_base}
    pulls: list[str] = []

    monkeypatch.setattr(
        runner,
        "_required_external_image_references",
        lambda *_args: dict(required_references),
    )

    def inspect(reference: str, _child_env: dict[str, str], *, service: str) -> str:
        del service
        if reference not in available:
            raise runner.operation_contract.MissingImageError("missing")
        return reference_ids[reference]

    def run(command: list[str], _child_env: dict[str, str], **_kwargs) -> int:
        assert command[:2] == ["docker", "pull"]
        pulls.append(command[2])
        available.add(command[2])
        return 0

    monkeypatch.setattr(runner, "_inspect_image_id", inspect)
    monkeypatch.setattr(runner, "_run", run)

    actual = runner._prepare_required_external_images(tmp_path / "selected.env", tmp_path, {})

    assert actual == expected_ids
    assert pulls == [python_base]


def test_images_prepare_rejects_mutable_reference_before_pull(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    config, _references, _image_ids = _image_contract_fixture()
    service = runner._THIRD_PARTY_SERVICES[0]
    config["services"][service]["image"] = "registry.example/mutable:latest"
    pulls: list[list[str]] = []
    monkeypatch.setattr(runner, "_rendered_services", lambda *_args, **_kwargs: config["services"])
    monkeypatch.setattr(runner, "_run", lambda command, _env, **_kwargs: pulls.append(command) or 0)

    with pytest.raises(runner.SelectedEnvError, match="digest pin"):
        runner._prepare_required_external_images(tmp_path / "selected.env", tmp_path, {})

    assert pulls == []


def test_images_prepare_verifies_host_boundary_with_pulled_postgres_image(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    image_ids = {service: f"sha256:{index:064x}" for index, service in enumerate(runner._THIRD_PARTY_SERVICES, 1)}
    identity = {
        "endpoint": "unix:///run/docker.sock",
        "id": "engine",
        "socket_device": 1,
        "socket_inode": 2,
        "socket_uid": os.getuid(),
        "socket_mode": stat.S_IFSOCK | 0o660,
    }
    probes: list[tuple[object, str]] = []
    monkeypatch.setattr(runner, "_verify_required_external_images", lambda *_args: dict(image_ids))
    monkeypatch.setattr(
        runner,
        "_verify_local_daemon",
        lambda _env, expected, image: probes.append((expected, image)),
    )

    runner._verify_daemon_after_operation(
        "images-prepare",
        tmp_path / "selected.env",
        tmp_path,
        {},
        "4.0.1",
        "a" * 64,
        identity,
        None,
        None,
    )

    assert probes == [(identity, image_ids["langfuse-postgres"])]


def test_stack_image_contract_rejects_mutable_third_party_and_image_id_drift(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _load_selected_env_runner()
    config, references, image_ids = _image_contract_fixture()
    source_digest = "a" * 64

    def output(command, _child_env):
        if command[-3:] == ["config", "--format", "json"]:
            return json.dumps(config)
        if command[:3] == ["docker", "image", "inspect"]:
            service = references[command[3]]
            labels = {"io.agentgov.source-artifact-sha256": source_digest} if service in runner._LOCAL_IMAGES else None
            return json.dumps([{"Id": image_ids[service], "Config": {"Labels": labels}}])
        raise AssertionError(command)

    third_party = runner._THIRD_PARTY_SERVICES[0]
    config["services"][third_party]["image"] = "registry.example/mutable:latest"
    monkeypatch.setattr(runner, "_run_output", output)
    with pytest.raises(runner.SelectedEnvError, match="digest pin"):
        runner._verify_stack_images({}, tmp_path / "selected.env", "4.0.0", source_digest, langfuse=True, running=False)

    config, references, image_ids = _image_contract_fixture()
    expected_ids = dict(image_ids)
    expected_ids["agent-gov-api"] = "sha256:" + "f" * 64
    with pytest.raises(runner.SelectedEnvError, match="identity 漂移"):
        runner._verify_stack_images(
            {},
            tmp_path / "selected.env",
            "4.0.0",
            source_digest,
            langfuse=True,
            running=False,
            expected_ids=expected_ids,
        )


def test_stack_image_contract_rejects_running_container_image_drift(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    config, references, image_ids = _image_contract_fixture()
    source_digest = "a" * 64

    def output(command, _child_env):
        if command[-3:] == ["config", "--format", "json"]:
            return json.dumps(config)
        if command[:3] == ["docker", "image", "inspect"]:
            service = references[command[3]]
            labels = {"io.agentgov.source-artifact-sha256": source_digest} if service in runner._LOCAL_IMAGES else None
            return json.dumps([{"Id": image_ids[service], "Config": {"Labels": labels}}])
        if "ps" in command and "-q" in command:
            return f"container-{command[-1]}"
        if command[:3] == ["docker", "container", "inspect"]:
            return json.dumps(
                [
                    {
                        "Image": "sha256:" + "f" * 64,
                        "Config": {"Env": [], "Labels": {}},
                        "HostConfig": {"PortBindings": {}, "Tmpfs": {}},
                        "Mounts": [],
                    },
                ],
            )
        raise AssertionError(command)

    monkeypatch.setattr(runner, "_run_output", output)

    with pytest.raises(runner.SelectedEnvError, match="运行容器未使用"):
        runner._verify_stack_images(
            {},
            tmp_path / "selected.env",
            "4.0.0",
            source_digest,
            langfuse=True,
            running=True,
            expected_ids=image_ids,
        )
