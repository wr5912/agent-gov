from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"
VOLUME_PERMISSION_SCRIPT = REPO_ROOT / "scripts" / "fix_host_backend_volume_permissions.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _create_runtime_root(root: Path, *, with_marker: bool = True) -> Path:
    for relative_path in ("data", "governor-workspace", "claude-roots/governor"):
        (root / relative_path).mkdir(parents=True, exist_ok=True)
    if with_marker:
        marker = root / "data/.agent-gov/runtime-coordination/receipt.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")
    return root


def _volume_command(root: Path) -> list[str]:
    return [
        str(VOLUME_PERMISSION_SCRIPT),
        "--root",
        str(root),
        "--approved-root",
        str(root),
        "--uid",
        "1000",
        "--gid",
        "1000",
    ]


def test_deploy_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_script_requires_explicit_trusted_target_and_preserves_private_remote_env() -> None:
    text = _script_text()

    assert "DEFAULT_HOST" not in text
    assert 'DEPLOY_USER="${DEPLOY_USER:-root}"' not in text
    assert "StrictHostKeyChecking=accept-new" not in text
    assert "StrictHostKeyChecking=yes" in text
    assert "UserKnownHostsFile=" in text
    assert 'ssh-keygen -F "$DEPLOY_TARGET"' in text
    for required_option in (
        "--target",
        "--user",
        "--remote-dir",
        "--known-hosts-file",
        "--source-commit",
        "--project-name",
        "--change-id",
        "--confirm-target",
    ):
        assert required_option in text
    assert "cp -n docker/.env.example docker/.env" in text

    for excluded in (
        "--exclude='/images/'",
        "--exclude='/docker/.env'",
        "--exclude='/docker/.env.local-debug'",
        "--exclude='/frontend/.env.local'",
    ):
        assert excluded in text


def test_deploy_script_without_explicit_change_contract_has_no_external_effect() -> None:
    result = subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "missing required deployment field" in result.stderr


def test_deploy_script_packages_project_and_langfuse_dependency_images() -> None:
    text = _script_text()

    for image in (
        "agent-gov-api:${VERSION}",
        "agent-gov-ui:${VERSION}",
        "agent-gov-litellm-sidecar:${VERSION}",
    ):
        assert image in text

    for env_key in (
        "LANGFUSE_WORKER_IMAGE",
        "LANGFUSE_WEB_IMAGE",
        "LANGFUSE_POSTGRES_IMAGE",
        "LANGFUSE_CLICKHOUSE_IMAGE",
        "LANGFUSE_REDIS_IMAGE",
        "LANGFUSE_MINIO_IMAGE",
    ):
        assert env_key in text

    assert "docker save" in text
    assert "docker load" in text
    assert "sha256sum" in text
    assert "agent-gov-${VERSION}-images.tar.gz" in text
    assert "agent-gov-${VERSION}-langfuse-deps-images.tar.gz" in text


def test_deploy_script_uses_loaded_images_for_full_compose_stack() -> None:
    text = _script_text()

    assert "git fetch" not in text
    assert 'git archive "$SOURCE_COMMIT"' in text
    assert 'git show "${SOURCE_COMMIT}:VERSION"' in text
    assert "working tree must be clean" not in text
    assert "--profile langfuse down --remove-orphans" in text
    assert '--project-name "$project_name"' in text
    assert "COMPOSE_ENV_FILE=docker/.env" in text
    assert 'COMPOSE_UP_FLAGS="--no-build --pull never"' in text
    assert "make --no-print-directory all-up" in text
    assert 'docker ps -aq --filter "name=agent-gov"' not in text
    assert "docker rm -f" not in text
    assert "--profile langfuse up -d --no-build --pull never" not in text
    assert "runtime_root=$(expand_remote_value" not in text
    assert "chmod a+rwx" not in text
    assert 'DEPLOY_TARGET_MARKER=".agentgov-deploy-target"' in text
    assert 'DEPLOY_RECEIPT=".agentgov-deploy-receipt"' in text
    assert "unmarked remote project path must be empty" in text
    assert "rm -rf" not in text
    assert 'find "$canonical_tmp_dir" -xdev -depth -mindepth 1 -delete' in text
    assert 'rmdir -- "$canonical_tmp_dir"' in text


