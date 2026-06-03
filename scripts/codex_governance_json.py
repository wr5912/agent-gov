from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


PYTHON_SUFFIXES = {".py"}


class PythonMetricLike(Protocol):
    legacy_json_type_imports: set[str]
    disallowed_jsonobject_fields: set[str]
    store_jsonobject_returns: set[str]


def annotation_contains_name(annotation: ast.expr | None, name: str) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed_annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
        return annotation_contains_name(parsed_annotation, name)
    if isinstance(annotation, ast.Name):
        return annotation.id == name
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == name
    if isinstance(annotation, ast.Subscript):
        return annotation_contains_name(annotation.value, name) or annotation_contains_name(annotation.slice, name)
    if isinstance(annotation, ast.Tuple):
        return any(annotation_contains_name(item, name) for item in annotation.elts)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return annotation_contains_name(annotation.left, name) or annotation_contains_name(annotation.right, name)
    return False


def is_records_path(rel_path: str) -> bool:
    return "records" in Path(rel_path).parts and Path(rel_path).suffix in PYTHON_SUFFIXES


def is_store_path(rel_path: str) -> bool:
    return "stores" in Path(rel_path).parts and Path(rel_path).suffix in PYTHON_SUFFIXES


def is_legacy_json_type_import(rel_path: str, module: str | None, level: int) -> bool:
    if module in {"app.runtime.records.json_types", "records.json_types"}:
        return True
    return module == "json_types" and level == 1 and is_records_path(rel_path)


def is_allowed_jsonobject_field(field_name: str) -> bool:
    allowed_exact = {
        "after",
        "agent_activity",
        "applied_agent_version",
        "applied_diff",
        "attribution_summary",
        "before",
        "checks_json",
        "compensations",
        "error_json",
        "eval_case_snapshot",
        "eval_cases",
        "execution_apply_result",
        "gate_result",
        "input_json",
        "latest_regression_gate",
        "manual_restore_result",
        "metadata",
        "optimization_plan_error",
        "payload",
        "pre_execution_agent_version",
        "profile_version",
        "proposal_summary",
        "raw_output_json",
        "request_json",
        "response_json",
        "results",
        "source_summary",
        "snapshot",
        "task_context",
        "validated_output_json",
    }
    return (
        field_name in allowed_exact
        or field_name.startswith("raw_")
        or field_name.endswith("_json")
        or field_name.endswith("_snapshot")
        or field_name.endswith("_diff")
    )


def legacy_json_type_import_issue_specs(
    current_python: Mapping[str, PythonMetricLike],
    base_python: Mapping[str, PythonMetricLike],
    empty_metrics: PythonMetricLike,
) -> list[tuple[str, str, bool]]:
    issues: list[tuple[str, str, bool]] = []
    for rel_path, metrics in sorted(current_python.items()):
        base_metrics = base_python.get(rel_path, empty_metrics)
        new_imports = metrics.legacy_json_type_imports - base_metrics.legacy_json_type_imports
        existing_imports = metrics.legacy_json_type_imports & base_metrics.legacy_json_type_imports
        for name in sorted(new_imports):
            issues.append((rel_path, f"new legacy JsonObject import from records boundary: {name}", True))
        for name in sorted(existing_imports):
            issues.append((rel_path, f"existing legacy JsonObject import from records boundary not increased: {name}", False))
    return issues


def jsonobject_boundary_issue_specs(
    current_python: Mapping[str, PythonMetricLike],
    base_python: Mapping[str, PythonMetricLike],
    empty_metrics: PythonMetricLike,
) -> list[tuple[str, str, bool]]:
    issues: list[tuple[str, str, bool]] = []
    for rel_path, metrics in sorted(current_python.items()):
        base_metrics = base_python.get(rel_path, empty_metrics)
        for name in sorted(metrics.disallowed_jsonobject_fields - base_metrics.disallowed_jsonobject_fields):
            issues.append((rel_path, f"new non-boundary JsonObject record field: {name}", True))
        for name in sorted(metrics.disallowed_jsonobject_fields & base_metrics.disallowed_jsonobject_fields):
            issues.append((rel_path, f"existing non-boundary JsonObject record field not increased: {name}", False))
        for name in sorted(metrics.store_jsonobject_returns - base_metrics.store_jsonobject_returns):
            issues.append((rel_path, f"new store public method returning JsonObject: {name}", True))
    return issues
