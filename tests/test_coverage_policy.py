import json
from pathlib import Path

from scripts.check_test_coverage_policy import collect_pytest_nodeids, evaluate_policy, main_flow_test_bindings


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_coverage_policy_rejects_coverage_regression(tmp_path):
    policy = {
        "coverage": {"global": {"line_percent_min": 90.0, "branch_percent_min": 90.0}},
        "main_flows": [
            {"id": "flow", "scenarios": [{"id": "scenario", "pytest": ["tests/test_policy.py::test_main_flow"]}]}
        ],
    }
    coverage_data = {
        "totals": {
            "percent_covered": 76.0,
            "percent_statements_covered": 80.0,
            "num_branches": 10,
            "missing_branches": 4,
        },
        "files": {},
    }
    test_file = tmp_path / "tests" / "test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_main_flow():\n    assert True\n", encoding="utf-8")

    errors = evaluate_policy(coverage_data=coverage_data, policy=policy, repo_root=tmp_path)

    assert any("line coverage 80.00%" in error for error in errors)
    assert any("branch coverage 60.00%" in error for error in errors)


def test_coverage_policy_rejects_unbound_main_flow_scenario(tmp_path):
    policy = {
        "coverage": {"global": {"line_percent_min": 0, "branch_percent_min": 0}},
        "main_flows": [{"id": "flow", "scenarios": [{"id": "scenario"}]}],
    }
    coverage_data = {
        "totals": {"percent_covered": 100.0, "num_branches": 0, "missing_branches": 0},
        "files": {},
    }

    errors = evaluate_policy(coverage_data=coverage_data, policy=policy, repo_root=tmp_path)

    assert errors == ["main flow scenario flow.scenario has no pytest or ui_scripts binding"]


def test_coverage_policy_uses_statement_percent_for_line_threshold(tmp_path):
    policy = {
        "coverage": {"global": {"line_percent_min": 90.0, "branch_percent_min": 50.0}},
        "main_flows": [
            {"id": "flow", "scenarios": [{"id": "scenario", "pytest": ["tests/test_policy.py::test_main_flow"]}]}
        ],
    }
    coverage_data = {
        "totals": {
            "percent_covered": 75.0,
            "percent_statements_covered": 95.0,
            "num_branches": 10,
            "missing_branches": 5,
        },
        "files": {},
    }
    test_file = tmp_path / "tests" / "test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_main_flow():\n    assert True\n", encoding="utf-8")

    errors = evaluate_policy(coverage_data=coverage_data, policy=policy, repo_root=tmp_path)

    assert errors == []


def test_coverage_policy_accepts_pytest_and_frontend_bindings(tmp_path):
    test_file = tmp_path / "tests" / "test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_main_flow():\n    assert True\n", encoding="utf-8")
    package_json = tmp_path / "frontend" / "package.json"
    package_json.parent.mkdir()
    _write_json(package_json, {"scripts": {"verify:feedback-ui-states": "node script.mjs"}})
    policy = {
        "coverage": {"global": {"line_percent_min": 80.0, "branch_percent_min": 50.0}},
        "main_flows": [
            {
                "id": "flow",
                "scenarios": [
                    {
                        "id": "scenario",
                        "pytest": ["tests/test_policy.py::test_main_flow"],
                        "ui_scripts": ["verify:feedback-ui-states"],
                    }
                ],
            }
        ],
    }
    coverage_data = {
        "totals": {"percent_covered": 80.0, "percent_statements_covered": 82.0, "num_branches": 10, "missing_branches": 5},
        "files": {},
    }

    errors = evaluate_policy(coverage_data=coverage_data, policy=policy, repo_root=tmp_path)
    pytest_nodes, ui_scripts = main_flow_test_bindings(policy)

    assert errors == []
    assert pytest_nodes == ["tests/test_policy.py::test_main_flow"]
    assert ui_scripts == ["verify:feedback-ui-states"]


def test_collect_pytest_nodeids_rejects_unknown_parametrized_case(tmp_path):
    test_file = tmp_path / "tests" / "test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import pytest\n\n@pytest.mark.parametrize('value', [1], ids=['known'])\ndef test_case(value):\n    assert value\n",
        encoding="utf-8",
    )

    errors = collect_pytest_nodeids(
        ["tests/test_policy.py::test_case[missing]"],
        repo_root=tmp_path,
    )

    assert len(errors) == 1
    assert "pytest could not collect" in errors[0]
    assert "not found" in errors[0]
