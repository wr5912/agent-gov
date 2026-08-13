"""用一次性内核 capability 约束 private container acceptance Make target。"""

from __future__ import annotations

import array
import hashlib
import json
import os
import re
import socket
import stat
import struct
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from scripts.container_acceptance_candidate_authority import CandidateSnapshotIdentity, PreparedCandidateAuthority
    from scripts.container_acceptance_contract import AcceptanceVerifierIdentity
    from scripts.container_acceptance_lock import AcceptanceLifecycleLock
    from scripts.container_acceptance_receipt import PreparedReceiptAuthority

MAKE_GATE_FD_ENV: Final = "AGENT_GOV_ACCEPTANCE_MAKE_GATE_FD"
MAKE_GATE_NONCE_ENV: Final = "AGENT_GOV_ACCEPTANCE_MAKE_GATE_NONCE"
MAKE_GATE_ENVIRONMENT_KEYS: Final = frozenset({MAKE_GATE_FD_ENV, MAKE_GATE_NONCE_ENV})
_CONTRACT: Final = "agentgov.container-acceptance-make-gate.v1"
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_TARGET = re.compile(r"^_[a-z0-9][a-z0-9-]{0,63}$")
_MAX_MESSAGE_BYTES: Final = 64 * 1024
_MAX_PROC_BYTES: Final = 16 * 1024
_MAX_PWD_BYTES: Final = 4096
_CREDENTIALS_SIZE: Final = struct.calcsize("3i")
_MAX_PROCESS_DEPTH: Final = 8
_SERVER_TIMEOUT_SECONDS: Final = 0.2
_CLIENT_TIMEOUT_SECONDS: Final = 5.0
_MAX_FAILURE_CODE_DEPTH: Final = 6
_FAILURE_CODE_PART = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FAILURE_PHASE_ATTRIBUTE: Final = "_agentgov_make_gate_failure_phase"
_FAILURE_PHASES: Final = frozenset({"invocation", "gate_open", "authority_exchange"})
_MAKE_RUNTIME_KEYS: Final = frozenset({"MAKEFLAGS", "MAKELEVEL", "MFLAGS", "MAKE_TERMERR", "MAKE_TERMOUT", "PWD"})
_MAKE_FLAG = re.compile(r"^(?:-?[krsS]+|--no-print-directory)$")
_TTY_PATH = re.compile(r"^/dev/(?:pts/[0-9]+|tty[0-9]+)$")
_NESTED_TARGETS: Final = {
    "_container-core-smoke": frozenset({"_smoke", "_ui-smoke", "_container-openapi-check"}),
    "_container-health-e2e": frozenset({"_container-health-diagnose"}),
}
_BROWSER_AUTHORITY_TARGETS: Final = frozenset(
    {
        "_container-health-e2e",
        "_container-openapi-check",
        "_ui-feedback-smoke",
        "_ui-openai-responses-smoke",
        "_ui-playground-cancel-smoke",
    }
)


class MakeGateError(RuntimeError):
    """private Make verifier 未持有当前验收生命周期能力。"""


class MakeGateEnvironment(dict[str, str]):
    """承载受管 verifier 环境及 Make/gate 瞬态键。"""


class MakeGateRequest(TypedDict):
    contract: str
    nonce: str
    pid: int
    target: str


class MakeGateResponse(TypedDict):
    contract: str
    permit: str
    receipt_authority: str
    request_sha256: str
    toolchain_evidence: str
    toolchain_sha256: str


class GateProcessRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class _GateAuthority:
    command: tuple[str, ...]
    target: str
    allowed_targets: frozenset[str]
    managed: Mapping[str, str]
    profile: str
    verifier: AcceptanceVerifierIdentity
    candidate: PreparedCandidateAuthority
    receipt: PreparedReceiptAuthority
    lock: AcceptanceLifecycleLock


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _safe_failure_code(error: BaseException) -> str:
    """仅投影有界异常类型链，不泄露异常消息或运行环境。"""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(parts) < _MAX_FAILURE_CODE_DEPTH and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        parts.append(name if _FAILURE_CODE_PART.fullmatch(name) is not None else "UnknownFailure")
        current = current.__cause__ or current.__context__
    return ".".join(parts)


