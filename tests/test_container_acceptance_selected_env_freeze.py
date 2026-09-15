from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_selected_env_operation as runner  # noqa: E402
from scripts import selected_env_container_contract as container_contract  # noqa: E402
from scripts import selected_env_image_inventory as inventory  # noqa: E402
from scripts import selected_env_operation_contract as operation_contract  # noqa: E402
from scripts import selected_env_persistent_source as persistent_source  # noqa: E402
from scripts import selected_env_python_toolchain as python_toolchain  # noqa: E402
from scripts import selected_env_source_snapshot as source_snapshot  # noqa: E402

_DIGEST = "a" * 64
_IMAGE_ID = "sha256:" + "b" * 64


def _mini_freezer(_repo_root: Path, destination: Path) -> str:
    destination.mkdir(mode=0o700)
    (destination / "VERSION").write_text("4.0.0\n", encoding="utf-8")
    (destination / "sentinel").write_text("safe\n", encoding="utf-8")
    for name in ("runtime-bootstrap", "api-gate"):
        bind = destination / "docker" / name
        bind.mkdir(parents=True)
        (bind / "payload").write_text(f"{name}\n", encoding="utf-8")
    return _DIGEST


def _mini_source_hash(root: Path) -> str:
    try:
        safe = (root / "sentinel").read_text(encoding="utf-8") == "safe\n"
    except OSError:
        safe = False
    return _DIGEST if safe else "f" * 64


def _base_image_config() -> container_contract.ImageConfig:
    return container_contract.ImageConfig(
        environment=(("BASE", "1"),),
        labels=(("base", "image"),),
        user="1000:1000",
        entrypoint=("python",),
        command=("app.py",),
        healthcheck=(("test", ("CMD", "image-health")), ("interval", 30_000_000_000)),
        exposed_ports=("9000/tcp",),
    )


def _service_config(bind_source: Path) -> dict[str, object]:
    return {
        "container_name": "agent-gov-api",
        "deploy": {"replicas": 1},
        "init": True,
        "extra_hosts": ["host.docker.internal=host-gateway"],
        "environment": {"BASE": "2", "APP": "safe"},
        "labels": {"role": "api"},
        "volumes": [
            {"type": "bind", "source": bind_source.as_posix(), "target": "/source", "read_only": True},
            {"type": "volume", "source": "agentgov_data", "target": "/data", "read_only": False},
        ],
        "tmpfs": ["/tmp"],
        "healthcheck": {
            "test": ["CMD", "service-health"],
            "interval": "5s",
            "timeout": "1s",
            "start_period": "3s",
            "retries": 2,
        },
        "restart": "unless-stopped",
        "logging": {"driver": "json-file", "options": {"max-size": "50m", "max-file": "5"}},
        "expose": ["8090"],
        "ports": [{"target": 8080, "published": "50400", "host_ip": "127.0.0.1", "protocol": "tcp"}],
        "privileged": False,
        "cap_add": ["NET_BIND_SERVICE"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "devices": [{"source": "/dev/null", "target": "/dev/agentgov-null", "permissions": "r"}],
        "read_only": True,
        "network_mode": "none",
        "pid": "host",
        "ipc": "shareable",
        "user": "2000:2000",
        "entrypoint": ["/bin/agentgov"],
        "command": ["--serve"],
    }


def _matching_container(bind_source: Path) -> dict[str, object]:
    return {
        "Name": "/agent-gov-api",
        "Image": _IMAGE_ID,
        "Config": {
            "Env": ["APP=safe", "BASE=2"],
            "Labels": {
                "base": "image",
                "role": "api",
                "com.docker.compose.project": "agent-gov",
            },
            "User": "2000:2000",
            "Entrypoint": ["/bin/agentgov"],
            "Cmd": ["--serve"],
            "Healthcheck": {
                "Test": ["CMD", "service-health"],
                "Interval": 5_000_000_000,
                "Timeout": 1_000_000_000,
                "StartPeriod": 3_000_000_000,
                "StartInterval": 0,
                "Retries": 2,
            },
            "ExposedPorts": {"8080/tcp": {}, "8090/tcp": {}, "9000/tcp": {}},
        },
        "HostConfig": {
            "Init": True,
            "ExtraHosts": ["host.docker.internal:host-gateway"],
            "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "50400"}]},
            "Tmpfs": {"/tmp": ""},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "50m", "max-file": "5"}},
            "Privileged": False,
            "CapAdd": ["CAP_NET_BIND_SERVICE"],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "MaskedPaths": ["/proc/kcore"],
            "ReadonlyPaths": ["/proc/sys"],
            "Devices": [
                {
                    "PathOnHost": "/dev/null",
                    "PathInContainer": "/dev/agentgov-null",
                    "CgroupPermissions": "r",
                },
            ],
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "PidMode": "host",
            "IpcMode": "shareable",
        },
        "Mounts": [
            {"Type": "bind", "Source": bind_source.as_posix(), "Destination": "/source", "RW": False},
            {
                "Type": "volume",
                "Name": "agentgov_data",
                "Source": "/var/lib/docker/volumes/agentgov_data/_data",
                "Destination": "/data",
                "RW": True,
            },
        ],
        "NetworkSettings": {"Networks": {}},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
    }