def test_deploy_script_uses_python_health_checks_without_remote_curl_dependency() -> None:
    text = _script_text()

    assert "from urllib.request import Request, urlopen" in text
    assert '("API health", "http://127.0.0.1:${host_port}/health", 60, True)' in text
    assert '("UI", "http://127.0.0.1:${frontend_port}", 60, False)' in text
    assert '("Langfuse", "http://127.0.0.1:${langfuse_port}", 90, False)' in text
    assert "curl " not in text


def test_volume_permission_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(VOLUME_PERMISSION_SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(VOLUME_PERMISSION_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_volume_permission_script_has_no_implicit_root_or_image_selection() -> None:
    text = VOLUME_PERMISSION_SCRIPT.read_text(encoding="utf-8")

    assert 'TARGET_ROOT=""' in text
    assert 'APPROVED_ROOT=""' in text
    assert "docker images" not in text
    assert '-v "$TARGET_ROOT:/target"' not in text
    assert "chown -R" not in text
    assert 'LAYOUT_MARKER_RELATIVE="data/.agent-gov/runtime-coordination/receipt.json"' in text
    for relative_path in ("data", "governor-workspace", "claude-roots/governor"):
        assert f'"{relative_path}"' in text


def test_volume_permission_script_rejects_missing_contract_root_and_layout(tmp_path: Path) -> None:
    no_arguments = subprocess.run(
        [str(VOLUME_PERMISSION_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert no_arguments.returncode == 2

    root_result = subprocess.run(
        _volume_command(Path("/")),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_result.returncode == 2
    assert "non-root absolute path" in root_result.stderr

    missing_marker_root = _create_runtime_root(tmp_path / "missing-marker", with_marker=False)
    marker_result = subprocess.run(
        _volume_command(missing_marker_root),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert marker_result.returncode == 1
    assert "coordination receipt is missing" in marker_result.stderr


def test_volume_permission_script_rejects_symlink_root(tmp_path: Path) -> None:
    runtime_root = _create_runtime_root(tmp_path / "runtime-root")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(runtime_root, target_is_directory=True)

    result = subprocess.run(
        _volume_command(linked_root),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "non-symlink directory" in result.stderr


def test_volume_permission_script_rejects_symlinked_layout_marker_parent(tmp_path: Path) -> None:
    runtime_root = _create_runtime_root(tmp_path / "runtime-root", with_marker=False)
    external_marker_dir = tmp_path / "external-marker"
    external_marker_dir.mkdir()
    (external_marker_dir / "receipt.json").write_text("{}\n", encoding="utf-8")
    marker_parent = runtime_root / "data/.agent-gov/runtime-coordination"
    marker_parent.parent.mkdir(parents=True)
    marker_parent.symlink_to(external_marker_dir, target_is_directory=True)

    result = subprocess.run(
        _volume_command(runtime_root),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "must not traverse symlinks" in result.stderr


def test_volume_permission_script_defaults_to_side_effect_free_dry_run(tmp_path: Path) -> None:
    runtime_root = _create_runtime_root(tmp_path / "runtime-root")
    result = subprocess.run(
        _volume_command(runtime_root),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "mode=dry-run" in result.stdout
    assert "no filesystem changes were made" in result.stdout


def test_volume_permission_script_apply_requires_confirmation_before_docker(tmp_path: Path) -> None:
    runtime_root = _create_runtime_root(tmp_path / "runtime-root")
    result = subprocess.run(
        [*_volume_command(runtime_root), "--apply"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--confirm-root must exactly match" in result.stderr


def test_volume_permission_script_apply_mounts_only_three_approved_directories(tmp_path: Path) -> None:
    runtime_root = _create_runtime_root(tmp_path / "runtime-root")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n', encoding="utf-8")
    fake_docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(docker_log)

    result = subprocess.run(
        [
            *_volume_command(runtime_root),
            "--apply",
            "--confirm-root",
            str(runtime_root),
            "--change-id",
            "change-123",
            "--image",
            "agent-gov-api:test",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert "image inspect agent-gov-api:test" in calls
    assert "--network none --read-only --user 0:0 --cap-drop ALL" in calls
    assert f"src={runtime_root}/data,dst=/approved/data" in calls
    assert f"src={runtime_root}/governor-workspace,dst=/approved/governor-workspace" in calls
    assert f"src={runtime_root}/claude-roots/governor,dst=/approved/governor-claude-root" in calls
    assert f"src={runtime_root},dst=/target" not in calls