def _mark_failure_phase(error: BaseException, phase: str) -> BaseException:
    if phase not in _FAILURE_PHASES:
        raise RuntimeError("private Make gate failure phase is not registered")
    error.__dict__[_FAILURE_PHASE_ATTRIBUTE] = phase
    return error


def _safe_failure_phase(error: BaseException) -> str:
    phase = error.__dict__.get(_FAILURE_PHASE_ATTRIBUTE)
    return phase if isinstance(phase, str) and phase in _FAILURE_PHASES else "invocation"


def validate_make_gate_environment(environ: Mapping[str, str]) -> MakeGateEnvironment:
    present = MAKE_GATE_ENVIRONMENT_KEYS.intersection(environ)
    if not present:
        return MakeGateEnvironment()
    raw_fd = environ.get(MAKE_GATE_FD_ENV, "")
    nonce = environ.get(MAKE_GATE_NONCE_ENV, "")
    if present != MAKE_GATE_ENVIRONMENT_KEYS or not raw_fd.isascii() or not raw_fd.isdecimal() or int(raw_fd) < 3 or _NONCE.fullmatch(nonce) is None:
        raise MakeGateError("private Make gate environment is invalid")
    return MakeGateEnvironment({MAKE_GATE_FD_ENV: raw_fd, MAKE_GATE_NONCE_ENV: nonce})


def managed_environment_from_gate_spawn(environ: Mapping[str, str]) -> MakeGateEnvironment:
    overlay = validate_make_gate_environment(environ)
    working_directory = environ.get("PWD")
    if overlay:
        valid_pwd = (
            isinstance(working_directory, str)
            and Path(working_directory).is_absolute()
            and 0 < len(working_directory.encode()) <= _MAX_PWD_BYTES
            and "\x00" not in working_directory
            and "\n" not in working_directory
            and "\r" not in working_directory
        )
        if not valid_pwd:
            raise MakeGateError("private Make gate spawn environment is invalid")
    elif working_directory is not None:
        raise MakeGateError("private Make gate spawn environment is invalid")
    transient = {*MAKE_GATE_ENVIRONMENT_KEYS, "PWD"}
    return MakeGateEnvironment({key: value for key, value in environ.items() if key not in transient})


def _validate_authority(authority: _GateAuthority) -> None:
    from scripts import container_acceptance_contract as acceptance_contract
    from scripts import container_acceptance_lock as acceptance_lock
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    acceptance_contract.validate_managed_environment(authority.managed)
    expected = acceptance_contract.verifier_execution_argv(
        authority.profile,
        authority.verifier,
        authority.candidate.snapshot_repository_root,
    )
    identity = authority.receipt.identity
    current = authority.receipt.verify_current()
    authority.lock.assert_current()
    valid = (
        authority.command == expected
        and authority.target == authority.command[-1]
        and authority.target == authority.verifier.invocation_argv[-1]
        and identity.profile == authority.profile
        and identity.verifier == authority.verifier
        and identity.candidate_snapshot == authority.candidate.recovery
        and identity.managed_environment_sha256 == authority.managed.get(acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV)
        and current == authority.receipt.prepared_sha256
        and authority.receipt.lifecycle_lock_sha256 == acceptance_lock.lifecycle_descriptor_sha256(authority.lock.descriptor)
    )
    if not valid:
        raise MakeGateError("private Make gate authority is inconsistent")
    acceptance_toolchain.validate_execution_tool_authority(authority.managed, commands=("make",))


def _validate_browser_target_authority(target: str) -> None:
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    if target in _BROWSER_AUTHORITY_TARGETS:
        acceptance_toolchain.validate_browser_runtime_authority()


def _gate_environment(managed: Mapping[str, str], descriptor: int, nonce: str, repository: Path) -> MakeGateEnvironment:
    environment = managed_environment_from_gate_spawn(managed)
    environment.update({MAKE_GATE_FD_ENV: str(descriptor), MAKE_GATE_NONCE_ENV: nonce, "PWD": str(repository)})
    managed_environment_from_gate_spawn(environment)
    return environment


