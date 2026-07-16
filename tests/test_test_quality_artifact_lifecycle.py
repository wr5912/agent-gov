from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from scripts.run_mutation_lane import (
    collect_mutation_details,
    finalize_mutants_dir,
    prepare_mutation_artifact_dir,
    reset_mutants_dir,
)
from scripts.run_test_lane import cleanup_coverage_data, prepare_test_artifact_dir


def _write(path: Path, content: str = "stale") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_test_artifact_preparation_removes_only_owned_outputs(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "lane"
    for name in ("junit.xml", "coverage.json", "evidence.json", "gitconfig", ".coverage", ".coverage.worker"):
        _write(artifact_dir / name)
    selection = artifact_dir / "selection.json"
    _write(selection, "{}\n")

    prepare_test_artifact_dir(artifact_dir)

    assert selection.exists()
    assert {path.name for path in artifact_dir.iterdir()} == {"selection.json"}


def test_test_artifact_preparation_rejects_symlink_atomically(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "lane"
    junit = artifact_dir / "junit.xml"
    _write(junit)
    outside = tmp_path / "outside.json"
    _write(outside, "{}\n")
    artifact_dir.mkdir(exist_ok=True)
    (artifact_dir / "coverage.json").symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symlink"):
        prepare_test_artifact_dir(artifact_dir)

    assert junit.exists()
    assert outside.exists()


def test_coverage_cleanup_preserves_exported_report(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "lane"
    report = artifact_dir / "coverage.json"
    _write(report, "{}\n")
    _write(artifact_dir / ".coverage")
    _write(artifact_dir / ".coverage.worker")

    cleanup_coverage_data(artifact_dir)

    assert report.exists()
    assert not (artifact_dir / ".coverage").exists()
    assert not (artifact_dir / ".coverage.worker").exists()


def test_mutation_artifact_preparation_preserves_unowned_files(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "mutation"
    for name in ("mutmut-cicd-stats.json", "results.txt", "survivors.diff", "summary.json"):
        _write(artifact_dir / name)
    note = artifact_dir / "operator-note.txt"
    _write(note)

    prepare_mutation_artifact_dir(artifact_dir)

    assert {path.name for path in artifact_dir.iterdir()} == {note.name}


def test_mutation_details_capture_survivor_names_diffs_and_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    names = ["app.state.x__mutmut_1", "app.state.x__mutmut_2"]

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "results":
            stdout = "".join(f"    {name}: survived\n" for name in names)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        name = command[2]
        return subprocess.CompletedProcess(command, 0, stdout=f"# {name}: survived\n--- old\n+++ new\n", stderr="")

    monkeypatch.setattr("scripts.run_mutation_lane.subprocess.run", fake_run)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    details = collect_mutation_details(
        mutmut_bin=tmp_path / "mutmut",
        repo_root=tmp_path,
        artifact_dir=artifact_dir,
        stats={"survived": 2},
    )

    assert details.survivors == tuple(names)
    assert all(name in (artifact_dir / "survivors.diff").read_text(encoding="utf-8") for name in names)
    assert details.results_sha256 == hashlib.sha256((artifact_dir / "results.txt").read_bytes()).hexdigest()
    assert details.survivors_sha256 == hashlib.sha256((artifact_dir / "survivors.diff").read_bytes()).hexdigest()


def test_mutation_details_reject_count_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="app.state.x__mutmut_1: survived\n", stderr="")

    monkeypatch.setattr("scripts.run_mutation_lane.subprocess.run", fake_run)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    with pytest.raises(ValueError, match="survivor evidence mismatch"):
        collect_mutation_details(
            mutmut_bin=tmp_path / "mutmut",
            repo_root=tmp_path,
            artifact_dir=artifact_dir,
            stats={"survived": 2},
        )


def test_mutants_workdir_is_retained_below_gate_and_removed_after_success(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    _write(mutants_dir / "state.json")

    assert finalize_mutants_dir(mutants_dir, score=69.99, required_score=70.0) is False
    assert mutants_dir.exists()
    assert finalize_mutants_dir(mutants_dir, score=70.0, required_score=70.0) is True
    assert not mutants_dir.exists()


def test_mutants_workdir_rejects_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    mutants_dir = tmp_path / "mutants"
    mutants_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        reset_mutants_dir(mutants_dir)

    assert outside.exists()
