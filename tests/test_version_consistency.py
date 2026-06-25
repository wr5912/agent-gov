"""版本唯一真相源（根 VERSION）与一致性硬门验证。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_version_consistency.py"


def _run_check() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=30)


def test_app_version_reads_version_file() -> None:
    # app/version.py 不得硬编码，必须读根 VERSION（→ OpenAPI info.version / health runtime_version）。
    from app.version import APP_VERSION

    assert APP_VERSION == (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_consistency_gate_passes_on_aligned_repo() -> None:
    result = _run_check()
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_detects_frontend_drift() -> None:
    # 篡改 frontend/package.json 版本 -> 硬门必须拦截（证明防漂移生效）；finally 还原，避免污染工作区。
    pkg = ROOT / "frontend" / "package.json"
    original = pkg.read_text(encoding="utf-8")
    try:
        pkg.write_text(re.sub(r'("version":\s*)"[^"]*"', r'\1"9.9.9-drift"', original, count=1), encoding="utf-8")
        result = _run_check()
        assert result.returncode != 0
        assert "package.json" in (result.stdout + result.stderr)
    finally:
        pkg.write_text(original, encoding="utf-8")