def test_every_mutating_selected_env_operation_freezes_deployable_source() -> None:
    assert frozenset(operation_contract.OPERATIONS) == operation_contract.SOURCE_FREEZE_OPERATIONS
    assert operation_contract.DOCKER_MUTATING_OPERATIONS <= operation_contract.SOURCE_FREEZE_OPERATIONS
    assert {"ui-up", "langfuse-prepare"} <= operation_contract.DOCKER_MUTATING_OPERATIONS
    assert {"runtime-bootstrap", "runtime-clean", "runtime-migrate"} <= operation_contract.HOST_MUTATING_OPERATIONS


def test_command_runner_executes_snapshot_bytes_and_rechecks_after_command(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    script = source / "scripts/probe.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('snapshot-safe')\n", encoding="utf-8")
    child_env = {
        source_snapshot.SOURCE_ROOT_ENV: source.as_posix(),
        source_snapshot.SOURCE_DIGEST_ENV: _DIGEST,
    }
    monkeypatch.setattr(runner, "source_artifact_sha256", lambda root: _DIGEST if root == source else "f" * 64)
    monkeypatch.setattr(runner.python_toolchain, "bind_python_command", lambda command, _env: command)

    output = runner._run_output([sys.executable, "scripts/probe.py"], child_env)

    assert output == "snapshot-safe"


def test_command_sync_barrier_rejects_snapshot_mutation_before_and_during_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sentinel = source / "sentinel"
    sentinel.write_text("safe\n", encoding="utf-8")
    child_env = {
        source_snapshot.SOURCE_ROOT_ENV: source.as_posix(),
        source_snapshot.SOURCE_DIGEST_ENV: _DIGEST,
    }
    monkeypatch.setattr(runner, "source_artifact_sha256", _mini_source_hash)
    sentinel.write_text("hostile-before\n", encoding="utf-8")
    called = False

    def must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(runner.subprocess, "run", must_not_run)
    with pytest.raises(runner.SelectedEnvError, match="同步屏障"):
        runner._run(["true"], child_env)
    assert not called

    operation_root = tmp_path / "operation"
    operation_root.mkdir()
    selected = operation_root / "selected.env"
    selected.write_text("A=1\n", encoding="utf-8")
    frozen = source_snapshot.freeze_operation_source(
        tmp_path,
        operation_root,
        selected,
        persistent_binds=False,
        freeze_source=_mini_freezer,
        hash_source=_mini_source_hash,
    )
    frozen_sentinel = frozen.root / "sentinel"
    frozen_metadata = frozen_sentinel.stat()
    frozen_mode = frozen_metadata.st_mode & 0o777
    frozen_env = source_snapshot.source_environment(frozen)

    def mutate_during(*_args, **_kwargs):
        frozen_sentinel.chmod(0o600)
        frozen_sentinel.write_text("hostile-during\n", encoding="utf-8")
        frozen_sentinel.write_text("safe\n", encoding="utf-8")
        os.utime(frozen_sentinel, ns=(frozen_metadata.st_atime_ns, frozen_metadata.st_mtime_ns))
        frozen_sentinel.chmod(frozen_mode)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", mutate_during)
    with pytest.raises(runner.SelectedEnvError, match="瞬时变化"):
        runner._run(["true"], frozen_env)


