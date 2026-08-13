from __future__ import annotations

import array
import hashlib
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.container_acceptance_candidate_authority as candidate_authority_module
import scripts.container_acceptance_candidate_storage as candidate_storage
import scripts.container_acceptance_contract as acceptance_contract
import scripts.container_acceptance_lock as acceptance_lock
import scripts.container_acceptance_make_gate as make_gate
import scripts.container_acceptance_receipt as acceptance_receipt
import scripts.container_acceptance_tool_authority as tool_authority
import scripts.container_acceptance_toolchain as toolchain

from runtime_container_acceptance_test_support import candidate_authority, candidate_reservation
from test_container_acceptance_toolchain_authority import _daemon, _dependency


def _make_record() -> dict[str, object]:
    identity = Path("/usr/bin/make").stat()
    return {
        "command": "make",
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "uid": identity.st_uid,
        "gid": identity.st_gid,
        "size": identity.st_size,
    }


def _managed_environment(candidate: object, authority: toolchain.CapturedToolchainAuthority) -> dict[str, str]:
    snapshot = candidate.snapshot
    runtime = snapshot.runtime_root
    python_record = next(record for record in authority.payload["tools"] if record["command"] == "python")
    values = {
        "AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE": snapshot.git_tree_sha,
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES": str(snapshot.frontend_dependencies.regular_bytes),
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES": str(snapshot.frontend_dependencies.entries),
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256": snapshot.frontend_dependencies.sha256,
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT": str(snapshot.frontend_dependencies.root),
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES": str(snapshot.pnpm_dependencies.regular_bytes),
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES": str(snapshot.pnpm_dependencies.entries),
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256": snapshot.pnpm_dependencies.sha256,
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT": str(snapshot.pnpm_dependencies.root),
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES": str(snapshot.python_dependencies.regular_bytes),
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES": str(snapshot.python_dependencies.entries),
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256": snapshot.python_dependencies.sha256,
        "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES": str(snapshot.python_dependencies.root),
        toolchain.PYTHON_EXECUTABLE_ENV: str(python_record["invocation_path"]),
        toolchain.PYTHON_TOOLCHAIN_SHA256_ENV: hashlib.sha256(
            (snapshot.repository_root / "scripts/container_acceptance_toolchain.py").read_bytes()
        ).hexdigest(),
        "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT": str(runtime),
        "AGENT_GOV_ACCEPTANCE_RUN_ID": snapshot.run_id,
        "AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256": snapshot.selected_env_sha256,
        "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_SHA256": authority.sha256,
        "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
        "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE": snapshot.profile,
        "BUILDX_CONFIG": str(runtime / "buildx"),
        "HOME": str(runtime / "home"),
        "LC_ALL": "C.UTF-8",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "TMPDIR": str(runtime / "tmp"),
        "VERIFY_SCREENSHOT_DIR": str(runtime / "screenshots"),
        "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
    }
    values.update(
        toolchain.managed_tool_environment(
            frontend_dependency_root=snapshot.frontend_dependencies.root,
            pnpm_dependency_root=snapshot.pnpm_dependencies.root,
        )
    )
    values[acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV] = acceptance_contract.managed_environment_sha256(values)
    return values


