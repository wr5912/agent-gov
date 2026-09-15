from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import run_selected_env_operation as runner
from scripts import selected_env_persistent_source as persistent_source
from scripts import selected_env_source_snapshot as source_snapshot

_CANONICAL = "a" * 64
_EXECUTION = "b" * 64
_CONTAINER_ID = "c" * 64
_HELPER_IMAGE = "postgres@sha256:" + "d" * 64
_LOCKED_ENV = {persistent_source._LOCK_NONCE_ENV: "e" * 64}


def _readonly_source(root: Path) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    payload = source / "payload"
    payload.write_text("safe\n", encoding="utf-8")
    payload.chmod(0o444)
    source.chmod(0o555)
    return source


def _prepare_fake_cas(cas_root: Path, digest: str, source: Path) -> Path:
    cas_root.parent.mkdir(mode=0o755, parents=True)
    cas_root.mkdir(mode=0o755)
    target = cas_root / digest / "source"
    target.parent.mkdir(mode=0o755)
    shutil.copytree(source, target, copy_function=shutil.copy2)
    for path in sorted((target, *target.rglob("*")), reverse=True):
        path.chmod(0o555 if path.is_dir() else path.stat().st_mode & 0o555)
    target.parent.chmod(0o555)
    cas_root.chmod(0o555)
    return target