def test_selected_env_input_rejects_transient_mutate_and_restore(tmp_path: Path, monkeypatch) -> None:
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    snapshot = source_snapshot.write_operation_input(operation_root, b"A=safe\n")
    environment = source_snapshot.seal_operation_input(snapshot)
    source = tmp_path / "source"
    source.mkdir()
    environment.update(
        {
            source_snapshot.SOURCE_ROOT_ENV: source.as_posix(),
            source_snapshot.SOURCE_DIGEST_ENV: _DIGEST,
        }
    )
    metadata = snapshot.stat()
    monkeypatch.setattr(runner, "source_artifact_sha256", lambda _root: _DIGEST)

    def mutate_during(*_args, **_kwargs):
        snapshot.chmod(0o600)
        snapshot.write_bytes(b"A=hostile\n")
        snapshot.write_bytes(b"A=safe\n")
        os.utime(snapshot, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        snapshot.chmod(0o400)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", mutate_during)
    with pytest.raises(runner.SelectedEnvError, match="瞬时变化"):
        runner._run(["true"], environment)


def test_python_command_fails_closed_without_frozen_dependency_boundary() -> None:
    with pytest.raises(runner.SelectedEnvError, match="冻结解释器"):
        python_toolchain.bind_python_command(["python", "-c", "print('unsafe')"], {})


def test_frozen_python_dependency_is_independent_from_live_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    live_dependency = tmp_path / "live/frozen_probe.py"
    live_dependency.parent.mkdir()
    live_dependency.write_text("VALUE = 'snapshot-safe'\n", encoding="utf-8")
    dependency = python_toolchain._snapshot_source(live_dependency, Path("frozen_probe.py"))
    load_dependencies = python_toolchain._dependency_source_files
    monkeypatch.setattr(
        python_toolchain,
        "_dependency_source_files",
        lambda names: (*load_dependencies(names), dependency),
    )
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)

    environment = python_toolchain.prepare_python_toolchain(operation_root)
    live_dependency.write_text("VALUE = 'hostile-live-source'\n", encoding="utf-8")
    command = python_toolchain.bind_python_command(
        [
            "python",
            "-c",
            "import dotenv, frozen_probe, pydantic, sqlalchemy, yaml; print(frozen_probe.VALUE)",
        ],
        environment,
    )
    result = subprocess.run(
        command,
        env={**os.environ, **environment},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "snapshot-safe"


def test_persistent_compose_binds_are_separate_from_command_snapshot(tmp_path: Path, monkeypatch) -> None:
    cas_root = tmp_path / "root-owned-cas"
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    selected = operation_root / "selected.env"
    selected.write_text("HOST_RUNTIME_VOLUME_ROOT=/unused\n", encoding="utf-8")
    frozen = source_snapshot.freeze_operation_source(
        tmp_path,
        operation_root,
        selected,
        persistent_binds=True,
        freeze_source=_mini_freezer,
        hash_source=_mini_source_hash,
    )
    environment = source_snapshot.source_environment(frozen)

    assert frozen.root == operation_root / "source-snapshot"
    assert frozen.persistent_root == cas_root / _DIGEST / "source"
    assert environment["RUNTIME_BOOTSTRAP_HOST_DIR"] == (frozen.persistent_root / "docker/runtime-bootstrap").as_posix()
    assert environment["AGENTGOV_API_GATE_STATE_DIR_HOST"] == (frozen.persistent_root / "docker/api-gate").as_posix()
    assert not environment["RUNTIME_BOOTSTRAP_HOST_DIR"].startswith(runner.REPO_ROOT.as_posix())
    for path in (frozen.root, *frozen.root.rglob("*")):
        assert not path.lstat().st_mode & 0o222
    assert source_snapshot.verify_command_source(environment, hash_source=_mini_source_hash) == frozen.root


def test_persistent_operation_command_snapshot_tamper_fails_barrier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", tmp_path / "root-owned-cas")
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    selected = operation_root / "selected.env"
    selected.write_text("HOST_RUNTIME_VOLUME_ROOT=/unused\n", encoding="utf-8")
    frozen = source_snapshot.freeze_operation_source(
        tmp_path,
        operation_root,
        selected,
        persistent_binds=True,
        freeze_source=_mini_freezer,
        hash_source=_mini_source_hash,
    )
    environment = source_snapshot.source_environment(frozen)
    payload = frozen.root / "docker/runtime-bootstrap/payload"
    payload.chmod(0o600)
    payload.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(runner.SelectedEnvError, match="source"):
        source_snapshot.verify_command_source(environment, hash_source=_mini_source_hash)


def test_selected_env_overrides_hostile_docker_config_with_private_bound_plugin_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    hostile = tmp_path / "hostile-docker-config"
    hostile.mkdir()
    (hostile / "cli-plugins").mkdir()
    observed: dict[str, object] = {}
    frozen = source_snapshot.FrozenDeploymentSource(runner.REPO_ROOT, _DIGEST)

    monkeypatch.setattr(
        runner.selected_env_reexec,
        "freeze_deployment_source",
        lambda *_args: (frozen, "4.0.0"),
    )

    def selected_env_child(*_args, **kwargs) -> dict[str, str]:
        return {**kwargs.get("explicit", {}), "DOCKER_CONFIG": hostile.as_posix(), "PATH": "/hostile"}

    monkeypatch.setattr(source_snapshot, "selected_env_child_env", selected_env_child)
    monkeypatch.setattr(source_snapshot.python_toolchain, "prepare_python_toolchain", lambda *_args: {})

    def launch(_command: list[str], child_env: dict[str, str]) -> int:
        docker_config = Path(child_env["DOCKER_CONFIG"])
        docker_cli = source_snapshot.verify_docker_toolchain(child_env)
        observed.update(
            path=docker_config,
            mode=docker_config.stat().st_mode & 0o777,
            entries=[entry.name for entry in docker_config.iterdir()],
            docker_cli=docker_cli,
        )
        return 0

    monkeypatch.setattr(runner, "_run", launch)

    assert runner.run_operation(selected, "check") == 0
    assert observed["path"] != hostile
    assert observed["mode"] == 0o500
    assert observed["entries"] == ["cli-plugins"]
    assert Path(observed["docker_cli"]).parent == Path(observed["path"]).parent


def test_docker_commands_bind_verified_cli_and_compose_plugin_copies(tmp_path: Path, monkeypatch) -> None:
    trusted_cli = tmp_path / "trusted-docker"
    trusted_plugin = tmp_path / "trusted-compose"
    trusted_cli.write_bytes(b"docker-cli")
    trusted_plugin.write_bytes(b"compose-plugin")
    trusted_cli.chmod(0o555)
    trusted_plugin.chmod(0o555)
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    monkeypatch.setattr(source_snapshot, "_DOCKER_CANDIDATES", (trusted_cli,))
    monkeypatch.setattr(source_snapshot, "_COMPOSE_PLUGIN_CANDIDATES", (trusted_plugin,))

    trusted_buildx = tmp_path / "trusted-buildx"
    trusted_buildx.write_bytes(b"buildx-plugin")
    trusted_buildx.chmod(0o555)
    monkeypatch.setattr(source_snapshot, "_BUILDX_PLUGIN_CANDIDATES", (trusted_buildx,))

    environment = source_snapshot.prepare_docker_toolchain(operation_root, include_buildx=True)
    bound = source_snapshot.bind_docker_command(["docker", "compose", "version"], environment)

    assert Path(bound[0]) == Path(environment[source_snapshot.DOCKER_CLI_ENV])
    assert Path(environment[source_snapshot.COMPOSE_PLUGIN_ENV]).read_bytes() == b"compose-plugin"
    assert Path(environment[source_snapshot.BUILDX_PLUGIN_ENV]).read_bytes() == b"buildx-plugin"
    assert (operation_root / "docker-toolchain/docker-config").stat().st_mode & 0o777 == 0o500
    assert environment[source_snapshot.BUILDX_CONFIG_ENV] == str(operation_root / "buildx-state")
    assert environment[source_snapshot.BUILDX_BUILDER_ENV] == "default"
    (operation_root / "buildx-state" / "state").write_text("local build state", encoding="utf-8")
    source_snapshot.verify_docker_toolchain(environment)
    poisoned = {**environment, source_snapshot.BUILDX_CONFIG_ENV: str(tmp_path / "untrusted")}
    with pytest.raises(runner.SelectedEnvError, match="Buildx state/builder"):
        source_snapshot.verify_docker_toolchain(poisoned)
    poisoned = {**environment, source_snapshot.BUILDX_BUILDER_ENV: "remote"}
    with pytest.raises(runner.SelectedEnvError, match="Buildx state/builder"):
        source_snapshot.verify_docker_toolchain(poisoned)
    docker_config = operation_root / "docker-toolchain/docker-config"
    docker_config.chmod(0o700)
    (docker_config / ".token_seed.lock").write_text("unbound", encoding="utf-8")
    with pytest.raises(runner.SelectedEnvError, match="Docker config"):
        source_snapshot.verify_docker_toolchain(environment)
    (docker_config / ".token_seed.lock").unlink()
    docker_config.chmod(0o500)
    copied_plugin = Path(environment[source_snapshot.COMPOSE_PLUGIN_ENV])
    copied_plugin.chmod(0o700)
    with pytest.raises(runner.SelectedEnvError, match="Compose plugin.*漂移"):
        source_snapshot.verify_docker_toolchain(environment)


def test_build_and_up_commands_forbid_implicit_pulls(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "_bootstrap", lambda *_args: None)
    monkeypatch.setattr(runner, "_prepare_harnesses", lambda *_args: None)
    monkeypatch.setattr(runner, "_langfuse_prepare", lambda *_args: None)
    monkeypatch.setattr(runner, "_run", lambda command, _env, **_kwargs: calls.append(command) or 0)

    for operation in ("build", "ui-build", "ui-up", "langfuse-up"):
        runner._execute_operation(
            operation,
            selected,
            tmp_path,
            tmp_path,
            {},
            no_build=False,
            force_recreate=False,
        )
    runner._start_stack(
        selected,
        tmp_path,
        tmp_path,
        {},
        langfuse=False,
        no_build=False,
        force_recreate=False,
    )

    build_commands = [command for command in calls if "build" in command]
    up_commands = [command for command in calls if "up" in command]
    assert build_commands and all("--pull=false" in command for command in build_commands)
    assert up_commands and all(command[index : index + 2] == ["--pull", "never"] for command in up_commands for index in [command.index("--pull")])


def test_build_fails_before_mutation_when_digest_prerequisite_is_missing(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: {"id": "daemon"})
    monkeypatch.setattr(
        runner,
        "_verify_required_external_images",
        lambda *_args: (_ for _ in ()).throw(runner.SelectedEnvError("missing prerequisite")),
    )
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda *_args: events.append("probe"))

    with pytest.raises(runner.SelectedEnvError, match="missing prerequisite"):
        runner._prepare_daemon_boundary("build", tmp_path / "env", tmp_path, {}, "4.0.0", _DIGEST)
    assert events == []


