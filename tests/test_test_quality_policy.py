from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.run_mutation_lane import mutation_score
from scripts.test_quality.collection import CollectionResult, collect_pytest_nodeids, nodeid_digest
from scripts.test_quality.coverage import CoverageSnapshot, compare_coverage_snapshots, evaluate_coverage
from scripts.test_quality.evidence import build_evidence, utc_now, validate_evidence, write_evidence
from scripts.test_quality.impact import select_impacted_nodes
from scripts.test_quality.models import PortfolioPolicy, PortfolioRule, QualityPolicy
from scripts.test_quality.policy import classify_nodes, load_quality_policy, main_flow_bindings, validate_quality_policy
from scripts.test_quality.pytest_plugin import pytest_collection_modifyitems

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "tests/quality_policy.json"


def _policy() -> QualityPolicy:
    return load_quality_policy(POLICY_PATH)


def _write_run_artifacts(path: Path, nodeid: str) -> None:
    path.mkdir()
    (path / "junit.xml").write_text(
        '<testsuites><testsuite><testcase name="test_case">'
        f'<properties><property name="agentgov_nodeid" value="{nodeid}" /></properties>'
        "</testcase></testsuite></testsuites>\n",
        encoding="utf-8",
    )
    (path / "coverage.json").write_text('{"totals":{"percent_covered":100}}\n', encoding="utf-8")


def test_quality_policy_schema_forbids_unknown_fields() -> None:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw["portfolio"]["rules"][0]["classification"]["legacy_bucket"] = "slow"

    with pytest.raises(ValidationError, match="legacy_bucket"):
        QualityPolicy.model_validate(raw)


def test_quality_policy_rejects_coverage_regression() -> None:
    coverage = {
        "totals": {
            "percent_statements_covered": 50.0,
            "num_branches": 10,
            "missing_branches": 6,
        },
        "files": {},
    }

    errors = evaluate_coverage(coverage, _policy().coverage)

    assert any("line coverage 50.00%" in error for error in errors)
    assert any("branch coverage 40.00%" in error for error in errors)


def test_parallel_coverage_comparison_allows_only_bounded_order_noise() -> None:
    reference = CoverageSnapshot(1000, 200, 10, 80.0, 70.0)
    bounded = CoverageSnapshot(1000, 200, 10, 80.05, 69.95)
    drifted = CoverageSnapshot(1001, 200, 10, 80.2, 70.0)

    bounded_errors, _, _ = compare_coverage_snapshots(reference, bounded, max_delta_percentage_points=0.1)
    drifted_errors, _, _ = compare_coverage_snapshots(reference, drifted, max_delta_percentage_points=0.1)

    assert bounded_errors == []
    assert "coverage instrumentation universe mismatch" in drifted_errors
    assert any("coverage delta exceeds 0.10" in error for error in drifted_errors)


def test_portfolio_requires_exactly_one_effective_classification() -> None:
    policy = _policy()
    nodeid = "tests/test_agent_config_files.py::test_example"
    collection = CollectionResult((nodeid,), nodeid_digest([nodeid]))
    duplicate = PortfolioRule(
        id="overlap",
        selectors=["tests/test_agent_*.py"],
        classification=policy.portfolio.rules[0].classification,
    )
    overlapping = policy.model_copy(update={"portfolio": PortfolioPolicy(rules=[*policy.portfolio.rules, duplicate])})

    classifications, errors = classify_nodes(overlapping, collection)

    assert classifications == {}
    assert any("matched: agent-lifecycle-suite, overlap" in error for error in errors)


def test_repository_quality_policy_covers_every_collected_leaf() -> None:
    validation = validate_quality_policy(_policy(), repo_root=REPO_ROOT)

    assert validation.errors == ()
    assert len(validation.collection.nodeids) == len(validation.classifications)
    assert len(validation.collection.nodeids) >= 1000
    assert {classification.owner for classification in validation.classifications.values()} == {
        "agent-lifecycle",
        "engineering-governance",
        "frontend-experience",
        "improvement-governance",
        "integrations",
        "runtime-platform",
        "security-response",
    }


def test_main_flow_bindings_are_deduplicated() -> None:
    pytest_selectors, ui_scripts = main_flow_bindings(_policy())

    assert len(pytest_selectors) == len(set(pytest_selectors))
    assert len(ui_scripts) == len(set(ui_scripts))
    assert "verify:design-parity" in ui_scripts


def test_impact_selection_is_targeted_and_unknown_paths_fail_closed() -> None:
    policy = _policy()
    nodes = (
        "tests/test_state_machines.py::test_transition",
        "tests/test_runtime_db.py::test_schema",
        "tests/test_improvement_api.py::test_create",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))
    targeted = select_impacted_nodes(
        changed_paths=["app/runtime/state_machines.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    unknown = select_impacted_nodes(
        changed_paths=["app/new_unmapped_module.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )

    assert targeted.mode == "impacted"
    assert targeted.nodeids == (
        "tests/test_runtime_db.py::test_schema",
        "tests/test_state_machines.py::test_transition",
    )
    assert unknown.mode == "full"
    assert unknown.nodeids == tuple(sorted(nodes))


def test_tia_git_diff_includes_deleted_paths() -> None:
    selector = (REPO_ROOT / "scripts/select_impacted_tests.py").read_text(encoding="utf-8")

    assert "--diff-filter=ACMRD" in selector


def test_collect_pytest_nodeids_rejects_unknown_parametrized_case(tmp_path: Path) -> None:
    test_file = tmp_path / "tests/test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import pytest\n\n@pytest.mark.parametrize('value', [1], ids=['known'])\ndef test_case(value):\n    assert value\n",
        encoding="utf-8",
    )

    errors = collect_pytest_nodeids(["tests/test_policy.py::test_case[missing]"], repo_root=tmp_path)

    assert len(errors) == 1
    assert "could not collect" in errors[0]


def test_evidence_rejects_tamper_partial_and_stale_run(tmp_path: Path) -> None:
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
    stale_errors = validate_evidence(
        artifact_dir=artifact_dir,
        policy_path=POLICY_PATH,
        expected_selection=(nodeid, "tests/test_example.py::test_missing"),
        expected_collection=CollectionResult((nodeid, "tests/test_example.py::test_missing"), nodeid_digest([nodeid, "tests/test_example.py::test_missing"])),
        expected_run_id="different-run",
        expected_job="different-job",
    )
    assert any("complete expected lane" in error for error in stale_errors)
    assert any("global collection" in error for error in stale_errors)
    assert any("GitHub run mismatch" in error for error in stale_errors)
    assert any("GitHub job mismatch" in error for error in stale_errors)

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