def test_materialize_uses_fixed_root_owned_no_pull_helper_boundary(tmp_path: Path, monkeypatch) -> None:
    source = _readonly_source(tmp_path / "operation")
    cas_root = tmp_path / "var/lib/agentgov/deployment-sources-v1"
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    commands: list[list[str]] = []
    daemon_checks: list[str] = []

    def run(command: list[str], _env: dict[str, str]) -> int:
        commands.append(command)
        _prepare_fake_cas(cas_root, _CANONICAL, source)
        return 0

    monkeypatch.setattr(persistent_source, "_ROOT_OWNER_UID", os.geteuid())
    target = persistent_source.materialize(
        source,
        _CANONICAL,
        _EXECUTION,
        dict(_LOCKED_ENV),
        helper_image=_HELPER_IMAGE,
        run_command=run,
        hash_source=lambda path: _EXECUTION if (path / "payload").read_text() == "safe\n" else "f" * 64,
        verify_daemon=lambda: daemon_checks.append("verified"),
    )

    assert target == cas_root / _CANONICAL / "source"
    assert daemon_checks == ["verified", "verified"]
    command = commands[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--user") + 1] == "0:0"
    assert f"type=bind,source={source},target=/agentgov-input,readonly" in command
    assert f"{cas_root}:/agentgov-cas" in command
    assert any("chown -R 0:0" in item for item in command)


def test_real_freeze_then_materialize_preserves_normalized_tree_digest(tmp_path: Path, monkeypatch) -> None:
    operation_root = tmp_path / "operation"
    operation_root.mkdir(mode=0o700)
    selected = operation_root / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    cas_root = tmp_path / "var/lib/agentgov/deployment-sources-v1"
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    monkeypatch.setattr(persistent_source, "_ROOT_OWNER_UID", os.geteuid())

    def freeze(_repo_root: Path, destination: Path) -> str:
        destination.mkdir(mode=0o700)
        nested = destination / "nested"
        nested.mkdir(mode=0o700)
        (nested / "payload").write_text("safe\n", encoding="utf-8")
        return _CANONICAL

    def mode_sensitive_hash(root: Path) -> str:
        return _CANONICAL if root.stat().st_mode & 0o777 == 0o700 else _EXECUTION

    frozen = source_snapshot.freeze_operation_source(
        tmp_path / "repo",
        operation_root,
        selected,
        persistent_binds=True,
        freeze_source=freeze,
        hash_source=mode_sensitive_hash,
    )

    def materialize(_command: list[str], _env: dict[str, str]) -> int:
        _prepare_fake_cas(cas_root, _CANONICAL, frozen.root)
        return 0

    target = persistent_source.materialize(
        frozen.root,
        frozen.digest,
        frozen.execution_digest or "",
        dict(_LOCKED_ENV),
        helper_image=_HELPER_IMAGE,
        run_command=materialize,
        hash_source=mode_sensitive_hash,
        verify_daemon=lambda: None,
    )

    assert frozen.root.stat().st_mode & 0o777 == 0o555
    assert target.stat().st_mode & 0o777 == 0o555
    assert (target / "nested/payload").read_text(encoding="utf-8") == "safe\n"


def test_materialized_tree_survives_operation_snapshot_removal(tmp_path: Path, monkeypatch) -> None:
    operation = tmp_path / "operation"
    source = _readonly_source(operation)
    cas_root = tmp_path / "var/lib/agentgov/deployment-sources-v1"
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    monkeypatch.setattr(persistent_source, "_ROOT_OWNER_UID", os.geteuid())

    def run(_command: list[str], _env: dict[str, str]) -> int:
        _prepare_fake_cas(cas_root, _CANONICAL, source)
        return 0

    target = persistent_source.materialize(
        source,
        _CANONICAL,
        _EXECUTION,
        dict(_LOCKED_ENV),
        helper_image=_HELPER_IMAGE,
        run_command=run,
        hash_source=lambda _path: _EXECUTION,
        verify_daemon=lambda: None,
    )
    source.chmod(0o755)
    shutil.rmtree(operation)

    assert (target / "payload").read_text(encoding="utf-8") == "safe\n"
    assert target.as_posix().startswith(cas_root.as_posix())


def test_materialize_rejects_operator_owned_persistent_tree(tmp_path: Path, monkeypatch) -> None:
    source = _readonly_source(tmp_path / "operation")
    cas_root = tmp_path / "var/lib/agentgov/deployment-sources-v1"
    _prepare_fake_cas(cas_root, _CANONICAL, source)
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    non_operator_uid = os.geteuid() + 1
    monkeypatch.setattr(persistent_source, "_ROOT_OWNER_UID", non_operator_uid)

    with pytest.raises(persistent_source.SelectedEnvError, match="root owner|root-owned"):
        persistent_source.materialize(
            source,
            _CANONICAL,
            _EXECUTION,
            dict(_LOCKED_ENV),
            helper_image=_HELPER_IMAGE,
            run_command=lambda *_args: (_ for _ in ()).throw(AssertionError("must not mutate")),
            hash_source=lambda _path: _EXECUTION,
            verify_daemon=lambda: None,
        )


def test_cleanup_refuses_cas_still_mounted_by_any_container(tmp_path: Path, monkeypatch) -> None:
    source = _readonly_source(tmp_path / "operation")
    cas_root = tmp_path / "var/lib/agentgov/deployment-sources-v1"
    target = _prepare_fake_cas(cas_root, _CANONICAL, source)
    monkeypatch.setattr(persistent_source, "_PERSISTENT_CAS_ROOT", cas_root)
    monkeypatch.setattr(persistent_source, "_ROOT_OWNER_UID", os.geteuid())
    mutations: list[list[str]] = []

    def output(command: list[str], _env: dict[str, str]) -> str:
        if "ls" in command:
            return _CONTAINER_ID
        if "inspect" in command:
            return json.dumps([{"Mounts": [{"Source": (target / "docker/runtime-bootstrap").as_posix()}]}])
        raise AssertionError(command)

    with pytest.raises(persistent_source.SelectedEnvError, match="仍被容器引用"):
        persistent_source.cleanup_obsolete(
            dict(_LOCKED_ENV),
            keep_digest=None,
            helper_image=_HELPER_IMAGE,
            run_command=lambda command, _env: mutations.append(command) or 0,
            run_output=output,
            verify_daemon=lambda: None,
        )
    assert mutations == []


def test_persistent_bind_root_is_not_operator_runtime_volume() -> None:
    target = persistent_source.bind_source_root(_CANONICAL)

    assert target == Path("/var/lib/agentgov/deployment-sources-v1") / _CANONICAL / "source"
    assert "volume-agent-gov" not in target.parts


def test_up_materializes_after_daemon_and_image_boundaries(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    identity = {"id": "daemon"}
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: events.append("capture") or identity)
    monkeypatch.setattr(
        runner,
        "_verify_local_daemon",
        lambda *_args: events.append("daemon-verified"),
    )
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **_kwargs: events.append("images-verified") or {"agent-gov-api": "sha256:" + "e" * 64},
    )

    def materialize(*_args, **kwargs) -> Path:
        events.append("materialize-start")
        kwargs["verify_daemon"]()
        events.append("materialized")
        return Path("/var/lib/agentgov/deployment-sources-v1") / _CANONICAL / "source"

    monkeypatch.setattr(runner.persistent_source, "materialize", materialize)
    boundary = runner._prepare_daemon_boundary(
        "up",
        tmp_path / "selected.env",
        tmp_path,
        {
            runner.source_snapshot.SOURCE_DIGEST_ENV: _EXECUTION,
            persistent_source._LOCK_NONCE_ENV: "e" * 64,
        },
        "4.0.0",
        _CANONICAL,
    )

    assert boundary == (identity, runner.HOST_FILESYSTEM_PROBE_IMAGE, {"agent-gov-api": "sha256:" + "e" * 64})
    assert events == [
        "capture",
        "daemon-verified",
        "images-verified",
        "materialize-start",
        "daemon-verified",
        "materialized",
    ]


@pytest.mark.parametrize(("operation", "keep"), [("up", _CANONICAL), ("down", None)])
def test_start_and_stop_cleanup_obsolete_persistent_sources(
    tmp_path: Path,
    monkeypatch,
    operation: str,
    keep: str | None,
) -> None:
    cleanup: list[str | None] = []
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **_kwargs: {"agent-gov-api": "sha256:" + "e" * 64},
    )
    monkeypatch.setattr(
        runner.persistent_source,
        "cleanup_obsolete",
        lambda *_args, **kwargs: cleanup.append(kwargs["keep_digest"]),
    )

    runner._verify_daemon_after_operation(
        operation,
        tmp_path / "selected.env",
        tmp_path,
        {},
        "4.0.0",
        _CANONICAL,
        {"id": "daemon"},
        runner.HOST_FILESYSTEM_PROBE_IMAGE,
        None,
    )

    assert cleanup == [keep]