@contextmanager
def _prepared_gate_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lock: object,
    *,
    target: str = "_smoke",
    makefile_writer: Callable[[Path], None] | None = None,
) -> Iterator[tuple[object, object, dict[str, str]]]:
    authority = toolchain.capture_toolchain_authority(dependency_capturer=_dependency, daemon_capturer=_daemon)
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", authority)
    monkeypatch.setattr(acceptance_receipt.acceptance_candidate, "verify_candidate_snapshot", lambda _candidate: None)
    module = SimpleNamespace(candidate_authority=candidate_authority_module)
    private_parent = tool_authority.private_state_paths().candidates
    with tempfile.TemporaryDirectory(prefix=f"make-gate-{tmp_path.name}-", dir=private_parent) as raw_root:
        trusted_anchor = Path(raw_root)
        candidate_parent = trusted_anchor / "candidates"
        candidate_parent.mkdir(mode=0o700)
        reservation = candidate_reservation(module, profile="core", parent=candidate_parent)
        verifier = next(verifier for verifier in acceptance_contract.VERIFIER_REGISTRY["core"] if verifier.invocation_argv[-1] == target)
        reserved = acceptance_receipt.write_reserved_receipt(
            receipt_root=trusted_anchor / "receipts",
            reservation=reservation,
            verifier=verifier,
            lifecycle_lock_sha256=acceptance_lock.lifecycle_descriptor_sha256(lock.descriptor),
            trusted_anchor=trusted_anchor,
        )
        candidate = candidate_authority(module, profile="core", reservation=reservation, reserved_receipt_sha256=reserved.reserved_sha256)
        repository = candidate.snapshot_repository_root
        repository.mkdir(parents=True)
        scripts = repository / "scripts"
        scripts.mkdir()
        for source in (toolchain.REPO_ROOT / "scripts").glob("*.py"):
            shutil.copyfile(source, scripts / source.name)
        for source in (toolchain.REPO_ROOT / "app").rglob("*.py"):
            destination = repository / source.relative_to(toolchain.REPO_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        candidate.snapshot.frontend_dependencies.root.mkdir(parents=True)
        candidate.snapshot.python_dependencies.root.mkdir(parents=True)
        candidate.snapshot.pnpm_dependencies.root.mkdir(parents=True)
        makefile = (toolchain.REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
        names = "CONTAINER_ACCEPTANCE_REPO_ROOT CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON CONTAINER_ACCEPTANCE_BOOTSTRAP CONTAINER_ACCEPTANCE_TOOLCHAIN CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER REQUIRE_CONTAINER_ACCEPTANCE PYTHON_RUN".split()  # noqa: SIM905
        assignments = [next(line for line in makefile if line.startswith(f"override {name} ")) for name in names]
        if makefile_writer is None:
            (repository / "Makefile").write_text(
                "\n".join(
                    (
                        "override SHELL := /bin/sh",
                        "override .SHELLFLAGS := -c",
                        *assignments,
                        f"{target}:",
                        "\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
                        "",
                    )
                ),
                encoding="utf-8",
            )
        else:
            makefile_writer(repository)
        for source in scripts.iterdir():
            source.chmod(0o400)
        scripts.chmod(0o500)
        repository.chmod(0o500)
        identity = candidate_storage.PathIdentity.from_stat(repository.stat(follow_symlinks=False))
        candidate = replace(candidate, snapshot=replace(candidate.snapshot, repository_identity=identity))
        managed = _managed_environment(candidate, authority)
        prepared = acceptance_receipt.transition_reserved_to_prepared(
            reserved,
            candidate=candidate,
            managed_environment_sha256=managed[acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV],
            cleanup_on_failure=lambda _identity: None,
        )
        yield verifier, prepared, managed


def _gate_environment(client: socket.socket, nonce: str) -> tuple[dict[str, str], int]:
    descriptor = os.dup(client.fileno())
    return (
        {
            make_gate.MAKE_GATE_FD_ENV: str(descriptor),
            make_gate.MAKE_GATE_NONCE_ENV: nonce,
            "PWD": str(toolchain.REPO_ROOT),
        },
        descriptor,
    )


def _request(nonce: str, target: str = "_smoke") -> bytes:
    return make_gate._canonical_json({"contract": make_gate._CONTRACT, "nonce": nonce, "pid": os.getpid(), "target": target})


def _receive_lifecycle_descriptor(client: socket.socket) -> int:
    _response, ancillary, flags, _address = client.recvmsg(
        make_gate._MAX_MESSAGE_BYTES + 1,
        socket.CMSG_SPACE(array.array("i").itemsize),
    )
    assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
    return make_gate._received_descriptor(ancillary)


def test_gate_allows_exact_make_process_without_full_authority_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, str] = {}
    monkeypatch.setattr(make_gate, "_validate_authority", lambda _authority: None)
    build_response = make_gate._response

    def measured_response(request: make_gate.MakeGateRequest, authority: make_gate._GateAuthority) -> bytes:
        encoded = build_response(request, authority)
        observed["_response_bytes"] = str(len(encoded))
        return encoded

    monkeypatch.setattr(make_gate, "_response", measured_response)

    with (
        acceptance_lock.lifecycle_lock({}) as lock,
        _prepared_gate_authority(monkeypatch, tmp_path, lock) as (
            verifier,
            receipt,
            managed,
        ),
    ):
        repository = receipt.identity.candidate_snapshot.snapshot.repository_root
        command = list(acceptance_contract.verifier_execution_argv("core", verifier, repository))

        def process_runner(argv: list[str], environment: dict[str, str], pass_fds: tuple[int, ...]) -> int:
            observed.update(environment)
            master, terminal = os.openpty()
            observed["_terminal"] = os.ttyname(terminal)
            try:
                completed = subprocess.run(
                    argv,
                    check=False,
                    cwd=repository,
                    env=environment,
                    pass_fds=pass_fds,
                    start_new_session=True,
                    stdout=terminal,
                    stderr=terminal,
                    timeout=15,
                )
            finally:
                os.close(terminal)
            try:
                encoded = os.read(master, make_gate._MAX_MESSAGE_BYTES)
            except OSError:
                encoded = b""
            finally:
                os.close(master)
            observed["_stderr"] = encoded.decode(errors="replace")
            return completed.returncode

        result = make_gate.run_make_verifier(
            command,
            managed,
            profile="core",
            verifier=verifier,
            candidate=SimpleNamespace(snapshot_repository_root=repository),
            receipt=receipt,
            lock=lock,
            process_runner=process_runner,
        )

    assert result == 0, observed.get("_stderr", "")
    assert observed["PWD"] == str(receipt.identity.candidate_snapshot.snapshot.repository_root) and observed["_terminal"].startswith("/dev/pts/")
    assert not any(
        key.startswith(("AGENT_GOV_ACCEPTANCE_LOCK_", "AGENT_GOV_PREPARED_")) or key == "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE" for key in observed
    )
    assert int(observed["_response_bytes"]) <= make_gate._MAX_MESSAGE_BYTES
    assert observed["_stderr"] == ""


def test_production_python_wrapper_rejects_wrong_snapshot_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wrong_cwd = tmp_path / "wrong-cwd"
    wrong_cwd.mkdir()
    monkeypatch.setattr(make_gate, "_validate_authority", lambda _authority: None)
    with (
        acceptance_lock.lifecycle_lock({}) as lock,
        _prepared_gate_authority(monkeypatch, tmp_path, lock) as (
            verifier,
            receipt,
            managed,
        ),
    ):
        repository = receipt.identity.candidate_snapshot.snapshot.repository_root
        makefile = repository / "Makefile"
        makefile.write_text(makefile.read_text().replace("\t@$(REQUIRE_CONTAINER_ACCEPTANCE)", f"\t@cd {wrong_cwd} && $(REQUIRE_CONTAINER_ACCEPTANCE)"))
        command = list(acceptance_contract.verifier_execution_argv("core", verifier, repository))

        def process_runner(argv: list[str], environment: dict[str, str], pass_fds: tuple[int, ...]) -> int:
            return subprocess.run(argv, cwd=repository, env=environment, pass_fds=pass_fds, start_new_session=True, timeout=15).returncode

        with pytest.raises(make_gate.MakeGateError, match="exact verifier permit"):
            make_gate.run_make_verifier(
                command,
                managed,
                profile="core",
                verifier=verifier,
                candidate=SimpleNamespace(snapshot_repository_root=repository),
                receipt=receipt,
                lock=lock,
                process_runner=process_runner,
            )


def test_noncanonical_recipe_helper_cannot_receive_gate_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(make_gate, "_validate_authority", lambda _authority: None)
    with (
        acceptance_lock.lifecycle_lock({}) as lock,
        _prepared_gate_authority(monkeypatch, tmp_path, lock) as (
            verifier,
            receipt,
            managed,
        ),
    ):
        repository = receipt.identity.candidate_snapshot.snapshot.repository_root
        makefile = repository / "Makefile"
        direct = f"\t@{toolchain.REPO_ROOT}/.venv/bin/python {toolchain.REPO_ROOT}/scripts/container_acceptance_make_gate.py check _smoke"
        makefile.write_text(makefile.read_text().replace("\t@$(REQUIRE_CONTAINER_ACCEPTANCE)", direct))
        command = list(acceptance_contract.verifier_execution_argv("core", verifier, repository))

        def process_runner(argv: list[str], environment: dict[str, str], pass_fds: tuple[int, ...]) -> int:
            return subprocess.run(
                argv,
                check=False,
                cwd=repository,
                env=environment,
                pass_fds=pass_fds,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode

        with pytest.raises(make_gate.MakeGateError, match="exact verifier permit"):
            make_gate.run_make_verifier(
                command,
                managed,
                profile="core",
                verifier=verifier,
                candidate=SimpleNamespace(snapshot_repository_root=repository),
                receipt=receipt,
                lock=lock,
                process_runner=process_runner,
            )


def test_gate_helper_rejects_forged_socket_without_lifecycle_descriptor() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    gate_descriptor = os.dup(client.fileno())
    nonce = "1" * 32
    environment = {
        make_gate.MAKE_GATE_FD_ENV: str(gate_descriptor),
        make_gate.MAKE_GATE_NONCE_ENV: nonce,
        "PWD": str(toolchain.REPO_ROOT),
    }

    def fake_server() -> None:
        request = server.recv(4096)
        response = make_gate._canonical_json(
            {
                "contract": make_gate._CONTRACT,
                "cookie": "0" * 16,
                "permit": "once",
                "request_sha256": hashlib.sha256(request).hexdigest(),
            }
        )
        server.sendall(response)

    worker = threading.Thread(target=fake_server)
    worker.start()
    try:
        with pytest.raises(make_gate.MakeGateError, match="handshake"):
            make_gate.check_make_gate("_private", environment)
    finally:
        worker.join(timeout=2)
        client.close()
        server.close()
    with pytest.raises(OSError):
        os.fstat(gate_descriptor)


def test_gate_client_timeout_closes_its_inherited_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    environment, gate_descriptor = _gate_environment(client, "2" * 32)
    release = threading.Event()

    def silent_server() -> None:
        server.recv(make_gate._MAX_MESSAGE_BYTES)
        release.wait(timeout=1)

    monkeypatch.setattr(make_gate, "_CLIENT_TIMEOUT_SECONDS", 0.05)
    worker = threading.Thread(target=silent_server)
    worker.start()
    started = time.monotonic()
    try:
        with pytest.raises(make_gate.MakeGateError, match="handshake") as captured:
            make_gate.check_make_gate("_smoke", environment)
    finally:
        release.set()
        worker.join(timeout=2)
        client.close()
        server.close()
    assert time.monotonic() - started < 1
    assert make_gate._safe_failure_phase(captured.value) == "authority_exchange"
    assert make_gate._safe_failure_code(captured.value) == "MakeGateError.TimeoutError"
    with pytest.raises(OSError):
        os.fstat(gate_descriptor)


def test_gate_timeout_cli_reports_only_bounded_phase_and_exception_types(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "private-path-and-env-value-must-not-appear"
    failure = make_gate.MakeGateError(secret)
    failure.__cause__ = TimeoutError(secret)

    def fail(_target: str, _environ: object) -> None:
        make_gate._mark_failure_phase(failure, "authority_exchange")
        raise failure

    monkeypatch.setattr(make_gate, "check_make_gate", fail)
    monkeypatch.setattr(os.sys, "argv", ["container_acceptance_make_gate.py", "check", "_smoke"])

    assert make_gate.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "failure_phase=authority_exchange failure_code=MakeGateError.TimeoutError\n"
    assert secret not in captured.err


def test_gate_server_authority_cli_reports_only_bounded_phase_and_exception_types(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "private-authority-message-must-not-appear"
    cause = make_gate.MakeGateError(secret)
    failure = make_gate.MakeGateError(secret)
    failure.__cause__ = cause

    def fail(_target: str, _environ: object) -> None:
        make_gate._mark_failure_phase(failure, "authority_exchange")
        raise failure

    monkeypatch.setattr(make_gate, "check_make_gate", fail)
    monkeypatch.setattr(os.sys, "argv", ["container_acceptance_make_gate.py", "check", "_smoke"])

    assert make_gate.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "failure_phase=authority_exchange failure_code=MakeGateError.MakeGateError\n"
    assert secret not in captured.err


def test_gate_client_rejects_truncated_response_and_closes_received_descriptor() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    environment, gate_descriptor = _gate_environment(client, "3" * 32)

    with acceptance_lock.lifecycle_lock({}) as lock:

        def oversized_server() -> None:
            server.recv(make_gate._MAX_MESSAGE_BYTES)
            server.sendmsg(
                [b"x" * (make_gate._MAX_MESSAGE_BYTES + 2)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [lock.descriptor]))],
            )

        worker = threading.Thread(target=oversized_server)
        worker.start()
        try:
            with pytest.raises(make_gate.MakeGateError, match="handshake"):
                make_gate.check_make_gate("_smoke", environment)
        finally:
            worker.join(timeout=2)
            client.close()
            server.close()
    with pytest.raises(OSError):
        os.fstat(gate_descriptor)


def test_gate_client_rejects_truncated_or_extra_ancillary_without_fd_leak() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    baseline = len(tuple(Path("/proc/self/fd").iterdir()))
    environment, _gate_descriptor = _gate_environment(client, "4" * 32)

    with acceptance_lock.lifecycle_lock({}) as lock:

        def ancillary_server() -> None:
            request = server.recv(make_gate._MAX_MESSAGE_BYTES)
            response = make_gate._canonical_json(
                {
                    "contract": make_gate._CONTRACT,
                    "permit": "once",
                    "receipt_authority": "x",
                    "request_sha256": hashlib.sha256(request).hexdigest(),
                    "toolchain_evidence": "x",
                    "toolchain_sha256": "0" * 64,
                }
            )
            server.sendmsg(
                [response],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [lock.descriptor] * 16))],
            )

        worker = threading.Thread(target=ancillary_server)
        worker.start()
        try:
            with pytest.raises(make_gate.MakeGateError, match="handshake"):
                make_gate.check_make_gate("_smoke", environment)
        finally:
            worker.join(timeout=2)
    assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline
    client.close()
    server.close()