def test_clean_host_images_prepare_bootstraps_postgres_probe_before_other_pulls(tmp_path: Path) -> None:
    postgres = "postgres@sha256:" + "1" * 64
    redis = "redis@sha256:" + "2" * 64
    minio = "minio@sha256:" + "3" * 64
    references = {"redis": redis, "langfuse-postgres": postgres, "minio": minio}
    ids = {reference: f"sha256:{index:064x}" for index, reference in enumerate(references.values(), start=1)}
    available: set[str] = set()
    events: list[str] = []

    def inspect(reference: str, _env: dict[str, str], *, service: str) -> str:
        events.append(f"inspect:{service}")
        if reference not in available:
            raise operation_contract.MissingImageError("missing")
        return ids[reference]

    def pull(command: list[str], _env: dict[str, str]) -> int:
        reference = command[2]
        events.append(f"pull:{reference}")
        available.add(reference)
        return 0

    def verify(_snapshot: Path, _root: Path, _env: dict[str, str]) -> dict[str, str]:
        events.append("final-inventory")
        return {service: ids[reference] for service, reference in references.items()}

    inventory.prepare_required_external_images(
        tmp_path / "env",
        tmp_path,
        {},
        load_references=lambda *_args: references,
        inspect_image=inspect,
        run_command=pull,
        verify_inventory=verify,
        verify_bootstrap_probe=lambda image_id: events.append(f"nonce:{image_id}"),
        verify_before_bootstrap_pull=lambda: events.append("daemon-recheck"),
    )

    postgres_pull = events.index(f"pull:{postgres}")
    nonce = events.index(f"nonce:{ids[postgres]}")
    daemon_recheck = events.index("daemon-recheck")
    other_pulls = [events.index(f"pull:{reference}") for reference in (redis, minio)]
    assert daemon_recheck < postgres_pull < nonce < min(other_pulls)
    assert events[-1] == "final-inventory"