def _proc_stat(pid: int) -> tuple[int, int, int]:
    try:
        encoded = _read_bounded_proc_file(pid, "stat")
        fields = encoded[encoded.rfind(b") ") + 2 :].split()
        return int(fields[1]), int(fields[2]), int(fields[3])
    except (IndexError, OSError, ValueError) as exc:
        raise MakeGateError("private Make gate process identity is unavailable") from exc


def _make_process(sender_pid: int, command: tuple[str, ...]) -> int:
    current = sender_pid
    runner_pid = os.getpid()
    descendants: list[tuple[int, int, int]] = []
    for _depth in range(_MAX_PROCESS_DEPTH):
        parent, group, session = _proc_stat(current)
        if parent == runner_pid:
            if current != group or current != session:
                raise MakeGateError("private Make verifier is not an isolated process group")
            _verify_make_process(current, command)
            if any(descendant_group != current or descendant_session != current for _pid, descendant_group, descendant_session in descendants):
                raise MakeGateError("private Make gate sender escaped the verifier process group")
            return current
        if parent <= 1 or parent == current:
            break
        descendants.append((current, group, session))
        current = parent
    raise MakeGateError("private Make gate sender is outside the verifier process group")


def _verify_make_process(pid: int, command: tuple[str, ...]) -> None:
    record = _tool_record("make")
    if _proc_cmdline(pid) != tuple(os.fsencode(item) for item in command) or not _process_executable_matches(pid, record):
        raise MakeGateError("private Make process authority drifted")


