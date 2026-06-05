from __future__ import annotations

import ast
from collections.abc import Mapping

from codex_governance_json import annotation_contains_name


def _annotation_as_expr(annotation: ast.expr | None) -> ast.expr | None:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return None
    return annotation


def _annotation_direct_name(annotation: ast.expr | None) -> str | None:
    annotation = _annotation_as_expr(annotation)
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def _annotation_union_names(annotation: ast.expr | None) -> set[str]:
    annotation = _annotation_as_expr(annotation)
    if annotation is None:
        return set()
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_union_names(annotation.left) | _annotation_union_names(annotation.right)
    return {name} if (name := _annotation_direct_name(annotation)) else set()


def annotation_is_base_json_union(annotation: ast.expr | None) -> bool:
    return {"BaseModel", "JsonObject"}.issubset(_annotation_union_names(annotation))


def annotation_is_bare_basemodel(annotation: ast.expr | None) -> bool:
    return _annotation_direct_name(annotation) == "BaseModel"


def annotation_contains_output_formatter_result_basemodel(annotation: ast.expr | None) -> bool:
    annotation = _annotation_as_expr(annotation)
    if annotation is None:
        return False
    if isinstance(annotation, ast.Subscript):
        if _annotation_direct_name(annotation.value) == "OutputFormatterResult" and annotation_contains_name(annotation.slice, "BaseModel"):
            return True
        return annotation_contains_output_formatter_result_basemodel(annotation.value) or annotation_contains_output_formatter_result_basemodel(
            annotation.slice
        )
    if isinstance(annotation, ast.Tuple):
        return any(annotation_contains_output_formatter_result_basemodel(item) for item in annotation.elts)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return annotation_contains_output_formatter_result_basemodel(annotation.left) or annotation_contains_output_formatter_result_basemodel(annotation.right)
    return False


def is_typed_output_runner_function(rel_path: str, function_name: str) -> bool:
    return function_name in {"run_profile_json", "_run_profile_json", "format_agent_text"} and rel_path in {
        "app/runtime/agent_job_runner.py",
        "app/runtime/claude_runtime.py",
    }


def is_typed_output_completion_function(function_name: str) -> bool:
    return function_name.endswith("_job") and "complete" in function_name


def typed_output_stage_erasure_issue_specs(
    current_python: Mapping[str, object],
    base_python: Mapping[str, object],
    empty_metrics: object,
) -> list[tuple[str, str, bool]]:
    specs: list[tuple[str, str, bool]] = []
    for rel_path, metrics in sorted(current_python.items()):
        current_erasure = set(getattr(metrics, "typed_output_stage_erasure", set()))
        base_erasure = set(getattr(base_python.get(rel_path, empty_metrics), "typed_output_stage_erasure", set()))
        for name in sorted(current_erasure - base_erasure):
            specs.append((rel_path, f"new typed-output stage erasure: {name}", True))
    return specs
