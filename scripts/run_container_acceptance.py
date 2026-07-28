#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker/docker-compose.yml"
LOCK_FILE = Path(f"/tmp/agentgov-container-acceptance-{os.getuid()}.lock")
ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
RUN_ID_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUN_ID"
PROFILE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE"
LABEL_KEY: Final = "io.agentgov.acceptance-run-id"

CORE_SERVICES = ("agent-gov-litellm-sidecar", "claude-agent-api", "claude-agent-ui")
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
    delegated_refresh: bool = False


PROFILES = {
    "core": AcceptanceProfile("core", (), CORE_SERVICES, CORE_SERVICES),
    "langfuse": AcceptanceProfile(
        "langfuse",
        ("langfuse",),
        CORE_SERVICES,
        (*CORE_SERVICES, *LANGFUSE_SERVICES),
    ),
    "isolated-health": AcceptanceProfile("isolated-health", (), (), (), delegated_refresh=True),
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
    raw = requested or Path(environ.get("COMPOSE_ENV_FILE", "docker/.env"))
    selected = raw if raw.is_absolute() else REPO_ROOT / raw
    selected = selected.resolve()
    default_env = (REPO_ROOT / "docker/.env").resolve()
    if profile.delegated_refresh and selected == default_env and not selected.is_file():
        selected = (REPO_ROOT / "docker/.env.example").resolve()
    if not selected.is_file():
        raise AcceptanceError("所选 Compose env 文件不存在")
    return selected


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
    env_file: Path,
    run_id: str,
    environ: dict[str, str],
) -> dict[str, str]:
    child_env = dict(environ)
    child_env.update(
        {
            ACTIVE_ENV: "1",
            RUN_ID_ENV: run_id,
            PROFILE_ENV: profile.name,
            "COMPOSE_ENV_FILE": str(env_file),
            "AGENT_GOV_COMPOSE_ENV_FILE": str(env_file),
        }
    )
    child_env["APP_VERSION"] = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
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


def refresh_profile(profile: AcceptanceProfile, env_file: Path, env: dict[str, str]) -> None:
    base = compose_command(profile, env_file)
    _validate_service_model(base, profile, env)
    _run_checked([*base, "build", *profile.build_services], env=env, label="Compose 镜像重建")
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
        child_env = build_acceptance_env(profile, env_file, run_id, environ)
        print(f"CONTAINER_ACCEPTANCE_REFRESH profile={profile.name} run_id={run_id}")
        if not profile.delegated_refresh:
            refresh_profile(profile, env_file, child_env)
            if source_fingerprint(env_file) != initial_fingerprint:
                raise AcceptanceError("构建或 recreate 期间工作树/env 已变化")
        returncode = _run_child(command, child_env)
        if source_fingerprint(env_file) != initial_fingerprint:
            raise AcceptanceError("容器验收期间工作树/env 已变化，结果无效")
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