def _read_bounded_proc_file(pid: int, leaf: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(f"/proc/{pid}/{leaf}", os.O_RDONLY | os.O_CLOEXEC)
        encoded = os.read(descriptor, _MAX_PROC_BYTES + 1)
    except OSError as exc:
        raise MakeGateError("private Make gate process identity is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(encoded) > _MAX_PROC_BYTES:
        raise MakeGateError("private Make gate process identity is oversized")
    return encoded


def _proc_cmdline(pid: int) -> tuple[bytes, ...]:
    encoded = _read_bounded_proc_file(pid, "cmdline")
    if not encoded or not encoded.endswith(b"\0"):
        raise MakeGateError("private Make gate process command is invalid")
    arguments = tuple(encoded[:-1].split(b"\0"))
    if not arguments or any(not argument for argument in arguments):
        raise MakeGateError("private Make gate process command is invalid")
    return arguments


def _tool_record(command: str) -> Mapping[str, object]:
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    records = tuple(item for item in acceptance_toolchain.active_toolchain_authority().payload["tools"] if item["command"] == command)
    if len(records) != 1:
        raise MakeGateError("private Make gate tool authority is unavailable")
    return records[0]


def _stat_identity(identity: os.stat_result) -> tuple[int, ...]:
    return (
        identity.st_dev,
        identity.st_ino,
        stat.S_IMODE(identity.st_mode),
        identity.st_uid,
        identity.st_gid,
        identity.st_size,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    )


def _process_executable_matches(pid: int, record: Mapping[str, object]) -> bool:
    descriptor: int | None = None
    try:
        descriptor = os.open(f"/proc/{pid}/exe", os.O_RDONLY | os.O_CLOEXEC)
        identity = os.fstat(descriptor)
    except OSError as exc:
        raise MakeGateError("private Make gate process executable is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    expected = tuple(record.get(key) for key in ("device", "inode", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"))
    return stat.S_ISREG(identity.st_mode) and _stat_identity(identity) == expected


def _bootstrap_transport_argv(cmdline: tuple[bytes, ...]) -> bool:
    if len(cmdline) != 17 or any(re.fullmatch(rb"[0-9]+", item) is None for item in cmdline[7:10]):
        return False
    descriptors = tuple(int(item) for item in cmdline[7:10])
    return min(descriptors) >= 3 and len(set(descriptors)) == 3 and cmdline[6] == b"/proc/self/fd/" + cmdline[8]


def _process_cwd_matches_snapshot(pid: int, snapshot: CandidateSnapshotIdentity) -> bool:
    from scripts import container_acceptance_candidate_storage as candidate_storage

    descriptor: int | None = None
    proc_path = f"/proc/{pid}/cwd"
    try:
        descriptor = os.open(proc_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        before = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
        linked = candidate_storage.real_directory_identity(snapshot.repository_root)
        link_target = os.readlink(proc_path)
        after = candidate_storage.PathIdentity.from_stat(os.fstat(descriptor))
    except (OSError, candidate_storage.CandidateStorageError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return before == after == linked == snapshot.repository_identity and link_target == str(snapshot.repository_root)


def _verify_helper_process(pid: int, target: str, authority: _GateAuthority) -> None:
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    snapshot = authority.receipt.identity.candidate_snapshot.snapshot
    python_record = _tool_record("python")
    script = snapshot.repository_root / "scripts/container_acceptance_make_gate.py"
    cmdline = _proc_cmdline(pid)
    import_authority = snapshot.repository_root / "scripts/container_acceptance_import_authority.py"
    toolchain = snapshot.repository_root / "scripts/container_acceptance_toolchain.py"
    expected = (
        os.fsencode(str(python_record.get("invocation_path"))),
        b"-I",
        b"-P",
        b"-S",
        b"-X",
        b"pycache_prefix=/dev/null",
        cmdline[6],
        *cmdline[7:10],
        os.fsencode(str(import_authority)),
        os.fsencode(str(toolchain)),
        os.fsencode(str(snapshot.repository_root)),
        b"python",
        os.fsencode(str(script)),
        b"check",
        os.fsencode(target),
    )
    valid = (
        _bootstrap_transport_argv(cmdline)
        and cmdline == expected
        and authority.managed.get(acceptance_toolchain.PYTHON_EXECUTABLE_ENV) == python_record.get("invocation_path")
        and _process_executable_matches(pid, python_record)
        and _process_cwd_matches_snapshot(pid, snapshot)
    )
    if not valid:
        raise MakeGateError("private Make gate helper authority drifted")


def _credentials(ancillary: list[tuple[int, int, bytes]]) -> tuple[int, int, int]:
    descriptors, valid_rights = _right_descriptors(ancillary)
    values = [
        struct.unpack("3i", data)
        for level, kind, data in ancillary
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == _CREDENTIALS_SIZE
    ]
    if descriptors or not valid_rights or len(ancillary) != 1 or len(values) != 1:
        _close_descriptors(descriptors)
        raise MakeGateError("private Make gate sender credentials are invalid")
    return values[0]


def _right_descriptors(ancillary: list[tuple[int, int, bytes]]) -> tuple[list[int], bool]:
    descriptors: list[int] = []
    valid = True
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            continue
        values = array.array("i")
        valid = valid and len(data) % values.itemsize == 0
        values.frombytes(data[: len(data) - (len(data) % values.itemsize)])
        descriptors.extend(values)
    return descriptors, valid


def _close_descriptors(descriptors: Sequence[int]) -> None:
    for descriptor in descriptors:
        with suppress(OSError):
            os.close(descriptor)


def _request(encoded: bytes, nonce: str, allowed_targets: frozenset[str]) -> tuple[MakeGateRequest, str]:
    try:
        payload = json.loads(encoded)
    except (UnicodeError, ValueError) as exc:
        raise MakeGateError("private Make gate request is invalid") from exc
    required = {"contract", "nonce", "pid", "target"}
    target = payload.get("target") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("contract") != _CONTRACT
        or payload.get("nonce") != nonce
        or type(payload.get("pid")) is not int
        or not isinstance(target, str)
        or target not in allowed_targets
        or _TARGET.fullmatch(target) is None
        or encoded != _canonical_json(payload)
    ):
        raise MakeGateError("private Make gate request is invalid")
    return cast(MakeGateRequest, payload), target


def _response(request: MakeGateRequest, authority: _GateAuthority) -> bytes:
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    toolchain_environment = acceptance_toolchain.serialized_authority_environment(acceptance_toolchain.active_toolchain_authority())
    return _canonical_json(
        {
            "contract": _CONTRACT,
            "permit": "once",
            "receipt_authority": authority.receipt.to_json(),
            "request_sha256": hashlib.sha256(_canonical_json(request)).hexdigest(),
            "toolchain_evidence": toolchain_environment[acceptance_toolchain.TOOLCHAIN_EVIDENCE_ENV],
            "toolchain_sha256": toolchain_environment[acceptance_toolchain.TOOLCHAIN_SHA256_ENV],
        }
    )


def _cwd_matches_snapshot(snapshot: CandidateSnapshotIdentity) -> bool:
    return _process_cwd_matches_snapshot(os.getpid(), snapshot)


def _valid_terminal(value: str | None) -> bool:
    if not isinstance(value, str) or len(value.encode()) > 128 or _TTY_PATH.fullmatch(value) is None:
        return False
    try:
        identity = os.stat(value, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISCHR(identity.st_mode) and identity.st_uid in {0, os.geteuid()}


def _managed_gate_environment(environ: Mapping[str, str]) -> MakeGateEnvironment:
    level = environ.get("MAKELEVEL", "")
    flags = environ.get("MAKEFLAGS", "")
    mflags = environ.get("MFLAGS", "")
    working_directory = environ.get("PWD", "")
    terminal_values = tuple(environ.get(key) for key in ("MAKE_TERMOUT", "MAKE_TERMERR"))
    tokens = (*flags.split(), *mflags.split())
    valid = (
        level.isdecimal()
        and 1 <= int(level) <= _MAX_PROCESS_DEPTH
        and len(flags) <= 1024
        and len(mflags) <= 1024
        and all(_MAKE_FLAG.fullmatch(token) is not None for token in tokens)
        and (terminal_values == (None, None) or all(_valid_terminal(value) for value in terminal_values))
        and Path(working_directory).is_absolute()
        and "\n" not in working_directory
    )
    if not valid:
        raise MakeGateError("private Make runtime environment is invalid")
    without_make_runtime = MakeGateEnvironment({key: value for key, value in environ.items() if key not in _MAKE_RUNTIME_KEYS - {"PWD"}})
    return managed_environment_from_gate_spawn(without_make_runtime)


def _serve_gate(
    server: socket.socket,
    authority: _GateAuthority,
    nonce: str,
    stop: threading.Event,
    used: set[str],
) -> None:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    server.settimeout(_SERVER_TIMEOUT_SECONDS)
    while not stop.is_set():
        try:
            encoded, ancillary, flags, _address = server.recvmsg(_MAX_MESSAGE_BYTES + 1, socket.CMSG_SPACE(_CREDENTIALS_SIZE))
        except TimeoutError:
            continue
        if not encoded:
            descriptors, _valid = _right_descriptors(ancillary)
            _close_descriptors(descriptors)
            return
        if len(encoded) > _MAX_MESSAGE_BYTES or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            descriptors, _valid = _right_descriptors(ancillary)
            _close_descriptors(descriptors)
            raise MakeGateError("private Make gate request is oversized")
        pid, uid, gid = _credentials(ancillary)
        request, target = _request(encoded, nonce, authority.allowed_targets)
        if pid != request["pid"] or uid != os.geteuid() or gid != os.getegid() or target in used:
            raise MakeGateError("private Make gate sender identity is invalid")
        _verify_helper_process(pid, target, authority)
        _make_process(pid, authority.command)
        _validate_authority(authority)
        _validate_browser_target_authority(target)
        response = _response(request, authority)
        if len(response) > _MAX_MESSAGE_BYTES:
            raise MakeGateError("private Make gate response is oversized")
        server.sendmsg(
            [response],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [authority.lock.descriptor]))],
        )
        used.add(target)