def test_received_descriptor_rejects_and_closes_extra_rights() -> None:
    first, first_writer = os.pipe()
    second, second_writer = os.pipe()
    try:
        with pytest.raises(make_gate.MakeGateError, match="descriptor"):
            make_gate._received_descriptor([(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [first, second]).tobytes())])
        for descriptor in (first, second):
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        os.close(first_writer)
        os.close(second_writer)


def test_received_descriptor_closes_delivered_right_when_control_data_is_truncated() -> None:
    descriptor, writer = os.pipe()
    try:
        with pytest.raises(make_gate.MakeGateError, match="descriptor"):
            make_gate._received_descriptor(
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]).tobytes())],
                flags=socket.MSG_CTRUNC,
            )
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        os.close(writer)


def test_gate_server_authority_rejection_wakes_the_make_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "private-authority-message-must-not-appear"
    observed: dict[str, str] = {}
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "_private:\n"
        f"\t@PYTHONPATH={toolchain.REPO_ROOT} {toolchain.REPO_ROOT}/.venv/bin/python "
        f"{toolchain.REPO_ROOT}/scripts/container_acceptance_make_gate.py check _private\n",
        encoding="utf-8",
    )
    command = ["/usr/bin/make", "--no-print-directory", "-f", str(makefile), "_private"]
    validations = 0

    def validate(_authority: object) -> None:
        nonlocal validations
        validations += 1
        if validations > 1:
            raise make_gate.MakeGateError(secret)

    monkeypatch.setattr(make_gate, "_validate_authority", validate)
    monkeypatch.setattr(make_gate, "_verify_helper_process", lambda _pid, _target, _authority: None)
    monkeypatch.setattr(
        toolchain,
        "active_toolchain_authority",
        lambda: SimpleNamespace(payload={"tools": [_make_record()]}),
    )

    def process_runner(argv: list[str], environment: dict[str, str], pass_fds: tuple[int, ...]) -> int:
        completed = subprocess.run(
            argv,
            check=False,
            env={**environment, "LC_ALL": "C.UTF-8"},
            pass_fds=pass_fds,
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        observed["stderr"] = completed.stderr
        return completed.returncode

    started = time.monotonic()
    with acceptance_lock.lifecycle_lock({}) as lock, pytest.raises(make_gate.MakeGateError, match="exact verifier permit"):
        make_gate.run_make_verifier(
            command,
            {},
            profile="core",
            verifier=SimpleNamespace(invocation_argv=("make", "_private")),
            candidate=SimpleNamespace(snapshot_repository_root=tmp_path),
            receipt=SimpleNamespace(),
            lock=lock,
            process_runner=process_runner,
        )
    assert time.monotonic() - started < 2
    diagnostic, *_make_diagnostics = observed["stderr"].splitlines()
    assert diagnostic == "failure_phase=authority_exchange failure_code=MakeGateError.MakeGateError"
    assert secret not in observed["stderr"]


def test_gate_server_rejects_request_ancillary_before_issuing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(make_gate, "_verify_helper_process", lambda _pid, _target, _authority: None)
    monkeypatch.setattr(make_gate, "_make_process", lambda _pid, _command: _pid)
    monkeypatch.setattr(make_gate, "_validate_authority", lambda _authority: None)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    stop = threading.Event()
    errors: list[BaseException] = []
    used: set[str] = set()
    nonce = "5" * 32
    extra_descriptor = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    with acceptance_lock.lifecycle_lock({}) as lock:
        authority = make_gate._GateAuthority(
            ("/usr/bin/make", "_smoke"),
            "_smoke",
            frozenset({"_smoke"}),
            {},
            "core",
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            lock,
        )
        worker = threading.Thread(
            target=make_gate._server_thread,
            args=(server, authority, nonce, stop, used, errors),
        )
        worker.start()
        try:
            baseline = len(tuple(Path("/proc/self/fd").iterdir()))
            client.sendmsg(
                [_request(nonce)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [extra_descriptor]))],
            )
            worker.join(timeout=2)
            assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline
        finally:
            stop.set()
            client.close()
            server.close()
            worker.join(timeout=2)
            os.close(extra_descriptor)
    assert not worker.is_alive()
    assert used == set()
    assert len(errors) == 1 and isinstance(errors[0], make_gate.MakeGateError)


