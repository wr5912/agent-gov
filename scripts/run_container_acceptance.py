#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.yml"
LANGFUSE_COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.langfuse.yml"
LOCK_FILE = Path(f"/tmp/agentgov-container-acceptance-{os.getuid()}.lock")
ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
RUN_ID_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUN_ID"
PROFILE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE"
LABEL_KEY: Final = "io.agentgov.acceptance-run-id"
ISOLATED_MOUNT_PATHS: Final = {
    "AGENTGOV_API_GATE_STATE_DIR_HOST": "api-gate",
    "HOST_DATA_MOUNT": "data",
    "HOST_GOVERNOR_WORKSPACE_MOUNT": "governor-workspace",
    "HOST_AGENTSCOPE_RUNTIME_CANDIDATES_MOUNT": "agentscope-runtime/candidates",
    "HOST_AGENTSCOPE_RUNTIME_DATA_MOUNT": "agentscope-runtime/data",
    "HOST_AGENTSCOPE_RUNTIME_WORKSPACES_MOUNT": "agentscope-runtime/workspaces",
    "LANGFUSE_POSTGRES_DATA_MOUNT": "langfuse/postgres",
    "LANGFUSE_CLICKHOUSE_DATA_MOUNT": "langfuse/clickhouse/data",
    "LANGFUSE_CLICKHOUSE_LOGS_MOUNT": "langfuse/clickhouse/logs",
    "LANGFUSE_REDIS_DATA_MOUNT": "langfuse/redis",
    "LANGFUSE_MINIO_DATA_MOUNT": "langfuse/minio",
}

CORE_SERVICES = ("agentscope-runtime", "agent-gov-api", "agent-gov-ui")
LANGFUSE_SERVICES = (
    "langfuse-postgres",
    "langfuse-clickhouse",
    "langfuse-redis",
    "langfuse-minio",
    "langfuse-web",
    "langfuse-worker",
)


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


@dataclass(frozen=True)
class AcceptanceProfile:
    name: str
    compose_profiles: tuple[str, ...]
    build_services: tuple[str, ...]
    expected_services: tuple[str, ...]


@dataclass(frozen=True)
class IsolatedEnvironment:
    env_file: Path
    runtime_root: Path
    project_name: str
    container_prefix: str
    overrides: dict[str, str]


PROFILES = {
    "core": AcceptanceProfile("core", (), CORE_SERVICES, CORE_SERVICES),
    "langfuse": AcceptanceProfile(
        "langfuse",
        ("langfuse",),
        CORE_SERVICES,
        (*CORE_SERVICES, *LANGFUSE_SERVICES),
    ),
}


class AcceptanceError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="刷新 Docker Compose 运行态后执行真实容器验收。")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("必须在 `--` 后提供验收命令")
    return args


def resolve_env_file(profile: AcceptanceProfile, requested: Path | None, environ: dict[str, str]) -> Path:
    del profile
    raw = requested or Path(environ.get("COMPOSE_ENV_FILE", "docker/.env"))
    selected = raw if raw.is_absolute() else REPO_ROOT / raw
    selected = selected.resolve()
    if not selected.is_file():
        raise AcceptanceError("所选 Compose env 文件不存在")
    return selected


def _allocate_loopback_ports(count: int) -> tuple[int, ...]:
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for port in range(50400, 50500):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as exc:
                listener.close()
                if exc.errno == errno.EADDRINUSE:
                    continue
                raise AcceptanceError("无法检查 50400–50499 范围内的验收端口") from exc
            sockets.append(listener)
            ports.append(port)
            if len(ports) == count:
                return tuple(ports)
        raise AcceptanceError(f"50400–50499 范围内可用验收端口不足：需要 {count} 个，仅找到 {len(ports)} 个")
    finally:
        for listener in sockets:
            listener.close()