def _server_thread(
    server: socket.socket,
    authority: _GateAuthority,
    nonce: str,
    stop: threading.Event,
    used: set[str],
    errors: list[BaseException],
) -> None:
    try:
        _serve_gate(server, authority, nonce, stop, used)
    except BaseException as exc:
        if not stop.is_set():
            errors.append(exc)
            with suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)


def run_make_verifier(
    command: Sequence[str],
    managed: Mapping[str, str],
    *,
    profile: str,
    verifier: AcceptanceVerifierIdentity,
    candidate: PreparedCandidateAuthority,
    receipt: PreparedReceiptAuthority,
    lock: AcceptanceLifecycleLock,
    process_runner: GateProcessRunner,
) -> int:
    frozen_command = tuple(command)
    target = frozen_command[-1] if frozen_command else ""
    authority = _GateAuthority(
        frozen_command,
        target,
        frozenset({target, *_NESTED_TARGETS.get(target, ())}),
        managed,
        profile,
        verifier,
        candidate,
        receipt,
        lock,
    )
    _validate_authority(authority)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    nonce = os.urandom(16).hex()
    stop = threading.Event()
    used: set[str] = set()
    errors: list[BaseException] = []
    worker = threading.Thread(target=_server_thread, args=(server, authority, nonce, stop, used, errors), daemon=True)
    try:
        os.set_inheritable(client.fileno(), True)
        worker.start()
        result = process_runner(
            list(frozen_command),
            _gate_environment(managed, client.fileno(), nonce, authority.candidate.snapshot_repository_root),
            (client.fileno(),),
        )
    finally:
        stop.set()
        client.close()
        server.close()
        worker.join(timeout=1)
    if worker.is_alive() or errors or target not in used:
        raise MakeGateError("private Make gate did not issue the exact verifier permit") from (errors[0] if errors else None)
    return result


