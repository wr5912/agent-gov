#!/usr/bin/env python3
"""Exercise archive/load/Compose/health/recovery through an isolated real shell."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict
from urllib.request import ProxyHandler, Request, build_opener

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_atomic_cutover_archive import validate_image_archive

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
PORT_RANGE: Final = range(50490, 50500)


class BoundaryAcceptanceError(RuntimeError):
    """The real isolated deployment boundary was not proven."""


class BoundaryEvidence(TypedDict):
    schema: str
    scope: str
    archive_validation: bool
    exclusive_lock: bool
    docker_load: bool
    compose_recreate: bool
    health: bool
    health_failure_probe: bool
    primitive_restore: bool
    transaction_execute_recover: bool
    compose_config_forbidden: bool
    old_archive_sha256: str
    candidate_archive_sha256: str
    old_image_id_sha256: str
    candidate_image_id_sha256: str
    transport: str


@dataclass(frozen=True)
class RemoteShell:
    ssh: tuple[str, ...]
    environment: Mapping[str, str]
    trace: list[tuple[str, ...]]

    def command(self, arguments: list[str]) -> list[str]:
        return [*self.ssh, shlex.join(arguments)]

    def run(self, arguments: list[str], *, capture: bool = False) -> str:
        if arguments[:2] == ["docker", "compose"] and "config" in arguments[2:]:
            raise BoundaryAcceptanceError("远端验收禁止 docker compose config")
        self.trace.append(tuple(arguments))
        result = subprocess.run(
            self.command(arguments),
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise BoundaryAcceptanceError("隔离 SSH remote shell 命令失败")
        return result.stdout.strip() if capture else ""


def _process_environment(home: Path) -> Mapping[str, str]:
    return {
        "HOME": home.as_posix(),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }


def _control_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _generate_key(path: Path, environment: Mapping[str, str]) -> None:
    result = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path.as_posix()],
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise BoundaryAcceptanceError("临时 SSH key 生成失败")


def _write_sshd_configuration(
    path: Path,
    *,
    port: int,
    host_key: Path,
    authorized_keys: Path,
    username: str,
) -> None:
    path.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {path.parent / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "PubkeyAuthentication yes",
                "StrictModes no",
                "PermitRootLogin no",
                "LogLevel ERROR",
                f"AllowUsers {username}",
                "",
            )
        ),
        encoding="utf-8",
    )


def _ssh_client(client_key: Path, port: int, username: str, environment: Mapping[str, str]) -> RemoteShell:
    return RemoteShell(
        ssh=(
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            client_key.as_posix(),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{username}@127.0.0.1",
        ),
        environment=environment,
        trace=[],
    )


@contextmanager
def _ephemeral_ssh(root: Path) -> Iterator[RemoteShell]:
    sshd = next((path for path in (Path("/usr/sbin/sshd"), Path("/usr/local/sbin/sshd")) if path.is_file()), None)
    if sshd is None or shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        raise BoundaryAcceptanceError("真实 SSH 工具链不可用")
    host_key, client_key = root / "host-key", root / "client-key"
    environment = _process_environment(root)
    for key in (host_key, client_key):
        _generate_key(key, environment)
    authorized_keys = root / "authorized_keys"
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized_keys.chmod(0o600)
    port = _control_port()
    username = pwd.getpwuid(os.geteuid()).pw_name
    configuration = root / "sshd_config"
    _write_sshd_configuration(
        configuration,
        port=port,
        host_key=host_key,
        authorized_keys=authorized_keys,
        username=username,
    )
    server = subprocess.Popen(
        [sshd.as_posix(), "-D", "-e", "-f", configuration.as_posix()],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shell = _ssh_client(client_key, port, username, environment)
    try:
        for _attempt in range(50):
            if server.poll() is not None:
                raise BoundaryAcceptanceError("临时 sshd 未能启动")
            try:
                shell.run(["uname", "-s"])
                break
            except BoundaryAcceptanceError:
                time.sleep(0.1)
        else:
            raise BoundaryAcceptanceError("临时 sshd 未就绪")
        yield shell
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def _docker_output(shell: RemoteShell, *arguments: str) -> str:
    return shell.run(["docker", *arguments], capture=True)


def _verify_exclusive_lock(shell: RemoteShell, lock_path: Path) -> None:
    holder = subprocess.Popen(
        shell.command(["flock", "-n", lock_path.as_posix(), "sleep", "5"]),
        env=shell.environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _attempt in range(50):
            probe = subprocess.run(
                shell.command(["flock", "-n", lock_path.as_posix(), "test", "-d", lock_path.parent.as_posix()]),
                env=shell.environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode != 0:
                break
            if holder.poll() is not None:
                raise BoundaryAcceptanceError("远端排他锁 holder 提前退出")
            time.sleep(0.1)
        else:
            raise BoundaryAcceptanceError("远端排他锁未拒绝第二个执行者")
    finally:
        holder.terminate()
        with suppress(subprocess.TimeoutExpired):
            holder.wait(timeout=1)
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    for _attempt in range(60):
        try:
            shell.run(["flock", "-n", lock_path.as_posix(), "test", "-d", lock_path.parent.as_posix()])
            return
        except BoundaryAcceptanceError:
            time.sleep(0.1)
    raise BoundaryAcceptanceError("远端排他锁在 holder 结束后未释放")


def _available_port() -> int:
    for port in PORT_RANGE:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise BoundaryAcceptanceError("50490-50499 没有可用隔离验收端口")


def _save_archive(shell: RemoteShell, reference: str, archive: Path) -> str:
    save = subprocess.Popen(
        shell.command(["docker", "save", reference]),
        env=shell.environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert save.stdout is not None
    try:
        with archive.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
            shutil.copyfileobj(save.stdout, compressed, length=1024 * 1024)
        if save.wait() != 0:
            raise BoundaryAcceptanceError("真实 docker save 失败")
    finally:
        save.stdout.close()
        if save.poll() is None:
            save.kill()
            save.wait()
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def _load_archive(shell: RemoteShell, archive: Path) -> None:
    load = subprocess.Popen(
        shell.command(["docker", "load"]),
        env=shell.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert load.stdin is not None
    try:
        with gzip.open(archive, "rb") as source:
            shutil.copyfileobj(source, load.stdin, length=1024 * 1024)
        load.stdin.close()
        if load.wait() != 0:
            raise BoundaryAcceptanceError("真实 docker load 失败")
    finally:
        if not load.stdin.closed:
            load.stdin.close()
        if load.poll() is None:
            load.kill()
            load.wait()


def _write_compose(path: Path, reference: str, port: int) -> None:
    path.write_text(
        f"services:\n  web:\n    image: {reference}\n    environment:\n      FRONTEND_PORT: '5173'\n    ports:\n      - '127.0.0.1:{port}:5173'\n",
        encoding="utf-8",
    )


def _compose(shell: RemoteShell, project: str, compose_file: Path, *arguments: str) -> None:
    shell.run(["docker", "compose", "--project-name", project, "--file", compose_file.as_posix(), *arguments])


def _health(port: int, attempts: int = 45) -> None:
    opener = build_opener(ProxyHandler({}))
    for _attempt in range(attempts):
        try:
            with opener.open(Request(f"http://127.0.0.1:{port}", headers={"User-Agent": "agentgov-boundary-acceptance"}), timeout=3) as response:
                response.read(1)
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise BoundaryAcceptanceError("真实 Compose HTTP health 未通过")


def _prove_health_probe_failure() -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = int(unavailable.getsockname()[1])
        try:
            _health(port, attempts=1)
        except BoundaryAcceptanceError:
            return
    raise BoundaryAcceptanceError("故障恢复分支未被真实触发")


@dataclass(frozen=True)
class BoundaryImages:
    old_reference: str
    candidate_reference: str
    old_archive: Path
    candidate_archive: Path
    old_id: str
    candidate_id: str
    old_digest: str
    candidate_digest: str


def _prepare_images(shell: RemoteShell, root: Path, source: str, run_id: str) -> BoundaryImages:
    old_reference = f"agentgov-remote-boundary-old:{run_id}"
    candidate_reference = f"agentgov-remote-boundary-candidate:{run_id}"
    source_id = _docker_output(shell, "image", "inspect", source, "--format", "{{.Id}}")
    architecture = _docker_output(shell, "image", "inspect", source, "--format", "{{.Architecture}}")
    source_digest = _docker_output(
        shell,
        "image",
        "inspect",
        source,
        "--format",
        '{{ index .Config.Labels "io.agentgov.source-artifact-sha256" }}',
    )
    if not source_id.startswith("sha256:") or len(source_digest) != 64:
        raise BoundaryAcceptanceError("验收源镜像缺少不可变 identity 或 source label")
    shell.run(["docker", "image", "tag", source, old_reference])
    variant_container = f"agentgov-remote-boundary-image-{run_id}"
    shell.run(["docker", "container", "create", "--name", variant_container, source])
    shell.run(
        ["docker", "commit", "--change", f"LABEL io.agentgov.boundary-variant={run_id}", variant_container, candidate_reference]
    )
    candidate_id = _docker_output(shell, "image", "inspect", candidate_reference, "--format", "{{.Id}}")
    if candidate_id == source_id or not candidate_id.startswith("sha256:"):
        raise BoundaryAcceptanceError("候选与旧镜像必须具有不同 immutable identity")
    old_archive, candidate_archive = root / "old.tar.gz", root / "candidate.tar.gz"
    old_digest = _save_archive(shell, old_reference, old_archive)
    candidate_digest = _save_archive(shell, candidate_reference, candidate_archive)
    validate_image_archive(
        candidate_archive,
        architecture=architecture,
        expected_images=frozenset({candidate_reference}),
        source_digest=source_digest,
    )
    return BoundaryImages(
        old_reference,
        candidate_reference,
        old_archive,
        candidate_archive,
        source_id,
        candidate_id,
        old_digest,
        candidate_digest,
    )


def _exercise_primitives(shell: RemoteShell, images: BoundaryImages, compose_file: Path, project: str, port: int) -> None:
    _write_compose(compose_file, images.old_reference, port)
    _compose(shell, project, compose_file, "up", "-d", "--force-recreate", "--no-build", "--pull", "never")
    _health(port)
    shell.run(["docker", "image", "rm", images.candidate_reference])
    _load_archive(shell, images.candidate_archive)
    if _docker_output(shell, "image", "inspect", images.candidate_reference, "--format", "{{.Id}}") != images.candidate_id:
        raise BoundaryAcceptanceError("真实 docker load 未恢复 candidate identity")
    _write_compose(compose_file, images.candidate_reference, port)
    _compose(shell, project, compose_file, "up", "-d", "--force-recreate", "--no-build", "--pull", "never")
    _health(port)
    _prove_health_probe_failure()
    shell.run(["docker", "image", "rm", images.old_reference])
    _load_archive(shell, images.old_archive)
    _write_compose(compose_file, images.old_reference, port)
    _compose(shell, project, compose_file, "up", "-d", "--force-recreate", "--no-build", "--pull", "never")
    _health(port)


def _evidence(shell: RemoteShell, images: BoundaryImages) -> BoundaryEvidence:
    return {
        "schema": "agentgov-remote-deploy-boundary-evidence-v2",
        "scope": "primitives_only",
        "archive_validation": True,
        "exclusive_lock": True,
        "docker_load": True,
        "compose_recreate": True,
        "health": True,
        "health_failure_probe": True,
        "primitive_restore": True,
        "transaction_execute_recover": False,
        "compose_config_forbidden": not any(
            command[:2] == ("docker", "compose") and "config" in command[2:] for command in shell.trace
        ),
        "old_archive_sha256": images.old_digest,
        "candidate_archive_sha256": images.candidate_digest,
        "old_image_id_sha256": hashlib.sha256(images.old_id.encode()).hexdigest(),
        "candidate_image_id_sha256": hashlib.sha256(images.candidate_id.encode()).hexdigest(),
        "transport": "ephemeral-sshd-loopback",
    }


def run() -> BoundaryEvidence:
    """Run the destructive-only-to-unique-fixtures acceptance and return safe evidence."""
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    source = os.environ.get("REMOTE_DEPLOY_ACCEPTANCE_IMAGE", f"agent-gov-ui:{version}")
    run_id = uuid.uuid4().hex[:16]
    project = f"agentgov-remote-boundary-{run_id}"
    variant_container = f"agentgov-remote-boundary-image-{run_id}"
    with tempfile.TemporaryDirectory(prefix="agentgov-remote-boundary-") as raw_root:
        root = Path(raw_root)
        with _ephemeral_ssh(root) as shell:
            _verify_exclusive_lock(shell, root / "deploy.lock")
            compose_file = root / "compose.yml"
            try:
                images = _prepare_images(shell, root, source, run_id)
                _exercise_primitives(shell, images, compose_file, project, _available_port())
                return _evidence(shell, images)
            finally:
                with suppress(BoundaryAcceptanceError):
                    _compose(shell, project, compose_file, "down", "--remove-orphans", "--volumes")
                with suppress(BoundaryAcceptanceError):
                    shell.run(["docker", "container", "rm", "--force", variant_container])
                for reference in (
                    f"agentgov-remote-boundary-old:{run_id}",
                    f"agentgov-remote-boundary-candidate:{run_id}",
                ):
                    with suppress(BoundaryAcceptanceError):
                        shell.run(["docker", "image", "rm", reference])


def main() -> int:
    try:
        print(json.dumps(run(), sort_keys=True))
    except BoundaryAcceptanceError as exc:
        print(f"REMOTE_DEPLOY_BOUNDARY_NOT_PROVEN: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"REMOTE_DEPLOY_BOUNDARY_NOT_PROVEN: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