def _compose_mount_variables() -> set[str]:
    compose_text = "\n".join(path.read_text(encoding="utf-8") for path in (COMPOSE_FILE, LANGFUSE_COMPOSE_FILE))
    variables = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", compose_text))
    return {name for name in variables if name.endswith("_MOUNT") or name == "AGENTGOV_API_GATE_STATE_DIR_HOST"}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def prepare_isolated_environment(source_env: Path, run_id: str, temp_dir: Path) -> IsolatedEnvironment:
    runtime_root = (temp_dir / "runtime-root").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    api_port, frontend_port, langfuse_port, minio_port, minio_console_port = _allocate_loopback_ports(5)
    token = run_id.rsplit("-", 1)[-1]
    api_key = secrets.token_hex(32)
    project_name = f"agv-acceptance-{os.getuid()}-{token}"
    container_prefix = project_name
    overrides = {
        "APP_VERSION": f"acceptance-{token}",
        "COMPOSE_PROJECT_NAME": project_name,
        "CONTAINER_NAME_PREFIX": container_prefix,
        "API_KEY": api_key,
        "FRONTEND_RUNTIME_API_KEY": api_key,
        "HOST_RUNTIME_VOLUME_ROOT": runtime_root.as_posix(),
        "RUNTIME_BOOTSTRAP_HOST_DIR": (REPO_ROOT / "docker/runtime-bootstrap").resolve().as_posix(),
        "API_BIND_IP": "127.0.0.1",
        "API_ALLOW_PUBLIC_BIND": "0",
        "HOST_PORT": str(api_port),
        "FRONTEND_BIND_IP": "127.0.0.1",
        "FRONTEND_ALLOW_PUBLIC_BIND": "0",
        "FRONTEND_HOST_PORT": str(frontend_port),
        "FRONTEND_RUNTIME_API_BASE": f"http://127.0.0.1:{api_port}",
        "API_BASE": f"http://127.0.0.1:{api_port}",
        "AGENTGOV_API_MODE": "open",
        "AGENTGOV_ACCEPTANCE_IDENTITY": "",
        "AGENTGOV_ACCEPTANCE_API_KEY": "",
        "AGENTGOV_API_GATE_STATE_FILE": "",
        "FRONTEND_URL": f"http://127.0.0.1:{frontend_port}",
        "LANGFUSE_BIND_IP": "127.0.0.1",
        "LANGFUSE_ALLOW_PUBLIC_BIND": "0",
        "LANGFUSE_HOST_PORT": str(langfuse_port),
        "LANGFUSE_NEXTAUTH_URL": f"http://127.0.0.1:{langfuse_port}",
        "FRONTEND_LANGFUSE_URL": f"http://127.0.0.1:{langfuse_port}",
        "LANGFUSE_MINIO_HOST_PORT": str(minio_port),
        "LANGFUSE_MINIO_CONSOLE_HOST_PORT": str(minio_console_port),
        "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT": f"http://127.0.0.1:{minio_port}",
        "LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT": f"http://127.0.0.1:{minio_port}",
        "AGENT_GOV_RUNTIME_UID": str(os.getuid()),
        "AGENT_GOV_RUNTIME_GID": str(os.getgid()),
    }
    for name, relative in ISOLATED_MOUNT_PATHS.items():
        path = (runtime_root / relative).resolve()
        if not _is_relative_to(path, runtime_root):
            raise AcceptanceError(f"隔离挂载路径逃逸: {name}")
        path.mkdir(parents=True, exist_ok=True)
        overrides[name] = path.as_posix()

    gate_state = runtime_root / "api-gate/api-gate-state.json"
    gate_state.write_bytes((REPO_ROOT / "docker/api-gate/api-gate-state.json").read_bytes())
    gate_state.chmod(0o600)

    missing = _compose_mount_variables() - set(overrides)
    if missing:
        raise AcceptanceError("Compose 新增了未隔离的宿主挂载变量: " + ", ".join(sorted(missing)))

    isolated_env_file = temp_dir / "compose.acceptance.env"
    _write_isolated_env(source_env, isolated_env_file, overrides)
    return IsolatedEnvironment(
        env_file=isolated_env_file,
        runtime_root=runtime_root,
        project_name=project_name,
        container_prefix=container_prefix,
        overrides=overrides,
    )


def _write_isolated_env(source: Path, target: Path, overrides: dict[str, str]) -> None:
    # 每个隔离键只保留一个定义，让 Compose、dotenv 和 shell 消费者取得相同值。
    lines = [line for line in source.read_text(encoding="utf-8").splitlines() if line.split("=", 1)[0].strip().removeprefix("export ").strip() not in overrides]
    lines.extend(["", "# 本轮隔离验收配置；不得复用为正式部署配置。"])
    lines.extend(f"{key}={value}" for key, value in sorted(overrides.items()))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)


