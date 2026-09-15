"""使用选定 env 对当前源码执行轻量、可移植的 Compose 镜像构建。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from scripts import selected_env_source_snapshot as source_snapshot
from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings, read_stable_env_file, verify_stable_env_file
from scripts.agentscope_atomic_cutover_lock import run_with_global_cutover_lock
from scripts.selected_env_operation_contract import OperationEnvironment, SelectedEnvError, require_current_epoch_env

OPERATIONS = frozenset({"build", "ui-build"})
_SOURCE_LABEL = "io.agentgov.source-artifact-sha256"


def _build_environment(
    snapshot: Path,
    repo_root: Path,
    version: str,
    source_digest: str,
    input_environment: OperationEnvironment,
) -> tuple[OperationEnvironment, tuple[Path, Path]]:
    compose_files = (
        repo_root / "docker/docker-compose.yml",
        repo_root / "docker/docker-compose.langfuse.yml",
    )
    child_env = source_snapshot.selected_env_child_env(
        snapshot,
        explicit={
            "AGENTGOV_SOURCE_ARTIFACT_SHA256": source_digest,
            "APP_VERSION": version,
            "AGENTGOV_RUNTIME_VERSION": version,
        },
        compose_files=compose_files,
    )
    child_env.update(input_environment)
    child_env.update(
        {
            "AGENT_GOV_COMPOSE_ENV_FILE": snapshot.as_posix(),
            "COMPOSE_ENV_FILE": snapshot.as_posix(),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        },
    )
    child_env.pop("DOCKER_CONTEXT", None)
    return child_env, compose_files


def _verify_built_images(
    operation: str,
    version: str,
    source_digest: str,
    repo_root: Path,
    child_env: OperationEnvironment,
) -> None:
    services = (
        ("agent-gov-ui",)
        if operation == "ui-build"
        else (
            "agent-gov-api",
            "agent-gov-agentscope-runtime",
            "agent-gov-ui",
        )
    )
    for image in services:
        try:
            inspected = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    f'{{{{ index .Config.Labels "{_SOURCE_LABEL}" }}}}',
                    f"{image}:{version}",
                ],
                cwd=repo_root,
                env=child_env,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise SelectedEnvError("无法核验构建镜像标签") from exc
        if inspected.returncode != 0 or inspected.stdout.strip() != source_digest:
            raise SelectedEnvError(f"构建镜像源码标签不匹配: {image}")


def run_direct_build(source: Path, operation: str, *, repo_root: Path) -> int:
    """Build images without runtime deployment attestation and source copying."""

    if operation not in OPERATIONS:
        raise SelectedEnvError(f"unsupported direct build operation: {operation}")
    payload, original_identity = read_stable_env_file(source, error_type=SelectedEnvError)
    with tempfile.TemporaryDirectory(prefix="agentgov-selected-env-build-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        snapshot = source_snapshot.write_operation_input(directory, payload)
        input_environment = source_snapshot.seal_operation_input(snapshot)
        parse_selected_env_bindings(snapshot)
        require_current_epoch_env(snapshot, operation)
        version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
        if not version:
            raise SelectedEnvError("VERSION 不得为空")
        source_digest = source_artifact_sha256(repo_root)
        child_env, compose_files = _build_environment(
            snapshot,
            repo_root,
            version,
            source_digest,
            input_environment,
        )
        verify_stable_env_file(source, payload, original_identity, error_type=SelectedEnvError)
        command = [
            "docker",
            "compose",
            "--env-file",
            snapshot.as_posix(),
            "-f",
            compose_files[0].as_posix(),
            "build",
            "--pull=false",
        ]
        if operation == "ui-build":
            command.append("agent-gov-ui")

        def execute() -> int:
            try:
                result = subprocess.run(command, cwd=repo_root, env=child_env, check=False)
            except OSError as exc:
                raise SelectedEnvError("无法执行 Docker Compose build") from exc
            if result.returncode != 0:
                raise SelectedEnvError(f"Docker Compose build failed (exit={result.returncode})")
            _verify_built_images(operation, version, source_digest, repo_root, child_env)
            return 0

        result = run_with_global_cutover_lock(SelectedEnvError, execute)
        verify_stable_env_file(source, payload, original_identity, error_type=SelectedEnvError)
        if source_artifact_sha256(repo_root) != source_digest:
            raise SelectedEnvError("Docker Compose build 期间 deployable source 发生变化")
        return result
