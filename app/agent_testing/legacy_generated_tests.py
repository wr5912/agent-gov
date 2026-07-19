from __future__ import annotations

import ast
from typing import Literal

LEGACY_GENERATED_TEST_MARKER = "# Generated from a confirmed AgentGov regression test design."
LegacyGeneratedTestClassification = Literal["not_marked", "archivable_weak_test", "unknown_marked_test"]


def classify_legacy_generated_test(source: str, *, filename: str) -> LegacyGeneratedTestClassification:
    if LEGACY_GENERATED_TEST_MARKER not in source:
        return "not_marked"
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, UnicodeError):
        return "unknown_marked_test"
    test_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    if test_functions and all(_is_legacy_generated_test_function(function) for function in test_functions):
        return "archivable_weak_test"
    return "unknown_marked_test"


def _is_legacy_generated_test_function(function: ast.FunctionDef) -> bool:
    assigned_names = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name)}
    agent_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "agent"
        and node.func.attr in {"invoke", "run"}
    ]
    static_checkpoint_assertions = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name) and node.test.func.id == "all"
    ]
    return {"expected_behavior", "checkpoints", "result"}.issubset(assigned_names) and bool(agent_calls) and bool(static_checkpoint_assertions)