def _tracked_and_untracked_paths() -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AcceptanceError("无法枚举当前工作树") from exc
    if result.returncode:
        raise AcceptanceError("无法枚举当前工作树")
    decoded = (os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
    return tuple(sorted((REPO_ROOT / item for item in decoded), key=lambda path: path.as_posix()))


def _update_path_digest(digest: _Digest, path: Path) -> None:
    relative = path.relative_to(REPO_ROOT).as_posix().encode("utf-8", errors="surrogateescape")
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"MISSING\0")
        return
    digest.update(metadata.st_mode.to_bytes(8, "big"))
    if path.is_symlink():
        digest.update(b"SYMLINK\0")
        digest.update(os.fsencode(os.readlink(path)))
        return
    if not path.is_file():
        digest.update(b"NON_FILE\0")
        return
    digest.update(b"FILE\0")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


def source_fingerprint(env_file: Path) -> str:
    digest = hashlib.sha256()
    for path in _tracked_and_untracked_paths():
        _update_path_digest(digest, path)
    digest.update(b"\0SELECTED_ENV\0")
    with env_file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_acceptance_env(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    run_id: str,
    environ: dict[str, str],
) -> dict[str, str]:
    child_env = dict(environ)
    child_env.update(isolation.overrides)
    child_env.update(
        {
            ACTIVE_ENV: "1",
            RUN_ID_ENV: run_id,
            PROFILE_ENV: profile.name,
            "LANGFUSE_ENABLED": "true" if profile.name == "langfuse" else "false",
            "COMPOSE_ENV_FILE": str(isolation.env_file),
            "AGENT_GOV_COMPOSE_ENV_FILE": str(isolation.env_file),
        }
    )
    return child_env


def compose_command(profile: AcceptanceProfile, env_file: Path) -> list[str]:
    command = [
        "docker",
        "compose",
        "--parallel",
        "3",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILE),
    ]
    if "langfuse" in profile.compose_profiles:
        command.extend(["-f", str(LANGFUSE_COMPOSE_FILE)])
    for compose_profile in profile.compose_profiles:
        command.extend(["--profile", compose_profile])
    return command


def _run_checked(command: list[str], *, env: dict[str, str], label: str, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=capture,
            text=capture,
        )
    except OSError as exc:
        raise AcceptanceError(f"{label}无法启动，验收命令未执行") from exc
    if result.returncode:
        raise AcceptanceError(f"{label}失败，验收命令未执行")
    return result.stdout.strip() if capture else ""


def _validate_service_model(base: list[str], profile: AcceptanceProfile, env: dict[str, str]) -> None:
    output = _run_checked([*base, "config", "--services"], env=env, label="Compose 服务解析", capture=True)
    if set(output.splitlines()) != set(profile.expected_services):
        raise AcceptanceError("Compose 服务集合与验收 profile 不一致")


def _validate_isolated_mounts(
    base: list[str],
    isolation: IsolatedEnvironment,
    env: dict[str, str],
) -> None:
    raw = _run_checked([*base, "config", "--format", "json"], env=env, label="Compose 隔离挂载解析", capture=True)
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("Compose 隔离挂载解析未返回 JSON") from exc
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        raise AcceptanceError("Compose 隔离挂载解析缺少 services")
    bootstrap = (REPO_ROOT / "docker/runtime-bootstrap").resolve()
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        volumes = service.get("volumes")
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            raw_source = volume.get("source")
            if not isinstance(raw_source, str) or not raw_source:
                raise AcceptanceError(f"服务 {service_name} 存在无法审计的 bind source")
            source = Path(raw_source).resolve()
            if source == bootstrap:
                if not volume.get("read_only"):
                    raise AcceptanceError("Runtime bootstrap 在验收容器中必须只读")
                continue
            if not _is_relative_to(source, isolation.runtime_root):
                raise AcceptanceError(f"服务 {service_name} 的 bind mount 未隔离到临时根")


def _bootstrap_isolated_runtime(isolation: IsolatedEnvironment, env: dict[str, str]) -> None:
    _run_checked(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/bootstrap_runtime_volume.py"),
            "--env-file",
            str(isolation.env_file),
            "--runtime-root",
            str(isolation.runtime_root),
            "--quiet",
        ],
        env=env,
        label="隔离 Runtime 初始化",
    )


