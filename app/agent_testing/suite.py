from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import yaml

from .legacy_generated_tests import classify_legacy_generated_test
from .schemas import AgentTestDiagnostic, AgentTestSuiteSummary


def inspect_agent_test_suite(
    workspace: Path,
    *,
    agent_id: str,
    commit_sha: str,
) -> AgentTestSuiteSummary:
    diagnostics: list[AgentTestDiagnostic] = []
    _inspect_declared_agent_id(workspace, diagnostics)
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

    test_files: list[Path] = []
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
        _validate_python(path, relative, diagnostics)

    if not test_files:
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_TEST_FILES_MISSING",
                path="tests",
                message="测试目录中没有 test_*.py。",
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
        diagnostics=diagnostics,
    )


def _validate_python(path: Path, relative: Path, diagnostics: list[AgentTestDiagnostic]) -> None:
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=relative.as_posix())
    except (OSError, UnicodeError, SyntaxError) as exc:
        diagnostics.append(
            AgentTestDiagnostic(
                level="error",
                code="AGENT_TEST_PYTHON_INVALID",
                path=relative.as_posix(),
                message=f"测试 Python 文件不可解析：{exc.__class__.__name__}: {exc}",
            )
        )
        return
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


def _inspect_declared_agent_id(workspace: Path, diagnostics: list[AgentTestDiagnostic]) -> None:
    manifest = workspace / "agent.yaml"
    if not manifest.is_file():
        return
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return
    agent = payload.get("agent") if isinstance(payload, dict) else None
    if isinstance(agent, dict) and agent.get("id"):
        diagnostics.append(
            AgentTestDiagnostic(
                level="warning",
                code="AGENT_MANIFEST_ID_IGNORED",
                path="agent.yaml",
                message="agent.yaml 中的 agent.id 不参与身份解析；权威 agent_id 来自导入 API 路径。",
            )
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