def test_images_prepare_does_not_pull_on_non_missing_inspect_failure(tmp_path: Path) -> None:
    postgres = "postgres@sha256:" + "1" * 64
    pulls: list[list[str]] = []

    with pytest.raises(runner.SelectedEnvError, match="daemon unavailable"):
        inventory.prepare_required_external_images(
            tmp_path / "env",
            tmp_path,
            {},
            load_references=lambda *_args: {"langfuse-postgres": postgres},
            inspect_image=lambda *_args, **_kwargs: (_ for _ in ()).throw(runner.SelectedEnvError("daemon unavailable")),
            run_command=lambda command, *_args: pulls.append(command) or 0,
            verify_inventory=lambda *_args: {},
        )
    assert pulls == []


def test_images_prepare_does_not_pull_after_malformed_available_image_metadata(tmp_path: Path) -> None:
    postgres = "postgres@sha256:" + "1" * 64
    pulls: list[list[str]] = []

    def output(command: list[str], _env: dict[str, str]) -> str:
        if command[2] == "ls":
            return json.dumps(
                {
                    "Repository": "postgres",
                    "Digest": "sha256:" + "1" * 64,
                    "ID": _IMAGE_ID,
                }
            )
        if command[2] == "inspect":
            return "{not-json"
        raise AssertionError(command)

    def inspect(reference: str, child_env: dict[str, str], *, service: str) -> str:
        return inventory.inspect_image_id(reference, child_env, service=service, run_output=output)

    with pytest.raises(runner.SelectedEnvError, match="不是 JSON"):
        inventory.prepare_required_external_images(
            tmp_path / "env",
            tmp_path,
            {},
            load_references=lambda *_args: {"langfuse-postgres": postgres},
            inspect_image=inspect,
            run_command=lambda command, *_args: pulls.append(command) or 0,
            verify_inventory=lambda *_args: {},
        )
    assert pulls == []