def test_gate_server_issues_exact_target_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(make_gate, "_verify_helper_process", lambda _pid, _target, _authority: None)
    monkeypatch.setattr(make_gate, "_make_process", lambda _pid, _command: _pid)
    monkeypatch.setattr(make_gate, "_validate_authority", lambda _authority: None)
    monkeypatch.setattr(make_gate, "_response", lambda _request_value, _authority: b"{}")
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    stop = threading.Event()
    errors: list[BaseException] = []
    used: set[str] = set()
    nonce = "6" * 32
    with acceptance_lock.lifecycle_lock({}) as lock:
        authority = make_gate._GateAuthority(
            ("/usr/bin/make", "_smoke"),
            "_smoke",
            frozenset({"_smoke"}),
            {},
            "core",
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            lock,
        )
        worker = threading.Thread(
            target=make_gate._server_thread,
            args=(server, authority, nonce, stop, used, errors),
        )
        worker.start()
        try:
            client.sendall(_request(nonce))
            received = _receive_lifecycle_descriptor(client)
            os.close(received)
            client.sendall(_request(nonce))
            worker.join(timeout=2)
        finally:
            stop.set()
            client.close()
            server.close()
            worker.join(timeout=2)
    assert used == {"_smoke"}
    assert len(errors) == 1 and isinstance(errors[0], make_gate.MakeGateError)