def _received_descriptor(
    ancillary: list[tuple[int, int, bytes]],
    *,
    flags: int = 0,
    oversized: bool = False,
) -> int:
    descriptors, valid_rights = _right_descriptors(ancillary)
    invalid = oversized or bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)) or not valid_rights or len(ancillary) != 1 or len(descriptors) != 1
    if invalid:
        _close_descriptors(descriptors)
        raise MakeGateError("private Make gate lifecycle descriptor is invalid")
    return descriptors[0]


def _validate_receipt_authority(
    payload: MakeGateResponse,
    target: str,
    environ: Mapping[str, str],
    lifecycle_descriptor: int,
) -> None:
    from scripts import container_acceptance_contract as acceptance_contract
    from scripts import container_acceptance_lock as acceptance_lock
    from scripts import container_acceptance_receipt as acceptance_receipt
    from scripts import container_acceptance_toolchain as acceptance_toolchain

    raw_receipt = payload.get("receipt_authority")
    evidence = payload.get("toolchain_evidence")
    toolchain_sha256 = payload.get("toolchain_sha256")
    if not all(isinstance(value, str) for value in (raw_receipt, evidence, toolchain_sha256)):
        raise MakeGateError("private Make gate durable authority is invalid")
    acceptance_toolchain.initialize_toolchain_authority(
        {
            acceptance_toolchain.TOOLCHAIN_EVIDENCE_ENV: evidence,
            acceptance_toolchain.TOOLCHAIN_SHA256_ENV: toolchain_sha256,
        }
    )
    receipt = acceptance_receipt.PreparedReceiptAuthority.from_json(raw_receipt)
    identity = receipt.identity
    snapshot = identity.candidate_snapshot.snapshot
    managed_digest = acceptance_contract.managed_environment_sha256(_managed_gate_environment(environ))
    outer_target = identity.verifier.invocation_argv[-1]
    lock_digest = acceptance_lock.lifecycle_descriptor_sha256(lifecycle_descriptor)
    valid = (
        receipt.verify_current() == receipt.prepared_sha256
        and receipt.lifecycle_lock_sha256 == lock_digest
        and identity.profile == environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE")
        and identity.run_id == environ.get("AGENT_GOV_ACCEPTANCE_RUN_ID")
        and identity.managed_environment_sha256 == managed_digest
        and managed_digest == environ.get(acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV)
        and toolchain_sha256 == environ.get(acceptance_toolchain.TOOLCHAIN_SHA256_ENV)
        and target in {outer_target, *_NESTED_TARGETS.get(outer_target, ())}
        and environ.get("PWD") == str(snapshot.repository_root)
        and _cwd_matches_snapshot(snapshot)
    )
    if not valid:
        raise MakeGateError("private Make gate durable authority is inconsistent")


