"""版本唯一真相源（根 VERSION）与一致性硬门验证。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from scripts.check_version_consistency import artifact_consistency_errors

from app_test_utils import load_test_app

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_version_consistency.py"


def _run_check() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=30)


def test_app_version_reads_version_file() -> None:
    # app/version.py 不得硬编码，必须读根 VERSION（→ OpenAPI info.version / health runtime_version）。
    from app.version import APP_VERSION

    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == APP_VERSION


def test_health_reports_runtime_version_from_version_file(monkeypatch, tmp_path: Path) -> None:
    module = load_test_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["runtime_version"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_consistency_gate_passes_on_aligned_repo() -> None:
    result = _run_check()
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_detects_frontend_drift_without_mutating_checkout(tmp_path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "docker").mkdir()
    (tmp_path / "VERSION").write_text("3.0.0\n", encoding="utf-8")
    (tmp_path / "frontend/package.json").write_text('{"version":"9.9.9-drift"}\n', encoding="utf-8")
    (tmp_path / "docker/docker-compose.yml").write_text("image: agent-gov-api:${APP_VERSION:-dev}\n", encoding="utf-8")

    errors = artifact_consistency_errors(tmp_path, app_version="3.0.0")

    assert len(errors) == 1
    assert "package.json" in errors[0]
