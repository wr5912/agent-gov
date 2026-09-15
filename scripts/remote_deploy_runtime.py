#!/usr/bin/env python3
"""Validate, execute, and recover the remote Docker deployment transaction."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_atomic_cutover_archive import ImageArchiveError, validate_image_archive
from scripts.remote_deploy_transaction import (
    ArchiveIdentity,
    DeployTransaction,
    ImageIdentity,
    TransactionError,
    activate_transaction,
    begin_transaction,
    finalize_rollback,
    finalize_success,
    load_transaction,
    mark_recovery_required,
    rollback_source,
    transition,
)

_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_TRUSTED_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PROJECT_NAMES: Final = ("agent-gov-agentscope-runtime", "agent-gov-api", "agent-gov-ui")
ProcessEnvironment = dict[str, str]


class RuntimeDeployError(RuntimeError):
    """The remote Docker deployment cannot proceed or recover deterministically."""


class DockerBinding(TypedDict):
    command: list[str]
    environment: dict[str, str]


def _sha256(path: Path) -> str:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeDeployError("镜像归档必须是普通非符号链接文件")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _trusted_regular_file(candidates: Sequence[Path], label: str) -> Path:
    for candidate in candidates:
        if not candidate.is_file() or candidate.is_symlink():
            continue
        metadata = candidate.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise RuntimeDeployError(f"trusted {label} owner/mode 无效")
        return candidate
    raise RuntimeDeployError(f"trusted {label} 不可用")


@contextmanager
def _bound_docker(home: Path) -> Iterator[DockerBinding]:
    source_cli = _trusted_regular_file((Path("/usr/bin/docker"), Path("/usr/local/bin/docker")), "Docker CLI")
    source_plugin = _trusted_regular_file(
        tuple(
            Path(path)
            for path in (
                "/usr/local/lib/docker/cli-plugins/docker-compose",
                "/usr/local/libexec/docker/cli-plugins/docker-compose",
                "/usr/lib/docker/cli-plugins/docker-compose",
                "/usr/libexec/docker/cli-plugins/docker-compose",
            )
        ),
        "Compose plugin",
    )
    cli_digest, plugin_digest = _sha256(source_cli), _sha256(source_plugin)
    with tempfile.TemporaryDirectory(prefix="agentgov-remote-docker-") as raw_boundary:
        boundary = Path(raw_boundary)
        docker_config = boundary / "config"
        plugin_directory = docker_config / "cli-plugins"
        plugin_directory.mkdir(mode=0o700, parents=True)
        bound_cli, bound_plugin = boundary / "docker", plugin_directory / "docker-compose"
        shutil.copyfile(source_cli, bound_cli)
        shutil.copyfile(source_plugin, bound_plugin)
        bound_cli.chmod(0o500)
        bound_plugin.chmod(0o500)
        if _sha256(source_cli) != cli_digest or _sha256(bound_cli) != cli_digest:
            raise RuntimeDeployError("Docker CLI 在绑定期间变化")
        if _sha256(source_plugin) != plugin_digest or _sha256(bound_plugin) != plugin_digest:
            raise RuntimeDeployError("Compose plugin 在绑定期间变化")
        environment = {
            "HOME": home.as_posix(),
            "DOCKER_CONFIG": docker_config.as_posix(),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "PATH": f"{boundary}:{_TRUSTED_PATH}",
        }
        yield DockerBinding(command=[bound_cli.as_posix()], environment=environment)
        if _sha256(bound_cli) != cli_digest or _sha256(bound_plugin) != plugin_digest:
            raise RuntimeDeployError("绑定的 Docker CLI/plugin 在部署期间变化")


def _docker_output(binding: DockerBinding, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        [*binding["command"], *arguments],
        env=binding["environment"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeDeployError("远端 Docker 只读核验失败")
    return result.stdout.strip()


def _normalized_architecture(value: str) -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(value, value)


def _project_references(version: str) -> tuple[str, ...]:
    if not version or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_.-]*", version):
        raise RuntimeDeployError("部署版本无效")
    return tuple(f"{name}:{version}" for name in _PROJECT_NAMES)


def _archive_identity(path: Path) -> ArchiveIdentity:
    expected_path = path.with_name(f"{path.name}.sha256")
    try:
        record = expected_path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise RuntimeDeployError("镜像归档 checksum 缺失") from exc
    digest = _sha256(path)
    if not record or _SHA256.fullmatch(record[0]) is None or record[0] != digest:
        raise RuntimeDeployError("镜像归档 checksum 不匹配")
    return ArchiveIdentity(path.resolve(strict=True).as_posix(), digest)


def _validate_archives(
    *,
    project: ArchiveIdentity,
    dependency: ArchiveIdentity | None,
    architecture: str,
    version: str,
    source_digest: str,
) -> None:
    project_path = Path(project.path)
    if _sha256(project_path) != project.sha256:
        raise RuntimeDeployError("项目镜像归档在事务期间变化")
    validate_image_archive(
        project_path,
        architecture=architecture,
        expected_images=frozenset(_project_references(version)),
        source_digest=source_digest,
    )
    if dependency is not None:
        dependency_path = Path(dependency.path)
        if _sha256(dependency_path) != dependency.sha256:
            raise RuntimeDeployError("依赖镜像归档在事务期间变化")
        validate_image_archive(dependency_path, architecture=architecture, forbidden_prefix="agent-gov-")


def _inspect_image(binding: DockerBinding, reference: str) -> str | None:
    result = subprocess.run(
        [*binding["command"], "image", "inspect", reference, "--format", "{{.Id}}"],
        env=binding["environment"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        missing = result.returncode == 1 and any(
            marker in result.stderr
            for marker in (f"No such image: {reference}", f"No such object: {reference}")
        )
        if missing:
            return None
        raise RuntimeDeployError("远端镜像 inspect 执行失败，不能判定为 fresh host")
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:"):
        raise RuntimeDeployError("远端镜像 identity 无效")
    return image_id


def _compose_project(live_root: Path) -> str:
    env_file = live_root / "docker/.env"
    if env_file.is_symlink():
        raise RuntimeDeployError("既有 Compose env identity 无效")
    values = _env_values(env_file, frozenset({"COMPOSE_PROJECT_NAME"})) if env_file.is_file() else {}
    project = values.get("COMPOSE_PROJECT_NAME", "agent-gov")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) is None:
        raise RuntimeDeployError("既有 Compose project name 无效")
    return project


def _assert_no_prior_runtime(binding: DockerBinding, live_root: Path) -> None:
    project_filter = f"label=com.docker.compose.project={_compose_project(live_root)}"
    inventories = (
        ("ps", "-aq", "--filter", project_filter),
        ("network", "ls", "-q", "--filter", project_filter),
        ("volume", "ls", "-q", "--filter", project_filter),
    )
    if any(_docker_output(binding, arguments) for arguments in inventories):
        raise RuntimeDeployError("旧镜像 tag 缺失但仍存在既有 Compose/runtime 资源")


def _save_old_images(binding: DockerBinding, references: tuple[str, ...], destination: Path) -> tuple[ImageIdentity, ...]:
    identities = tuple(ImageIdentity(reference, image_id) for reference in references if (image_id := _inspect_image(binding, reference)))
    if identities and len(identities) != len(references):
        raise RuntimeDeployError("既有部署的项目镜像集合不完整")
    if not identities:
        return ()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = destination.with_name(f".{destination.name}.tmp")
    process = subprocess.Popen(
        [*binding["command"], "save", *references],
        env=binding["environment"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        with temporary.open("xb") as raw_output, gzip.GzipFile(fileobj=raw_output, mode="wb", compresslevel=1, mtime=0) as compressed:
            shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
        if process.wait() != 0:
            raise RuntimeDeployError("旧项目镜像归档失败")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)
    return identities


def _source_digest(root: Path) -> str | None:
    if not (root / "VERSION").is_file() or not (root / "scripts/agentscope_atomic_cutover_bootstrap.py").is_file():
        return None
    root_text = root.as_posix()
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256

        return source_artifact_sha256(root)
    except (OSError, RuntimeError, ValueError):
        return None


def _env_values(path: Path, keys: frozenset[str]) -> ProcessEnvironment:
    values: ProcessEnvironment = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeDeployError("部署私有 env 不可读") from exc
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        left, value = raw.split("=", 1)
        key = left.removeprefix("export ").strip()
        if key in keys:
            values[key] = value.strip().strip("\"'")
    return values


def _local_langfuse(env_file: Path) -> bool:
    values = _env_values(env_file, frozenset({"LANGFUSE_ENABLED", "LANGFUSE_BASE_URL"}))
    enabled = values.get("LANGFUSE_ENABLED", "false").casefold() in {"true", "1", "yes", "on", "t", "y"}
    return enabled and values.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000").rstrip("/") == "http://langfuse-web:3000"


def prepare_transaction(
    *,
    live_root: Path,
    stage_root: Path,
    backup_root: Path,
    toolchain_root: Path,
    transaction_id: str,
    version: str,
    source_digest: str,
    with_langfuse: bool,
) -> DeployTransaction:
    """Validate all archives and snapshot the old images before source activation."""
    project_path = live_root / f"images/agent-gov-{version}-images.tar.gz"
    dependency_path = live_root / f"images/agent-gov-{version}-langfuse-deps-images.tar.gz"
    project = _archive_identity(project_path)
    dependency = _archive_identity(dependency_path) if with_langfuse else None
    with _bound_docker(Path(os.environ.get("HOME", live_root.as_posix()))) as binding:
        architecture = _normalized_architecture(_docker_output(binding, ("info", "--format", "{{.Architecture}}")))
        _validate_archives(
            project=project,
            dependency=dependency,
            architecture=architecture,
            version=version,
            source_digest=source_digest,
        )
        old_version = (live_root / "VERSION").read_text(encoding="utf-8").strip() if (live_root / "VERSION").is_file() else None
        old_references = _project_references(old_version) if old_version else ()
        recovery_archive = live_root.parent / ".agentgov-deploy-recovery" / transaction_id / "old-project-images.tar.gz"
        old_images = _save_old_images(binding, old_references, recovery_archive) if old_references else ()
        if not old_images:
            _assert_no_prior_runtime(binding, live_root)
        old_archive = ArchiveIdentity(recovery_archive.as_posix(), _sha256(recovery_archive)) if old_images else None
    old_env = live_root / "docker/.env"
    return begin_transaction(
        transaction_id=transaction_id,
        live_root=live_root,
        stage_root=stage_root,
        backup_root=backup_root,
        toolchain_root=toolchain_root,
        source_sha256=source_digest,
        version=version,
        with_langfuse=with_langfuse,
        project_archive=project,
        dependency_archive=dependency,
        old_version=old_version,
        old_source_sha256=_source_digest(live_root),
        old_with_langfuse=_local_langfuse(old_env) if old_env.is_file() else False,
        old_images=old_images,
        old_archive=old_archive,
    )


def _compose_operation(state: DeployTransaction, operation: str, *, old: bool = False) -> None:
    source_root = Path(state.live_root)
    env_file = source_root / "docker/.env"
    compose_files = [source_root / "docker/docker-compose.yml"]
    with_langfuse = state.old_with_langfuse if old else state.with_langfuse
    if with_langfuse:
        compose_files.append(source_root / "docker/docker-compose.langfuse.yml")
    for path in (env_file, *compose_files):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeDeployError("冻结恢复所需 Compose/env 边界缺失") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeDeployError("冻结恢复所需 Compose/env 边界无效")
    version = state.old_version if old else state.version
    if version is None:
        raise RuntimeDeployError("恢复 Compose 缺少旧版本 identity")
    with _bound_docker(Path(os.environ.get("HOME", source_root.as_posix()))) as binding:
        command = [*binding["command"], "compose", "--env-file", env_file.as_posix()]
        for compose_file in compose_files:
            command.extend(("--file", compose_file.as_posix()))
        if with_langfuse:
            command.extend(("--profile", "langfuse"))
        if operation == "down":
            command.extend(("down", "--remove-orphans"))
        elif operation == "up":
            command.extend(("up", "-d", "--wait", "--remove-orphans", "--pull", "never", "--no-build", "--force-recreate"))
        else:
            raise RuntimeDeployError("冻结 Compose operation 无效")
        environment = dict(binding["environment"])
        environment["APP_VERSION"] = version
        result = subprocess.run(
            command,
            cwd=source_root,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeDeployError(f"冻结 Compose {operation} 失败")


def _load_archive(binding: DockerBinding, identity: ArchiveIdentity) -> None:
    if _sha256(Path(identity.path)) != identity.sha256:
        raise RuntimeDeployError("待加载镜像归档摘要漂移")
    process = subprocess.Popen(
        [*binding["command"], "load"],
        env=binding["environment"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None
    try:
        with gzip.open(identity.path, "rb") as source:
            shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeDeployError("远端 docker load 失败")
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _verify_loaded_project(binding: DockerBinding, state: DeployTransaction) -> None:
    for reference in _project_references(state.version):
        result = _docker_output(binding, ("image", "inspect", reference, "--format", '{{ index .Config.Labels "io.agentgov.source-artifact-sha256" }}'))
        if result != state.source_sha256:
            raise RuntimeDeployError("loaded project image source identity 不匹配")


def _port(values: Mapping[str, str], key: str, default: int) -> int:
    raw = values.get(key, str(default))
    if not raw.isdigit() or not 50400 <= int(raw) <= 50499:
        raise RuntimeDeployError(f"{key} 不在 50400-50499")
    return int(raw)


def _health(state: DeployTransaction, *, old: bool = False) -> None:
    env_file = Path(state.live_root) / "docker/.env"
    values = _env_values(env_file, frozenset({"HOST_PORT", "FRONTEND_HOST_PORT", "LANGFUSE_HOST_PORT"}))
    checks = [
        ("api", f"http://127.0.0.1:{_port(values, 'HOST_PORT', 50400)}/health/ready", 60),
        ("ui", f"http://127.0.0.1:{_port(values, 'FRONTEND_HOST_PORT', 50401)}", 60),
    ]
    with_langfuse = state.old_with_langfuse if old else state.with_langfuse
    if with_langfuse:
        checks.append(("langfuse", f"http://127.0.0.1:{_port(values, 'LANGFUSE_HOST_PORT', 50402)}", 90))
    opener = build_opener(ProxyHandler({}))
    for label, url, attempts in checks:
        last_error = "unavailable"
        for _attempt in range(attempts):
            try:
                request = Request(url, headers={"User-Agent": "agent-gov-remote-deploy"})
                with opener.open(request, timeout=5) as response:
                    response.read(1)
                    if 200 <= response.status < 400:
                        break
                    last_error = f"http-{response.status}"
            except HTTPError as exc:
                last_error = f"http-{exc.code}"
            except (OSError, TimeoutError, URLError):
                last_error = "transport"
            time.sleep(2)
        else:
            raise RuntimeDeployError(f"{label} health 失败: {last_error}")


def execute_transaction(live_root: Path) -> DeployTransaction:
    """Load the candidate images, recreate Compose, and prove health."""
    state = load_transaction(live_root)
    if state.phase != "activated":
        raise RuntimeDeployError("部署事务尚未完成 source 激活")
    try:
        with _bound_docker(Path(os.environ.get("HOME", live_root.as_posix()))) as binding:
            architecture = _normalized_architecture(_docker_output(binding, ("info", "--format", "{{.Architecture}}")))
            _validate_archives(
                project=state.project_archive,
                dependency=state.dependency_archive,
                architecture=architecture,
                version=state.version,
                source_digest=state.source_sha256,
            )
            state = transition(state, "images-loading")
            _load_archive(binding, state.project_archive)
            if state.dependency_archive is not None:
                _load_archive(binding, state.dependency_archive)
            state = transition(state, "images-loaded")
            _verify_loaded_project(binding, state)
        state = transition(state, "compose-starting")
        _compose_operation(state, "up")
        state = transition(state, "compose-recreated")
        state = transition(state, "health-checking")
        _health(state)
        return transition(state, "healthy")
    except (ImageArchiveError, OSError, RuntimeDeployError, subprocess.SubprocessError, TransactionError, ValueError) as exc:
        try:
            mark_recovery_required(live_root, "candidate-deploy-failed")
        except (OSError, TransactionError) as state_exc:
            raise RuntimeDeployError("部署失败且恢复状态无法持久化") from state_exc
        raise RuntimeDeployError("候选部署失败；必须执行持久恢复事务") from exc


def _restore_old_images(binding: DockerBinding, state: DeployTransaction, architecture: str) -> None:
    if not state.old_images or state.old_archive is None:
        return
    expected = frozenset(identity.reference for identity in state.old_images)
    if _sha256(Path(state.old_archive.path)) != state.old_archive.sha256:
        raise RuntimeDeployError("旧镜像恢复归档摘要漂移")
    validate_image_archive(
        Path(state.old_archive.path),
        architecture=architecture,
        expected_images=expected,
        source_digest=state.old_source_sha256,
    )
    _load_archive(binding, state.old_archive)
    for identity in state.old_images:
        if _inspect_image(binding, identity.reference) != identity.image_id:
            raise RuntimeDeployError("旧镜像 identity 恢复失败")


def recover_transaction(live_root: Path) -> str:
    """Idempotently finish healthy commit or restore old source/images/Compose."""
    state = load_transaction(live_root)
    if state.phase in {"healthy", "finalizing-success"}:
        if state.phase == "healthy":
            _health(state)
        finalize_success(live_root)
        return "committed"
    if state.phase in {"rolled-back", "finalizing-rollback"}:
        finalize_rollback(live_root)
        return "rolled-back"
    try:
        with _bound_docker(Path(os.environ.get("HOME", live_root.as_posix()))) as binding:
            architecture = _normalized_architecture(_docker_output(binding, ("info", "--format", "{{.Architecture}}")))
            _restore_old_images(binding, state, architecture)
        if not state.old_images and state.phase not in {"prepared", "live-moved", "candidate-moved", "preserved-moving"}:
            _compose_operation(state, "down")
        state = rollback_source(live_root)
        if state.old_images:
            state = transition(state, "rollback-compose-starting")
            _compose_operation(state, "up", old=True)
            _health(state, old=True)
            state = transition(state, "rolled-back")
        else:
            state = transition(state, "rolled-back")
        finalize_rollback(live_root)
        return "rolled-back"
    except (ImageArchiveError, OSError, RuntimeDeployError, subprocess.SubprocessError, TransactionError, ValueError) as exc:
        try:
            mark_recovery_required(live_root, "rollback-failed")
        except (OSError, TransactionError) as state_exc:
            raise RuntimeDeployError("恢复失败且事务状态无法持久化") from state_exc
        raise RuntimeDeployError("恢复未完成；事务保持 fail closed，可重复执行 recover") from exc


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    for command in (prepare,):
        command.add_argument("--live-root", type=Path, required=True)
        command.add_argument("--stage-root", type=Path, required=True)
        command.add_argument("--backup-root", type=Path, required=True)
        command.add_argument("--toolchain-root", type=Path, required=True)
        command.add_argument("--transaction-id", required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--source-sha256", required=True)
        command.add_argument("--with-langfuse", choices=("0", "1"), required=True)
    for name in ("activate", "execute", "commit", "recover", "status"):
        command = commands.add_parser(name)
        command.add_argument("--live-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_transaction(
                live_root=args.live_root,
                stage_root=args.stage_root,
                backup_root=args.backup_root,
                toolchain_root=args.toolchain_root,
                transaction_id=args.transaction_id,
                version=args.version,
                source_digest=args.source_sha256,
                with_langfuse=args.with_langfuse == "1",
            )
            print("prepared")
        elif args.command == "activate":
            activate_transaction(args.live_root)
            print("activated")
        elif args.command == "execute":
            execute_transaction(args.live_root)
            print("healthy")
        elif args.command == "commit":
            finalize_success(args.live_root)
            print("committed")
        elif args.command == "recover":
            print(recover_transaction(args.live_root))
        else:
            state = load_transaction(args.live_root)
            print(json.dumps({"phase": state.phase, "transaction_id": state.transaction_id}, sort_keys=True))
    except (ImageArchiveError, OSError, RuntimeDeployError, subprocess.SubprocessError, TransactionError, ValueError):
        parser.exit(1, "远端部署事务命令失败；未输出私有 env 或工具输入。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
