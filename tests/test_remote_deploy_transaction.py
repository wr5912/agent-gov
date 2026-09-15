from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from scripts import remote_deploy_runtime as runtime
from scripts import remote_deploy_transaction as transaction
from scripts.agentscope_atomic_cutover_bootstrap import freeze_deployable_source, source_artifact_sha256
from scripts.remote_deploy_boundary_acceptance import RemoteShell, _ephemeral_ssh

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts/deploy_agent_gov_to_host"


@dataclass(frozen=True)
class SourceTransactionFixture:
    live: Path
    stage: Path
    backup: Path
    candidate_digest: str
    old_digest: str
    old_marker: bytes


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


@pytest.fixture(scope="module")
def deployable_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("remote-deploy-transaction-template")
    template = root / "source"
    freeze_deployable_source(REPO_ROOT, template)
    return template


@pytest.fixture
def source_transaction(tmp_path: Path, deployable_template: Path) -> SourceTransactionFixture:
    live = tmp_path / "agent gov"
    stage = tmp_path / "agent gov.stage.candidate"
    backup = tmp_path / "agent gov.previous.candidate"
    _copy_tree(deployable_template, live)
    _copy_tree(deployable_template, stage)
    old_marker = b"old-live-source\n"
    (live / "VERSION").write_bytes(b"3.9.9\n")
    (live / "old-source.marker").write_bytes(old_marker)
    (live / ".git").mkdir()
    (live / ".git/config").write_bytes(b"remote-only-git\n")
    (live / "images").mkdir()
    (live / "images/remote-only.marker").write_bytes(b"remote-only-images\n")
    private_env = live / "docker/.env"
    private_env.write_bytes(b"PRIVATE_CONFIG=opaque\n")
    private_env.chmod(0o600)
    (stage / "candidate-source.marker").write_bytes(b"candidate\n")
    (stage / "docker/.env").write_bytes(private_env.read_bytes())
    (stage / "docker/.env").chmod(0o600)
    candidate_digest = source_artifact_sha256(stage)
    old_digest = source_artifact_sha256(live)
    _payload, anchor = transaction.capture_live_env(private_env)
    transaction.write_candidate_state(
        stage,
        transaction.CandidateState(
            live_root=live.as_posix(),
            stage_root=stage.as_posix(),
            source_sha256=candidate_digest,
            candidate_env_sha256=hashlib.sha256((stage / "docker/.env").read_bytes()).hexdigest(),
            live_env=anchor,
        ),
    )
    transaction.begin_transaction(
        transaction_id="deploy-transaction-fixture",
        live_root=live,
        stage_root=stage,
        backup_root=backup,
        toolchain_root=tmp_path / "stable-toolchain",
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
    return SourceTransactionFixture(live, stage, backup, candidate_digest, old_digest, old_marker)


def _kill_after_rename(live: Path, number: int) -> None:
    child = os.fork()
    if child == 0:
        original = transaction._rename
        count = 0

        def crash_after_rename(source: Path, destination: Path) -> None:
            nonlocal count
            original(source, destination)
            count += 1
            if count == number:
                os.kill(os.getpid(), signal.SIGKILL)

        transaction._rename = crash_after_rename
        transaction.activate_transaction(live)
        os._exit(0)
    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL


@pytest.mark.parametrize("rename_number", [1, 2, 3])
def test_activation_resumes_after_sigkill_in_every_rename_window(
    source_transaction: SourceTransactionFixture,
    rename_number: int,
) -> None:
    fixture = source_transaction

    _kill_after_rename(fixture.live, rename_number)
    state = transaction.activate_transaction(fixture.live)

    assert state.phase == "activated"
    assert source_artifact_sha256(fixture.live) == fixture.candidate_digest
    assert fixture.backup.is_dir()
    assert not fixture.stage.exists()
    assert (fixture.live / ".git/config").read_bytes() == b"remote-only-git\n"
    assert (fixture.live / "images/remote-only.marker").read_bytes() == b"remote-only-images\n"
    assert transaction.candidate_state_path(fixture.stage).is_file()


def test_activated_source_can_be_rolled_back_idempotently(source_transaction: SourceTransactionFixture) -> None:
    fixture = source_transaction
    transaction.activate_transaction(fixture.live)
    failed = transaction.mark_recovery_required(fixture.live, "candidate-deploy-failed")
    persisted = transaction.load_transaction(fixture.live)

    state = transaction.rollback_source(fixture.live)
    repeated = transaction.rollback_source(fixture.live)

    assert state.phase == repeated.phase == "source-rolled-back"
    assert failed.recovery_from_phase == persisted.recovery_from_phase == "activated"
    assert source_artifact_sha256(fixture.live) == fixture.old_digest
    assert (fixture.live / "old-source.marker").read_bytes() == fixture.old_marker
    assert not fixture.stage.exists()
    assert not fixture.backup.exists()


def test_prepared_transaction_can_be_recovered_before_the_first_rename(
    source_transaction: SourceTransactionFixture,
) -> None:
    fixture = source_transaction

    state = transaction.rollback_source(fixture.live)

    assert state.phase == "source-rolled-back"
    assert source_artifact_sha256(fixture.live) == fixture.old_digest
    assert not fixture.stage.exists()
    assert not fixture.backup.exists()


@pytest.mark.parametrize("damaged_stage", ["deleted", "corrupt"])
def test_backup_only_rename_window_restores_old_source(
    source_transaction: SourceTransactionFixture,
    damaged_stage: str,
) -> None:
    fixture = source_transaction
    _kill_after_rename(fixture.live, 1)
    if damaged_stage == "deleted":
        shutil.rmtree(fixture.stage)
    else:
        (fixture.stage / "VERSION").write_text("corrupt\n", encoding="utf-8")

    state = transaction.rollback_source(fixture.live)

    assert state.phase == "source-rolled-back"
    assert source_artifact_sha256(fixture.live) == fixture.old_digest
    assert not fixture.stage.exists()
    assert not fixture.backup.exists()


def test_candidate_and_transaction_records_coexist_before_first_rename(source_transaction: SourceTransactionFixture) -> None:
    fixture = source_transaction

    state = transaction.load_transaction(fixture.live)

    assert state.phase == "prepared"
    assert transaction.transaction_path(fixture.live).is_file()
    assert transaction.candidate_state_path(fixture.stage).is_file()
    assert fixture.live.is_dir() and fixture.stage.is_dir()


def test_lifecycle_rejects_illegal_phase_transition(source_transaction: SourceTransactionFixture) -> None:
    state = transaction.load_transaction(source_transaction.live)

    with pytest.raises(transaction.TransactionError, match="非法部署事务转移"):
        transaction.transition(state, "healthy")


def test_remote_flock_covers_stage_archives_activation_compose_and_health() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    lock = "start_remote_lock"
    stage = 'log "Uploading ${DEPLOY_REF} tracked code into an isolated sibling stage"'
    archive = 'upload_image_archive "$PROJECT_ARCHIVE"'
    activate = 'remote_deploy_python_toolchain.py" activate-candidate'
    execute = 'remote_deploy_runtime.py" execute'
    commit = 'remote_deploy_runtime.py" commit'
    release = "release_remote_lock"

    main = source.index('log "Remote path: $REMOTE:$REMOTE_PATH"')
    assert "flock -n 9" in source
    assert 'flock -n -E 74 8' in source
    assert "flock -s 8" in source
    assert 'rsync --rsync-path="$remote_rsync_path"' in source
    assert source.index(lock, main) < source.index(stage) < source.index(archive)
    assert source.index(archive) < source.index(activate) < source.index(execute) < source.index(commit)
    assert source.index(commit) < source.index(release, source.index(commit))
    assert "unfinished remote deployment transaction" in source
    assert "--recover" in source
    assert 'runner="$toolchain/recovery/scripts/remote_deploy_runtime.py"' in source
    assert "remote source activation requires recovery" in source
    assert "remote deployment commit requires recovery" in source
    assert "${REMOTE_STAGE##*.}" in source
    transaction_source = (REPO_ROOT / "scripts/remote_deploy_transaction.py").read_text(encoding="utf-8")
    assert "_fsync_directory(destination_parent)" in transaction_source


def _advance_to_healthy(live: Path) -> None:
    state = transaction.activate_transaction(live)
    for phase in (
        "images-loading",
        "images-loaded",
        "compose-starting",
        "compose-recreated",
        "health-checking",
        "healthy",
    ):
        state = transaction.transition(state, phase)


def _attach_old_archive(live: Path) -> Path:
    state = transaction.load_transaction(live)
    root = live.parent / ".agentgov-deploy-recovery" / state.transaction_id
    root.mkdir(parents=True)
    archive = root / "old-project-images.tar.gz"
    archive.write_bytes(b"real recovery archive bytes\n")
    updated = replace(
        state,
        old_archive=transaction.ArchiveIdentity(archive.as_posix(), hashlib.sha256(archive.read_bytes()).hexdigest()),
    )
    transaction._store_transaction(updated)
    return root


def _sigkill_after_recovery_root_removal(live: Path, recovery_root: Path, function_name: str) -> None:
    child = os.fork()
    if child == 0:
        original = transaction._cleanup_directory

        def crash(path: Path, *, missing_allowed: bool) -> None:
            original(path, missing_allowed=missing_allowed)
            if path == recovery_root:
                os.kill(os.getpid(), signal.SIGKILL)

        transaction._cleanup_directory = crash
        getattr(transaction, function_name)(live)
        os._exit(0)
    _pid, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL


def test_success_finalization_reenters_after_sigkill_post_archive_removal(
    source_transaction: SourceTransactionFixture,
) -> None:
    fixture = source_transaction
    recovery_root = _attach_old_archive(fixture.live)
    _advance_to_healthy(fixture.live)

    _sigkill_after_recovery_root_removal(fixture.live, recovery_root, "finalize_success")

    assert transaction.load_transaction(fixture.live).phase == "finalizing-success"
    assert not recovery_root.exists()
    transaction.finalize_success(fixture.live)
    assert not transaction.transaction_path(fixture.live).exists()


def test_rollback_finalization_reenters_after_sigkill_post_archive_removal(
    source_transaction: SourceTransactionFixture,
) -> None:
    fixture = source_transaction
    recovery_root = _attach_old_archive(fixture.live)
    state = transaction.rollback_source(fixture.live)
    transaction.transition(state, "rolled-back")

    _sigkill_after_recovery_root_removal(fixture.live, recovery_root, "finalize_rollback")

    assert transaction.load_transaction(fixture.live).phase == "finalizing-rollback"
    assert not recovery_root.exists()
    transaction.finalize_rollback(fixture.live)
    assert not transaction.transaction_path(fixture.live).exists()


def test_lost_real_ssh_lock_blocks_mutation_and_cleanup_preserves_stage(tmp_path: Path) -> None:
    ssh_root = tmp_path / "ssh"
    ssh_root.mkdir()
    remote_root = tmp_path / "remote"
    live = remote_root / "agent gov"
    stage = remote_root / "agent gov.stage.lock-loss"
    stage.mkdir(parents=True)
    marker = remote_root / "must-not-exist"
    local_tmp = tmp_path / "local-tmp"
    local_boundary = tmp_path / "local-boundary"
    local_tmp.mkdir()
    local_boundary.mkdir()
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    prefix = source.split('\ncd "$ROOT_DIR"\n', 1)[0]
    cleanup = source[source.index("cleanup_deploy() {") : source.index("trap cleanup_deploy EXIT")]

    with _ephemeral_ssh(ssh_root) as shell:
        harness = tmp_path / "lock-loss.sh"
        ssh_options = " ".join(shlex.quote(part) for part in shell.ssh[1:-1])
        harness.write_text(
            prefix
            + "\n"
            + cleanup
            + f'''\nraw_remote_run() {{ {shlex.join(shell.ssh)} "$@"; }}
SSH_OPTIONS=({ssh_options})
REMOTE={shlex.quote(shell.ssh[-1])}
REMOTE_PATH={shlex.quote(live.as_posix())}
REMOTE_TRANSACTION_PATH={shlex.quote(transaction.transaction_path(live).as_posix())}
REMOTE_STAGE={shlex.quote(stage.as_posix())}
TMP_DIR={shlex.quote(local_tmp.as_posix())}
LOCAL_BOUNDARY_DIR={shlex.quote(local_boundary.as_posix())}
start_remote_lock
trap cleanup_deploy EXIT
remote_transaction_status
[ "$REMOTE_TRANSACTION_STATUS" = TRANSACTION_ABSENT ]
kill "$REMOTE_LOCK_PID"
wait "$REMOTE_LOCK_PID" || true
remote_run "touch {shlex.quote(marker.as_posix())}"
''',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", harness],
            env=dict(shell.environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        for _attempt in range(50):
            probe = subprocess.run(
                shell.command(["flock", "-n", f"{live}.deploy.lock", "true"]),
                env=shell.environment,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("second SSH deployment could not acquire the released real flock")

    assert result.returncode != 0
    assert "lock connection was lost" in result.stderr
    assert not marker.exists()
    assert stage.is_dir()


def _lock_harness(
    shell: RemoteShell,
    *,
    live: Path,
    transaction_path: Path,
    body: str,
) -> str:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    prefix = source.split('\ncd "$ROOT_DIR"\n', 1)[0]
    ssh_options = " ".join(shlex.quote(part) for part in shell.ssh[1:-1])
    return (
        prefix
        + "\n"
        + f'''raw_remote_run() {{ {shlex.join(shell.ssh)} "$@"; }}
SSH_OPTIONS=({ssh_options})
REMOTE={shlex.quote(shell.ssh[-1])}
REMOTE_PATH={shlex.quote(live.as_posix())}
REMOTE_TRANSACTION_PATH={shlex.quote(transaction_path.as_posix())}
REMOTE_LOCK_PROBE_TIMEOUT=2
{body}
'''
    )


def _remote_lock_available(shell: RemoteShell, path: Path) -> bool:
    result = subprocess.run(
        shell.command(["flock", "-n", path.as_posix(), "true"]),
        env=shell.environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _wait_for(condition: Callable[[], bool], message: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(message)


@dataclass(frozen=True)
class LockLossFixture:
    live: Path
    transaction_path: Path
    pid_path: Path
    action_finished: Path
    second_marker: Path
    third_marker: Path
    source_dir: Path
    rsync_target: Path
    payload: bytes


def _make_lock_loss_fixture(tmp_path: Path) -> LockLossFixture:
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    live = remote_root / "agent gov"
    source_dir = tmp_path / "rsync source"
    source_dir.mkdir()
    payload = os.urandom(1024 * 1024)
    (source_dir / "payload.bin").write_bytes(payload)
    return LockLossFixture(
        live=live,
        transaction_path=transaction.transaction_path(live),
        pid_path=tmp_path / "guardian.pid",
        action_finished=remote_root / "first-action-finished",
        second_marker=remote_root / "second-deploy-mutated",
        third_marker=remote_root / "third-deploy-mutated",
        source_dir=source_dir,
        rsync_target=remote_root / "rsync target",
        payload=payload,
    )


def _action_spec(fixture: LockLossFixture, action: str) -> tuple[str, Callable[[], bool]]:
    if action == "remote-run":
        body = f'''remote_run "bash -s -- {shlex.quote(fixture.action_finished.as_posix())}" <<'REMOTE_ACTION'
set -euo pipefail
sleep 6
printf 'finished\\n' > "$1"
REMOTE_ACTION'''
        return body, fixture.action_finished.is_file
    body = f'''REMOTE_RSYNC_TARGET={shlex.quote(fixture.rsync_target.as_posix())}
remote_rsync -az --bwlimit=64 --protect-args -e "$(rsync_ssh_command)" \\
  {shlex.quote(fixture.source_dir.as_posix())}/ "$(rsync_remote_target "$REMOTE_RSYNC_TARGET")"'''
    return body, lambda: (fixture.rsync_target / "payload.bin").is_file()


def _start_locked_action(
    shell: RemoteShell,
    fixture: LockLossFixture,
    action: str,
    action_body: str,
) -> subprocess.Popen[str]:
    harness = fixture.pid_path.parent / f"first-{action}.sh"
    harness.write_text(
        _lock_harness(
            shell,
            live=fixture.live,
            transaction_path=fixture.transaction_path,
            body=f'''trap release_remote_lock EXIT
start_remote_lock
printf '%s\\n' "$REMOTE_LOCK_PID" > {shlex.quote(fixture.pid_path.as_posix())}
{action_body}''',
        ),
        encoding="utf-8",
    )
    first = subprocess.Popen(
        ["bash", harness],
        env=dict(shell.environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    activity_lock = Path(f"{fixture.live}.deploy.lock.activity")
    _wait_for(
        lambda: fixture.pid_path.is_file() and not _remote_lock_available(shell, activity_lock),
        f"{action} did not enter the real locked data path",
    )
    return first


def _kill_guardian_and_reject_contender(
    shell: RemoteShell,
    fixture: LockLossFixture,
    action: str,
    action_completed: Callable[[], bool],
) -> None:
    os.kill(int(fixture.pid_path.read_text(encoding="utf-8").strip()), signal.SIGKILL)
    owner_lock = Path(f"{fixture.live}.deploy.lock")
    _wait_for(
        lambda: _remote_lock_available(shell, owner_lock),
        "guardian SSH loss did not release its owner lock",
    )
    assert not action_completed()
    contender = fixture.pid_path.parent / f"contender-{action}.sh"
    contender.write_text(
        _lock_harness(
            shell,
            live=fixture.live,
            transaction_path=fixture.transaction_path,
            body=f'''start_remote_lock
remote_run "touch {shlex.quote(fixture.second_marker.as_posix())}"
release_remote_lock''',
        ),
        encoding="utf-8",
    )
    second = subprocess.run(
        ["bash", contender],
        env=dict(shell.environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert second.returncode != 0
    assert "another deployment holds the remote transaction lock" in second.stderr
    assert not fixture.second_marker.exists()
    assert not action_completed()


def _verify_completion_and_successor(
    shell: RemoteShell,
    fixture: LockLossFixture,
    action: str,
    action_completed: Callable[[], bool],
    first: subprocess.Popen[str],
) -> None:
    _stdout, first_stderr = first.communicate(timeout=20)
    assert first.returncode != 0
    assert "lock connection was lost" in first_stderr
    assert action_completed()
    if action == "rsync":
        assert (fixture.rsync_target / "payload.bin").read_bytes() == fixture.payload
    successor = fixture.pid_path.parent / f"successor-{action}.sh"
    successor.write_text(
        _lock_harness(
            shell,
            live=fixture.live,
            transaction_path=fixture.transaction_path,
            body=f'''start_remote_lock
remote_run "touch {shlex.quote(fixture.third_marker.as_posix())}"
release_remote_lock''',
        ),
        encoding="utf-8",
    )
    third = subprocess.run(
        ["bash", successor],
        env=dict(shell.environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert third.returncode == 0, third.stderr
    assert fixture.third_marker.is_file()


@pytest.mark.parametrize("action", ["remote-run", "rsync"])
def test_mid_action_guardian_loss_keeps_second_deploy_out_until_action_finishes(
    tmp_path: Path,
    action: str,
) -> None:
    fixture = _make_lock_loss_fixture(tmp_path)
    action_body, action_completed = _action_spec(fixture, action)
    ssh_root = tmp_path / "ssh"
    ssh_root.mkdir()
    with _ephemeral_ssh(ssh_root) as shell:
        first = _start_locked_action(shell, fixture, action, action_body)
        try:
            _kill_guardian_and_reject_contender(shell, fixture, action, action_completed)
            _verify_completion_and_successor(shell, fixture, action, action_completed, first)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()


def _actual_docker_binding(tmp_path: Path, *, docker_host: str = "unix:///var/run/docker.sock") -> runtime.DockerBinding:
    docker = shutil.which("docker")
    assert docker is not None
    return runtime.DockerBinding(
        command=[docker],
        environment={"HOME": tmp_path.as_posix(), "PATH": os.defpath, "DOCKER_HOST": docker_host},
    )


@pytest.mark.parametrize("reference", runtime._project_references("inspect-failure"))
def test_each_project_image_inspect_daemon_failure_is_not_fresh(
    tmp_path: Path,
    reference: str,
) -> None:
    binding = _actual_docker_binding(tmp_path, docker_host=f"unix://{tmp_path}/missing-docker.sock")

    with pytest.raises(runtime.RuntimeDeployError, match="不能判定为 fresh host"):
        runtime._inspect_image(binding, reference)


def test_actual_docker_missing_image_is_the_only_absent_result(tmp_path: Path) -> None:
    binding = _actual_docker_binding(tmp_path)
    reference = f"agentgov-confirmed-missing:{uuid.uuid4().hex}"

    assert runtime._inspect_image(binding, reference) is None


def test_fresh_runtime_proof_rejects_an_actual_running_compose_container(tmp_path: Path) -> None:
    binding = _actual_docker_binding(tmp_path)
    project = f"agentgov-old-runtime-{uuid.uuid4().hex[:12]}"
    live = tmp_path / "live"
    (live / "docker").mkdir(parents=True)
    (live / "docker/.env").write_text(f"COMPOSE_PROJECT_NAME={project}\n", encoding="utf-8")
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    image = f"agent-gov-ui:{version}"
    assert runtime._inspect_image(binding, image) is not None
    name = f"{project}-running"
    started = subprocess.run(
        [*binding["command"], "run", "--detach", "--name", name, "--label", f"com.docker.compose.project={project}", image],
        env=binding["environment"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    try:
        running = subprocess.check_output(
            [*binding["command"], "inspect", name, "--format", "{{.State.Running}}"],
            env=binding["environment"],
            text=True,
        ).strip()
        assert running == "true"
        with pytest.raises(runtime.RuntimeDeployError, match="仍存在既有"):
            runtime._assert_no_prior_runtime(binding, live)
    finally:
        subprocess.run(
            [*binding["command"], "rm", "--force", name],
            env=binding["environment"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