def test_gate_server_poll_timeout_stops_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(make_gate, "_SERVER_TIMEOUT_SECONDS", 0.05)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    stop = threading.Event()
    errors: list[BaseException] = []
    used: set[str] = set()
    worker = threading.Thread(
        target=make_gate._server_thread,
        args=(server, SimpleNamespace(), "7" * 32, stop, used, errors),
    )
    worker.start()
    time.sleep(0.02)
    started = time.monotonic()
    stop.set()
    worker.join(timeout=0.5)
    client.close()
    server.close()
    assert not worker.is_alive()
    assert errors == []
    assert time.monotonic() - started < 0.5


def test_gate_sender_must_remain_in_exact_make_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_pid = os.getpid()
    observed = {
        100: (200, 999, 200),
        200: (runner_pid, 200, 200),
    }
    monkeypatch.setattr(make_gate, "_proc_stat", lambda pid: observed[pid])
    monkeypatch.setattr(make_gate, "_verify_make_process", lambda _pid, _command: None)

    with pytest.raises(make_gate.MakeGateError, match="escaped"):
        make_gate._make_process(100, ("/usr/bin/make", "_smoke"))


def test_gate_helper_missing_overlay_is_an_explicit_gate_failure() -> None:
    with pytest.raises(make_gate.MakeGateError, match="descriptor"):
        make_gate.check_make_gate("_private", {})


