from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

JsonObject = dict[str, object]


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(JsonObject, value)


def evaluate_policy(*, coverage_data: JsonObject | None, policy: JsonObject, repo_root: Path) -> list[str]:
    errors: list[str] = []
    if coverage_data is not None:
        errors.extend(evaluate_coverage(coverage_data, policy))
    errors.extend(validate_main_flow_manifest(policy, repo_root=repo_root))
    return errors


def evaluate_coverage(coverage_data: JsonObject, policy: JsonObject) -> list[str]:
    coverage_policy = _mapping(policy.get("coverage"), "coverage")
    global_policy = _mapping(coverage_policy.get("global"), "coverage.global")
    totals = _mapping(coverage_data.get("totals"), "coverage_json.totals")
    errors: list[str] = []
    errors.extend(_check_summary_thresholds("global", totals, global_policy))
    files_policy = coverage_policy.get("files", [])
    if not isinstance(files_policy, list):
        return [*errors, "coverage.files must be a list"]
    files = _mapping(coverage_data.get("files"), "coverage_json.files")
    for item in files_policy:
        if not isinstance(item, dict):
            errors.append("coverage.files entries must be objects")
            continue
        path = _required_str(item, "path", "coverage.files entry", errors)
        if not path:
            continue
        file_data = files.get(path)
        if not isinstance(file_data, dict):
            errors.append(f"coverage file {path} is missing from coverage JSON")
            continue
        errors.extend(_check_summary_thresholds(path, _mapping(file_data.get("summary"), f"coverage_json.files.{path}.summary"), item))
    return errors


def validate_main_flow_manifest(policy: JsonObject, *, repo_root: Path) -> list[str]:
    flows = policy.get("main_flows")
    if not isinstance(flows, list) or not flows:
        return ["main_flows must be a non-empty list"]
    frontend_scripts = _frontend_package_scripts(repo_root)
    errors: list[str] = []
    for flow_index, flow in enumerate(flows):
        if not isinstance(flow, dict):
            errors.append(f"main_flows[{flow_index}] must be an object")
            continue
        flow_id = _required_str(flow, "id", f"main_flows[{flow_index}]", errors)
        scenarios = flow.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            errors.append(f"main flow {flow_id or flow_index} must define non-empty scenarios")
            continue
        for scenario_index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                errors.append(f"main flow {flow_id} scenario {scenario_index} must be an object")
                continue
            scenario_name = _required_str(scenario, "id", f"main flow {flow_id} scenario {scenario_index}", errors)
            pytest_nodes = _string_list(scenario.get("pytest"), f"{flow_id}.{scenario_name}.pytest", errors)
            ui_scripts = _string_list(scenario.get("ui_scripts"), f"{flow_id}.{scenario_name}.ui_scripts", errors)
            if not pytest_nodes and not ui_scripts:
                errors.append(f"main flow scenario {flow_id}.{scenario_name} has no pytest or ui_scripts binding")
            for nodeid in pytest_nodes:
                errors.extend(_validate_pytest_nodeid(nodeid, repo_root=repo_root))
            for script_name in ui_scripts:
                if script_name not in frontend_scripts:
                    errors.append(f"frontend script {script_name} referenced by {flow_id}.{scenario_name} is not defined")
    return errors


def main_flow_test_bindings(policy: JsonObject) -> tuple[list[str], list[str]]:
    pytest_nodes: list[str] = []
    ui_scripts: list[str] = []
    flows = policy.get("main_flows", [])
    if not isinstance(flows, list):
        return pytest_nodes, ui_scripts
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        scenarios = flow.get("scenarios", [])
        if not isinstance(scenarios, list):
            continue
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            for nodeid in scenario.get("pytest", []) if isinstance(scenario.get("pytest"), list) else []:
                if isinstance(nodeid, str) and nodeid not in pytest_nodes:
                    pytest_nodes.append(nodeid)
            for script_name in scenario.get("ui_scripts", []) if isinstance(scenario.get("ui_scripts"), list) else []:
                if isinstance(script_name, str) and script_name not in ui_scripts:
                    ui_scripts.append(script_name)
    return pytest_nodes, ui_scripts