def test_digest_inventory_accepts_tagged_reference_without_filtered_image_ls() -> None:
    reference = "docker.io/postgres:17.9@sha256:" + "1" * 64
    commands: list[list[str]] = []

    def output(command: list[str], _env: dict[str, str]) -> str:
        commands.append(command)
        if command[2] == "ls":
            return json.dumps(
                {
                    "Repository": "postgres",
                    "Digest": "sha256:" + "1" * 64,
                    "ID": _IMAGE_ID,
                }
            )
        if command[2] == "inspect":
            return json.dumps([{"Id": _IMAGE_ID}])
        raise AssertionError(command)

    assert inventory.inspect_image_id(reference, {}, service="langfuse-postgres", run_output=output) == _IMAGE_ID
    assert reference not in commands[0]
    assert commands[1][-1] == reference


@pytest.mark.parametrize(
    ("inventory_lines", "error"),
    [
        ("{not-json", "不是 JSON"),
        (json.dumps({"Repository": "postgres", "Digest": "sha256:" + "1" * 64}), "字段无效"),
        (
            "\n".join(json.dumps({"Repository": "postgres", "Digest": "sha256:" + "1" * 64, "ID": image_id}) for image_id in (_IMAGE_ID, "sha256:" + "c" * 64)),
            "不唯一",
        ),
    ],
)
def test_digest_inventory_rejects_invalid_identity_without_pull(inventory_lines: str, error: str) -> None:
    reference = "docker.io/postgres:17.9@sha256:" + "1" * 64
    commands: list[list[str]] = []

    def output(command: list[str], _env: dict[str, str]) -> str:
        commands.append(command)
        if command[2] == "ls":
            return inventory_lines
        raise AssertionError(command)

    with pytest.raises(runner.SelectedEnvError, match=error):
        inventory.inspect_image_id(reference, {}, service="langfuse-postgres", run_output=output)
    assert len(commands) == 1


