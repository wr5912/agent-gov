from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"
REMOTE_HELPER = REPO_ROOT / "scripts" / "agent_gov_release_remote"
RELEASECTL = REPO_ROOT / "scripts" / "releasectl"
INSTALLER = REPO_ROOT / "scripts" / "install_agent_gov_release_controller"


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_shell_entrypoints_are_executable_and_have_valid_syntax() -> None:
    for script in (DEPLOY_SCRIPT, REMOTE_HELPER, RELEASECTL, INSTALLER):
        assert os.access(script, os.X_OK), f"not executable: {script}"
        result = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_deploy_reads_the_controller_source_clone_and_exact_master_sha() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert 'ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.."' in text
    assert "AGENT_GOV_SOURCE_REPO_DIR" in text
    assert 'SOURCE_REPO_DIR="${AGENT_GOV_SOURCE_REPO_DIR:-$ROOT_DIR}"' in text
    assert '[[ "$REF_SHA" =~ ^[0-9a-f]{40}$ ]]' in text
    assert 'git -C "$SOURCE_REPO_DIR" fetch origin master --prune' in text
    assert 'git -C "$SOURCE_REPO_DIR" merge-base --is-ancestor' in text
    assert 'git -C "$SOURCE_REPO_DIR" archive "$REF_SHA"' in text
    assert "git archive origin/master" not in text
    assert '"$TMP_DIR/$COMPOSE_FILE"' in text


def test_deploy_pins_ssh_and_installs_a_stable_remote_helper() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=accept-new" not in text
    assert "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" in text
    assert "AGENT_GOV_REMOTE_HELPER" in text
    assert 'sync_remote_helper' in text
    assert 'shared/bin/agent-gov-release-remote' in text
    assert 'invoke_remote_helper rollback "$ROLLBACK_RELEASE_ID"' in text
    assert 'invoke_remote_helper diagnose "$DIAGNOSE_RELEASE_ID"' in text


def test_deploy_keeps_release_metadata_and_images_immutable_without_github_secrets() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert 'RELEASE_ID="${ENVIRONMENT}-${SHORT_SHA}"' in text
    assert 'IMAGE_VERSION="${VERSION}-${SHORT_SHA}"' in text
    assert '"agent-gov-api:${IMAGE_VERSION}"' in text
    assert '"agent-gov-ui:${IMAGE_VERSION}"' in text
    assert '"agent-gov-litellm-sidecar:${IMAGE_VERSION}"' in text
    assert '"$REMOTE_PATH/releases/$RELEASE_ID"' in text
    assert '"$REMOTE_PATH/shared/docker.env"' in text
    assert '"repository": repository' in text
    assert '"commit_sha": sha' in text
    assert '"image_digests": images' in text
    assert "project-images.tar.gz" in text
    assert "dependency-images.tar.gz" in text
    assert "sha256sum" in text
    assert "prepared_release_state" in text
    assert 'PREPARED_STATE=$(prepared_release_state)' in text
    assert "Reusing committed immutable release" in text
    assert "hasher.update(chunk)" in text
    assert "read_bytes()" not in text
    assert "GITHUB_TOKEN" not in text
    assert "GH_TOKEN" not in text


def test_remote_helper_restores_archived_images_and_checks_readiness() -> None:
    text = text_of(REMOTE_HELPER)

    assert 'flock -n 9' in text
    assert 'RELEASES_DIR="$RELEASE_ROOT/releases"' in text
    assert 'SHARED_ENV="$SHARED_DIR/docker.env"' in text
    assert "atomic_current_link" in text
    assert 'load_release_images "$previous_path"' in text
    assert 'load_release_images "$target"' in text
    assert 'load_release_images "$current_path"' in text
    assert "legacy-images.tar.gz" in text
    assert 'docker save "${legacy_images[@]}"' in text
    assert "/health/ready" in text
    assert "Automatic rollback" in text
    assert "return 2" in text
    assert "return 3" in text
    assert "--profile langfuse up -d --wait --wait-timeout 180" in text
    assert "GITHUB_TOKEN" not in text
    assert "source_release/docker/.env.example" not in text
    assert "chmod a+rwx" not in text
    for key in (
        "LANGFUSE_POSTGRES_UID",
        "LANGFUSE_CLICKHOUSE_UID",
        "LANGFUSE_REDIS_UID",
        "LANGFUSE_MINIO_UID",
    ):
        assert key in text


def test_target_private_runtime_env_fails_closed_instead_of_using_an_example() -> None:
    deploy = text_of(DEPLOY_SCRIPT)
    remote = text_of(REMOTE_HELPER)

    assert "target private Compose env is missing" in deploy
    assert "Initializing target private Compose env" not in deploy
    assert "private Compose env is missing" in remote


def test_invalid_ref_fails_locally_without_transport_or_network(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/wr5912/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = os.environ.copy()
    environment["AGENT_GOV_SOURCE_REPO_DIR"] = str(source)

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--ref", "not-a-sha", "--preflight-only"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "lowercase full 40-character commit SHA" in result.stderr
