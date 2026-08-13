from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .legacy_generated_tests import classify_legacy_generated_test
from .schemas import AgentTestDiagnostic, AgentTestSuiteSummary


@dataclass(frozen=True)
class _InspectedTestFiles:
    test_files: tuple[Path, ...]
    live_test_files: tuple[str, ...]


def inspect_agent_test_suite(
    workspace: Path,
    *,
    agent_id: str,
    commit_sha: str,
) -> AgentTestSuiteSummary:
    diagnostics: list[AgentTestDiagnostic] = []
    tests_dir = workspace / "tests"
    if not tests_dir.is_dir() or tests_dir.is_symlink():
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_TESTS_DIRECTORY_MISSING",
                path="tests",
                message="Workspace 未提供 tests/；允许导入，但不能作为普通发布的可测试版本。",
            )
        )
        return AgentTestSuiteSummary(
            agent_id=agent_id,
            commit_sha=commit_sha,
            tests_directory_present=False,
            readme_present=False,
            test_file_count=0,
            diagnostics=diagnostics,
        )

    readme_present = (tests_dir / "README.md").is_file()
    if not readme_present:
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_TESTS_README_MISSING",
                path="tests/README.md",
                message="测试目录缺少开发者维护说明。",
            )
        )

    inspected = _inspect_test_files(workspace, tests_dir, diagnostics)
    test_files = inspected.test_files
    live_test_files = inspected.live_test_files

    if not test_files:
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_TEST_FILES_MISSING",
                path="tests",
                message="测试目录中没有 test_*.py。",
            )
        )
    if live_test_files:
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_TEST_LIVE_FIXTURE_REQUIRES_P1",
                path=live_test_files[0],
                message="测试套件使用 agent live fixture；P0 静态 lane 必须整套拒绝并交由 P1 live lane 执行。",
            )
        )
    digest = _suite_digest(workspace, tests_dir) if test_files else None
    return AgentTestSuiteSummary(
        agent_id=agent_id,
        commit_sha=commit_sha,
        tests_directory_present=True,
        readme_present=readme_present,
        test_file_count=len(test_files),
        test_files=[path.relative_to(workspace).as_posix() for path in test_files],
        suite_digest=digest,
        requires_live_agent=bool(live_test_files),
        live_test_files=live_test_files,
        diagnostics=diagnostics,
    )


def _inspect_test_files(
    workspace: Path,
    tests_dir: Path,
    diagnostics: list[AgentTestDiagnostic],
) -> _InspectedTestFiles:
    test_files: list[Path] = []
    live_test_files: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        relative = path.relative_to(workspace)
        if path.parent != tests_dir:
            diagnostics.append(
                AgentTestDiagnostic(
                    level="error",
                    code="AGENT_TEST_LAYOUT_NESTED",
                    path=relative.as_posix(),
                    message="第一阶段只接受 tests/ 下的扁平 Python 测试文件。",
                )
            )
        if path.name.startswith("test_") and path.parent == tests_dir:
            test_files.append(path)
        if _validate_python(path, relative, diagnostics):
            live_test_files.append(relative.as_posix())
    return _InspectedTestFiles(test_files=tuple(test_files), live_test_files=tuple(live_test_files))


def _validate_python(path: Path, relative: Path, diagnostics: list[AgentTestDiagnostic]) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source, filename=relative.as_posix())
    except (OSError, UnicodeError, SyntaxError) as exc:
        diagnostics.append(
            AgentTestDiagnostic(
                level="error",
                code="AGENT_TEST_PYTHON_INVALID",
                path=relative.as_posix(),
                message=f"测试 Python 文件不可解析：{exc.__class__.__name__}: {exc}",
            )
        )
        return False
    legacy_classification = classify_legacy_generated_test(source, filename=relative.as_posix())
    if legacy_classification != "not_marked":
        diagnostics.append(
            AgentTestDiagnostic(
                level="error",
                code="AGENT_TEST_LEGACY_GENERATED_ASSERTION",
                path=relative.as_posix(),
                message=(
                    "检测到旧平台生成测试标记；该文件可能只验证非空响应或静态检查点，不能作为发布证据。"
                    "请运行 Workspace 测试资产迁移；结构不明时必须人工审查并移除旧标记。"
                ),
            )
        )
    return _uses_live_agent_fixture(module)


def _uses_live_agent_fixture(module: ast.Module) -> bool:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if any(argument.arg == "agent" for argument in arguments):
                return True
        if isinstance(node, ast.Call) and _is_live_fixture_call(node):
            return True
    return False


def _is_live_fixture_call(node: ast.Call) -> bool:
    function_name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
    return function_name in {"usefixtures", "getfixturevalue"} and any(
        isinstance(argument, ast.Constant) and argument.value == "agent" for argument in node.args
    )


def _suite_digest(workspace: Path, tests_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in tests_dir.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(workspace).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