def test_images_prepare_daemon_probe_does_not_treat_boundary_failure_as_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: {"id": "daemon"})
    monkeypatch.setattr(
        runner,
        "_inspect_image_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(runner.SelectedEnvError("daemon unavailable")),
    )

    with pytest.raises(runner.SelectedEnvError, match="daemon unavailable"):
        runner._prepare_daemon_boundary("images-prepare", tmp_path / "env", tmp_path, {}, "4.0.0", _DIGEST)


def test_images_prepare_rejects_final_image_id_drift(tmp_path: Path) -> None:
    postgres = "postgres@sha256:" + "1" * 64
    references = {"langfuse-postgres": postgres}
    stable_id = "sha256:" + "2" * 64

    with pytest.raises(runner.SelectedEnvError, match="identity.*漂移"):
        inventory.prepare_required_external_images(
            tmp_path / "env",
            tmp_path,
            {},
            load_references=lambda *_args: references,
            inspect_image=lambda *_args, **_kwargs: stable_id,
            run_command=lambda *_args: 0,
            verify_inventory=lambda *_args: {"langfuse-postgres": "sha256:" + "3" * 64},
        )


def test_container_config_projection_accepts_exact_frozen_compose(tmp_path: Path) -> None:
    bind_source = tmp_path / "bind"
    bind_source.mkdir()
    rendered = json.dumps([_matching_container(bind_source)])

    container_contract.verify_container_config(
        rendered,
        _service_config(bind_source),
        _base_image_config(),
        expected_image_id=_IMAGE_ID,
        project_name=lambda: "agent-gov",
        service="agent-gov-api",
    )