def test_post_wrapper_make_environment_digest_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with (
        acceptance_lock.lifecycle_lock({}) as lock,
        _prepared_gate_authority(monkeypatch, tmp_path, lock) as (
            _verifier,
            receipt,
            managed,
        ),
    ):
        environment = {
            **managed,
            "MAKEFLAGS": "--no-print-directory",
            "MAKELEVEL": "1",
            "MFLAGS": "",
            "PWD": str(receipt.identity.candidate_snapshot.snapshot.repository_root),
            make_gate.MAKE_GATE_FD_ENV: "9",
            make_gate.MAKE_GATE_NONCE_ENV: "a" * 32,
        }
        assert (
            acceptance_contract.managed_environment_sha256(make_gate._managed_gate_environment(environment))
            == managed[acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV]
        )
        environment["UNMANAGED_ACCEPTANCE_VALUE"] = "unexpected"
        with pytest.raises(acceptance_contract.AcceptanceContractError, match="managed acceptance environment"):
            acceptance_contract.managed_environment_sha256(make_gate._managed_gate_environment(environment))
        environment.update({"MAKE_TERMOUT": "/tmp/not-a-terminal", "MAKE_TERMERR": "/tmp/not-a-terminal"})
        with pytest.raises(make_gate.MakeGateError, match="runtime environment"):
            make_gate._managed_gate_environment(environment)
        environment.pop("MAKE_TERMOUT")
        environment.pop("MAKE_TERMERR")
        environment["MAKEFLAGS"] = "-j2"
        with pytest.raises(make_gate.MakeGateError, match="runtime environment"):
            make_gate._managed_gate_environment(environment)
        with pytest.raises(make_gate.MakeGateError, match="spawn environment"):
            make_gate.managed_environment_from_gate_spawn({"PWD": "/tmp"})


def test_invalid_gate_target_or_spawn_closes_inherited_descriptor() -> None:
    for target, pwd, message in (("not-private", str(toolchain.REPO_ROOT), "target"), ("_smoke", "relative", "spawn")):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        environment, descriptor = _gate_environment(client, "8" * 32)
        environment["PWD"] = pwd
        try:
            with pytest.raises(make_gate.MakeGateError, match=message):
                make_gate.check_make_gate(target, environment)
            with pytest.raises(OSError):
                os.fstat(descriptor)
        finally:
            client.close()
            server.close()