def _check_summary_thresholds(name: str, summary: JsonObject, policy: JsonObject) -> list[str]:
    errors: list[str] = []
    line_percent = _number(summary.get("percent_covered"), f"{name}.percent_covered", errors)
    branch_percent = _branch_percent(summary, f"{name}.branch_percent", errors)
    min_line = _number(policy.get("line_percent_min", 0), f"{name}.line_percent_min", errors)
    min_branch = _number(policy.get("branch_percent_min", 0), f"{name}.branch_percent_min", errors)
    if line_percent + 0.00001 < min_line:
        errors.append(f"{name} line coverage {line_percent:.2f}% is below required {min_line:.2f}%")
    if branch_percent + 0.00001 < min_branch:
        errors.append(f"{name} branch coverage {branch_percent:.2f}% is below required {min_branch:.2f}%")
    return errors


def _branch_percent(summary: JsonObject, context: str, errors: list[str]) -> float:
    num_branches = _number(summary.get("num_branches", 0), f"{context}.num_branches", errors)
    missing_branches = _number(summary.get("missing_branches", 0), f"{context}.missing_branches", errors)
    if num_branches <= 0:
        return 100.0
    return ((num_branches - missing_branches) / num_branches) * 100


def _frontend_package_scripts(repo_root: Path) -> set[str]:
    package_json = repo_root / "frontend" / "package.json"
    if not package_json.exists():
        return set()
    data = load_json(package_json)
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return set()
    return {key for key, value in scripts.items() if isinstance(key, str) and isinstance(value, str)}


def _validate_pytest_nodeid(nodeid: str, *, repo_root: Path) -> list[str]:
    if "::" not in nodeid:
        return [f"pytest binding {nodeid} must include a test function"]
    path_text, test_part = nodeid.split("::", 1)
    path = repo_root / path_text
    if not path.exists():
        return [f"pytest binding {nodeid} references missing file {path_text}"]
    function_name = test_part.split("[", 1)[0].split("::", 1)[0]
    source = path.read_text(encoding="utf-8")
    if f"def {function_name}(" not in source and f"async def {function_name}(" not in source:
        return [f"pytest binding {nodeid} references missing test function {function_name}"]
    return []


def _mapping(value: object, context: str) -> JsonObject:
    if isinstance(value, dict):
        return cast(JsonObject, value)
    raise ValueError(f"{context} must be an object")


def _required_str(value: JsonObject, key: str, context: str, errors: list[str]) -> str:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    errors.append(f"{context}.{key} must be a non-empty string")
    return ""


def _string_list(value: object, context: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{context} must be a list")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        else:
            errors.append(f"{context}[{index}] must be a non-empty string")
    return result


def _number(value: object, context: str, errors: list[str]) -> float:
    if isinstance(value, int | float):
        return float(value)
    errors.append(f"{context} must be a number")
    return 0.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate coverage thresholds and main-flow test bindings.")
    parser.add_argument("--coverage-json", type=Path)
    parser.add_argument("--policy", type=Path, default=Path("tests/coverage_policy.json"))
    parser.add_argument("--manifest-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    policy = load_json(args.policy)
    if not args.manifest_only and args.coverage_json is None:
        print("COVERAGE_POLICY_FAIL: --coverage-json is required unless --manifest-only is set")
        return 1
    coverage_data = None if args.manifest_only else load_json(args.coverage_json)
    errors = evaluate_policy(coverage_data=coverage_data, policy=policy, repo_root=repo_root)
    if errors:
        for error in errors:
            print(f"COVERAGE_POLICY_FAIL: {error}")
        return 1
    pytest_nodes, ui_scripts = main_flow_test_bindings(policy)
    suffix = f"main_flow_pytest={len(pytest_nodes)} main_flow_ui={len(ui_scripts)}"
    if coverage_data is None:
        print(f"coverage policy OK: manifest-only {suffix}")
    else:
        totals = _mapping(coverage_data.get("totals"), "coverage_json.totals")
        branch_percent = _branch_percent(totals, "global.branch_percent", [])
        print(f"coverage policy OK: line={float(totals.get('percent_covered', 0)):.2f}% branch={branch_percent:.2f}% {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