def test_container_config_projection_rejects_unsupported_deploy_constraints(tmp_path: Path) -> None:
    bind_source = tmp_path / "bind"
    bind_source.mkdir()
    service_config = _service_config(bind_source)
    service_config["deploy"] = {"replicas": 2}

    with pytest.raises(runner.SelectedEnvError, match="deploy"):
        container_contract.verify_container_config(
            json.dumps([_matching_container(bind_source)]),
            service_config,
            _base_image_config(),
            expected_image_id=_IMAGE_ID,
            project_name=lambda: "agent-gov",
            service="agent-gov-api",
        )


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("environment", lambda value: value["Config"].update(Env=["APP=hostile", "BASE=2"])),
        ("labels", lambda value: value["Config"]["Labels"].update(role="hostile")),
        ("mounts", lambda value: value["Mounts"][0].update(Source="/hostile")),
        ("ports", lambda value: value["HostConfig"]["PortBindings"]["8080/tcp"][0].update(HostPort="50499")),
        ("privileged", lambda value: value["HostConfig"].update(Privileged=True)),
        ("cap_add", lambda value: value["HostConfig"].update(CapAdd=["CAP_SYS_ADMIN"])),
        ("cap_drop", lambda value: value["HostConfig"].update(CapDrop=[])),
        ("security_opt", lambda value: value["HostConfig"].update(SecurityOpt=[])),
        ("devices", lambda value: value["HostConfig"].update(Devices=[])),
        ("read_only_rootfs", lambda value: value["HostConfig"].update(ReadonlyRootfs=False)),
        ("network_mode", lambda value: value["HostConfig"].update(NetworkMode="host")),
        ("pid_mode", lambda value: value["HostConfig"].update(PidMode="")),
        ("ipc_mode", lambda value: value["HostConfig"].update(IpcMode="private")),
        ("user", lambda value: value["Config"].update(User="0:0")),
        ("entrypoint", lambda value: value["Config"].update(Entrypoint=["/bin/sh"])),
        ("command", lambda value: value["Config"].update(Cmd=["--hostile"])),
        ("container_name", lambda value: value.update(Name="/hostile")),
        ("init", lambda value: value["HostConfig"].update(Init=False)),
        ("extra_hosts", lambda value: value["HostConfig"].update(ExtraHosts=["hostile:127.0.0.1"])),
        ("healthcheck", lambda value: value["Config"]["Healthcheck"].update(Retries=9)),
        ("restart", lambda value: value["HostConfig"]["RestartPolicy"].update(Name="always")),
        ("logging", lambda value: value["HostConfig"]["LogConfig"]["Config"].update(**{"max-file": "99"})),
        ("exposed_ports", lambda value: value["Config"]["ExposedPorts"].update({"9999/tcp": {}})),
        ("tmpfs", lambda value: value["HostConfig"].update(Tmpfs={"/tmp": "ro"})),
        ("networks", lambda value: value["NetworkSettings"].update(Networks={"hostile": {}})),
        ("running", lambda value: value["State"].update(Running=False)),
        ("health", lambda value: value["State"]["Health"].update(Status="unhealthy")),
    ],
)
def test_container_config_projection_rejects_hostile_actual_drift(
    tmp_path: Path,
    field: str,
    mutate,
) -> None:
    bind_source = tmp_path / "bind"
    bind_source.mkdir()
    container = copy.deepcopy(_matching_container(bind_source))
    mutate(container)

    with pytest.raises(runner.SelectedEnvError, match="配置不匹配"):
        container_contract.verify_container_config(
            json.dumps([container]),
            _service_config(bind_source),
            _base_image_config(),
            expected_image_id=_IMAGE_ID,
            project_name=lambda: "agent-gov",
            service=f"agent-gov-api:{field}",
        )


def test_ui_postconditions_are_scoped_to_ui_image_and_running_state(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, bool, tuple[str, ...] | None]] = []
    probes: list[str] = []

    def verify(*_args, **kwargs) -> dict[str, str]:
        calls.append((_args[0].get("operation", ""), kwargs["running"], kwargs["services"]))
        return {"agent-gov-ui": _IMAGE_ID}

    monkeypatch.setattr(runner, "_verify_stack_images", verify)
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda _env, _identity, image: probes.append(image))
    for operation in ("ui-build", "ui-up"):
        runner._verify_daemon_after_operation(
            operation,
            tmp_path / "env",
            tmp_path,
            {"operation": operation},
            "4.0.0",
            _DIGEST,
            {"id": "daemon"},
            _IMAGE_ID,
            {"agent-gov-ui": _IMAGE_ID} if operation == "ui-up" else None,
        )

    assert calls == [
        ("ui-build", False, ("agent-gov-ui",)),
        ("ui-up", True, ("agent-gov-ui",)),
    ]
    assert probes == [_IMAGE_ID, _IMAGE_ID]


def test_deploy_scripts_have_no_pull_fallback_and_isolate_compose_plugins() -> None:
    source = (runner.REPO_ROOT / "scripts/deploy_agent_gov_to_host").read_text(encoding="utf-8")
    runtime_source = (runner.REPO_ROOT / "scripts/remote_deploy_runtime.py").read_text(encoding="utf-8")

    assert " docker pull " not in source
    assert 'DOCKER_CONFIG="$LOCAL_DOCKER_CONFIG"' in source
    assert '"$LOCAL_DOCKER_BIN")' in source
    assert '"$boundary/docker" info' in source
    assert "compose=(" not in source
    assert " docker pull " not in runtime_source
    assert '"DOCKER_CONFIG": docker_config.as_posix()' in runtime_source
    assert 'yield DockerBinding(command=[bound_cli.as_posix()], environment=environment)' in runtime_source
    assert '[*binding["command"], "compose"' in runtime_source
