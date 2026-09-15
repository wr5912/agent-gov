from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from scripts import remote_deploy_transaction as transaction
from scripts.agentscope_atomic_cutover_bootstrap import freeze_deployable_source, source_artifact_sha256
from scripts.remote_deploy_python_toolchain import ToolchainError, build_bundle, verify_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts/deploy_agent_gov_to_host"


@pytest.fixture(autouse=True)
def restore_temporary_directory_access(tmp_path: Path) -> Iterator[None]:
    """让运行环境探针创建的 000 目录可由 pytest 正常回收。"""

    yield
    for root, directories, _files in os.walk(tmp_path, topdown=True, followlinks=False):
        for name in directories:
            entry = Path(root, name)
            if not entry.is_symlink() and stat.S_ISDIR(entry.lstat().st_mode):
                entry.chmod(0o700)


@pytest.fixture(scope="module")
def remote_python_toolchain_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("remote-python-toolchain")
    upload = root / "upload"
    build_bundle(REPO_ROOT, upload)
    digest = hashlib.sha256((upload / "manifest.json").read_bytes()).hexdigest()
    bundle = root / digest
    upload.rename(bundle)
    return bundle


def _copy_remote_deploy_source(destination: Path) -> Path:
    source = destination / "source"
    source.mkdir()
    shutil.copyfile(REPO_ROOT / "requirements-api.txt", source / "requirements-api.txt")
    shutil.copytree(
        REPO_ROOT / "scripts",
        source / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        REPO_ROOT / "app",
        source / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"),
    )
    return source


def _system_python() -> Path:
    remote_python = Path("/usr/bin/python3")
    return remote_python if remote_python.is_file() else Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)


def _verify_command(bundle: Path, source: Path) -> list[os.PathLike[str] | str]:
    return [
        _system_python(),
        "-S",
        bundle / "verify.py",
        "verify",
        "--bundle",
        bundle,
        "--source-root",
        source,
    ]


