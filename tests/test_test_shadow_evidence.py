from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import compare_test_shadow_evidence as comparison
from scripts import evaluate_test_shadow_history as history
from scripts.test_quality.collection import CollectionResult, nodeid_digest
from scripts.test_quality.evidence import build_evidence, parse_junit, utc_now, write_evidence

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "tests/quality_policy.json"
COMPARE = REPO_ROOT / "scripts/compare_test_shadow_evidence.py"
HISTORY = REPO_ROOT / "scripts/evaluate_test_shadow_history.py"


def _run_real_pytest(tmp_path: Path, *, name: str, selection: tuple[str, ...], global_nodes: tuple[str, ...] | None = None) -> Path:
    artifact_dir = tmp_path / name
    artifact_dir.mkdir()
    environment = dict(os.environ)
    # 这是一个独立的真实 pytest 证据样本，不能继承外层门禁的 -x/-k 等
    # PYTEST_ADDOPTS；否则失败样本会在 pytest-cov 写出 coverage.json 前退出。
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "scripts.test_quality.pytest_plugin",
        "--noconftest",
        "-c",
        "/dev/null",
        f"--junitxml={artifact_dir / 'junit.xml'}",
        "--cov=samplepkg",
        f"--cov-report=json:{artifact_dir / 'coverage.json'}",
        *selection,
    ]
    started_at = utc_now()
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60, check=False)
    completed_at = utc_now()
    assert result.returncode in {0, 1}, result.stdout + result.stderr
    assert (artifact_dir / "junit.xml").is_file() and (artifact_dir / "coverage.json").is_file()
    outcomes, errors = parse_junit(artifact_dir / "junit.xml")
    assert not errors
    all_nodes = global_nodes or tuple(sorted(outcomes))
    evidence = build_evidence(
        repo_root=REPO_ROOT,
        policy_path=POLICY_PATH,
        artifact_dir=artifact_dir,
        lane="main-full",
        global_collection=CollectionResult(all_nodes, nodeid_digest(all_nodes)),
        selection=tuple(sorted(outcomes)),
        command=command,
        started_at=started_at,
        completed_at=completed_at,
        wall_seconds=max((completed_at - started_at).total_seconds(), 0.001),
        workers=0,
        scheduler="serial",
    )
    write_evidence(evidence, artifact_dir / "evidence.json")
    return artifact_dir


def _write_real_suite(tmp_path: Path) -> None:
    package = tmp_path / "samplepkg"
    package.mkdir()
    (package / "__init__.py").write_text("def result(value):\n    return value\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "from samplepkg import result\n\ndef test_passing_leaf():\n    assert result(1) == 1\n\ndef test_failing_leaf():\n    assert result(1) == 2\n",
        encoding="utf-8",
    )


def _compare(tmp_path: Path, serial: Path, tia: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    report = tmp_path / "shadow-report.json"
    result = subprocess.run(
        [sys.executable, str(COMPARE), "--serial-dir", str(serial), "--tia-dir", str(tia), "--policy", str(POLICY_PATH), "--output", str(report)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, report


def test_shadow_failed_serial_still_reports_real_tia_miss_without_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    _write_real_suite(tmp_path)
    serial = _run_real_pytest(tmp_path, name="serial", selection=("tests/test_sample.py",))
    serial_outcomes, _ = parse_junit(serial / "junit.xml")
    tia = _run_real_pytest(
        tmp_path,
        name="tia",
        selection=("tests/test_sample.py::test_passing_leaf",),
        global_nodes=tuple(sorted(serial_outcomes)),
    )

    result, report_path = _compare(tmp_path, serial, tia)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    direct_report = comparison.compare_evidence(
        serial_dir=serial,
        candidate_dirs=[],
        tia_dir=tia,
        policy_path=POLICY_PATH,
    )

    assert result.returncode == 1
    assert direct_report == report
    assert len(report["tia"]["misses"]) == 1
    assert report["tia"]["misses"][0].endswith("::test_failing_leaf")
    assert report["sample_passed"] is False
    assert any("serial evidence contains non-passing leaves" in error for error in report["mismatches"])


def test_shadow_history_rereads_sources_and_rejects_duplicate_or_tampered_report(tmp_path: Path) -> None:
    _write_real_suite(tmp_path)
    serial = _run_real_pytest(tmp_path, name="serial", selection=("tests/test_sample.py",))
    serial_outcomes, _ = parse_junit(serial / "junit.xml")
    tia = _run_real_pytest(
        tmp_path,
        name="tia",
        selection=("tests/test_sample.py::test_passing_leaf",),
        global_nodes=tuple(sorted(serial_outcomes)),
    )
    _, report_path = _compare(tmp_path, serial, tia)
    duplicate = subprocess.run(
        [sys.executable, str(HISTORY), "--policy", str(POLICY_PATH), str(report_path), str(report_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    duplicate_result = json.loads(duplicate.stdout)
    direct_duplicate_result = history.evaluate_history(
        [report_path, report_path],
        policy_path=POLICY_PATH,
    )
    assert duplicate.returncode == 1
    assert direct_duplicate_result == duplicate_result
    assert duplicate_result["paired_samples"] == 1
    assert any("duplicate serial evidence sample" in blocker for blocker in duplicate_result["blockers"])

    forged = json.loads(report_path.read_text(encoding="utf-8"))
    forged["sample_passed"] = True
    report_path.write_text(json.dumps(forged), encoding="utf-8")
    tampered = subprocess.run(
        [sys.executable, str(HISTORY), "--policy", str(POLICY_PATH), str(report_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    tampered_result = json.loads(tampered.stdout)
    direct_tampered_result = history.evaluate_history(
        [report_path],
        policy_path=POLICY_PATH,
    )
    assert tampered.returncode == 1
    assert direct_tampered_result == tampered_result
    assert tampered_result["paired_samples"] == 0
    assert any("differs from its revalidated source" in blocker for blocker in tampered_result["blockers"])
