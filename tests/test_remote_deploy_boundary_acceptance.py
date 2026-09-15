from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts/remote_deploy_boundary_acceptance.py"


def test_boundary_runner_reports_only_real_primitives_and_keeps_transaction_gap_open() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert '"docker", "save"' in source
    assert '"docker", "load"' in source
    assert '"docker", "compose"' in source
    assert "validate_image_archive(" in source
    assert "_verify_exclusive_lock(" in source
    assert "_health(port)" in source
    assert '"primitive_restore": True' in source
    assert '"transaction_execute_recover": False' in source
    assert '"scope": "primitives_only"' in source
    assert "候选与旧镜像必须具有不同 immutable identity" in source
    assert "远端验收禁止 docker compose config" in source
    assert '"transport": "ephemeral-sshd-loopback"' in source
    assert '"ssh-keygen"' in source
    assert '"/usr/sbin/sshd"' in source
    assert 'arguments[:2] == ["docker", "compose"] and "config" in arguments[2:]' in source
    assert "mock" not in source.casefold()


def test_real_remote_deploy_boundary_acceptance() -> None:
    if os.environ.get("RUN_REMOTE_DEPLOY_BOUNDARY_ACCEPTANCE") != "1":
        pytest.skip("set RUN_REMOTE_DEPLOY_BOUNDARY_ACCEPTANCE=1 for the real Docker/Compose boundary lane")

    result = subprocess.run(
        [str(REPO_ROOT / ".venv/bin/python"), str(RUNNER)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["archive_validation"] is True
    assert evidence["exclusive_lock"] is True
    assert evidence["docker_load"] is True
    assert evidence["compose_recreate"] is True
    assert evidence["health"] is True
    assert evidence["health_failure_probe"] is True
    assert evidence["primitive_restore"] is True
    assert evidence["transaction_execute_recover"] is False
    assert evidence["compose_config_forbidden"] is True
    assert evidence["scope"] == "primitives_only"
    assert evidence["old_image_id_sha256"] != evidence["candidate_image_id_sha256"]
    assert evidence["transport"] == "ephemeral-sshd-loopback"