def _parse_created(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    date_part, dot, remainder = normalized.partition(".")
    if not dot:
        return datetime.fromisoformat(normalized)
    fraction, offset_sign, offset = remainder.partition("+")
    sign = "+"
    if not offset_sign:
        fraction, offset_sign, offset = remainder.partition("-")
        sign = "-"
    suffix = f"{sign}{offset}" if offset_sign else ""
    return datetime.fromisoformat(f"{date_part}.{fraction[:6]}{suffix}")


def _inspect_value(arguments: list[str], *, env: dict[str, str], label: str) -> str:
    return _run_checked(["docker", "inspect", "--format", *arguments], env=env, label=label, capture=True)


def _verify_container(
    base: list[str],
    service: str,
    run_id: str,
    started_at: datetime,
    env: dict[str, str],
    *,
    check_image: bool,
) -> None:
    container_id = _run_checked([*base, "ps", "-q", service], env=env, label="容器定位", capture=True)
    if not container_id:
        raise AcceptanceError(f"服务 {service} 没有运行容器")
    running = _inspect_value(["{{.State.Running}}", container_id], env=env, label="容器状态检查")
    container_label = _inspect_value(
        [f'{{{{index .Config.Labels "{LABEL_KEY}"}}}}', container_id],
        env=env,
        label="容器 freshness 检查",
    )
    created = _inspect_value(["{{.Created}}", container_id], env=env, label="容器创建时间检查")
    if running != "true" or container_label != run_id:
        raise AcceptanceError(f"服务 {service} 未加载本轮容器配置")
    if _parse_created(created) < started_at - timedelta(seconds=2):
        raise AcceptanceError(f"服务 {service} 没有在本轮 recreate")
    if check_image:
        image_id = _inspect_value(["{{.Image}}", container_id], env=env, label="镜像定位")
        image_label = _inspect_value(
            [f'{{{{index .Config.Labels "{LABEL_KEY}"}}}}', image_id],
            env=env,
            label="镜像 freshness 检查",
        )
        if image_label != run_id:
            raise AcceptanceError(f"服务 {service} 未使用本轮构建镜像")


def refresh_profile(profile: AcceptanceProfile, isolation: IsolatedEnvironment, env: dict[str, str]) -> None:
    base = compose_command(profile, isolation.env_file)
    _validate_service_model(base, profile, env)
    _validate_isolated_mounts(base, isolation, env)
    if profile.name == "langfuse":
        _run_checked(
            [
                *base,
                "--profile",
                "langfuse-maintenance",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--pull",
                "missing",
                "langfuse-volume-init",
            ],
            env=env,
            label="隔离 Langfuse 卷初始化",
        )
    _run_checked([*base, "build", *profile.build_services], env=env, label="Compose 镜像重建")
    _run_checked(
        [
            *base,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--pull",
            "never",
            "--entrypoint",
            "python",
            "agent-gov-api",
            "-m",
            "app.runtime.published_harness_preparation",
        ],
        env=env,
        label="已发布 Harness 启动前快照准备",
    )
    started_at = datetime.now(timezone.utc)
    _run_checked(
        [
            *base,
            "up",
            "-d",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "180",
            "--remove-orphans",
            *profile.expected_services,
        ],
        env=env,
        label="Compose 服务 recreate",
    )
    local_services = set(profile.build_services)
    for service in profile.expected_services:
        _verify_container(base, service, env[RUN_ID_ENV], started_at, env, check_image=service in local_services)


def cleanup_profile(profile: AcceptanceProfile, isolation: IsolatedEnvironment, env: dict[str, str]) -> None:
    base = compose_command(profile, isolation.env_file)
    _run_checked(
        [*base, "down", "--volumes", "--remove-orphans", "--timeout", "15"],
        env=env,
        label="隔离 Compose 清理",
    )
    cleanup_runtime_root(base, isolation.runtime_root, env)


def _validate_cleanup_root(runtime_root: Path) -> None:
    parent = runtime_root.parent
    if (
        runtime_root.name != "runtime-root"
        or parent.parent != Path(tempfile.gettempdir()).resolve()
        or not parent.name.startswith(f"agentgov-acceptance-{os.getuid()}-")
        or parent.is_symlink()
        or runtime_root.is_symlink()
        or runtime_root.resolve() != runtime_root
        or parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
    ):
        raise AcceptanceError("拒绝清理未经确认的临时验收根目录")


def cleanup_runtime_root(base: list[str], runtime_root: Path, env: dict[str, str]) -> None:
    """仅在 Compose down 成功后回收本轮临时卷，不修改正式卷权限。"""
    _validate_cleanup_root(runtime_root)
    if not runtime_root.exists():
        return
    try:
        shutil.rmtree(runtime_root)
        return
    except PermissionError:
        pass
    raw = _run_checked([*base, "config", "--format", "json"], env=env, label="隔离清理镜像解析", capture=True)
    try:
        image = json.loads(raw)["services"]["agent-gov-api"]["image"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AcceptanceError("隔离清理缺少已构建 API 镜像") from exc
    if not isinstance(image, str) or not image:
        raise AcceptanceError("隔离清理缺少已构建 API 镜像")
    # 只修复目录所有权；不跟随链接、不更改文件或硬链接指向的内容。
    repair = (
        "import os,sys; uid,gid=map(int,sys.argv[1:]); "
        "root='/acceptance-runtime'; "
        "[(os.chown(path,uid,gid,follow_symlinks=False),os.chmod(path,0o700)) "
        "for path,dirs,files in os.walk(root,followlinks=False)]"
    )
    _run_checked(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={runtime_root},dst=/acceptance-runtime",
            "--entrypoint",
            "python",
            image,
            "-c",
            repair,
            str(os.getuid()),
            str(os.getgid()),
        ],
        env=env,
        label="隔离临时卷目录权限回收",
        capture=True,
    )
    try:
        shutil.rmtree(runtime_root)
    except OSError as exc:
        raise AcceptanceError("隔离临时卷回收失败，已保留临时目录供检查") from exc


def _run_child(command: list[str], env: dict[str, str]) -> int:
    try:
        return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode
    except OSError as exc:
        raise AcceptanceError("验收命令无法启动") from exc


def run_acceptance(profile: AcceptanceProfile, env_file: Path, command: list[str], environ: dict[str, str]) -> int:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        initial_fingerprint = source_fingerprint(env_file)
        run_id = f"{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(6)}"
        temp_root = Path(tempfile.mkdtemp(prefix=f"agentgov-acceptance-{os.getuid()}-"))
        try:
            isolation = prepare_isolated_environment(env_file, run_id, temp_root)
        except BaseException:
            try:
                shutil.rmtree(temp_root)
            except OSError:
                print("CONTAINER_ACCEPTANCE_CLEANUP_FAIL: 临时环境准备失败后的目录回收失败", file=sys.stderr)
            raise
        else:
            child_env = build_acceptance_env(profile, isolation, run_id, environ)
            print(f"CONTAINER_ACCEPTANCE_REFRESH profile={profile.name} run_id={run_id} project={isolation.project_name} isolated=true")
            operation_error: BaseException | None = None
            try:
                _bootstrap_isolated_runtime(isolation, child_env)
                refresh_profile(profile, isolation, child_env)
                if source_fingerprint(env_file) != initial_fingerprint:
                    raise AcceptanceError("构建或 recreate 期间工作树/源 env 已变化")
                returncode = _run_child(command, child_env)
                if source_fingerprint(env_file) != initial_fingerprint:
                    raise AcceptanceError("容器验收期间工作树/源 env 已变化，结果无效")
            except BaseException as exc:
                operation_error = exc
                raise
            finally:
                try:
                    cleanup_profile(profile, isolation, child_env)
                    shutil.rmtree(temp_root)
                except (AcceptanceError, OSError) as cleanup_error:
                    detail = str(cleanup_error) if isinstance(cleanup_error, AcceptanceError) else "隔离临时目录回收失败"
                    if operation_error is None:
                        raise AcceptanceError(detail) from cleanup_error
                    print(f"CONTAINER_ACCEPTANCE_CLEANUP_FAIL: {detail}", file=sys.stderr)
            if returncode == 0:
                print(f"CONTAINER_ACCEPTANCE_OK profile={profile.name} run_id={run_id}")
            return returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = PROFILES[args.profile]
    try:
        env_file = resolve_env_file(profile, args.env_file, os.environ)
        return run_acceptance(profile, env_file, args.command, os.environ)
    except AcceptanceError as exc:
        print(f"CONTAINER_ACCEPTANCE_FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("CONTAINER_ACCEPTANCE_FAIL: 无法读取当前工作树或所选配置", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
