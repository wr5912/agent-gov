from __future__ import annotations

import importlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_selected_env_runner() -> ModuleType:
    return importlib.import_module("scripts.run_selected_env_operation")


def test_selected_env_child_removes_hostile_ambient_and_rejects_control_explicit(tmp_path, monkeypatch) -> None:
    images = importlib.import_module("scripts.agentscope_atomic_cutover_images")
    env_file = tmp_path / "selected.env"
    env_file.write_text(
        "HOME=/selected/operator\nHOST_RUNTIME_VOLUME_ROOT=/selected/operator/volume-agent-gov\nAGENTGOV_API_MODE=open\n",
        encoding="utf-8",
    )
    hostile = {
        "HOST_RUNTIME_VOLUME_ROOT": "/tmp/ambient-root",
        "AGENTGOV_API_MODE": "drain",
        "COMPOSE_FILE": "/tmp/hostile.yml",
        "COMPOSE_PROFILES": "hostile",
        "MAKEFLAGS": "--eval=hostile",
        "PYTHONPATH": "/tmp/hostile-python",
        "BASH_ENV": "/tmp/hostile-shell",
        "DOCKER_HOST": "tcp://remote.invalid:2376",
        "DOCKER_CONTEXT": "remote",
        "APP_VERSION": "hostile",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LANG", "C.UTF-8")

    child = images.selected_env_child_env(
        env_file,
        explicit={"APP_VERSION": "4.0.0"},
        compose_files=(
            REPO_ROOT / "docker/docker-compose.yml",
            REPO_ROOT / "docker/docker-compose.langfuse.yml",
        ),
    )

    assert child["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert child["LANG"] == "C.UTF-8"
    assert child["APP_VERSION"] == "4.0.0"
    for key in hostile:
        if key != "APP_VERSION":
            assert key not in child
    with pytest.raises(ValueError, match="未授权键"):
        images.selected_env_child_env(env_file, explicit={"PATH": "/tmp/hostile"})


def test_compose_interpolation_key_scan_handles_plain_nested_and_escaped_dollars(tmp_path) -> None:
    images = importlib.import_module("scripts.agentscope_atomic_cutover_images")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  api:\n    image: ${OUTER:-${INNER}}\n    command: $PLAIN $$ESCAPED $${ALSO_ESCAPED}\n",
        encoding="utf-8",
    )

    assert images.compose_interpolation_keys((compose,)) == {"OUTER", "INNER", "PLAIN"}


@pytest.mark.parametrize(
    "content",
    ("API_KEY=one\nAPI_KEY=two\n", "API_KEY='unterminated\n", "API_KEY=x\x00y\n"),
)
def test_selected_env_rejects_duplicate_or_invalid_dotenv(tmp_path, content: str) -> None:
    env_module = importlib.import_module("scripts.agentscope_atomic_cutover_env")
    env_file = tmp_path / "selected.env"
    env_file.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        env_module.parse_selected_env_bindings(env_file)


def test_selected_env_transaction_uses_snapshot_and_rejects_source_env_swap(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    env_file = tmp_path / "selected.env"
    original = "HOME=/operator\nHOST_RUNTIME_VOLUME_ROOT=/operator/volume-agent-gov\n"
    env_file.write_text(original, encoding="utf-8")
    observed: dict[str, str] = {}

    frozen = runner.source_snapshot.FrozenDeploymentSource(runner.REPO_ROOT, "a" * 64)
    monkeypatch.setattr(
        runner.selected_env_reexec,
        "freeze_deployment_source",
        lambda *_args: (frozen, "4.0.0"),
    )
    monkeypatch.setattr(
        runner.source_snapshot,
        "selected_env_child_env",
        lambda *_args, **kwargs: {**kwargs.get("explicit", {}), "PATH": "/usr/bin"},
    )
    monkeypatch.setattr(runner.source_snapshot.python_toolchain, "prepare_python_toolchain", lambda *_args: {})

    def launch(command: list[str], child_env: dict[str, str]) -> int:
        snapshot = Path(child_env[runner.source_snapshot.INPUT_FILE_ENV])
        state = runner.selected_env_reexec.load_frozen_stage(child_env)
        observed["operation"] = command[command.index("--operation") + 1]
        observed["snapshot"] = snapshot.read_text(encoding="utf-8")
        observed["source_base"] = command[command.index("--env-base-dir") + 1]
        env_file.write_text(original + "HOST_PORT=50401\n", encoding="utf-8")
        runner.verify_stable_env_file(
            state.original_env,
            snapshot.read_bytes(),
            state.original_identity,
            error_type=runner.SelectedEnvError,
        )
        return 0

    monkeypatch.setattr(runner, "_run", launch)

    with pytest.raises(runner.SelectedEnvError, match="事务.*发生变化"):
        runner.run_operation(env_file, "down")

    assert observed == {
        "operation": "down",
        "snapshot": original,
        "source_base": tmp_path.as_posix(),
    }


@pytest.mark.parametrize("link_parent", [False, True])
def test_selected_env_reader_rejects_leaf_and_parent_symlinks(tmp_path, link_parent: bool) -> None:
    runner = _load_selected_env_runner()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "selected.env"
    target.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    if link_parent:
        alias = tmp_path / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        selected = alias / "selected.env"
    else:
        selected = tmp_path / "selected-link.env"
        selected.symlink_to(target)

    with pytest.raises(runner.SelectedEnvError, match="符号链接"):
        runner._read_stable_regular_file(selected)


def test_selected_env_transaction_rejects_deployable_source_digest_drift(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    env_file = tmp_path / "selected.env"
    env_file.write_text("HOME=/operator\n", encoding="utf-8")
    digests = iter(("a" * 64, "b" * 64))

    monkeypatch.setattr(runner, "source_artifact_sha256", lambda _root: next(digests))
    monkeypatch.setattr(runner.source_snapshot, "selected_env_child_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "_prepare_daemon_boundary", lambda *_args, **_kwargs: (None, None, None))
    monkeypatch.setattr(runner, "_verify_daemon_after_operation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_execute_operation", lambda *_args, **_kwargs: 0)

    with pytest.raises(runner.SelectedEnvError, match="deployable source"):
        runner.run_operation(env_file, "down")


def test_selected_env_build_probes_fixed_host_boundary_before_build(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    calls: list[tuple[str, object]] = []
    postgres_image_id = "sha256:" + "a" * 64
    identity = {
        "endpoint": "unix:///run/docker.sock",
        "id": "engine",
        "socket_device": 1,
        "socket_inode": 2,
        "socket_uid": os.getuid(),
        "socket_mode": stat.S_IFSOCK | 0o660,
    }
    monkeypatch.setattr(
        runner,
        "_capture_local_daemon",
        lambda _env: calls.append(("capture", None)) or identity,
    )
    monkeypatch.setattr(
        runner,
        "_verify_local_daemon",
        lambda _env, expected, image: calls.append(("probe", (expected, image))),
    )
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("build pre-probe must not require app images")),
    )
    monkeypatch.setattr(
        runner,
        "_verify_required_external_images",
        lambda *_args: {"langfuse-postgres": postgres_image_id},
    )

    result = runner._prepare_daemon_boundary(
        "build",
        tmp_path / "selected.env",
        tmp_path,
        {"DOCKER_HOST": "unix:///var/run/docker.sock"},
        "4.0.0",
        "a" * 64,
    )

    assert calls == [
        ("capture", None),
        ("probe", (identity, postgres_image_id)),
    ]
    assert result == (identity, postgres_image_id, None)


def test_images_prepare_captures_daemon_without_requiring_preloaded_probe(tmp_path, monkeypatch) -> None:
    runner = _load_selected_env_runner()
    identity = {
        "endpoint": "unix:///run/docker.sock",
        "id": "engine",
        "socket_device": 1,
        "socket_inode": 2,
        "socket_uid": os.getuid(),
        "socket_mode": stat.S_IFSOCK | 0o660,
    }
    probes: list[str] = []
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: identity)
    monkeypatch.setattr(
        runner,
        "_inspect_image_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(runner.operation_contract.MissingImageError("missing")),
    )
    monkeypatch.setattr(
        runner,
        "_verify_local_daemon",
        lambda _env, _identity, image: probes.append(image),
    )

    result = runner._prepare_daemon_boundary(
        "images-prepare",
        tmp_path / "selected.env",
        tmp_path,
        {"DOCKER_HOST": "unix:///var/run/docker.sock"},
        "4.0.1",
        "a" * 64,
    )

    assert result == (identity, None, None)
    assert probes == []


def test_compose_diagnose_uses_frozen_python_without_ambient_python3(tmp_path: Path) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("HOST_PORT=1\n", encoding="utf-8")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "dirname").symlink_to(shutil.which("dirname"))
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CAPTURE_FILE"\n', encoding="utf-8")
    docker.chmod(0o700)
    capture = tmp_path / "docker-calls.txt"
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts/compose_diagnose.sh")],
        cwd=REPO_ROOT,
        env={
            "PATH": str(commands),
            "PYTHONHOME": sys.base_prefix,
            "PYTHONPATH": "",
            "AGENTGOV_OPERATION_PYTHON": sys.executable,
            "COMPOSE_ENV_FILE": str(selected),
            "DOCKER_CAPTURE_FILE": str(capture),
            "API_BASE": "http://127.0.0.1:1",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0
    assert "API: unhealthy" in result.stdout
    assert "python3: command not found" not in result.stderr
    assert f"--env-file {selected}" in capture.read_text(encoding="utf-8")


def test_compose_diagnose_rejects_missing_frozen_python(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts/compose_diagnose.sh")],
        cwd=REPO_ROOT,
        env={"AGENTGOV_OPERATION_PYTHON": str(tmp_path / "missing-python")},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    assert "Frozen Python interpreter is not executable" in result.stderr
    assert "=== Compose service state ===" not in result.stdout