def _frozen_environment(bundle: Path, home: Path) -> dict[str, str]:
    return {
        "HOME": home.as_posix(),
        "PATH": os.defpath,
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "PYTHONHOME": (bundle / "runtime").as_posix(),
        "PYTHONPATH": (bundle / "dependencies").as_posix(),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _write_rsync_protocol_driver(path: Path) -> None:
    path.write_text(
        '#!/bin/sh\nset -eu\n[ "$#" -ge 2 ] || exit 64\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o700)


def _deployable_transfer_source(parent: Path) -> Path:
    source = parent / "deployable-source"
    freeze_deployable_source(REPO_ROOT, source)
    shutil.copyfile(REPO_ROOT / "docker/.env.example", source / "docker/.env.example")
    return source


def _rsync_source_stage(source: Path, stage: Path, driver: Path) -> None:
    stage.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "rsync",
            "-az",
            "--delete",
            "--protect-args",
            "-e",
            driver.as_posix(),
            "--exclude=/.git/",
            "--exclude=/.venv/",
            "--exclude=/images/",
            "--exclude=/docker/.env",
            "--exclude=/docker/.env.bak-*",
            "--exclude=/docker/.env.local-debug",
            "--exclude=/frontend/.env.local",
            f"{source}/",
            f"fixture:{stage}/",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _install_cached_toolchain(source: Path, live_root: Path) -> Path:
    destination = live_root / "images/python-toolchains" / source.name
    destination.parent.mkdir(mode=0o700, parents=True)
    shutil.copytree(source, destination)
    return destination


def _candidate_preflight_data_root(env_path: Path) -> Path:
    operator_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    suffix = hashlib.sha256(env_path.absolute().as_posix().encode()).hexdigest()[:20]
    return operator_home / "volume-agent-gov" / f".remote-deploy-preflight-unused-{suffix}"


def _valid_private_env(path: Path) -> bytes:
    content = (REPO_ROOT / "docker/.env.example").read_text(encoding="utf-8")
    preflight_data_root = _candidate_preflight_data_root(path)
    assert not preflight_data_root.exists()
    replacements = {
        "API_KEY=replace-with-private-api-key": "API_KEY=" + "a" * 40,
        "FRONTEND_RUNTIME_API_KEY=replace-with-private-api-key": "FRONTEND_RUNTIME_API_KEY=" + "a" * 40,
        "AGENTGOV_RUNTIME_SHARED_SECRET=": "AGENTGOV_RUNTIME_SHARED_SECRET=" + "b" * 64,
        "MODEL_PROVIDER_API_KEY=replace-with-private-provider-key": "MODEL_PROVIDER_API_KEY=" + "c" * 40,
    }
    for source, target in replacements.items():
        assert source in content
        content = content.replace(source, target, 1)
    content += f"\nHOST_DATA_MOUNT={preflight_data_root.as_posix()}\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path.read_bytes()


def _candidate_command(bundle: Path, stage: Path, live: Path, digest: str) -> list[str]:
    return [
        (bundle / "runtime/bin/python").as_posix(),
        (stage / "scripts/remote_deploy_python_toolchain.py").as_posix(),
        "prepare-candidate",
        "--bundle",
        bundle.as_posix(),
        "--stage-root",
        stage.as_posix(),
        "--live-root",
        live.as_posix(),
        "--source-sha256",
        digest,
    ]


def test_fresh_host_executes_frozen_python_toolchain_without_a_project_venv(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    source = _copy_remote_deploy_source(tmp_path)
    result = subprocess.run(
        _verify_command(remote_python_toolchain_bundle, source),
        cwd=source,
        env={"HOME": tmp_path.as_posix(), "PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "toolchain 校验通过" in result.stdout
    assert not (source / ".venv").exists()
    assert not (source / "docker/.env").exists()


def test_recovery_runner_is_verified_and_executable_without_live_or_stage_source(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    verify_bundle(remote_python_toolchain_bundle, require_cache_key=True)
    runner = remote_python_toolchain_bundle / "recovery/scripts/remote_deploy_runtime.py"

    result = subprocess.run(
        [(remote_python_toolchain_bundle / "runtime/bin/python").as_posix(), runner.as_posix(), "--help"],
        cwd=tmp_path,
        env=_frozen_environment(remote_python_toolchain_bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "recover" in result.stdout


@pytest.mark.parametrize("stage_damage", ["deleted", "corrupt"])
def test_frozen_runner_recovers_backup_when_live_is_missing_and_stage_is_unusable(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
    stage_damage: str,
) -> None:
    live = tmp_path / "agent gov"
    stage = tmp_path / "agent gov.stage.frozen-recovery"
    backup = tmp_path / "agent gov.previous.frozen-recovery"
    freeze_deployable_source(REPO_ROOT, live)
    freeze_deployable_source(REPO_ROOT, stage)
    (live / "VERSION").write_text("3.9.9\n", encoding="utf-8")
    (live / "old-source.marker").write_text("old\n", encoding="utf-8")
    private_env = _valid_private_env(live / "docker/.env")
    (stage / "docker/.env").write_bytes(private_env)
    candidate_digest = source_artifact_sha256(stage)
    old_digest = source_artifact_sha256(live)
    _payload, anchor = transaction.capture_live_env(live / "docker/.env")
    transaction.write_candidate_state(
        stage,
        transaction.CandidateState(
            live_root=live.as_posix(),
            stage_root=stage.as_posix(),
            source_sha256=candidate_digest,
            candidate_env_sha256=hashlib.sha256(private_env).hexdigest(),
            live_env=anchor,
        ),
    )
    transaction.begin_transaction(
        transaction_id="deploy-frozen-recovery-fixture",
        live_root=live,
        stage_root=stage,
        backup_root=backup,
        toolchain_root=remote_python_toolchain_bundle,
        source_sha256=candidate_digest,
        version="4.0.1",
        with_langfuse=False,
        project_archive=transaction.ArchiveIdentity((tmp_path / "candidate.tar.gz").as_posix(), "a" * 64),
        dependency_archive=None,
        old_version="3.9.9",
        old_source_sha256=old_digest,
        old_with_langfuse=False,
        old_images=(),
        old_archive=None,
    )
    os.rename(live, backup)
    if stage_damage == "deleted":
        shutil.rmtree(stage)
    else:
        (stage / "VERSION").write_text("corrupt\n", encoding="utf-8")
    runner = remote_python_toolchain_bundle / "recovery/scripts/remote_deploy_runtime.py"

    result = subprocess.run(
        [
            (remote_python_toolchain_bundle / "runtime/bin/python").as_posix(),
            runner.as_posix(),
            "recover",
            "--live-root",
            live.as_posix(),
        ],
        cwd=tmp_path,
        env=_frozen_environment(remote_python_toolchain_bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rolled-back"
    assert source_artifact_sha256(live) == old_digest
    assert (live / "old-source.marker").read_text(encoding="utf-8") == "old\n"
    assert not stage.exists() and not backup.exists()
    assert not transaction.transaction_path(live).exists()


def test_remote_stdlib_python_minor_may_differ_from_frozen_runtime(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    source = _copy_remote_deploy_source(tmp_path)
    system_minor = subprocess.check_output(
        [_system_python(), "-S", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
    ).strip()
    frozen_minor = subprocess.check_output(
        [remote_python_toolchain_bundle / "runtime/bin/python", "-S", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        env=_frozen_environment(remote_python_toolchain_bundle, tmp_path),
        text=True,
    ).strip()
    if system_minor == frozen_minor:
        pytest.skip("system verifier and frozen runtime use the same Python minor on this host")

    result = subprocess.run(
        _verify_command(remote_python_toolchain_bundle, source),
        cwd=source,
        env={"HOME": tmp_path.as_posix(), "PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_cache_key_and_unresolved_symlink_are_rejected(
    remote_python_toolchain_bundle: Path,
) -> None:
    wrong_name = remote_python_toolchain_bundle.with_name("wrong-cache-key")
    link = remote_python_toolchain_bundle.with_name("toolchain-link")
    remote_python_toolchain_bundle.rename(wrong_name)
    try:
        with pytest.raises(ToolchainError, match="cache key"):
            verify_bundle(wrong_name, REPO_ROOT, require_cache_key=True)
    finally:
        wrong_name.rename(remote_python_toolchain_bundle)
    link.symlink_to(remote_python_toolchain_bundle, target_is_directory=True)
    try:
        with pytest.raises(ToolchainError, match="绝对普通目录"):
            verify_bundle(link, REPO_ROOT)
    finally:
        link.unlink()


def test_content_addressed_python_toolchain_reuse_is_read_only(
    remote_python_toolchain_bundle: Path,
) -> None:
    before = {
        path.relative_to(remote_python_toolchain_bundle).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in remote_python_toolchain_bundle.rglob("*")
        if path.is_file()
    }

    verify_bundle(remote_python_toolchain_bundle, REPO_ROOT, require_cache_key=True)
    verify_bundle(remote_python_toolchain_bundle, REPO_ROOT, require_cache_key=True)

    after = {
        path.relative_to(remote_python_toolchain_bundle).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in remote_python_toolchain_bundle.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_corrupt_remote_python_toolchain_fails_without_touching_private_env(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    source = _copy_remote_deploy_source(tmp_path)
    private_env = source / "docker/.env"
    private_env.parent.mkdir()
    private_env.write_bytes(b"existing private environment bytes\n")
    before = private_env.read_bytes()
    damaged = tmp_path / "damaged-toolchain"
    shutil.copytree(remote_python_toolchain_bundle, damaged)
    target = damaged / "dependencies/dotenv/__init__.py"
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"\n")
    damaged.chmod(0o500)

    result = subprocess.run(
        _verify_command(damaged, source),
        cwd=source,
        env={"HOME": tmp_path.as_posix(), "PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "未执行候选 env、source 激活或 Compose" in result.stderr
    assert private_env.read_bytes() == before
    assert not (source / ".venv").exists()


def test_activate_cli_never_claims_live_state_is_unchanged_after_failure(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    live = tmp_path / "agent gov"
    stage = tmp_path / "agent gov.stage.failure"
    backup = tmp_path / "agent gov.previous.failure"
    live.mkdir()
    stage.mkdir()
    marker = live / "live.marker"
    marker.write_bytes(b"existing-live-state\n")

    result = subprocess.run(
        [
            (remote_python_toolchain_bundle / "runtime/bin/python").as_posix(),
            (REPO_ROOT / "scripts/remote_deploy_python_toolchain.py").as_posix(),
            "activate-candidate",
            "--bundle",
            remote_python_toolchain_bundle.as_posix(),
            "--stage-root",
            stage.as_posix(),
            "--live-root",
            live.as_posix(),
            "--backup-root",
            backup.as_posix(),
        ],
        cwd=REPO_ROOT,
        env=_frozen_environment(remote_python_toolchain_bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "live source/env 状态需要人工核验；未执行 Compose" in result.stderr
    assert "未触碰 live env" not in result.stderr
    assert tmp_path.as_posix() not in result.stderr
    assert marker.read_bytes() == b"existing-live-state\n"


def _container_ids() -> tuple[str, ...]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return tuple(sorted(result.stdout.splitlines()))


def test_real_rsync_stage_requires_durable_transaction_before_activation(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    host = tmp_path / "temporary host with spaces"
    host.mkdir()
    live = host / "agent gov"
    stage = host / "agent gov.stage.behavior"
    backup = host / "agent gov.previous.behavior"
    live.mkdir()
    (live / "old-source.marker").write_text("old source\n", encoding="utf-8")
    (live / ".git").mkdir()
    (live / ".git/config").write_text("existing remote git metadata\n", encoding="utf-8")
    (live / ".venv").mkdir()
    (live / ".venv/remote-only.marker").write_text("preserve\n", encoding="utf-8")
    original_env = _valid_private_env(live / "docker/.env")
    preflight_data_root = _candidate_preflight_data_root(live / "docker/.env")
    bundle = _install_cached_toolchain(remote_python_toolchain_bundle, live)
    driver = tmp_path / "rsync-shell"
    _write_rsync_protocol_driver(driver)
    transfer_source = _deployable_transfer_source(tmp_path)
    _rsync_source_stage(transfer_source, stage, driver)
    (stage / "candidate-source.marker").write_text("candidate source\n", encoding="utf-8")
    digest = source_artifact_sha256(stage)
    before_containers = _container_ids()
    exec_trace = tmp_path / "candidate-preflight-execve.trace"

    prepared = subprocess.run(
        [
            "strace",
            "-f",
            "-qq",
            "-e",
            "trace=execve",
            "-s",
            "4096",
            "-o",
            exec_trace.as_posix(),
            *_candidate_command(bundle, stage, live, digest),
        ],
        cwd=stage,
        env=_frozen_environment(bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert prepared.returncode == 0, prepared.stderr
    docker_compose_execs = [
        line
        for line in exec_trace.read_text(encoding="utf-8").splitlines()
        if '"docker"' in line and '"compose"' in line
    ]
    assert all('"config"' not in line for line in docker_compose_execs)
    assert prepared.stdout.strip() == "0"
    assert (live / "old-source.marker").read_text(encoding="utf-8") == "old source\n"
    assert (live / "docker/.env").read_bytes() == original_env
    assert not preflight_data_root.exists()
    assert _container_ids() == before_containers

    activated = subprocess.run(
        [
            (bundle / "runtime/bin/python").as_posix(),
            (stage / "scripts/remote_deploy_python_toolchain.py").as_posix(),
            "activate-candidate",
            "--bundle",
            bundle.as_posix(),
            "--stage-root",
            stage.as_posix(),
            "--live-root",
            live.as_posix(),
            "--backup-root",
            backup.as_posix(),
        ],
        cwd=stage,
        env=_frozen_environment(bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert activated.returncode == 1
    assert "live source/env 状态需要人工核验" in activated.stderr
    assert stage.is_dir()
    assert (stage / "candidate-source.marker").read_text(encoding="utf-8") == "candidate source\n"
    assert (live / "old-source.marker").read_text(encoding="utf-8") == "old source\n"
    assert (live / "docker/.env").read_bytes() == original_env
    assert (live / ".git/config").is_file()
    assert (live / ".venv/remote-only.marker").is_file()
    assert bundle.is_dir()
    assert not backup.exists()
    assert _container_ids() == before_containers


def test_fresh_private_env_preflight_failure_leaves_live_source_and_containers_unchanged(
    tmp_path: Path,
    remote_python_toolchain_bundle: Path,
) -> None:
    host = tmp_path / "fresh temporary host"
    host.mkdir()
    live = host / "agent gov"
    stage = host / "agent gov.stage.fresh"
    live.mkdir()
    marker = live / "live-source.marker"
    marker.write_bytes(b"existing live bytes\n")
    bundle = _install_cached_toolchain(remote_python_toolchain_bundle, live)
    driver = tmp_path / "rsync-shell"
    _write_rsync_protocol_driver(driver)
    transfer_source = _deployable_transfer_source(tmp_path)
    _rsync_source_stage(transfer_source, stage, driver)
    digest = source_artifact_sha256(stage)
    before_containers = _container_ids()

    result = subprocess.run(
        _candidate_command(bundle, stage, live, digest),
        cwd=stage,
        env=_frozen_environment(bundle, tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "未触碰 live env 或 Compose 状态" in result.stderr
    assert marker.read_bytes() == b"existing live bytes\n"
    assert not (live / "docker/.env").exists()
    assert stage.is_dir()
    assert _container_ids() == before_containers


def test_remote_python_toolchain_is_proven_before_source_env_or_compose_mutation() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    runtime = (REPO_ROOT / "scripts/remote_deploy_runtime.py").read_text(encoding="utf-8")
    fresh_verify = "# Fresh host 也必须在源码、私有 env 或 Compose 状态变化前，真实执行本轮冻结解释器。"
    sync = 'log "Uploading ${DEPLOY_REF} tracked code into an isolated sibling stage"'
    candidate = '"$stage/scripts/remote_deploy_python_toolchain.py" prepare-candidate'
    activate = '"$stage/scripts/remote_deploy_python_toolchain.py" activate-candidate'
    persisted = '"$toolchain/recovery/scripts/remote_deploy_runtime.py" prepare'
    load = "_load_archive(binding, state.project_archive)"
    compose = '_compose_operation(state, "up")'

    assert 'remote_deploy_python_toolchain.py" bundle' in text
    assert "Reusing the content-addressed remote Python toolchain" in text
    assert text.index(fresh_verify) < text.index(sync) < text.index(candidate)
    assert text.index(candidate) < text.index(persisted) < text.index(activate)
    execute = runtime.index("def execute_transaction")
    assert runtime.index("_validate_archives(", execute) < runtime.index(load) < runtime.index(compose, execute)
    assert "--protect-args" in text
    assert ".venv/bin/python scripts/run_selected_env_operation.py" not in text
    assert "--operation images-prepare" not in text
    assert "--operation build" not in text
    compose_adapter = runtime[runtime.index("def _compose_operation") : runtime.index("def _load_archive")]
    assert '"config"' not in compose_adapter
    assert "compose config" not in text
    assert '"$toolchain/recovery/scripts/remote_deploy_runtime.py" execute' in text
    assert '"$toolchain/recovery/scripts/remote_deploy_runtime.py" commit' in text
