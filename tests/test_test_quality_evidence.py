from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from scripts.run_mutation_lane import mutation_score
from scripts.test_quality.collection import CollectionResult, nodeid_digest
from scripts.test_quality.evidence import build_evidence, utc_now, validate_evidence, write_evidence
from scripts.test_quality.pytest_plugin import pytest_collection_modifyitems

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "tests/quality_policy.json"


def _write_run_artifacts(path: Path, nodeid: str) -> None:
    path.mkdir()
    (path / "junit.xml").write_text(
        '<testsuites><testsuite><testcase name="test_case">'
        f'<properties><property name="agentgov_nodeid" value="{nodeid}" /></properties>'
        "</testcase></testsuite></testsuites>\n",
        encoding="utf-8",
    )
    (path / "coverage.json").write_text('{"totals":{"percent_covered":100}}\n', encoding="utf-8")


def test_evidence_rejects_tamper_partial_and_stale_commit(tmp_path: Path) -> None:
    nodeid = "tests/test_example.py::test_case"
    artifact_dir = tmp_path / "evidence"
    _write_run_artifacts(artifact_dir, nodeid)
    collection = CollectionResult((nodeid,), nodeid_digest([nodeid]))
    started = utc_now() - timedelta(seconds=1)
    evidence = build_evidence(
        repo_root=REPO_ROOT,
        policy_path=POLICY_PATH,
        artifact_dir=artifact_dir,
        lane="main-full",
        global_collection=collection,
        selection=(nodeid,),
        command=["pytest", nodeid],
        started_at=started,
        completed_at=utc_now(),
        wall_seconds=1,
        workers=0,
        scheduler="serial",
    )
    write_evidence(evidence, artifact_dir / "evidence.json")

    assert (
        validate_evidence(
            artifact_dir=artifact_dir,
            policy_path=POLICY_PATH,
            expected_selection=(nodeid,),
        )
        == []
    )
    expected_nodes = (nodeid, "tests/test_example.py::test_missing")
    stale_errors = validate_evidence(
        artifact_dir=artifact_dir,
        policy_path=POLICY_PATH,
        expected_selection=expected_nodes,
        expected_collection=CollectionResult(expected_nodes, nodeid_digest(expected_nodes)),
        expected_sha="different-commit",
    )
    assert any("complete expected lane" in error for error in stale_errors)
    assert any("global collection" in error for error in stale_errors)
    assert any("commit mismatch" in error for error in stale_errors)

    (artifact_dir / "junit.xml").write_text(
        '<testsuites><testsuite><testcase name="test_case">'
        f'<properties><property name="agentgov_nodeid" value="{nodeid}" /></properties><skipped />'
        "</testcase></testsuite></testsuites>\n",
        encoding="utf-8",
    )
    skipped_evidence = build_evidence(
        repo_root=REPO_ROOT,
        policy_path=POLICY_PATH,
        artifact_dir=artifact_dir,
        lane="main-full",
        global_collection=collection,
        selection=(nodeid,),
        command=["pytest", nodeid],
        started_at=started,
        completed_at=utc_now(),
        wall_seconds=1,
        workers=0,
        scheduler="serial",
    )
    write_evidence(skipped_evidence, artifact_dir / "evidence.json")
    skipped_errors = validate_evidence(
        artifact_dir=artifact_dir,
        policy_path=POLICY_PATH,
        require_all_passed=True,
    )
    assert any("non-passed pytest leaves" in error for error in skipped_errors)

    (artifact_dir / "coverage.json").write_text('{"totals":{"percent_covered":0}}\n', encoding="utf-8")
    assert any("artifact hash mismatch" in error for error in validate_evidence(artifact_dir=artifact_dir, policy_path=POLICY_PATH))


def test_evidence_rejects_symlink_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "evidence"
    artifact_dir.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    (artifact_dir / "evidence.json").symlink_to(target)

    errors = validate_evidence(artifact_dir=artifact_dir, policy_path=POLICY_PATH)

    assert errors == ["evidence artifact must not be a symlink: evidence.json"]


def test_mutation_score_rejects_empty_or_inconsistent_statistics() -> None:
    with pytest.raises(ValueError, match="zero mutants"):
        mutation_score({"total": 0, "killed": 0})
    with pytest.raises(ValueError, match="inconsistent"):
        mutation_score({"total": 2, "killed": 3})

    assert mutation_score({"total": 20, "killed": 17}) == (20, 17, 85.0)


def test_pytest_plugin_records_exact_leaf_nodeid() -> None:
    item = type("Item", (), {"nodeid": "tests/test_policy.py::test_case[param]", "user_properties": []})()

    pytest_collection_modifyitems([item])

    assert item.user_properties == [("agentgov_nodeid", "tests/test_policy.py::test_case[param]")]