def _take_gate_socket(descriptor: int) -> socket.socket:
    duplicate: int | None = None
    gate: socket.socket | None = None
    try:
        duplicate = os.dup(descriptor)
        gate = socket.socket(fileno=duplicate)
        duplicate = None
        gate.settimeout(_CLIENT_TIMEOUT_SECONDS)
        if gate.family != socket.AF_UNIX or gate.type != socket.SOCK_SEQPACKET:
            raise MakeGateError("private Make gate descriptor is invalid")
        return gate
    except BaseException:
        if duplicate is not None:
            os.close(duplicate)
        elif gate is not None:
            gate.close()
        raise
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _open_gate_socket(target: str, environ: Mapping[str, str]) -> tuple[socket.socket, MakeGateEnvironment]:
    descriptor: int | None = None
    try:
        overlay = validate_make_gate_environment(environ)
        descriptor = int(overlay[MAKE_GATE_FD_ENV])
        managed_environment_from_gate_spawn(environ)
        if not isinstance(target, str) or _TARGET.fullmatch(target) is None:
            raise MakeGateError("private Make gate target is invalid")
        gate = _take_gate_socket(descriptor)
        descriptor = None
        return gate, overlay
    except MakeGateError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise
    except (KeyError, OSError, ValueError) as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise MakeGateError("private Make gate descriptor is invalid") from exc


def _response_payload(response: bytes, flags: int, encoded_request: bytes) -> MakeGateResponse:
    if len(response) > _MAX_MESSAGE_BYTES or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise MakeGateError("private Make gate response is oversized")
    payload = json.loads(response)
    required = {
        "contract",
        "permit",
        "receipt_authority",
        "request_sha256",
        "toolchain_evidence",
        "toolchain_sha256",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("contract") != _CONTRACT
        or payload.get("permit") != "once"
        or payload.get("request_sha256") != hashlib.sha256(encoded_request).hexdigest()
        or response != _canonical_json(payload)
    ):
        raise MakeGateError("private Make gate response is invalid")
    return cast(MakeGateResponse, payload)


def _exchange_gate_authority(
    gate: socket.socket,
    request: MakeGateRequest,
    target: str,
    environ: Mapping[str, str],
) -> None:
    encoded = _canonical_json(request)
    lifecycle_descriptor: int | None = None
    try:
        gate.sendall(encoded)
        response, ancillary, flags, _address = gate.recvmsg(
            _MAX_MESSAGE_BYTES + 1,
            socket.CMSG_SPACE(array.array("i").itemsize),
        )
        lifecycle_descriptor = _received_descriptor(
            ancillary,
            flags=flags,
            oversized=len(response) > _MAX_MESSAGE_BYTES,
        )
        payload = _response_payload(response, flags, encoded)
        _validate_receipt_authority(payload, target, environ, lifecycle_descriptor)
    finally:
        if lifecycle_descriptor is not None:
            os.close(lifecycle_descriptor)


def check_make_gate(target: str, environ: Mapping[str, str]) -> None:
    frozen_environment = dict(environ)
    try:
        gate, overlay = _open_gate_socket(target, frozen_environment)
    except MakeGateError as exc:
        _mark_failure_phase(exc, "gate_open")
        raise
    request = MakeGateRequest(contract=_CONTRACT, nonce=overlay[MAKE_GATE_NONCE_ENV], pid=os.getpid(), target=target)
    try:
        _exchange_gate_authority(gate, request, target, frozen_environment)
    except (OSError, UnicodeError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        failure = MakeGateError("private Make gate handshake failed")
        _mark_failure_phase(failure, "authority_exchange")
        raise failure from exc
    finally:
        gate.close()


def main() -> int:
    try:
        if len(os.sys.argv) != 3 or os.sys.argv[1] != "check":
            failure = MakeGateError("private Make gate invocation is invalid")
            _mark_failure_phase(failure, "invocation")
            raise failure
        check_make_gate(os.sys.argv[2], os.environ)
    except MakeGateError as exc:
        os.sys.stderr.write(f"failure_phase={_safe_failure_phase(exc)} failure_code={_safe_failure_code(exc)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
