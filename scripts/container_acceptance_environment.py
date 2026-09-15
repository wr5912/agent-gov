"""容器验收 profile、隔离配置和子进程环境构建。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_atomic_cutover_env import parse_selected_env_payload, read_stable_env_file
from scripts.container_acceptance_inputs import (
    ACCEPTANCE_CONTEXT_ENV,
    ACCEPTANCE_PROFILE_ENV,
    ACCEPTANCE_RUN_ID_ENV,
    ACCEPTANCE_TARGET_ENV,
    DEPLOYABLE_SOURCE_ROOT_ENV,
    LIVE_SOURCE_ROOT_ENV,
    SCENARIO_FILE_ENV_KEYS,
    AcceptanceError,
    ScenarioFileSnapshot,
    snapshot_environment,
)
from scripts.container_acceptance_toolchain import (
    BROWSER_ACCEPTANCE_TARGETS,
    SYSTEM_TOOL_PATHS,
    TRUSTED_USER_HOME,
    AcceptanceToolchain,
    capture_acceptance_toolchain,
    toolchain_environment,
)
from scripts.initialize_runtime_shared_secret import initialize_shared_secret

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
RUN_ID_ENV: Final = ACCEPTANCE_RUN_ID_ENV
PROFILE_ENV: Final = ACCEPTANCE_PROFILE_ENV
LOOPBACK_NO_PROXY: Final = "localhost,127.0.0.1,::1"
PROXY_ENV_KEYS: Final = frozenset({"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"})
# 只把启动 Docker/本地工具所需的宿主进程能力带入隔离验收；业务配置由所选 env 文件提供。
HOST_PROCESS_ENV_KEYS: Final = frozenset(
    {
        "BUILDKIT_PROGRESS",
        "DOCKER_API_VERSION",
        "DOCKER_BUILDKIT",
        "DOCKER_DEFAULT_PLATFORM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
    },
)
ACCEPTANCE_CONTROL_ENV_KEYS: Final = frozenset(
    {
        "BROWSER",
        "LIVE_ACCEPTANCE_CONCURRENCY",
        "LIVE_ACCEPTANCE_RUNS",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_HEADLESS",
        "REAL_ACCEPTANCE_AGENT_ID",
        "REAL_ACTION_TIMEOUT_MS",
        "REAL_TEST_RUN_TIMEOUT_MS",
        "REQUIRE_LIVE_RUNTIME",
        "UV",
        "VERIFY_SCREENSHOT_DIR",
    },
)
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
    source_root: Path = REPO_ROOT


class _IsolatedVersionOverrides(TypedDict):
    APP_VERSION: str
    AGENTGOV_RUNTIME_VERSION: str


PROFILES = {
    "core": AcceptanceProfile("core", (), CORE_SERVICES, CORE_SERVICES),
    "langfuse": AcceptanceProfile(
        "langfuse",
        ("langfuse",),
        CORE_SERVICES,
        (*CORE_SERVICES, *LANGFUSE_SERVICES),
    ),
}

_MAKE = "/usr/bin/make"
_ALLOWED_ACCEPTANCE_COMMANDS: Final[dict[str, dict[tuple[str, ...], str]]] = {
    "core": {
        (_MAKE, "--no-print-directory", "_smoke"): "smoke",
        (_MAKE, "--no-print-directory", "_ui-smoke"): "ui-smoke",
        (_MAKE, "--no-print-directory", "_container-core-smoke"): "container-core-smoke",
        (_MAKE, "--no-print-directory", "_container-mcp-technical-smoke"): "container-mcp-technical-smoke",
        (_MAKE, "--no-print-directory", "_container-openapi-check"): "container-openapi-check",
        (
            _MAKE,
            "--no-print-directory",
            "_ui-playground-cancel-smoke",
            "AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1",
            "BROWSER=both",
        ): "ui-playground-cancel-smoke",
        (
            _MAKE,
            "--no-print-directory",
            "_ui-playground-cancel-smoke",
            "AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=0",
            "BROWSER=both",
            "REAL_ACCEPTANCE_AGENT_ID=security-operations-expert",
        ): "ui-playground-technical-smoke",
    },
    "langfuse": {
        (_MAKE, "--no-print-directory", "_container-live-test"): "container-live-test",
        (_MAKE, "--no-print-directory", "_container-technical-live-smoke"): "container-technical-live-smoke",
        (_MAKE, "--no-print-directory", "_container-release-candidate"): "container-release-candidate",
        (_MAKE, "--no-print-directory", "_langfuse-smoke"): "langfuse-smoke",
        (_MAKE, "--no-print-directory", "_main-flow-live-test"): "main-flow-live-test",
        (
            _MAKE,
            "--no-print-directory",
            "_ui-feedback-smoke",
            "AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1",
            "BROWSER=both",
        ): "ui-feedback-smoke",
        (
            _MAKE,
            "--no-print-directory",
            "_ui-agent-candidate-technical-smoke",
            "BROWSER=both",
        ): "ui-agent-candidate-technical-smoke",
    },
}


def validated_acceptance_command(profile: AcceptanceProfile, command: list[str]) -> tuple[str, str]:
    target = _ALLOWED_ACCEPTANCE_COMMANDS.get(profile.name, {}).get(tuple(command))
    if target is None:
        raise AcceptanceError("验收子命令必须是与 profile 匹配的公开 Make 验收目标")
    digest = hashlib.sha256(json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return target, digest


def acceptance_allowlisted_targets() -> frozenset[str]:
    """返回公共目标及其唯一允许的私有实现目标。"""

    public_targets: set[str] = set()
    private_targets: set[str] = set()
    for commands in _ALLOWED_ACCEPTANCE_COMMANDS.values():
        for command, target in commands.items():
            public_targets.add(target)
            private_targets.add(command[2])
    return frozenset(public_targets | private_targets)


def resolve_env_file(profile: AcceptanceProfile, requested: Path | None, environ: dict[str, str]) -> Path:
    del profile
    raw = requested or Path(environ.get("COMPOSE_ENV_FILE", "docker/.env"))
    selected = raw if raw.is_absolute() else REPO_ROOT / raw
    selected = Path(os.path.abspath(selected))
    payload, _identity = read_stable_env_file(selected, error_type=AcceptanceError)
    try:
        parse_selected_env_payload(payload)
    except ValueError as exc:
        raise AcceptanceError(str(exc).replace("cutover env", "所选 Compose env")) from exc
    return selected


def allocate_loopback_ports(count: int) -> tuple[int, ...]:
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for port in range(50400, 50500):
            try:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError as exc:
                raise AcceptanceError("无法创建用于检查 50400–50499 验收端口的 socket") from exc
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


def compose_files(source_root: Path) -> tuple[Path, Path]:
    return source_root / "docker/docker-compose.yml", source_root / "docker/docker-compose.langfuse.yml"


def _compose_mount_variables(source_root: Path = REPO_ROOT) -> set[str]:
    compose_text = "\n".join(path.read_text(encoding="utf-8") for path in compose_files(source_root))
    variables = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", compose_text))
    return {name for name in variables if name.endswith("_MOUNT") or name == "AGENTGOV_API_GATE_STATE_DIR_HOST"}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _acceptance_ports(allocated: tuple[int, ...] | None) -> tuple[int, int, int, int, int]:
    ports = allocated if allocated is not None else allocate_loopback_ports(5)
    if len(ports) != 5:
        raise AcceptanceError("隔离验收必须绑定 5 个 50400–50499 范围内的端口")
    return ports[0], ports[1], ports[2], ports[3], ports[4]


def prepare_isolated_environment(
    source_env: Path,
    run_id: str,
    temp_dir: Path,
    *,
    source_env_payload: bytes | None = None,
    source_root: Path = REPO_ROOT,
    source_digest: str | None = None,
    allocated_ports: tuple[int, ...] | None = None,
) -> IsolatedEnvironment:
    runtime_root = (temp_dir / "runtime-root").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    api_port, frontend_port, langfuse_port, minio_port, minio_console_port = _acceptance_ports(allocated_ports)
    token = run_id.rsplit("-", 1)[-1]
    api_key = secrets.token_hex(32)
    project_name = f"agv-acceptance-{os.getuid()}-{token}"
    overrides = {
        "AGENTGOV_SOURCE_ARTIFACT_SHA256": source_digest or source_artifact_sha256(source_root),
        **_isolated_version_overrides(source_root, token),
        "COMPOSE_PROJECT_NAME": project_name,
        "CONTAINER_NAME_PREFIX": project_name,
        "API_KEY": api_key,
        "FRONTEND_RUNTIME_API_KEY": api_key,
        "HOST_RUNTIME_VOLUME_ROOT": runtime_root.as_posix(),
        "RUNTIME_BOOTSTRAP_HOST_DIR": (source_root / "docker/runtime-bootstrap").resolve().as_posix(),
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
    gate_state.write_bytes((source_root / "docker/api-gate/api-gate-state.json").read_bytes())
    gate_state.chmod(0o600)
    for directory in (runtime_root / "docker-config", runtime_root / "buildx-state"):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    missing = _compose_mount_variables(source_root) - set(overrides)
    if missing:
        raise AcceptanceError("Compose 新增了未隔离的宿主挂载变量: " + ", ".join(sorted(missing)))

    isolated_env_file = temp_dir / "compose.acceptance.env"
    if source_env_payload is None:
        source_env_payload, _identity = read_stable_env_file(source_env, error_type=AcceptanceError)
    _write_isolated_env(source_env_payload, isolated_env_file, overrides)
    return IsolatedEnvironment(
        env_file=isolated_env_file,
        runtime_root=runtime_root,
        project_name=project_name,
        container_prefix=project_name,
        overrides=overrides,
        source_root=source_root,
    )


def _isolated_version_overrides(source_root: Path, token: str) -> _IsolatedVersionOverrides:
    try:
        runtime_version = (source_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AcceptanceError("隔离验收源码缺少产品 VERSION") from exc
    except UnicodeError as exc:
        raise AcceptanceError("隔离验收源码的产品 VERSION 无效") from exc
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,126}", runtime_version) is None:
        raise AcceptanceError("隔离验收源码的产品 VERSION 无效")
    return {"APP_VERSION": f"acceptance-{token}", "AGENTGOV_RUNTIME_VERSION": runtime_version}


def _write_isolated_env(source: bytes, target: Path, overrides: dict[str, str]) -> None:
    # 每个隔离键只保留一个定义，让 Compose、dotenv 和 shell 消费者取得相同值。
    try:
        bindings = parse_selected_env_payload(source)
    except ValueError as exc:
        raise AcceptanceError(str(exc).replace("cutover env", "所选 Compose env")) from exc
    lines = [binding.original.string.rstrip("\r\n") for binding in bindings if binding.key not in overrides]
    lines.extend(["", "# 本轮隔离验收配置；不得复用为正式部署配置。"])
    lines.extend(f"{key}={value}" for key, value in sorted(overrides.items()))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    try:
        initialize_shared_secret(target)
    except (OSError, ValueError):
        raise AcceptanceError("ISOLATED_RUNTIME_SHARED_SECRET_INITIALIZATION_FAILED") from None


def build_acceptance_env(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    run_id: str,
    environ: dict[str, str],
    snapshots: tuple[ScenarioFileSnapshot, ...] = (),
    *,
    acceptance_target: str = "",
    toolchain: AcceptanceToolchain | None = None,
    live_source_root: Path = REPO_ROOT,
) -> dict[str, str]:
    preserved_names = HOST_PROCESS_ENV_KEYS | ACCEPTANCE_CONTROL_ENV_KEYS
    child_env = {name: value for name, value in environ.items() if name in preserved_names}
    for name in SCENARIO_FILE_ENV_KEYS:
        child_env.pop(name, None)
    child_env.update(snapshot_environment(snapshots))
    child_env.update(isolation.overrides)
    bound_toolchain = toolchain or capture_acceptance_toolchain(
        environ,
        acceptance_target,
        error_type=AcceptanceError,
    )
    if acceptance_target in BROWSER_ACCEPTANCE_TARGETS:
        child_env["BROWSER"] = "both"
    child_env.update(
        {
            ACTIVE_ENV: "1",
            RUN_ID_ENV: run_id,
            PROFILE_ENV: profile.name,
            "LANGFUSE_ENABLED": "true" if profile.name == "langfuse" else "false",
            "COMPOSE_ENV_FILE": str(isolation.env_file),
            "AGENT_GOV_COMPOSE_ENV_FILE": str(isolation.env_file),
            ACCEPTANCE_CONTEXT_ENV: str(isolation.runtime_root.parent / "acceptance-context.json"),
            "NO_PROXY": LOOPBACK_NO_PROXY,
            "no_proxy": LOOPBACK_NO_PROXY,
            "BUILDX_CONFIG": str(isolation.runtime_root / "buildx-state"),
            "BUILDX_BUILDER": "default",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "HOME": str(TRUSTED_USER_HOME),
            LIVE_SOURCE_ROOT_ENV: str(live_source_root),
            DEPLOYABLE_SOURCE_ROOT_ENV: str(isolation.source_root),
        }
    )
    child_env.update(toolchain_environment(bound_toolchain))
    if not bound_toolchain["execution_root"]:
        child_env.update(
            {
                "DOCKER_CONFIG": str(isolation.runtime_root / "docker-config"),
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(isolation.source_root),
                        str(isolation.source_root / "packages/agentgov-testkit/src"),
                    )
                ),
            }
        )
    child_env[ACCEPTANCE_TARGET_ENV] = acceptance_target
    for name in PROXY_ENV_KEYS:
        child_env.pop(name, None)
    return child_env


def compose_command(
    profile: AcceptanceProfile,
    env_file: Path,
    source_root: Path = REPO_ROOT,
    *,
    docker_path: str | Path = SYSTEM_TOOL_PATHS["docker"],
) -> list[str]:
    compose_file, langfuse_compose_file = compose_files(source_root)
    command = [
        str(docker_path),
        "compose",
        "--parallel",
        "3",
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
    ]
    if "langfuse" in profile.compose_profiles:
        command.extend(["-f", str(langfuse_compose_file)])
    for compose_profile in profile.compose_profiles:
        command.extend(["--profile", compose_profile])
    return command
