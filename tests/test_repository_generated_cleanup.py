from __future__ import annotations

from pathlib import Path

import pytest
from scripts.cleanup_repository_generated import cleanup_repository_generated


def _write(path: Path, content: str = "generated") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_generated_cleanup_is_allowlisted_dry_run_and_idempotent(tmp_path: Path) -> None:
    generated = {
        ".coverage": tmp_path / ".coverage",
        ".pytest_cache": tmp_path / ".pytest_cache/cache",
        "app/__pycache__": tmp_path / "app/__pycache__/module.pyc",
        "app/worker": tmp_path / "app/worker/__pycache__/agent_jobs.pyc",
        "frontend/dist": tmp_path / "frontend/dist/app.js",
        "frontend/tsconfig.tsbuildinfo": tmp_path / "frontend/tsconfig.tsbuildinfo",
        "mutants": tmp_path / "mutants/mutmut-stats.json",
    }
    for path in generated.values():
        _write(path)

    protected = (
        tmp_path / ".venv/bin/python",
        tmp_path / "frontend/node_modules/package/index.js",
        tmp_path / "artifacts/test-quality/evidence.json",
        tmp_path / "docker/.env",
        tmp_path / "docker/volume/data/runtime.sqlite3",
        tmp_path / ".git/config",
    )
    for path in protected:
        _write(path)

    preview = cleanup_repository_generated(repo_root=tmp_path, dry_run=True)

    assert set(preview["candidates"]) == set(generated)
    assert preview["removed"] == []
    assert all(path.exists() for path in (*generated.values(), *protected))

    applied = cleanup_repository_generated(repo_root=tmp_path)

    assert set(applied["removed"]) == set(generated)
    assert all(not path.exists() for path in generated.values())
    assert all(path.exists() for path in protected)
    assert cleanup_repository_generated(repo_root=tmp_path)["candidates"] == []


def test_evidence_cleanup_requires_explicit_scope(tmp_path: Path) -> None:
    evidence = tmp_path / "artifacts/test-quality/main-full/evidence.json"
    cache = tmp_path / ".pytest_cache/cache"
    _write(evidence)
    _write(cache)

    result = cleanup_repository_generated(repo_root=tmp_path, scope="evidence")

    assert result["removed"] == ["artifacts"]
    assert not evidence.exists()
    assert cache.exists()


def test_cleanup_rejects_symlinks_before_removing_anything(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _write(outside / "keep.txt")
    mutants = tmp_path / "mutants"
    mutants.symlink_to(outside, target_is_directory=True)
    cache = tmp_path / ".pytest_cache/cache"
    _write(cache)

    with pytest.raises(ValueError, match="refusing to clean symlink: mutants"):
        cleanup_repository_generated(repo_root=tmp_path)

    assert mutants.is_symlink()
    assert cache.exists()
    assert (outside / "keep.txt").exists()


def test_cleanup_rejects_evidence_directory_containing_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-evidence"
    outside.mkdir()
    _write(outside / "keep.txt")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "outside").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="directory containing symlink"):
        cleanup_repository_generated(repo_root=tmp_path, scope="evidence")

    assert artifacts.exists()
    assert (outside / "keep.txt").exists()


def test_legacy_worker_with_source_is_preserved_but_its_cache_is_cleaned(tmp_path: Path) -> None:
    source = tmp_path / "app/worker/new_worker.py"
    cache = tmp_path / "app/worker/__pycache__/new_worker.pyc"
    _write(source, "active = True\n")
    _write(cache)

    result = cleanup_repository_generated(repo_root=tmp_path)

    assert result["skipped_protected"] == ["app/worker"]
    assert source.exists()
    assert not cache.exists()