def test_daemon_global_lock_serializes_competing_mutations() -> None:
    held = False
    events: list[str] = []

    class Daemon:
        def capture(self, _env):
            events.append("capture")
            return {"id": "daemon"}

        def verify(self, _env, _expected) -> None:
            events.append("verify")

    def run(command: list[str], _env: dict[str, str]) -> int:
        nonlocal held
        if command[1:3] == ["network", "create"]:
            if held:
                return 1
            held = True
            return 0
        if command[1:3] == ["network", "rm"]:
            held = False
            return 0
        raise AssertionError(command)

    def output(command: list[str], env: dict[str, str]) -> str:
        if command[1:3] == ["network", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": _CONTAINER_ID,
                        "Name": persistent_source._LOCK_NAME,
                        "Internal": True,
                        "Labels": {
                            persistent_source._LOCK_LABEL: "v1",
                            persistent_source._LOCK_NONCE_LABEL: env[persistent_source._LOCK_NONCE_ENV],
                        },
                    }
                ]
            )
        if command[1:3] == ["network", "ls"]:
            return "" if not held else _CONTAINER_ID
        raise AssertionError(command)

    first_env: dict[str, str] = {}
    second_env: dict[str, str] = {}
    with persistent_source.daemon_mutation_lock(
        True,
        first_env,
        Daemon,
        run_command=run,
        run_output=output,
    ):
        assert held
        with pytest.raises(persistent_source.SelectedEnvError, match="正在执行"):
            with persistent_source.daemon_mutation_lock(
                True,
                second_env,
                Daemon,
                run_command=run,
                run_output=output,
            ):
                raise AssertionError("competing operation must not enter")
    assert not held
    assert persistent_source._LOCK_NONCE_ENV not in first_env
    assert persistent_source._LOCK_NONCE_ENV not in second_env


def test_frozen_mutation_acquires_host_cutover_lock_before_daemon_lock(tmp_path: Path, monkeypatch) -> None:
    payload = b"AGENTGOV_API_MODE=open\n"
    original = tmp_path / "selected.env"
    snapshot = tmp_path / "snapshot.env"
    original.write_bytes(payload)
    snapshot.write_bytes(payload)
    version = (runner.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    state = SimpleNamespace(
        original_env=original,
        original_identity=(0,) * 9,
        original_digest=hashlib.sha256(payload).hexdigest(),
        live_repo_root=runner.REPO_ROOT,
    )
    events: list[str] = []
    host_locked = False

    def host_lock(_error_type, execute):
        nonlocal host_locked
        events.append("host-enter")
        host_locked = True
        try:
            return execute()
        finally:
            host_locked = False
            events.append("host-exit")

    @contextmanager
    def daemon_lock(_operation: str, _child_env: dict[str, str]):
        assert host_locked
        events.append("daemon-enter")
        try:
            yield {"id": "daemon"}
        finally:
            events.append("daemon-exit")

    monkeypatch.setenv("APP_VERSION", version)
    monkeypatch.setenv("AGENTGOV_SOURCE_ARTIFACT_SHA256", _CANONICAL)
    monkeypatch.setattr(runner.selected_env_reexec, "load_frozen_stage", lambda _env: state)
    monkeypatch.setattr(runner, "_verified_command_root", lambda _env: runner.REPO_ROOT)
    monkeypatch.setattr(runner.selected_env_reexec, "verify_running_from_frozen_source", lambda *_args: None)
    monkeypatch.setattr(runner.source_snapshot, "verify_operation_input", lambda _env: snapshot)
    monkeypatch.setattr(runner.selected_env_reexec, "resolve_source_base", lambda *_args: tmp_path)
    monkeypatch.setattr(runner, "run_with_global_cutover_lock", host_lock)
    monkeypatch.setattr(runner, "_daemon_mutation_lock", daemon_lock)
    monkeypatch.setattr(runner, "_prepare_daemon_boundary", lambda *_args: (None, None, None))
    monkeypatch.setattr(runner, "_execute_operation", lambda *_args, **_kwargs: events.append("execute") or 0)
    monkeypatch.setattr(runner, "_verify_daemon_after_operation", lambda *_args: None)
    monkeypatch.setattr(runner.selected_env_reexec, "verify_stage_postconditions", lambda *_args: None)

    assert runner._run_frozen_stage(snapshot, "down") == 0
    assert events == ["host-enter", "daemon-enter", "execute", "daemon-exit", "host-exit"]


def test_cas_mutation_without_daemon_global_lock_fails_before_docker(tmp_path: Path) -> None:
    source = _readonly_source(tmp_path / "operation")
    calls: list[list[str]] = []

    with pytest.raises(persistent_source.SelectedEnvError, match="daemon-global lock"):
        persistent_source.materialize(
            source,
            _CANONICAL,
            _EXECUTION,
            {},
            helper_image=_HELPER_IMAGE,
            run_command=lambda command, _env: calls.append(command) or 0,
            hash_source=lambda _path: _EXECUTION,
            verify_daemon=lambda: None,
        )
    assert calls == []
