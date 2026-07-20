from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = REPO_ROOT / "scripts" / "check_codex_governance.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_governance_baseline_uses_nested_project_tree(tmp_path: Path) -> None:
    outer_root = tmp_path / "outer"
    project_root = outer_root / "ai" / "agent-gov"
    outer_root.mkdir()
    _git(outer_root, "init")
    _git(outer_root, "config", "user.name", "Test")
    _git(outer_root, "config", "user.email", "test@example.com")

    large_source = project_root / "app" / "large.py"
    large_source.parent.mkdir(parents=True)
    large_source.write_text("x = 1\n" * 5, encoding="utf-8")
    docs_root = project_root / "docs"
    docs_root.mkdir()
    (docs_root / "README.md").write_text("# Docs\n\n- docs/guide.md\n", encoding="utf-8")
    guide = docs_root / "guide.md"
    guide.write_text("# Guide\n\n历史记录：TODO kept for audit.\n", encoding="utf-8")
    (docs_root / "开放接口规范.json").write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")
    _git(outer_root, "add", ".")
    _git(outer_root, "commit", "-m", "baseline")
    guide.write_text("# Guide\n\n历史记录：TODO kept for audit.\n\n新增整理说明。\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), "--root", str(project_root), "--mode", "fail", "--python-file-lines", "2"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "BASELINE: app/large.py" in result.stdout
    assert "unfinished marker" not in result.stdout
    assert "new active static OpenAPI snapshot" not in result.stdout
